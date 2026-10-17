"""Separate Zomato and Swiggy workspaces in the restaurant admin.

/dashboard/<slug>/delivery/<platform>/            dashboard + live order board
                               orders/            order history (+ CSV)
                               orders/<id>/       one order: items, money, timeline, platform calls
                               menu/              item mapping, platform prices, stock, menu sync
                               payouts/           settlement import & reconciliation
                               logs/              inbound events and outbound calls, retries
                               settings/          outlet connection, API, webhook, simulator

Everything works on the branch the user is in. Operations (accepting, marking ready…) are open
to cashiers and managers; settings, menu sync and payouts need an administrator.
"""
import csv
import hashlib
import hmac
import json
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from integrations.delivery import service
from integrations.delivery.platforms import CAPABILITY_LABELS, PLATFORMS, ACTIONS
from integrations.models import (Connection, ExternalOrderLink, MenuSyncJob, PlatformAction, PlatformEvent, PlatformMenuMapping,
                                 SettlementBatch)
from super_admin.activity import log_activity, user_role

ADMIN_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN'}
# (action, label, example path) for the endpoint-path fields
ACTION_ROWS = [
    ('accept', 'Accept order', '/orders/{order_id}/accept'), ('reject', 'Reject order', '/orders/{order_id}/reject'),
    ('mark_ready', 'Food ready', '/orders/{order_id}/ready'), ('mark_picked_up', 'Handed to rider', '/orders/{order_id}/picked-up'),
    ('item_availability', 'Item stock', '/outlets/{outlet_id}/items/{item_id}/stock'),
    ('store_status', 'Outlet online/offline', '/outlets/{outlet_id}/status'), ('menu_sync', 'Menu sync', '/outlets/{outlet_id}/menu'),
]
PREP_CHOICES = [10, 15, 20, 25, 30, 40, 50, 60]
OPERATOR_ROLES = ADMIN_ROLES | {'CASHIER'}


def _is_admin(user):
    from super_admin.permissions import custom_role_of
    if user.is_superuser:
        return True
    role = custom_role_of(user)
    if role is not None:
        return role.level('delivery') == 'manage' and role.level('integrations') == 'manage'
    return user_role(user) in ADMIN_ROLES


def _can_operate(user):
    from super_admin.permissions import custom_role_of
    if user.is_superuser:
        return True
    if custom_role_of(user) is not None:
        return True  # the role middleware has already checked the "delivery" area
    return user_role(user) in OPERATOR_ROLES


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def _env(request, restaurant_slug, platform):
    from restaurant_admin.views import get_active_branch, resolve_restaurant
    if platform not in PLATFORMS:
        raise Http404('Unknown platform')
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    if not request.user.is_staff or not _can_operate(request.user):
        return HttpResponseForbidden('Online orders are handled by cashiers and managers.')
    branch = get_active_branch(request, restaurant)
    if branch is None:
        return HttpResponseForbidden('Add a branch first.')
    conn = service.connection_for(branch, platform)
    return {'restaurant': restaurant, 'branch': branch, 'platform': platform, 'p': PLATFORMS[platform], 'conn': conn,
            'slug': restaurant_slug, 'is_admin': _is_admin(request.user)}


def _url(env, name, **kw):
    return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': env['slug'], 'platform': env['platform'], **kw})


def _tabs(env):
    t = [('dashboard', 'Dashboard', 'fa-gauge', 'delivery_dashboard'), ('orders', 'Orders', 'fa-receipt', 'delivery_orders'),
         ('menu', 'Menu & stock', 'fa-utensils', 'delivery_menu')]
    if env['is_admin']:
        t += [('payouts', 'Payouts', 'fa-indian-rupee-sign', 'delivery_payouts'), ('logs', 'Logs', 'fa-list-check', 'delivery_logs'),
              ('settings', 'Settings', 'fa-sliders', 'delivery_settings')]
    return [{'key': k, 'label': l, 'icon': i, 'url': _url(env, n)} for k, l, i, n in t]


def _render(request, env, partial, context, tab):
    other = 'swiggy' if env['platform'] == 'zomato' else 'zomato'
    data = {
        'restaurant': env['restaurant'], 'partial': partial, 'tab': tab, 'tabs': _tabs(env), 'env': env, 'p': env['p'],
        'conn': env['conn'], 'branch': env['branch'], 'is_admin': env['is_admin'], 'active_menu': f'delivery_{env["platform"]}',
        'page_title': env['p']['label'], 'other': PLATFORMS[other], 'prep_choices': PREP_CHOICES,
        'other_url': reverse('restaurant_admin:delivery_dashboard', kwargs={'restaurant_slug': env['slug'], 'platform': other}),
        'u': {n: _url(env, f'delivery_{n}') for n in ('dashboard', 'orders', 'menu', 'payouts', 'logs', 'settings', 'live',
                                                     'store', 'menu_sync', 'menu_export', 'simulate')},
        **context,
    }
    return render(request, 'guests/shell_restaurant.html', data)


def _need_conn(request, env):
    if env['conn'] is None:
        if env['is_admin']:
            return redirect(_url(env, 'delivery_settings'))
        return _render(request, env, 'delivery/_not_connected.html', {}, 'dashboard')
    return None


