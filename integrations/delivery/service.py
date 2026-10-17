"""The Zomato / Swiggy order pipeline and operations.

Inbound:  receive() stores every request (durable, de-duplicated) and then processes it.
          A failed event stays in the ledger and ``run_jobs`` retries it with backoff.
Outbound: perform() validates the transition, records a PlatformAction, sends it through the
          adapter and only moves the order once the platform confirmed it (or the outlet is in
          manual mode, where staff repeat the action on the partner tablet).
"""
import csv
import hashlib
import io
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

from integrations.delivery.platforms import PLATFORMS, PayloadError, Result, adapter_for
from integrations.models import (Connection, ExternalOrderLink, MenuSyncJob, PlatformAction, PlatformEvent,
                                 PlatformMenuMapping, PlatformOrderHistory, SettlementBatch, SettlementLine)

logger = logging.getLogger(__name__)
ZERO = Decimal('0')
MAX_BODY = 512 * 1024
OPEN = ['placed', 'accepted', 'ready', 'picked_up']

# platform status → allowed next statuses
TRANSITIONS = {
    'placed': {'accepted', 'rejected', 'cancelled'},
    'accepted': {'ready', 'picked_up', 'delivered', 'cancelled'},
    'ready': {'picked_up', 'delivered', 'cancelled'},
    'picked_up': {'delivered', 'cancelled'},
}
# staff action → (allowed from, moves to)
ACTION_FLOW = {
    'accept': ({'placed'}, 'accepted'),
    'reject': ({'placed'}, 'rejected'),
    'mark_ready': ({'accepted'}, 'ready'),
    'mark_picked_up': ({'accepted', 'ready'}, 'picked_up'),
}
ACTION_LABELS = {'accept': 'Accept', 'reject': 'Reject', 'mark_ready': 'Food ready', 'mark_picked_up': 'Handed to rider',
                 'item_availability': 'Item stock', 'store_status': 'Outlet online/offline', 'menu_sync': 'Menu sync'}
# platform status → our kitchen/POS order status
ORDER_STATUS = {'placed': 'P', 'accepted': 'PR', 'ready': 'READY', 'picked_up': 'S', 'delivered': 'COMPLETED',
                'rejected': 'C', 'cancelled': 'C'}
TIMESTAMP = {'accepted': 'accepted_at', 'ready': 'ready_at', 'picked_up': 'picked_up_at', 'delivered': 'delivered_at',
             'rejected': 'cancelled_at', 'cancelled': 'cancelled_at'}


class InvalidTransition(Exception):
    pass


class Unmapped(Exception):
    def __init__(self, items):
        super().__init__(f'{len(items)} item(s) are not mapped to the menu')
        self.items = items


def connection_for(branch, provider):
    return Connection.objects.filter(branch=branch, provider=provider).order_by('-is_active', 'id').first()


# ==========================================================================================
# Inbound
# ==========================================================================================

def receive(connection, raw_body, headers):
    """Store, de-duplicate and process one webhook. Returns (http_status, json_body)."""
    if len(raw_body) > MAX_BODY:
        return 413, {'error': 'payload_too_large'}
    adapter = adapter_for(connection)
    if not adapter.verify(raw_body, headers):
        _note_error(connection, 'A webhook with an invalid signature was rejected.')
        return 401, {'error': 'invalid_signature'}
    if not connection.is_active:
        return 409, {'error': 'outlet_disabled'}
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return 400, {'error': 'invalid_json'}
    if not isinstance(payload, dict):
        return 400, {'error': 'invalid_payload'}
    outlet = str(adapter.get(payload, 'outlet_id', '') or '')
    if outlet and connection.external_store_id and outlet != connection.external_store_id:
        return 422, {'error': 'outlet_mismatch'}
    key = adapter.dedupe_key(payload, raw_body)
    try:
        with transaction.atomic():
            event, created = PlatformEvent.objects.get_or_create(
                connection=connection, dedupe_key=key,
                defaults={'provider': connection.provider, 'event_type': adapter.event_type(payload),
                          'external_order_id': str(adapter.get(payload, 'order_id', '') or '')[:120],
                          'payload': payload, 'payload_sha256': hashlib.sha256(raw_body).hexdigest()})
    except IntegrityError:
        event, created = PlatformEvent.objects.get(connection=connection, dedupe_key=key), False
    Connection.objects.filter(pk=connection.pk).update(last_event_at=timezone.now())
    if created or event.state == PlatformEvent.State.RETRY:
        process(event)
        event.refresh_from_db()
    return 200, {'received': True, 'duplicate': not created, 'state': event.state}


