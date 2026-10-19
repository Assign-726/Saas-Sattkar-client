"""Management dashboard for the hotel: how the property is doing over a period, compared
with the period before, plus what is happening right now and what needs attention.

Room figures come from hotel.reports (the same numbers as Reports → Performance), so the
dashboard and the reports never disagree. Banquets and directly-paid spa follow the P&L rule
(finance.pnl): completed events and treatments not already charged to a room folio.
"""
import datetime
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from hotel import ops, reports
from hotel.models import (BanquetEvent, GuestRequest, MaintenanceIssue, Room, RoomBooking, SpaAppointment)

ZERO = Decimal('0')

PERIODS = [('today', 'Today'), ('yesterday', 'Yesterday'), ('7d', '7 days'), ('30d', '30 days'),
           ('mtd', 'This month'), ('last_month', 'Last month'), ('next_30', 'Next 30 days')]


def previous_period(start, end):
    length = (end - start).days + 1
    p_end = start - datetime.timedelta(days=1)
    return p_end - datetime.timedelta(days=length - 1), p_end


def pct_change(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if not prev:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _other_revenue(branch, start, end):
    events = list(BanquetEvent.objects.filter(branch=branch, status='COMPLETED', date__gte=start, date__lte=end)
                  .select_related('venue', 'menu'))
    spa = (SpaAppointment.objects.filter(branch=branch, status='COMPLETED', settled_by__in=['CASH', 'CARD', 'UPI'],
                                         date__gte=start, date__lte=end).aggregate(n=Count('id'), net=Sum('price')))
    return {'banquets': sum((e.subtotal for e in events), ZERO), 'banquet_count': len(events),
            'spa': spa['net'] or ZERO, 'spa_count': spa['n'] or 0}


def live(branch):
    """Today at the front desk."""
    today = timezone.localdate()
    stays = RoomBooking.objects.filter(branch=branch)
    rooms = Room.objects.filter(branch=branch, is_active=True)
    total_rooms = rooms.count()
    in_house = list(stays.filter(booking_status='CHECKED_IN').select_related('room'))
    ooo = sum(1 for i in MaintenanceIssue.objects.filter(branch=branch, out_of_order=True).exclude(status='RESOLVED')
              if i.blocks(today, today + datetime.timedelta(days=1)))
    sellable = max(0, total_rooms - ooo)
    tonight = stays.filter(check_in_date__lte=today, check_out_date__gt=today,
                           booking_status__in=['RESERVED', 'CHECKED_IN']).count()
    balance_due = ZERO
    for b in in_house:
        bal = b.folio().balance
        if bal > 0:
            balance_due += bal
    return {
        'in_house': len(in_house),
        'guests_in_house': sum(b.num_guests or 0 for b in in_house),
        'tonight': tonight, 'sellable': sellable, 'total_rooms': total_rooms,
        'tonight_pct': round(tonight * 100 / sellable) if sellable else 0,
        'arrivals_due': stays.filter(check_in_date=today, booking_status='RESERVED').count(),
        'arrived': stays.filter(check_in_date=today, booking_status='CHECKED_IN').count(),
        'departures_due': stays.filter(check_out_date=today, booking_status='CHECKED_IN').count(),
        'departed': stays.filter(check_out_date=today, booking_status='CHECKED_OUT').count(),
        'dirty': rooms.filter(status=Room.RoomStatus.CLEANING).count(),
        'ooo': ooo,
        'balance_due': balance_due,
        'open_requests': GuestRequest.objects.filter(branch=branch).exclude(status='RESOLVED').count(),
    }


def _feedback(branch, start, end):
    from guests.models import Feedback
    lo, hi = _bounds(start, end)
    qs = Feedback.objects.filter(branch=branch, area=Feedback.Area.STAY, created_at__gte=lo, created_at__lt=hi)
    agg = qs.aggregate(n=Count('id'), avg=Avg('rating'))
    if not agg['n']:
        return None
    rec = list(qs.exclude(recommend__isnull=True).values_list('recommend', flat=True))
    nps = None
    if rec:
        nps = round((sum(1 for r in rec if r >= 9) - sum(1 for r in rec if r <= 6)) * 100 / len(rec))
    return {'count': agg['n'], 'avg': round(agg['avg'], 1), 'stars': int(round(agg['avg'])), 'nps': nps,
            'open': Feedback.objects.filter(branch=branch, area=Feedback.Area.STAY,
                                            status__in=[Feedback.Status.NEW, Feedback.Status.IN_PROGRESS]).count()}


def _service(branch, start, end):
    lo, hi = _bounds(start, end)
    reqs = list(GuestRequest.objects.filter(branch=branch, created_at__gte=lo, created_at__lt=hi).exclude(kind='COMPLIMENT'))
    done = [r for r in reqs if r.status == 'RESOLVED']
    return {'requests': len(reqs), 'complaints': sum(1 for r in reqs if r.kind == 'COMPLAINT'),
            'on_time': round(sum(1 for r in done if not r.is_breached) * 100 / len(done)) if done else None}


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.datetime.combine(start, datetime.time.min), tz),
            timezone.make_aware(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min), tz))


