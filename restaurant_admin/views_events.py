"""Club & Events: ticketed events, tiers, guestlist, door passes, ticket grid, table floor plan and gate scanner.

The same views serve the Restaurant admin (/dashboard/<slug>/club-events/…) and the Hotel admin
(/hotel-admin/<slug>/club-events/…). ``club_module_required`` works out which admin the request is in
and puts the business, branch and template context on ``request.club``; templates extend
``base_template`` and build links with ``{% url ns|add:':club_…' %}``.
"""
import csv
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import models, transaction
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import Table, Zone
from events.models import ClubEvent, EventBooking, TicketType
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant
from super_admin.models import RestaurantUser

logger = logging.getLogger(__name__)

NS_RESTAURANT, NS_HOTEL = 'restaurant_admin', 'hotel_admin'
BASES = {NS_RESTAURANT: 'restaurant_admin/base.html', NS_HOTEL: 'hotel_admin/base_hotel.html'}
SUCCESS = EventBooking.PaymentStatus.SUCCESS


# ── Plumbing ─────────────────────────────────────────────────────────────────────────────────────

def _ns(request):
    match = getattr(request, 'resolver_match', None)
    return NS_HOTEL if match is not None and match.namespace == NS_HOTEL else NS_RESTAURANT


def club_module_required(view_func):
    """Resolve business + branch for either admin, check the Club module is on, and set ``request.club``."""
    @wraps(view_func)
    def wrapper(request, restaurant_slug=None, *args, **kwargs):
        from restaurant_project import constants
        ns = _ns(request)
        if ns == NS_HOTEL:
            hotel_ctx = getattr(request, 'hotel_ctx', None)
            if hotel_ctx is None:  # only reachable through hotel.urls_admin.club()
                raise Http404
            restaurant, branch = hotel_ctx['restaurant'], hotel_ctx['current_branch']
            ctx = dict(hotel_ctx)
        else:
            restaurant = resolve_restaurant(request, restaurant_slug)
            if isinstance(restaurant, HttpResponse):
                return restaurant
            branch = get_active_branch(request, restaurant)
            ctx = {'restaurant': restaurant, 'current_branch': branch}
        ctx.update(ns=ns, base_template=BASES[ns], rslug=restaurant_slug, in_hotel=ns == NS_HOTEL)

        if not branch:
            messages.error(request, "No active branch found.")
            return redirect(f'{ns}:dashboard', restaurant_slug=restaurant_slug)
        plan_on = constants.is_module_enabled('ENABLE_CLUB_EVENTS_MODULE', restaurant)
        if not (plan_on and branch.has_club_module) and not request.user.is_superuser:
            return render(request, 'restaurant_admin/events/upgrade_required.html',
                          {**ctx, 'active_menu': 'club_events', 'globally_off': not plan_on})
        request.club = SimpleNamespace(restaurant=restaurant, branch=branch, ns=ns, slug=restaurant_slug, ctx=ctx)
        return view_func(request, restaurant_slug, *args, **kwargs)
    return wrapper


def _render(request, template, active_menu, **extra):
    return render(request, f'restaurant_admin/events/{template}',
                  {**request.club.ctx, 'active_menu': active_menu, **_staff_flags(request), **extra})


def _go(request, name, **kwargs):
    return redirect(f'{request.club.ns}:{name}', restaurant_slug=request.club.slug, **kwargs)


def _back(request, fallback_name, **kwargs):
    """Redirect to a same-site ``next`` (forms posted from the ticket grid / floor plan) or to ``fallback_name``."""
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return _go(request, fallback_name, **kwargs)