def _need_admin(env):
    if not env['is_admin']:
        return HttpResponseForbidden('Only administrators can change this.')
    return None


def view(fn, admin=False, conn=True):
    @login_required
    def wrapper(request, restaurant_slug, platform, *args, **kwargs):
        env = _env(request, restaurant_slug, platform)
        if isinstance(env, HttpResponse):
            return env
        if admin:
            denied = _need_admin(env)
            if denied:
                return denied
        if conn:
            missing = _need_conn(request, env)
            if missing:
                return missing
        return fn(request, env, *args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------

@login_required
def home(request, restaurant_slug):
    from restaurant_admin.views import get_active_branch, resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    branch = get_active_branch(request, restaurant)
    first = Connection.objects.filter(branch=branch, provider__in=list(PLATFORMS), is_active=True).order_by('provider').first()
    return redirect('restaurant_admin:delivery_dashboard', restaurant_slug=restaurant_slug, platform=first.provider if first else 'zomato')


# --------------------------------------------------------------------------
# Dashboard & live board
# --------------------------------------------------------------------------

def _period(request):
    from restaurant_admin import dashboard_metrics as DM
    return DM.resolve_period(request.GET.get('period', 'today'), request.GET.get('from', ''), request.GET.get('to', ''))


def _dashboard(request, env):
    from restaurant_admin import dashboard_metrics as DM
    period, label, start, end = _period(request)
    st = service.stats(env['conn'], start, end)
    chart = {'labels': [s['label'] for s in st['series']], 'orders': [s['orders'] for s in st['series']], 'gmv': [s['gmv'] for s in st['series']]}
    return _render(request, env, 'delivery/_dashboard.html', {
        'st': st, 'chart': chart, 'period': period, 'period_label': label, 'periods': DM.PERIODS[:6], 'start': start, 'end': end,
        'board': service.live_board(env['conn']), 'attn': service.attention(env['conn']),
        'reasons_reject': env['p']['reject_reasons'],
    }, 'dashboard')


def _live(request, env):
    """Polled every 10 s by the dashboard: fresh board HTML plus counts for the chime."""
    board = service.live_board(env['conn'])
    html = render_to_string('delivery/_board.html', {'board': board, 'p': env['p'], 'conn': env['conn'], 'u': {
        'orders': _url(env, 'delivery_orders')}, 'reasons_reject': env['p']['reject_reasons'], 'env': env, 'prep_choices': PREP_CHOICES}, request=request)
    newest = ExternalOrderLink.objects.filter(connection=env['conn']).order_by('-id').values_list('id', flat=True).first() or 0
    return JsonResponse({'html': html, 'placed': len(board['placed']), 'newest': newest,
                         'open': sum(len(v) for v in board.values()), 'store_open': env['conn'].is_store_open})


def _order_action(request, env, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)
    link = get_object_or_404(ExternalOrderLink, pk=pk, connection=env['conn'])
    action = request.POST.get('action')
    if action not in service.ACTION_FLOW:
        messages.error(request, 'Unknown action.')
        return redirect(request.POST.get('next') or _url(env, 'delivery_dashboard'))
    ok, msg = service.perform(link, action, request.user, prep_minutes=request.POST.get('prep_minutes'), reason=request.POST.get('reason'))
    log_activity(request, action='UPDATE', message=f'{env["p"]["label"]} order #{link.external_id}: {service.ACTION_LABELS[action]} — {msg}',
                 area='restaurant', restaurant=env['restaurant'], object_type='Order', object_id=link.order_id, object_repr=f'#{link.external_id}')
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': ok, 'message': msg})
    (messages.success if ok else messages.error)(request, msg)
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else _url(env, 'delivery_dashboard'))


def _store(request, env):
    if request.method != 'POST':
        return HttpResponse(status=405)
    want_open = request.POST.get('open') == '1'
    ok, msg = service.set_store_open(env['conn'], want_open, request.user)
    log_activity(request, action='UPDATE', message=f'{env["p"]["label"]} outlet set {"online" if want_open else "offline"} — {msg}',
                 area='restaurant', restaurant=env['restaurant'])
    (messages.success if ok else messages.error)(request, f'Outlet {"online" if want_open else "offline"}. {msg}' if ok else msg)
    return redirect(request.POST.get('next') or _url(env, 'delivery_dashboard'))


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

