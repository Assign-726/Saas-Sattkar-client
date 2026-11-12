"""Waiter / Captain screens: the live table board, opening a table in the POS, and asking for the bill.

A waiter taps a table, takes the order in the POS and sends it to the kitchen. The cashier settles.
"""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Order, Table
from restaurant_admin.waiter import is_waiter

ACTIVE_WINDOW_HOURS = 18  # same window the floor plan uses for "occupied"


def _is_staff(user):
    return user.is_authenticated and user.is_staff


def _context(request, restaurant_slug):
    from restaurant_admin.views import get_active_branch, resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant, None
    profile = getattr(request.user, 'restaurant_profile', None)
    branch = (profile.branch if profile and profile.branch_id else None) or get_active_branch(request, restaurant)
    return restaurant, branch


def _active_orders(restaurant, branch):
    """Open dine-in orders: not cancelled / completed, not served-and-paid, within the service window."""
    cutoff = timezone.now() - timezone.timedelta(hours=ACTIVE_WINDOW_HOURS)
    qs = (Order.objects.filter(restaurant=restaurant, table__isnull=False, order_time__gte=cutoff)
          .exclude(status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.COMPLETED])
          .exclude(payment_status=Order.PaymentStatus.FAILED)
          .exclude(status=Order.OrderStatus.SERVED, payment_status=Order.PaymentStatus.PAID)
          .select_related('waiter', 'cashier')
          .prefetch_related('items')
          .order_by('-order_time'))
    if branch:
        qs = qs.filter(branch=branch)
    return qs


def _pick_order(orders):
    """The order a table card represents: the latest sent order, else a draft that already has items."""
    sent = [o for o in orders if o.status != Order.OrderStatus.DRAFT]
    if sent:
        return sent[0]
    drafts = [o for o in orders if o.status == Order.OrderStatus.DRAFT and len(o.items.all())]
    return drafts[0] if drafts else None


def _state(order):
    if order is None:
        return 'free'
    if order.bill_requested_at:
        return 'bill'
    if order.status == Order.OrderStatus.DRAFT:
        return 'ordering'
    if order.status == Order.OrderStatus.SERVED:
        return 'served'
    if order.is_ready or order.status == Order.OrderStatus.READY:
        return 'ready'
    return 'kitchen'


def _person(user):
    if not user:
        return ''
    return (user.get_full_name() or user.username).strip()


@login_required
@user_passes_test(_is_staff)
def waiter_tables(request, restaurant_slug=None):
    restaurant, branch = _context(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    return render(request, 'restaurant_admin/waiter/tables.html', {
        'restaurant': restaurant,
        'restaurant_slug': restaurant.slug,
        'current_branch': branch,
        'is_waiter_screen': True,
        'active_menu': 'waiter_tables',
    })


@login_required
@user_passes_test(_is_staff)
def waiter_tables_api(request, restaurant_slug=None):
    restaurant, branch = _context(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)

    by_table = {}
    for o in _active_orders(restaurant, branch):
        by_table.setdefault(o.table_id, []).append(o)

    tables = Table.objects.filter(restaurant=restaurant, is_active=True).select_related('zone', 'master_table')
    if branch:
        tables = tables.filter(branch=branch)
    tables = tables.order_by('table_number', 'name')

    now = timezone.now()
    zones = {}
    mine = 0
    for t in tables:
        order = _pick_order(by_table.get(t.id, []))
        via_master = False
        if order is None and t.master_table_id:
            order = _pick_order(by_table.get(t.master_table_id, []))
            via_master = order is not None
        state = _state(order)
        row = {
            'id': t.id, 'name': t.name, 'number': t.table_number, 'capacity': t.capacity,
            'state': state, 'linked_to': t.master_table.name if t.master_table_id and t.master_table else '',
            'party_name': t.effective_party_name,
        }
        if order is not None:
            is_mine = order.waiter_id == request.user.id
            mine += 1 if is_mine and not via_master else 0
            row.update({
                'order_id': order.id,
                'order_number': order.order_number or f'#{order.id}',
                'total': float(order.total_amount or 0),
                'items': sum(i.quantity for i in order.items.all()),
                'covers': order.guest_count or 0,
                'minutes': max(0, int((now - order.order_time).total_seconds() // 60)) if order.order_time else 0,
                'waiter': _person(order.waiter),
                'mine': is_mine,
                'via_master': via_master,
            })
        zone_name = t.zone.name if t.zone_id and t.zone else 'Main Area'
        zones.setdefault(zone_name, []).append(row)

    data = [{'name': name, 'tables': rows} for name, rows in zones.items()]
    counts = {}
    for z in data:
        for r in z['tables']:
            counts[r['state']] = counts.get(r['state'], 0) + 1
    return JsonResponse({'success': True, 'zones': data, 'counts': counts, 'mine': mine,
                         'is_waiter': is_waiter(request.user)})


@login_required
@user_passes_test(_is_staff)
def waiter_open_table(request, pk, restaurant_slug=None):
    """Tap a table: continue its running order, or start a new one for this table."""
    restaurant, branch = _context(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    table = get_object_or_404(Table, pk=pk, restaurant=restaurant, is_active=True)
    if branch and table.branch_id and table.branch_id != branch.id:
        return redirect('restaurant_admin:waiter_tables', restaurant_slug=restaurant.slug)

    pos_url = reverse('restaurant_admin:order_create_manual', kwargs={'restaurant_slug': restaurant.slug})
    orders = list(_active_orders(restaurant, branch).filter(table_id__in=[t for t in (table.id, table.master_table_id) if t]))
    own = [o for o in orders if o.table_id == table.id]
    order = _pick_order(own)
    if order is None:
        # an empty draft someone opened on this table is reused instead of starting a second one
        order = next((o for o in own if o.status == Order.OrderStatus.DRAFT), None)
    if order is None and table.master_table_id:
        order = _pick_order([o for o in orders if o.table_id == table.master_table_id])
    if order is not None:
        return redirect(f'{pos_url}?{urlencode({"order_id": order.id})}')

    params = {'new': 1, 'table_id': table.id}
    covers = request.GET.get('covers', '')
    if str(covers).isdigit() and 0 < int(covers) <= 99:
        params['covers'] = int(covers)
    return redirect(f'{pos_url}?{urlencode(params)}')


@login_required
@user_passes_test(_is_staff)
@require_POST
def waiter_request_bill(request, pk, restaurant_slug=None):
    restaurant, branch = _context(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    order = get_object_or_404(Order, pk=pk, restaurant=restaurant)
    if order.status in (Order.OrderStatus.CANCELLED, Order.OrderStatus.COMPLETED, Order.OrderStatus.DRAFT):
        return JsonResponse({'success': False, 'error': 'Send the order to the kitchen before asking for the bill.'
                             if order.status == Order.OrderStatus.DRAFT else 'This bill is already closed.'}, status=400)
    first_time = order.bill_requested_at is None
    if first_time:
        order.bill_requested_at = timezone.now()
        order.save(update_fields=['bill_requested_at'])
        try:
            from notifications.service import notify_bill_request
            notify_bill_request(order, _person(request.user))
        except Exception:
            pass
        try:
            from core.sse import publish
            publish(restaurant.slug, {'type': 'table_status_update', 'order_id': order.id,
                                      'table_id': order.table_id, 'status': 'BILL_REQUESTED'})
        except Exception:
            pass
    return JsonResponse({'success': True, 'already': not first_time,
                         'message': 'The cashier has been asked for the bill.'})
