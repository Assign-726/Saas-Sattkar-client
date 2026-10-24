"""Hotel admin screens for guest services, banquets, sales, cashier, back office, the action
centre and the occupancy forecast. Business rules live in ``hotel.ops``."""
import calendar
import datetime
from decimal import Decimal
import uuid

from django.core.files.storage import default_storage
from django.db.models import Count, Prefetch, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from hotel import ops
from hotel import pricing
from hotel.models import (BanquetEvent, BanquetMenu, BanquetPayment, BanquetVenue, CashierShift, ComplianceLicence, FixedAsset,
                          FolioPayment, GuestRequest, HotelSettings, LaundryOrder, LaundryRate, LostFoundItem, PaidOut, PromoCode,
                          RateOverride, Room, RoomBooking, RoomCategory, RoomPackage, SafetyCheck, SalesActivity, SalesLead,
                          SecurityIncident, ServiceContract, SpaAppointment, SpaTherapist, SpaTreatment, SpaPackage, StatutoryFiling,
                          UtilityReading, Visitor)
from hotel.views_admin import hotel_admin_required, optimize_image_to_webp
from hotel.views_pms import _back, _base, _can, _date, _money, _render, _report
from super_admin import permissions as P

D0 = Decimal('0')


def _url(ctx, name, **kw):
    return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': ctx['rslug'], **kw})


def _branch(ctx):
    b = ctx['current_branch']
    if b is None:
        raise Http404('No hotel branch selected')
    return b


def _in_house(branch):
    return (RoomBooking.objects.filter(branch=branch, booking_status__in=ops.OPEN_STAY)
            .select_related('room').order_by('room__room_number'))


def _active_booking(branch, value):
    if not value:
        return None
    return RoomBooking.objects.filter(branch=branch, pk=value, booking_status__in=ops.OPEN_STAY).select_related('room').first()


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _time(value):
    try:
        return datetime.time.fromisoformat((value or '').strip()[:5])
    except ValueError:
        return None


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


# ============================================================ action centre

