"""Numbers behind the restaurant dashboard.

Everything is computed for a period (and the equally long period before it, for deltas) on
the orders that count as sales: not drafts, not cancelled, not refunded — the same rule the
Profit & Loss uses. "Live" figures are always about today, whatever period is selected.
All queries are scoped to a list of branches (one branch, or every branch of the business).
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Max, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone

ZERO = Decimal('0')
OPEN_STATUSES = ['P', 'PR', 'READY', 'S']
NOT_SALES = ['DRAFT', 'C']
MAX_DAYS = 400

PERIODS = [
    ('today', 'Today'),
    ('yesterday', 'Yesterday'),
    ('last_7', 'Last 7 days'),
    ('last_30', 'Last 30 days'),
    ('this_month', 'This month'),
    ('last_month', 'Last month'),
    ('this_year', 'This year'),
]
PAYMENT_LABELS = {
    'cash': 'Cash', 'upi': 'UPI', 'card': 'Card', 'credit': 'On credit', 'O': 'Online', 'C': 'Pay at counter',
    'points': 'Loyalty points', 'gift_card': 'Gift card', 'room': 'Charged to room',
}
CHANNEL_META = {
    'dine_in': ('Dine-in', 'fa-utensils'),
    'takeaway': ('Takeaway', 'fa-bag-shopping'),
    'delivery': ('Own delivery', 'fa-motorcycle'),
}
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _d(v):
    return Decimal(v or 0)


def pct_change(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if not prev:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def share(part, whole):
    return round(float(part) / float(whole) * 100, 1) if whole else 0


def fmt_pct(v):
    """12.0 -> '12', 12.5 -> '12.5'."""
    return f'{v:g}'


def hour_label(h):
    return f'{h % 12 or 12} {"am" if h < 12 else "pm"}'


def resolve_period(key, start_str='', end_str='', today=None):
    """(key, label, start, end) — custom dates fall back to this month when invalid."""
    today = today or timezone.localdate()
    if key == 'custom':
        try:
            s = datetime.strptime(start_str, '%Y-%m-%d').date()
            e = datetime.strptime(end_str, '%Y-%m-%d').date()
            if s > e:
                s, e = e, s
            e = min(e, today)
            s = max(s, e - timedelta(days=MAX_DAYS))
            return 'custom', f'{s:%d %b} – {e:%d %b %Y}', s, e
        except ValueError:
            key = 'this_month'
    first = today.replace(day=1)
    ranges = {
        'today': (today, today),
        'yesterday': (today - timedelta(days=1), today - timedelta(days=1)),
        'last_7': (today - timedelta(days=6), today),
        'last_30': (today - timedelta(days=29), today),
        'this_month': (first, today),
        'last_month': ((first - timedelta(days=1)).replace(day=1), first - timedelta(days=1)),
        'this_year': (today.replace(month=1, day=1), today),
    }
    if key not in ranges:
        key = 'today'
    s, e = ranges[key]
    return key, dict(PERIODS)[key], s, e


def previous_period(start, end):
    length = (end - start).days + 1
    p_end = start - timedelta(days=1)
    return p_end - timedelta(days=length - 1), p_end


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))


def _orders(restaurant, branches):
    from core.models import Order
    qs = Order.objects.filter(restaurant=restaurant)
    return qs.filter(branch__in=branches) if branches is not None else qs


def sales_orders(restaurant, branches, start, end, until=None):
    """Orders that count as sales in [start, end]; ``until`` cuts the last day off at a time of day."""
    s_dt, e_dt = _bounds(start, end)
    if until is not None:
        e_dt = min(e_dt, until)
    return (_orders(restaurant, branches).filter(order_time__gte=s_dt, order_time__lt=e_dt)
            .exclude(status__in=NOT_SALES).exclude(payment_status='refunded'))


def _totals(qs):
    a = qs.aggregate(n=Count('id'), gross=Sum('total_amount'), tax=Sum('tax_amount'), disc=Sum('discount_amount'),
                     guests=Sum('guest_count'), paid=Sum('paid_amount'), due=Sum('due_amount'), subtotal=Sum('subtotal'))
    n = a['n'] or 0
    gross = _d(a['gross'])
    guests = a['guests'] or 0
    return {
        'orders': n, 'gross': gross, 'tax': _d(a['tax']), 'net': gross - _d(a['tax']),
        'discounts': _d(a['disc']), 'guests': guests, 'collected': _d(a['paid']), 'due': _d(a['due']),
        'aov': (gross / n) if n else ZERO, 'per_guest': (gross / guests) if guests else ZERO,
        'discount_rate': share(_d(a['disc']), gross + _d(a['disc'])),
    }


def _series(qs, start, end):
    """Sales per hour (single day) or per day, as a list aligned to the period."""
    tz = timezone.get_current_timezone()
    if start == end:
        rows = qs.annotate(k=ExtractHour('order_time', tzinfo=tz)).values('k').annotate(v=Sum('total_amount'), n=Count('id'))
        found = {r['k']: r for r in rows}
        return [{'label': hour_label(h), 'sales': float(_d(found.get(h, {}).get('v'))), 'orders': found.get(h, {}).get('n', 0)}
                for h in range(24)]
    rows = qs.annotate(k=TruncDate('order_time', tzinfo=tz)).values('k').annotate(v=Sum('total_amount'), n=Count('id'))
    found = {r['k']: r for r in rows}
    out, day = [], start
    fmt = '%d %b' if (end - start).days < 120 else '%b %y'
    while day <= end:
        r = found.get(day, {})
        out.append({'label': day.strftime(fmt), 'date': day.isoformat(), 'sales': float(_d(r.get('v'))), 'orders': r.get('n', 0)})
        day += timedelta(days=1)
    return out


def _channels(qs, total):
    from integrations.models import ExternalOrderLink
    partner = {r['source']: r for r in ExternalOrderLink.objects.filter(order__in=qs).values('source')
               .annotate(n=Count('id'), v=Sum('order__total_amount'), commission=Sum('commission_amount'))}
    own = qs.exclude(external_link__isnull=False).values('order_type').annotate(n=Count('id'), v=Sum('total_amount'))
    rows = []
    for r in own:
        label, icon = CHANNEL_META.get(r['order_type'] or 'dine_in', ((r['order_type'] or 'Other').title(), 'fa-receipt'))
        rows.append({'key': r['order_type'], 'label': label, 'icon': icon, 'orders': r['n'], 'sales': _d(r['v']), 'commission': ZERO})
    for source, r in partner.items():
        rows.append({'key': source, 'label': source.replace('_', ' ').title(), 'icon': 'fa-plug', 'orders': r['n'],
                     'sales': _d(r['v']), 'commission': _d(r['commission']), 'partner': True})
    for r in rows:
        r['share'] = share(r['sales'], total)
        r['aov'] = r['sales'] / r['orders'] if r['orders'] else ZERO
    return sorted(rows, key=lambda r: r['sales'], reverse=True)


def _payments(qs, total):
    sums, counts = defaultdict(Decimal), defaultdict(int)
    for method, payments, amount in qs.values_list('payment_method', 'payments', 'total_amount'):
        entries = [p for p in (payments or []) if isinstance(p, dict) and p.get('amount')]
        if entries:
            for p in entries:
                key = str(p.get('method') or method or 'cash')
                sums[key] += _d(str(p.get('amount')))
                counts[key] += 1
        else:
            key = method or 'cash'
            sums[key] += _d(amount)
            counts[key] += 1
    rows = [{'key': k, 'label': PAYMENT_LABELS.get(k, k.replace('_', ' ').title()), 'amount': v, 'count': counts[k]}
            for k, v in sums.items()]
    for r in rows:
        r['share'] = share(r['amount'], total)
    return sorted(rows, key=lambda r: r['amount'], reverse=True)


def _heatmap(qs):
    tz = timezone.get_current_timezone()
    rows = (qs.annotate(dow=ExtractIsoWeekDay('order_time', tzinfo=tz), hr=ExtractHour('order_time', tzinfo=tz))
            .values('dow', 'hr').annotate(n=Count('id'), v=Sum('total_amount')))
    cells = {(r['dow'], r['hr']): r for r in rows}
    if not cells:
        return None
    hours = [h for _, h in cells]
    lo, hi = min(hours), max(hours)
    peak = max(cells.values(), key=lambda r: r['n'])
    top = peak['n'] or 1
    grid = []
    for dow in range(1, 8):
        row = []
        for h in range(lo, hi + 1):
            c = cells.get((dow, h))
            n = c['n'] if c else 0
            row.append({'n': n, 'sales': float(_d(c['v'])) if c else 0, 'level': 0 if not n else min(4, 1 + int(n / top * 3.999)),
                        'title': f'{WEEKDAYS[dow - 1]} {hour_label(h)}: {n} orders'})
        grid.append({'day': WEEKDAYS[dow - 1], 'cells': row})
    by_hour = defaultdict(int)
    by_day = defaultdict(Decimal)
    for (dow, h), r in cells.items():
        by_hour[h] += r['n']
        by_day[dow] += _d(r['v'])
    peak_hour = max(by_hour, key=by_hour.get)
    best_day = max(by_day, key=by_day.get)
    return {
        'hours': [hour_label(h) for h in range(lo, hi + 1)], 'grid': grid,
        'peak': f'{WEEKDAYS[peak["dow"] - 1]} {hour_label(peak["hr"])}–{hour_label((peak["hr"] + 1) % 24)}',
        'peak_hour': f'{hour_label(peak_hour)}–{hour_label((peak_hour + 1) % 24)}',
        'best_day': WEEKDAYS[best_day - 1],
    }


def _menu(qs, restaurant, branches, total):
    from core.models import MenuItem, OrderItem
    line_value = F('quantity') * F('unit_price') - F('discount_amount')
    items = OrderItem.objects.filter(order__in=qs)
    top = list(items.values('menu_item_id', 'menu_item__name', 'menu_item__category__name')
               .annotate(qty=Sum('quantity'), sales=Sum(line_value), orders=Count('order', distinct=True)).order_by('-sales')[:10])
    for t in top:
        t['sales'] = _d(t['sales'])
        t['share'] = share(t['sales'], total)
    by_qty = list(items.values('menu_item__name').annotate(qty=Sum('quantity')).order_by('-qty')[:5])
    cats = list(items.values('menu_item__category__name').annotate(qty=Sum('quantity'), sales=Sum(line_value)).order_by('-sales')[:8])
    cat_total = sum((_d(c['sales']) for c in cats), ZERO)
    for c in cats:
        c['name'] = c['menu_item__category__name'] or 'Uncategorised'
        c['sales'] = _d(c['sales'])
        c['share'] = share(c['sales'], cat_total)
    sold_ids = items.values('menu_item_id')
    menu = MenuItem.objects.filter(restaurant=restaurant, availability=True)
    if branches is not None:
        menu = menu.filter(branch__in=branches)
    unsold_qs = menu.exclude(id__in=sold_ids)
    agg = items.aggregate(qty=Sum('quantity'), lines=Count('id'))
    unsold_count = unsold_qs.count()
    unsold = list(unsold_qs.order_by('name').values_list('name', flat=True)[:6])
    return {
        'top': top, 'by_qty': by_qty, 'categories': cats,
        'items_sold': agg['qty'] or 0, 'menu_size': menu.count(),
        'unsold_count': unsold_count, 'unsold': unsold, 'unsold_more': max(0, unsold_count - len(unsold)),
        'items_per_order': round((agg['qty'] or 0) / qs.count(), 1) if qs.exists() else 0,
    }


def _staff(qs):
    rows = list(qs.filter(cashier__isnull=False).values('cashier_id', 'cashier__first_name', 'cashier__last_name', 'cashier__username')
                .annotate(orders=Count('id'), sales=Sum('total_amount'), disc=Sum('discount_amount')).order_by('-sales')[:8])
    for r in rows:
        r['name'] = (f'{r["cashier__first_name"]} {r["cashier__last_name"]}'.strip() or r['cashier__username'])
        r['sales'] = _d(r['sales'])
        r['aov'] = r['sales'] / r['orders'] if r['orders'] else ZERO
        r['disc'] = _d(r['disc'])
        r['initials'] = ''.join(p[0] for p in r['name'].split()[:2]).upper() or '?'
    return rows


def _tables(qs):
    rows = list(qs.filter(table__isnull=False).values('table__name', 'table__zone__name', 'table__capacity')
                .annotate(orders=Count('id'), sales=Sum('total_amount'), guests=Sum('guest_count')).order_by('-sales')[:6])
    for r in rows:
        r['sales'] = _d(r['sales'])
    return rows


def _kitchen(qs):
    done = qs.filter(prep_start__isnull=False, ready_at__isnull=False, ready_at__gt=F('prep_start'))
    dur = ExpressionWrapper(F('ready_at') - F('prep_start'), output_field=DurationField())
    a = done.annotate(d=dur).aggregate(avg=Avg('d'), worst=Max('d'), n=Count('id'),
                                       slow=Count('id', filter=Q(d__gt=timedelta(minutes=20))))
    if not a['n']:
        return None
    return {'avg': round(a['avg'].total_seconds() / 60, 1), 'worst': round(a['worst'].total_seconds() / 60),
            'tracked': a['n'], 'slow': a['slow'], 'slow_pct': share(a['slow'], a['n'])}


def _guests(qs, restaurant, branches, s_dt):
    phones = set(p for p in qs.exclude(phone='').values_list('phone', flat=True) if p)
    returning = set()
    if phones:
        returning = set(_orders(restaurant, branches).filter(order_time__lt=s_dt, phone__in=phones)
                        .exclude(status__in=NOT_SALES).values_list('phone', flat=True).distinct())
    total_orders = qs.count()
    known_orders = qs.exclude(phone='').count()
    top = list(qs.exclude(phone='').values('phone').annotate(name=Max('customer_name'), visits=Count('id'), spend=Sum('total_amount'))
               .order_by('-spend')[:5])
    for t in top:
        t['spend'] = _d(t['spend'])
        t['returning'] = t['phone'] in returning
    return {
        'unique': len(phones), 'returning': len(returning), 'new': len(phones) - len(returning),
        'returning_share': share(len(returning), len(phones)), 'capture': share(known_orders, total_orders),
        'anonymous': total_orders - known_orders, 'top': top,
    }


def _feedback(restaurant, branches, start, end):
    from guests.models import Feedback
    from super_admin.modules import FEEDBACK_AREAS, RESTAURANT
    s_dt, e_dt = _bounds(start, end)
    qs = Feedback.objects.filter(restaurant=restaurant, area__in=FEEDBACK_AREAS[RESTAURANT], created_at__gte=s_dt, created_at__lt=e_dt)
    if branches is not None:
        qs = qs.filter(branch__in=branches)
    a = qs.aggregate(n=Count('id'), avg=Avg('rating'), food=Avg('food'), service=Avg('service'),
                     rec=Count('id', filter=Q(recommend__isnull=False)), pro=Count('id', filter=Q(recommend__gte=9)),
                     det=Count('id', filter=Q(recommend__lte=6)), low=Count('id', filter=Q(rating__lte=2)),
                     open=Count('id', filter=Q(status__in=['NEW', 'IN_PROGRESS'])))
    if not a['n']:
        return None
    return {
        'count': a['n'], 'avg': round(a['avg'], 1), 'stars': round(a['avg']), 'food': round(a['food'], 1) if a['food'] else None,
        'service': round(a['service'], 1) if a['service'] else None,
        'nps': round((a['pro'] - a['det']) / a['rec'] * 100) if a['rec'] else None,
        'low': a['low'], 'open': a['open'],
    }


def _cancellations(restaurant, branches, start, end):
    s_dt, e_dt = _bounds(start, end)
    base = _orders(restaurant, branches).filter(order_time__gte=s_dt, order_time__lt=e_dt)
    c = base.filter(status='C').aggregate(n=Count('id'), v=Sum('total_amount'))
    r = base.filter(payment_status='refunded').exclude(status__in=NOT_SALES).aggregate(n=Count('id'), v=Sum('total_amount'))
    reasons = list(base.exclude(status__in=NOT_SALES).exclude(discount_reason__isnull=True).exclude(discount_reason='')
                   .filter(discount_amount__gt=0).values('discount_reason').annotate(n=Count('id'), v=Sum('discount_amount'))
                   .order_by('-v')[:5])
    coupons = base.exclude(status__in=NOT_SALES).filter(coupon__isnull=False).count()
    return {'cancelled': c['n'] or 0, 'cancelled_value': _d(c['v']), 'refunded': r['n'] or 0, 'refunded_value': _d(r['v']),
            'discount_reasons': reasons, 'coupons': coupons}


def live(restaurant, branches, now=None):
    """Right-now operations — independent of the selected period."""
    from core.models import InventoryItem, Table
    from reservations.models import Reservation
    now = now or timezone.now()
    today = timezone.localdate(now)
    s_dt, _ = _bounds(today, today)
    orders = _orders(restaurant, branches)
    open_qs = orders.filter(status__in=OPEN_STATUSES, order_time__gte=now - timedelta(hours=18)).exclude(payment_status__in=['paid', 'refunded'])
    by_status = {r['status']: r['n'] for r in open_qs.values('status').annotate(n=Count('id'))}
    tables = Table.objects.filter(restaurant=restaurant, is_active=True)
    if branches is not None:
        tables = tables.filter(branch__in=branches)
    occupied = open_qs.filter(table__isnull=False).values('table').distinct().count()
    total_tables = tables.count()

    today_sales = sales_orders(restaurant, branches, today, today).aggregate(v=Sum('total_amount'), n=Count('id'))
    last_week = today - timedelta(days=7)
    same_time = now - timedelta(days=7)
    lw = sales_orders(restaurant, branches, last_week, last_week, until=same_time).aggregate(v=Sum('total_amount'), n=Count('id'))

    res = Reservation.objects.filter(restaurant=restaurant, date=today).exclude(status__in=['X', 'N'])
    if branches is not None:
        res = res.filter(branch__in=branches)
    upcoming = res.filter(start_time__gte=timezone.localtime(now).time()).order_by('start_time')

    stock = InventoryItem.objects.filter(restaurant=restaurant, is_active=True, quantity__lte=F('reorder_level'))
    if branches is not None:
        stock = stock.filter(branch__in=branches)

    late_cutoff = now - timedelta(minutes=25)
    return {
        'pending': by_status.get('P', 0), 'preparing': by_status.get('PR', 0), 'ready': by_status.get('READY', 0),
        'served_unpaid': by_status.get('S', 0), 'open_total': sum(by_status.values()),
        'late': open_qs.filter(status__in=['P', 'PR'], order_time__lt=late_cutoff).count(),
        'tables_occupied': occupied, 'tables_total': total_tables, 'occupancy': share(occupied, total_tables),
        'today_sales': _d(today_sales['v']), 'today_orders': today_sales['n'] or 0,
        'last_week_sales': _d(lw['v']), 'vs_last_week': pct_change(today_sales['v'], lw['v']),
        'open_due': _d(open_qs.aggregate(v=Sum('total_amount'))['v']) - _d(open_qs.aggregate(v=Sum('paid_amount'))['v']),
        'reservations': res.count(), 'covers': res.aggregate(c=Sum('party_size'))['c'] or 0,
        'next_reservations': list(upcoming[:4]),
        'low_stock_count': stock.count(),
        'low_stock': list(stock.order_by('quantity')[:5]),
        'since': s_dt,
    }


def _insights(cur, prev, deltas, menu, heat, channels, cancels, guests, kitchen, feedback):
    out = []
    if deltas['sales'] is not None and abs(deltas['sales']) >= 5:
        up = deltas['sales'] > 0
        out.append({'tone': 'good' if up else 'warn', 'icon': 'fa-arrow-trend-up' if up else 'fa-arrow-trend-down',
                    'text': f'Sales are {"up" if up else "down"} {abs(deltas["sales"]):.0f}% on the previous period.'})
    if deltas['aov'] is not None and abs(deltas['aov']) >= 8:
        up = deltas['aov'] > 0
        out.append({'tone': 'good' if up else 'warn', 'icon': 'fa-receipt',
                    'text': f'Average bill {"rose" if up else "fell"} {abs(deltas["aov"]):.0f}% to ₹{cur["aov"]:,.0f}.'})
    if heat:
        out.append({'tone': 'info', 'icon': 'fa-clock', 'text': f'Busiest slot was {heat["peak"]}; {heat["best_day"]} is your strongest day.'})
    if menu['top'] and menu['top'][0]['share'] >= 10:
        t = menu['top'][0]
        out.append({'tone': 'info', 'icon': 'fa-star', 'text': f'{t["menu_item__name"]} alone brought in {fmt_pct(t["share"])}% of sales.'})
    if cur['discount_rate'] >= 8:
        out.append({'tone': 'warn', 'icon': 'fa-tags', 'text': f'Discounts were {fmt_pct(cur["discount_rate"])}% of the bill value — most restaurants keep this under 5%.'})
    partner = [c for c in channels if c.get('partner')]
    if partner:
        p_share = round(sum(c['share'] for c in partner), 1)
        if p_share >= 30:
            out.append({'tone': 'warn', 'icon': 'fa-plug', 'text': f'Aggregators made up {fmt_pct(p_share)}% of sales; their commission is on the P&L.'})
    if cancels['cancelled'] and cur['orders'] and share(cancels['cancelled'], cur['orders'] + cancels['cancelled']) >= 5:
        out.append({'tone': 'warn', 'icon': 'fa-ban', 'text': f'{cancels["cancelled"]} order{"s" if cancels["cancelled"] != 1 else ""} worth ₹{cancels["cancelled_value"]:,.0f} were cancelled.'})
    if guests['unique'] >= 5:
        out.append({'tone': 'good' if guests['returning_share'] >= 30 else 'info', 'icon': 'fa-user-check',
                    'text': f'{fmt_pct(guests["returning_share"])}% of identified guests came back after an earlier visit.'})
    if guests['capture'] < 40 and cur['orders'] >= 10:
        out.append({'tone': 'info', 'icon': 'fa-address-book', 'text': f'Only {fmt_pct(guests["capture"])}% of bills have a phone number — capture more to grow repeat visits.'})
    if kitchen and kitchen['slow_pct'] >= 15:
        out.append({'tone': 'warn', 'icon': 'fa-fire-burner', 'text': f'{fmt_pct(kitchen["slow_pct"])}% of tickets took over 20 minutes in the kitchen.'})
    if feedback and feedback['low']:
        out.append({'tone': 'warn', 'icon': 'fa-face-frown', 'text': f'{feedback["low"]} guest{"s" if feedback["low"] != 1 else ""} rated their visit 2 stars or lower.'})
    if menu['unsold_count'] and menu['menu_size'] and share(menu['unsold_count'], menu['menu_size']) >= 20:
        out.append({'tone': 'info', 'icon': 'fa-utensils', 'text': f'{menu["unsold_count"]} of {menu["menu_size"]} dishes did not sell — consider trimming the menu.'})
    return out[:6]


def branch_comparison(restaurant, branches, start, end):
    rows = []
    for b in branches:
        t = _totals(sales_orders(restaurant, [b], start, end))
        rows.append({'branch': b, **t})
    total = sum((r['gross'] for r in rows), ZERO)
    for r in rows:
        r['share'] = share(r['gross'], total)
    return sorted(rows, key=lambda r: r['gross'], reverse=True)


def compute(restaurant, branches, start, end):
    """Full dashboard for [start, end] on ``branches`` (a list, or None for every branch)."""
    qs = sales_orders(restaurant, branches, start, end)
    p_start, p_end = previous_period(start, end)
    today = timezone.localdate()
    # A period ending today is compared with the previous one up to the same time of day
    until = None
    if end == today:
        until = timezone.now() - timedelta(days=(end - start).days + 1)
    prev_qs = sales_orders(restaurant, branches, p_start, p_end, until=until)

    cur, prev = _totals(qs), _totals(prev_qs)
    cancels = _cancellations(restaurant, branches, start, end)
    menu = _menu(qs, restaurant, branches, cur['gross'])
    deltas = {
        'sales': pct_change(cur['gross'], prev['gross']), 'orders': pct_change(cur['orders'], prev['orders']),
        'aov': pct_change(cur['aov'], prev['aov']), 'guests': pct_change(cur['guests'], prev['guests']),
        'discounts': pct_change(cur['discounts'], prev['discounts']), 'net': pct_change(cur['net'], prev['net']),
    }
    series = _series(qs, start, end)
    prev_series = _series(prev_qs, p_start, p_end)
    s_dt, _ = _bounds(start, end)
    heat = _heatmap(qs)
    channels = _channels(qs, cur['gross'])
    guests = _guests(qs, restaurant, branches, s_dt)
    kitchen = _kitchen(qs)
    feedback = _feedback(restaurant, branches, start, end)
    return {
        'start': start, 'end': end, 'prev_start': p_start, 'prev_end': p_end, 'single_day': start == end,
        'cur': cur, 'prev': prev, 'deltas': deltas,
        'series': series, 'prev_series': prev_series,
        'best': max(series, key=lambda s: s['sales']) if cur['orders'] else None,
        'channels': channels, 'payments': _payments(qs, cur['gross']),
        'heat': heat, 'menu': menu, 'staff': _staff(qs), 'tables': _tables(qs),
        'kitchen': kitchen, 'guests': guests, 'feedback': feedback, 'cancels': cancels,
        'insights': _insights(cur, prev, deltas, menu, heat, channels, cancels, guests, kitchen, feedback),
        'recent': list(_orders(restaurant, branches).exclude(status='DRAFT').select_related('table', 'branch').order_by('-order_time')[:8]),
    }
