"""Hotel performance figures: occupancy, ADR, RevPAR, revenue mix, and the night audit.

A room night is "sold" when a stay covers that night and the stay is checked in or out.
Future nights also count reserved stays ("on the books"); past reserved stays that never
arrived do not. Room revenue is the stay's net room charge spread evenly over its nights.
"""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from hotel.models import (FolioCharge, FolioPayment, HousekeepingTask, MaintenanceIssue, NightAudit, Room,
                          RoomBooking, RoomCategory)
from hotel.pricing import nights_between

ZERO = Decimal('0')
CENT = Decimal('0.01')
SOLD = ('CHECKED_IN', 'CHECKED_OUT')

PERIODS = [('today', 'Today'), ('yesterday', 'Yesterday'), ('7d', 'Last 7 days'), ('30d', 'Last 30 days'),
           ('mtd', 'This month'), ('last_month', 'Last month'), ('next_30', 'Next 30 days (on the books)')]


def resolve_period(key, start=None, end=None):
    today = timezone.localdate()
    if key == 'custom' and start and end:
        return (min(start, end), max(start, end), 'Custom')
    if key == 'yesterday':
        d = today - datetime.timedelta(days=1)
        return d, d, 'Yesterday'
    if key == '7d':
        return today - datetime.timedelta(days=6), today, 'Last 7 days'
    if key == 'mtd':
        return today.replace(day=1), today, 'This month'
    if key == 'last_month':
        last = today.replace(day=1) - datetime.timedelta(days=1)
        return last.replace(day=1), last, 'Last month'
    if key == 'next_30':
        return today, today + datetime.timedelta(days=29), 'Next 30 days'
    if key == 'today':
        return today, today, 'Today'
    return today - datetime.timedelta(days=29), today, 'Last 30 days'


def _pct(a, b):
    return round(float(a) / float(b) * 100, 1) if b else 0.0


def _div(a, b):
    return (Decimal(a) / Decimal(b)).quantize(CENT) if b else ZERO


def _stays(branch, start, end):
    """Stays touching any night in [start, end]."""
    return (RoomBooking.objects.filter(branch=branch, check_in_date__lte=end, check_out_date__gt=start)
            .filter(Q(booking_status__in=SOLD) | Q(booking_status='RESERVED'))
            .select_related('room', 'room__category'))


def _counts_for(b, night, today):
    return b.booking_status in SOLD or (b.booking_status == 'RESERVED' and night >= today)


def _ooo_by_night(branch, start, end):
    """{night: set(room ids)} out of order."""
    out = defaultdict(set)
    issues = MaintenanceIssue.objects.filter(branch=branch, out_of_order=True)
    for i in issues:
        first = i.block_from or i.created_at.date()
        last = i.block_until or (i.resolved_at.date() - datetime.timedelta(days=1) if i.resolved_at else end)
        if i.status == MaintenanceIssue.Status.RESOLVED and i.resolved_at:
            last = min(last, i.resolved_at.date() - datetime.timedelta(days=1))
        d = max(first, start)
        while d <= min(last, end):
            out[d].add(i.room_id)
            d += datetime.timedelta(days=1)
    return out


def daily(branch, start, end):
    """One row per night with sold rooms, sellable rooms, revenue, occupancy, ADR and RevPAR."""
    today = timezone.localdate()
    rooms = list(Room.objects.filter(branch=branch, is_active=True).values_list('id', 'category_id'))
    ooo = _ooo_by_night(branch, start, end)
    sold = defaultdict(int)
    revenue = defaultdict(lambda: ZERO)
    for b in _stays(branch, start, end):
        per_night = _div(b.total_room_charges, b.nights_count)
        for night in nights_between(b.check_in_date, b.check_out_date):
            if start <= night <= end and _counts_for(b, night, today):
                sold[night] += 1
                revenue[night] += per_night
    rows = []
    d = start
    while d <= end:
        sellable = max(0, len(rooms) - len(ooo.get(d, ())))
        rev = revenue[d]
        rows.append({'date': d, 'sold': sold[d], 'sellable': sellable, 'ooo': len(ooo.get(d, ())), 'revenue': rev,
                     'occupancy': _pct(sold[d], sellable), 'adr': _div(rev, sold[d]), 'revpar': _div(rev, sellable),
                     'forecast': d >= today})
        d += datetime.timedelta(days=1)
    return rows


