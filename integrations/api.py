"""Public REST API (v1) for partners: aggregators, channel managers, websites, ERPs.

Authentication: ``Authorization: Bearer hk_live_…`` (or ``X-Api-Key``). Every key belongs to
exactly one branch, so a key can only ever see and change that branch's menu, orders,
rooms and bookings. Errors are ``{"error": {"code": "...", "message": "..."}}``.
"""
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from integrations import payloads
from integrations.models import ApiKey, Connection, ExternalBookingLink, ExternalOrderLink

RATE_LIMIT = getattr(settings, 'API_RATE_LIMIT_PER_MINUTE', 120)

STATUS_IN = {
    'accepted': 'P', 'placed': 'P', 'preparing': 'PR', 'ready': 'READY', 'served': 'S', 'dispatched': 'S',
    'picked_up': 'S', 'delivered': 'COMPLETED', 'completed': 'COMPLETED', 'cancelled': 'C', 'rejected': 'C',
}


def error(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def _client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')) or None


def _raw_key(request):
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.META.get('HTTP_X_API_KEY', '').strip()


def endpoint(scope, methods=('GET',)):
    """Authenticate the key, check its scope and rate limit, and pass it to the view."""
    def deco(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, 'method_not_allowed', f'Use {" or ".join(methods)}.')
            key = ApiKey.authenticate(_raw_key(request))
            if key is None:
                return error(401, 'unauthorized', 'Missing, invalid, expired or revoked API key.')
            if not key.has_scope(scope):
                return error(403, 'insufficient_scope', f'This key does not have the "{scope}" permission.', required_scope=scope)
            bucket = f'api-rl:{key.id}:{int(timezone.now().timestamp() // 60)}'
            cache.add(bucket, 0, 70)
            try:
                used = cache.incr(bucket)
            except ValueError:
                used = 1
            if used > RATE_LIMIT:
                resp = error(429, 'rate_limited', f'More than {RATE_LIMIT} requests a minute. Slow down and retry.')
                resp['Retry-After'] = '60'
                return resp
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now(), last_used_ip=_client_ip(request), request_count=F('request_count') + 1)
            if key.connection_id:
                Connection.objects.filter(pk=key.connection_id).update(last_event_at=timezone.now())
            request.api_key = key
            if request.method in ('POST', 'PATCH', 'PUT'):
                try:
                    request.json = json.loads(request.body or b'{}')
                except (ValueError, UnicodeDecodeError):
                    if 'json' in (request.content_type or ''):
                        return error(400, 'invalid_json', 'The request body is not valid JSON.')
                    request.json = {}  # empty or form-encoded body on an action endpoint
                if not isinstance(request.json, dict):
                    return error(400, 'invalid_json', 'The request body must be a JSON object.')
            resp = view(request, key, *args, **kwargs)
            resp['X-RateLimit-Limit'] = str(RATE_LIMIT)
            resp['X-RateLimit-Remaining'] = str(max(0, RATE_LIMIT - used))
            return resp
        return wrapper
    return deco


def _dec(value, default=None):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _source(key, body_source=''):
    if key.connection_id:
        return key.connection.provider
    return (body_source or 'api').strip().lower()[:30]


# --------------------------------------------------------------------------
# Branch & menu
# --------------------------------------------------------------------------

@endpoint('menu:read')
def branch_info(request, key):
    b = key.branch
    return JsonResponse({
        'branch': {'id': b.id, 'name': b.name, 'slug': b.slug, 'phone': b.phone, 'city': b.city,
                   'restaurant': getattr(b, 'has_restaurant_module', True), 'hotel': getattr(b, 'has_hotel_module', False)},
        'business': {'id': key.restaurant_id, 'name': key.restaurant.name},
        'key': {'name': key.name, 'scopes': key.scopes, 'channel': key.connection.provider if key.connection_id else None},
    })


def _menu_qs(key):
    from core.models import MenuItem
    return (MenuItem.objects.filter(restaurant=key.restaurant).filter(Q(branch=key.branch) | Q(branch__isnull=True))
            .select_related('category'))