def _insights(cur, prev, deltas, outlook, other):
    out = []
    rows = [r for r in cur['rows'] if r['sellable']]
    if deltas['occupancy'] is not None and abs(deltas['occupancy']) >= 3:
        up = deltas['occupancy'] > 0
        out.append({'tone': 'green' if up else 'orange', 'icon': 'fa-bed',
                    'text': f"Occupancy {'up' if up else 'down'} {abs(deltas['occupancy']):g} points on the previous period "
                            f"({prev['occupancy']:g}% → {cur['occupancy']:g}%)."})
    if deltas['adr'] is not None and abs(deltas['adr']) >= 5:
        up = deltas['adr'] > 0
        out.append({'tone': 'green' if up else 'orange', 'icon': 'fa-tag',
                    'text': f"Average room rate {'rose' if up else 'fell'} {abs(deltas['adr']):g}% to ₹{cur['adr']:,.0f}."})
    if len(rows) > 1:
        best = max(rows, key=lambda r: (r['occupancy'], r['revenue']))
        if best['sold']:
            out.append({'tone': 'indigo', 'icon': 'fa-star',
                        'text': f"Best night: {best['date'].strftime('%a %d %b')} at {min(100, best['occupancy']):g}% "
                                f"({best['sold']} rooms, ₹{best['revenue']:,.0f})."})
    if cur['sources'] and cur['arrivals']:
        top = cur['sources'][0]
        share = round(top['bookings'] * 100 / cur['arrivals'])
        if share >= 40 and len(cur['sources']) > 1:
            out.append({'tone': 'blue', 'icon': 'fa-route',
                        'text': f"{top['source']} brought {share}% of arrivals — worth checking you are not over-reliant on one source."})
    commission = sum((c['commission'] for c in cur['channels']), ZERO)
    if commission > 0:
        out.append({'tone': 'orange', 'icon': 'fa-percent',
                    'text': f"₹{commission:,.0f} paid in OTA commission; direct bookings would have kept it."})
    if cur['cancel_rate'] >= 15 and cur['bookings_made'] >= 5:
        out.append({'tone': 'red', 'icon': 'fa-ban',
                    'text': f"{cur['cancel_rate']:g}% of bookings made in this period were cancelled."})
    if cur['no_shows']:
        out.append({'tone': 'orange', 'icon': 'fa-user-xmark',
                    'text': f"{cur['no_shows']} no-show{'s' if cur['no_shows'] != 1 else ''} — consider a deposit on arrival day."})
    if other['banquets'] > 0:
        out.append({'tone': 'purple', 'icon': 'fa-champagne-glasses',
                    'text': f"Banquets added ₹{other['banquets']:,.0f} from {other['banquet_count']} event{'s' if other['banquet_count'] != 1 else ''}."})
    soft = [r for r in outlook if r['sellable'] and r['otb_pct'] < 30]
    hot = [r for r in outlook if r['sellable'] and r['otb_pct'] >= 85]
    if hot:
        out.append({'tone': 'green', 'icon': 'fa-fire',
                    'text': f"{len(hot)} of the next {len(outlook)} nights are 85%+ booked — room to raise rates "
                            f"(first: {hot[0]['date'].strftime('%a %d %b')})."})
    if soft and len(soft) >= len(outlook) // 2:
        out.append({'tone': 'orange', 'icon': 'fa-chart-line',
                    'text': f"{len(soft)} of the next {len(outlook)} nights are under 30% booked — a package or promo could fill them."})
    return out[:6]