def summary(branch, start, end):
    today = timezone.localdate()
    s_dt = timezone.make_aware(datetime.datetime.combine(start, datetime.time.min))
    e_dt = timezone.make_aware(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min))
    rows = daily(branch, start, end)
    sold = sum(r['sold'] for r in rows)
    sellable = sum(r['sellable'] for r in rows)
    room_rev = sum((r['revenue'] for r in rows), ZERO)

    charges = FolioCharge.objects.filter(booking__branch=branch, is_void=False, service_date__gte=start, service_date__lte=end)
    extras = charges.aggregate(net=Sum('amount'), tax=Sum('tax_amount'))
    extras_net = extras['net'] or ZERO
    extras_by_kind = []
    labels = dict(FolioCharge.Kind.choices)
    for row in charges.values('kind').annotate(n=Count('id'), net=Sum('amount')).order_by('-net'):
        extras_by_kind.append({'kind': labels.get(row['kind'], row['kind']), 'count': row['n'], 'net': row['net'] or ZERO})

    pays = FolioPayment.objects.filter(booking__branch=branch, is_void=False, received_at__gte=s_dt, received_at__lt=e_dt)
    methods = dict(FolioPayment.Method.choices)
    by_method = defaultdict(lambda: ZERO)
    for p in pays:
        by_method[methods.get(p.method, p.method)] += p.signed_amount
    collected = sum(by_method.values(), ZERO)

    # Room-type performance
    per_cat = defaultdict(lambda: {'sold': 0, 'revenue': ZERO})
    for b in _stays(branch, start, end):
        per_night = _div(b.total_room_charges, b.nights_count)
        for night in nights_between(b.check_in_date, b.check_out_date):
            if start <= night <= end and _counts_for(b, night, today):
                c = per_cat[b.room.category_id]
                c['sold'] += 1
                c['revenue'] += per_night
    days = (end - start).days + 1
    room_count = dict(Room.objects.filter(branch=branch, is_active=True).values('category_id').annotate(n=Count('id')).values_list('category_id', 'n'))
    categories = []
    for cat in RoomCategory.objects.filter(branch=branch).order_by('name'):
        c = per_cat.get(cat.id, {'sold': 0, 'revenue': ZERO})
        capacity = room_count.get(cat.id, 0) * days
        if not capacity and not c['sold']:
            continue
        categories.append({'name': cat.name, 'rooms': room_count.get(cat.id, 0), 'sold': c['sold'], 'revenue': c['revenue'],
                           'adr': _div(c['revenue'], c['sold']), 'occupancy': _pct(c['sold'], capacity)})

    # Arrivals in the period: source mix, length of stay, lead time
    arrivals = RoomBooking.objects.filter(branch=branch, check_in_date__gte=start, check_in_date__lte=end)
    live = arrivals.exclude(booking_status__in=['CANCELLED', 'NO_SHOW'])
    sources = []
    src_labels = dict(RoomBooking.Source.choices)
    for row in live.values('source').annotate(n=Count('id'), rev=Sum('total_room_charges')).order_by('-n'):
        sources.append({'source': src_labels.get(row['source'], row['source']), 'bookings': row['n'], 'revenue': row['rev'] or ZERO})
    channels = []
    from integrations.models import ExternalBookingLink
    for row in (ExternalBookingLink.objects.filter(booking__in=live).values('source', 'connection__name')
                .annotate(n=Count('id'), rev=Sum('booking__grand_total'), com=Sum('commission_amount')).order_by('-rev')):
        channels.append({'name': row['connection__name'] or row['source'].replace('_', ' ').title(), 'bookings': row['n'],
                         'revenue': row['rev'] or ZERO, 'commission': row['com'] or ZERO,
                         'net': (row['rev'] or ZERO) - (row['com'] or ZERO)})
    stay_lengths = [b.nights_count for b in live.only('check_in_date', 'check_out_date')]
    leads = [(b.check_in_date - timezone.localtime(b.created_at).date()).days for b in live.only('check_in_date', 'created_at')]

    cancelled = RoomBooking.objects.filter(branch=branch, booking_status='CANCELLED', cancelled_at__gte=s_dt, cancelled_at__lt=e_dt).count()
    no_shows = arrivals.filter(booking_status='NO_SHOW').count()
    made = RoomBooking.objects.filter(branch=branch, created_at__gte=s_dt, created_at__lt=e_dt).count()

    return {
        'rows': rows, 'sold': sold, 'sellable': sellable, 'occupancy': _pct(sold, sellable),
        'room_revenue': room_rev.quantize(CENT), 'adr': _div(room_rev, sold), 'revpar': _div(room_rev, sellable),
        'extras_revenue': extras_net, 'extras_tax': extras['tax'] or ZERO, 'extras_by_kind': extras_by_kind,
        'total_revenue': (room_rev + extras_net).quantize(CENT), 'trevpar': _div(room_rev + extras_net, sellable),
        'payments': sorted(by_method.items(), key=lambda kv: -kv[1]), 'collected': collected,
        'categories': categories, 'sources': sources, 'channels': channels,
        'arrivals': live.count(), 'avg_stay': round(sum(stay_lengths) / len(stay_lengths), 1) if stay_lengths else 0,
        'avg_lead': round(sum(leads) / len(leads), 1) if leads else 0,
        'cancelled': cancelled, 'no_shows': no_shows, 'bookings_made': made,
        'cancel_rate': _pct(cancelled, made) if made else 0.0,
        'has_forecast': any(r['forecast'] for r in rows),
    }