@hotel_admin_required
def action_centre(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    items = ops.action_items(branch, P.access_map(request.user))
    stays = RoomBooking.objects.filter(branch=branch)
    snapshot = {
        'arrivals': stays.filter(check_in_date=today, booking_status='RESERVED').count(),
        'departures': stays.filter(check_out_date=today, booking_status='CHECKED_IN').count(),
        'in_house': stays.filter(booking_status='CHECKED_IN').count(),
        'requests': GuestRequest.objects.filter(branch=branch).exclude(status='RESOLVED').count(),
        'events': BanquetEvent.objects.filter(branch=branch, date=today, status__in=BanquetEvent.HOLDS_VENUE).count(),
        'spa': SpaAppointment.objects.filter(branch=branch, date=today).exclude(status__in=['CANCELLED', 'NO_SHOW']).count(),
    }
    events = BanquetEvent.objects.filter(branch=branch, date=today, status__in=BanquetEvent.HOLDS_VENUE).select_related('venue')
    spa = SpaAppointment.objects.filter(branch=branch, date=today, status__in=SpaAppointment.LIVE).select_related('treatment', 'therapist')
    return _render(request, ctx, 'hotel_ops/_action_centre.html', 'action_centre', 'Action centre',
                   items=items, snapshot=snapshot, events=events, spa=spa, today=today)


# ============================================================ guest requests

@hotel_admin_required
def requests_view(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    tab = request.GET.get('tab', 'open')
    qs = GuestRequest.objects.filter(branch=branch).select_related('room', 'booking', 'allowance', 'created_by', 'resolved_by')
    if tab == 'open':
        qs = qs.exclude(status='RESOLVED')
    elif tab == 'complaints':
        qs = qs.filter(kind='COMPLAINT')
    elif tab == 'resolved':
        qs = qs.filter(status='RESOLVED')
    rows = list(qs[:300])
    if tab == 'open':
        rows.sort(key=lambda r: (not r.is_breached, r.priority != 'HIGH', r.created_at))
    open_all = list(GuestRequest.objects.filter(branch=branch).exclude(status='RESOLVED'))
    month = timezone.now() - datetime.timedelta(days=30)
    done = GuestRequest.objects.filter(branch=branch, status='RESOLVED', resolved_at__gte=month).exclude(kind='COMPLIMENT')
    mins = [r.age_minutes for r in done]
    kpis = {
        'open': len(open_all), 'breached': sum(1 for r in open_all if r.is_breached),
        'complaints': GuestRequest.objects.filter(branch=branch, kind='COMPLAINT', created_at__gte=month).count(),
        'avg': round(sum(mins) / len(mins)) if mins else None,
        'on_time': _pct(sum(1 for r in done if not r.is_breached), len(mins)),
    }
    by_dept = (GuestRequest.objects.filter(branch=branch, created_at__gte=month).values('department')
               .annotate(n=Count('id')).order_by('-n'))
    labels = dict(GuestRequest.Department.choices)
    return _render(request, ctx, 'hotel_ops/_requests.html', 'requests', 'Guest requests',
                   rows=rows, tab=tab, kpis=kpis, in_house=_in_house(branch), kinds=GuestRequest.Kind.choices,
                   departments=GuestRequest.Department.choices, sla=GuestRequest.SLA_MINUTES,
                   by_dept=[{'label': labels.get(d['department'], d['department']), 'n': d['n']} for d in by_dept],
                   rooms=Room.objects.filter(branch=branch, is_active=True).order_by('room_number'),
                   can_edit=_can(request, 'guest_services'))


@hotel_admin_required
@require_POST
def request_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    booking = _active_booking(branch, request.POST.get('booking'))
    room = Room.objects.filter(branch=branch, pk=request.POST.get('room') or 0).first() if not booking else None
    _, err = ops.log_request(branch, request.user, description=request.POST.get('description'), kind=request.POST.get('kind'),
                             department=request.POST.get('department'), priority=request.POST.get('priority'),
                             booking=booking, room=room, guest_name=request.POST.get('guest_name', ''))
    _report(request, err, 'Ticket logged and sent to the department.')
    return _back(request, _url(ctx, 'requests'))


@hotel_admin_required
@require_POST
def request_action(request, restaurant_slug, ctx, pk):
    req = get_object_or_404(GuestRequest, pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    if op == 'ack':
        err, ok = ops.acknowledge_request(req, request.user), 'Ticket acknowledged.'
    elif op == 'resolve':
        err = ops.resolve_request(req, request.user, request.POST.get('resolution'), _money(request.POST.get('allowance') or 0) or 0)
        ok = 'Ticket resolved.'
    else:
        err, ok = 'Unknown action.', None
    _report(request, err, ok)
    return _back(request, _url(ctx, 'requests'))


# ============================================================ lost & found

@hotel_admin_required
def lostfound(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    tab = request.GET.get('tab', 'stored')
    qs = LostFoundItem.objects.filter(branch=branch).select_related('booking')
    qs = qs.filter(status='STORED') if tab == 'stored' else qs.exclude(status='STORED')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(item__icontains=q) | Q(location__icontains=q) | Q(reference__icontains=q) | Q(description__icontains=q))
    today = timezone.localdate()
    stored = LostFoundItem.objects.filter(branch=branch, status='STORED')
    kpis = {'stored': stored.count(), 'valuable': stored.filter(is_valuable=True).count(),
            'past_hold': stored.filter(found_on__lt=today - datetime.timedelta(days=LostFoundItem.HOLD_DAYS)).count(),
            'returned': LostFoundItem.objects.filter(branch=branch, status='RETURNED', returned_on__gte=today - datetime.timedelta(days=30)).count()}
    recent = RoomBooking.objects.filter(branch=branch, check_out_date__gte=today - datetime.timedelta(days=14),
                                        check_in_date__lte=today).select_related('room').order_by('room__room_number')
    return _render(request, ctx, 'hotel_ops/_lostfound.html', 'lostfound', 'Lost & found', rows=qs[:300], tab=tab, q=q, kpis=kpis,
                   today=today, recent=recent, can_edit=_can(request, 'guest_services'))


@hotel_admin_required
@require_POST
def lostfound_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    item, location = (request.POST.get('item') or '').strip(), (request.POST.get('location') or '').strip()
    if not item or not location:
        _report(request, 'Enter the item and where it was found.')
    else:
        booking = RoomBooking.objects.filter(branch=branch, pk=request.POST.get('booking') or 0).first()
        LostFoundItem.objects.create(
            branch=branch, item=item[:150], location=location[:120], booking=booking,
            found_on=_date(request.POST.get('found_on'), timezone.localdate()),
            description=(request.POST.get('description') or '')[:255], is_valuable=bool(request.POST.get('is_valuable')),
            found_by=(request.POST.get('found_by') or '')[:120], storage_location=(request.POST.get('storage_location') or '')[:120],
            created_by=request.user)
        _report(request, None, 'Item logged.')
    return _back(request, _url(ctx, 'lostfound'))


@hotel_admin_required
@require_POST
def lostfound_action(request, restaurant_slug, ctx, pk):
    item = get_object_or_404(LostFoundItem, pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    if item.status != 'STORED':
        _report(request, 'This item is already closed.')
    elif op == 'return':
        who = (request.POST.get('returned_to') or '').strip()
        if not who:
            _report(request, 'Enter who collected it (name and ID or courier reference).')
        else:
            item.status, item.returned_to, item.returned_on = 'RETURNED', who[:150], timezone.localdate()
            item.closing_note = (request.POST.get('note') or '')[:255]
            item.save()
            _report(request, None, f'{item.item} marked as returned.')
    elif op == 'dispose':
        item.status, item.returned_on, item.closing_note = 'DISPOSED', timezone.localdate(), (request.POST.get('note') or 'Disposed')[:255]
        item.save()
        _report(request, None, f'{item.item} marked as donated / disposed.')
    return _back(request, _url(ctx, 'lostfound'))


# ============================================================ laundry

@hotel_admin_required
def laundry(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    tab = request.GET.get('tab', 'orders')
    rates = list(LaundryRate.objects.filter(branch=branch))
    orders = (LaundryOrder.objects.filter(branch=branch).select_related('booking', 'booking__room')
              .prefetch_related('lines'))
    show = request.GET.get('show', 'active')
    if show == 'active':
        orders = orders.exclude(status__in=['DELIVERED', 'CANCELLED'])
    orders = list(orders[:200])
    today = timezone.localdate()
    delivered_today = LaundryOrder.objects.filter(branch=branch, status='DELIVERED', delivered_at__date=today).prefetch_related('lines')
    kpis = {'active': LaundryOrder.objects.filter(branch=branch).exclude(status__in=['DELIVERED', 'CANCELLED']).count(),
            'ready': LaundryOrder.objects.filter(branch=branch, status='READY').count(),
            'express': LaundryOrder.objects.filter(branch=branch, is_express=True).exclude(status__in=['DELIVERED', 'CANCELLED']).count(),
            'revenue': sum((o.amount for o in delivered_today), D0)}
    return _render(request, ctx, 'hotel_ops/_laundry.html', 'laundry', 'Laundry & valet', tab=tab, show=show, rates=rates,
                   active_rates=[r for r in rates if r.is_active], orders=orders, kpis=kpis, in_house=_in_house(branch),
                   statuses=dict(LaundryOrder.Status.choices), can_edit=_can(request, 'guest_services'))


@hotel_admin_required
@require_POST
def laundry_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    booking = _active_booking(branch, request.POST.get('booking'))
    quantities = {}
    for key, value in request.POST.items():
        if key.startswith('q_'):
            try:
                _, rate_id, service = key.split('_', 2)
                quantities[(int(rate_id), service)] = max(0, min(99, int(value or 0)))
            except ValueError:
                continue
    order, err = ops.create_laundry(branch, request.user, booking, quantities, bool(request.POST.get('express')),
                                    request.POST.get('notes'))
    _report(request, err, order and f'Laundry order {order.reference} created · ₹{order.amount:,.2f}.')
    return _back(request, _url(ctx, 'laundry'))


@hotel_admin_required
@require_POST
def laundry_action(request, restaurant_slug, ctx, pk):
    order = get_object_or_404(LaundryOrder.objects.select_related('booking'), pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    if op == 'advance':
        err = ops.advance_laundry(order, request.user)
        ok = 'Delivered and charged to the room.' if order.status == 'DELIVERED' else f'Marked {order.get_status_display().lower()}.'
    elif op == 'cancel' and order.status not in ('DELIVERED', 'CANCELLED'):
        order.status = 'CANCELLED'
        order.save(update_fields=['status'])
        err, ok = None, 'Order cancelled.'
    else:
        err, ok = 'This order can’t be changed.', None
    _report(request, err, ok)
    return _back(request, _url(ctx, 'laundry'))


@hotel_admin_required
@require_POST
def laundry_rate_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if request.POST.get('op') == 'seed':
        ops.seed_laundry_rates(branch)
        _report(request, None, 'Standard price list added. Adjust the prices to yours.')
        return redirect(_url(ctx, 'laundry') + '?tab=prices')
    rate = LaundryRate.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first()
    item = (request.POST.get('item') or '').strip()
    if not item and not rate:
        _report(request, 'Enter the item name.')
        return redirect(_url(ctx, 'laundry') + '?tab=prices')
    rate = rate or LaundryRate(branch=branch)
    if item:
        rate.item = item[:80]
    for f in ('wash_price', 'dry_clean_price', 'press_price'):
        v = _money(request.POST.get(f)) if (request.POST.get(f) or '').strip() else None
        setattr(rate, f, v if v and v > 0 else None)
    rate.is_active = request.POST.get('is_active', '1') == '1'
    if LaundryRate.objects.filter(branch=branch, item=rate.item).exclude(pk=rate.pk).exists():
        _report(request, f'{rate.item} is already on the price list.')
    else:
        rate.save()
        _report(request, None, 'Price list updated.')
    return redirect(_url(ctx, 'laundry') + '?tab=prices')


# ============================================================ spa

@hotel_admin_required
def spa(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if branch:
        from hotel.views import ensure_rich_spa_catalog
        ensure_rich_spa_catalog(branch)
    today = timezone.localdate()
    day = _date(request.GET.get('date'), today)
    therapists = list(SpaTherapist.objects.filter(branch=branch, is_active=True))
    treatments = list(SpaTreatment.objects.filter(branch=branch, is_active=True))
    appts = list(SpaAppointment.objects.filter(branch=branch, date=day).select_related('treatment', 'therapist', 'booking', 'booking__room'))
    live = [a for a in appts if a.status not in ('CANCELLED', 'NO_SHOW')]
    booked_min = sum(a.duration_minutes for a in live)
    capacity = len(therapists) * ((SpaAppointment.CLOSE_TIME.hour - SpaAppointment.OPEN_TIME.hour) * 60)
    done = [a for a in appts if a.status == 'COMPLETED' and a.settled_by != 'COMPLIMENTARY']
    kpis = {'count': len(live), 'util': _pct(booked_min, capacity), 'completed': sum(1 for a in appts if a.status == 'COMPLETED'),
            'revenue': sum((a.price for a in done), D0)}
    return _render(request, ctx, 'hotel_ops/_spa.html', 'spa', 'Spa & wellness', day=day, today=today,
                   days=[today + datetime.timedelta(days=i) for i in range(7)], therapists=therapists, treatments=treatments,
                   grid=ops.spa_grid(appts, therapists) if therapists else [], appts=appts, kpis=kpis, in_house=_in_house(branch),
                   settlements=SpaAppointment.Settlement.choices, can_edit=_can(request, 'guest_services'),
                   prev_day=day - datetime.timedelta(days=1), next_day=day + datetime.timedelta(days=1))


@hotel_admin_required
@require_POST
def spa_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    treatment = SpaTreatment.objects.filter(branch=branch, pk=request.POST.get('treatment') or 0, is_active=True).first()
    therapist_id = request.POST.get('therapist')
    therapist = None
    if therapist_id and therapist_id != 'auto':
        therapist = SpaTherapist.objects.filter(branch=branch, pk=therapist_id, is_active=True).first()

    day, start = _date(request.POST.get('date')), _time(request.POST.get('start'))
    if not (treatment and day and start):
        _report(request, 'Choose the treatment, date and time.')
    else:
        # If therapist was not selected or set to 'auto', resolve the best qualified therapist
        if not therapist:
            cat_key = (treatment.category or '').strip()
            active_therapists = list(SpaTherapist.objects.filter(branch=branch, is_active=True))
            qualified = [th for th in active_therapists if th.can_perform_category(cat_key)]
            pool = qualified if qualified else active_therapists
            for th in pool:
                if not ops.spa_clash(branch, th, day, start, treatment.duration_minutes):
                    therapist = th
                    break
            if not therapist and pool:
                therapist = pool[0]

        if not therapist:
            _report(request, f'No specialist is assigned to the "{treatment.get_category_display() if hasattr(treatment, "get_category_display") else treatment.category}" category. Please configure specialists in Spa Setup.')
        else:
            appt, err = ops.book_spa(branch, request.user, treatment=treatment, therapist=therapist, date=day, start=start,
                                     guest_name=request.POST.get('guest_name', ''), guest_phone=request.POST.get('guest_phone', ''),
                                     booking=_active_booking(branch, request.POST.get('booking')), notes=request.POST.get('notes', ''))
            _report(request, err, appt and f'Booked {treatment.name} with {therapist.name} at {start:%H:%M}.')
    return redirect(_url(ctx, 'spa') + f'?date={(day or timezone.localdate()).isoformat()}')


@hotel_admin_required
@require_POST
def spa_action(request, restaurant_slug, ctx, pk):
    appt = get_object_or_404(SpaAppointment.objects.select_related('booking', 'treatment'), pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    if op == 'complete':
        err, ok = ops.complete_spa(appt, request.user, request.POST.get('settled_by')), 'Treatment completed and settled.'
    elif op == 'delete':
        if appt.charge:
            err, ok = 'Cannot delete appointment with posted folio charge. Void the charge first.', None
        else:
            appt.delete()
            err, ok = None, 'Appointment deleted successfully.'
    else:
        status = {'start': 'IN_TREATMENT', 'cancel': 'CANCELLED', 'no_show': 'NO_SHOW'}.get(op)
        err, ok = (ops.spa_status(appt, status), 'Appointment updated.') if status else ('Unknown action.', None)
    _report(request, err, ok)
    return redirect(_url(ctx, 'spa') + f'?date={appt.date.isoformat()}')


@hotel_admin_required
def spa_setup(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if branch:
        from hotel.views import ensure_rich_spa_catalog
        ensure_rich_spa_catalog(branch)
    active_tab = request.GET.get('tab', 'treatments')
    spa_categories = [
        ('massages', 'Massages & Bodywork'),
        ('ayurveda', 'Ancient Ayurveda'),
        ('facials', 'Aesthetic Facials'),
        ('steam-hydro', 'Steam & Hydrotherapy'),
        ('couples', 'Couples Sanctuary'),
    ]
    return _render(request, ctx, 'hotel_ops/_spa_setup.html', 'spa', 'Spa set-up',
                   therapists=SpaTherapist.objects.filter(branch=branch).order_by('name'),
                   treatments=SpaTreatment.objects.filter(branch=branch).order_by('category', 'name'),
                   packages=SpaPackage.objects.filter(branch=branch).order_by('title'),
                   spa_categories=spa_categories,
                   active_tab=active_tab,
                   can_edit=_can(request, 'guest_services'))


@hotel_admin_required
@require_POST
def spa_setup_save(request, restaurant_slug, ctx):
    from django.utils.text import slugify
    branch = _branch(ctx)
    kind = request.POST.get('kind')
    name = (request.POST.get('name') or '').strip()
    active = request.POST.get('is_active', '1') == '1'
    tab = 'treatments'

    if kind == 'therapist':
        tab = 'therapists'
        obj = SpaTherapist.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or SpaTherapist(branch=branch)
        if not name and not obj.pk:
            _report(request, 'Enter the therapist’s name.')
        else:
            obj.name = name[:150] or obj.name
            obj.title = (request.POST.get('title') or obj.title or 'Certified Wellness Specialist')[:150]
            obj.experience_years = _int(request.POST.get('experience_years'), obj.experience_years or 5)

            # Photo upload from device or URL or removal (Max 2MB)
            if 'photo_file' in request.FILES and request.FILES['photo_file']:
                p_file = request.FILES['photo_file']
                if p_file.size > 2 * 1024 * 1024:
                    _report(request, f"Photo '{p_file.name}' exceeds the 2MB limit ({round(p_file.size / (1024*1024), 2)}MB). Max allowed size is 2MB.")
                else:
                    try:
                        converted = optimize_image_to_webp(p_file)
                        save_name = default_storage.save(f"hotel/spa/therapists/th_{uuid.uuid4().hex[:8]}.webp", converted)
                        obj.photo = default_storage.url(save_name)
                    except Exception as e:
                        _report(request, f"Photo upload failed: {e}")
            elif request.POST.get('remove_photo') in ('1', 'true', 'on'):
                obj.photo = ''
            elif request.POST.get('photo'):
                obj.photo = request.POST.get('photo').strip()

            rating_val = _money(request.POST.get('rating'))
            if rating_val is not None and 1 <= rating_val <= 5:
                obj.rating = rating_val
            avail = request.POST.get('availability')
            if avail:
                obj.availability = avail.strip()
            langs = request.POST.get('languages')
            if langs is not None:
                obj.languages = langs.strip()
            obj.skills = (request.POST.get('skills') or '')[:255]
            bio = request.POST.get('bio')
            if bio is not None:
                obj.bio = bio.strip()
            cats = request.POST.getlist('categories')
            if not cats and request.POST.get('categories'):
                cats = [c.strip() for c in request.POST.get('categories').replace('\n', ',').split(',') if c.strip()]
            obj.categories = cats
            specs = request.POST.get('specialties')
            if specs is not None:
                obj.specialties = [s.strip() for s in specs.replace('\n', ',').split(',') if s.strip()]
            certs = request.POST.get('certifications')
            if certs is not None:
                obj.certifications = [s.strip() for s in certs.replace('\n', ',').split(',') if s.strip()]
            obj.is_active = active
            obj.save()
            _report(request, None, f'Specialist "{obj.name}" saved successfully.')

    elif kind == 'treatment':
        tab = 'treatments'
        obj = SpaTreatment.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or SpaTreatment(branch=branch)
        price, dur = _money(request.POST.get('price')), _int(request.POST.get('duration_minutes'), 60)
        gst = _money(request.POST.get('gst_percent') or '18')
        if not (name or obj.pk) or price is None or price <= 0 or not (15 <= dur <= 240) or gst is None or not (0 <= gst <= 28):
            _report(request, 'Enter a valid name, price above zero, duration of 15–240 minutes and GST of 0–28%.')
        else:
            obj.name = name[:150] or obj.name
            if not obj.slug and name:
                obj.slug = slugify(name)[:150]
            obj.category = (request.POST.get('category') or obj.category or 'massages')[:60]
            obj.tag = (request.POST.get('tag') or obj.tag or '')[:100]
            obj.price = price
            orig_price = _money(request.POST.get('original_price'))
            if orig_price is not None:
                obj.original_price = orig_price
            obj.duration_minutes = dur
            obj.gst_percent = gst
            rating_val = _money(request.POST.get('rating'))
            if rating_val is not None and 1 <= rating_val <= 5:
                obj.rating = rating_val
            suite = request.POST.get('suite_type')
            if suite:
                obj.suite_type = suite.strip()[:100]

            # Primary Cover Image: device upload (Max 2MB) or URL or removal
            if 'cover_image_file' in request.FILES and request.FILES['cover_image_file']:
                c_file = request.FILES['cover_image_file']
                if c_file.size > 2 * 1024 * 1024:
                    _report(request, f"Cover image '{c_file.name}' exceeds the 2MB limit ({round(c_file.size / (1024*1024), 2)}MB). Max allowed size is 2MB.")
                else:
                    try:
                        converted = optimize_image_to_webp(c_file)
                        save_name = default_storage.save(f"hotel/spa/treatments/cover_{uuid.uuid4().hex[:8]}.webp", converted)
                        obj.cover_image = default_storage.url(save_name)
                    except Exception as e:
                        _report(request, f"Cover image upload failed: {e}")
            elif request.POST.get('remove_cover_image') in ('1', 'true', 'on'):
                obj.cover_image = ''
            elif request.POST.get('cover_image'):
                obj.cover_image = request.POST.get('cover_image').strip()

            # Gallery Management with STRICT Limit of Max 5 Images per service
            MAX_IMAGES_PER_SERVICE = 5
            delete_gallery = set(request.POST.getlist('delete_gallery_images'))

            # Start with retained gallery images
            raw_kept = request.POST.getlist('existing_gallery_images')
            if raw_kept or request.POST.get('has_gallery_field') == '1':
                current_gallery = [img.strip() for img in raw_kept if img.strip() and img.strip() not in delete_gallery]
            else:
                current_gallery = [img for img in (obj.gallery_images or []) if img not in delete_gallery]

            # External URLs if provided
            extra_urls = request.POST.get('gallery_urls', '').strip()
            if extra_urls:
                for u in extra_urls.replace(',', '\n').splitlines():
                    u_clean = u.strip()
                    if u_clean and u_clean not in current_gallery:
                        current_gallery.append(u_clean)

            # Available slots for new device uploads:
            # Service capacity is strictly MAX_IMAGES_PER_SERVICE (5) total
            has_cover = 1 if bool(obj.cover_image) else 0
            current_count = has_cover + len(current_gallery)
            slots_available = max(0, MAX_IMAGES_PER_SERVICE - current_count)

            # Process device gallery file uploads (Max 2MB per file)
            uploaded_files = request.FILES.getlist('gallery_image_files')
            if uploaded_files:
                accepted_files = uploaded_files[:slots_available]
                skipped_files = len(uploaded_files) - len(accepted_files)
                for g_file in accepted_files:
                    if g_file.size > 2 * 1024 * 1024:
                        _report(request, f"Gallery file '{g_file.name}' exceeds the 2MB limit ({round(g_file.size / (1024*1024), 2)}MB). Skipped.")
                        continue
                    try:
                        converted = optimize_image_to_webp(g_file)
                        save_name = default_storage.save(f"hotel/spa/treatments/gal_{uuid.uuid4().hex[:8]}.webp", converted)
                        current_gallery.append(default_storage.url(save_name))
                    except Exception as e:
                        _report(request, f"Gallery file '{g_file.name}' upload failed: {e}")
                if skipped_files > 0:
                    _report(request, f"Limit reached: strictly 5 images maximum per service. {skipped_files} additional image(s) were skipped.")

            # Hard cap: ensure (has_cover + gallery) <= 5
            max_gallery_allowed = MAX_IMAGES_PER_SERVICE - (1 if bool(obj.cover_image) else 0)
            if len(current_gallery) > max_gallery_allowed:
                current_gallery = current_gallery[:max_gallery_allowed]
            obj.gallery_images = current_gallery

            short_d = request.POST.get('short_description')
            if short_d is not None:
                obj.short_description = short_d.strip()
            full_d = request.POST.get('full_description')
            if full_d is not None:
                obj.full_description = full_d.strip()
            steps = request.POST.get('ritual_steps')
            if steps is not None:
                obj.ritual_steps = [s.strip() for s in steps.splitlines() if s.strip()]
            bens = request.POST.get('benefits')
            if bens is not None:
                obj.benefits = [s.strip() for s in bens.splitlines() if s.strip()]
            amenities = request.POST.get('included_amenities')
            if amenities is not None:
                obj.included_amenities = [s.strip() for s in amenities.splitlines() if s.strip()]
            obj.is_featured = request.POST.get('is_featured') in ('1', 'true', 'on')
            obj.is_active = active
            obj.save()
            _report(request, None, f'Treatment "{obj.name}" saved successfully.')

    elif kind == 'package':
        tab = 'packages'
        title = (request.POST.get('title') or name).strip()
        obj = SpaPackage.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or SpaPackage(branch=branch)
        price = _money(request.POST.get('price'))
        dur = (request.POST.get('duration') or '150 Mins').strip()
        dur_mins = _int(request.POST.get('duration_minutes'), 150)
        if not (title or obj.pk) or price is None or price <= 0:
            _report(request, 'Enter a valid package title and price above zero.')
        else:
            obj.title = title[:180] or obj.title
            if not obj.slug and title:
                obj.slug = slugify(title)[:180]
            obj.tag = (request.POST.get('tag') or obj.tag or 'Signature Ritual')[:100]
            obj.duration = dur
            obj.duration_minutes = dur_mins
            obj.price = price
            orig_price = _money(request.POST.get('original_price'))
            if orig_price is not None:
                obj.original_price = orig_price

            # Banner image upload (Max 2MB) or URL or removal
            if 'image_file' in request.FILES and request.FILES['image_file']:
                pkg_file = request.FILES['image_file']
                if pkg_file.size > 2 * 1024 * 1024:
                    _report(request, f"Package image '{pkg_file.name}' exceeds the 2MB limit ({round(pkg_file.size / (1024*1024), 2)}MB). Max allowed size is 2MB.")
                else:
                    try:
                        converted = optimize_image_to_webp(pkg_file)
                        save_name = default_storage.save(f"hotel/spa/packages/pkg_{uuid.uuid4().hex[:8]}.webp", converted)
                        obj.image = default_storage.url(save_name)
                    except Exception as e:
                        _report(request, f"Package image upload failed: {e}")
            elif request.POST.get('remove_image') in ('1', 'true', 'on'):
                obj.image = ''
            elif request.POST.get('image'):
                obj.image = request.POST.get('image').strip()

            desc = request.POST.get('description')
            if desc is not None:
                obj.description = desc.strip()
            inclusions = request.POST.get('inclusions')
            if inclusions is not None:
                obj.inclusions = [s.strip() for s in inclusions.splitlines() if s.strip()]
            obj.popular = request.POST.get('popular') in ('1', 'true', 'on')
            obj.is_active = active
            obj.save()
            _report(request, None, f'Signature package "{obj.title}" saved successfully.')

    return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")


@hotel_admin_required
@require_POST
def spa_setup_delete(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    kind = request.POST.get('kind')
    pk = _int(request.POST.get('pk'))
    tab = 'treatments'

    if kind == 'treatment':
        tab = 'treatments'
        item = SpaTreatment.objects.filter(pk=pk, branch=branch).first()
        if not item:
            _report(request, 'Treatment not found or already deleted.')
            return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")

        name = item.name
        appts = SpaAppointment.objects.filter(treatment=item)
        if appts.filter(charge__isnull=False).exists():
            _report(request, f'Cannot delete treatment "{name}" because associated appointments have billed charges on guest folios. Set it to Inactive instead.')
            return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")

        other_treatment = SpaTreatment.objects.filter(branch=branch, is_active=True).exclude(pk=item.pk).first()
        if appts.exists():
            if other_treatment:
                count = appts.count()
                appts.update(treatment=other_treatment)
                item.delete()
                _report(request, None, f'Treatment "{name}" deleted successfully. {count} scheduled booking(s) reassigned to "{other_treatment.name}".')
            else:
                count = appts.count()
                appts.delete()
                item.delete()
                _report(request, None, f'Treatment "{name}" and {count} test appointment(s) deleted successfully.')
        else:
            item.delete()
            _report(request, None, f'Treatment "{name}" deleted successfully.')

    elif kind == 'therapist':
        tab = 'therapists'
        item = SpaTherapist.objects.filter(pk=pk, branch=branch).first()
        if not item:
            _report(request, 'Specialist not found or already deleted.')
            return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")

        name = item.name
        appts = SpaAppointment.objects.filter(therapist=item)
        if appts.filter(charge__isnull=False).exists():
            _report(request, f'Cannot delete specialist "{name}" because their appointments have billed charges on guest folios. Set them to Inactive instead.')
            return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")

        other_therapist = SpaTherapist.objects.filter(branch=branch, is_active=True).exclude(pk=item.pk).first()
        if appts.exists():
            if other_therapist:
                count = appts.count()
                appts.update(therapist=other_therapist)
                item.delete()
                _report(request, None, f'Specialist "{name}" deleted successfully. {count} scheduled appointment(s) reassigned to {other_therapist.name}.')
            else:
                count = appts.count()
                appts.delete()
                item.delete()
                _report(request, None, f'Specialist "{name}" and {count} test appointment(s) deleted successfully.')
        else:
            item.delete()
            _report(request, None, f'Specialist "{name}" deleted successfully.')

    elif kind == 'package':
        tab = 'packages'
        item = SpaPackage.objects.filter(pk=pk, branch=branch).first()
        if not item:
            _report(request, 'Signature package not found or already deleted.')
            return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")

        name = item.title
        item.delete()
        _report(request, None, f'Signature package "{name}" deleted successfully.')

    return redirect(f"{_url(ctx, 'spa_setup')}?tab={tab}")


@hotel_admin_required
@require_POST
def spa_setup_reset_catalog(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    from hotel.views import ensure_rich_spa_catalog
    ensure_rich_spa_catalog(branch, force=True)
    _report(request, None, 'Demo spa catalog defaults successfully restored.')
    return redirect(_url(ctx, 'spa_setup'))


# ============================================================ banquets

def _event_from_post(event, branch, post):
    venue = BanquetVenue.objects.filter(branch=branch, pk=post.get('venue') or 0).first()
    if venue is None:
        return 'Choose a venue.'
    day = _date(post.get('date'))
    if day is None:
        return 'Choose the event date.'
    name, client = (post.get('name') or '').strip(), (post.get('client_name') or '').strip()
    if not name or not client:
        return 'Enter the event name and the client.'
    event.venue, event.date, event.name, event.client_name = venue, day, name[:150], client[:150]
    event.event_type = post.get('event_type') if post.get('event_type') in BanquetEvent.TYPES else 'Other'
    event.session = post.get('session') if post.get('session') in BanquetEvent.Session.values else 'DINNER'
    event.layout = post.get('layout') if post.get('layout') in BanquetEvent.LAYOUTS else 'Round tables'
    event.pax = max(0, _int(post.get('pax'), 0))
    event.menu = BanquetMenu.objects.filter(branch=branch, pk=post.get('menu') or 0).first()
    for f in ('decor', 'av', 'other_charges'):
        v = _money(post.get(f) or '0')
        if v is None or v < 0:
            return 'Charges must be numbers of zero or more.'
        setattr(event, f, v)
    vc = (post.get('venue_charge') or '').strip()
    event.venue_charge = _money(vc) if vc else None
    if event.venue_charge is not None and event.venue_charge < 0:
        return 'Venue charge must be zero or more.'
    gst = _money(post.get('gst_percent') or '18')
    if gst is None or not (0 <= gst <= 28):
        return 'GST must be between 0% and 28%.'
    event.gst_percent = gst
    for f in ('contact_phone', 'contact_email', 'client_gstin', 'kitchen_notes', 'housekeeping_notes', 'engineering_notes', 'notes'):
        setattr(event, f, (post.get(f) or '').strip())
    event.contact_phone, event.client_gstin = event.contact_phone[:30], event.client_gstin.upper()[:20]
    status = post.get('status')
    if status in ('ENQUIRY', 'TENTATIVE', 'CONFIRMED') and event.status not in ('COMPLETED', 'CANCELLED'):
        event.status = status
    return ops.check_event(event)


@hotel_admin_required
def banquets(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    tab = request.GET.get('tab', 'events')
    venues = list(BanquetVenue.objects.filter(branch=branch))
    events = BanquetEvent.objects.filter(branch=branch).select_related('venue', 'menu').prefetch_related('payments')
    status = request.GET.get('status', '')
    when = request.GET.get('when', 'upcoming')
    if status in BanquetEvent.Status.values:
        events = events.filter(status=status)
    if when == 'upcoming':
        events = events.filter(date__gte=today)
    elif when == 'past':
        events = events.filter(date__lt=today).order_by('-date')
    upcoming = [e for e in BanquetEvent.objects.filter(branch=branch, date__gte=today).exclude(status__in=['CANCELLED', 'COMPLETED'])
                .select_related('venue', 'menu')]
    kpis = {'upcoming': len(upcoming), 'today': sum(1 for e in upcoming if e.date == today and e.status in BanquetEvent.HOLDS_VENUE),
            'confirmed': sum((e.total for e in upcoming if e.status == 'CONFIRMED'), D0),
            'pipeline': sum((e.total for e in upcoming if e.status in ('ENQUIRY', 'TENTATIVE')), D0)}
    month = _date((request.GET.get('month') or '') + '-01') if request.GET.get('month') else today.replace(day=1)
    month = (month or today).replace(day=1)
    cal = []
    if tab == 'calendar':
        month_events = list(BanquetEvent.objects.filter(branch=branch, date__year=month.year, date__month=month.month)
                            .exclude(status='CANCELLED').select_related('venue'))
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month):
            cal.append([{'date': d, 'in_month': d.month == month.month, 'today': d == today,
                         'events': [e for e in month_events if e.date == d]} for d in week])
    prev_m = (month - datetime.timedelta(days=1)).replace(day=1)
    next_m = (month + datetime.timedelta(days=32)).replace(day=1)
    return _render(request, ctx, 'hotel_ops/_banquets.html', 'banquets', 'Banquets & events', tab=tab, events=list(events[:200]),
                   venues=venues, menus=BanquetMenu.objects.filter(branch=branch, is_active=True), kpis=kpis, today=today,
                   status=status, when=when, statuses=BanquetEvent.Status.choices, sessions=BanquetEvent.Session.choices,
                   types=BanquetEvent.TYPES, layouts=BanquetEvent.LAYOUTS, cal=cal, month=month, prev_m=prev_m, next_m=next_m,
                   can_edit=_can(request, 'banquets'), default_date=today + datetime.timedelta(days=14))


@hotel_admin_required
@require_POST
def banquet_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    pk = request.POST.get('pk')
    event = get_object_or_404(BanquetEvent, pk=pk, branch=branch) if pk else BanquetEvent(branch=branch, created_by=request.user)
    if event.status in ('COMPLETED', 'CANCELLED'):
        _report(request, 'Completed or cancelled events can’t be edited.')
        return redirect(_url(ctx, 'banquet_detail', pk=event.pk))
    err = _event_from_post(event, branch, request.POST)
    if err:
        _report(request, err)
        return redirect(_url(ctx, 'banquet_detail', pk=event.pk) if event.pk else _url(ctx, 'banquets'))
    event.save()
    _report(request, None, f'Event {event.code} saved.')
    return redirect(_url(ctx, 'banquet_detail', pk=event.pk))


@hotel_admin_required
def banquet_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    event = get_object_or_404(BanquetEvent.objects.select_related('venue', 'menu', 'company_account'), pk=pk, branch=branch)
    return _render(request, ctx, 'hotel_ops/_banquet_detail.html', 'banquets', event.name, e=event,
                   payments=event.payments.all(), venues=BanquetVenue.objects.filter(branch=branch, is_active=True),
                   menus=BanquetMenu.objects.filter(branch=branch, is_active=True), statuses=BanquetEvent.Status.choices,
                   sessions=BanquetEvent.Session.choices, types=BanquetEvent.TYPES, layouts=BanquetEvent.LAYOUTS,
                   methods=[m for m in FolioPayment.Method.choices if m[0] not in ('COMPANY', 'OTA')],
                   clash=ops.venue_clash(event), can_edit=_can(request, 'banquets'), today=timezone.localdate())


@hotel_admin_required
@require_POST
def banquet_action(request, restaurant_slug, ctx, pk):
    event = get_object_or_404(BanquetEvent.objects.select_related('venue', 'menu'), pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    status = {'tentative': 'TENTATIVE', 'confirm': 'CONFIRMED', 'complete': 'COMPLETED', 'cancel': 'CANCELLED'}.get(op)
    if op == 'cancel' and not (request.POST.get('reason') or '').strip():
        err = 'Give a reason for cancelling.'
    else:
        err = ops.set_event_status(event, request.user, status, request.POST.get('reason')) if status else 'Unknown action.'
    _report(request, err, f'Event {event.get_status_display().lower()}.')
    return redirect(_url(ctx, 'banquet_detail', pk=event.pk))


@hotel_admin_required
@require_POST
def banquet_payment_add(request, restaurant_slug, ctx, pk):
    event = get_object_or_404(BanquetEvent.objects.select_related('venue', 'menu'), pk=pk, branch=_branch(ctx))
    if event.status == 'CANCELLED' and request.POST.get('kind') != 'REFUND':
        _report(request, 'This event is cancelled; you can only record refunds.')
    else:
        p, err = ops.event_payment(event, request.user, request.POST.get('amount'), request.POST.get('method'),
                                   request.POST.get('reference'), 'REFUND' if request.POST.get('kind') == 'REFUND' else 'PAYMENT')
        _report(request, err, p and f'₹{p.amount:,.2f} recorded.')
    return redirect(_url(ctx, 'banquet_detail', pk=event.pk))


@hotel_admin_required
@require_POST
def banquet_payment_action(request, restaurant_slug, ctx, pk, payment_id):
    event = get_object_or_404(BanquetEvent, pk=pk, branch=_branch(ctx))
    p = get_object_or_404(BanquetPayment, pk=payment_id, event=event)
    reason = (request.POST.get('reason') or '').strip()
    if p.is_void:
        _report(request, 'Already voided.')
    elif not reason:
        _report(request, 'Give a reason for voiding the payment.')
    else:
        p.is_void, p.void_reason = True, reason[:200]
        p.save(update_fields=['is_void', 'void_reason'])
        _report(request, None, 'Payment voided.')
    return redirect(_url(ctx, 'banquet_detail', pk=event.pk))


@hotel_admin_required
def banquet_beo(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    event = get_object_or_404(BanquetEvent.objects.select_related('venue', 'menu'), pk=pk, branch=branch)
    return render(request, 'hotel_ops/banquet_beo.html', {**ctx, 'e': event, 'settings': HotelSettings.get_settings(branch),
                                                          'invoice': request.GET.get('doc') == 'invoice', 'now': timezone.now()})


@hotel_admin_required
def banquet_setup(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    return _render(request, ctx, 'hotel_ops/_banquet_setup.html', 'banquets', 'Venues & menus',
                   venues=BanquetVenue.objects.filter(branch=branch), menus=BanquetMenu.objects.filter(branch=branch),
                   can_edit=_can(request, 'banquets'))


@hotel_admin_required
@require_POST
def banquet_setup_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    name = (request.POST.get('name') or '').strip()
    active = request.POST.get('is_active', '1') == '1'
    if request.POST.get('kind') == 'venue':
        obj = BanquetVenue.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or BanquetVenue(branch=branch)
        rate, cap = _money(request.POST.get('full_day_rate') or '0'), _int(request.POST.get('capacity'), 0)
        if not (name or obj.pk) or rate is None or rate < 0 or cap < 1:
            _report(request, 'Enter a name, a capacity of at least 1 and a rate of zero or more.')
        else:
            obj.name, obj.kind = name[:120] or obj.name, (request.POST.get('venue_kind') or '')[:80]
            obj.capacity, obj.full_day_rate, obj.is_active = cap, rate, active
            obj.save()
            _report(request, None, 'Venue saved.')
    else:
        obj = BanquetMenu.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or BanquetMenu(branch=branch)
        price = _money(request.POST.get('price_per_cover'))
        if not (name or obj.pk) or price is None or price <= 0:
            _report(request, 'Enter a name and a price per cover above zero.')
        else:
            obj.name, obj.price_per_cover = name[:120] or obj.name, price
            obj.description, obj.is_active = (request.POST.get('description') or ''), active
            obj.save()
            _report(request, None, 'Menu package saved.')
    return redirect(_url(ctx, 'banquet_setup'))


# ============================================================ sales

@hotel_admin_required
def sales(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    tab = request.GET.get('tab', 'pipeline')
    leads = list(SalesLead.objects.filter(branch=branch).select_related('owner'))
    open_leads = [l for l in leads if l.is_open]
    q90 = today - datetime.timedelta(days=90)
    won = [l for l in leads if l.stage == 'WON' and l.closed_on and l.closed_on >= q90]
    lost = [l for l in leads if l.stage == 'LOST' and l.closed_on and l.closed_on >= q90]
    kpis = {'pipeline': sum((l.value for l in open_leads), D0), 'weighted': sum((l.weighted for l in open_leads), D0),
            'open': len(open_leads), 'won': sum((l.value for l in won), D0), 'won_n': len(won),
            'win_rate': _pct(len(won), len(won) + len(lost)),
            'due': sum(1 for l in open_leads if l.next_action_on and l.next_action_on <= today)}
    columns = [{'stage': s, 'label': SalesLead.Stage(s).label, 'leads': [l for l in open_leads if l.stage == s]}
               for s in SalesLead.PIPELINE]
    for c in columns:
        c['value'] = sum((l.value for l in c['leads']), D0)
    acts = (SalesActivity.objects.filter(lead__branch=branch).select_related('lead')
            .order_by('is_done', 'due_on'))
    return _render(request, ctx, 'hotel_ops/_sales.html', 'sales', 'Sales & CRM', tab=tab, leads=leads, columns=columns, kpis=kpis,
                   today=today, overdue=[a for a in acts if not a.is_done and a.due_on < today],
                   due_today=[a for a in acts if not a.is_done and a.due_on == today],
                   upcoming=[a for a in acts if not a.is_done and a.due_on > today][:30],
                   done=[a for a in acts if a.is_done][:20], types=SalesLead.Type.choices, sources=SalesLead.SOURCES,
                   kinds=SalesActivity.KINDS, can_edit=_can(request, 'sales'))


@hotel_admin_required
def sales_lead(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    lead = get_object_or_404(SalesLead.objects.select_related('owner', 'company_account', 'event'), pk=pk, branch=branch)
    return _render(request, ctx, 'hotel_ops/_sales_lead.html', 'sales', lead.company, l=lead, activities=lead.activities.all(),
                   types=SalesLead.Type.choices, sources=SalesLead.SOURCES, kinds=SalesActivity.KINDS, stages=SalesLead.Stage.choices,
                   today=timezone.localdate(), can_edit=_can(request, 'sales'))


@hotel_admin_required
@require_POST
def sales_lead_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    pk = request.POST.get('pk')
    lead = get_object_or_404(SalesLead, pk=pk, branch=branch) if pk else SalesLead(branch=branch, owner=request.user)
    company = (request.POST.get('company') or '').strip()
    value = _money(request.POST.get('value') or '0')
    if not company or value is None or value < 0:
        _report(request, 'Enter the client name and an estimated value of zero or more.')
        return _back(request, _url(ctx, 'sales'))
    lead.company, lead.value = company[:150], value
    lead.lead_type = request.POST.get('lead_type') if request.POST.get('lead_type') in SalesLead.Type.values else 'CORPORATE'
    lead.source = (request.POST.get('source') or 'Inbound call')[:40]
    for f, n in (('contact_name', 120), ('phone', 30), ('email', 254), ('next_action', 150)):
        setattr(lead, f, (request.POST.get(f) or '').strip()[:n])
    lead.requirement = (request.POST.get('requirement') or '').strip()
    lead.next_action_on = _date(request.POST.get('next_action_on'))
    if request.POST.get('stage') in SalesLead.PIPELINE and lead.is_open:
        lead.stage = request.POST.get('stage')
    lead.save()
    _report(request, None, f'{lead.company} saved.')
    return redirect(_url(ctx, 'sales_lead', pk=lead.pk))


@hotel_admin_required
@require_POST
def sales_lead_action(request, restaurant_slug, ctx, pk):
    lead = get_object_or_404(SalesLead, pk=pk, branch=_branch(ctx))
    op = request.POST.get('op')
    target = None
    if op in ('next', 'back'):
        err, ok = ops.move_lead(lead, 1 if op == 'next' else -1), f'Moved to {lead.get_stage_display()}.'
    elif op == 'win':
        target, err = ops.win_lead(lead, request.user, request.POST.get('create'))
        ok = 'Deal won.' + (' Company account created.' if request.POST.get('create') == 'account' else
                            ' Event enquiry created.' if request.POST.get('create') == 'event' else '')
    elif op == 'lose':
        err, ok = ops.lose_lead(lead, request.POST.get('reason')), 'Deal marked as lost.'
    else:
        err, ok = 'Unknown action.', None
    _report(request, err, ok)
    if not err and isinstance(target, BanquetEvent):
        return redirect(_url(ctx, 'banquet_detail', pk=target.pk))
    return _back(request, _url(ctx, 'sales_lead', pk=lead.pk))


@hotel_admin_required
@require_POST
def sales_activity_add(request, restaurant_slug, ctx, pk):
    lead = get_object_or_404(SalesLead, pk=pk, branch=_branch(ctx))
    subject = (request.POST.get('subject') or '').strip()
    if not subject:
        _report(request, 'Add a subject.')
    else:
        done = bool(request.POST.get('is_done'))
        due = _date(request.POST.get('due_on'), timezone.localdate())
        SalesActivity.objects.create(lead=lead, subject=subject[:200], due_on=due, owner=request.user, is_done=done,
                                     kind=request.POST.get('kind') if request.POST.get('kind') in dict(SalesActivity.KINDS) else 'CALL',
                                     done_at=timezone.now() if done else None)
        if not done and lead.is_open:
            lead.next_action, lead.next_action_on = subject[:150], due
            lead.save(update_fields=['next_action', 'next_action_on'])
        _report(request, None, 'Activity saved.')
    return _back(request, _url(ctx, 'sales_lead', pk=lead.pk))


@hotel_admin_required
@require_POST
def sales_activity_action(request, restaurant_slug, ctx, activity_id):
    act = get_object_or_404(SalesActivity.objects.select_related('lead'), pk=activity_id, lead__branch=_branch(ctx))
    act.is_done = not act.is_done
    act.done_at = timezone.now() if act.is_done else None
    act.save(update_fields=['is_done', 'done_at'])
    _report(request, None, 'Activity done.' if act.is_done else 'Activity reopened.')
    return _back(request, _url(ctx, 'sales') + '?tab=activities')


# ============================================================ packages & promotions

@hotel_admin_required
def packages(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    pkgs = RoomPackage.objects.filter(branch=branch)
    promos = PromoCode.objects.filter(branch=branch)
    sold = {}
    for ref in RoomBooking.objects.filter(branch=branch).values_list('folio_charges__reference', flat=True):
        if ref and ref.startswith('PKG-'):
            sold[ref] = sold.get(ref, 0) + 1
    for p in pkgs:
        p.sold = sold.get(f'PKG-{p.pk}', 0)
    for p in promos:
        p.state = p.problem()
    return _render(request, ctx, 'hotel_ops/_packages.html', 'packages', 'Packages & promotions', pkgs=pkgs, promos=promos,
                   plans=RoomPackage._meta.get_field('meal_plan').choices, can_edit=_can(request, 'sales'), today=timezone.localdate())


@hotel_admin_required
@require_POST
def package_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    p = RoomPackage.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or RoomPackage(branch=branch)
    if request.POST.get('op') == 'toggle' and p.pk:
        p.is_active = not p.is_active
        p.save(update_fields=['is_active'])
        _report(request, None, f'{p.name} {"on sale" if p.is_active else "paused"}.')
        return redirect(_url(ctx, 'packages'))
    name, price = (request.POST.get('name') or '').strip(), _money(request.POST.get('price_per_night'))
    gst = _money(request.POST.get('gst_percent') or '18')
    if not name or price is None or price <= 0 or gst is None or not (0 <= gst <= 28) or not (request.POST.get('inclusions') or '').strip():
        _report(request, 'Enter a name, the inclusions, a price per night above zero and GST of 0–28%.')
        return redirect(_url(ctx, 'packages'))
    p.name, p.price_per_night, p.gst_percent = name[:120], price, gst
    p.inclusions = request.POST.get('inclusions').strip()
    p.meal_plan = request.POST.get('meal_plan') if request.POST.get('meal_plan') in ('EP', 'CP', 'MAP', 'AP') else 'CP'
    p.min_nights = max(1, _int(request.POST.get('min_nights'), 1))
    p.valid_from, p.valid_until = _date(request.POST.get('valid_from')), _date(request.POST.get('valid_until'))
    p.save()
    _report(request, None, f'Package {p.name} saved.')
    return redirect(_url(ctx, 'packages'))


@hotel_admin_required
@require_POST
def promo_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    p = PromoCode.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first()
    if request.POST.get('op') == 'toggle' and p:
        p.is_active = not p.is_active
        p.save(update_fields=['is_active'])
        _report(request, None, f'{p.code} {"active" if p.is_active else "paused"}.')
        return redirect(_url(ctx, 'packages'))
    code = ''.join((request.POST.get('code') or '').upper().split())[:30]
    pct = _money(request.POST.get('percent'))
    if not code or pct is None or not (0 < pct <= 50):
        _report(request, 'Enter a code and a discount between 1% and 50%.')
    elif PromoCode.objects.filter(branch=branch, code=code).exclude(pk=getattr(p, 'pk', None)).exists():
        _report(request, f'{code} already exists.')
    else:
        p = p or PromoCode(branch=branch)
        cap = (request.POST.get('max_discount') or '').strip()
        p.code, p.percent, p.description = code, pct, (request.POST.get('description') or '')[:150]
        p.max_discount = _money(cap) if cap else None
        p.valid_from, p.valid_until = _date(request.POST.get('valid_from')), _date(request.POST.get('valid_until'))
        mu = (request.POST.get('max_uses') or '').strip()
        p.max_uses = _int(mu) if mu else None
        p.save()
        _report(request, None, f'Promo code {code} saved.')
    return redirect(_url(ctx, 'packages'))


@hotel_admin_required
@require_POST
def booking_offer_add(request, restaurant_slug, ctx, booking_id):
    branch = _branch(ctx)
    booking = get_object_or_404(RoomBooking.objects.select_related('room', 'room__category'), pk=booking_id, branch=branch)
    if request.POST.get('package'):
        pkg = RoomPackage.objects.filter(branch=branch, pk=request.POST.get('package'), is_active=True).first()
        charge, err = ops.apply_package(booking, request.user, pkg) if pkg else (None, 'Choose a package.')
        ok = charge and f'{pkg.name} added to the folio.'
    else:
        charge, err = ops.apply_promo(booking, request.user, request.POST.get('promo'))
        ok = charge and f'Promo applied: ₹{abs(charge.amount):,.2f} off.'
    _report(request, err, ok)
    return redirect(reverse('hotel_admin:booking_detail', kwargs={'restaurant_slug': ctx['rslug'], 'booking_id': booking.pk}))


# ============================================================ cashier

@hotel_admin_required
def cashier(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    shift = ops.open_shift(branch)
    takings = ops.shift_takings(shift) if shift else None
    history = CashierShift.objects.filter(branch=branch, closed_at__isnull=False).select_related('cashier')[:20]
    methods = dict(FolioPayment.Method.choices)
    return _render(request, ctx, 'hotel_ops/_cashier.html', 'cashier', 'Cashier', shift=shift, takings=takings,
                   by_method=[(methods.get(k, k), v) for k, v in (takings or {}).get('by_method', {}).items() if v],
                   paid_outs=shift.paid_outs.all() if shift else [], history=history,
                   denominations=CashierShift.DENOMINATIONS, can_edit=_can(request, 'finance'))


@hotel_admin_required
@require_POST
def cashier_open(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if ops.open_shift(branch):
        _report(request, 'A shift is already open. Close it before opening a new one.')
    else:
        fl = _money(request.POST.get('opening_float') or '0')
        if fl is None or fl < 0:
            _report(request, 'Enter the opening float as a number.')
        else:
            CashierShift.objects.create(branch=branch, cashier=request.user, opening_float=fl)
            _report(request, None, 'Shift opened.')
    return redirect(_url(ctx, 'cashier'))


@hotel_admin_required
@require_POST
def cashier_close(request, restaurant_slug, ctx):
    shift = ops.open_shift(_branch(ctx))
    if shift is None:
        _report(request, 'No shift is open.')
    else:
        counts = {str(d): request.POST.get(f'd_{d}') for d in CashierShift.DENOMINATIONS}
        if not any(_int(v) for v in counts.values()):
            _report(request, 'Count the cash drawer before closing.')
        else:
            err = ops.close_shift(shift, counts, request.POST.get('handed_over_to'), request.POST.get('notes'))
            v = shift.variance
            _report(request, err, f'Shift closed. Variance ₹{v:,.2f}.' if v is not None else 'Shift closed.')
    return redirect(_url(ctx, 'cashier'))


@hotel_admin_required
@require_POST
def cashier_paidout_add(request, restaurant_slug, ctx):
    shift = ops.open_shift(_branch(ctx))
    amount = _money(request.POST.get('amount'))
    purpose = (request.POST.get('purpose') or '').strip()
    if shift is None:
        _report(request, 'Open a shift first.')
    elif amount is None or amount <= 0 or not purpose:
        _report(request, 'Enter the purpose and an amount above zero.')
    elif amount > ops.shift_takings(shift)['expected']:
        _report(request, 'There isn’t that much cash in the drawer.')
    else:
        PaidOut.objects.create(shift=shift, amount=amount, purpose=purpose[:150], paid_to=(request.POST.get('paid_to') or '')[:120],
                               created_by=request.user)
        _report(request, None, f'Paid-out of ₹{amount:,.2f} recorded.')
    return redirect(_url(ctx, 'cashier'))


# ============================================================ security

@hotel_admin_required
def security(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    tab = request.GET.get('tab', 'visitors')
    visitors = Visitor.objects.filter(branch=branch, time_in__date__gte=today - datetime.timedelta(days=7))
    incidents = SecurityIncident.objects.filter(branch=branch)[:100]
    kpis = {'inside': Visitor.objects.filter(branch=branch, time_out__isnull=True, time_in__date=today).count(),
            'today': Visitor.objects.filter(branch=branch, time_in__date=today).count(),
            'guests': RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN').aggregate(n=Sum('num_guests'))['n'] or 0,
            'open': SecurityIncident.objects.filter(branch=branch, is_closed=False).count()}
    return _render(request, ctx, 'hotel_ops/_security.html', 'security', 'Security & visitors', tab=tab, visitors=visitors,
                   incidents=incidents, kpis=kpis, kinds=SecurityIncident.KINDS, severities=SecurityIncident.SEVERITY,
                   rooms=_in_house(branch).filter(booking_status='CHECKED_IN'), can_edit=_can(request, 'back_office'))


@hotel_admin_required
@require_POST
def security_visitor_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    name, meeting = (request.POST.get('name') or '').strip(), (request.POST.get('meeting') or '').strip()
    if not name or not meeting:
        _report(request, 'Enter the visitor’s name and who they are meeting.')
    else:
        v = Visitor.objects.create(branch=branch, name=name[:120], phone=(request.POST.get('phone') or '')[:30],
                                   purpose=(request.POST.get('purpose') or 'Visit')[:80], meeting=meeting[:120],
                                   id_type=(request.POST.get('id_type') or '')[:30],
                                   id_last4=''.join(ch for ch in (request.POST.get('id_number') or '') if ch.isalnum())[-4:],
                                   vehicle=(request.POST.get('vehicle') or '').upper()[:20], created_by=request.user)
        _report(request, None, f'Visitor pass {v.pass_number} issued.')
    return redirect(_url(ctx, 'security'))


@hotel_admin_required
@require_POST
def security_visitor_action(request, restaurant_slug, ctx, pk):
    v = get_object_or_404(Visitor, pk=pk, branch=_branch(ctx))
    if v.time_out is None:
        v.time_out = timezone.now()
        v.save(update_fields=['time_out'])
        _report(request, None, f'{v.name} checked out.')
    return redirect(_url(ctx, 'security'))


@hotel_admin_required
@require_POST
def security_incident_add(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    desc, location = (request.POST.get('description') or '').strip(), (request.POST.get('location') or '').strip()
    sev = request.POST.get('severity') if request.POST.get('severity') in dict(SecurityIncident.SEVERITY) else 'LOW'
    if not desc or not location:
        _report(request, 'Describe what happened and where.')
    else:
        inc = SecurityIncident.objects.create(
            branch=branch, kind=(request.POST.get('kind') or 'Other')[:40], location=location[:120], severity=sev,
            description=desc, action_taken=(request.POST.get('action_taken') or '').strip(),
            reported_by=(request.POST.get('reported_by') or '')[:120], created_by=request.user)
        if sev in ('HIGH', 'CRITICAL'):
            ops._alert(branch, f'{inc.get_severity_display()} incident · {inc.kind}', f'{location}: {desc[:180]}',
                       _url(ctx, 'security') + '?tab=incidents', level='critical', key=f'inc-{inc.pk}')
        _report(request, None, 'Incident reported.')
    return redirect(_url(ctx, 'security') + '?tab=incidents')


@hotel_admin_required
@require_POST
def security_incident_action(request, restaurant_slug, ctx, pk):
    inc = get_object_or_404(SecurityIncident, pk=pk, branch=_branch(ctx))
    if not inc.is_closed:
        note = (request.POST.get('action_taken') or '').strip()
        if note:
            inc.action_taken = (inc.action_taken + '\n' + note).strip()
        if not inc.action_taken:
            _report(request, 'Record the action taken before closing.')
            return redirect(_url(ctx, 'security') + '?tab=incidents')
        inc.is_closed, inc.closed_at = True, timezone.now()
        inc.save()
        _report(request, None, 'Incident closed.')
    return redirect(_url(ctx, 'security') + '?tab=incidents')


# ============================================================ compliance

@hotel_admin_required
def compliance(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    tab = request.GET.get('tab', 'licences')
    licences = list(ComplianceLicence.objects.filter(branch=branch))
    for l in licences:
        l.days_left = (l.expires_on - today).days
    filings = list(StatutoryFiling.objects.filter(branch=branch, due_on__gte=today - datetime.timedelta(days=120)))
    for f in filings:
        f.days_left = (f.due_on - today).days
    checks = list(SafetyCheck.objects.filter(branch=branch))
    for c in checks:
        c.days_left = (c.next_due - today).days if c.next_due else None
    kpis = {'licences': sum(1 for l in licences if l.days_left <= 60), 'expired': sum(1 for l in licences if l.days_left < 0),
            'filings': sum(1 for f in filings if not f.filed_on and f.days_left <= 7),
            'safety': sum(1 for c in checks if c.days_left is None or c.days_left < 0),
            'foreign': sum(1 for b in RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN') if b.is_foreign_national)}
    return _render(request, ctx, 'hotel_ops/_compliance.html', 'compliance', 'Compliance', tab=tab, licences=licences, filings=filings,
                   checks=checks, kpis=kpis, today=today, this_month=today.replace(day=1),
                   can_edit=_can(request, 'back_office'))


@hotel_admin_required
@require_POST
def compliance_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    kind = request.POST.get('kind')
    tab = {'licence': 'licences', 'filing': 'filings', 'safety': 'safety'}.get(kind, 'licences')
    back = _url(ctx, 'compliance') + f'?tab={tab}'
    name = (request.POST.get('name') or '').strip()
    if kind == 'licence':
        obj = ComplianceLicence.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or ComplianceLicence(branch=branch)
        exp = _date(request.POST.get('expires_on'))
        if not (name or obj.pk) or exp is None:
            _report(request, 'Enter the licence name and its expiry date.')
            return redirect(back)
        obj.name, obj.expires_on = name[:120] or obj.name, exp
        obj.authority, obj.number = (request.POST.get('authority') or '')[:150], (request.POST.get('number') or '')[:80]
        obj.notes = (request.POST.get('notes') or '')[:255]
        doc = request.FILES.get('document')
        if doc:
            if doc.size > 5 * 1024 * 1024 or not doc.name.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png')):
                _report(request, 'Upload a PDF or image under 5 MB.')
                return redirect(back)
            obj.document = doc
        obj.save()
    elif kind == 'filing':
        due = _date(request.POST.get('due_on'))
        if not name or due is None:
            _report(request, 'Enter the filing and its due date.')
            return redirect(back)
        StatutoryFiling.objects.create(branch=branch, name=name[:120], due_on=due)
    elif kind == 'safety':
        every = _int(request.POST.get('every_days'), 30)
        if not name or not (1 <= every <= 730):
            _report(request, 'Enter the check and how often it is due (1–730 days).')
            return redirect(back)
        SafetyCheck.objects.create(branch=branch, name=name[:120], every_days=every, last_done=_date(request.POST.get('last_done')))
    _report(request, None, 'Saved.')
    return redirect(back)


@hotel_admin_required
@require_POST
def compliance_action(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    op, pk = request.POST.get('op'), request.POST.get('pk') or 0
    today = timezone.localdate()
    tab = 'licences'
    if op == 'seed':
        for name in ops.STANDARD_LICENCES:
            ComplianceLicence.objects.get_or_create(branch=branch, name=name, defaults={'expires_on': today + datetime.timedelta(days=365)})
        for name, every in ops.STANDARD_SAFETY:
            SafetyCheck.objects.get_or_create(branch=branch, name=name, defaults={'every_days': every})
        _report(request, None, 'Standard licences and safety checks added. Enter each one’s real number and expiry date.')
    elif op == 'month_filings':
        n = ops.monthly_filings(branch, today.replace(day=1))
        tab = 'filings'
        _report(request, None, f'{n} filings added for {today:%B}.' if n else 'This month’s filings are already listed.')
    elif op == 'filed':
        f = get_object_or_404(StatutoryFiling, pk=pk, branch=branch)
        f.filed_on, f.reference = today, (request.POST.get('reference') or '')[:80]
        f.save()
        tab = 'filings'
        _report(request, None, f'{f.name} marked as filed.')
    elif op == 'done':
        c = get_object_or_404(SafetyCheck, pk=pk, branch=branch)
        c.last_done, c.done_by = today, (request.POST.get('done_by') or request.user.get_full_name() or request.user.username)[:120]
        c.save()
        tab = 'safety'
        _report(request, None, f'{c.name} recorded as done today.')
    elif op == 'renew':
        l = get_object_or_404(ComplianceLicence, pk=pk, branch=branch)
        exp = _date(request.POST.get('expires_on'))
        if exp is None or exp <= today:
            _report(request, 'Enter a new expiry date in the future.')
        else:
            l.expires_on = exp
            if request.POST.get('number'):
                l.number = request.POST.get('number')[:80]
            l.save()
            _report(request, None, f'{l.name} renewed until {exp:%d %b %Y}.')
    elif op in ('delete_licence', 'delete_filing', 'delete_safety'):
        model, tab = {'delete_licence': (ComplianceLicence, 'licences'), 'delete_filing': (StatutoryFiling, 'filings'),
                      'delete_safety': (SafetyCheck, 'safety')}[op]
        get_object_or_404(model, pk=pk, branch=branch).delete()
        _report(request, None, 'Removed.')
    return redirect(_url(ctx, 'compliance') + f'?tab={tab}')


# ============================================================ assets & contracts

@hotel_admin_required
def assets(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    today = timezone.localdate()
    tab = request.GET.get('tab', 'assets')
    items = list(FixedAsset.objects.filter(branch=branch))
    for a in items:
        a.bv = a.book_value(today)
    contracts = list(ServiceContract.objects.filter(branch=branch))
    for c in contracts:
        c.days_left = (c.end_on - today).days
    live = [a for a in items if not a.is_disposed]
    kpis = {'count': len(live), 'cost': sum((a.cost for a in live), D0), 'book': sum((a.bv for a in live), D0),
            'contracts': sum(1 for c in contracts if c.days_left <= 30),
            'warranty': sum(1 for a in live if a.warranty_until and 0 <= (a.warranty_until - today).days <= 60)}
    return _render(request, ctx, 'hotel_ops/_assets.html', 'assets', 'Assets & contracts', tab=tab, items=items, contracts=contracts,
                   kpis=kpis, today=today, categories=FixedAsset.CATEGORIES, can_edit=_can(request, 'back_office'))


@hotel_admin_required
@require_POST
def asset_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    a = FixedAsset.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first() or FixedAsset(branch=branch)
    if request.POST.get('op') == 'dispose' and a.pk:
        a.is_disposed = True
        a.notes = (request.POST.get('notes') or a.notes)[:255]
        a.save()
        _report(request, None, f'{a.name} marked as disposed.')
        return redirect(_url(ctx, 'assets'))
    name, cost, dep = (request.POST.get('name') or '').strip(), _money(request.POST.get('cost')), _money(request.POST.get('depreciation_percent') or '10')
    bought = _date(request.POST.get('purchased_on'))
    if not name or cost is None or cost <= 0 or bought is None or dep is None or not (0 <= dep <= 100):
        _report(request, 'Enter the name, purchase date, a cost above zero and depreciation of 0–100%.')
        return redirect(_url(ctx, 'assets'))
    a.name, a.cost, a.depreciation_percent, a.purchased_on = name[:150], cost, dep, bought
    a.category = request.POST.get('category') if request.POST.get('category') in FixedAsset.CATEGORIES else 'Other'
    a.location, a.vendor = (request.POST.get('location') or '')[:120], (request.POST.get('vendor') or '')[:120]
    a.warranty_until = _date(request.POST.get('warranty_until'))
    a.save()
    _report(request, None, f'Asset {a.tag} saved.')
    return redirect(_url(ctx, 'assets'))


@hotel_admin_required
@require_POST
def contract_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    back = _url(ctx, 'assets') + '?tab=contracts'
    c = ServiceContract.objects.filter(branch=branch, pk=request.POST.get('pk') or 0).first()
    if request.POST.get('op') == 'renew' and c:
        end, value = _date(request.POST.get('end_on')), _money(request.POST.get('annual_value') or str(c.annual_value))
        if end is None or end <= c.end_on or value is None or value < 0:
            _report(request, 'Enter a new end date after the current one and the annual value.')
        else:
            c.start_on, c.end_on, c.annual_value = c.end_on + datetime.timedelta(days=1), end, value
            c.save()
            _report(request, None, f'{c.vendor} renewed until {end:%d %b %Y}.')
        return redirect(back)
    if request.POST.get('op') == 'delete' and c:
        c.delete()
        _report(request, None, 'Contract removed.')
        return redirect(back)
    vendor, scope = (request.POST.get('vendor') or '').strip(), (request.POST.get('scope') or '').strip()
    start, end, value = _date(request.POST.get('start_on')), _date(request.POST.get('end_on')), _money(request.POST.get('annual_value') or '0')
    if not vendor or not scope or not start or not end or end <= start or value is None or value < 0:
        _report(request, 'Enter the vendor, scope, a start date, a later end date and the annual value.')
        return redirect(back)
    c = c or ServiceContract(branch=branch)
    c.vendor, c.scope, c.start_on, c.end_on, c.annual_value = vendor[:150], scope[:200], start, end, value
    c.contact = (request.POST.get('contact') or '')[:150]
    c.save()
    _report(request, None, 'Contract saved.')
    return redirect(back)


# ============================================================ energy

@hotel_admin_required
def energy(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    s = HotelSettings.get_settings(branch)
    today = timezone.localdate()
    readings = list(UtilityReading.objects.filter(branch=branch, date__gte=today - datetime.timedelta(days=30)).order_by('date'))
    for r in readings:
        r.total_cost = r.cost(s)
    last7, prev7 = readings[-7:], readings[-14:-7]

    def per_room(rs):
        rooms = sum(r.occupied_rooms for r in rs)
        return (sum((r.electricity_kwh for r in rs), D0) / rooms).quantize(Decimal('0.1')) if rooms else None

    month = [r for r in readings if r.date.month == today.month and r.date.year == today.year]
    peak = max([r.electricity_kwh for r in readings] or [0]) or 1
    for r in readings:
        r.bar = int(r.electricity_kwh / peak * 100)
    kpis = {'per_room': per_room(last7), 'per_room_prev': per_room(prev7),
            'water': (sum((r.water_kl for r in last7), D0) * 1000 / max(1, sum(r.occupied_rooms for r in last7))).quantize(Decimal('1')) if last7 else None,
            'month_cost': sum((r.total_cost for r in month), D0), 'diesel': sum((r.diesel_l for r in readings), D0)}
    occupied = RoomBooking.objects.filter(branch=branch, booking_status='CHECKED_IN').count()
    return _render(request, ctx, 'hotel_ops/_energy.html', 'energy', 'Energy & utilities', readings=readings,
                   recent=list(reversed(readings))[:14], kpis=kpis, s=s, today=today, occupied=occupied,
                   recorded_today=any(r.date == today for r in readings), can_edit=_can(request, 'back_office'))


@hotel_admin_required
@require_POST
def energy_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if request.POST.get('op') == 'settings':
        s = HotelSettings.get_settings(branch)
        vals = {f: _money(request.POST.get(f)) for f in ('tariff_electricity', 'tariff_water', 'tariff_lpg', 'tariff_diesel', 'energy_target_kwh')}
        if any(v is None or v < 0 for v in vals.values()):
            _report(request, 'Tariffs and the target must be numbers of zero or more.')
        else:
            for k, v in vals.items():
                setattr(s, k, v)
            s.save(update_fields=list(vals))
            _report(request, None, 'Tariffs saved.')
        return redirect(_url(ctx, 'energy'))
    day = _date(request.POST.get('date'), timezone.localdate())
    vals = {f: _money(request.POST.get(f) or '0') for f in ('electricity_kwh', 'water_kl', 'lpg_kg', 'diesel_l')}
    if any(v is None or v < 0 for v in vals.values()) or day > timezone.localdate():
        _report(request, 'Enter readings as numbers of zero or more, for today or an earlier day.')
        return redirect(_url(ctx, 'energy'))
    occupied = RoomBooking.objects.filter(branch=branch, check_in_date__lte=day, check_out_date__gt=day,
                                          booking_status__in=['CHECKED_IN', 'CHECKED_OUT']).count()
    UtilityReading.objects.update_or_create(branch=branch, date=day, defaults={
        **vals, 'occupied_rooms': occupied, 'notes': (request.POST.get('notes') or '')[:200], 'recorded_by': request.user})
    _report(request, None, f'Readings for {day:%d %b} saved.')
    return redirect(_url(ctx, 'energy'))


# ============================================================ forecast

@hotel_admin_required
def reports_forecast(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    data = ops.forecast(branch)
    cats = list(RoomCategory.objects.filter(branch=branch, is_active=True)) if hasattr(RoomCategory, 'is_active') else \
        list(RoomCategory.objects.filter(branch=branch))
    lead = cats[0] if cats else None
    applied = set(RateOverride.objects.filter(category__branch=branch, label__startswith='Forecast ',
                                              start_date__gte=timezone.localdate()).values_list('start_date', flat=True))
    for r in data['rows']:
        r['rate'] = pricing.nightly_rates(lead, r['date'], r['date'] + datetime.timedelta(days=1))[0][1] if lead else None
        r['applied'] = r['date'] in applied
    peak = max([r['sellable'] for r in data['rows']] or [1]) or 1
    for r in data['rows']:
        r['otb_w'], r['pick_w'] = int(r['otb'] / peak * 100), int(r['pickup'] / peak * 100)
    return _render(request, ctx, 'hotel_ops/_forecast.html', 'forecast', 'Occupancy forecast', data=data, lead=lead,
                   can_edit=_can(request, 'revenue'))


@hotel_admin_required
@require_POST
def rate_suggest_apply(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    day = _date(request.POST.get('date'))
    change = _int(request.POST.get('change'), 0)
    if day is None or day < timezone.localdate() or not (-30 <= change <= 30) or change == 0:
        _report(request, 'That suggestion is no longer valid.')
        return redirect(_url(ctx, 'reports_forecast'))
    made = 0
    for cat in RoomCategory.objects.filter(branch=branch):
        price = pricing.nightly_rates(cat, day, day + datetime.timedelta(days=1))[0][1]
        RateOverride.objects.create(category=cat, label=f'Forecast {change:+d}%', start_date=day, end_date=day,
                                    price_per_night=(price * (1 + Decimal(change) / 100)).quantize(Decimal('1')), created_by=request.user)
        made += 1
    _report(request, None, f'{change:+d}% applied to {made} room types for {day:%d %b}. Channels pick it up from the rates feed.')
    return redirect(_url(ctx, 'reports_forecast'))
