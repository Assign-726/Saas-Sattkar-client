"""Kitchen & floor controls: wastage, stock counts, menu engineering (food cost) and the walk-in waitlist."""
import csv
import datetime
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import (InventoryItem, InventoryTransaction, MenuItem, MenuItemIngredient, Order, OrderItem, StockCount,
                         StockCountLine, Table)
from reservations.models import WaitlistEntry
from restaurant_admin import dashboard_metrics as M
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant

SHELL = 'guests/shell_restaurant.html'
ZERO = Decimal('0')
CENT = Decimal('0.01')


def _env(request, slug):
    restaurant = resolve_restaurant(request, slug)
    if isinstance(restaurant, HttpResponse):
        return None, restaurant
    return {'restaurant': restaurant, 'branch': get_active_branch(request, restaurant),
            'base_url': reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': restaurant.slug})}, None


def _render(request, env, partial, menu, title, **ctx):
    return render(request, SHELL, {**ctx, 'restaurant': env['restaurant'], 'current_branch': env['branch'], 'base_url': env['base_url'],
                                   'partial': partial, 'active_menu': menu, 'page_title': title})


def _items(env):
    qs = InventoryItem.objects.filter(restaurant=env['restaurant'])
    return qs.filter(branch=env['branch']) if env['branch'] else qs


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _period(request):
    return M.resolve_period(request.GET.get('period') or 'last_30', request.GET.get('from', ''), request.GET.get('to', ''))


def _csv(name, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(header)
    w.writerows(rows)
    return resp


# ============================================================ wastage

@login_required
@user_passes_test(is_admin)
def inventory_wastage(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    key, label, start, end = _period(request)
    s_dt, e_dt = M._bounds(start, end)
    logs = (InventoryTransaction.objects.filter(transaction_type='W', inventory_item__in=_items(env), created_at__gte=s_dt, created_at__lt=e_dt)
            .select_related('inventory_item', 'performed_by').order_by('-created_at'))
    reasons = dict(InventoryTransaction.WastageReason.choices)
    by_reason, by_item = defaultdict(lambda: ZERO), defaultdict(lambda: [0, ZERO])
    total = ZERO
    for t in logs:
        value = Decimal(-t.delta) * (t.unit_cost if t.unit_cost is not None else t.inventory_item.unit_cost)
        total += value
        by_reason[reasons.get(t.reason, 'Not given')] += value
        row = by_item[(t.inventory_item.name, t.inventory_item.unit or '')]
        row[0] += -t.delta
        row[1] += value
    if request.GET.get('export') == 'csv':
        return _csv(f'wastage-{start}-{end}.csv', ['When', 'Item', 'Quantity', 'Unit', 'Reason', 'Value', 'Note', 'By'],
                    [[timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'), t.inventory_item.name, -t.delta, t.inventory_item.unit,
                      reasons.get(t.reason, ''), (Decimal(-t.delta) * (t.unit_cost or t.inventory_item.unit_cost)).quantize(CENT), t.note,
                      t.performed_by.username if t.performed_by else ''] for t in logs])
    purchases = (InventoryTransaction.objects.filter(transaction_type__in=['P', 'R'], inventory_item__in=_items(env),
                                                     created_at__gte=s_dt, created_at__lt=e_dt).select_related('inventory_item'))
    bought = sum((Decimal(t.delta) * t.inventory_item.unit_cost for t in purchases if t.delta > 0), ZERO)
    top = max(by_reason.values() or [ZERO])
    page = Paginator(logs, 50).get_page(request.GET.get('page'))
    return _render(request, env, 'controls/_wastage.html', 'inventory_wastage', 'Wastage',
                   period=key, label=label, start=start, end=end, periods=M.PERIODS, page=page, total=total.quantize(CENT),
                   count=logs.count(), reasons=InventoryTransaction.WastageReason.choices,
                   by_reason=[(k, v, int(v / top * 100) if top else 0) for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])],
                   by_item=sorted(((n, u, q, v) for (n, u), (q, v) in by_item.items()), key=lambda r: -r[3])[:10],
                   share_of_purchases=round(float(total) / float(bought) * 100, 1) if bought else None,
                   items=_items(env).filter(is_active=True).order_by('name'))


@login_required
@user_passes_test(is_admin)
@require_POST
def inventory_wastage_add(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    url = reverse('restaurant_admin:inventory_wastage', kwargs={'restaurant_slug': env['restaurant'].slug})
    item = _items(env).filter(pk=_int(request.POST.get('item'), 0)).first()
    qty = _int(request.POST.get('quantity'), 0)
    reason = request.POST.get('reason') if request.POST.get('reason') in InventoryTransaction.WastageReason.values else ''
    if not item or qty <= 0:
        messages.error(request, 'Choose the item and a quantity of at least 1.')
        return redirect(url)
    if not reason:
        messages.error(request, 'Pick why it was wasted — it’s what makes the report useful.')
        return redirect(url)
    with transaction.atomic():
        item = InventoryItem.objects.select_for_update().get(pk=item.pk)
        short = qty > (item.quantity or 0)
        item.adjust(-qty)
        InventoryTransaction.objects.create(inventory_item=item, transaction_type='W', delta=-qty, reason=reason,
                                            note=(request.POST.get('note') or '').strip()[:255], unit_cost=item.unit_cost,
                                            performed_by=request.user)
    value = (Decimal(qty) * item.unit_cost).quantize(CENT)
    messages.success(request, f'Logged {qty} {item.unit or ""} {item.name} (₹{value}).'
                     + (' Stock went below zero — run a stock count to fix the books.' if short else ''))
    return redirect(url)


# ============================================================ stock counts

@login_required
@user_passes_test(is_admin)
def inventory_counts(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    counts = StockCount.objects.filter(restaurant=env['restaurant'])
    if env['branch']:
        counts = counts.filter(branch=env['branch'])
    rows = []
    for c in counts[:50]:
        lines = list(c.lines.all())
        counted = [l for l in lines if l.counted is not None]
        rows.append({'c': c, 'items': len(lines), 'counted': len(counted),
                     'value': sum((l.variance_value for l in counted), ZERO)})
    from core.models import InventoryCategory
    cats = InventoryCategory.objects.filter(restaurant=env['restaurant'], parent__isnull=True).order_by('name')
    return _render(request, env, 'controls/_counts.html', 'inventory_counts', 'Stock counts', rows=rows, categories=cats,
                   open_draft=counts.filter(status='DRAFT').first())


@login_required
@user_passes_test(is_admin)
@require_POST
def inventory_count_new(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    items = _items(env).filter(is_active=True)
    cat = _int(request.POST.get('category'))
    if cat:
        items = items.filter(Q(category_id=cat) | Q(category__parent_id=cat))
    items = list(items.order_by('name'))
    if not items:
        messages.error(request, 'There are no active stock items to count here.')
        return redirect('restaurant_admin:inventory_counts', restaurant_slug=env['restaurant'].slug)
    with transaction.atomic():
        count = StockCount.objects.create(restaurant=env['restaurant'], branch=env['branch'], created_by=request.user,
                                          title=(request.POST.get('title') or '').strip()[:120] or f'Stock count {timezone.localdate():%d %b %Y}')
        StockCountLine.objects.bulk_create([StockCountLine(count=count, item=i, expected=i.quantity or 0, unit_cost=i.unit_cost) for i in items])
    messages.success(request, f'Count started for {len(items)} items. Enter what is on the shelf, then post it.')
    return redirect('restaurant_admin:inventory_count_detail', restaurant_slug=env['restaurant'].slug, pk=count.pk)


def _count(env, pk):
    qs = StockCount.objects.filter(restaurant=env['restaurant'])
    return get_object_or_404(qs, pk=pk)


@login_required
@user_passes_test(is_admin)
def inventory_count_detail(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    count = _count(env, pk)
    lines = list(count.lines.select_related('item', 'item__category'))
    counted = [l for l in lines if l.counted is not None]
    over = sum((l.variance_value for l in counted if l.variance > 0), ZERO)
    under = sum((l.variance_value for l in counted if l.variance < 0), ZERO)
    if request.GET.get('export') == 'csv':
        return _csv(f'stock-count-{count.pk}.csv', ['Item', 'Unit', 'Book', 'Counted', 'Variance', 'Unit cost', 'Variance value'],
                    [[l.item.name, l.item.unit, l.expected, '' if l.counted is None else l.counted, '' if l.variance is None else l.variance,
                      l.unit_cost, '' if l.variance_value is None else l.variance_value.quantize(CENT)] for l in lines])
    return _render(request, env, 'controls/_count_detail.html', 'inventory_counts', count.title, count=count, lines=lines,
                   counted=len(counted), over=over, under=under, net=over + under)


@login_required
@user_passes_test(is_admin)
@require_POST
def inventory_count_save(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    count = _count(env, pk)
    url = reverse('restaurant_admin:inventory_count_detail', kwargs={'restaurant_slug': env['restaurant'].slug, 'pk': count.pk})
    if count.status != StockCount.Status.DRAFT:
        messages.error(request, 'This count is already closed.')
        return redirect(url)
    op = request.POST.get('op', 'save')
    if op == 'cancel':
        count.status = StockCount.Status.CANCELLED
        count.save(update_fields=['status'])
        messages.info(request, 'Count cancelled. Stock was not changed.')
        return redirect('restaurant_admin:inventory_counts', restaurant_slug=env['restaurant'].slug)
    lines = {l.pk: l for l in count.lines.select_related('item')}
    for key, value in request.POST.items():
        if not key.startswith('c_'):
            continue
        line = lines.get(_int(key[2:]))
        if not line:
            continue
        v = value.strip()
        line.counted = None if v == '' else max(0, _int(v, line.counted or 0))
    StockCountLine.objects.bulk_update(list(lines.values()), ['counted'])
    count.note = (request.POST.get('note') or count.note).strip()
    count.save(update_fields=['note'])
    if op != 'post':
        messages.success(request, 'Counts saved. Post the count when you’re done to correct the stock.')
        return redirect(url)
    done = [l for l in lines.values() if l.counted is not None]
    if not done:
        messages.error(request, 'Enter at least one counted quantity before posting.')
        return redirect(url)
    changed = 0
    with transaction.atomic():
        for line in done:
            delta = line.counted - line.expected
            if not delta:
                continue
            item = InventoryItem.objects.select_for_update().get(pk=line.item_id)
            item.adjust(delta)  # applied on top of sales made while counting
            InventoryTransaction.objects.create(inventory_item=item, transaction_type='S', delta=delta, unit_cost=line.unit_cost,
                                                stock_count=count, note=f'{count.title}: book {line.expected}, counted {line.counted}',
                                                performed_by=request.user)
            changed += 1
        count.status, count.posted_by, count.posted_at = StockCount.Status.POSTED, request.user, timezone.now()
        count.save(update_fields=['status', 'posted_by', 'posted_at'])
    messages.success(request, f'Count posted. {changed} item{"s" if changed != 1 else ""} corrected; {len(done) - changed} matched the books.')
    return redirect(url)


# ============================================================ menu engineering

def recipe_costs(restaurant, items):
    """{menu_item_id: plate cost} from recipes; items without a recipe are missing."""
    costs = defaultdict(lambda: ZERO)
    for ing in MenuItemIngredient.objects.filter(menu_item__in=items).select_related('inventory_item'):
        costs[ing.menu_item_id] += Decimal(ing.quantity_needed) * ing.inventory_item.unit_cost
    return costs


def menu_engineering(restaurant, branches, start, end):
    orders = M.sales_orders(restaurant, branches, start, end)
    rows = (OrderItem.objects.filter(order__in=orders).values('menu_item_id', 'menu_item__name', 'menu_item__category__name')
            .annotate(qty=Sum('quantity'), revenue=Sum(F('unit_price') * F('quantity')), disc=Sum('discount_amount')))
    rows = [r for r in rows if r['qty']]
    items = MenuItem.objects.filter(pk__in=[r['menu_item_id'] for r in rows])
    costs = recipe_costs(restaurant, items)
    total_qty = sum(r['qty'] for r in rows) or 0
    out = []
    for r in rows:
        revenue = Decimal(r['revenue'] or 0) - Decimal(r['disc'] or 0)
        avg_price = (revenue / r['qty']).quantize(CENT)
        cost = costs.get(r['menu_item_id'])
        has_recipe = r['menu_item_id'] in costs
        margin = (avg_price - cost).quantize(CENT) if has_recipe else None
        out.append({'id': r['menu_item_id'], 'name': r['menu_item__name'], 'category': r['menu_item__category__name'] or '—',
                    'qty': r['qty'], 'revenue': revenue.quantize(CENT), 'price': avg_price,
                    'cost': cost.quantize(CENT) if has_recipe else None, 'margin': margin,
                    'food_cost_pct': round(float(cost / avg_price * 100), 1) if has_recipe and avg_price else None,
                    'total_margin': (margin * r['qty']).quantize(CENT) if margin is not None else None,
                    'mix': round(r['qty'] / total_qty * 100, 1) if total_qty else 0})
    costed = [x for x in out if x['margin'] is not None]
    # Kasavana & Smith: popular = sells more than 70% of an even share; profitable = margin above the weighted average
    pop_line = (100 / len(out) * 0.7) if out else 0
    costed_qty = sum(x['qty'] for x in costed)
    avg_margin = (sum((x['total_margin'] for x in costed), ZERO) / costed_qty).quantize(CENT) if costed_qty else ZERO
    for x in out:
        if x['margin'] is None:
            x['class'] = 'unknown'
            continue
        popular, profitable = x['mix'] >= pop_line, x['margin'] >= avg_margin
        x['class'] = 'star' if popular and profitable else 'plowhorse' if popular else 'puzzle' if profitable else 'dog'
    out.sort(key=lambda x: -(x['total_margin'] if x['total_margin'] is not None else x['revenue']))
    revenue = sum((x['revenue'] for x in costed), ZERO)
    food = sum((x['cost'] * x['qty'] for x in costed), ZERO)
    return {'rows': out, 'avg_margin': avg_margin, 'pop_line': round(pop_line, 1),
            'food_cost_pct': round(float(food / revenue * 100), 1) if revenue else None,
            'theoretical_cost': food.quantize(CENT), 'costed_revenue': revenue.quantize(CENT),
            'uncosted': len(out) - len(costed), 'counts': {k: sum(1 for x in out if x['class'] == k) for k in ('star', 'plowhorse', 'puzzle', 'dog', 'unknown')}}


@login_required
@user_passes_test(is_admin)
def report_menu_engineering(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    key, label, start, end = _period(request)
    branches = [env['branch']] if env['branch'] else None
    data = menu_engineering(env['restaurant'], branches, start, end)
    cls = request.GET.get('class')
    rows = [r for r in data['rows'] if not cls or r['class'] == cls]
    if request.GET.get('export') == 'csv':
        return _csv(f'menu-engineering-{start}-{end}.csv', ['Item', 'Category', 'Sold', 'Menu mix %', 'Avg price', 'Plate cost', 'Food cost %', 'Margin', 'Total margin', 'Class'],
                    [[r['name'], r['category'], r['qty'], r['mix'], r['price'], r['cost'] if r['cost'] is not None else '',
                      r['food_cost_pct'] if r['food_cost_pct'] is not None else '', r['margin'] if r['margin'] is not None else '',
                      r['total_margin'] if r['total_margin'] is not None else '', r['class']] for r in rows])
    return _render(request, env, 'controls/_menu_engineering.html', 'menu_engineering', 'Menu engineering',
                   period=key, label=label, start=start, end=end, periods=M.PERIODS, d=data, rows=rows, cls=cls)


# ============================================================ waitlist

def _busy_tables(env):
    since = timezone.now() - datetime.timedelta(hours=12)
    qs = Order.objects.filter(restaurant=env['restaurant'], table__isnull=False, order_time__gte=since).exclude(status__in=['COMPLETED', 'C', 'DRAFT'])
    if env['branch']:
        qs = qs.filter(branch=env['branch'])
    return set(qs.values_list('table_id', flat=True))


@login_required
@user_passes_test(is_admin)
def reservation_waitlist(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    base = WaitlistEntry.objects.filter(restaurant=env['restaurant'])
    if env['branch']:
        base = base.filter(branch=env['branch'])
    today = timezone.localdate()
    waiting = list(base.filter(status__in=['WAITING', 'NOTIFIED']).order_by('created_at'))
    done_today = base.filter(created_at__date=today).exclude(status__in=['WAITING', 'NOTIFIED'])
    seated = [w for w in done_today if w.status == 'SEATED']
    waits = [w.waited_minutes for w in seated]
    tables = Table.objects.filter(restaurant=env['restaurant'], is_active=True)
    if env['branch']:
        tables = tables.filter(branch=env['branch'])
    busy = _busy_tables(env)
    held = {w.table_id for w in waiting if w.table_id}
    free = [t for t in tables.order_by('capacity', 'table_number') if t.id not in busy and t.id not in held]
    for w in waiting:
        w.fits = [t for t in free if t.capacity >= w.party_size][:4]
        digits = ''.join(ch for ch in w.phone if ch.isdigit())
        w.wa = ('91' + digits) if len(digits) == 10 else digits
        w.late = w.waited_minutes > w.quoted_minutes
    avg = round(sum(waits) / len(waits)) if waits else None
    ahead = sum(1 for w in waiting)
    return _render(request, env, 'controls/_waitlist.html', 'waitlist', 'Waitlist', waiting=waiting, free=free,
                   seated_count=len(seated), left_count=done_today.filter(status='LEFT').count(), avg_wait=avg,
                   suggested=max(10, (avg or 15) + 5 * max(0, ahead - len(free))),
                   recent=done_today.order_by('-created_at')[:15], tables=tables.order_by('table_number'))


@login_required
@user_passes_test(is_admin)
@require_POST
def reservation_waitlist_add(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    name = (request.POST.get('name') or '').strip()
    party = _int(request.POST.get('party_size'), 0)
    if not name or not (1 <= party <= 50):
        messages.error(request, 'Add a name and a party size between 1 and 50.')
    else:
        WaitlistEntry.objects.create(restaurant=env['restaurant'], branch=env['branch'], name=name[:120], party_size=party,
                                     phone=(request.POST.get('phone') or '').strip()[:20],
                                     quoted_minutes=max(0, min(240, _int(request.POST.get('quoted_minutes'), 15))),
                                     preference=(request.POST.get('preference') or '').strip()[:60],
                                     notes=(request.POST.get('notes') or '').strip()[:255], created_by=request.user)
        messages.success(request, f'{name} ({party}) added to the waitlist.')
    return redirect('restaurant_admin:reservation_waitlist', restaurant_slug=env['restaurant'].slug)


@login_required
@user_passes_test(is_admin)
@require_POST
def reservation_waitlist_update(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    entry = get_object_or_404(WaitlistEntry, pk=pk, restaurant=env['restaurant'])
    op = request.POST.get('op')
    now = timezone.now()
    if op == 'notify':
        entry.status, entry.notified_at = WaitlistEntry.Status.NOTIFIED, now
        table = Table.objects.filter(pk=_int(request.POST.get('table'), 0), restaurant=env['restaurant']).first()
        entry.table = table or entry.table
        entry.save(update_fields=['status', 'notified_at', 'table'])
    elif op == 'seat':
        table = Table.objects.filter(pk=_int(request.POST.get('table'), 0), restaurant=env['restaurant']).first()
        entry.status, entry.seated_at = WaitlistEntry.Status.SEATED, now
        entry.table = table or entry.table
        entry.save(update_fields=['status', 'seated_at', 'table'])
        messages.success(request, f'{entry.name} seated' + (f' at {entry.table.name}.' if entry.table else '.'))
    elif op == 'left':
        entry.status, entry.left_at = WaitlistEntry.Status.LEFT, now
        entry.save(update_fields=['status', 'left_at'])
    elif op == 'back':
        entry.status, entry.left_at, entry.seated_at = WaitlistEntry.Status.WAITING, None, None
        entry.save(update_fields=['status', 'left_at', 'seated_at'])
    return redirect('restaurant_admin:reservation_waitlist', restaurant_slug=env['restaurant'].slug)