def compute(branch, start, end, levels=None):
    cur = reports.summary(branch, start, end)
    p_start, p_end = previous_period(start, end)
    prev = reports.summary(branch, p_start, p_end)
    other = _other_revenue(branch, start, end)
    prev_other = _other_revenue(branch, p_start, p_end)
    total = cur['total_revenue'] + other['banquets'] + other['spa']
    prev_total = prev['total_revenue'] + prev_other['banquets'] + prev_other['spa']
    deltas = {
        'occupancy': round(cur['occupancy'] - prev['occupancy'], 1) if prev['sellable'] else None,  # points
        'adr': pct_change(cur['adr'], prev['adr']),
        'revpar': pct_change(cur['revpar'], prev['revpar']),
        'room_revenue': pct_change(cur['room_revenue'], prev['room_revenue']),
        'total': pct_change(total, prev_total),
        'arrivals': pct_change(cur['arrivals'], prev['arrivals']),
        'collected': pct_change(cur['collected'], prev['collected']),
    }
    fc = ops.forecast(branch, days=14)
    outlook = fc['rows']
    revenue_mix = [x for x in [
        {'label': 'Rooms', 'amount': cur['room_revenue'], 'icon': 'fa-bed'},
        {'label': 'Folio extras', 'amount': cur['extras_revenue'], 'icon': 'fa-receipt'},
        {'label': 'Banquets', 'amount': other['banquets'], 'icon': 'fa-champagne-glasses'},
        {'label': 'Spa (paid directly)', 'amount': other['spa'], 'icon': 'fa-spa'},
    ] if x['amount'] > 0]
    for x in revenue_mix:
        x['share'] = round(float(x['amount']) * 100 / float(total)) if total else 0
    top_src = max([x['bookings'] for x in cur['sources']] or [0])
    for x in cur['sources']:
        x['share'] = round(x['bookings'] * 100 / top_src) if top_src else 0
    top_pay = max([v for _, v in cur['payments']] or [ZERO])
    payments = [{'label': k, 'amount': v, 'share': round(float(v) * 100 / float(top_pay)) if top_pay else 0}
                for k, v in cur['payments']]
    attention = ops.action_items(branch, levels or {})
    single = start == end
    chart = {
        'labels': [r['date'].strftime('%d %b') for r in cur['rows']],
        'occupancy': [r['occupancy'] for r in cur['rows']],
        'prev_occupancy': [r['occupancy'] for r in prev['rows']],
        'revenue': [float(r['revenue']) for r in cur['rows']],
        'forecast': [r['forecast'] for r in cur['rows']],
        'outlook_labels': [r['date'].strftime('%a %d') for r in outlook],
        'outlook_otb': [r['otb_pct'] for r in outlook],
        'outlook_pickup': [max(0, r['pct'] - r['otb_pct']) for r in outlook],
    }
    return {
        'start': start, 'end': end, 'prev_start': p_start, 'prev_end': p_end, 'single_day': single,
        'cur': cur, 'prev': prev, 'deltas': deltas, 'other': other, 'total_revenue': total,
        'revenue_mix': revenue_mix, 'payments': payments, 'outlook': outlook, 'forecast_avg': fc['avg_pct'],
        'feedback': _feedback(branch, start, end), 'service': _service(branch, start, end),
        'attention': attention[:6], 'attention_total': len(attention),
        'insights': _insights(cur, prev, deltas, outlook, other),
        'chart': chart,
    }
