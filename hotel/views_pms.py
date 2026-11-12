"""Hotel PMS screens: booking folio, housekeeping, maintenance, rate calendar, reports and night audit."""
import csv
import datetime
import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from hotel import folio as F
from hotel.paging import paginate
from hotel import pricing, reports
from hotel import groups as G
from hotel import ledger as L
from hotel.models import (BookingGroup, CompanyAccount, FolioCharge, FolioPayment, HotelSettings, HousekeepingTask, LedgerPayment,
                          MaintenanceIssue, NightAudit, PromoCode, RateOverride, Room, RoomBooking, RoomCategory, RoomPackage)
from hotel.views_admin import hotel_admin_required
from super_admin import permissions as P

SHELL = 'guests/shell_hotel.html'
GSTIN_RE = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$')
DEFAULT_GST = {'RESTAURANT': 5, 'ROOM_SERVICE': 5, 'MINIBAR': 18, 'LAUNDRY': 18, 'SPA': 18, 'TRANSPORT': 5,
               'DAMAGE': 18, 'MISC': 18, 'DISCOUNT': 0}
MAX_UPLOAD = 5 * 1024 * 1024
PHONE_RE = re.compile(r'^(?:\+?91)?[6-9]\d{9}$|^\+[1-9]\d{6,14}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
ID_RULES = {
    'AADHAAR': (re.compile(r'^[2-9]\d{11}$'), "Aadhaar numbers have 12 digits and don't start with 0 or 1."),
    'PAN': (re.compile(r'^[A-Z]{5}\d{4}[A-Z]$'), 'PAN looks like ABCDE1234F.'),
    'PASSPORT': (re.compile(r'^[A-Z0-9]{6,9}$'), 'Passport numbers are 6-9 letters and digits.'),
    'VOTER_ID': (re.compile(r'^[A-Z]{3}\d{7}$'), 'Voter ID (EPIC) looks like ABC1234567.'),
    'DRIVING_LICENCE': (re.compile(r'^[A-Z]{2}[0-9A-Z-]{8,16}$'), 'Driving licence numbers start with the 2-letter state code, e.g. MH1420110012345.'),
}


def _base(slug):
    return reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': slug})


def _render(request, ctx, partial, menu, title, **extra):
    return render(request, SHELL, {**ctx, **extra, 'partial': partial, 'active_menu': menu, 'page_title': title,
                                   'base_url': _base(ctx['rslug'])})


def _can(request, area):
    return P.level(request.user, area) == P.MANAGE


def _date(value, default=None):
    try:
        return datetime.date.fromisoformat((value or '').strip())
    except ValueError:
        return default


def _money(value):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def _back(request, fallback):
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect(fallback)


def _report(request, error, ok=None):
    if error:
        messages.error(request, error)
    elif ok:
        messages.success(request, ok)


# ============================================================ booking folio

def _booking(ctx, booking_id):
    return get_object_or_404(RoomBooking.objects.select_related('room', 'room__category', 'branch'),
                             id=booking_id, branch=ctx['current_branch'])


def _detail_url(ctx, booking):
    return reverse('hotel_admin:booking_detail', kwargs={'restaurant_slug': ctx['rslug'], 'booking_id': booking.pk})


@hotel_admin_required
def booking_detail(request, restaurant_slug, ctx, booking_id):
    return _booking_page(request, ctx, _booking(ctx, booking_id))


def _booking_page(request, ctx, b, guest_errors=None, tab=None):
    folio = F.Folio(b)
    today = timezone.localdate()
    settings = HotelSettings.get_settings(b.branch)
    start = max(b.check_in_date, today) if b.booking_status == 'CHECKED_IN' else b.check_in_date
    move_rooms = []
    if b.booking_status in ('RESERVED', 'CHECKED_IN') and b.check_out_date > start:
        rooms = Room.objects.filter(branch=b.branch, is_active=True).exclude(pk=b.room_id).select_related('category')
        move_rooms = list(pricing.free_rooms(rooms, b.branch, start, b.check_out_date, exclude_booking=b).order_by('category__name', 'room_number'))
    room_gst = float(pricing.tax_fraction(b.room.category, b.room.category.base_price_per_night, settings) * 100)
    gst = {k: DEFAULT_GST.get(k, room_gst) for k in FolioCharge.Kind.values}
    history = (RoomBooking.objects.filter(branch__restaurant=b.branch.restaurant, guest_phone=b.guest_phone)
               .exclude(pk=b.pk).exclude(guest_phone='').order_by('-check_in_date')[:5]) if b.guest_phone else []
    return _render(request, ctx, 'hotel_pms/_booking.html', 'bookings', f'Booking {b.booking_id}',
                   b=b, folio=folio, today=today, settings=settings, move_rooms=move_rooms,
                   charge_kinds=FolioCharge.Kind.choices, methods=FolioPayment.Method.choices,
                   id_types=RoomBooking.IdType.choices, sources=RoomBooking.Source.choices,
                   event_types=RoomBooking.EventType.choices,
                   gst_json=gst, history=history, tasks=b.hk_tasks.select_related('room', 'assigned_to')[:10],
                   cabs=b.cab_bookings.all().order_by('pickup_datetime'), can_edit=_can(request, 'front_desk'),
                   kyc_missing=not b.kyc_complete, is_overdue=b.booking_status == 'CHECKED_IN' and b.check_out_date < today,
                   accounts=CompanyAccount.objects.filter(restaurant=b.branch.restaurant, is_active=True),
                   can_ledger=P.level(request.user, 'finance') != P.NONE,
                   packages=RoomPackage.objects.filter(branch=b.branch, is_active=True),
                   has_promos=PromoCode.objects.filter(branch=b.branch, is_active=True).exists(),
                   kyc_items=b.kyc_missing_items(require_document=settings.require_id_document),
                   kyc_enforced=settings.require_id_at_check_in,
                   guest_errors=guest_errors or {}, active_tab=tab or request.GET.get('tab', ''))


@hotel_admin_required
@require_POST
def booking_charge_add(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    p = request.POST
    _, err = F.post_charge(b, request.user, p.get('kind'), p.get('description'), _money(p.get('unit_price')) or 0,
                           _money(p.get('quantity')) or 1, _money(p.get('tax_percent')) or 0,
                           _date(p.get('service_date'), timezone.localdate()), p.get('reference'))
    _report(request, err, 'Charge posted to the folio.')
    return redirect(_detail_url(ctx, b) + '#folio')


@hotel_admin_required
@require_POST
def booking_charge_void_action(request, restaurant_slug, ctx, booking_id, charge_id):
    b = _booking(ctx, booking_id)
    charge = get_object_or_404(FolioCharge, pk=charge_id, booking=b)
    _report(request, F.void_charge(charge, request.user, request.POST.get('reason')), 'Charge voided.')
    return redirect(_detail_url(ctx, b) + '#folio')


@hotel_admin_required
@require_POST
def booking_payment_add(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    p = request.POST
    kind = FolioPayment.Kind.REFUND if p.get('kind') == 'REFUND' else FolioPayment.Kind.PAYMENT
    if p.get('method') == FolioPayment.Method.COMPANY and kind == FolioPayment.Kind.PAYMENT:
        account = CompanyAccount.objects.filter(restaurant=ctx['restaurant'], pk=p.get('company_account') or b.company_account_id or 0).first()
        _, err = L.bill_to_company(b, request.user, account, _money(p.get('amount')), override_limit=p.get('override_limit') == '1')
        _report(request, err, f'Billed to {account.name if account else "the company"}.')
        return redirect(_detail_url(ctx, b) + '#payments')
    _, err = F.record_payment(b, request.user, _money(p.get('amount')) or 0, p.get('method'), kind, p.get('reference'), p.get('note'))
    _report(request, err, 'Refund recorded.' if kind == 'REFUND' else 'Payment recorded.')
    return redirect(_detail_url(ctx, b) + '#payments')


@hotel_admin_required
@require_POST
def booking_payment_void_action(request, restaurant_slug, ctx, booking_id, payment_id):
    b = _booking(ctx, booking_id)
    pay = get_object_or_404(FolioPayment, pk=payment_id, booking=b)
    _report(request, F.void_payment(pay, request.user, request.POST.get('reason')), 'Payment voided.')
    return redirect(_detail_url(ctx, b) + '#payments')


@hotel_admin_required
@require_POST
def booking_guest_update(request, restaurant_slug, ctx, booking_id):
    """Validate the whole guest / registration form; on any error re-render it with the typed values."""
    b = _booking(ctx, booking_id)
    p = request.POST
    errors = {}

    def text(name, limit=None):
        v = (p.get(name) or '').strip()
        return v[:limit] if limit else v

    b.guest_name = text('guest_name', 150)
    if len(b.guest_name) < 2:
        errors['guest_name'] = "Enter the guest's full name."

    b.guest_phone = re.sub(r'[\s-]', '', text('guest_phone', 30))
    if not b.guest_phone:
        errors['guest_phone'] = 'A contact number is required.'
    elif not PHONE_RE.match(b.guest_phone):
        errors['guest_phone'] = 'Enter a valid phone number (10-digit Indian mobile, or + and country code).'

    b.guest_email = text('guest_email', 254)
    if b.guest_email and not EMAIL_RE.match(b.guest_email):
        errors['guest_email'] = 'Enter a valid email address.'

    for f in ('nationality', 'purpose_of_visit', 'arriving_from', 'going_to', 'visa_number', 'company_name'):
        setattr(b, f, text(f, RoomBooking._meta.get_field(f).max_length))
    b.id_type = p.get('id_type') if p.get('id_type') in RoomBooking.IdType.values else ''
    b.id_number = re.sub(r'\s', '', text('id_number', 60)).upper()
    if b.id_number and not b.id_type:
        errors['id_type'] = 'Choose the ID type.'
    if b.id_type and not b.id_number:
        errors['id_number'] = 'Enter the ID number.'
    elif b.id_type and b.id_number:
        rule = ID_RULES.get(b.id_type)
        if rule and not rule[0].match(b.id_number):
            errors['id_number'] = rule[1]

    today = timezone.localdate()
    b.date_of_birth = _date(p.get('date_of_birth'))
    if p.get('date_of_birth') and not b.date_of_birth:
        errors['date_of_birth'] = 'Enter a valid date.'
    elif b.date_of_birth and not (today.replace(year=today.year - 120) <= b.date_of_birth <= today):
        errors['date_of_birth'] = 'Date of birth must be in the past.'

    b.visa_valid_till = _date(p.get('visa_valid_till'))
    if b.is_foreign_national:
        if b.id_type and b.id_type != RoomBooking.IdType.PASSPORT:
            errors['id_type'] = 'Foreign nationals must register with a passport (Form C).'
        if b.visa_valid_till and b.visa_valid_till < b.check_in_date:
            errors['visa_valid_till'] = 'The visa expires before the arrival date.'

    doc = request.FILES.get('id_document')
    if doc:
        if doc.size > MAX_UPLOAD:
            errors['id_document'] = 'The ID scan must be 5 MB or smaller.'
        elif not (doc.content_type or '').startswith(('image/', 'application/pdf')):
            errors['id_document'] = 'Upload the ID as an image or PDF.'

    gstin = text('company_gstin').upper()
    if gstin and not GSTIN_RE.match(gstin):
        errors['company_gstin'] = 'Not a valid GSTIN (15 characters, e.g. 27AAPFU0939F1ZV).'
    b.company_gstin = gstin
    b.source = p.get('source') if p.get('source') in RoomBooking.Source.values else b.source
    account = CompanyAccount.objects.filter(restaurant=ctx['restaurant'], pk=p.get('company_account') or 0).first()
    b.company_account = account
    if account and not b.company_name:
        b.company_name = account.name
    if account and not b.company_gstin:
        b.company_gstin = account.gstin
    b.guest_address = text('guest_address')
    b.special_requests = text('special_requests')
    b.internal_notes = text('internal_notes')

    category = b.room.category
    entire = category.is_entire_property
    if entire:
        ev = (p.get('event_type') or '').upper()
        b.event_type = ev if ev in RoomBooking.EventType.values else ''
        hc = text('expected_headcount')
        b.expected_headcount = min(int(hc), 100000) if hc.isdigit() and int(hc) > 0 else None
        b.event_notes = text('event_notes', 2000)

    cap = max(20, category.max_occupancy) if entire else 20
    try:
        b.adults = int(p.get('adults') or 1)
        b.children = int(p.get('children') or 0)
    except ValueError:
        errors['adults'] = 'Enter whole numbers for guests.'
    else:
        if not 1 <= b.adults <= cap:
            errors['adults'] = f'Adults must be between 1 and {cap}.'
        elif not 0 <= b.children <= cap:
            errors['children'] = f'Children must be between 0 and {cap}.'
        elif b.adults + b.children > category.max_occupancy:
            errors['adults'] = (f'{category.name} takes at most {category.max_occupancy} guests. '
                                'Move the guest to a bigger room or split the booking.')

    if errors:
        messages.error(request, 'Guest details not saved. Fix the highlighted fields below.')
        b.id_document = RoomBooking.objects.only('id_document').get(pk=b.pk).id_document  # keep the stored scan
        return _booking_page(request, ctx, b, guest_errors=errors, tab='guest')

    if doc:
        b.id_document = doc
    b.num_guests = b.adults + b.children
    b.save()
    missing = b.kyc_missing_items(require_document=HotelSettings.get_settings(b.branch).require_id_document)
    if missing:
        messages.warning(request, f'Guest details saved. Still needed for registration: {", ".join(missing)}.')
    else:
        messages.success(request, 'Guest details saved. Registration is complete.')
    return redirect(_detail_url(ctx, b) + '?tab=guest')


@hotel_admin_required
@require_POST
def booking_dates_update(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    ci = _date(request.POST.get('check_in_date'), b.check_in_date)
    co = _date(request.POST.get('check_out_date'))
    if not co:
        messages.error(request, 'Choose the new departure date.')
    else:
        _report(request, F.change_dates(b, request.user, ci, co), f'Stay updated: {ci:%d %b} → {co:%d %b}. Room charges re-priced.')
    return redirect(_detail_url(ctx, b))


@hotel_admin_required
@require_POST
def booking_move_action(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    room = get_object_or_404(Room, pk=request.POST.get('room_id') or 0, branch=ctx['current_branch'])
    _report(request, F.move_room(b, request.user, room, reprice=request.POST.get('reprice') == '1'),
            f'Guest moved to room {room.room_number}.')
    return redirect(_detail_url(ctx, b))


@hotel_admin_required
@require_POST
def booking_no_show_action(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    _report(request, F.mark_no_show(b, request.user, charge_first_night=request.POST.get('charge') == '1'), 'Marked as a no-show.')
    return _back(request, _detail_url(ctx, b))


def _invoice_context(ctx, b):
    settings = HotelSettings.get_settings(b.branch)
    folio = F.Folio(b)
    return {**ctx, 'b': b, 'folio': folio, 'settings': settings, 'now': timezone.now(),
            'is_final': bool(b.invoice_number), 'restaurant': ctx['restaurant']}


@hotel_admin_required
def booking_invoice(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    return render(request, 'hotel_pms/invoice.html', _invoice_context(ctx, b))


@hotel_admin_required
def booking_registration_card(request, restaurant_slug, ctx, booking_id):
    b = _booking(ctx, booking_id)
    return render(request, 'hotel_pms/registration_card.html', _invoice_context(ctx, b))


# ============================================================ housekeeping

def _hk_url(ctx, day=None):
    url = reverse('hotel_admin:room_housekeeping', kwargs={'restaurant_slug': ctx['rslug']})
    return url + (f'?date={day.isoformat()}' if day else '')


def finish_cleaning(room, task=None):
    """A room whose cleaning is done goes back on sale, unless more cleaning is still open or a guest is in."""
    open_clean = HousekeepingTask.objects.filter(room=room, status__in=['PENDING', 'IN_PROGRESS'],
                                                 kind__in=['CHECKOUT_CLEAN', 'DEEP_CLEAN'])
    if task is not None:
        open_clean = open_clean.exclude(pk=task.pk)
    if room.status == Room.RoomStatus.CLEANING and not open_clean.exists():
        Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.AVAILABLE, updated_at=timezone.now())


@hotel_admin_required
def room_housekeeping(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    today = timezone.localdate()
    day = _date(request.GET.get('date'), today)
    staff_id = request.GET.get('staff') or ''
    tasks = (HousekeepingTask.objects.filter(branch=branch)
             .filter(Q(task_date=day) | Q(task_date__lt=day, status__in=['PENDING', 'IN_PROGRESS']))
             .select_related('room', 'room__category', 'assigned_to', 'booking'))
    if staff_id.isdigit():
        tasks = tasks.filter(assigned_to_id=int(staff_id))
    columns = [(s, label, [t for t in tasks if t.status == s]) for s, label in HousekeepingTask.Status.choices if s != 'SKIPPED']
    skipped = [t for t in tasks if t.status == 'SKIPPED']
    from restaurant_admin.models import StaffMember
    staff = StaffMember.objects.filter(restaurant=ctx['restaurant'], is_active=True).filter(
        Q(branch=branch) | Q(branch__isnull=True)).order_by('-department', 'name')
    hk_staff = [s for s in staff if s.department == 'HOUSEKEEPING'] or list(staff)
    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('room_number')
    in_house = set(RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN').values_list('room_id', flat=True))
    arriving = set(RoomBooking.objects.filter(branch=branch, booking_status='RESERVED', check_in_date=day).values_list('room_id', flat=True))
    done_today = HousekeepingTask.objects.filter(branch=branch, status='DONE', completed_at__date=day)
    times = [t.minutes_taken for t in done_today if t.minutes_taken is not None]
    by_staff = done_today.values('assigned_to__name').annotate(n=Count('id')).order_by('-n')
    return _render(request, ctx, 'hotel_pms/_housekeeping.html', 'housekeeping', 'Housekeeping',
                   day=day, today=today, columns=columns, skipped=skipped, staff=hk_staff, all_staff=staff, staff_id=staff_id,
                   rooms=rooms, in_house=in_house, arriving=arriving, kinds=HousekeepingTask.Kind.choices,
                   dirty_count=sum(1 for r in rooms if r.status == Room.RoomStatus.CLEANING),
                   done_count=done_today.count(), avg_minutes=round(sum(times) / len(times)) if times else None,
                   by_staff=by_staff, prev_day=day - datetime.timedelta(days=1), next_day=day + datetime.timedelta(days=1),
                   room_states=Room.RoomStatus.choices, can_edit=_can(request, 'rooms'))


@hotel_admin_required
@require_POST
def room_task_create(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    p = request.POST
    room_ids = [int(r) for r in p.getlist('room_id') if r.isdigit()]
    rooms = list(Room.objects.filter(branch=branch, pk__in=room_ids, is_active=True))
    kind = p.get('kind') if p.get('kind') in HousekeepingTask.Kind.values else HousekeepingTask.Kind.CHECKOUT_CLEAN
    day = _date(p.get('task_date'), timezone.localdate())
    staff = None
    if (p.get('assigned_to') or '').isdigit():
        from restaurant_admin.models import StaffMember
        staff = StaffMember.objects.filter(pk=int(p['assigned_to']), restaurant=ctx['restaurant']).first()
    if not rooms:
        messages.error(request, 'Pick at least one room.')
        return redirect(_hk_url(ctx, day))
    for room in rooms:
        HousekeepingTask.objects.create(branch=branch, room=room, kind=kind, task_date=day, assigned_to=staff,
                                        is_rush=p.get('is_rush') == '1', notes=(p.get('notes') or '')[:255], created_by=request.user)
        if kind in ('CHECKOUT_CLEAN', 'DEEP_CLEAN') and room.status == Room.RoomStatus.AVAILABLE:
            Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.CLEANING, updated_at=timezone.now())
    messages.success(request, f'{len(rooms)} task{"s" if len(rooms) != 1 else ""} added.')
    return redirect(_hk_url(ctx, day))


@hotel_admin_required
@require_POST
def room_task_generate(request, restaurant_slug, ctx):
    """Stay-over service for every occupied room that has none yet today."""
    branch = ctx['current_branch']
    today = timezone.localdate()
    made = 0
    for b in RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN', check_out_date__gt=today).select_related('room'):
        if not HousekeepingTask.objects.filter(room=b.room, task_date=today, kind='STAYOVER').exists():
            HousekeepingTask.objects.create(branch=branch, room=b.room, booking=b, kind='STAYOVER', task_date=today, created_by=request.user)
            made += 1
    messages.success(request, f'{made} stay-over task{"s" if made != 1 else ""} created.' if made else 'Every occupied room already has a stay-over task today.')
    return redirect(_hk_url(ctx))


@hotel_admin_required
@require_POST
def room_task_update(request, restaurant_slug, ctx, task_id):
    task = get_object_or_404(HousekeepingTask.objects.select_related('room'), pk=task_id, branch=ctx['current_branch'])
    op = request.POST.get('op')
    now = timezone.now()
    if op == 'assign':
        from restaurant_admin.models import StaffMember
        sid = request.POST.get('assigned_to') or ''
        task.assigned_to = StaffMember.objects.filter(pk=int(sid), restaurant=ctx['restaurant']).first() if sid.isdigit() else None
        task.save(update_fields=['assigned_to'])
    elif op == 'start':
        task.status, task.started_at = HousekeepingTask.Status.IN_PROGRESS, task.started_at or now
        task.save(update_fields=['status', 'started_at'])
    elif op == 'done':
        task.status, task.completed_at, task.completed_by = HousekeepingTask.Status.DONE, now, request.user
        task.started_at = task.started_at or now
        task.save(update_fields=['status', 'completed_at', 'completed_by', 'started_at'])
        if task.kind in ('CHECKOUT_CLEAN', 'DEEP_CLEAN'):
            finish_cleaning(task.room, task)
        messages.success(request, f'Room {task.room.room_number}: {task.get_kind_display().lower()} done.')
    elif op == 'skip':
        task.status = HousekeepingTask.Status.SKIPPED
        task.notes = (request.POST.get('notes') or task.notes or 'Skipped')[:255]
        task.save(update_fields=['status', 'notes'])
    elif op == 'reopen':
        task.status, task.completed_at = HousekeepingTask.Status.PENDING, None
        task.save(update_fields=['status', 'completed_at'])
    return _back(request, _hk_url(ctx))


# ============================================================ maintenance / out of order

def _sync_room_for_issue(room):
    today = timezone.localdate()
    blocked = room.id in pricing.blocked_room_ids(room.branch, today, today + datetime.timedelta(days=1))
    occupied = RoomBooking.objects.filter(room=room, booking_status='CHECKED_IN').exists()
    if blocked and not occupied and room.status != Room.RoomStatus.MAINTENANCE:
        Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.MAINTENANCE, updated_at=timezone.now())
    elif not blocked and room.status == Room.RoomStatus.MAINTENANCE:
        Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.AVAILABLE, updated_at=timezone.now())


@hotel_admin_required
def room_maintenance(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    show = request.GET.get('show', 'open')
    issues = MaintenanceIssue.objects.filter(branch=branch).select_related('room', 'reported_by')
    if show != 'all':
        issues = issues.exclude(status='RESOLVED')
    rows = []
    for i in issues[:200]:
        clashes = []
        if i.out_of_order and i.status != 'RESOLVED':
            qs = RoomBooking.objects.filter(room=i.room, booking_status='RESERVED')
            if i.block_from:
                qs = qs.filter(check_out_date__gt=i.block_from)
            if i.block_until:
                qs = qs.filter(check_in_date__lte=i.block_until)
            clashes = list(qs.order_by('check_in_date')[:5])
        rows.append((i, clashes))
    counts = MaintenanceIssue.objects.filter(branch=branch).exclude(status='RESOLVED').aggregate(
        open=Count('id'), ooo=Count('id', filter=Q(out_of_order=True)), urgent=Count('id', filter=Q(priority__in=['HIGH', 'URGENT'])))
    rooms = Room.objects.filter(branch=branch, is_active=True).order_by('room_number')
    return _render(request, ctx, 'hotel_pms/_maintenance.html', 'maintenance', 'Maintenance', rows=rows, show=show, counts=counts,
                   rooms=rooms, priorities=MaintenanceIssue.Priority.choices, today=timezone.localdate(), can_edit=_can(request, 'rooms'))


@hotel_admin_required
@require_POST
def room_issue_create(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    p = request.POST
    room = Room.objects.filter(pk=p.get('room_id') or 0, branch=branch).first()
    title = (p.get('title') or '').strip()
    url = reverse('hotel_admin:room_maintenance', kwargs={'restaurant_slug': ctx['rslug']})
    if not room or not title:
        messages.error(request, 'Choose a room and describe the problem.')
        return redirect(url)
    ooo = p.get('out_of_order') == '1'
    start, until = _date(p.get('block_from')), _date(p.get('block_until'))
    if ooo and start and until and until < start:
        messages.error(request, 'The “off sale until” date must be on or after the start date.')
        return redirect(url)
    issue = MaintenanceIssue.objects.create(
        branch=branch, room=room, title=title[:150], description=(p.get('description') or '').strip(),
        priority=p.get('priority') if p.get('priority') in MaintenanceIssue.Priority.values else 'MEDIUM',
        out_of_order=ooo, block_from=start if ooo else None, block_until=until if ooo else None,
        assigned_to=(p.get('assigned_to') or '').strip()[:120], reported_by=request.user)
    if ooo:
        _sync_room_for_issue(room)
    messages.success(request, f'Logged for room {room.room_number}.' + (' The room is off sale for those dates.' if ooo else ''))
    return redirect(url + f'#issue-{issue.pk}')


@hotel_admin_required
@require_POST
def room_issue_update(request, restaurant_slug, ctx, issue_id):
    issue = get_object_or_404(MaintenanceIssue.objects.select_related('room'), pk=issue_id, branch=ctx['current_branch'])
    op = request.POST.get('op')
    if op == 'start':
        issue.status = MaintenanceIssue.Status.IN_PROGRESS
        issue.save(update_fields=['status'])
    elif op == 'resolve':
        issue.status, issue.resolved_at = MaintenanceIssue.Status.RESOLVED, timezone.now()
        issue.resolution_note = (request.POST.get('resolution_note') or '').strip()[:255]
        cost = _money(request.POST.get('cost'))
        issue.cost = cost if cost and cost > 0 else issue.cost
        issue.save(update_fields=['status', 'resolved_at', 'resolution_note', 'cost'])
        messages.success(request, f'Room {issue.room.room_number}: “{issue.title}” resolved.')
    elif op == 'reopen':
        issue.status, issue.resolved_at = MaintenanceIssue.Status.OPEN, None
        issue.save(update_fields=['status', 'resolved_at'])
    _sync_room_for_issue(issue.room)
    return _back(request, reverse('hotel_admin:room_maintenance', kwargs={'restaurant_slug': ctx['rslug']}))


# ============================================================ rates calendar

@hotel_admin_required
def rates(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    today = timezone.localdate()
    start = _date(request.GET.get('start'), today)
    days = 30 if request.GET.get('days') == '30' else 14
    end = start + datetime.timedelta(days=days)
    dates = pricing.nights_between(start, end)
    categories = list(RoomCategory.objects.filter(branch=branch, is_active=True).order_by('base_price_per_night', 'name'))
    settings = HotelSettings.get_settings(branch)
    rooms = dict(Room.objects.filter(branch=branch, is_active=True).values('category_id').annotate(n=Count('id')).values_list('category_id', 'n'))
    booked = {}
    for b in RoomBooking.objects.filter(branch=branch, booking_status__in=pricing.ACTIVE_STAY, check_in_date__lt=end, check_out_date__gt=start).select_related('room'):
        for n in pricing.nights_between(max(b.check_in_date, start), min(b.check_out_date, end)):
            booked[(b.room.category_id, n)] = booked.get((b.room.category_id, n), 0) + 1
    grid = []
    for cat in categories:
        overrides = list(RateOverride.objects.filter(category=cat, start_date__lt=end, end_date__gte=start).order_by('-id'))
        cells = []
        for night, price, used in pricing.nightly_rates(cat, start, end, overrides):
            stop = any(o.stop_sell and o.applies_to(night) for o in overrides)
            min_stay = max([o.min_stay for o in overrides if o.applies_to(night)] or [1])
            left = rooms.get(cat.id, 0) - booked.get((cat.id, night), 0)
            cells.append({'date': night, 'price': price, 'special': used is not None, 'label': used.label if used else '',
                          'stop': stop, 'min_stay': min_stay, 'left': left, 'weekend': night.weekday() >= 5})
        grid.append({'cat': cat, 'cells': cells, 'rooms': rooms.get(cat.id, 0)})
    rules = RateOverride.objects.filter(category__branch=branch, end_date__gte=today).select_related('category').order_by('start_date')
    return _render(request, ctx, 'hotel_pms/_rates.html', 'rates', 'Rates & availability',
                   grid=grid, dates=dates, start=start, days=days, rules=rules, categories=categories, today=today,
                   prev=start - datetime.timedelta(days=days), next=start + datetime.timedelta(days=days),
                   weekdays=RateOverride.WEEKDAYS, settings=settings, can_edit=_can(request, 'revenue') and ctx['is_hotel_admin'])


@hotel_admin_required
@require_POST
def rate_save(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    p = request.POST
    url = reverse('hotel_admin:rates', kwargs={'restaurant_slug': ctx['rslug']})
    if not ctx['is_hotel_admin']:
        messages.error(request, 'Only hotel administrators can change rates.')
        return redirect(url)
    cats = list(RoomCategory.objects.filter(branch=branch, pk__in=[c for c in p.getlist('category') if c.isdigit()]))
    start, end = _date(p.get('start_date')), _date(p.get('end_date'))
    price = _money(p.get('price')) if (p.get('price') or '').strip() else None
    stop = p.get('stop_sell') == '1'
    try:
        min_stay = max(1, min(30, int(p.get('min_stay') or 1)))
    except ValueError:
        min_stay = 1
    weekdays = sorted({int(w) for w in p.getlist('weekdays') if w.isdigit() and 0 <= int(w) <= 6})
    error = None
    if not cats:
        error = 'Pick at least one room type.'
    elif not start or not end or end < start:
        error = 'Choose a valid date range.'
    elif (end - start).days > 400:
        error = 'A rule can cover at most 400 days.'
    elif price is not None and price <= 0:
        error = 'The price must be more than zero.'
    elif price is None and not stop and min_stay == 1:
        error = 'Set a price, a minimum stay or close the dates — otherwise the rule does nothing.'
    if error:
        messages.error(request, error)
        return redirect(url + f'?start={start or ""}')
    with transaction.atomic():
        for cat in cats:
            RateOverride.objects.create(category=cat, label=(p.get('label') or '').strip()[:80], start_date=start, end_date=end,
                                        weekdays=weekdays if len(weekdays) < 7 else [], price_per_night=price,
                                        min_stay=min_stay, stop_sell=stop, created_by=request.user)
    messages.success(request, f'Rate rule saved for {len(cats)} room type{"s" if len(cats) != 1 else ""}. Website prices update immediately.')
    return redirect(url + f'?start={start.isoformat()}')


@hotel_admin_required
@require_POST
def rate_delete(request, restaurant_slug, ctx, rule_id):
    rule = get_object_or_404(RateOverride, pk=rule_id, category__branch=ctx['current_branch'])
    if not ctx['is_hotel_admin']:
        messages.error(request, 'Only hotel administrators can change rates.')
        return _back(request, reverse('hotel_admin:rates', kwargs={'restaurant_slug': ctx['rslug']}))
    rule.delete()
    messages.success(request, 'Rate rule removed.')
    return _back(request, reverse('hotel_admin:rates', kwargs={'restaurant_slug': ctx['rslug']}))


# ============================================================ reports

def _period(request):
    key = request.GET.get('period') or '30d'
    start, end, label = reports.resolve_period(key, _date(request.GET.get('from')), _date(request.GET.get('to')))
    if (end - start).days > 366:
        start = end - datetime.timedelta(days=366)
    return key, start, end, label


@hotel_admin_required
def reports_view(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    tab = request.GET.get('tab') if request.GET.get('tab') in ('performance', 'lists', 'formc') else 'performance'
    key, start, end, label = _period(request)
    export = request.GET.get('export')
    if tab == 'formc':
        guests = reports.foreign_guests(branch, start, end)
        if export == 'csv':
            return _csv(f'form-c-{start}-{end}.csv', ['Booking', 'Guest', 'Nationality', 'Passport', 'Visa', 'Visa valid till', 'Date of birth',
                                                      'Arrived', 'Departs', 'Room', 'Arriving from', 'Going to', 'Purpose', 'Address', 'Phone'],
                        [[b.booking_id, b.guest_name, b.nationality, b.id_number, b.visa_number, b.visa_valid_till or '', b.date_of_birth or '',
                          b.check_in_date, b.check_out_date, b.room.room_number, b.arriving_from, b.going_to, b.purpose_of_visit,
                          b.guest_address, b.guest_phone] for b in guests])
        return _render(request, ctx, 'hotel_pms/_reports.html', 'reports', 'Reports', tab=tab, period=key, start=start, end=end,
                       label=label, periods=reports.PERIODS, guests=guests)
    if tab == 'lists':
        day = _date(request.GET.get('date'), timezone.localdate())
        lists = reports.front_office_lists(branch, day)
        which = request.GET.get('list') if request.GET.get('list') in lists else 'arrivals'
        if export == 'csv':
            return _csv(f'{which}-{day}.csv', ['Booking', 'Guest', 'Phone', 'Room', 'Type', 'Arrive', 'Depart', 'Guests', 'Status', 'Balance', 'Requests'],
                        [[b.booking_id, b.guest_name, b.guest_phone, b.room.room_number, b.room.category.name, b.check_in_date, b.check_out_date,
                          b.num_guests, b.get_booking_status_display(), b.folio().balance, b.special_requests] for b in lists[which]])
        return _render(request, ctx, 'hotel_pms/_reports.html', 'reports', 'Reports', tab=tab, day=day, lists=lists, which=which,
                       prev_day=day - datetime.timedelta(days=1), next_day=day + datetime.timedelta(days=1))
    s = reports.summary(branch, start, end)
    if export == 'csv':
        return _csv(f'hotel-performance-{start}-{end}.csv', ['Date', 'Rooms sold', 'Rooms available', 'Out of order', 'Occupancy %', 'Room revenue', 'ADR', 'RevPAR'],
                    [[r['date'], r['sold'], r['sellable'], r['ooo'], r['occupancy'], r['revenue'], r['adr'], r['revpar']] for r in s['rows']])
    chart = {'labels': [r['date'].strftime('%d %b') for r in s['rows']], 'occupancy': [r['occupancy'] for r in s['rows']],
             'adr': [float(r['adr']) for r in s['rows']], 'revenue': [float(r['revenue']) for r in s['rows']]}
    top_pay = max([v for _, v in s['payments']] or [0])
    top_src = max([x['bookings'] for x in s['sources']] or [0])
    return _render(request, ctx, 'hotel_pms/_reports.html', 'reports', 'Reports', tab=tab, period=key, start=start, end=end, label=label,
                   periods=reports.PERIODS, s=s, chart=chart,
                   payments=[(k, v, round(float(v) / float(top_pay) * 100) if top_pay else 0) for k, v in s['payments']],
                   sources=[{**x, 'share': round(x['bookings'] / top_src * 100) if top_src else 0} for x in s['sources']])


@hotel_admin_required
def management_dashboard(request, restaurant_slug, ctx):
    """How the hotel is doing over a period (vs the period before), today at a glance, and what needs attention."""
    from hotel import dashboard as D
    branch = ctx['current_branch']
    key = request.GET.get('period') or '7d'
    if key not in dict(D.PERIODS) and key != 'custom':
        key = '7d'
    start, end, label = reports.resolve_period(key, _date(request.GET.get('from')), _date(request.GET.get('to')))
    if key == 'custom' and label != 'Custom':
        key = '30d'
    if (end - start).days > 366:
        start = end - datetime.timedelta(days=366)
    d = D.compute(branch, start, end, P.access_map(request.user))
    if request.GET.get('export') == 'csv':
        return _csv(f'hotel-dashboard-{start}-{end}.csv', ['Date', 'Rooms sold', 'Rooms available', 'Occupancy %', 'Room revenue', 'ADR', 'RevPAR'],
                    [[r['date'], r['sold'], r['sellable'], r['occupancy'], r['revenue'], r['adr'], r['revpar']] for r in d['cur']['rows']])
    hour = timezone.localtime().hour
    greeting = 'Good morning' if hour < 12 else 'Good afternoon' if hour < 17 else 'Good evening'
    return _render(request, ctx, 'hotel_pms/_dashboard.html', 'mgmt_dashboard', 'Dashboard', d=d, live=D.live(branch),
                   period=key, period_label=label if key != 'custom' else 'Custom', periods=D.PERIODS,
                   today=timezone.localdate(), greeting=greeting)


def _csv(name, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(header)
    w.writerows(rows)
    return resp


# ============================================================ night audit

@hotel_admin_required
def night_audit(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    day = reports.business_date(branch)
    already = NightAudit.objects.filter(branch=branch, business_date=day).first()
    checks = reports.audit_checks(branch, day)
    snapshot = reports.day_snapshot(branch, day)
    history = NightAudit.objects.filter(branch=branch).select_related('closed_by')[:30]
    blockers = checks['pending_departures'].count()
    return _render(request, ctx, 'hotel_pms/_night_audit.html', 'night_audit', 'Night audit', day=day, already=already,
                   checks=checks, snap=snapshot, history=history, blockers=blockers, today=timezone.localdate(),
                   can_edit=_can(request, 'revenue'))


@hotel_admin_required
@require_POST
def night_audit_close(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    url = reverse('hotel_admin:night_audit', kwargs={'restaurant_slug': ctx['rslug']})
    day = reports.business_date(branch)
    if _date(request.POST.get('business_date')) != day:
        messages.error(request, 'The business date changed while you were on this page. Review it again.')
        return redirect(url)
    if NightAudit.objects.filter(branch=branch, business_date=day).exists():
        messages.info(request, f'{day:%d %b %Y} is already closed.')
        return redirect(url)
    checks = reports.audit_checks(branch, day)
    if checks['pending_departures'].exists():
        messages.error(request, 'Some guests were due to leave and are still checked in. Check them out or extend their stay first.')
        return redirect(url)
    no_shows = 0
    if request.POST.get('mark_no_shows') == '1':
        for b in checks['pending_arrivals'].filter(check_in_date__lte=day):
            if not F.mark_no_show(b, request.user, charge_first_night=request.POST.get('charge_no_shows') == '1'):
                no_shows += 1
    elif checks['pending_arrivals'].filter(check_in_date__lt=day).exists():
        messages.error(request, 'Guests from earlier days never arrived. Mark them as no-shows, cancel them, or move their dates.')
        return redirect(url)
    stayovers = 0
    if request.POST.get('stayovers') == '1':
        nxt = day + datetime.timedelta(days=1)
        for b in RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN', check_out_date__gt=nxt).select_related('room'):
            _, made = HousekeepingTask.objects.get_or_create(branch=branch, room=b.room, task_date=nxt, kind='STAYOVER',
                                                             defaults={'booking': b, 'created_by': request.user})
            stayovers += int(made)
    snapshot = reports.day_snapshot(branch, day)
    snapshot['no_shows_marked'] = no_shows
    NightAudit.objects.create(branch=branch, business_date=day, summary=snapshot, notes=(request.POST.get('notes') or '').strip(),
                              closed_by=request.user)
    extra = []
    if no_shows:
        extra.append(f'{no_shows} no-show{"s" if no_shows != 1 else ""} marked')
    if stayovers:
        extra.append(f'{stayovers} stay-over task{"s" if stayovers != 1 else ""} created for tomorrow')
    messages.success(request, f'Day closed: {day:%d %b %Y}.' + (' ' + ', '.join(extra) + '.' if extra else ''))
    return redirect(url)


@hotel_admin_required
def night_audit_detail(request, restaurant_slug, ctx, audit_id):
    audit = get_object_or_404(NightAudit, pk=audit_id, branch=ctx['current_branch'])
    return _render(request, ctx, 'hotel_pms/_night_audit_detail.html', 'night_audit', f'Night audit {audit.business_date:%d %b %Y}', audit=audit)


# ============================================================ city ledger

def _ledger_url(ctx, pk=None):
    if pk:
        return reverse('hotel_admin:ledger_account', kwargs={'restaurant_slug': ctx['rslug'], 'pk': pk})
    return reverse('hotel_admin:ledger', kwargs={'restaurant_slug': ctx['rslug']})


def _account(ctx, pk):
    return get_object_or_404(CompanyAccount, pk=pk, restaurant=ctx['restaurant'])


@hotel_admin_required
def ledger(request, restaurant_slug, ctx):
    rows = L.summary_rows(ctx['restaurant'])
    total = sum((r['balance'] for r in rows), Decimal('0'))
    overdue = sum((r['overdue'] for r in rows), Decimal('0'))
    if request.GET.get('export') == 'csv':
        return _csv('city-ledger.csv', ['Company', 'GSTIN', 'Balance', 'Overdue', 'Credit limit', 'Terms (days)'],
                    [[r['a'].name, r['a'].gstin, r['balance'], r['overdue'], r['a'].credit_limit or '', r['a'].payment_terms_days] for r in rows])
    return _render(request, ctx, 'hotel_pms/_ledger.html', 'ledger', 'City ledger', rows=rows, total=total, overdue=overdue,
                   can_edit=_can(request, 'finance'))


def _save_account(request, ctx, account=None):
    p = request.POST
    name = (p.get('name') or '').strip()
    gstin = (p.get('gstin') or '').strip().upper()
    if not name:
        return None, 'Company name is required.'
    if gstin and not GSTIN_RE.match(gstin):
        return None, f'“{gstin}” is not a valid GSTIN.'
    dup = CompanyAccount.objects.filter(restaurant=ctx['restaurant'], name__iexact=name)
    if account:
        dup = dup.exclude(pk=account.pk)
    if dup.exists():
        return None, f'There is already an account called {name}.'
    limit = _money(p.get('credit_limit')) if (p.get('credit_limit') or '').strip() else None
    if limit is not None and limit < 0:
        return None, 'The credit limit can’t be negative.'
    account = account or CompanyAccount(restaurant=ctx['restaurant'])
    account.name, account.gstin = name[:150], gstin
    for f in ('contact_name', 'email', 'phone'):
        setattr(account, f, (p.get(f) or '').strip()[:CompanyAccount._meta.get_field(f).max_length])
    account.address = (p.get('address') or '').strip()
    account.notes = (p.get('notes') or '').strip()
    account.credit_limit = limit
    account.payment_terms_days = max(0, min(365, int(p.get('payment_terms_days') or 30))) if str(p.get('payment_terms_days') or '30').isdigit() else 30
    account.is_active = p.get('is_active', '1') == '1'
    account.save()
    return account, None


@hotel_admin_required
@require_POST
def ledger_account_save(request, restaurant_slug, ctx):
    account = _account(ctx, request.POST['pk']) if (request.POST.get('pk') or '').isdigit() else None
    saved, err = _save_account(request, ctx, account)
    if err:
        messages.error(request, err)
        return redirect(_ledger_url(ctx, account.pk if account else None))
    messages.success(request, f'{saved.name} saved.')
    return redirect(_ledger_url(ctx, saved.pk))


@hotel_admin_required
def ledger_account(request, restaurant_slug, ctx, pk):
    a = _account(ctx, pk)
    lines = L.statement(a)
    if request.GET.get('export') == 'csv':
        return _csv(f'statement-{a.pk}.csv', ['Date', 'Reference', 'Details', 'Billed', 'Paid', 'Balance'],
                    [[l['date'], l['ref'], l['text'], l['debit'] or '', l['credit'] or '', l['balance']] for l in lines])
    ag = L.aging(a)
    bal = L.balance(a)
    return _render(request, ctx, 'hotel_pms/_ledger_account.html', 'ledger', a.name, a=a, lines=lines, ag=ag, balance=bal,
                   methods=[m for m in FolioPayment.Method.choices if m[0] not in ('COMPANY', 'OTA')], today=timezone.localdate(),
                   settlements=LedgerPayment.objects.filter(account=a).order_by('-received_on', '-id')[:50],
                   available=(a.credit_limit - bal) if a.credit_limit is not None else None, can_edit=_can(request, 'finance'))


@hotel_admin_required
@require_POST
def ledger_payment_add(request, restaurant_slug, ctx, pk):
    a = _account(ctx, pk)
    p = request.POST
    _, err = L.record_settlement(a, request.user, _money(p.get('amount')) or 0, p.get('method'), p.get('reference'), p.get('note'),
                                 _date(p.get('received_on'), timezone.localdate()))
    _report(request, err, f'Payment from {a.name} recorded.')
    return redirect(_ledger_url(ctx, a.pk))


@hotel_admin_required
@require_POST
def ledger_payment_void_action(request, restaurant_slug, ctx, pk, payment_id):
    a = _account(ctx, pk)
    pay = get_object_or_404(LedgerPayment, pk=payment_id, account=a)
    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        messages.error(request, 'Give a reason for voiding the payment.')
    elif not pay.is_void:
        pay.is_void, pay.void_reason = True, reason[:200]
        pay.save(update_fields=['is_void', 'void_reason'])
        messages.success(request, 'Payment voided.')
    return redirect(_ledger_url(ctx, a.pk))


# ============================================================ group bookings

def _group_url(ctx, pk=None):
    if pk:
        return reverse('hotel_admin:booking_group_detail', kwargs={'restaurant_slug': ctx['rslug'], 'pk': pk})
    return reverse('hotel_admin:booking_groups', kwargs={'restaurant_slug': ctx['rslug']})


@hotel_admin_required
def booking_groups(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    page_obj = paginate(request, BookingGroup.objects.filter(branch=branch).select_related('company_account'))
    rows = [(g, G.summary(g)) for g in page_obj.object_list]
    cats = RoomCategory.objects.filter(branch=branch, is_active=True).order_by('base_price_per_night')
    return _render(request, ctx, 'hotel_pms/_groups.html', 'groups', 'Group bookings', rows=rows, page_obj=page_obj, categories=cats,
                   accounts=CompanyAccount.objects.filter(restaurant=ctx['restaurant'], is_active=True), sources=RoomBooking.Source.choices,
                   today=timezone.localdate(), tomorrow=timezone.localdate() + datetime.timedelta(days=1), can_edit=_can(request, 'front_desk'))


@hotel_admin_required
@require_POST
def booking_group_create(request, restaurant_slug, ctx):
    branch = ctx['current_branch']
    p = request.POST
    ci, co = _date(p.get('check_in_date')), _date(p.get('check_out_date'))
    if not ci or not co:
        messages.error(request, 'Choose arrival and departure dates.')
        return redirect(_group_url(ctx))
    wanted = []
    for cat in RoomCategory.objects.filter(branch=branch, is_active=True):
        try:
            n = int(p.get(f'rooms_{cat.pk}') or 0)
        except ValueError:
            n = 0
        if n > 0:
            wanted.append((cat, n))
    rate = _money(p.get('negotiated_rate')) if (p.get('negotiated_rate') or '').strip() else None
    if rate is not None and rate <= 0:
        messages.error(request, 'The negotiated rate must be more than zero.')
        return redirect(_group_url(ctx))
    account = CompanyAccount.objects.filter(restaurant=ctx['restaurant'], pk=p.get('company_account') or 0).first()
    group, err = G.create_group(branch, request.user, name=p.get('name') or '', check_in=ci, check_out=co, rooms_wanted=wanted,
                                contact_name=p.get('contact_name') or '', contact_phone=p.get('contact_phone') or '',
                                contact_email=p.get('contact_email') or '', company_account=account,
                                source=p.get('source') if p.get('source') in RoomBooking.Source.values else RoomBooking.Source.DIRECT,
                                negotiated_rate=rate, notes=p.get('notes') or '', override_restrictions=p.get('override') == '1')
    if err:
        messages.error(request, err)
        return redirect(_group_url(ctx))
    messages.success(request, f'{group.name}: {group.bookings.count()} rooms blocked. Add guest names in the rooming list.')
    return redirect(_group_url(ctx, group.pk))


@hotel_admin_required
def booking_group_detail(request, restaurant_slug, ctx, pk):
    g = get_object_or_404(BookingGroup.objects.select_related('company_account'), pk=pk, branch=ctx['current_branch'])
    return _render(request, ctx, 'hotel_pms/_group_detail.html', 'groups', g.name, g=g, s=G.summary(g),
                   methods=[m for m in FolioPayment.Method.choices if m[0] != 'COMPANY'], today=timezone.localdate(),
                   can_edit=_can(request, 'front_desk'))


@hotel_admin_required
@require_POST
def booking_group_update(request, restaurant_slug, ctx, pk):
    g = get_object_or_404(BookingGroup, pk=pk, branch=ctx['current_branch'])
    p = request.POST
    op = p.get('op')
    if op == 'rooming':
        changed = 0
        for b in G.members(g):
            name = (p.get(f'name_{b.pk}') or '').strip()
            phone = (p.get(f'phone_{b.pk}') or '').strip()
            adults = p.get(f'adults_{b.pk}')
            fields = []
            if name and name != b.guest_name:
                b.guest_name = name[:150]
                fields.append('guest_name')
            if phone != b.guest_phone and phone:
                b.guest_phone = phone[:30]
                fields.append('guest_phone')
            if adults and adults.isdigit() and int(adults) != b.adults:
                b.adults = b.num_guests = max(1, min(10, int(adults)))
                fields += ['adults', 'num_guests']
            if fields:
                b.save(update_fields=fields + ['updated_at'])
                changed += 1
        messages.success(request, f'Rooming list saved ({changed} room{"s" if changed != 1 else ""} updated).')
    elif op == 'checkin':
        done, problems = G.check_in_all(g, request.user)
        if done:
            messages.success(request, f'{done} room{"s" if done != 1 else ""} checked in.')
        for pr in problems[:5]:
            messages.error(request, pr)
    elif op == 'checkout':
        done, problems = G.check_out_all(g, request.user, bill_company=p.get('bill_company') == '1')
        if done:
            messages.success(request, f'{done} room{"s" if done != 1 else ""} checked out.')
        for pr in problems[:5]:
            messages.error(request, pr)
    elif op == 'bill_company':
        done, problems = G.bill_company_all(g, request.user)
        if done:
            messages.success(request, f'{done} folio{"s" if done != 1 else ""} billed to {g.company_account.name}.')
        for pr in problems[:5]:
            messages.error(request, pr)
    elif op == 'pay':
        applied, err = G.pay(g, request.user, _money(p.get('amount')) or 0, p.get('method'), p.get('reference') or '')
        _report(request, err, f'₹{applied:,.2f} applied across the group’s folios.')
    elif op == 'cancel':
        n = G.cancel_all(g, request.user, (p.get('reason') or '').strip())
        messages.info(request, f'{n} reserved room{"s" if n != 1 else ""} cancelled.')
    return redirect(_group_url(ctx, g.pk))