def receive_shared(provider, raw_body, headers):
    """Platform-wide webhook (Sattkar as POS partner): route by the outlet ID inside the message."""
    from integrations.delivery.platforms import ADAPTERS
    if len(raw_body) > MAX_BODY:
        return 413, {'error': 'payload_too_large'}
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return 400, {'error': 'invalid_json'}
    if not isinstance(payload, dict):
        return 400, {'error': 'invalid_payload'}
    outlet = str(ADAPTERS[provider](None).get(payload, 'outlet_id', '') or '').strip()
    conn = None
    if outlet:
        conn = (Connection.objects.filter(provider=provider, external_store_id=outlet).select_related('branch', 'restaurant')
                .order_by('-is_active', 'id').first())
    else:
        order_id = str(ADAPTERS[provider](None).get(payload, 'order_id', '') or '')
        link = ExternalOrderLink.objects.filter(source=provider, external_id=order_id).select_related('connection').first() if order_id else None
        conn = link.connection if link else None
    if conn is None:
        return 404, {'error': 'unknown_outlet'}
    return receive(conn, raw_body, headers)


def process(event):
    """Apply one stored event. Safe to call again: every handler is idempotent."""
    conn = event.connection
    adapter = adapter_for(conn)
    event.attempts += 1
    try:
        with transaction.atomic():
            handler = HANDLERS.get(event.event_type)
            if handler is None:
                event.state, event.last_error = PlatformEvent.State.IGNORED, f'No handler for “{event.event_type}”.'
            else:
                handler(conn, adapter, event)
                event.state, event.last_error, event.unmapped = PlatformEvent.State.PROCESSED, '', []
        event.processed_at = timezone.now()
        Connection.objects.filter(pk=conn.pk).update(last_success_at=timezone.now())
    except Unmapped as exc:
        event.state, event.unmapped, event.last_error = PlatformEvent.State.NEEDS_MAPPING, exc.items, str(exc)
        names = ', '.join(f'“{u["name"]}”' for u in exc.items[:3])
        _notify(conn, 'delivery_attention', f'{PLATFORMS[conn.provider]["label"]} order #{event.external_order_id} is waiting',
                f'Match {names} to your menu to create the order', url_name='delivery_logs', level='critical',
                dedupe=f'delivery_unmapped:{event.pk}', query='?state=needs_mapping')
    except (PayloadError, InvalidTransition) as exc:
        event.state, event.last_error = PlatformEvent.State.FAILED, str(exc)
    except Exception as exc:  # transient: retry with backoff
        logger.exception('Platform event %s failed', event.pk)
        event.last_error = f'{exc.__class__.__name__}: {exc}'[:2000]
        if event.attempts >= PlatformEvent.MAX_ATTEMPTS:
            event.state = PlatformEvent.State.FAILED
        else:
            event.state = PlatformEvent.State.RETRY
            event.next_attempt_at = timezone.now() + timedelta(seconds=30 * 2 ** (event.attempts - 1))
    event.save()
    if event.state == PlatformEvent.State.FAILED:
        _note_error(conn, f'Could not process a {conn.get_provider_display()} event: {event.last_error[:200]}')
    return event


def _resolve_items(conn, norm):
    from core.models import MenuItem
    resolved, unknown = [], []
    menu = MenuItem.objects.filter(restaurant=conn.restaurant).filter(Q(branch=conn.branch) | Q(branch__isnull=True))
    for idx, it in enumerate(norm['items']):
        item = None
        if it['external_id']:
            m = (PlatformMenuMapping.objects.select_related('menu_item')
                 .filter(connection=conn, external_item_id=it['external_id'])
                 .filter(Q(external_variant_id=it['variant_id']) | Q(external_variant_id='')).order_by('-external_variant_id').first())
            item = m.menu_item if m else None
            if item is None:  # our own SKU / short code used as the partner item ID
                item = menu.filter(Q(sku__iexact=it['external_id']) | Q(short_code__iexact=it['external_id'])).first()
        if item is None:
            unknown.append({'index': idx, 'external_id': it['external_id'], 'variant_id': it['variant_id'], 'name': it['name'],
                            'price': str(it['unit_price'])})
        else:
            resolved.append((item, it))
    if unknown:
        raise Unmapped(unknown)
    return resolved