STATUS_FILTERS = [('open', 'Open'), ('placed', 'Awaiting acceptance'), ('accepted', 'Preparing'), ('ready', 'Ready'),
                  ('picked_up', 'Out for delivery'), ('delivered', 'Delivered'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')]


def _orders(request, env):
    qs = ExternalOrderLink.objects.filter(connection=env['conn']).select_related('order').order_by('-created_at')
    status = request.GET.get('status', '')
    if status == 'open':
        qs = qs.filter(platform_status__in=service.OPEN)
    elif status in dict(STATUS_FILTERS):
        qs = qs.filter(platform_status=status)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(external_id__icontains=q) | Q(order__customer_name__icontains=q) | Q(rider_name__icontains=q))
    d_from, d_to = parse_date(request.GET.get('from', '') or ''), parse_date(request.GET.get('to', '') or '')
    if d_from:
        qs = qs.filter(created_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(created_at__date__lte=d_to)
    if request.GET.get('settled') == 'no':
        qs = qs.filter(settled_amount__isnull=True).exclude(platform_status__in=['rejected', 'cancelled'])
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{env["platform"]}-orders.csv"'
        w = csv.writer(resp)
        w.writerow(['Platform order', 'Our order', 'Placed', 'Status', 'Customer payable', 'Items', 'Tax', 'Discount', 'Packaging',
                    'Commission', 'Expected payout', 'Settled', 'Prep min', 'Reason'])
        for l in qs[:5000]:
            w.writerow([l.external_id, l.order.branch_order_number or l.order_id, timezone.localtime(l.created_at).strftime('%Y-%m-%d %H:%M'),
                        l.get_platform_status_display(), l.customer_payable, l.item_total, l.tax_total, l.discount_total, l.packaging_fee,
                        l.commission_amount, l.expected_payout, '' if l.settled_amount is None else l.settled_amount, l.prep_minutes or '',
                        l.cancel_reason])
        return resp
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    counts = dict(ExternalOrderLink.objects.filter(connection=env['conn']).values_list('platform_status').annotate(n=Count('id')))
    params = request.GET.copy()
    params.pop('page', None)
    return _render(request, env, 'delivery/_orders.html', {
        'page': page, 'status': status, 'q': q, 'filters': STATUS_FILTERS, 'counts': counts,
        'd_from': request.GET.get('from', ''), 'd_to': request.GET.get('to', ''), 'qs_no_page': params.urlencode(),
    }, 'orders')


def _order_detail(request, env, pk):
    link = get_object_or_404(ExternalOrderLink.objects.select_related('order', 'connection'), pk=pk, connection=env['conn'])
    return _render(request, env, 'delivery/_order_detail.html', {
        'link': link, 'items': link.order.items.select_related('menu_item'), 'history': link.history.select_related('actor'),
        'actions': link.actions.all(), 'events': PlatformEvent.objects.filter(connection=env['conn'], external_order_id=link.external_id),
        'raw': json.dumps(link.raw, indent=2, default=str)[:20000], 'reasons_reject': env['p']['reject_reasons'],
        'settlement': link.settlement_lines.select_related('batch').first(),
    }, 'orders')


# --------------------------------------------------------------------------
# Menu & stock
# --------------------------------------------------------------------------

def _menu(request, env):
    from core.models import Category, MenuItem
    conn = env['conn']
    items = (MenuItem.objects.filter(restaurant=env['restaurant']).filter(Q(branch=env['branch']) | Q(branch__isnull=True))
             .select_related('category').order_by('category__name', 'name'))
    if request.method == 'POST':
        if not env['is_admin'] and request.POST.get('op') != 'stock':
            return HttpResponseForbidden('Only administrators can change listings and prices.')
        if request.POST.get('op') == 'stock':
            m = get_object_or_404(PlatformMenuMapping, pk=request.POST.get('mapping'), connection=conn)
            ok, msg = service.set_item_available(m, request.POST.get('available') == '1', request.user)
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'ok': ok, 'message': msg, 'available': m.is_available})
            (messages.success if ok else messages.error)(request, f'{m.menu_item.name}: {msg}')
            return redirect(request.get_full_path())
        mappings = {m.menu_item_id: m for m in conn.menu_mappings.all()}
        changed, errors = 0, []
        for it in items:
            if f'seen_{it.pk}' not in request.POST:
                continue
            listed = request.POST.get(f'listed_{it.pk}') == '1'
            ext = request.POST.get(f'ext_{it.pk}', '').strip()[:120]
            raw_price = request.POST.get(f'price_{it.pk}', '').strip()
            try:
                price = Decimal(raw_price) if raw_price else None
                if price is not None and price <= 0:
                    raise ValueError
            except (ArithmeticError, ValueError):
                errors.append(f'{it.name}: enter a price above zero')
                continue
            m = mappings.get(it.pk)
            if m is None and not listed and not ext:
                continue
            if ext and conn.menu_mappings.filter(external_item_id=ext).exclude(menu_item=it).exists():
                errors.append(f'{it.name}: ID {ext} is already used by another item')
                continue
            m = m or PlatformMenuMapping(connection=conn, menu_item=it)
            new = (listed, ext, price)
            if m.pk is None or (m.is_listed, m.external_item_id, m.platform_price) != new:
                m.is_listed, m.external_item_id, m.platform_price = new
                if m.pk:
                    m.sync_status = 'pending'
                m.save()
                changed += 1
        if errors:
            messages.error(request, ' · '.join(errors[:5]))
        if changed:
            log_activity(request, action='UPDATE', message=f'Updated {changed} {env["p"]["label"]} menu listing(s)', area='restaurant',
                         restaurant=env['restaurant'])
            messages.success(request, f'Saved {changed} item{"s" if changed != 1 else ""}. Sync the menu to publish the changes.')
        return redirect(request.get_full_path())

    mappings = {m.menu_item_id: m for m in conn.menu_mappings.all()}
    cat = request.GET.get('category', '')
    q = request.GET.get('q', '').strip()
    show = request.GET.get('show', '')
    if cat.isdigit():
        items = items.filter(category_id=int(cat))
    if q:
        items = items.filter(Q(name__icontains=q) | Q(sku__icontains=q))
    rows = []
    for it in items:
        m = mappings.get(it.pk)
        if show == 'listed' and not (m and m.is_listed):
            continue
        if show == 'unlisted' and m and m.is_listed:
            continue
        if show == 'no_id' and not (m and m.is_listed and not m.external_item_id):
            continue
        if show == 'out' and not (m and m.is_listed and not (m.is_available and it.availability)):
            continue
        rows.append({'item': it, 'm': m, 'price': (m.effective_price if m else (it.markup_price or it.price))})
    listed = [m for m in mappings.values() if m.is_listed]
    return _render(request, env, 'delivery/_menu.html', {
        'rows': rows, 'categories': Category.objects.filter(restaurant=env['restaurant']).filter(Q(branch=env['branch']) | Q(branch__isnull=True)).order_by('name'),
        'cat': cat, 'q': q, 'show': show, 'jobs': conn.menu_sync_jobs.select_related('created_by')[:6],
        'summary': {'listed': len(listed), 'no_id': sum(1 for m in listed if not m.external_item_id),
                    'out': sum(1 for m in listed if not m.is_available), 'pending': sum(1 for m in listed if m.sync_status != 'synced'),
                    'menu': items.count()},
    }, 'menu')


