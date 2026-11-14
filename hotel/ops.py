"""Hotel operations beyond the stay itself: guest requests, laundry, spa, banquets, sales,
packages & promotions, cashier shifts, compliance and the action centre.

Every function takes plain model objects and returns ``(result, error)`` or an error string,
the same convention as ``hotel.folio``, so views stay thin and tests can call these directly.
"""
import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from hotel import folio as Fo
from hotel.models import (BanquetEvent, BanquetPayment, CashierShift, ComplianceLicence, FolioCharge, FolioPayment, GuestRequest,
                          LaundryLine, LaundryOrder, LaundryRate, LostFoundItem, MaintenanceIssue, PromoCode, Room, RoomBooking,
                          SafetyCheck, SalesLead, SecurityIncident, ServiceContract, SpaAppointment, StatutoryFiling, Visitor)

ZERO = Decimal('0')
CENT = Decimal('0.01')
OPEN_STAY = (RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN)


def _alert(branch, title, body, url, level='warning', key=''):
    try:
        from notifications import service
        service.notify(branch.restaurant, 'room_alert', title, body=body, branch=branch, url=url, source='hotel',
                       level=level, dedupe_key=key)
    except Exception:
        pass


def _url(branch, name, **kw):
    try:
        return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': branch.restaurant.slug, **kw})
    except Exception:
        return ''


def _postable(booking):
    """The folio can take a charge (guest is booked or in house)."""
    return booking is not None and booking.booking_status in OPEN_STAY


# ============================================================ guest requests