def _on_placed(conn, adapter, event):
    from core.models import Order, OrderItem
    norm = adapter.normalize_order(event.payload)
    if norm['outlet_id'] and conn.external_store_id and norm['outlet_id'] != conn.external_store_id:
        raise PayloadError('The order is for a different outlet.')
    if ExternalOrderLink.objects.filter(branch=conn.branch, source=conn.provider, external_id=norm['order_id']).exists():
        return  # already created (replay)
    resolved = _resolve_items(conn, norm)
    a = norm['amounts']
    label = PLATFORMS[conn.provider]['label']
    prepaid = norm['payment'] == 'prepaid'
    order = Order.objects.create(
        restaurant=conn.restaurant, branch=conn.branch, status=Order.OrderStatus.PENDING, order_type=Order.OrderType.DELIVERY,
        customer_name=(norm['customer']['name'] or f'{label} guest')[:200], phone=norm['customer']['phone'],
        payment_method=Order.PaymentMethod.ONLINE if prepaid else Order.PaymentMethod.CASH,
        payment_status=Order.PaymentStatus.PAID if prepaid else Order.PaymentStatus.UNPAID,
        notes='\n'.join(filter(None, [f'{label} order #{norm["order_id"]}', norm['instructions']])),
    )
    for item, it in resolved:
        extra = sum((Decimal(x['price']) for x in it['addons']), ZERO)
        notes = ', '.join(filter(None, [', '.join(x['name'] for x in it['addons'] if x['name']), it['notes']]))
        OrderItem.objects.create(order=order, menu_item=item, quantity=it['quantity'], unit_price=it['unit_price'] + extra, notes=notes[:500])
    # The platform's own figures are the bill: keep them instead of recomputing with our tax rules.
    Order.objects.filter(pk=order.pk).update(
        subtotal=a['item_total'], tax_amount=a['tax_total'], discount_amount=a['discount_total'], total_amount=a['customer_payable'],
        paid_amount=a['customer_payable'] if prepaid else ZERO, due_amount=ZERO if prepaid else a['customer_payable'],
        payments=[{'method': conn.provider, 'amount': float(a['customer_payable']), 'reference': norm['order_id']}] if prepaid else [],
        **({'order_time': norm['placed_at']} if norm['placed_at'] and norm['placed_at'] > timezone.now() - timedelta(days=2) else {}),
    )
    basis = a['item_total'] + a['packaging_fee'] - a['merchant_discount']
    commission = (basis * (conn.commission_pct or ZERO) / 100).quantize(Decimal('0.01'))
    now = timezone.now()
    link = ExternalOrderLink.objects.create(
        branch=conn.branch, connection=conn, source=conn.provider, external_id=norm['order_id'], order=order,
        commission_amount=commission, raw=event.payload, platform_status='placed', placed_at=norm['placed_at'] or now,
        accept_by=norm['accept_by'] or now + timedelta(seconds=conn.accept_window_seconds or 180),
        item_total=a['item_total'], tax_total=a['tax_total'], discount_total=a['discount_total'], merchant_discount=a['merchant_discount'],
        packaging_fee=a['packaging_fee'], delivery_fee=a['delivery_fee'], customer_payable=a['customer_payable'],
        expected_payout=basis - commission, instructions=norm['instructions'], pickup_code=norm['pickup_code'],
    )
    PlatformOrderHistory.objects.create(link=link, to_status='placed', source='platform', note=f'Received from {label}')
    _notify(conn, 'delivery_new', f'New {label} order #{norm["order_id"]}',
            f'{order.customer_name} · {sum(it["quantity"] for _, it in resolved)} items · ₹{a["customer_payable"]:,.0f}'
            + (' · cash on delivery' if not prepaid else '') + f' · accept within {(conn.accept_window_seconds or 180) // 60} min',
            link=link, order=order, level='warning', dedupe=f'order_new:{order.pk}')
    oid = order.pk

    def after():
        from core.models import Order as O
        o = O.objects.filter(pk=oid).first()
        if o:
            try:
                o.deduct_inventory()
            except Exception:
                logger.exception('Stock deduction failed for platform order %s', oid)
        if conn.auto_accept:
            l = ExternalOrderLink.objects.filter(pk=link.pk).first()
            if l and l.platform_status == 'placed':
                perform(l, 'accept', None, source='system', prep_minutes=conn.default_prep_minutes)
    transaction.on_commit(after)


def _link_for(conn, adapter, event, required=True):
    order_id = str(adapter.get(event.payload, 'order_id', '') or '')
    link = ExternalOrderLink.objects.select_related('order').filter(branch=conn.branch, source=conn.provider, external_id=order_id).first()
    if link is None and required:
        raise PayloadError(f'Order #{order_id or "?"} is not known here.')
    return link


def _on_cancelled(conn, adapter, event):
    link = _link_for(conn, adapter, event, required=False)
    if link is None or link.platform_status in ('cancelled', 'rejected'):
        return
    link.cancel_reason = str(adapter.get(event.payload, 'reason', '') or '')[:200]
    link.cancelled_by = str(adapter.get(event.payload, 'cancelled_by', '') or 'platform')[:20]
    set_status(link, 'cancelled', 'platform', note=link.cancel_reason or 'Cancelled on the platform')
    _notify(conn, 'delivery_cancelled', f'{PLATFORMS[conn.provider]["label"]} order #{link.external_id} cancelled',
            f'{link.cancel_reason or "Cancelled on the platform"} · by {link.cancelled_by} — stop preparing it',
            link=link, order=link.order, level='critical', dedupe=f'delivery_cancelled:{link.pk}')


def _on_status(conn, adapter, event):
    link = _link_for(conn, adapter, event)
    to = adapter.normalize_status(event.payload)
    if not to or to == link.platform_status:
        return
    if to == 'cancelled':
        return _on_cancelled(conn, adapter, event)
    if to not in TRANSITIONS.get(link.platform_status, set()):
        return  # stale or out-of-order update: the order is already further along
    set_status(link, to, 'platform')