@endpoint('menu:read')
def menu(request, key):
    items = _menu_qs(key)
    if request.GET.get('available') in ('true', '1'):
        items = items.filter(availability=True)
    channel = request.GET.get('channel')
    out_items = [payloads.menu_item(m) for m in items.order_by('category__name', 'name')]
    if channel:
        out_items = [i for i in out_items if not i['channels'] or channel in i['channels']]
    cats = {}
    for i in out_items:
        if i['category_id'] and i['category_id'] not in cats:
            cats[i['category_id']] = {'id': i['category_id'], 'name': i['category']}
    return JsonResponse({'categories': list(cats.values()), 'items': out_items, 'count': len(out_items)})


@endpoint('menu:write', methods=('PATCH', 'POST'))
def menu_item_update(request, key, item_id):
    item = _menu_qs(key).filter(pk=item_id).first()
    if not item:
        return error(404, 'not_found', 'No such menu item in this branch.')
    body, fields = request.json, []
    if 'available' in body:
        if not isinstance(body['available'], bool):
            return error(422, 'invalid_field', '"available" must be true or false.', field='available')
        item.availability = body['available']
        fields.append('availability')
    for name, attr in (('price', 'price'), ('aggregator_price', 'markup_price')):
        if name in body:
            val = _dec(body[name])
            if val is None or val < 0:
                return error(422, 'invalid_field', f'"{name}" must be a positive number.', field=name)
            setattr(item, attr, val)
            fields.append(attr)
    if not fields:
        return error(422, 'nothing_to_update', 'Send "available", "price" or "aggregator_price".')
    item.save(update_fields=fields + ['updated_at'])
    return JsonResponse({'item': payloads.menu_item(item)})


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def _orders_qs(key):
    from core.models import Order
    return Order.objects.filter(restaurant=key.restaurant, branch=key.branch).exclude(status='DRAFT')


@endpoint('orders:read')
def orders(request, key):
    qs = _orders_qs(key)
    since = parse_datetime(request.GET.get('since', '') or '')
    if since:
        qs = qs.filter(order_time__gte=since)
    status = request.GET.get('status')
    if status:
        codes = [c for c, label in payloads.ORDER_STATUS.items() if label == status]
        qs = qs.filter(status__in=codes or [status])
    source = request.GET.get('source')
    if source == 'pos':
        qs = qs.filter(external_link__isnull=True)
    elif source:
        qs = qs.filter(external_link__source=source)
    try:
        limit = max(1, min(int(request.GET.get('limit', 50)), 200))
    except ValueError:
        limit = 50
    rows = list(qs.order_by('-order_time')[:limit])
    return JsonResponse({'orders': [payloads.order(o) for o in rows], 'count': len(rows)})


def _match_item(key, line, menu_cache):
    for field, lookup in (('menu_item_id', 'pk'), ('sku', 'sku__iexact'), ('short_code', 'short_code__iexact'), ('name', 'name__iexact')):
        value = line.get(field)
        if value not in (None, ''):
            cache_key = (field, str(value).lower())
            if cache_key not in menu_cache:
                menu_cache[cache_key] = _menu_qs(key).filter(**{lookup: value}).first()
            return menu_cache[cache_key], f'{field}={value}'
    return None, 'no identifier'