def _menu_sync(request, env):
    if request.method != 'POST':
        return HttpResponse(status=405)
    job, msg = service.sync_menu(env['conn'], request.user)
    log_activity(request, action='UPDATE', message=f'{env["p"]["label"]} menu sync: {msg}', area='restaurant', restaurant=env['restaurant'])
    (messages.success if job and job.state in ('submitted', 'succeeded', 'manual') else messages.error)(request, msg)
    return redirect(_url(env, 'delivery_menu'))


def _menu_export(request, env):
    job = None
    if request.GET.get('job', '').isdigit():
        job = get_object_or_404(MenuSyncJob, pk=int(request.GET['job']), connection=env['conn'])
    snap = job.menu_snapshot if job else service.menu_snapshot(env['conn'])
    name = f'{env["platform"]}-menu-{env["branch"].slug or env["branch"].pk}'
    if request.GET.get('format') == 'json':
        resp = HttpResponse(json.dumps(snap, indent=2), content_type='application/json')
        resp['Content-Disposition'] = f'attachment; filename="{name}.json"'
        return resp
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{name}.csv"'
    w = csv.writer(resp)
    w.writerow(['Item ID', 'Variant ID', 'Name', 'Category', 'Price', 'In stock', 'Description'])
    for r in snap.get('items', []):
        w.writerow([r['item_id'], r['variant_id'], r['name'], r['category'], r['price'], 'Yes' if r['in_stock'] else 'No', r['description']])
    return resp


# --------------------------------------------------------------------------
# Payouts
# --------------------------------------------------------------------------