def _on_rider(conn, adapter, event):
    link = _link_for(conn, adapter, event)
    r = adapter.rider(event.payload)
    for f, v in (('rider_name', r['name']), ('rider_phone', r['phone']), ('rider_eta', r['eta']), ('rider_status', r['status'])):
        if v:
            setattr(link, f, v)
    code = adapter.get(event.payload, 'pickup_code')
    if code:
        link.pickup_code = str(code)[:20]
    link.save()
    to = adapter.normalize_status(event.payload)
    if to and to in TRANSITIONS.get(link.platform_status, set()):
        set_status(link, to, 'platform', note=f'Rider {r["name"]}'.strip())


def _on_menu_result(conn, adapter, event):
    ref = str(adapter.get(event.payload, 'sync_reference', '') or '')
    job = (conn.menu_sync_jobs.filter(reference=ref).first() if ref else None) or conn.menu_sync_jobs.filter(state='submitted').first()
    if job is None:
        return
    errors = adapter.get(event.payload, 'sync_errors') or []
    ok = str(adapter.get(event.payload, 'status', '') or '').lower() in ('success', 'succeeded', 'live', 'approved', 'completed', 'done')
    job.state = MenuSyncJob.State.SUCCEEDED if ok and not errors else MenuSyncJob.State.FAILED
    job.errors, job.finished_at = errors if isinstance(errors, list) else [str(errors)], timezone.now()
    job.save()
    if job.state == MenuSyncJob.State.SUCCEEDED:
        _mark_synced(conn, job)


def _on_store_status(conn, adapter, event):
    is_open = adapter.get(event.payload, 'store_open')
    if is_open is None:
        raise PayloadError('No outlet status in the message.')
    Connection.objects.filter(pk=conn.pk).update(is_store_open=str(is_open).lower() in ('1', 'true', 'yes', 'open', 'online'),
                                                 store_status_changed_at=timezone.now())


def _on_item_stock(conn, adapter, event):
    item_id = str(adapter.get(event.payload, 'item_id', '') or '')
    avail = adapter.get(event.payload, 'available')
    if not item_id or avail is None:
        raise PayloadError('Item ID or availability missing.')
    conn.menu_mappings.filter(external_item_id=item_id).update(is_available=str(avail).lower() in ('1', 'true', 'yes'))


HANDLERS = {
    'order.placed': _on_placed, 'order.cancelled': _on_cancelled, 'order.status': _on_status, 'rider.update': _on_rider,
    'menu.sync_result': _on_menu_result, 'store.status': _on_store_status, 'item.stock': _on_item_stock,
}


# ==========================================================================================
# Order state
# ==========================================================================================

def set_status(link, to, source, actor=None, note=''):
    """Move a platform order and its kitchen/POS order together, with history."""
    frm = link.platform_status
    if to == frm:
        return link
    if to not in TRANSITIONS.get(frm, set()):
        raise InvalidTransition(f'An order that is “{link.get_platform_status_display()}” cannot become “{to}”.')
    now = timezone.now()
    link.platform_status = to
    if TIMESTAMP.get(to):
        setattr(link, TIMESTAMP[to], now)
    if to in ('rejected', 'cancelled') and not link.cancelled_by:
        link.cancelled_by = 'restaurant' if source in ('staff', 'system') else 'platform'
    link.save()
    PlatformOrderHistory.objects.create(link=link, from_status=frm, to_status=to, source=source, actor=actor, note=note[:300])
    order = link.order
    order._from_delivery = True
    order.status = ORDER_STATUS[to]
    fields = ['status']
    if to == 'accepted':
        order.prep_start, order.prep_minutes = now, link.prep_minutes or order.prep_minutes
        fields += ['prep_start', 'prep_minutes']
    elif to == 'ready':
        order.ready_at = now
        fields.append('ready_at')
    order.save(update_fields=fields)
    if to in ('rejected', 'cancelled'):
        try:
            order.restore_inventory()
        except Exception:
            logger.exception('Stock restore failed for order %s', order.pk)
    return link


def perform(link, action, user, source='staff', **kw):
    """Run a staff action on a platform order. Returns (ok, message)."""
    allowed_from, to = ACTION_FLOW[action]
    link = ExternalOrderLink.objects.select_related('connection', 'order').get(pk=link.pk)
    if link.platform_status not in allowed_from:
        return False, f'This order is already “{link.get_platform_status_display()}”.'
    conn = link.connection
    if action == 'accept':
        link.prep_minutes = max(5, min(int(kw.get('prep_minutes') or conn.default_prep_minutes or 15), 120))
        link.save(update_fields=['prep_minutes'])
        kw['prep_minutes'] = link.prep_minutes
    if action == 'reject':
        reasons = dict(PLATFORMS[conn.provider]['reject_reasons'])
        if kw.get('reason') not in reasons:
            return False, 'Choose why you are rejecting the order.'
        link.cancel_reason, link.cancelled_by = reasons[kw['reason']], 'restaurant'
        link.save(update_fields=['cancel_reason', 'cancelled_by'])
    act, created = PlatformAction.objects.get_or_create(
        idempotency_key=f'{conn.pk}:{link.pk}:{action}', defaults={'connection': conn, 'link': link, 'action': action,
                                                                    'payload': {k: v for k, v in kw.items() if v is not None},
                                                                    'created_by': user})
    if not created and act.state in ('succeeded', 'manual', 'unsupported'):
        return True, 'Already done.'
    return _execute(act, source=source, actor=user)