@endpoint('orders:write', methods=('POST',))
def create_order(request, key):
    from core.models import Order, OrderItem
    body = request.json
    external_id = str(body.get('external_id') or '').strip()[:120]
    if not external_id:
        return error(422, 'missing_field', '"external_id" (your order ID) is required so retries never duplicate an order.', field='external_id')
    source = _source(key, body.get('source', ''))

    existing = ExternalOrderLink.objects.filter(branch=key.branch, source=source, external_id=external_id).select_related('order').first()
    if existing:
        resp = JsonResponse({'order': payloads.order(existing.order), 'duplicate': True}, status=200)
        resp['Idempotent-Replay'] = 'true'
        return resp

    lines = body.get('items')
    if not isinstance(lines, list) or not lines:
        return error(422, 'missing_field', '"items" must be a non-empty list.', field='items')
    resolved, unknown, menu_cache = [], [], {}
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            return error(422, 'invalid_field', f'items[{idx}] must be an object.', field=f'items[{idx}]')
        item, ident = _match_item(key, line, menu_cache)
        qty = line.get('quantity', 1)
        if not isinstance(qty, int) or qty <= 0:
            return error(422, 'invalid_field', f'items[{idx}].quantity must be a positive whole number.', field=f'items[{idx}].quantity')
        if item is None:
            unknown.append({'index': idx, 'match': ident})
            continue
        price = _dec(line.get('unit_price'))
        if price is None:
            price = item.markup_price if item.markup_price and source != 'pos' else item.price
        resolved.append((item, qty, price, str(line.get('notes') or '')[:500]))
    if unknown:
        return error(422, 'unknown_items', 'Some items do not match this branch\'s menu. Match by menu_item_id, sku, short_code or exact name.', items=unknown)
    unavailable = [i.name for i, *_ in resolved if not i.availability]
    if unavailable and not body.get('allow_unavailable'):
        return error(409, 'items_unavailable', 'Some items are switched off at this branch.', items=unavailable)

    customer = body.get('customer') or {}
    payment = body.get('payment') or {}
    paid = str(payment.get('status', '')).lower() == 'paid'
    order_type = body.get('order_type') if body.get('order_type') in dict(Order.OrderType.choices) else Order.OrderType.DELIVERY

    try:
        with transaction.atomic():
            order = Order.objects.create(
                restaurant=key.restaurant, branch=key.branch, status=Order.OrderStatus.PENDING, order_type=order_type,
                customer_name=(str(customer.get('name') or '').strip() or f'{source.title()} guest')[:200],
                phone=str(customer.get('phone') or '')[:20],
                payment_method=Order.PaymentMethod.ONLINE if paid else Order.PaymentMethod.CASH,
                payment_status=Order.PaymentStatus.UNPAID,
                notes='\n'.join(filter(None, [f'{source.title()} order {external_id}', str(body.get('notes') or '')[:1000],
                                              str(customer.get('address') or '')[:500]])),
            )
            for item, qty, price, notes in resolved:
                OrderItem.objects.create(order=order, menu_item=item, quantity=qty, unit_price=price, notes=notes)
            order.update_total()
            if paid:
                order.paid_amount, order.due_amount = order.total_amount, Decimal('0')
                order.payment_status = Order.PaymentStatus.PAID
                order.payments = [{'method': source, 'amount': float(order.total_amount), 'reference': external_id}]
                order.save()
            commission = Decimal('0')
            if key.connection_id and key.connection.commission_pct:
                commission = (order.total_amount * key.connection.commission_pct / 100).quantize(Decimal('0.01'))
            ExternalOrderLink.objects.create(branch=key.branch, connection=key.connection, source=source, external_id=external_id,
                                             order=order, commission_amount=commission, raw=body)
    except IntegrityError:
        existing = ExternalOrderLink.objects.filter(branch=key.branch, source=source, external_id=external_id).select_related('order').first()
        if existing:
            return JsonResponse({'order': payloads.order(existing.order), 'duplicate': True}, status=200)
        raise
    try:
        order.deduct_inventory()
    except Exception:
        pass
    from notifications.service import notify_new_order
    if source in ('zomato', 'swiggy'):
        _notify_delivery_order(order, source)
    else:
        notify_new_order(order, source if source != 'pos' else 'api')
    return JsonResponse({'order': payloads.order(order), 'duplicate': False}, status=201)


def _notify_delivery_order(order, source):
    from notifications.service import notify, order_data
    d = order_data(order)
    notify(order.restaurant, 'delivery_new', f'New {source.title()} order {d["order_number"]}',
           body=f'{d["customer_name"]} · {d["items_count"]} items · ₹{d["total_amount"]:,.0f}', branch=order.branch,
           source=source, order=order, dedupe_key=f'order_new:{order.pk}', data=d)


@csrf_exempt
def bookings_collection(request):
    """GET lists bookings (bookings:read); POST creates one (bookings:write)."""
    if request.method == 'POST':
        return create_booking(request)
    return bookings(request)


@csrf_exempt
def orders_collection(request):
    """GET lists orders (orders:read); POST creates one (orders:write)."""
    if request.method == 'POST':
        return create_order(request)
    return orders(request)


@endpoint('orders:read')
def order_detail(request, key, order_id):
    o = _orders_qs(key).filter(pk=order_id).first()
    if not o:
        return error(404, 'not_found', 'No such order in this branch.')
    return JsonResponse({'order': payloads.order(o)})


