"""Profit & loss from what the system already records.

Revenue is net of tax: dining (orders), rooms (checked-out stays), room extras posted to hotel
folios (laundry, spa, minibar… — restaurant bills charged to a room are already in dining) and events (paid tickets).
Costs: partner commission (Integrations), purchases (net of input GST), payroll and
recorded expenses (net of GST). Every figure can be narrowed to one branch.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

ZERO = Decimal('0')


def _d(v):
    return Decimal(v or 0)


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))


def _months(start, end):
    y, m = start.year, start.month
    out = []
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def compute_pnl(restaurant, start, end, branch=None, module='all'):
    """``module`` = 'restaurant' (dining, events, purchases), 'hotel' (rooms) or 'all' (consolidated, incl. shared costs)."""
    from super_admin.modules import PAYROLL_DEPARTMENTS
    fb, rooms_on = module in ('all', 'restaurant'), module in ('all', 'hotel')
    from core.models import Order, PurchaseOrder, PurchaseReturn
    from events.models import EventBooking
    from finance.models import Expense
    from hotel.models import RoomBooking
    from integrations.models import ExternalBookingLink, ExternalOrderLink
    from restaurant_admin.models import MonthlyPayroll

    s_dt, e_dt = _bounds(start, end)

    def by_branch(qs, field='branch'):
        return qs.filter(**{field: branch}) if branch is not None else qs

    orders = by_branch(Order.objects.filter(restaurant=restaurant, order_time__gte=s_dt, order_time__lt=e_dt)
                       .exclude(status__in=['DRAFT', 'C']).exclude(payment_status='refunded'))
    o = orders.aggregate(n=Count('id'), gross=Sum('total_amount'), tax=Sum('tax_amount'), disc=Sum('discount_amount'))
    dining = _d(o['gross']) - _d(o['tax']) if fb else ZERO

    stays = by_branch(RoomBooking.objects.filter(branch__restaurant=restaurant, booking_status='CHECKED_OUT',
                                                 check_out_date__gte=start, check_out_date__lte=end))
    st = stays.aggregate(n=Count('id'), net=Sum('total_room_charges'))
    rooms = _d(st['net']) if rooms_on else ZERO

    from hotel.models import FolioCharge
    extras_qs = by_branch(FolioCharge.objects.filter(booking__branch__restaurant=restaurant, is_void=False, source_order__isnull=True,
                                                     service_date__gte=start, service_date__lte=end), 'booking__branch')
    ex = extras_qs.aggregate(n=Count('id'), net=Sum('amount'))
    room_extras = _d(ex['net']) if rooms_on else ZERO

    tickets = by_branch(EventBooking.objects.filter(event__branch__restaurant=restaurant, payment_status='SUCCESS',
                                                    created_at__gte=s_dt, created_at__lt=e_dt), 'event__branch')
    tk = tickets.aggregate(n=Count('id'), gross=Sum('total_amount'), tax=Sum('tax_amount'))
    events = _d(tk['gross']) - _d(tk['tax']) if fb else ZERO

    # Hotel banquets (completed events, before GST) and spa treatments paid directly (room-charged
    # spa is already in room extras through the folio).
    from hotel.models import BanquetEvent, SpaAppointment
    banquet_qs = by_branch(BanquetEvent.objects.filter(branch__restaurant=restaurant, status='COMPLETED', date__gte=start, date__lte=end)
                           .select_related('venue', 'menu'))
    banquet_list = list(banquet_qs) if rooms_on else []
    banquets = sum((e.subtotal for e in banquet_list), ZERO)
    spa_qs = by_branch(SpaAppointment.objects.filter(branch__restaurant=restaurant, status='COMPLETED', settled_by__in=['CASH', 'CARD', 'UPI'],
                                                     date__gte=start, date__lte=end))
    sp = spa_qs.aggregate(n=Count('id'), net=Sum('price'))
    spa_direct = _d(sp['net']) if rooms_on else ZERO

    revenue = dining + rooms + room_extras + events + banquets + spa_direct

    commission_orders = ZERO if not fb else _d(by_branch(ExternalOrderLink.objects.filter(order__in=orders)).aggregate(s=Sum('commission_amount'))['s'])
    commission_rooms = ZERO if not rooms_on else _d(by_branch(ExternalBookingLink.objects.filter(booking__in=stays)).aggregate(s=Sum('commission_amount'))['s'])
    commission = commission_orders + commission_rooms

    purchases_qs = by_branch(PurchaseOrder.objects.filter(restaurant=restaurant, purchase_date__gte=start, purchase_date__lte=end))
    pu = purchases_qs.aggregate(n=Count('id'), net=Sum('subtotal'), freight=Sum('freight_amount'))
    # Freight is part of landed cost; debit notes (goods sent back) come off purchases.
    returned = _d(by_branch(PurchaseReturn.objects.filter(restaurant=restaurant, return_date__gte=start, return_date__lte=end))
                  .aggregate(s=Sum('subtotal'))['s'])
    purchases = _d(pu['net']) + _d(pu['freight']) - returned if fb else ZERO

    month_q = Q()
    for y, m in _months(start, end):
        month_q |= Q(year=y, month=m)
    payroll_qs = by_branch(MonthlyPayroll.objects.filter(restaurant=restaurant).filter(month_q))
    if module in PAYROLL_DEPARTMENTS:
        payroll_qs = payroll_qs.filter(staff_member__department__in=PAYROLL_DEPARTMENTS[module])
    pr = payroll_qs.aggregate(n=Count('id'), net=Sum('net_salary'), drafts=Count('id', filter=Q(payment_status='DRAFT')))
    payroll = _d(pr['net'])

    expenses_qs = by_branch(Expense.objects.filter(restaurant=restaurant, date__gte=start, date__lte=end))
    if module != 'all':
        expenses_qs = expenses_qs.filter(department=module)
    exp_rows = list(expenses_qs.values('category__name', 'category__icon').annotate(total=Sum('amount'), tax=Sum('tax_amount'), n=Count('id'))
                    .order_by('-total'))
    expense_lines = [{'name': r['category__name'], 'icon': r['category__icon'], 'amount': _d(r['total']) - _d(r['tax']), 'count': r['n']} for r in exp_rows]
    expenses = sum((l['amount'] for l in expense_lines), ZERO)

    gross_profit = revenue - commission - purchases
    operating = payroll + expenses
    net_profit = gross_profit - operating

    def pct(part):
        return round(float(part / revenue * 100), 1) if revenue else None

    return {
        'start': start, 'end': end, 'branch': branch,
        'module': module,
        'revenue_lines': [line for line in [
            {'key': 'dining', 'name': 'Dining & delivery', 'amount': dining, 'detail': f'{o["n"] or 0} orders', 'icon': 'fa-utensils', 'on': fb},
            {'key': 'rooms', 'name': 'Room stays', 'amount': rooms, 'detail': f'{st["n"] or 0} checked-out stays', 'icon': 'fa-bed', 'on': rooms_on},
            {'key': 'room_extras', 'name': 'Room extras', 'amount': room_extras, 'detail': f'{ex["n"] or 0} folio charges', 'icon': 'fa-bell-concierge', 'on': rooms_on and bool(ex['n'])},
            {'key': 'events', 'name': 'Events & tickets', 'amount': events, 'detail': f'{tk["n"] or 0} bookings', 'icon': 'fa-ticket', 'on': fb},
            {'key': 'banquets', 'name': 'Banquets & events', 'amount': banquets, 'detail': f'{len(banquet_list)} completed events', 'icon': 'fa-champagne-glasses', 'on': rooms_on and bool(banquet_list)},
            {'key': 'spa', 'name': 'Spa (paid directly)', 'amount': spa_direct, 'detail': f'{sp["n"] or 0} treatments', 'icon': 'fa-spa', 'on': rooms_on and bool(sp['n'])},
        ] if line['on']],
        'revenue': revenue, 'discounts': _d(o['disc']),
        'commission': commission, 'purchases': purchases, 'purchases_count': pu['n'] or 0, 'cost_of_sales': commission + purchases,
        'gross_profit': gross_profit, 'gross_margin': pct(gross_profit),
        'payroll': payroll, 'payroll_count': pr['n'] or 0, 'payroll_drafts': pr['drafts'] or 0,
        'expense_lines': expense_lines, 'expenses': expenses,
        'operating': operating, 'net_profit': net_profit, 'net_margin': pct(net_profit),
        'cost_share': {
            'purchases': pct(purchases), 'payroll': pct(payroll), 'expenses': pct(expenses), 'commission': pct(commission),
        },
    }


def previous_period(start, end):
    length = (end - start).days + 1
    p_end = start - timedelta(days=1)
    return p_end - timedelta(days=length - 1), p_end


def month_range(d):
    start = d.replace(day=1)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    return start, nxt - timedelta(days=1)


def trend(restaurant, months=6, branch=None, today=None, module='all'):
    today = today or timezone.localdate()
    out = []
    cursor = today.replace(day=1)
    starts = []
    for _ in range(months):
        starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    for s in reversed(starts):
        s, e = month_range(s)
        e = min(e, today)
        p = compute_pnl(restaurant, s, e, branch, module)
        out.append({'label': s.strftime('%b %y'), 'revenue': float(p['revenue']),
                    'costs': float(p['commission'] + p['purchases'] + p['operating']), 'profit': float(p['net_profit'])})
    return out


def periods(today=None):
    """Named periods for the selector. Indian financial year runs April–March."""
    today = today or timezone.localdate()
    this_m = month_range(today)
    last_m = month_range(this_m[0] - timedelta(days=1))
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    q_start = date(today.year, q_start_month, 1)
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    return {
        'this_month': ('This month', this_m[0], today),
        'last_month': ('Last month', last_m[0], last_m[1]),
        'this_quarter': ('This quarter', q_start, today),
        'last_30': ('Last 30 days', today - timedelta(days=29), today),
        'this_fy': (f'FY {fy_start.year}-{str(fy_start.year + 1)[2:]}', fy_start, today),
    }