def _execute(act, source='staff', actor=None):
    conn = act.connection
    adapter = adapter_for(conn)
    act.attempts += 1
    kw = dict(act.payload or {})
    try:
        result = adapter.send(act.action, act.link, **kw)
    except Exception as exc:
        logger.exception('Platform action %s crashed', act.pk)
        result = Result('retry', f'{exc.__class__.__name__}: {exc}')
    act.response_code, act.response_body, act.error = result.code, (result.body or '')[:4000], ('' if result.ok else result.message[:300])
    if result.state == 'retry' and act.attempts < PlatformAction.MAX_ATTEMPTS:
        act.state = PlatformAction.State.RETRY
        act.next_attempt_at = timezone.now() + timedelta(seconds=PlatformAction.BACKOFF_SECONDS[min(act.attempts - 1, 3)])
    elif result.state == 'retry':
        act.state = PlatformAction.State.FAILED
    else:
        act.state = result.state
    if act.state in ('succeeded', 'manual', 'unsupported', 'failed'):
        act.completed_at = timezone.now()
    act.save()
    if result.state == 'succeeded':
        Connection.objects.filter(pk=conn.pk).update(last_success_at=timezone.now())
    elif act.state == 'failed':
        _note_error(conn, f'{ACTION_LABELS.get(act.action, act.action)} failed: {result.message}')
        _notify(conn, 'delivery_attention', f'{PLATFORMS[conn.provider]["label"]}: “{ACTION_LABELS.get(act.action, act.action)}” failed',
                result.message, link=act.link, level='warning', sound=False, dedupe=f'delivery_action:{act.pk}')

    # Order actions move the order once the platform confirmed — or when the platform does not take this action
    # through the API (manual / unsupported), because staff then do it on the partner app themselves.
    if act.link_id and act.action in ACTION_FLOW and act.state in ('succeeded', 'manual', 'unsupported'):
        link = ExternalOrderLink.objects.select_related('order').get(pk=act.link_id)
        to = ACTION_FLOW[act.action][1]
        if link.platform_status in ACTION_FLOW[act.action][0]:
            note = {'accept': f'Prep time {link.prep_minutes} min', 'reject': link.cancel_reason}.get(act.action, '')
            set_status(link, to, source, actor=actor, note=note or '')
    if act.action == 'store_status' and act.state in ('succeeded', 'manual', 'unsupported'):
        Connection.objects.filter(pk=conn.pk).update(is_store_open=bool(act.payload.get('open')), store_status_changed_at=timezone.now())
    messages = {
        'succeeded': f'{PLATFORMS[conn.provider]["label"]} confirmed.',
        'manual': result.message, 'unsupported': result.message + ' Updated here only.',
        'retry': f'{result.message} We will keep trying.', 'failed': result.message,
    }
    return act.state in ('succeeded', 'manual', 'unsupported'), messages.get(act.state, result.message)


def _notify(conn, kind, title, body, *, link=None, order=None, level='info', sound=True, dedupe='', url_name=None, query=''):
    """Admin notification for this outlet, linking to the order on the platform's board (or another delivery screen)."""
    from django.urls import reverse
    from notifications.service import notify
    try:
        if link is not None and url_name is None:
            url = reverse('restaurant_admin:delivery_order_detail', kwargs={'restaurant_slug': conn.restaurant.slug, 'platform': conn.provider, 'pk': link.pk})
        else:
            url = reverse(f'restaurant_admin:{url_name or "delivery_dashboard"}', kwargs={'restaurant_slug': conn.restaurant.slug, 'platform': conn.provider}) + query
    except Exception:
        url = ''
    data = {'order_number': f'#{link.external_id}', 'platform': conn.provider} if link is not None else {'platform': conn.provider}
    return notify(conn.restaurant, kind, title, body=body, branch=conn.branch, url=url, source=conn.provider, order=order,
                  level=level, sound=sound, dedupe_key=dedupe, data=data)


def _note_error(conn, message):
    Connection.objects.filter(pk=conn.pk).update(last_error=message[:300], last_error_at=timezone.now())


# ==========================================================================================
# Outlet, menu & stock
# ==========================================================================================

def set_store_open(conn, is_open, user):
    act = PlatformAction.objects.create(connection=conn, action='store_status', created_by=user, payload={'open': bool(is_open)},
                                        idempotency_key=f'{conn.pk}:store:{timezone.now().timestamp()}')
    return _execute(act, actor=user)