def front_office_lists(branch, day):
    base = RoomBooking.objects.filter(branch=branch).select_related('room', 'room__category')
    return {
        'arrivals': base.filter(check_in_date=day).exclude(booking_status__in=['CANCELLED']).order_by('booking_status', 'room__room_number'),
        'departures': base.filter(check_out_date=day, booking_status__in=['CHECKED_IN', 'CHECKED_OUT']).order_by('booking_status', 'room__room_number'),
        'in_house': base.filter(booking_status='CHECKED_IN').order_by('room__room_number'),
    }


def foreign_guests(branch, start, end):
    """Form C register: foreign nationals arriving in the period."""
    qs = (RoomBooking.objects.filter(branch=branch, check_in_date__gte=start, check_in_date__lte=end)
          .exclude(booking_status__in=['CANCELLED', 'NO_SHOW']).exclude(nationality='')
          .select_related('room').order_by('check_in_date'))
    return [b for b in qs if b.is_foreign_national]


# ------------------------------------------------------------------ night audit

def business_date(branch):
    """The first day not yet closed, never later than today."""
    today = timezone.localdate()
    last = NightAudit.objects.filter(branch=branch).order_by('-business_date').first()
    if not last:
        return today
    return min(today, last.business_date + datetime.timedelta(days=1))


def audit_checks(branch, day):
    base = RoomBooking.objects.filter(branch=branch).select_related('room')
    pending_arrivals = base.filter(booking_status='RESERVED', check_in_date__lte=day).order_by('check_in_date')
    pending_departures = base.filter(booking_status='CHECKED_IN', check_out_date__lte=day).order_by('check_out_date')
    open_balances = []
    for b in base.filter(booking_status='CHECKED_OUT', check_out_date__gte=day - datetime.timedelta(days=30),
                         payment_status__in=['PENDING', 'PARTIALLY_PAID']):
        bal = b.folio().balance
        if bal > 0:
            open_balances.append((b, bal))
    tasks = HousekeepingTask.objects.filter(branch=branch, task_date__lte=day, status__in=['PENDING', 'IN_PROGRESS']).count()
    return {'pending_arrivals': pending_arrivals, 'pending_departures': pending_departures,
            'open_balances': open_balances, 'open_tasks': tasks}


def day_snapshot(branch, day):
    s = summary(branch, day, day)
    lists = front_office_lists(branch, day)
    return {
        'date': day.isoformat(), 'sold': s['sold'], 'sellable': s['sellable'], 'occupancy': s['occupancy'],
        'room_revenue': str(s['room_revenue']), 'adr': str(s['adr']), 'revpar': str(s['revpar']),
        'extras_revenue': str(s['extras_revenue']), 'collected': str(s['collected']),
        'payments': [[k, str(v)] for k, v in s['payments']],
        'arrivals': lists['arrivals'].filter(booking_status__in=['CHECKED_IN', 'CHECKED_OUT']).count(),
        'departures': lists['departures'].filter(booking_status='CHECKED_OUT').count(),
        'in_house': lists['in_house'].count(), 'no_shows': s['no_shows'], 'cancelled': s['cancelled'],
    }