def _wants_json(request):
    return (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', ''))


def _post_only(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    return None


def _events(request):
    return ClubEvent.objects.filter(branch=request.club.branch)


def _event(request, pk):
    """An event of the branch being managed — never another branch's, even for a company-level login."""
    return get_object_or_404(ClubEvent.objects.select_related('branch'), pk=pk, branch=request.club.branch)


def _pick_event(request, event_id, qs=None):
    qs = qs if qs is not None else _events(request)
    if event_id:
        return get_object_or_404(qs, pk=event_id)
    return qs.filter(end_datetime__gte=timezone.now()).order_by('start_datetime').first() or qs.order_by('-start_datetime').first()


def _role(request):
    profile = getattr(request.user, 'restaurant_profile', None)
    return (getattr(request.user, 'role', None) or (profile.role if profile else '') or '').upper(), profile


def _staff_flags(request):
    """What a Gate Scanner login may do (managers may do everything)."""
    role, profile = _role(request)
    is_scanner = role == RestaurantUser.Role.GATE_SCANNER
    return {
        'is_scanner': is_scanner,
        'can_view_sales': not is_scanner or getattr(profile, 'can_view_analytics', False),
        'can_undo_checkin': not is_scanner or getattr(profile, 'can_issue_refunds', False),
        'can_cancel_pass': not is_scanner or getattr(profile, 'can_issue_refunds', False),
        'can_issue_passes': not is_scanner or getattr(profile, 'can_apply_discounts', False),
        'can_view_qr': not is_scanner or getattr(profile, 'can_manage_menu', False),
        'can_manage_events': not is_scanner,
    }


def _status(evt, now=None):
    now = now or timezone.now()
    if not evt.is_published:
        return 'draft'
    if evt.start_datetime <= now <= evt.end_datetime:
        return 'live'
    return 'upcoming' if evt.start_datetime > now else 'ended'


def _expire_holds():
    EventBooking.objects.filter(payment_status=EventBooking.PaymentStatus.PENDING,
                                expires_at__lt=timezone.now()).update(payment_status=EventBooking.PaymentStatus.EXPIRED)


# ── Events: list, create, edit, duplicate, publish, delete ──────────────────────────────────────

@login_required
@user_passes_test(is_admin)
@club_module_required
def club_events_list(request, restaurant_slug):
    """Events of the branch with upcoming / past / draft tabs, search and sell-through per event."""
    now = timezone.now()
    events = list(_events(request).prefetch_related('ticket_types').annotate(
        sold=models.Sum('bookings__quantity', filter=models.Q(bookings__payment_status=SUCCESS)),
        revenue=models.Sum('bookings__total_amount', filter=models.Q(bookings__payment_status=SUCCESS)),
        checked=models.Sum('bookings__quantity', filter=models.Q(bookings__payment_status=SUCCESS, bookings__checked_in=True)),
        booking_count=models.Count('bookings', filter=models.Q(bookings__payment_status=SUCCESS)),
    ).order_by('start_datetime'))
    for e in events:
        e.state = _status(e, now)
        e.capacity = sum(t.total_allocated for t in e.ticket_types.all() if t.is_active)
        e.sold = e.sold or 0
        e.sell_pct = min(100, round(e.sold * 100 / e.capacity)) if e.capacity else 0
        e.active_tiers = [t for t in e.ticket_types.all() if t.is_active]

    tab = request.GET.get('tab') or 'upcoming'
    q = (request.GET.get('q') or '').strip().lower()
    counts = {
        'upcoming': sum(1 for e in events if e.state in ('live', 'upcoming')),
        'drafts': sum(1 for e in events if e.state == 'draft'),
        'past': sum(1 for e in events if e.state == 'ended'),
        'all': len(events),
    }
    if tab == 'upcoming':
        shown = [e for e in events if e.state in ('live', 'upcoming')]
    elif tab == 'drafts':
        shown = [e for e in events if e.state == 'draft']
    elif tab == 'past':
        shown = sorted((e for e in events if e.state == 'ended'), key=lambda e: e.start_datetime, reverse=True)
    else:
        tab, shown = 'all', sorted(events, key=lambda e: e.start_datetime, reverse=True)
    if q:
        shown = [e for e in shown if q in e.title.lower() or q in (e.venue_name or '').lower()]

    next_event = next((e for e in events if e.state in ('live', 'upcoming')), None)
    return _render(request, 'list.html', 'club_events',
                   events=shown, tab=tab, q=request.GET.get('q', ''), counts=counts, next_event=next_event,
                   total_events=len(events), upcoming_events=counts['upcoming'],
                   total_tickets_sold=sum(e.sold for e in events),
                   total_revenue=sum((e.revenue or Decimal('0')) for e in events))


def _parse_dt(value):
    dt = datetime.fromisoformat(value)
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _read_event_form(request, event=None):
    """Validate the event form. Returns (fields, tiers, errors); tiers are SimpleNamespaces for re-rendering."""
    p = request.POST
    branch = request.club.branch
    errors = []
    f = {
        'title': p.get('title', '').strip(),
        'description': p.get('description', '').strip(),
        'venue_name': p.get('venue_name', '').strip() or branch.name,
        'venue_address': p.get('venue_address', '').strip() or (branch.address or ''),
        'age_restriction': p.get('age_restriction', '21+').strip() or '21+',
        'dress_code': p.get('dress_code', '').strip(),
        'is_published': p.get('is_published') == 'on',
        'is_featured': p.get('is_featured') == 'on',
        'terms_and_conditions': p.get('terms_and_conditions', '').strip()
        or 'Entry strictly allowed with a valid QR pass. Rights of admission reserved.',
    }
    if not f['title']:
        errors.append('Give the event a title.')
    for key, label in (('start_datetime', 'start'), ('end_datetime', 'end')):
        raw = p.get(key, '').strip()
        try:
            f[key] = _parse_dt(raw) if raw else None
        except ValueError:
            f[key] = None
        if not f[key]:
            errors.append(f'Pick the {label} date and time.')
    if f.get('start_datetime') and f.get('end_datetime') and f['end_datetime'] <= f['start_datetime']:
        errors.append('The event must end after it starts.')

    existing = {t.pk: t for t in event.ticket_types.all()} if event else {}
    tiers = []
    cols = [p.getlist(k) for k in ('tier_id[]', 'tier_name[]', 'tier_description[]', 'tier_price[]', 'tier_capacity[]', 'tier_limit[]')]
    width = max(len(c) for c in cols)
    for i in range(width):
        tid, name, desc, price, cap, limit = (c[i].strip() if i < len(c) else '' for c in cols)
        if not name:
            continue
        old = existing.get(int(tid)) if tid.isdigit() else None
        tier = SimpleNamespace(id=old.pk if old else '', name=name, description=desc, base_price=price,
                               total_allocated=cap, max_per_order=limit or '10', quantity_sold=old.quantity_sold if old else 0)
        try:
            tier.base_price = Decimal(price or '0')
            if tier.base_price < 0:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f'“{name}”: the price must be a number of 0 or more.')
        try:
            tier.total_allocated = int(cap)
            if tier.total_allocated < 1:
                raise ValueError
        except ValueError:
            errors.append(f'“{name}”: total tickets must be a whole number of 1 or more.')
        try:
            tier.max_per_order = int(limit or 10)
            if tier.max_per_order < 1:
                raise ValueError
        except ValueError:
            errors.append(f'“{name}”: max per order must be 1 or more.')
        if isinstance(tier.total_allocated, int) and tier.total_allocated < tier.quantity_sold:
            errors.append(f'“{name}”: {tier.quantity_sold} tickets are already sold, so total tickets can’t go below that.')
        tiers.append(tier)
    if not tiers:
        errors.append('Add at least one ticket tier.')
    return f, tiers, errors


def _save_tiers(event, tiers):
    kept = []
    for idx, t in enumerate(tiers):
        obj = TicketType.objects.filter(pk=t.id, event=event).first() if t.id else None
        obj = obj or TicketType(event=event)
        obj.name, obj.description, obj.base_price = t.name, t.description, t.base_price
        obj.total_allocated, obj.max_per_order, obj.display_order, obj.is_active = t.total_allocated, t.max_per_order, idx, True
        obj.save()
        kept.append(obj.pk)
    # Tiers removed in the form: keep (hidden) the ones that have bookings, delete the rest
    for old in event.ticket_types.exclude(pk__in=kept):
        if old.quantity_sold or old.bookings.exists():
            old.is_active = False
            old.save(update_fields=['is_active'])
        else:
            old.delete()


DEFAULT_TIERS = [
    SimpleNamespace(id='', name='General Admission', base_price='499', total_allocated='100', max_per_order='10', quantity_sold=0,
                    description='• 1 ticket admits 1 person to the general zone\n• Standing zone'),
    SimpleNamespace(id='', name='VIP Table / Pass', base_price='1499', total_allocated='30', max_per_order='6', quantity_sold=0,
                    description='• Dedicated VIP lounge seating\n• 1 complimentary beverage\n• Priority queue entry'),
]


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_create(request, restaurant_slug):
    """Create an event with its ticket tiers."""
    if request.method == 'POST':
        f, tiers, errors = _read_event_form(request)
        if not errors:
            with transaction.atomic():
                event = ClubEvent.objects.create(branch=request.club.branch, cover_image=request.FILES.get('cover_image'), **f)
                _save_tiers(event, tiers)
            messages.success(request, f'“{event.title}” created{" and published" if event.is_published else " as a draft"}.')
            return _go(request, 'club_events_list')
        for e in errors:
            messages.error(request, e)
        return _render(request, 'form.html', 'club_events', is_edit=False, event=SimpleNamespace(**f), ticket_types=tiers)
    return _render(request, 'form.html', 'club_events', is_edit=False, event=None, ticket_types=DEFAULT_TIERS)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_edit(request, restaurant_slug, pk):
    """Edit an event and its tiers. Tiers with sales can't drop below what's sold."""
    event = _event(request, pk)
    if request.method == 'POST':
        f, tiers, errors = _read_event_form(request, event)
        if not errors:
            with transaction.atomic():
                for k, v in f.items():
                    setattr(event, k, v)
                if request.FILES.get('cover_image'):
                    event.cover_image = request.FILES['cover_image']
                elif request.POST.get('remove_cover') == '1':
                    event.cover_image = None
                event.save()
                _save_tiers(event, tiers)
            messages.success(request, f'“{event.title}” saved.')
            return _go(request, 'club_events_list')
        for e in errors:
            messages.error(request, e)
        draft = SimpleNamespace(**f, pk=event.pk, id=event.pk, cover_image=event.cover_image)
        return _render(request, 'form.html', 'club_events', is_edit=True, event=draft, ticket_types=tiers)
    return _render(request, 'form.html', 'club_events', is_edit=True, event=event,
                   ticket_types=event.ticket_types.filter(is_active=True).order_by('display_order', 'id'))


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_duplicate(request, restaurant_slug, pk):
    """Copy an event and its tiers one week later, as a draft — handy for weekly club nights."""
    denied = _post_only(request)
    if denied:
        return denied
    src = _event(request, pk)
    with transaction.atomic():
        copy = ClubEvent.objects.create(
            branch=src.branch, title=f'{src.title} (copy)', description=src.description, cover_image=src.cover_image,
            start_datetime=src.start_datetime + timedelta(days=7), end_datetime=src.end_datetime + timedelta(days=7),
            venue_name=src.venue_name, venue_address=src.venue_address, age_restriction=src.age_restriction,
            dress_code=src.dress_code, terms_and_conditions=src.terms_and_conditions, is_published=False)
        for t in src.ticket_types.filter(is_active=True):
            TicketType.objects.create(event=copy, name=t.name, description=t.description, base_price=t.base_price,
                                      total_allocated=t.total_allocated, max_per_order=t.max_per_order, display_order=t.display_order)
    messages.success(request, f'Copied as a draft one week later — check the details and publish when ready.')
    return _go(request, 'club_event_edit', pk=copy.pk)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_delete(request, restaurant_slug, pk):
    """Delete an event that has no confirmed tickets (otherwise unpublish it)."""
    denied = _post_only(request)
    if denied:
        return denied
    event = _event(request, pk)
    confirmed = event.bookings.filter(payment_status=SUCCESS).count()
    if confirmed:
        messages.error(request, f'“{event.title}” has {confirmed} confirmed booking(s), so it can’t be deleted. Unpublish it instead.')
    else:
        title = event.title
        event.delete()
        messages.success(request, f'“{title}” deleted.')
    return _go(request, 'club_events_list')


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_toggle_publish(request, restaurant_slug, pk):
    denied = _post_only(request)
    if denied:
        return denied
    event = _event(request, pk)
    event.is_published = not event.is_published
    event.save(update_fields=['is_published'])
    messages.success(request, f'“{event.title}” is now {"live — guests can book" if event.is_published else "a draft — hidden from guests"}.')
    return _back(request, 'club_events_list')


# ── Guestlist, door passes, check-in, cancel, export ────────────────────────────────────────────

def _guestlist_qs(request, event):
    qs = EventBooking.objects.filter(event=event).select_related('ticket_type', 'assigned_table', 'checked_in_by')
    g = request.GET
    q = g.get('q', '').strip()
    if q:
        qs = qs.filter(models.Q(guest_name__icontains=q) | models.Q(guest_phone__icontains=q)
                       | models.Q(guest_email__icontains=q) | models.Q(booking_reference__icontains=q))
    status = g.get('status', '').strip()
    if status:
        qs = qs.filter(payment_status=status)
    else:  # abandoned online checkouts are noise at the door
        qs = qs.exclude(payment_status__in=[EventBooking.PaymentStatus.EXPIRED, EventBooking.PaymentStatus.FAILED])
    if g.get('checked_in') in ('0', '1'):
        qs = qs.filter(checked_in=g['checked_in'] == '1')
    if g.get('tier', '').isdigit():
        qs = qs.filter(ticket_type_id=int(g['tier']))
    return qs.order_by('-created_at')


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_guestlist(request, restaurant_slug, event_id=None):
    """Guestlist desk for one event: search, filter, check in, cancel, issue door passes, export."""
    events_list = _events(request).order_by('-start_datetime')
    selected_event = _pick_event(request, event_id)
    ctx = {'events_list': events_list, 'selected_event': selected_event,
           'search_query': request.GET.get('q', ''), 'status_filter': request.GET.get('status', ''),
           'checked_in_filter': request.GET.get('checked_in', ''), 'tier_filter': request.GET.get('tier', '')}
    if selected_event:
        _expire_holds()
        bookings = _guestlist_qs(request, selected_event)
        confirmed = EventBooking.objects.filter(event=selected_event, payment_status=SUCCESS)
        totals = confirmed.aggregate(guests=models.Sum('quantity'), sales=models.Sum('total_amount'),
                                     inside=models.Sum('quantity', filter=models.Q(checked_in=True)))
        guests, inside = totals['guests'] or 0, totals['inside'] or 0
        ctx.update(bookings=bookings, ticket_tiers=selected_event.ticket_types.filter(is_active=True),
                   total_confirmed_attendees=guests, checked_in_count=inside,
                   checkin_pct=round(inside * 100 / guests) if guests else 0,
                   total_sales_amount=totals['sales'] or Decimal('0'),
                   state=_status(selected_event), filtered=any(ctx[k] for k in ('search_query', 'status_filter', 'checked_in_filter', 'tier_filter')))
    return _render(request, 'guestlist.html', 'club_guestlist', **ctx)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_guestlist_export(request, restaurant_slug, event_id):
    """CSV of the guestlist (same filters as the screen)."""
    from super_admin.activity import log_activity
    event = _event(request, event_id)
    flags = _staff_flags(request)
    rows = _guestlist_qs(request, event)
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="guestlist-{event.slug or event.pk}.csv"'
    resp.write('﻿')
    w = csv.writer(resp)
    head = ['Booking ref', 'Guest', 'Phone', 'Email', 'Tier', 'Guests', 'Table', 'Status', 'Checked in', 'Checked in at', 'Booked at', 'Notes']
    if flags['can_view_sales']:
        head[7:7] = ['Paid (₹)', 'Payment method']
    w.writerow(head)
    for b in rows:
        row = [b.booking_reference, b.guest_name, b.guest_phone if flags['can_view_sales'] else '', b.guest_email,
               b.ticket_type.name, b.quantity, b.assigned_table.name if b.assigned_table else '',
               b.get_payment_status_display(), 'Yes' if b.checked_in else 'No',
               timezone.localtime(b.checked_in_at).strftime('%d %b %Y %I:%M %p') if b.checked_in_at else '',
               timezone.localtime(b.created_at).strftime('%d %b %Y %I:%M %p'), b.notes]
        if flags['can_view_sales']:
            row[7:7] = [f'{b.total_amount:.2f}', b.get_payment_method_display()]
        w.writerow(row)
    log_activity(request, action='EXPORT', message=f'Exported the guestlist of “{event.title}”',
                 area='hotel' if request.club.ns == NS_HOTEL else 'restaurant',
                 restaurant=request.club.restaurant, branch=request.club.branch, object_type='ClubEvent',
                 object_id=str(event.pk), object_repr=event.title)
    return resp


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_manual_booking_create(request, restaurant_slug, event_id):
    """Issue a door (cash) or complimentary pass from the guestlist / ticket grid."""
    denied = _post_only(request)
    if denied:
        return denied
    event = _event(request, event_id)
    if not _staff_flags(request)['can_issue_passes']:
        messages.error(request, 'Your login can’t issue door passes.')
        return _back(request, 'club_guestlist_event', event_id=event.id)

    p = request.POST
    name = ' '.join(p.get('guest_name', '').split())
    phone = p.get('guest_phone', '').strip()
    email = p.get('guest_email', '').strip()
    notes = p.get('notes', '').strip()
    method = p.get('payment_method') if p.get('payment_method') in (EventBooking.PaymentMethod.DOOR_CASH, EventBooking.PaymentMethod.COMPLIMENTARY) \
        else EventBooking.PaymentMethod.DOOR_CASH
    errors = []
    if not name:
        errors.append('Enter the guest’s name.')
    digits = ''.join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        errors.append('Enter a valid mobile number (10 digits).')
    try:
        qty = int(p.get('quantity') or 1)
        if not 1 <= qty <= 50:
            raise ValueError
    except ValueError:
        qty = 0
        errors.append('Guests must be between 1 and 50.')
    if email and '@' not in email:
        errors.append('That email address doesn’t look right.')
    tier = TicketType.objects.filter(pk=p.get('ticket_type_id') if str(p.get('ticket_type_id', '')).isdigit() else None,
                                     event=event, is_active=True).first()
    if not tier:
        errors.append('Pick a ticket tier.')
    if errors:
        for e in errors:
            messages.error(request, e)
        return _back(request, 'club_guestlist_event', event_id=event.id)

    with transaction.atomic():
        locked = TicketType.objects.select_for_update().get(pk=tier.pk)
        if locked.available_quantity < qty:
            messages.error(request, f'Only {locked.available_quantity} “{locked.name}” ticket(s) left — can’t issue {qty}.')
            return _back(request, 'club_guestlist_event', event_id=event.id)
        unit = Decimal('0.00') if method == EventBooking.PaymentMethod.COMPLIMENTARY else locked.base_price
        booking = EventBooking.objects.create(
            booking_reference=EventBooking.generate_booking_reference(), event=event, ticket_type=locked,
            guest_name=name, guest_phone=phone, guest_email=email, quantity=qty, unit_price=unit,
            tax_amount=Decimal('0.00'), total_amount=unit * qty, payment_status=SUCCESS, payment_method=method,
            notes=notes)
        locked.quantity_sold += qty
        locked.save(update_fields=['quantity_sold'])
        booking.security_token = booking.generate_security_token()
        booking.generate_qr_code(force=True)
        booking.save(update_fields=['security_token', 'booking_qr_code'])

    if email:
        try:
            from core.emails import send_event_ticket_email
            send_event_ticket_email(booking, request=request)
        except Exception as err:
            logger.warning('Could not email pass %s: %s', booking.booking_reference, err)
    messages.success(request, f'Pass {booking.booking_reference} issued to {name} ({qty} guest{"s" if qty > 1 else ""})'
                              f'{" — emailed" if email else ""}.')
    return _back(request, 'club_guestlist_event', event_id=event.id)


def _booking(request, booking_id):
    return get_object_or_404(EventBooking.objects.select_related('event', 'ticket_type', 'assigned_table'),
                             pk=booking_id, event__branch=request.club.branch)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_booking_toggle_checkin(request, restaurant_slug, booking_id):
    """Check a guest in, or undo it. Answers JSON for the ticket grid / floor plan."""
    denied = _post_only(request)
    if denied:
        return denied
    booking = _booking(request, booking_id)
    flags = _staff_flags(request)

    def done(ok, msg, level=messages.SUCCESS, status=200):
        if _wants_json(request):
            return JsonResponse({'success': ok, 'message': msg, 'checked_in': booking.checked_in,
                                 'checked_in_at': timezone.localtime(booking.checked_in_at).strftime('%I:%M %p') if booking.checked_in_at else None},
                                status=status)
        messages.add_message(request, level, msg)
        return _back(request, 'club_guestlist_event', event_id=booking.event_id)

    if booking.payment_status != SUCCESS:
        return done(False, f'{booking.booking_reference} is {booking.get_payment_status_display().lower()} — it can’t be checked in.', messages.ERROR, 400)
    if booking.checked_in:
        if not flags['can_undo_checkin']:
            return done(False, 'Your login can’t undo a check-in.', messages.ERROR, 403)
        booking.checked_in, booking.checked_in_at, booking.checked_in_by = False, None, None
        booking.save(update_fields=['checked_in', 'checked_in_at', 'checked_in_by'])
        return done(True, f'Check-in undone for {booking.guest_name}.', messages.INFO)
    booking.mark_checked_in(staff_user=request.user)
    extra = f' (+{booking.quantity - 1})' if booking.quantity > 1 else ''
    return done(True, f'{booking.guest_name}{extra} checked in.')


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_booking_cancel(request, restaurant_slug, booking_id):
    """Cancel a confirmed pass that hasn't been used and put its tickets back on sale."""
    denied = _post_only(request)
    if denied:
        return denied
    booking = _booking(request, booking_id)
    if not _staff_flags(request)['can_cancel_pass']:
        messages.error(request, 'Your login can’t cancel passes.')
    elif booking.payment_status != SUCCESS:
        messages.error(request, f'{booking.booking_reference} is already {booking.get_payment_status_display().lower()}.')
    elif booking.checked_in:
        messages.error(request, f'{booking.guest_name} is already checked in — undo the check-in first.')
    else:
        reason = request.POST.get('reason', '').strip()
        with transaction.atomic():
            tier = TicketType.objects.select_for_update().get(pk=booking.ticket_type_id)
            tier.quantity_sold = max(0, tier.quantity_sold - booking.quantity)
            tier.save(update_fields=['quantity_sold'])
            booking.payment_status = EventBooking.PaymentStatus.CANCELLED
            booking.assigned_table, booking.assigned_at = None, None
            stamp = f'Cancelled {timezone.localtime().strftime("%d %b %I:%M %p")} by {request.user.get_full_name() or request.user.username}'
            booking.notes = '\n'.join(x for x in [booking.notes, stamp + (f': {reason}' if reason else '')] if x)
            booking.save(update_fields=['payment_status', 'assigned_table', 'assigned_at', 'notes'])
        msg = f'Pass {booking.booking_reference} cancelled — {booking.quantity} ticket(s) back on sale.'
        if booking.payment_method == EventBooking.PaymentMethod.ONLINE and booking.total_amount > 0:
            msg += f' Refund ₹{booking.total_amount:.0f} to the guest from your payment gateway.'
        messages.success(request, msg)
    return _back(request, 'club_guestlist_event', event_id=booking.event_id)


# ── Gate scanner ────────────────────────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
@club_module_required
def club_gate_scanner(request, restaurant_slug, event_id=None):
    """Camera / manual QR check-in at the door. Only passes for the chosen event are admitted."""
    events_list = _events(request).filter(end_datetime__gte=timezone.now() - timedelta(days=1)).order_by('start_datetime')
    selected_event = _pick_event(request, event_id, events_list) if (event_id or events_list.exists()) else None
    ctx = {'events_list': events_list, 'selected_event': selected_event}
    if selected_event:
        t = EventBooking.objects.filter(event=selected_event, payment_status=SUCCESS).aggregate(
            guests=models.Sum('quantity'), inside=models.Sum('quantity', filter=models.Q(checked_in=True)))
        recent = EventBooking.objects.filter(event=selected_event, checked_in=True).select_related('ticket_type').order_by('-checked_in_at')[:8]
        ctx.update(total_guests=t['guests'] or 0, inside=t['inside'] or 0, recent=recent)
    return _render(request, 'scanner.html', 'club_scanner', **ctx)


# ── Floor plan (tables) ─────────────────────────────────────────────────────────────────────────

def _tables(request):
    r, b = request.club.restaurant, request.club.branch
    return Table.objects.filter(restaurant=r, branch=b, is_active=True).select_related('zone').order_by('zone__name', 'table_number')


def _floor_data(request, event):
    tables = list(_tables(request))
    confirmed = list(EventBooking.objects.filter(event=event, payment_status=SUCCESS).select_related('ticket_type', 'assigned_table')) if event else []
    by_table = {b.assigned_table_id: b for b in confirmed if b.assigned_table_id}
    unassigned = [b for b in confirmed if not b.assigned_table_id]

    def info(b):
        return {'id': b.id, 'reference': b.booking_reference, 'guest_name': b.guest_name, 'guest_phone': b.guest_phone,
                'ticket_tier': b.ticket_type.name, 'quantity': b.quantity, 'total_amount': str(b.total_amount),
                'checked_in': b.checked_in,
                'checked_in_at': timezone.localtime(b.checked_in_at).strftime('%I:%M %p') if b.checked_in_at else None}

    rows = []
    for t in tables:
        b = by_table.get(t.id)
        rows.append({'id': t.id, 'name': t.name, 'table_number': t.table_number, 'capacity': t.capacity,
                     'shape': t.shape or 'round', 'zone_id': t.zone_id, 'zone_name': t.zone.name if t.zone else 'No zone',
                     'status': ('CHECKED_IN' if b.checked_in else 'RESERVED') if b else 'AVAILABLE',
                     'booking': info(b) if b else None})
    metrics = {
        'total_tables': len(tables), 'assigned_tables': len(by_table), 'available_tables': max(0, len(tables) - len(by_table)),
        'seated': sum(b.quantity for b in by_table.values()), 'seats': sum(t.capacity for t in tables),
        'total_attendees': sum(b.quantity for b in confirmed), 'checked_in': sum(b.quantity for b in confirmed if b.checked_in),
        'total_sales': str(sum((b.total_amount for b in confirmed), Decimal('0'))),
    }
    return rows, [info(b) for b in unassigned], metrics


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan(request, restaurant_slug, event_id=None):
    """Tables of the branch, coloured by who holds them for the chosen event."""
    events_list = _events(request).order_by('-start_datetime')
    selected_event = _pick_event(request, event_id)
    rows, unassigned, metrics = _floor_data(request, selected_event)
    zones = Zone.objects.filter(restaurant=request.club.restaurant).filter(
        models.Q(branch=request.club.branch) | models.Q(branch__isnull=True)).order_by('name')
    zone_counts = {}
    for t in rows:
        zone_counts[t['zone_id']] = zone_counts.get(t['zone_id'], 0) + 1
    zones = [z for z in zones if zone_counts.get(z.id)]
    for z in zones:
        z.table_count = zone_counts[z.id]
    tables_url = None
    if request.club.ns == NS_RESTAURANT:
        tables_url = reverse('restaurant_admin:table_list', kwargs={'restaurant_slug': request.club.slug})
    return _render(request, 'floor_plan.html', 'club_floor_plan',
                   events_list=events_list, selected_event=selected_event, zones=zones, metrics=metrics,
                   tables_data=rows, unassigned=unassigned, tables_url=tables_url)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan_api(request, restaurant_slug, event_id):
    event = _event(request, event_id)
    rows, unassigned, metrics = _floor_data(request, event)
    return JsonResponse({'success': True, 'tables': rows, 'unassigned': unassigned, 'metrics': metrics})


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan_assign_table(request, restaurant_slug, event_id):
    """POST JSON {booking_id, table_id|null, force_swap?}: give a booking a table, or take it away."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=405)
    event = _event(request, event_id)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON body.'}, status=400)
    booking = EventBooking.objects.filter(pk=data.get('booking_id'), event=event, payment_status=SUCCESS).first() \
        if str(data.get('booking_id', '')).isdigit() else None
    if not booking:
        return JsonResponse({'success': False, 'error': 'Booking not found for this event.'}, status=404)

    table_id = data.get('table_id')
    with transaction.atomic():
        if table_id:
            table = _tables(request).filter(pk=table_id).first() if str(table_id).isdigit() else None
            if not table:
                return JsonResponse({'success': False, 'error': 'Table not found for this branch.'}, status=404)
            holder = EventBooking.objects.filter(event=event, assigned_table=table, payment_status=SUCCESS).exclude(pk=booking.pk).first()
            if holder:
                if not data.get('force_swap'):
                    return JsonResponse({'success': False, 'conflict': True,
                                         'error': f'{table.name} is already held by {holder.guest_name} ({holder.booking_reference}). Move them off it?',
                                         'existing_holder': {'id': holder.id, 'name': holder.guest_name, 'reference': holder.booking_reference}},
                                        status=409)
                holder.assigned_table, holder.assigned_at = None, None
                holder.save(update_fields=['assigned_table', 'assigned_at'])
            booking.assigned_table, booking.assigned_at = table, timezone.now()
            booking.save(update_fields=['assigned_table', 'assigned_at'])
            warn = f' Heads up: {booking.quantity} guests on a {table.capacity}-seat table.' if booking.quantity > table.capacity else ''
            return JsonResponse({'success': True, 'message': f'{table.name} given to {booking.guest_name}.{warn}',
                                 'booking_id': booking.id, 'table_id': table.id, 'table_name': table.name,
                                 'status': 'CHECKED_IN' if booking.checked_in else 'RESERVED'})
        prev = booking.assigned_table
        booking.assigned_table, booking.assigned_at = None, None
        booking.save(update_fields=['assigned_table', 'assigned_at'])
        return JsonResponse({'success': True, 'message': f'{booking.guest_name} moved off {prev.name if prev else "the table"}.',
                             'booking_id': booking.id, 'table_id': prev.id if prev else None, 'status': 'AVAILABLE'})


# ── Ticket grid ─────────────────────────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
@club_module_required
def club_ticket_grid(request, restaurant_slug, event_id=None):
    """Every ticket of every tier as a numbered seat: available, held at checkout, sold or checked in."""
    events = _events(request).order_by('-start_datetime')
    selected_event = _pick_event(request, event_id)
    ctx = {'events': events, 'events_list': events, 'selected_event': selected_event}
    if selected_event:
        _expire_holds()
        tiers = list(selected_event.ticket_types.all().order_by('display_order', 'id'))
        ctx.update(ticket_types=tiers, active_tiers=[t for t in tiers if t.is_active],
                   total_allocated=sum(t.total_allocated for t in tiers), total_sold=sum(t.quantity_sold for t in tiers),
                   total_available=sum(t.available_quantity for t in tiers), total_holds=sum(t.active_holds_count for t in tiers),
                   total_checked_in=selected_event.total_attendees_checked_in, total_revenue=selected_event.total_revenue)
    return _render(request, 'ticket_grid.html', 'club_ticket_grid', **ctx)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_ticket_grid_api(request, restaurant_slug, event_id):
    """Live JSON for the ticket grid: one slot per ticket of each tier."""
    event = _event(request, event_id)
    _expire_holds()
    now = timezone.now()
    show_contact = _staff_flags(request)['can_view_sales']
    tiers_out, totals = [], {'allocated': 0, 'sold': 0, 'checked_in': 0, 'holds': 0}

    def stamp(dt):
        return timezone.localtime(dt).strftime('%d %b, %I:%M %p') if dt else None

    for tt in event.ticket_types.all().order_by('display_order', 'id'):
        allocated = tt.total_allocated
        totals['allocated'] += allocated
        slots, n = [], 1
        bookings = list(tt.bookings.filter(payment_status=SUCCESS).select_related('assigned_table').order_by('created_at')) + \
            list(tt.bookings.filter(payment_status=EventBooking.PaymentStatus.PENDING, expires_at__gt=now).order_by('created_at'))
        for b in bookings:
            held = b.payment_status != SUCCESS
            for _ in range(b.quantity):
                if n > allocated:
                    break
                status = 'PENDING_HOLD' if held else ('CHECKED_IN' if b.checked_in else 'SOLD')
                totals['holds' if held else 'sold'] += 1
                totals['checked_in'] += status == 'CHECKED_IN'
                slots.append({'slot_number': f'{n:02d}', 'raw_number': n, 'status': status, 'booking_id': b.id,
                              'booking_reference': b.booking_reference, 'guest_name': b.guest_name,
                              'guest_phone': b.guest_phone if show_contact else '', 'guest_email': b.guest_email if show_contact else '',
                              'unit_price': str(b.unit_price), 'total_amount': str(b.total_amount), 'quantity': b.quantity,
                              'checked_in': b.checked_in, 'checked_in_at': stamp(b.checked_in_at), 'booked_at': stamp(b.created_at),
                              'assigned_table': b.assigned_table.name if not held and b.assigned_table else None,
                              'payment_method': b.get_payment_method_display(),
                              'hold_expires_in_seconds': max(0, int((b.expires_at - now).total_seconds())) if held and b.expires_at else 0,
                              'pass_url': reverse('events:customer-booking-confirmation', args=[b.booking_reference]) if not held else None})
                n += 1
        while n <= allocated:
            slots.append({'slot_number': f'{n:02d}', 'raw_number': n, 'status': 'AVAILABLE', 'unit_price': str(tt.base_price)})
            n += 1
        sold = sum(1 for s in slots if s['status'] in ('SOLD', 'CHECKED_IN'))
        tiers_out.append({
            'id': tt.id, 'name': tt.name, 'description': tt.description, 'base_price': str(tt.base_price), 'is_active': tt.is_active,
            'total_allocated': allocated, 'sold_count': sold,
            'checked_in_count': sum(1 for s in slots if s['status'] == 'CHECKED_IN'),
            'available_count': sum(1 for s in slots if s['status'] == 'AVAILABLE'),
            'hold_count': sum(1 for s in slots if s['status'] == 'PENDING_HOLD'),
            'occupancy_rate': round(sold / allocated * 100, 1) if allocated else 0, 'slots': slots,
        })
    return JsonResponse({
        'event': {'id': event.id, 'title': event.title, 'start_datetime': event.start_datetime.isoformat(),
                  'end_datetime': event.end_datetime.isoformat(), 'venue_name': event.venue_name},
        'metrics': {'total_allocated': totals['allocated'], 'total_sold': totals['sold'],
                    'total_available': max(0, totals['allocated'] - totals['sold'] - totals['holds']),
                    'total_checked_in': totals['checked_in'], 'total_holds': totals['holds'],
                    'total_revenue': str(event.total_revenue),
                    'occupancy_rate': round(totals['sold'] / totals['allocated'] * 100, 1) if totals['allocated'] else 0},
        'ticket_types': tiers_out,
    })