def set_item_available(mapping, available, user=None):
    mapping.is_available = bool(available)
    mapping.save(update_fields=['is_available', 'updated_at'])
    conn = mapping.connection
    if not mapping.is_listed or not conn.is_active:
        return True, 'Saved.'
    act = PlatformAction.objects.create(
        connection=conn, action='item_availability', created_by=user,
        payload={'item_id': mapping.external_item_id or str(mapping.menu_item_id), 'available': mapping.is_available},
        idempotency_key=f'{conn.pk}:stock:{mapping.pk}:{timezone.now().timestamp()}')
    return _execute(act, actor=user)


def menu_snapshot(conn):
    rows = []
    for m in (conn.menu_mappings.filter(is_listed=True).select_related('menu_item__category', 'menu_item__subcategory')
              .order_by('menu_item__category__name', 'menu_item__name')):
        it = m.menu_item
        rows.append({
            'item_id': m.external_item_id or it.sku or str(it.pk), 'variant_id': m.external_variant_id, 'sattkar_id': it.pk,
            'name': it.name, 'category': it.category.name if it.category else '', 'description': (it.description or '')[:500],
            'price': str(m.effective_price), 'in_stock': bool(m.is_available and it.availability),
        })
    return {'outlet_id': conn.external_store_id, 'generated_at': timezone.now().isoformat(), 'items': rows}


def validate_menu(snapshot):
    errors = []
    for r in snapshot['items']:
        if Decimal(r['price']) <= 0:
            errors.append(f'{r["name"]}: price must be above zero')
        if not r['category']:
            errors.append(f'{r["name"]}: needs a category')
    return errors


def sync_menu(conn, user):
    snap = menu_snapshot(conn)
    if not snap['items']:
        return None, 'List at least one item for this platform first.'
    problems = validate_menu(snap)
    job = MenuSyncJob.objects.create(connection=conn, items_count=len(snap['items']), menu_snapshot=snap, created_by=user)
    if problems:
        job.state, job.errors, job.finished_at = MenuSyncJob.State.FAILED, problems, timezone.now()
        job.save()
        return job, f'{len(problems)} item(s) need fixing before the menu can be sent.'
    act = PlatformAction.objects.create(connection=conn, action='menu_sync', created_by=user, payload={'menu': snap, 'job': job.pk},
                                        idempotency_key=f'{conn.pk}:menu:{job.pk}')
    ok, message = _execute(act, actor=user)
    act.refresh_from_db()
    if act.state == 'succeeded':
        job.reference = str((json.loads(act.response_body or '{}') if act.response_body.startswith('{') else {}).get('reference', ''))[:120]
        job.state = MenuSyncJob.State.SUBMITTED
        message = 'Menu submitted. It goes live once the platform approves it.'
    elif act.state in ('manual', 'unsupported'):
        job.state = MenuSyncJob.State.MANUAL
        _mark_synced(conn, job)
        message = 'Menu exported — download it and upload it in the partner dashboard.'
    elif act.state == 'retry':
        job.state = MenuSyncJob.State.QUEUED
    else:
        job.state, job.errors = MenuSyncJob.State.FAILED, [act.error]
    job.finished_at = timezone.now() if job.state != MenuSyncJob.State.QUEUED else None
    job.save()
    return job, message


def _mark_synced(conn, job):
    now = timezone.now()
    ids = [r['sattkar_id'] for r in (job.menu_snapshot or {}).get('items', [])]
    for m in conn.menu_mappings.filter(menu_item_id__in=ids).select_related('menu_item'):
        m.sync_status, m.last_synced_at, m.last_synced_price, m.last_error = 'synced', now, m.effective_price, ''
        m.save(update_fields=['sync_status', 'last_synced_at', 'last_synced_price', 'last_error', 'updated_at'])


def map_unmapped(event, choices, user):
    """Staff matched unknown platform items to menu items: save mappings and re-run the event."""
    from core.models import MenuItem
    conn = event.connection
    for ext_id, menu_item_id in choices.items():
        item = MenuItem.objects.filter(pk=menu_item_id, restaurant=conn.restaurant).filter(Q(branch=conn.branch) | Q(branch__isnull=True)).first()
        if not item or not ext_id:
            continue
        m, _ = PlatformMenuMapping.objects.get_or_create(connection=conn, menu_item=item)
        if m.external_item_id != ext_id:
            PlatformMenuMapping.objects.filter(connection=conn, external_item_id=ext_id).exclude(pk=m.pk).update(external_item_id='')
            m.external_item_id = ext_id
            m.save()
    event.state = PlatformEvent.State.RETRY
    event.save(update_fields=['state'])
    return process(event)


# ==========================================================================================
# Settlements
# ==========================================================================================