def log_request(branch, user, *, description, kind, department, priority='NORMAL', booking=None, room=None, guest_name=''):
    description = (description or '').strip()
    if not description:
        return None, 'Describe what the guest needs.'
    if kind not in GuestRequest.Kind.values or department not in GuestRequest.Department.values:
        return None, 'Choose the type and department.'
    if booking is not None:
        room = booking.room
        guest_name = guest_name or booking.guest_name
    sla = 0 if kind == GuestRequest.Kind.COMPLIMENT else GuestRequest.SLA_MINUTES.get(department, 30)
    if priority == 'HIGH' and sla:
        sla = max(10, sla // 2)
    req = GuestRequest.objects.create(
        branch=branch, booking=booking, room=room, guest_name=guest_name[:150], kind=kind, department=department,
        priority='HIGH' if priority == 'HIGH' else 'NORMAL', description=description, sla_minutes=sla, created_by=user,
        status=GuestRequest.Status.RESOLVED if kind == GuestRequest.Kind.COMPLIMENT else GuestRequest.Status.OPEN,
        resolved_at=timezone.now() if kind == GuestRequest.Kind.COMPLIMENT else None)
    if kind == GuestRequest.Kind.COMPLAINT:
        where = f'Room {room.room_number}' if room else 'Guest'
        _alert(branch, f'Complaint · {where}', description[:200], _url(branch, 'requests'),
               level='critical' if req.priority == 'HIGH' else 'warning', key=f'gr-{req.pk}')
    return req, None


def acknowledge_request(req, user):
    if req.status != GuestRequest.Status.OPEN:
        return 'Only open tickets can be acknowledged.'
    req.status, req.acknowledged_at = GuestRequest.Status.ACKNOWLEDGED, timezone.now()
    req.save(update_fields=['status', 'acknowledged_at'])
    return None


def resolve_request(req, user, resolution, allowance=None):
    if req.status == GuestRequest.Status.RESOLVED:
        return 'This ticket is already resolved.'
    resolution = (resolution or '').strip()
    if not resolution:
        return 'Say what was done to resolve it.'
    allowance = Decimal(allowance or 0)
    with transaction.atomic():
        if allowance > 0:
            if req.kind != GuestRequest.Kind.COMPLAINT or not _postable(req.booking):
                return 'An allowance can only be given on a complaint from a guest who is still booked or in house.'
            charge, err = Fo.post_charge(req.booking, user, FolioCharge.Kind.DISCOUNT,
                                         f'Service recovery · ticket #{req.pk}', allowance, 1, 0)
            if err:
                return err
            req.allowance = charge
        req.status, req.resolution, req.resolved_by, req.resolved_at = GuestRequest.Status.RESOLVED, resolution, user, timezone.now()
        if not req.acknowledged_at:
            req.acknowledged_at = req.resolved_at
        req.save()
    return None


# ============================================================ laundry

STANDARD_LAUNDRY = [('Shirt', 120, 220, 60), ('Trousers', 140, 240, 70), ('T-shirt', 90, None, 50), ('Kurta', 130, 240, 60),
                    ('Saree', None, 450, 120), ('Suit (2 piece)', None, 650, 220), ('Dress', 160, 380, 90),
                    ('Jacket', None, 520, 150), ('Undergarments', 50, None, None), ('Socks (pair)', 40, None, None)]


def seed_laundry_rates(branch):
    for i, (item, wash, dry, press) in enumerate(STANDARD_LAUNDRY):
        LaundryRate.objects.get_or_create(branch=branch, item=item, defaults={
            'wash_price': wash, 'dry_clean_price': dry, 'press_price': press, 'sort_order': i})


def create_laundry(branch, user, booking, quantities, express=False, notes=''):
    """``quantities``: {(rate_id, service): qty}."""
    if not _postable(booking):
        return None, 'Choose a guest who is booked or in house.'
    rates = {r.pk: r for r in LaundryRate.objects.filter(branch=branch, is_active=True)}
    lines = []
    for (rate_id, service), qty in quantities.items():
        r = rates.get(rate_id)
        price = r.price(service) if r else None
        if qty > 0 and price:
            lines.append(LaundryLine(item=r.item, service=service, quantity=qty, rate=price))
    if not lines:
        return None, 'Enter at least one item.'
    with transaction.atomic():
        order = LaundryOrder.objects.create(branch=branch, booking=booking, is_express=express, notes=(notes or '')[:255],
                                            created_by=user)
        for l in lines:
            l.order = order
        LaundryLine.objects.bulk_create(lines)
    return order, None


def advance_laundry(order, user):
    nxt = order.next_status
    if not nxt:
        return 'This order is already finished.'
    with transaction.atomic():
        if nxt == LaundryOrder.Status.DELIVERED and order.charge_id is None:
            if not _postable(order.booking):
                return 'The guest has checked out. Settle this order at the desk instead.'
            desc = f'Laundry {order.reference}' + (' (express)' if order.is_express else '')
            charge, err = Fo.post_charge(order.booking, user, FolioCharge.Kind.LAUNDRY, desc, order.amount, 1,
                                         LaundryOrder.GST_PERCENT, reference=order.reference)
            if err:
                return err
            order.charge, order.delivered_at = charge, timezone.now()
        order.status = nxt
        order.save()
    return None


# ============================================================ spa

def _minutes(t):
    return t.hour * 60 + t.minute


def spa_clash(branch, therapist, date, start, duration, exclude=None):
    begin, end = _minutes(start), _minutes(start) + duration
    qs = SpaAppointment.objects.filter(branch=branch, therapist=therapist, date=date, status__in=SpaAppointment.LIVE)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    for a in qs:
        if a.start_minutes < end and a.start_minutes + a.duration_minutes > begin:
            return a
    return None


def book_spa(branch, user, *, treatment, therapist, date, start, guest_name='', guest_phone='', booking=None, notes=''):
    if booking is not None:
        if not _postable(booking):
            return None, 'That booking is not active.'
        guest_name = guest_name or booking.guest_name
        guest_phone = guest_phone or booking.guest_phone
    if not (guest_name or '').strip():
        return None, 'Enter the guest’s name.'
    if date < timezone.localdate():
        return None, 'Pick today or a later date.'
    open_m, close_m = _minutes(SpaAppointment.OPEN_TIME), _minutes(SpaAppointment.CLOSE_TIME)
    if _minutes(start) < open_m or _minutes(start) + treatment.duration_minutes > close_m:
        return None, f'The spa is open {SpaAppointment.OPEN_TIME:%H:%M}–{SpaAppointment.CLOSE_TIME:%H:%M}; this treatment would not finish in time.'
    clash = spa_clash(branch, therapist, date, start, treatment.duration_minutes)
    if clash:
        return None, f'{therapist.name} is busy {clash.start_time:%H:%M}–{clash.end_time:%H:%M}. Pick another time or therapist.'
    appt = SpaAppointment.objects.create(
        branch=branch, treatment=treatment, therapist=therapist, date=date, start_time=start,
        duration_minutes=treatment.duration_minutes, booking=booking, guest_name=guest_name.strip()[:150],
        guest_phone=(guest_phone or '')[:30], price=treatment.price, gst_percent=treatment.gst_percent,
        notes=(notes or '')[:255], created_by=user)
    return appt, None


def spa_status(appt, status):
    if appt.status not in SpaAppointment.LIVE:
        return 'This appointment is already closed.'
    if status not in (SpaAppointment.Status.IN_TREATMENT, SpaAppointment.Status.CANCELLED, SpaAppointment.Status.NO_SHOW):
        return 'Unknown status.'
    appt.status = status
    appt.save(update_fields=['status'])
    return None


def complete_spa(appt, user, settled_by):
    if appt.status not in SpaAppointment.LIVE:
        return 'This appointment is already closed.'
    if settled_by not in SpaAppointment.Settlement.values:
        return 'Choose how it was settled.'
    with transaction.atomic():
        if settled_by == SpaAppointment.Settlement.ROOM:
            if not _postable(appt.booking):
                return 'Only in-house guests can charge the spa to their room.'
            charge, err = Fo.post_charge(appt.booking, user, FolioCharge.Kind.SPA, f'Spa · {appt.treatment.name}',
                                         appt.price, 1, appt.gst_percent, service_date=appt.date)
            if err:
                return err
            appt.charge = charge
        appt.status, appt.settled_by, appt.settled_at = SpaAppointment.Status.COMPLETED, settled_by, timezone.now()
        appt.save()
    return None


def spa_grid(appointments, therapists):
    """Rows of 30-minute slots × therapists; each cell is None (free), 'covered' or an appointment
    with its row span, for a scheduler table."""
    start, end = _minutes(SpaAppointment.OPEN_TIME), _minutes(SpaAppointment.CLOSE_TIME)
    slots = list(range(start, end, 30))
    by_t = {t.pk: [a for a in appointments if a.therapist_id == t.pk and a.status in SpaAppointment.LIVE + ('COMPLETED',)]
            for t in therapists}
    covered = set()
    rows = []
    for i, m in enumerate(slots):
        cells = []
        for t in therapists:
            if (t.pk, i) in covered:
                cells.append({'covered': True})
                continue
            appt = next((a for a in by_t[t.pk] if a.start_minutes <= m < a.start_minutes + 30), None)
            if appt:
                span = max(1, min(-(-appt.duration_minutes // 30), len(slots) - i))
                for k in range(1, span):
                    covered.add((t.pk, i + k))
                cells.append({'appt': appt, 'span': span})
            else:
                cells.append({'free': True, 'therapist': t, 'time': f'{m // 60:02d}:{m % 60:02d}'})
        rows.append({'label': f'{m // 60:02d}:{m % 60:02d}', 'cells': cells})
    return rows


# ============================================================ banquets

def _sessions_overlap(a, b):
    return a == b or BanquetEvent.Session.FULL_DAY in (a, b)


def venue_clash(event):
    if event.status not in BanquetEvent.HOLDS_VENUE:
        return None
    for other in (BanquetEvent.objects.filter(branch=event.branch, venue=event.venue, date=event.date,
                                              status__in=BanquetEvent.HOLDS_VENUE).exclude(pk=event.pk)):
        if _sessions_overlap(other.session, event.session):
            return other
    return None


def check_event(event):
    """Error for an event about to be saved, or None."""
    if event.pax < 1:
        return 'Enter the number of guests.'
    if event.pax > event.venue.capacity:
        return f'{event.venue.name} seats up to {event.venue.capacity}. Choose a larger venue or reduce the guest count.'
    clash = venue_clash(event)
    if clash:
        return f'{event.venue.name} is already held for “{clash.name}” ({clash.get_session_display()}). Pick another venue, session or date.'
    return None


def event_payment(event, user, amount, method, reference='', kind='PAYMENT'):
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:
        return None, 'Enter the amount as a number.'
    if amount <= 0:
        return None, 'Amount must be more than zero.'
    if method not in FolioPayment.Method.values or method in (FolioPayment.Method.COMPANY, FolioPayment.Method.OTA):
        return None, 'Choose how it was paid.'
    if kind == 'REFUND' and amount > event.paid:
        return None, 'You can’t refund more than has been paid.'
    p = BanquetPayment.objects.create(event=event, kind=kind, amount=amount, method=method, reference=(reference or '')[:100],
                                      received_by=user)
    if kind == 'PAYMENT' and event.status == BanquetEvent.Status.ENQUIRY:
        event.status = BanquetEvent.Status.TENTATIVE
        if check_event(event):
            event.status = BanquetEvent.Status.ENQUIRY
        else:
            event.save(update_fields=['status'])
    return p, None


def set_event_status(event, user, status, reason=''):
    if event.status in (BanquetEvent.Status.COMPLETED, BanquetEvent.Status.CANCELLED):
        return f'This event is already {event.get_status_display().lower()}.'
    if status == BanquetEvent.Status.CANCELLED:
        event.status, event.cancellation_reason = status, (reason or '').strip()[:255]
        event.save(update_fields=['status', 'cancellation_reason'])
        return None
    if status not in (BanquetEvent.Status.TENTATIVE, BanquetEvent.Status.CONFIRMED, BanquetEvent.Status.COMPLETED):
        return 'Unknown status.'
    old = event.status
    event.status = status
    err = check_event(event)
    if err:
        event.status = old
        return err
    if status == BanquetEvent.Status.COMPLETED and not event.invoice_number:
        event.invoice_number = Fo.next_invoice_number(event.branch)
    event.save()
    return None


# ============================================================ sales

def move_lead(lead, step):
    if not lead.is_open:
        return 'This deal is closed.'
    i = SalesLead.PIPELINE.index(lead.stage) + step
    if i < 0:
        return None
    if i >= len(SalesLead.PIPELINE):
        return 'Use “Mark won” to close the deal.'
    lead.stage = SalesLead.PIPELINE[i]
    lead.save(update_fields=['stage'])
    return None


def win_lead(lead, user, create=None):
    """Close as won; optionally set up the business it brings ('account' or 'event')."""
    from hotel.models import BanquetVenue, CompanyAccount
    if not lead.is_open:
        return None, 'This deal is already closed.'
    target = None
    with transaction.atomic():
        if create == 'account':
            target, _ = CompanyAccount.objects.get_or_create(
                restaurant=lead.branch.restaurant, name=lead.company[:150],
                defaults={'contact_name': lead.contact_name, 'phone': lead.phone, 'email': lead.email,
                          'notes': f'From sales deal: {lead.requirement[:200]}'})
            lead.company_account = target
        elif create == 'event':
            venue = BanquetVenue.objects.filter(branch=lead.branch, is_active=True).order_by('-capacity').first()
            if venue is None:
                return None, 'Add a banquet venue first.'
            target = BanquetEvent.objects.create(
                branch=lead.branch, name=lead.company[:150], client_name=lead.contact_name or lead.company,
                contact_phone=lead.phone, contact_email=lead.email, venue=venue,
                date=timezone.localdate() + datetime.timedelta(days=30), event_type='Wedding' if lead.lead_type == 'WEDDING' else 'Conference',
                pax=min(venue.capacity, 100), notes=lead.requirement, created_by=user)
            lead.event = target
        lead.stage, lead.closed_on, lead.next_action, lead.next_action_on = SalesLead.Stage.WON, timezone.localdate(), '', None
        lead.save()
    return target, None


def lose_lead(lead, reason):
    if not lead.is_open:
        return 'This deal is already closed.'
    if not (reason or '').strip():
        return 'Say why the deal was lost.'
    lead.stage, lead.lost_reason, lead.closed_on = SalesLead.Stage.LOST, reason.strip()[:200], timezone.localdate()
    lead.next_action, lead.next_action_on = '', None
    lead.save()
    return None


# ============================================================ packages & promotions

def apply_package(booking, user, package):
    if not _postable(booking):
        return None, 'Packages can only be added to an active booking.'
    nights = booking.nights_count
    if nights < package.min_nights:
        return None, f'{package.name} needs at least {package.min_nights} nights.'
    on = booking.check_in_date
    if (package.valid_from and on < package.valid_from) or (package.valid_until and on > package.valid_until):
        return None, f'{package.name} is not valid for these dates.'
    if booking.folio_charges.filter(is_void=False, reference=f'PKG-{package.pk}').exists():
        return None, 'This package is already on the folio.'
    return Fo.post_charge(booking, user, FolioCharge.Kind.MISC, f'Package · {package.name}', package.price_per_night,
                          nights, package.gst_percent, reference=f'PKG-{package.pk}')


def apply_promo(booking, user, code):
    if not _postable(booking):
        return None, 'Promo codes can only be applied to an active booking.'
    code = (code or '').strip().upper()
    with transaction.atomic():
        promo = PromoCode.objects.select_for_update().filter(branch=booking.branch, code=code).first()
        if promo is None:
            return None, f'There is no promo code {code}.'
        problem = promo.problem()
        if problem:
            return None, f'{code} {problem}.'
        if booking.folio_charges.filter(is_void=False, kind=FolioCharge.Kind.DISCOUNT, reference__startswith='PROMO-').exists():
            return None, 'A promo code is already applied to this booking.'
        amount = (Decimal(booking.total_room_charges or 0) * promo.percent / 100).quantize(CENT)
        if promo.max_discount:
            amount = min(amount, promo.max_discount)
        if amount <= 0:
            return None, 'There are no room charges to discount.'
        charge, err = Fo.post_charge(booking, user, FolioCharge.Kind.DISCOUNT, f'Promo {code} ({promo.percent:g}% off room)',
                                     amount, 1, 0, reference=f'PROMO-{code}')
        if err:
            return None, err
        PromoCode.objects.filter(pk=promo.pk).update(uses=F('uses') + 1)
    return charge, None


# ============================================================ cashier

def open_shift(branch):
    return CashierShift.objects.filter(branch=branch, closed_at__isnull=True).first()


def shift_takings(shift):
    """Cash and non-cash money taken at this branch while the shift was open."""
    end = shift.closed_at or timezone.now()
    window = {'created_at__gte': shift.opened_at, 'created_at__lte': end, 'is_void': False}
    fp = FolioPayment.objects.filter(booking__branch=shift.branch, received_at__gte=shift.opened_at, received_at__lte=end, is_void=False)
    by_method = {m: ZERO for m in FolioPayment.Method.values}
    for p in fp:
        by_method[p.method] = by_method.get(p.method, ZERO) + p.signed_amount
    for p in BanquetPayment.objects.filter(event__branch=shift.branch, **window):
        by_method[p.method] = by_method.get(p.method, ZERO) + p.signed
    spa = SpaAppointment.objects.filter(branch=shift.branch, status='COMPLETED', settled_at__gte=shift.opened_at, settled_at__lte=end)
    for a in spa:
        if a.settled_by in ('CASH', 'CARD', 'UPI'):
            by_method[a.settled_by] = by_method.get(a.settled_by, ZERO) + a.total
    paid_outs = shift.paid_outs.aggregate(t=Sum('amount'))['t'] or ZERO
    expected = shift.opening_float + by_method.get('CASH', ZERO) - paid_outs
    return {'by_method': by_method, 'paid_outs': paid_outs, 'expected': expected.quantize(CENT)}


def close_shift(shift, counts, handed_over_to='', notes=''):
    if not shift.is_open:
        return 'This shift is already closed.'
    counted = ZERO
    clean = {}
    for d in CashierShift.DENOMINATIONS:
        n = max(0, int(counts.get(str(d)) or counts.get(d) or 0))
        if n:
            clean[str(d)] = n
            counted += d * n
    t = shift_takings(shift)
    shift.denominations, shift.counted_cash, shift.expected_cash = clean, counted, t['expected']
    shift.closed_at, shift.handed_over_to, shift.notes = timezone.now(), (handed_over_to or '')[:120], (notes or '')[:255]
    shift.save()
    return None


# ============================================================ compliance

STANDARD_LICENCES = ['Fire NOC', 'FSSAI food licence', 'Trade licence', 'Pollution control consent (CTO)',
                     'Lift / elevator fitness certificate', 'Police / Sarai registration', 'Music performance licence',
                     'Bar licence']
STANDARD_SAFETY = [('Fire evacuation drill', 90), ('Fire extinguisher refill & check', 365), ('Overhead water tank cleaning', 180),
                   ('Pest control', 30), ('Kitchen exhaust duct cleaning', 120), ('DG set load test', 30),
                   ('First-aid kit audit', 30), ('Electrical safety audit', 365)]


def monthly_filings(branch, month):
    """Create the usual Indian statutory filings for a month (idempotent). ``month`` is the 1st of the month filed for."""
    nxt = (month.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    plan = [(f'TDS deposit · {month:%b %Y}', nxt.replace(day=7)), (f'GSTR-1 · {month:%b %Y}', nxt.replace(day=11)),
            (f'PF & ESI contribution · {month:%b %Y}', nxt.replace(day=15)), (f'GSTR-3B · {month:%b %Y}', nxt.replace(day=20))]
    made = 0
    for name, due in plan:
        _, created = StatutoryFiling.objects.get_or_create(branch=branch, name=name, defaults={'due_on': due})
        made += created
    return made


# ============================================================ action centre

def action_items(branch, levels):
    """What needs someone's attention now, grouped for the action centre. ``levels``: {area: level}."""
    today = timezone.localdate()
    now = timezone.now()
    soon = today + datetime.timedelta(days=30)
    items = []

    def add(area, icon, tone, title, count, url, detail=''):
        if count and levels.get(area, 'none') != 'none':
            items.append({'area': area, 'icon': icon, 'tone': tone, 'title': title, 'count': count, 'url': url, 'detail': detail})

    open_req = list(GuestRequest.objects.filter(branch=branch).exclude(status=GuestRequest.Status.RESOLVED))
    add('guest_services', 'fa-stopwatch', 'red', 'Guest requests past their response time',
        sum(1 for r in open_req if r.is_breached), _url(branch, 'requests'))
    add('guest_services', 'fa-face-frown', 'orange', 'Open complaints',
        sum(1 for r in open_req if r.kind == 'COMPLAINT'), _url(branch, 'requests') + '?tab=complaints')
    add('guest_services', 'fa-shirt', 'blue', 'Laundry ready to deliver',
        LaundryOrder.objects.filter(branch=branch, status='READY').count(), _url(branch, 'laundry'))
    add('guest_services', 'fa-spa', 'purple', 'Spa appointments still to come today',
        SpaAppointment.objects.filter(branch=branch, date=today, status='BOOKED').count(), _url(branch, 'spa'))
    add('guest_services', 'fa-box-archive', 'gray', 'Lost & found items past the 90-day hold',
        LostFoundItem.objects.filter(branch=branch, status='STORED', found_on__lt=today - datetime.timedelta(days=LostFoundItem.HOLD_DAYS)).count(),
        _url(branch, 'lostfound'))
    add('banquets', 'fa-champagne-glasses', 'indigo', 'Events today and tomorrow',
        BanquetEvent.objects.filter(branch=branch, date__in=[today, today + datetime.timedelta(days=1)],
                                    status__in=BanquetEvent.HOLDS_VENUE).count(), _url(branch, 'banquets'))
    add('banquets', 'fa-file-signature', 'orange', 'Event enquiries waiting more than 2 days for a quote',
        BanquetEvent.objects.filter(branch=branch, status='ENQUIRY', created_at__lt=now - datetime.timedelta(days=2), date__gte=today).count(),
        _url(branch, 'banquets') + '?status=ENQUIRY')
    due_events = [e for e in BanquetEvent.objects.filter(branch=branch, status='CONFIRMED', date__gte=today,
                                                       date__lte=today + datetime.timedelta(days=3)).select_related('venue', 'menu')
                  if e.balance > 0]
    add('banquets', 'fa-indian-rupee-sign', 'red', 'Confirmed events in the next 3 days with money due', len(due_events),
        _url(branch, 'banquets'))
    add('sales', 'fa-phone', 'orange', 'Sales follow-ups overdue',
        SalesLead.objects.filter(branch=branch, stage__in=SalesLead.PIPELINE, next_action_on__lt=today).count(), _url(branch, 'sales'))
    shift = open_shift(branch)
    add('finance', 'fa-cash-register', 'orange', 'Cashier shift open for more than 12 hours',
        1 if shift and shift.opened_at < now - datetime.timedelta(hours=12) else 0, _url(branch, 'cashier'))
    add('back_office', 'fa-shield-halved', 'red', 'Open high-severity security incidents',
        SecurityIncident.objects.filter(branch=branch, is_closed=False, severity__in=['HIGH', 'CRITICAL']).count(), _url(branch, 'security'))
    add('back_office', 'fa-person-walking-arrow-right', 'blue', 'Visitors still on the premises',
        Visitor.objects.filter(branch=branch, time_out__isnull=True, time_in__date=today).count(), _url(branch, 'security'))
    lic = ComplianceLicence.objects.filter(branch=branch, expires_on__lte=soon)
    add('back_office', 'fa-certificate', 'red', 'Licences expired or expiring within 30 days', lic.count(), _url(branch, 'compliance'),
        ', '.join(l.name for l in lic[:3]))
    add('back_office', 'fa-file-invoice', 'orange', 'Statutory filings due within 7 days',
        StatutoryFiling.objects.filter(branch=branch, filed_on__isnull=True, due_on__lte=today + datetime.timedelta(days=7)).count(),
        _url(branch, 'compliance') + '?tab=filings')
    overdue_checks = [c for c in SafetyCheck.objects.filter(branch=branch) if c.next_due is None or c.next_due < today]
    add('back_office', 'fa-fire-extinguisher', 'red', 'Safety checks overdue', len(overdue_checks), _url(branch, 'compliance') + '?tab=safety')
    add('back_office', 'fa-file-contract', 'orange', 'Service contracts ending within 30 days',
        ServiceContract.objects.filter(branch=branch, end_on__lte=soon).count(), _url(branch, 'assets') + '?tab=contracts')
    add('rooms', 'fa-screwdriver-wrench', 'red', 'Rooms out of order today',
        sum(1 for i in MaintenanceIssue.objects.filter(branch=branch, out_of_order=True).exclude(status='RESOLVED')
            if i.blocks(today, today + datetime.timedelta(days=1))), _url(branch, 'room_maintenance'))
    foreign = [b for b in RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN') if b.is_foreign_national]
    add('front_desk', 'fa-passport', 'orange', 'Foreign guests in house (check Form C is filed)', len(foreign),
        _url(branch, 'reports') + '?tab=formc')

    # Stores & inventory action items
    try:
        from hotel_inventory import services as store_services
        from hotel_inventory.models import Requisition, PurchaseOrder
        low_lvls, low_its = store_services.low_stock(branch)
        tot_low = len(low_lvls) + len(low_its)
        add('stores', 'fa-boxes-stacked', 'orange', 'Store items below minimum or par level', tot_low,
            _url(branch, 'stores') + '?low=1')

        pending_reqs = Requisition.objects.filter(branch=branch, status=Requisition.Status.SUBMITTED).count()
        add('stores', 'fa-file-lines', 'blue', 'Requisitions waiting for approval', pending_reqs,
            _url(branch, 'stores_requisitions'))

        pending_pos = PurchaseOrder.objects.filter(branch=branch, status__in=[PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIALLY_RECEIVED]).count()
        add('stores', 'fa-truck-fast', 'indigo', 'Purchase orders awaiting delivery', pending_pos,
            _url(branch, 'stores_pos'))
    except Exception:
        pass

    order = {'red': 0, 'orange': 1, 'indigo': 2, 'purple': 2, 'blue': 3, 'gray': 4}
    items.sort(key=lambda i: order.get(i['tone'], 5))
    return items


# ============================================================ forecast

def forecast(branch, days=30):
    """On-the-books occupancy for the next ``days`` nights, with a simple pickup estimate and a
    rate recommendation per night."""
    today = timezone.localdate()
    rooms = Room.objects.filter(branch=branch, is_active=True).count()
    end = today + datetime.timedelta(days=days)
    blocked = {}
    for issue in MaintenanceIssue.objects.filter(branch=branch, out_of_order=True).exclude(status='RESOLVED'):
        for n in range(days):
            d = today + datetime.timedelta(days=n)
            if issue.blocks(d, d + datetime.timedelta(days=1)):
                blocked[d] = blocked.get(d, 0) + 1
    stays = list(RoomBooking.objects.filter(branch=branch, booking_status__in=OPEN_STAY, check_in_date__lt=end, check_out_date__gt=today)
                 .values_list('check_in_date', 'check_out_date'))
    # Pace: how many rooms each weekday usually ends up with (last 8 weeks), and what share of
    # bookings usually arrive within N days of the night. Pickup = the usual gap still to come.
    past = list(RoomBooking.objects.filter(branch=branch, check_in_date__lt=today, check_out_date__gt=today - datetime.timedelta(days=56))
                .exclude(booking_status__in=['CANCELLED', 'NO_SHOW']).values_list('created_at', 'check_in_date', 'check_out_date'))
    sold = {}
    for n in range(1, 57):
        d = today - datetime.timedelta(days=n)
        sold.setdefault(d.weekday(), []).append(sum(1 for _, ci, co in past if ci <= d < co))
    usual = {wd: sum(v) / len(v) for wd, v in sold.items()}
    lead = [max(0, (ci - ca.date()).days) for ca, ci, _ in past if ca]
    rows = []
    for n in range(days):
        d = today + datetime.timedelta(days=n)
        sellable = max(0, rooms - blocked.get(d, 0))
        otb = sum(1 for ci, co in stays if ci <= d < co)
        still_to_come = (sum(1 for x in lead if x < n) / len(lead)) if lead else 0
        pickup = int(round(max(0, usual.get(d.weekday(), 0) - otb) * still_to_come))
        fc = min(sellable, otb + pickup)
        pct = round(fc / sellable * 100) if sellable else 0
        change = 15 if pct >= 85 else 8 if pct >= 70 else -12 if pct <= 30 else -6 if pct <= 45 else 0
        rows.append({'date': d, 'sellable': sellable, 'otb': otb, 'pickup': pickup, 'forecast': fc, 'pct': pct,
                     'otb_pct': round(otb / sellable * 100) if sellable else 0, 'change': change, 'weekend': d.weekday() >= 4})
    los = list(RoomBooking.objects.filter(branch=branch, check_in_date__gte=today - datetime.timedelta(days=90))
               .exclude(booking_status='CANCELLED').values_list('check_in_date', 'check_out_date'))
    los_n = [(co - ci).days for ci, co in los]

    def buckets(values, edges):
        out = []
        for label, lo, hi in edges:
            c = sum(1 for v in values if lo <= v <= hi)
            out.append({'label': label, 'count': c, 'pct': round(c / len(values) * 100) if values else 0})
        return out

    return {
        'rows': rows, 'rooms': rooms,
        'avg_pct': round(sum(r['pct'] for r in rows) / len(rows)) if rows else 0,
        'lead': buckets(lead, [('Same day', 0, 0), ('1–7 days', 1, 7), ('8–30 days', 8, 30), ('31+ days', 31, 10_000)]),
        'los': buckets(los_n, [('1 night', 1, 1), ('2 nights', 2, 2), ('3 nights', 3, 3), ('4–6', 4, 6), ('7+', 7, 10_000)]),
        'avg_lead': round(sum(lead) / len(lead), 1) if lead else None,
        'avg_los': round(sum(los_n) / len(los_n), 1) if los_n else None,
    }