def _payouts(request, env):
    conn = env['conn']
    if request.method == 'POST':
        f = request.FILES.get('file')
        if not f or f.size > 5 * 1024 * 1024 or not f.name.lower().endswith('.csv'):
            messages.error(request, 'Choose a CSV file under 5 MB.')
            return redirect(_url(env, 'delivery_payouts'))
        try:
            text = f.read().decode('utf-8-sig', errors='replace')
            batch = service.import_settlement(conn, text, file_name=f.name, reference=request.POST.get('reference', ''),
                                              payout_date=parse_date(request.POST.get('payout_date', '') or ''), user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(_url(env, 'delivery_payouts'))
        log_activity(request, action='CREATE', message=f'Imported {env["p"]["label"]} payout {batch.reference or f.name}: {batch.lines_count} orders',
                     area='restaurant', restaurant=env['restaurant'], object_type='SettlementBatch', object_id=batch.pk)
        messages.success(request, f'Imported {batch.lines_count} orders: {batch.matched} match, {batch.mismatched} differ, {batch.unmatched} not found.')
        return redirect(_url(env, 'delivery_payout_detail', pk=batch.pk))
    unsettled = (ExternalOrderLink.objects.filter(connection=conn, settled_amount__isnull=True)
                 .exclude(platform_status__in=['rejected', 'cancelled', 'placed']))
    from django.db.models import Sum
    agg = unsettled.aggregate(n=Count('id'), v=Sum('expected_payout'), old=Count('id', filter=Q(created_at__lt=timezone.now() - timedelta(days=10))))
    return _render(request, env, 'delivery/_payouts.html', {
        'batches': conn.settlements.all()[:30], 'unsettled': agg,
    }, 'payouts')


def _payout_detail(request, env, pk):
    batch = get_object_or_404(SettlementBatch, pk=pk, connection=env['conn'])
    show = request.GET.get('show', '')
    lines = batch.lines.select_related('link')
    if show in ('matched', 'mismatch', 'unmatched'):
        lines = lines.filter(match=show)
    return _render(request, env, 'delivery/_payout_detail.html', {'batch': batch, 'lines': lines[:1000], 'show': show}, 'payouts')


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

def _logs(request, env):
    conn = env['conn']
    kind = request.GET.get('kind', 'events')
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'retry_event':
            ev = get_object_or_404(PlatformEvent, pk=request.POST.get('id'), connection=conn)
            ev = service.process(ev)
            (messages.success if ev.state == 'processed' else messages.error)(request, f'Event {ev.get_state_display().lower()}. {ev.last_error}'.strip())
        elif op == 'retry_action':
            act = get_object_or_404(PlatformAction, pk=request.POST.get('id'), connection=conn)
            if act.state in ('retry', 'failed'):
                ok, msg = service._execute(act, actor=request.user)
                (messages.success if ok else messages.error)(request, msg)
        elif op == 'map':
            ev = get_object_or_404(PlatformEvent, pk=request.POST.get('id'), connection=conn, state='needs_mapping')
            choices = {}
            for i, u in enumerate(ev.unmapped):
                val = request.POST.get(f'item_{i}', '')
                if val.isdigit():
                    choices[u['external_id'] or u['name']] = int(val)
            missing = [u for u in ev.unmapped if not u['external_id']]
            if missing:
                messages.error(request, 'Some items arrived without a platform item ID; ask the platform to add IDs, then retry.')
            ev = service.map_unmapped(ev, choices, request.user)
            log_activity(request, action='UPDATE', message=f'Mapped {len(choices)} {env["p"]["label"]} item(s) and reprocessed an order',
                         area='restaurant', restaurant=env['restaurant'])
            (messages.success if ev.state == 'processed' else messages.error)(
                request, 'Order created.' if ev.state == 'processed' else f'Still not processed: {ev.last_error}')
        return redirect(request.POST.get('next') or request.get_full_path())
    state = request.GET.get('state', '')
    if kind == 'actions':
        qs = conn.platform_actions.select_related('link', 'created_by')
        if state:
            qs = qs.filter(state=state)
        states = PlatformAction.State.choices
    else:
        qs = conn.platform_events.all()
        if state:
            qs = qs.filter(state=state)
        states = PlatformEvent.State.choices
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    from core.models import MenuItem
    menu = None
    if kind == 'events' and any(e.state == 'needs_mapping' for e in page):
        menu = (MenuItem.objects.filter(restaurant=env['restaurant']).filter(Q(branch=env['branch']) | Q(branch__isnull=True)).order_by('name'))
    return _render(request, env, 'delivery/_logs.html', {'page': page, 'kind': kind, 'state': state, 'states': states, 'menu': menu}, 'logs')


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _is_brand_admin(user):
    """Brand-wide keys are changed only by someone responsible for the whole business."""
    if user.is_superuser:
        return True
    from super_admin.permissions import custom_role_of
    if custom_role_of(user) is not None:
        return False
    profile = getattr(user, 'restaurant_profile', None)
    role = user_role(user)
    return role == 'GENERAL_MANAGER' or (role == 'RESTAURANT_ADMIN' and profile is not None and (profile.is_primary_admin or profile.branch_id is None))


def _clean_url(request, field, label):
    from integrations.dispatch import UnsafeURL, check_url
    value = request.POST.get(field, '').strip()
    if value:
        try:
            check_url(value)
        except UnsafeURL as exc:
            messages.error(request, f'{label}: {exc}')
            return None
    return value[:300]


def _paths_from_post(request, prefix='path_'):
    return {a: request.POST.get(f'{prefix}{a}', '').strip()[:200] for a in ACTIONS if request.POST.get(f'{prefix}{a}', '').strip()}


def _save_account(request, env):
    from integrations.models import PlatformAccount
    if not _is_brand_admin(request.user):
        return HttpResponseForbidden('Shared settings are managed by the business owner or General Manager.')
    acc, created = PlatformAccount.objects.get_or_create(restaurant=env['restaurant'], provider=env['platform'])
    base = _clean_url(request, 'acc_api_base_url', 'API base URL')
    token_url = _clean_url(request, 'acc_token_url', 'Token URL')
    if base is None or token_url is None:
        return redirect(_url(env, 'delivery_settings'))
    secrets_ = acc.get_secrets()
    changed = []
    for key in ('client_id', 'client_secret', 'access_token', 'webhook_secret'):
        new = request.POST.get(f'acc_{key}', '').strip()[:1000]
        if request.POST.get(f'acc_clear_{key}') == '1':
            if secrets_.pop(key, None):
                changed.append(key)
        elif new and new != secrets_.get(key):
            secrets_[key] = new
            changed.append(key)
    if changed:
        acc.set_secrets(secrets_)
    acc.environment = request.POST.get('acc_environment') if request.POST.get('acc_environment') in Connection.Environment.values else 'sandbox'
    acc.api_base_url, acc.token_url = base, token_url
    acc.auth_header = request.POST.get('acc_auth_header', '').strip()[:60]
    acc.signature_header = request.POST.get('acc_signature_header', '').strip()[:60]
    acc.api_paths = _paths_from_post(request, 'acc_path_')
    acc.updated_by = request.user
    acc.save()
    names = {'client_id': 'client ID', 'client_secret': 'client secret', 'access_token': 'access token', 'webhook_secret': 'webhook secret'}
    log_activity(request, action='SECURITY', area='restaurant', restaurant=env['restaurant'], object_type='PlatformAccount', object_id=acc.pk,
                 message=f'{"Set up" if created else "Updated"} the {env["p"]["label"]} brand account'
                         + (f' · changed {", ".join(names[c] for c in changed)}' if changed else ''))
    messages.success(request, f'Shared {env["p"]["label"]} settings saved for every branch of {env["restaurant"].name}. A branch\'s own keys still take priority.')
    return redirect(_url(env, 'delivery_settings'))


def _test_connection(request, env):
    from integrations.delivery import credentials as C
    conn = env['conn']
    if conn is None:
        messages.error(request, 'Connect the outlet first.')
        return redirect(_url(env, 'delivery_settings'))
    r = C.resolve(conn)
    ok, msg = False, ''
    if not r.has_auth:
        msg = 'No credentials yet — add an access token or client ID + secret.'
    elif r.uses_oauth:
        try:
            C.access_token(conn, force=True, resolved=r)
            ok, msg = True, f'{env["p"]["label"]} issued an access token with these credentials.'
        except C.TokenError as exc:
            msg = str(exc)
    else:
        ok, msg = True, 'Access token saved. It is checked by the platform on the first order action.'
    if not r.get('api_base_url'):
        msg += ' The API base URL is still missing.'
    missing = [a for a in ('accept', 'reject', 'mark_ready') if conn.can(a) and a not in r.paths]
    if missing:
        msg += f' No endpoint path yet for: {", ".join(m.replace("_", " ") for m in missing)}.'
    if conn.account and r.sources.get('client_id') == 'brand' or (conn.account and r.sources.get('access_token') == 'brand'):
        conn.account.last_test_at, conn.account.last_test_ok, conn.account.last_test_message = timezone.now(), ok, msg[:300]
        conn.account.save(update_fields=['last_test_at', 'last_test_ok', 'last_test_message'])
    log_activity(request, action='SECURITY', area='restaurant', restaurant=env['restaurant'],
                 message=f'Tested the {env["p"]["label"]} connection: {"ok" if ok else "failed"}')
    (messages.success if ok else messages.error)(request, msg.strip())
    return redirect(_url(env, 'delivery_settings'))


def _settings(request, env):
    conn = env['conn']
    if request.method == 'POST':
        op = request.POST.get('op', 'save')
        if op == 'account':
            return _save_account(request, env)
        if op == 'test':
            return _test_connection(request, env)
        if op == 'rotate' and conn:
            conn.set_webhook_secret('')
            conn.save()
            log_activity(request, action='SECURITY', message=f'Rotated the {env["p"]["label"]} outlet signing secret', area='restaurant', restaurant=env['restaurant'])
            messages.success(request, 'New signing secret created. Update it on the partner side now — the old one no longer works.')
            return redirect(_url(env, 'delivery_settings'))
        creating = conn is None
        conn = conn or Connection(restaurant=env['restaurant'], branch=env['branch'], provider=env['platform'], created_by=request.user)
        conn.external_store_id = request.POST.get('external_store_id', '').strip()[:120]
        if not conn.external_store_id:
            messages.error(request, f'Enter your {env["p"]["label"]} outlet ID.')
            return redirect(_url(env, 'delivery_settings'))
        clash = Connection.objects.filter(provider=env['platform'], external_store_id=conn.external_store_id).exclude(pk=conn.pk).first()
        if clash:
            messages.error(request, f'Outlet ID {conn.external_store_id} is already connected to {clash.branch.name}.')
            return redirect(_url(env, 'delivery_settings'))
        try:
            conn.commission_pct = max(Decimal('0'), min(Decimal(request.POST.get('commission_pct') or '0'), Decimal('60')))
            conn.default_prep_minutes = max(5, min(int(request.POST.get('default_prep_minutes') or 15), 120))
            conn.accept_window_seconds = max(30, min(int(request.POST.get('accept_window_seconds') or 180), 900))
        except (ArithmeticError, ValueError):
            messages.error(request, 'Commission, prep time and acceptance window must be numbers.')
            return redirect(_url(env, 'delivery_settings'))
        base = _clean_url(request, 'api_base_url', 'Outlet API base URL')
        if base is None:
            return redirect(_url(env, 'delivery_settings'))
        conn.name = request.POST.get('name', '').strip()[:120]
        conn.is_active = request.POST.get('is_active') == '1'
        conn.auto_accept = request.POST.get('auto_accept') == '1'
        conn.mode = request.POST.get('mode') if request.POST.get('mode') in Connection.Mode.values else Connection.Mode.MANUAL
        conn.environment = request.POST.get('environment') if request.POST.get('environment') in Connection.Environment.values else 'sandbox'
        conn.api_base_url = base
        conn.api_paths = _paths_from_post(request)
        conn.auth_header = (request.POST.get('auth_header', '').strip() or 'Authorization')[:60]
        conn.signature_header = (request.POST.get('signature_header', '').strip() or 'X-Signature')[:60]
        conn.capabilities = {k: request.POST.get(f'cap_{k}') == '1' for k, _ in CAPABILITY_LABELS}
        # This branch's own keys: blank keeps the saved value, "remove" clears it
        creds = conn.get_credentials()
        if creds.get('api_key') and not creds.get('access_token'):
            creds['access_token'] = creds.pop('api_key')
        creds.pop('api_key', None)
        changed_keys = []
        for key in ('client_id', 'client_secret', 'access_token'):
            if request.POST.get(f'clear_{key}') == '1' or request.POST.get('clear_credentials') == '1':
                if creds.pop(key, None):
                    changed_keys.append(key)
                continue
            new = request.POST.get(key if key != 'access_token' else 'api_key', '').strip()[:1000]
            if new and new != creds.get(key):
                creds[key] = new
                changed_keys.append(key)
        conn.set_credentials(creds)
        token_url = _clean_url(request, 'token_url', 'Branch token URL')
        if token_url is None:
            return redirect(_url(env, 'delivery_settings'))
        conn.token_url = token_url
        platform_secret = request.POST.get('platform_webhook_secret', '').strip()[:300]
        if platform_secret:
            conn.set_webhook_secret(platform_secret)
        conn.save()
        from integrations.delivery.credentials import resolve
        if conn.mode == Connection.Mode.API and not resolve(conn).api_ready:
            messages.warning(request, f'API mode needs an API base URL and credentials (outlet, brand account or .env). Until then actions show as “not configured”.')
        log_activity(request, action='CREATE' if creating else 'UPDATE',
                     message=f'{"Connected" if creating else "Updated"} {env["p"]["label"]} outlet {conn.external_store_id} ({conn.get_mode_display().split(" –")[0]}, {conn.environment})'
                             + (f' · changed branch {", ".join(k.replace("_", " ") for k in changed_keys)}' if changed_keys else '')
                             + (' · new platform webhook secret' if platform_secret else ''),
                     area='restaurant', restaurant=env['restaurant'], object_type='Connection', object_id=conn.pk)
        messages.success(request, f'{env["p"]["label"]} {"connected" if creating else "settings saved"}.')
        return redirect(_url(env, 'delivery_settings') if creating else request.get_full_path())

    from integrations.delivery import credentials as C
    from integrations.delivery.crypto import mask
    from integrations.models import PlatformAccount
    caps = {**PLATFORMS[env['platform']]['capabilities'], **((conn.capabilities or {}) if conn else {})}
    acc = PlatformAccount.objects.filter(restaurant=env['restaurant'], provider=env['platform']).first()
    acc_secrets = acc.get_secrets() if acc else {}
    resolved, rows = C.describe(conn) if conn else (None, [])
    outlet_creds = conn.get_credentials() if conn else {}
    sattkar = C.env_defaults(env['platform'])
    return _render(request, env, 'delivery/_settings.html', {
        'caps': [(k, l, caps.get(k)) for k, l in CAPABILITY_LABELS], 'actions': ACTIONS, 'action_rows': ACTION_ROWS,
        'webhook_url': request.build_absolute_uri(reverse('delivery_webhook', kwargs={'provider': env['platform'], 'public_id': conn.public_id})) if conn else '',
        'shared_webhook_url': request.build_absolute_uri(reverse('delivery_webhook_shared', kwargs={'provider': env['platform']})),
        'paths': (conn.api_paths or {}) if conn else {}, 'sample': json.dumps(SAMPLE_PAYLOAD, indent=2),
        'outlet_secret': conn.get_webhook_secret() if conn else '',
        'outlet_masks': {k: mask(v) for k, v in outlet_creds.items() if k in ('access_token', 'api_key', 'client_id', 'client_secret') and v},
        'outlet_token_mask': mask(outlet_creds.get('access_token') or outlet_creds.get('api_key') or ''),
        'acc': acc, 'acc_masks': {k: mask(v) for k, v in acc_secrets.items() if k in C.SECRET_KEYS},
        'acc_paths': (acc.api_paths or {}) if acc else {}, 'brand_admin': _is_brand_admin(request.user),
        'resolved': resolved, 'cred_rows': rows, 'resolved_paths': resolved.paths if resolved else {},
        'path_sources': resolved.path_sources if resolved else {}, 'source_labels': C.SOURCE_LABELS,
        'sattkar_configured': any(sattkar.get(k) for k in ('client_id', 'access_token', 'webhook_secret', 'api_base_url')),
    }, 'settings')


SAMPLE_PAYLOAD = {
    'event': 'order.placed', 'event_id': 'evt_7f3a', 'outlet_id': '<your outlet ID>',
    'order': {'id': 'ZO-482193', 'created_at': '2026-10-16T19:42:05+05:30', 'payment_type': 'prepaid',
              'customer': {'name': 'Aditi', 'phone': '+91 9XXXX XX210'}, 'instructions': 'Less spicy',
              'items': [{'item_id': 'PT-01', 'name': 'Paneer Tikka', 'quantity': 2, 'unit_price': '320.00',
                         'addons': [{'name': 'Extra mint chutney', 'price': '20.00'}]}],
              'amounts': {'item_total': '680.00', 'tax_total': '34.00', 'discount_total': '60.00', 'merchant_discount': '30.00',
                          'packaging_fee': '20.00', 'delivery_fee': '0.00', 'customer_payable': '674.00'}},
}


def _simulate(request, env):
    """Sandbox only: push realistic platform messages through the real, signed webhook pipeline."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    conn = env['conn']
    if conn.environment != Connection.Environment.SANDBOX:
        messages.error(request, 'The simulator only works for outlets in sandbox mode.')
        return redirect(_url(env, 'delivery_settings'))
    kind = request.POST.get('kind', 'order')
    payload = _sim_payload(env, kind)
    if payload is None:
        messages.error(request, 'Nothing to simulate yet — create a test order first.' if kind != 'order'
                       else 'List at least one menu item on this platform first (Menu & stock).')
        return redirect(request.POST.get('next') or _url(env, 'delivery_settings'))
    raw = json.dumps(payload).encode()
    sig = hmac.new(conn.get_webhook_secret().encode(), raw, hashlib.sha256).hexdigest()
    status, body = service.receive(conn, raw, {'X-Signature': sig})
    ok = status == 200 and body.get('state') in ('processed', 'needs_mapping')
    (messages.success if ok else messages.error)(request, f'Test {kind.replace("_", " ")} sent → {body.get("state", body.get("error"))}.')
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else _url(env, 'delivery_dashboard'))


def _sim_payload(env, kind):
    conn = env['conn']
    tag = random.randint(100000, 999999)
    if kind == 'order':
        maps = list(conn.menu_mappings.filter(is_listed=True).select_related('menu_item')[:40])
        if not maps:
            return None
        picks = random.sample(maps, k=min(len(maps), random.randint(1, 3)))
        items = [{'item_id': m.external_item_id or m.menu_item.sku or str(m.menu_item_id), 'name': m.menu_item.name,
                  'quantity': random.randint(1, 2), 'unit_price': str(m.effective_price)} for m in picks]
        total = sum(Decimal(i['unit_price']) * i['quantity'] for i in items)
        disc = (total * Decimal('0.1')).quantize(Decimal('1')) if random.random() < .4 else Decimal('0')
        tax = ((total - disc) * Decimal('0.05')).quantize(Decimal('0.01'))
        pack = Decimal('20.00')
        prefix = 'ZO' if env['platform'] == 'zomato' else 'SW'
        return {'event': 'order.placed', 'event_id': f'sim_{tag}', 'outlet_id': conn.external_store_id,
                'order': {'id': f'{prefix}-{tag}', 'created_at': timezone.now().isoformat(), 'payment_type': random.choice(['prepaid'] * 4 + ['cod']),
                          'customer': {'name': random.choice(['Aditi', 'Rohan', 'Meera', 'Kabir', 'Sana', 'Vikram']), 'phone': '+91 9XXXX XX' + str(tag)[:3]},
                          'instructions': random.choice(['', '', 'Less spicy', 'No onion please', 'Send cutlery']),
                          'items': items,
                          'amounts': {'item_total': str(total), 'tax_total': str(tax), 'discount_total': str(disc),
                                      'merchant_discount': str((disc / 2).quantize(Decimal('0.01'))), 'packaging_fee': str(pack),
                                      'delivery_fee': '0.00', 'customer_payable': str(total - disc + tax + pack)}}}
    wanted = {'rider': ['accepted', 'ready'], 'picked_up': ['ready', 'accepted'], 'delivered': ['picked_up'], 'cancel': ['placed', 'accepted']}[kind]
    link = ExternalOrderLink.objects.filter(connection=conn, platform_status__in=wanted).order_by('-created_at').first()
    if link is None:
        return None
    if kind == 'rider':
        return {'event': 'rider.assigned', 'event_id': f'sim_{tag}', 'order_id': link.external_id, 'status': 'rider_assigned',
                'rider': {'name': random.choice(['Ramesh K', 'Sunil', 'Imran', 'Deepak']), 'phone': '+91 9XXXX XX' + str(tag)[:3],
                          'eta': (timezone.now() + timedelta(minutes=8)).isoformat()}, 'pickup_code': str(tag)[:4]}
    if kind == 'cancel':
        return {'event': 'order.cancelled', 'event_id': f'sim_{tag}', 'order_id': link.external_id, 'reason': 'Customer cancelled', 'cancelled_by': 'customer'}
    return {'event': 'order.status', 'event_id': f'sim_{tag}', 'order_id': link.external_id, 'status': kind}


# --------------------------------------------------------------------------
# Public webhook (no login; authenticated by signature)
# --------------------------------------------------------------------------

@csrf_exempt
@require_POST
def webhook(request, provider, public_id):
    if provider not in PLATFORMS:
        raise Http404()
    conn = Connection.objects.filter(public_id=public_id, provider=provider).select_related('branch', 'restaurant').first()
    if conn is None:
        return JsonResponse({'error': 'unknown_outlet'}, status=404)
    status, body = service.receive(conn, request.body, request.headers)
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def webhook_shared(request, provider):
    """One URL per platform for POS-partner setups: the outlet is found from the message itself."""
    if provider not in PLATFORMS:
        raise Http404()
    status, body = service.receive_shared(provider, request.body, request.headers)
    return JsonResponse(body, status=status)


# --------------------------------------------------------------------------
# URL entry points
# --------------------------------------------------------------------------

dashboard = view(_dashboard)
live = view(_live)
order_action = view(_order_action)
store = view(_store)
orders = view(_orders)
order_detail = view(_order_detail)
menu = view(_menu)
menu_sync = view(_menu_sync, admin=True)
menu_export = view(_menu_export, admin=True)
payouts = view(_payouts, admin=True)
payout_detail = view(_payout_detail, admin=True)
logs = view(_logs, admin=True)
settings_view = view(_settings, admin=True, conn=False)
simulate = view(_simulate, admin=True)