COLUMNS = {
    'order_id': ['order_id', 'order id', 'orderid', 'order no', 'order number', 'order_no', 'order #'],
    'net': ['net_payout', 'net payout', 'net amount', 'net_amount', 'payout', 'settlement amount', 'net receivable', 'amount paid', 'net'],
    'order_value': ['order_value', 'order value', 'bill amount', 'customer payable', 'order total', 'total'],
    'commission': ['commission', 'commission amount', 'platform fee', 'service fee'],
    'taxes': ['tds', 'tcs', 'taxes', 'gst on commission', 'tax deducted', 'tax'],
    'adjustments': ['adjustments', 'adjustment', 'deductions', 'other deductions'],
}


def _amount(v):
    try:
        return Decimal(str(v or '0').replace(',', '').replace('₹', '').strip() or '0').quantize(Decimal('0.01'))
    except InvalidOperation:
        return ZERO


def import_settlement(conn, file_text, *, file_name='', reference='', payout_date=None, user=None, tolerance=Decimal('1.00')):
    reader = csv.DictReader(io.StringIO(file_text.lstrip('﻿')))
    if not reader.fieldnames:
        raise ValueError('The file is empty.')
    norm = {h.strip().lower(): h for h in reader.fieldnames if h}
    cols = {k: next((norm[a] for a in aliases if a in norm), None) for k, aliases in COLUMNS.items()}
    if not cols['order_id'] or not cols['net']:
        raise ValueError('The file needs an order ID column and a net payout column.')
    batch = SettlementBatch.objects.create(connection=conn, reference=reference[:120], payout_date=payout_date, file_name=file_name[:200], created_by=user)
    links = {l.external_id: l for l in ExternalOrderLink.objects.filter(branch=conn.branch, source=conn.provider)}
    totals = {k: ZERO for k in ('order_value', 'commission', 'taxes', 'adjustments', 'net', 'expected')}
    counts = {'matched': 0, 'mismatch': 0, 'unmatched': 0}
    lines = []
    for row in reader:
        oid = str(row.get(cols['order_id']) or '').strip()
        if not oid:
            continue
        vals = {k: _amount(row.get(cols[k])) if cols[k] else ZERO for k in ('order_value', 'commission', 'taxes', 'adjustments', 'net')}
        link = links.get(oid)
        expected = link.expected_payout if link else None
        diff = vals['net'] - expected if expected is not None else ZERO
        match = 'unmatched' if link is None else ('matched' if abs(diff) <= tolerance else 'mismatch')
        counts[match] += 1
        for k in ('order_value', 'commission', 'taxes', 'adjustments', 'net'):
            totals[k] += vals[k]
        totals['expected'] += expected or ZERO
        lines.append(SettlementLine(batch=batch, link=link, external_order_id=oid[:120], order_value=vals['order_value'],
                                    commission=vals['commission'], taxes=vals['taxes'], adjustments=vals['adjustments'],
                                    net_paid=vals['net'], expected_net=expected, difference=diff, match=match))
        if link:
            link.settled_amount, link.settled_at = vals['net'], payout_date or timezone.localdate()
            fields = ['settled_amount', 'settled_at']
            if cols['commission'] and vals['commission']:
                link.commission_amount = vals['commission']  # actual commission feeds the P&L
                fields.append('commission_amount')
            link.save(update_fields=fields)
    SettlementLine.objects.bulk_create(lines)
    dates = [l.link.placed_at.date() for l in lines if l.link and l.link.placed_at]
    SettlementBatch.objects.filter(pk=batch.pk).update(
        order_value=totals['order_value'], commission=totals['commission'], taxes=totals['taxes'], adjustments=totals['adjustments'],
        net_payout=totals['net'], expected_payout=totals['expected'], lines_count=len(lines), matched=counts['matched'],
        mismatched=counts['mismatch'], unmatched=counts['unmatched'],
        period_start=min(dates) if dates else None, period_end=max(dates) if dates else None)
    batch.refresh_from_db()
    return batch


# ==========================================================================================
# Background jobs (cron: ``manage.py run_delivery_jobs`` every minute)
# ==========================================================================================

def run_jobs(now=None):
    now = now or timezone.now()
    done = {'events': 0, 'actions': 0}
    for event in PlatformEvent.objects.filter(state='retry').filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))[:200]:
        process(event)
        done['events'] += 1
    for act in PlatformAction.objects.filter(state='retry', next_attempt_at__lte=now).select_related('connection', 'link')[:200]:
        _execute(act, source='system')
        done['actions'] += 1
    return done


# ==========================================================================================
# Reporting
# ==========================================================================================