@endpoint('orders:write', methods=('POST', 'PATCH'))
def order_status(request, key, order_id):
    o = _orders_qs(key).filter(pk=order_id).first()
    if not o:
        return error(404, 'not_found', 'No such order in this branch.')
    wanted = str(request.json.get('status', '')).lower()
    code = STATUS_IN.get(wanted)
    if not code:
        return error(422, 'invalid_field', f'"status" must be one of: {", ".join(sorted(STATUS_IN))}.', field='status')
    if o.status in ('C', 'COMPLETED') and o.status != code:
        return error(409, 'order_closed', f'The order is already {payloads.ORDER_STATUS[o.status]}.')
    o.status = code
    reason = str(request.json.get('reason') or '').strip()
    fields = ['status']
    if reason:
        o.notes = (o.notes + '\n' if o.notes else '') + f'{wanted.title()}: {reason}'[:500]
        fields.append('notes')
    o.save(update_fields=fields)
    return JsonResponse({'order': payloads.order(o)})


# --------------------------------------------------------------------------
# Rooms & bookings (hotel channel managers / OTAs)
# --------------------------------------------------------------------------

def _dates(request_or_body, get=True):
    src = request_or_body
    ci, co = parse_date(str(src.get('check_in') or '')), parse_date(str(src.get('check_out') or ''))
    if not ci or not co:
        return None, None, error(422, 'missing_field', '"check_in" and "check_out" are required (YYYY-MM-DD).')
    if co <= ci:
        return None, None, error(422, 'invalid_field', '"check_out" must be after "check_in".')
    if (co - ci).days > 90:
        return None, None, error(422, 'invalid_field', 'Stays longer than 90 nights are not accepted through the API.')
    return ci, co, None


def _rooms_conn(key):
    """The channel connection behind this key, when it is a rooms channel."""
    conn = key.connection if key.connection_id else None
    return conn if conn is not None and Connection.KIND.get(conn.provider) in ('rooms', 'any') else None


@endpoint('rooms:read')
def room_availability(request, key):
    """Room types bookable for a stay, priced night by night with this channel's adjustment."""
    from hotel import pricing
    from hotel.models import HotelSettings, Room, RoomCategory
    from integrations import channels
    ci, co, err = _dates(request.GET)
    if err:
        return err
    conn = _rooms_conn(key)
    maps = channels.mappings(conn)
    settings_obj = HotelSettings.get_settings(key.branch)
    out = []
    for cat in RoomCategory.objects.filter(branch=key.branch, is_active=True).order_by('base_price_per_night'):
        m = maps.get(cat.id)
        if maps and m is None:
            continue
        rooms = Room.objects.filter(category=cat, is_active=True)
        free = pricing.free_rooms(rooms, key.branch, ci, co).count()
        if m is not None and m.allotment is not None:
            free = min(free, m.allotment)
        q = pricing.quote(cat, ci, co, settings_obj)
        closed_reason = q['restriction'] or ('Closed on this channel.' if m and m.is_closed else '')
        nightly = [{'date': n['date'].isoformat(), 'price': str(channels.channel_price(n['price'], m))} for n in q['nights']]
        total = sum((Decimal(n['price']) for n in nightly), Decimal('0'))
        out.append({'category_id': cat.id, 'name': cat.name, 'external_room_id': m.external_room_id if m else '',
                    'rate_per_night': nightly[0]['price'] if nightly else str(cat.base_price_per_night),
                    'nightly_rates': nightly, 'total_price': str(total), 'tax_inclusive': cat.price_is_tax_inclusive,
                    'max_occupancy': cat.max_occupancy, 'total_rooms': rooms.count(),
                    'available': 0 if closed_reason else max(0, free), 'closed_reason': closed_reason})
    return JsonResponse({'check_in': ci.isoformat(), 'check_out': co.isoformat(), 'nights': (co - ci).days, 'room_types': out})


