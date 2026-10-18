"""Import / export endpoints for the Menu Items and Menu Categories pages (logic in menu_io)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from restaurant_admin import menu_io as io
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant
from super_admin.activity import log_activity
from super_admin.models import Restaurant


def _scope(request, restaurant_slug):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant, None
    return restaurant, get_active_branch(request, restaurant)


def _fmt(request):
    return 'csv' if request.GET.get('format') == 'csv' else 'xlsx'


def _filtered_menu(request, restaurant, branch):
    """The same filters as the Menu Items page, so Export gives what is on screen."""
    qs = io.menu_scope(restaurant, branch).select_related('category', 'subcategory')
    q = request.GET.get('q', '').strip()
    cat = request.GET.get('category', '').strip()
    status = request.GET.get('status', '').strip()
    if cat.isdigit():
        qs = qs.filter(Q(category_id=cat) | Q(subcategory__category_id=cat))
    if status == 'available':
        qs = qs.filter(availability=True)
    elif status == 'unavailable':
        qs = qs.filter(availability=False)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(short_code__icontains=q) | Q(description__icontains=q) |
                       Q(subcategory__name__icontains=q) | Q(category__name__icontains=q))
    return qs.order_by('category__name', 'subcategory__name', 'name')


def _log(request, restaurant, branch, action, message, extra=None):
    log_activity(request, action=action, message=message, area='restaurant', restaurant=restaurant, branch=branch,
                 object_type='MenuItem' if 'item' in message else 'Category', extra=extra or {})


def _run_import(request, restaurant_slug, columns, plan_fn, apply_fn, label):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Not allowed'}, status=403)
    mode = request.POST.get('mode', 'upsert')
    if mode not in ('upsert', 'create', 'update'):
        mode = 'upsert'
    try:
        rows = io.read_rows(request.FILES.get('file'), columns)
    except io.ImportFileError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
    if not rows:
        return JsonResponse({'success': False, 'error': 'The file has headings but no rows to import.'}, status=400)
    result = plan_fn(rows, restaurant, branch, mode)
    plan, new_cats = result if isinstance(result, tuple) else (result, [])
    summary = io.summarize(plan)
    if request.POST.get('step') != 'apply':
        return JsonResponse({'success': True, 'step': 'preview', 'total': len(plan), 'summary': summary,
                             'new_categories': new_cats, 'rows': io.preview_rows(plan),
                             'shown': min(len(plan), io.PREVIEW_ROWS)})
    if not (summary['create'] or summary['update']):
        return JsonResponse({'success': False, 'error': 'Nothing to import — every row is unchanged, skipped or has an error.'},
                            status=400)
    counts = apply_fn(plan, restaurant, branch)
    parts = [f"{counts['create']} added", f"{counts['update']} updated"]
    if counts.get('categories'):
        parts.append(f"{counts['categories']} new categor{'y' if counts['categories'] == 1 else 'ies'}")
    if counts.get('subcategories'):
        parts.append(f"{counts['subcategories']} new subcategor{'y' if counts['subcategories'] == 1 else 'ies'}")
    if summary['error']:
        parts.append(f"{summary['error']} row{'s' if summary['error'] != 1 else ''} with errors not imported")
    text = f"{label} imported: " + ', '.join(parts) + '.'
    messages.success(request, text)
    _log(request, restaurant, branch, 'IMPORT', text, {'file': getattr(request.FILES.get('file'), 'name', ''),
                                                       'mode': mode, **counts, 'errors': summary['error']})
    return JsonResponse({'success': True, 'step': 'done', 'counts': counts, 'message': text})


@login_required
@user_passes_test(is_admin)
@require_GET
def menu_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return io.file_response(restaurant, 'menu-items-template', io.MENU_COLUMNS, io.MENU_SAMPLE, _fmt(request), template=True)
    items = _filtered_menu(request, restaurant, branch)
    rows = list(io.menu_export_rows(items))
    _log(request, restaurant, branch, 'EXPORT', f'Exported {len(rows)} menu items ({_fmt(request).upper()})')
    return io.file_response(restaurant, 'menu-items', io.MENU_COLUMNS, rows, _fmt(request))


@login_required
@user_passes_test(is_admin)
@require_POST
def menu_import(request, restaurant_slug=None):
    return _run_import(request, restaurant_slug, io.MENU_COLUMNS, io.plan_menu, io.apply_menu, 'Menu items')


@login_required
@user_passes_test(is_admin)
@require_GET
def category_export(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    if request.GET.get('template'):
        return io.file_response(restaurant, 'menu-categories-template', io.CATEGORY_COLUMNS, io.CATEGORY_SAMPLE,
                                _fmt(request), template=True)
    cats = io.categories_for_export(restaurant, branch)
    q = request.GET.get('q', '').strip()
    if q:
        cats = cats.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(subcategories__name__icontains=q)).distinct()
    rows = list(io.category_export_rows(cats))
    _log(request, restaurant, branch, 'EXPORT', f'Exported {len(rows)} menu categories ({_fmt(request).upper()})')
    return io.file_response(restaurant, 'menu-categories', io.CATEGORY_COLUMNS, rows, _fmt(request))


@login_required
@user_passes_test(is_admin)
@require_POST
def category_import(request, restaurant_slug=None):
    return _run_import(request, restaurant_slug, io.CATEGORY_COLUMNS, io.plan_categories, io.apply_categories,
                       'Menu categories')