def stats(conn, start, end):
    """Performance of one outlet on one platform for [start, end] (local dates)."""
    from restaurant_admin.dashboard_metrics import _bounds, pct_change, previous_period
    s_dt, e_dt = _bounds(start, end)
    base = ExternalOrderLink.objects.filter(connection=conn, created_at__gte=s_dt, created_at__lt=e_dt)

    def totals(qs):
        good = qs.exclude(platform_status__in=['rejected', 'cancelled'])
        a = good.aggregate(n=Count('id'), gmv=Sum('customer_payable'), items=Sum('item_total'), comm=Sum('commission_amount'),
                           payout=Sum('expected_payout'), settled=Sum('settled_amount'), disc=Sum('merchant_discount'))
        n_all = qs.count()
        rejected = qs.filter(platform_status='rejected').count()
        cancelled = qs.filter(platform_status='cancelled').count()
        dur = lambda a_, b_: ExpressionWrapper(F(a_) - F(b_), output_field=DurationField())
        t = qs.filter(accepted_at__isnull=False, placed_at__isnull=False).aggregate(v=Avg(dur('accepted_at', 'placed_at')))['v']
        p = qs.filter(ready_at__isnull=False, accepted_at__isnull=False).aggregate(v=Avg(dur('ready_at', 'accepted_at')))['v']
        late = qs.filter(ready_at__isnull=False, accepted_at__isnull=False, prep_minutes__isnull=False).annotate(d=dur('ready_at', 'accepted_at'))
        late_n = sum(1 for r in late.values('d', 'prep_minutes') if r['d'] > timedelta(minutes=r['prep_minutes']))
        n = a['n'] or 0
        gmv = a['gmv'] or ZERO
        return {
            'orders': n, 'all': n_all, 'gmv': gmv, 'items': a['items'] or ZERO, 'commission': a['comm'] or ZERO,
            'payout': a['payout'] or ZERO, 'settled': a['settled'] or ZERO, 'merchant_discount': a['disc'] or ZERO,
            'aov': gmv / n if n else ZERO, 'rejected': rejected, 'cancelled': cancelled,
            'reject_rate': round(rejected / n_all * 100, 1) if n_all else 0,
            'cancel_rate': round(cancelled / n_all * 100, 1) if n_all else 0,
            'accept_secs': int(t.total_seconds()) if t else None,
            'prep_mins': round(p.total_seconds() / 60, 1) if p else None,
            'late': late_n, 'late_rate': round(late_n / late.count() * 100, 1) if late.count() else 0,
            'commission_rate': round(float((a['comm'] or ZERO) / gmv * 100), 1) if gmv else 0,
        }

    cur = totals(base)
    p_start, p_end = previous_period(start, end)
    ps, pe = _bounds(p_start, p_end)
    prev = totals(ExternalOrderLink.objects.filter(connection=conn, created_at__gte=ps, created_at__lt=pe))
    tz = timezone.get_current_timezone()
    good = base.exclude(platform_status__in=['rejected', 'cancelled'])
    if start == end:
        rows = {r['k']: r for r in good.annotate(k=ExtractHour('created_at', tzinfo=tz)).values('k').annotate(n=Count('id'), v=Sum('customer_payable'))}
        series = [{'label': f'{h % 12 or 12}{"a" if h < 12 else "p"}', 'orders': rows.get(h, {}).get('n', 0), 'gmv': float(rows.get(h, {}).get('v') or 0)} for h in range(24)]
    else:
        rows = {r['k']: r for r in good.annotate(k=TruncDate('created_at', tzinfo=tz)).values('k').annotate(n=Count('id'), v=Sum('customer_payable'))}
        series, day = [], start
        while day <= end:
            r = rows.get(day, {})
            series.append({'label': day.strftime('%d %b'), 'orders': r.get('n', 0), 'gmv': float(r.get('v') or 0)})
            day += timedelta(days=1)
    from core.models import OrderItem
    top = list(OrderItem.objects.filter(order__external_link__in=good).values('menu_item__name')
               .annotate(qty=Sum('quantity'), sales=Sum(F('quantity') * F('unit_price'))).order_by('-qty')[:6])
    reasons = list(base.filter(platform_status__in=['rejected', 'cancelled']).exclude(cancel_reason='')
                   .values('cancel_reason', 'cancelled_by').annotate(n=Count('id')).order_by('-n')[:5])
    return {
        'cur': cur, 'prev': prev,
        'deltas': {k: pct_change(cur[k], prev[k]) for k in ('orders', 'gmv', 'aov', 'payout')},
        'series': series, 'top': top, 'reasons': reasons,
    }


def live_board(conn):
    qs = (ExternalOrderLink.objects.filter(connection=conn, platform_status__in=OPEN)
          .select_related('order').prefetch_related('order__items__menu_item').order_by('created_at'))
    cols = {s: [] for s in OPEN}
    for link in qs[:200]:
        cols[link.platform_status].append(link)
    return cols


def attention(conn):
    return {
        'unmapped': list(conn.platform_events.filter(state='needs_mapping').order_by('-received_at')[:20]),
        'failed_events': conn.platform_events.filter(state='failed', received_at__gte=timezone.now() - timedelta(days=3)).count(),
        'failed_actions': list(conn.platform_actions.filter(state__in=['failed', 'retry']).select_related('link')
                               .filter(created_at__gte=timezone.now() - timedelta(days=2))[:10]),
        'overdue': ExternalOrderLink.objects.filter(connection=conn, platform_status='placed', accept_by__lt=timezone.now()).count(),
        'unmapped_menu': conn.menu_mappings.filter(is_listed=True, external_item_id='').count(),
    }