@endpoint('rooms:read')
def rates(request, key):
    """ARI calendar: price, rooms left, minimum stay and closed flag for every night (up to 90 days)."""
    from integrations import channels
    start = parse_date(request.GET.get('from') or '') or timezone.localdate()
    end = parse_date(request.GET.get('to') or '') or (start + timedelta(days=30))
    if end <= start:
        return error(422, 'invalid_field', '"to" must be after "from".')
    if (end - start).days > channels.MAX_DAYS:
        return error(422, 'invalid_field', f'Ask for at most {channels.MAX_DAYS} days at a time.')
    return JsonResponse({'from': start.isoformat(), 'to': end.isoformat(), 'currency': 'INR',
                         'room_types': channels.ari(key.branch, start, end, _rooms_conn(key))})


def _notify_rooms(branch, kind, title, body, booking=None, level='info', sound=True):
    try:
        from notifications.service import notify
        url = f'/hotel-admin/{branch.restaurant.slug}/bookings/{booking.pk}/' if booking is not None else ''
        notify(branch.restaurant, kind, title, body=body, branch=branch, level=level, source='channel', url=url, sound=sound)
    except Exception:
        pass


def _bookings_qs(key):
    from hotel.models import RoomBooking
    return RoomBooking.objects.filter(branch=key.branch).select_related('room__category')


@endpoint('bookings:read')
def bookings(request, key):
    qs = _bookings_qs(key)
    since = parse_datetime(request.GET.get('since', '') or '')
    if since:
        qs = qs.filter(updated_at__gte=since)
    rows = list(qs.order_by('-created_at')[:200])
    return JsonResponse({'bookings': [payloads.booking(b) for b in rows], 'count': len(rows)})


@endpoint('bookings:write', methods=('POST',))
def create_booking(request, key):
    from hotel import pricing
    from hotel.models import HotelSettings, Room, RoomBooking
    from integrations import channels
    body = request.json
    external_id = str(body.get('external_id') or '').strip()[:120]
    if not external_id:
        return error(422, 'missing_field', '"external_id" (your reservation ID) is required.', field='external_id')
    source = _source(key, body.get('source', ''))
    existing = ExternalBookingLink.objects.filter(branch=key.branch, source=source, external_id=external_id).select_related('booking').first()
    if existing:
        resp = JsonResponse({'booking': payloads.booking(existing.booking), 'duplicate': True})
        resp['Idempotent-Replay'] = 'true'
        return resp
    ci, co, err = _dates(body)
    if err:
        return err
    conn = _rooms_conn(key)
    cat = channels.category_for(conn, key.branch, body)
    if not cat:
        return error(422, 'unknown_room_type', 'Send a "category_id", a mapped "external_room_id" or a "room_type" that exists at this branch.')
    guest = body.get('guest') or {}
    if not str(guest.get('name') or '').strip():
        return error(422, 'missing_field', '"guest.name" is required.', field='guest.name')
    try:
        guests = max(1, int(body.get('guests') or 1))
    except (TypeError, ValueError):
        return error(422, 'invalid_field', '"guests" must be a whole number.', field='guests')
    amount = body.get('amount') or {}
    total, taxes = _dec(amount.get('total'), Decimal('0')), _dec(amount.get('taxes'), Decimal('0'))
    if total is None or taxes is None:
        return error(422, 'invalid_field', '"amount.total" and "amount.taxes" must be numbers.')
    paid = str(body.get('payment_status', '')).lower() == 'paid'
    settings_obj = HotelSettings.get_settings(key.branch)
    quote = pricing.quote(cat, ci, co, settings_obj)

    with transaction.atomic():
        candidates = Room.objects.select_for_update().filter(category=cat, is_active=True).order_by('room_number')
        room = pricing.free_rooms(candidates, key.branch, ci, co).first()
        if not room:
            label = conn.name or conn.get_provider_display() if conn else source
            _notify_rooms(key.branch, 'room_alert', f'Overbooking risk: {label} sold {cat.name}',
                          f'{guest.get("name")} · {ci:%d %b}–{co:%d %b} · ref {external_id}. No room is free — contact the channel.', level='critical')
            return error(409, 'sold_out', f'No {cat.name} room is free for those dates.')
        notes = f'Booked through {source} ({external_id}).'
        if quote['restriction']:
            notes += f' Accepted although: {quote["restriction"]}'
        b = RoomBooking(
            room=room, branch=key.branch, guest_name=str(guest['name'])[:150], guest_email=str(guest.get('email') or '')[:254],
            guest_phone=str(guest.get('phone') or '')[:30], check_in_date=ci, check_out_date=co,
            num_guests=guests, adults=guests, special_requests=str(body.get('special_requests') or '')[:2000],
            source=channels.booking_source(conn) if conn else RoomBooking.Source.OTA, internal_notes=notes,
            payment_status=RoomBooking.PaymentStatus.PAID if paid else RoomBooking.PaymentStatus.PENDING,
            payment_method='OTA' if paid else 'PAY_AT_DESK',
        )
        if total:
            b.grand_total, b.taxes, b.total_room_charges = total, taxes, total - taxes
        else:
            b.grand_total, b.taxes, b.total_room_charges = quote['total'], quote['tax'], quote['net']
        b.save()
        commission = Decimal('0')
        if conn is not None and conn.commission_pct:
            commission = (b.grand_total * conn.commission_pct / 100).quantize(Decimal('0.01'))
        ExternalBookingLink.objects.create(branch=key.branch, connection=conn, source=source, external_id=external_id,
                                           booking=b, commission_amount=commission, raw=body)
    _notify_rooms(key.branch, 'room_booking', f'New {source.replace("_", " ")} booking · {cat.name}',
                  f'{b.guest_name} · {ci:%d %b}–{co:%d %b} · room {room.room_number} · ₹{b.grand_total:,.0f}', booking=b, level='success')
    return JsonResponse({'booking': payloads.booking(b), 'duplicate': False}, status=201)


