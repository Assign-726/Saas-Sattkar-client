"""Import / export endpoints for Stock & Supply Chain (logic in stock_io; files via menu_io)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.models import DeliveryChallan, InventoryTransaction, StockCount
from restaurant_admin import menu_io as mio
from restaurant_admin import stock_io as sio
from restaurant_admin import views_controls as ctl
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant
from super_admin.activity import log_activity
from super_admin.models import Restaurant


def _scope(request, slug):
    restaurant = resolve_restaurant(request, slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant, None
    return restaurant, get_active_branch(request, restaurant)


def _fmt(request):
    return 'csv' if request.GET.get('format') == 'csv' else 'xlsx'


def _log(request, restaurant, branch, action, message, extra=None):
    log_activity(request, action=action, message=message, area='restaurant', restaurant=restaurant, branch=branch,
                 object_type='Inventory', extra=extra or {})


def _export(request, restaurant, branch, what, columns, rows, label, **kw):
    rows = list(rows)
    _log(request, restaurant, branch, 'EXPORT', f'Exported {len(rows)} {label} ({_fmt(request).upper()})')
    return mio.file_response(restaurant, what, columns, rows, _fmt(request), **kw)


def _import(request, restaurant, branch, columns, plan_fn, apply_fn, label, required=('name',)):
    """Preview (step=preview) or save (step=apply) an uploaded sheet; JSON for the import window."""
    mode = request.POST.get('mode', 'upsert')
    if mode not in ('upsert', 'create', 'update'):
        mode = 'upsert'
    try:
        rows = mio.read_rows(request.FILES.get('file'), columns, required=required)
    except mio.ImportFileError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
    if not rows:
        return JsonResponse({'success': False, 'error': 'The file has headings but no rows to import.'}, status=400)
    result = plan_fn(rows, mode)
    plan, new_cats = result if isinstance(result, tuple) else (result, [])
    summary = mio.summarize(plan)
    if request.POST.get('step') != 'apply':
        return JsonResponse({'success': True, 'step': 'preview', 'total': len(plan), 'summary': summary,
                             'new_categories': new_cats, 'rows': mio.preview_rows(plan),
                             'shown': min(len(plan), mio.PREVIEW_ROWS)})
    if not (summary['create'] or summary['update']):
        return JsonResponse({'success': False, 'error': 'Nothing to import — every row is unchanged, skipped or has an error.'},
                            status=400)
    counts = apply_fn(plan)
    parts = []
    if counts.get('create'):
        parts.append(f"{counts['create']} added")
    if counts.get('update'):
        parts.append(f"{counts['update']} updated")
    if counts.get('categories'):
        parts.append(f"{counts['categories']} new categor{'y' if counts['categories'] == 1 else 'ies'}")
    if summary['error']:
        parts.append(f"{summary['error']} row{'s' if summary['error'] != 1 else ''} with errors not imported")
    text = f'{label} imported: ' + ', '.join(parts) + '.'
    messages.success(request, text)
    _log(request, restaurant, branch, 'IMPORT', text, {'file': getattr(request.FILES.get('file'), 'name', ''), 'mode': mode,
                                                       **counts, 'errors': summary['error']})
    return JsonResponse({'success': True, 'step': 'done', 'counts': counts, 'message': text})


def guarded(view, method):
    view = login_required(user_passes_test(is_admin)(method(view)))
    return view


# ── stock items ───────────────────────────────────────────────────────────

def _inventory_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return mio.file_response(restaurant, 'stock-items-template', sio.ITEM_COLUMNS, sio.ITEM_SAMPLE, _fmt(request),
                                 template=True, lists=sio.ITEM_LISTS, note=sio.ITEM_NOTE)
    items = sio.items_scope(restaurant, branch).select_related('category', 'category__parent')
    q = request.GET.get('q', '').strip()
    if q:  # same search as the Inventory page
        items = items.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(category__name__icontains=q) |
                             Q(hsn_sac_code__icontains=q) | Q(category__hsn_sac_code__icontains=q)).distinct()
    return _export(request, restaurant, branch, 'stock-items', sio.ITEM_COLUMNS, sio.item_export_rows(items.order_by('name')),
                   'stock items', lists=sio.ITEM_LISTS, note=sio.ITEM_NOTE)


def _inventory_import(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    return _import(request, restaurant, branch, sio.ITEM_COLUMNS,
                   lambda rows, mode: sio.plan_items(rows, restaurant, branch, mode),
                   lambda plan: sio.apply_items(plan, restaurant, branch, request.user), 'Stock items')


# ── stock categories ──────────────────────────────────────────────────────

def _category_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return mio.file_response(restaurant, 'stock-categories-template', sio.CATEGORY_COLUMNS, sio.CATEGORY_SAMPLE,
                                 _fmt(request), template=True, lists=sio.CATEGORY_LISTS, note=sio.CATEGORY_NOTE)
    return _export(request, restaurant, branch, 'stock-categories', sio.CATEGORY_COLUMNS,
                   sio.category_export_rows(restaurant, branch), 'stock categories', lists=sio.CATEGORY_LISTS,
                   note=sio.CATEGORY_NOTE)


def _category_import(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    return _import(request, restaurant, branch, sio.CATEGORY_COLUMNS,
                   lambda rows, mode: sio.plan_categories(rows, restaurant, branch, mode),
                   lambda plan: sio.apply_categories(plan, restaurant, branch, request.user), 'Stock categories')


# ── wastage ───────────────────────────────────────────────────────────────

def _wastage_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return mio.file_response(restaurant, 'wastage-template', sio.WASTAGE_COLUMNS, sio.WASTAGE_SAMPLE, _fmt(request),
                                 template=True, lists=sio.WASTAGE_LISTS, note=sio.WASTAGE_NOTE)
    key, label, start, end = ctl._period(request)  # same period as the Wastage page
    s_dt, e_dt = ctl.M._bounds(start, end)
    logs = (InventoryTransaction.objects.filter(transaction_type='W', inventory_item__in=sio.items_scope(restaurant, branch),
                                                created_at__gte=s_dt, created_at__lt=e_dt)
            .select_related('inventory_item', 'performed_by').order_by('-created_at'))
    return _export(request, restaurant, branch, f'wastage-{start}-to-{end}', sio.WASTAGE_EXPORT_COLUMNS,
                   sio.wastage_export_rows(logs), 'wastage entries', lists=sio.WASTAGE_LISTS,
                   note=sio.WASTAGE_NOTE + ' Importing an exported file logs the wastage again.')


def _wastage_import(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    return _import(request, restaurant, branch, sio.WASTAGE_COLUMNS,
                   lambda rows, mode: sio.plan_wastage(rows, restaurant, branch),
                   lambda plan: sio.apply_wastage(plan, restaurant, branch, request.user), 'Wastage',
                   required=('name', 'sku'))


# ── stock counts ──────────────────────────────────────────────────────────

def _count_for(request, restaurant_slug, pk):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant, None, None
    qs = StockCount.objects.filter(restaurant=restaurant)
    return restaurant, branch, qs.filter(pk=pk).first()


def _count_export(request, pk, restaurant_slug=None):
    restaurant, branch, count = _count_for(request, restaurant_slug, pk)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if not count:
        from django.http import Http404
        raise Http404
    return _export(request, restaurant, branch, f'stock-count-{count.pk}', sio.COUNT_COLUMNS, sio.count_export_rows(count),
                   f'stock count lines ({count.title})', note=sio.COUNT_NOTE)


def _count_import(request, pk, restaurant_slug=None):
    restaurant, branch, count = _count_for(request, restaurant_slug, pk)
    if not isinstance(restaurant, Restaurant) or not count:
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    if count.status != StockCount.Status.DRAFT:
        return JsonResponse({'success': False, 'error': 'This count is already closed — start a new count to import.'},
                            status=400)
    return _import(request, restaurant, branch, sio.COUNT_COLUMNS, lambda rows, mode: sio.plan_count(rows, count),
                   lambda plan: sio.apply_count(plan, count), f'Counted quantities for “{count.title}”',
                   required=('name', 'sku'))


# ── exports only ──────────────────────────────────────────────────────────

def _logs_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    qs = InventoryTransaction.objects.filter(inventory_item__in=sio.items_scope(restaurant, branch))
    q, kind = request.GET.get('q', '').strip(), request.GET.get('type', '').strip()
    from_date, to_date = request.GET.get('from_date', '').strip(), request.GET.get('to_date', '').strip()
    if q:  # same filters as the Inventory Logs page
        qs = qs.filter(Q(inventory_item__name__icontains=q) | Q(performed_by__username__icontains=q))
    if kind:
        qs = qs.filter(transaction_type=kind)
    try:
        if from_date:
            qs = qs.filter(created_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(created_at__date__lte=to_date)
    except Exception:
        pass
    qs = qs.select_related('inventory_item', 'order', 'purchase_order', 'performed_by').order_by('-created_at')[:50000]
    return _export(request, restaurant, branch, 'inventory-logs', sio.as_columns(sio.LOG_HEADERS), sio.log_export_rows(qs),
                   'inventory log rows', note='Every stock movement: purchases, sales, wastage, counts and adjustments.')


def _challan_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    qs = DeliveryChallan.objects.filter(restaurant=restaurant).select_related('from_branch', 'to_branch') \
        .prefetch_related('items__inventory_item', 'items__menu_item').order_by('-dispatch_date', '-created_at')
    status, q = request.GET.get('status', '').strip(), request.GET.get('q', '').strip()
    if status:  # same filters as the Delivery Challans page
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(challan_no__icontains=q) | Q(receiver_name__icontains=q) | Q(receiver_gstin__icontains=q))
    summary, lines = sio.challan_export(qs)
    _log(request, restaurant, branch, 'EXPORT', f'Exported {len(summary)} delivery challans ({_fmt(request).upper()})')
    if _fmt(request) == 'csv':  # CSV is one table: one row per challan line
        return mio.file_response(restaurant, 'delivery-challan-lines', sio.as_columns(sio.CHALLAN_LINE_HEADERS), lines, 'csv')
    return mio.file_response(restaurant, 'delivery-challans', sio.as_columns(sio.CHALLAN_HEADERS), summary, 'xlsx',
                             note='Sheet 1 lists the challans; the “Items” sheet lists every line.',
                             extra_sheets=[('Items', sio.CHALLAN_LINE_HEADERS, lines)])


# ── parties ───────────────────────────────────────────────────────────────

def _party_export(request, restaurant_slug=None):
    from restaurant_admin import party_io as pio
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return mio.file_response(restaurant, 'parties-template', pio.COLUMNS, pio.SAMPLE, _fmt(request), template=True,
                                 lists=pio.LISTS, note=pio.NOTE)
    parties = pio.scope(restaurant, branch)
    tab, q = request.GET.get('tab', 'all'), request.GET.get('q', '').strip()
    if tab in ('vendor', 'customer'):  # same tabs and search as the Parties page
        parties = parties.filter(party_type__in=[tab, 'both'])
    if q:
        parties = parties.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(gstin__icontains=q))
    return _export(request, restaurant, branch, 'parties', pio.COLUMNS, pio.export_rows(parties.order_by('name')), 'parties',
                   lists=pio.LISTS, note=pio.NOTE)


def _party_import(request, restaurant_slug=None):
    from restaurant_admin import party_io as pio
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    return _import(request, restaurant, branch, pio.COLUMNS, lambda rows, mode: pio.plan(rows, restaurant, branch, mode),
                   lambda plan: pio.apply(plan, restaurant, branch, request.user), 'Parties')


party_export = guarded(_party_export, require_GET)
party_import = guarded(_party_import, require_POST)
inventory_export = guarded(_inventory_export, require_GET)
inventory_import = guarded(_inventory_import, require_POST)
inventory_category_export = guarded(_category_export, require_GET)
inventory_category_import = guarded(_category_import, require_POST)
inventory_wastage_export = guarded(_wastage_export, require_GET)
inventory_wastage_import = guarded(_wastage_import, require_POST)
inventory_count_export = guarded(_count_export, require_GET)
inventory_count_import = guarded(_count_import, require_POST)
inventory_logs_export = guarded(_logs_export, require_GET)
challan_export = guarded(_challan_export, require_GET)