@endpoint('bookings:write', methods=('POST',))
def modify_booking(request, key, booking_id):
    """Change dates (and optionally the agreed amount) of a channel booking that hasn't checked out."""
    from hotel import folio
    body = request.json
    b = _bookings_qs(key).filter(booking_id=booking_id).first()
    if not b:
        return error(404, 'not_found', 'No such booking at this branch.')
    ci, co, err = _dates(body)
    if err:
        return err
    if (ci, co) != (b.check_in_date, b.check_out_date):
        problem = folio.change_dates(b, None, ci, co)
        if problem:
            return error(409, 'cannot_modify', problem)
    amount = body.get('amount') or {}
    if amount.get('total') not in (None, ''):
        total, taxes = _dec(amount.get('total')), _dec(amount.get('taxes'), Decimal('0'))
        if total is None or taxes is None:
            return error(422, 'invalid_field', '"amount.total" and "amount.taxes" must be numbers.')
        b.grand_total, b.taxes, b.total_room_charges = total, taxes, total - taxes
        b.save(update_fields=['grand_total', 'taxes', 'total_room_charges', 'updated_at'])
        folio.sync_payment_status(b)
    if body.get('special_requests') is not None:
        b.special_requests = str(body['special_requests'])[:2000]
        b.save(update_fields=['special_requests', 'updated_at'])
    b.refresh_from_db()
    _notify_rooms(key.branch, 'room_alert', f'Channel changed booking {b.booking_id}',
                  f'{b.guest_name} · now {b.check_in_date:%d %b}–{b.check_out_date:%d %b}', booking=b, sound=False)
    return JsonResponse({'booking': payloads.booking(b)})


@endpoint('bookings:write', methods=('POST',))
def cancel_booking(request, key, booking_id):
    from hotel import folio
    b = _bookings_qs(key).filter(booking_id=booking_id).first()
    if not b:
        return error(404, 'not_found', 'No such booking at this branch.')
    if b.booking_status in ('CHECKED_IN', 'CHECKED_OUT'):
        return error(409, 'booking_in_progress', 'The guest has already checked in; cancel at the front desk.')
    if b.booking_status not in ('CANCELLED', 'NO_SHOW'):
        reason = str((getattr(request, 'json', None) or {}).get('reason') or 'Cancelled by the channel')[:255]
        folio.cancel(b, None, reason)
        _notify_rooms(key.branch, 'room_alert', f'Channel cancelled {b.booking_id}',
                      f'{b.guest_name} · {b.check_in_date:%d %b}–{b.check_out_date:%d %b} · {reason}', booking=b, level='warning')
    return JsonResponse({'booking': payloads.booking(b)})
