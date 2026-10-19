"""Activity Logs screen shared by Restaurant Admin, Hotel PMS and Super Admin.

Each admin gets a thin wrapper view that decides *which* logs the viewer may
see (``base_qs``); ``logs_page`` does filtering, KPIs, CSV export and every
delete mode (single, selected, current page, all matching filters, retention).
"""
import csv
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, QueryDict
from django.shortcuts import redirect, render
from django.utils import timezone

from super_admin.activity import KEEP_DAYS, TRIGGER_DAYS, log_activity, purge_activity_logs
from super_admin.context_processors import can_view_logs
from super_admin.models import ActivityLog

PER_PAGE_OPTIONS = (25, 50, 100, 200)
FILTER_KEYS = ('q', 'action', 'area', 'outcome', 'user', 'date_from', 'date_to')

ACTION_TONE = {
    'CREATE': 'green', 'UPDATE': 'blue', 'DELETE': 'red', 'STATUS': 'indigo', 'EXPORT': 'teal',
    'IMPORT': 'teal', 'SETTINGS': 'gray', 'SECURITY': 'orange', 'LOGIN': 'green', 'LOGOUT': 'gray',
    'LOGIN_FAILED': 'red', 'OTHER': 'gray',
}
ACTION_ICON = {
    'CREATE': 'fa-plus', 'UPDATE': 'fa-pen', 'DELETE': 'fa-trash-can', 'STATUS': 'fa-arrows-rotate',
    'EXPORT': 'fa-file-arrow-down', 'IMPORT': 'fa-file-arrow-up', 'SETTINGS': 'fa-sliders',
    'SECURITY': 'fa-shield-halved', 'LOGIN': 'fa-right-to-bracket', 'LOGOUT': 'fa-right-from-bracket',
    'LOGIN_FAILED': 'fa-triangle-exclamation', 'OTHER': 'fa-circle-dot',
}


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def apply_filters(qs, params):
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(message__icontains=q) | Q(username__icontains=q) | Q(path__icontains=q)
                       | Q(ip_address__icontains=q) | Q(object_repr__icontains=q))
    if params.get('action') in ActivityLog.Action.values:
        qs = qs.filter(action=params['action'])
    if params.get('area') in ActivityLog.Area.values:
        qs = qs.filter(area=params['area'])
    if params.get('outcome') in ActivityLog.Outcome.values:
        qs = qs.filter(outcome=params['outcome'])
    if params.get('user'):
        qs = qs.filter(username=params['user'])
    tz = timezone.get_current_timezone()
    start, end = _parse_date(params.get('date_from')), _parse_date(params.get('date_to'))
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        qs = qs.filter(created_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))
    return qs


def _ids(values):
    return [int(v) for v in values if str(v).isdigit()][:1000]


def _export_csv(qs, filename):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(['Time', 'User', 'Role', 'Business', 'Branch', 'Area', 'Action', 'Outcome',
                     'Activity', 'Method', 'Path', 'Status', 'IP', 'Device'])
    for log in qs.select_related('restaurant', 'branch').iterator(chunk_size=500):
        writer.writerow([
            timezone.localtime(log.created_at).strftime('%Y-%m-%d %H:%M:%S'), log.username, log.role,
            log.restaurant.name if log.restaurant else '', log.branch.name if log.branch else '',
            log.get_area_display(), log.get_action_display(), log.get_outcome_display(), log.message,
            log.method, log.path, log.status_code or '', log.ip_address, log.device,
        ])
    return response


def logs_page(request, *, base_qs, template, area, areas, extra_context=None, restaurant=None, export_name='activity-logs'):
    """Render the Activity Logs screen and handle its actions."""
    if request.method == 'POST':
        return _handle_post(request, base_qs, area, restaurant)

    params = request.GET
    qs = apply_filters(base_qs, params)

    if params.get('export') == 'csv':
        log_activity(request, action='EXPORT', message=f'Exported {qs.count()} activity logs', area=area, restaurant=restaurant)
        return _export_csv(qs, f'{export_name}-{timezone.localdate():%Y%m%d}.csv')

    try:
        per_page = int(params.get('per_page', 50))
    except ValueError:
        per_page = 50
    per_page = per_page if per_page in PER_PAGE_OPTIONS else 50
    page_obj = Paginator(qs.select_related('restaurant', 'branch'), per_page).get_page(params.get('page'))
    for log in page_obj:
        log.tone = ACTION_TONE.get(log.action, 'gray')
        log.icon = ACTION_ICON.get(log.action, 'fa-circle-dot')
        log.role_label = (log.role or '').replace('_', ' ').title() or '—'

    now = timezone.now()
    today_start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min), timezone.get_current_timezone())
    oldest = base_qs.order_by('created_at').values_list('created_at', flat=True).first()
    filters = {k: params.get(k, '') for k in FILTER_KEYS}
    keep = {k: v for k, v in filters.items() if v}
    if per_page != 50:
        keep['per_page'] = per_page

    context = {
        'logs': page_obj,
        'page_obj': page_obj,
        'filters': filters,
        'has_filters': any(filters.values()),
        'filter_qs': urlencode(keep),
        'filter_qs_amp': ('&' + urlencode(keep)) if keep else '',
        'per_page': per_page,
        'per_page_options': PER_PAGE_OPTIONS,
        'action_choices': ActivityLog.Action.choices,
        'area_choices': [c for c in ActivityLog.Area.choices if c[0] in areas],
        'outcome_choices': ActivityLog.Outcome.choices,
        'user_choices': list(base_qs.exclude(username='').order_by('username').values_list('username', flat=True).distinct()[:200]),
        'stats': {
            'total': base_qs.count(),
            'today': base_qs.filter(created_at__gte=today_start).count(),
            'week': base_qs.filter(created_at__gte=now - timedelta(days=7)).count(),
            'failed': base_qs.filter(created_at__gte=now - timedelta(days=7)).filter(
                Q(outcome='failed') | Q(action='LOGIN_FAILED')).count(),
            'matching': page_obj.paginator.count,
        },
        'retention': {
            'trigger_days': TRIGGER_DAYS,
            'keep_days': KEEP_DAYS,
            'trigger_months': round(TRIGGER_DAYS / 30),
            'keep_months': round(KEEP_DAYS / 30),
            'oldest': oldest,
            'next_cleanup': (oldest + timedelta(days=TRIGGER_DAYS)) if oldest else None,
            'purgeable': base_qs.filter(created_at__lt=now - timedelta(days=KEEP_DAYS)).count(),
        },
        'logs_area': area,
    }
    context.update(extra_context or {})
    return render(request, template, context)


def _handle_post(request, base_qs, area, restaurant):
    op = request.POST.get('op', '')
    back_qs = request.POST.get('qs', '')
    if op == 'delete_one':
        qs = base_qs.filter(id__in=_ids([request.POST.get('id')]))
        label = 'log'
    elif op in ('delete_selected', 'delete_page'):
        qs = base_qs.filter(id__in=_ids(request.POST.getlist('ids')))
        label = 'selected logs' if op == 'delete_selected' else 'logs on this page'
    elif op == 'delete_filtered':
        params = QueryDict(back_qs)
        if not any(params.get(k) for k in FILTER_KEYS):
            messages.error(request, 'Apply at least one filter before deleting all matching logs.')
            return redirect(f'{request.path}?{back_qs}')
        qs = apply_filters(base_qs, params)
        label = 'logs matching the filters'
    elif op == 'purge_now':
        deleted = purge_activity_logs(base_qs, force=True)
        if deleted:
            log_activity(request, action='DELETE', message=f'Ran log cleanup · removed {deleted} logs older than {KEEP_DAYS} days',
                         area=area, restaurant=restaurant)
            messages.success(request, f'Cleanup complete — removed {deleted} logs older than {KEEP_DAYS} days.')
        else:
            messages.info(request, f'Nothing to clean up — no logs older than {KEEP_DAYS} days.')
        return redirect(request.path)
    else:
        messages.error(request, 'Unknown action.')
        return redirect(f'{request.path}?{back_qs}')

    deleted, _ = qs.delete()
    if deleted:
        log_activity(request, action='DELETE', message=f'Deleted {deleted} activity logs ({label})', area=area, restaurant=restaurant)
        messages.success(request, f'Deleted {deleted} activity log{"s" if deleted != 1 else ""}.')
    else:
        messages.info(request, 'No logs were deleted.')
    return redirect(f'{request.path}?{back_qs}' if back_qs else request.path)


# --------------------------------------------------------------------------
# Admin-specific wrappers
# --------------------------------------------------------------------------

@login_required
def restaurant_activity_logs(request, restaurant_slug=None):
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    if not request.user.is_staff or not can_view_logs(request.user, 'restaurant'):
        return HttpResponseForbidden('Activity logs are available to business administrators only.')
    from super_admin.activity import user_role
    full_view = request.user.is_superuser or user_role(request.user) == 'GENERAL_MANAGER'
    areas = ('restaurant', 'hotel', 'auth') if full_view else ('restaurant', 'auth')
    base_qs = ActivityLog.objects.filter(restaurant=restaurant, area__in=areas)
    return logs_page(
        request, base_qs=base_qs, template='restaurant_admin/activity_logs.html', area='restaurant',
        areas=areas, restaurant=restaurant, export_name=f'{restaurant.slug}-activity',
        extra_context={'restaurant': restaurant, 'active_menu': 'activity_logs'},
    )


def hotel_activity_logs_view(request, restaurant_slug, ctx):
    if not ctx.get('is_hotel_admin') or not can_view_logs(request.user, 'hotel'):
        return HttpResponseForbidden('Activity logs are available to hotel administrators only.')
    restaurant = ctx['restaurant']
    areas = ('hotel', 'auth')
    base_qs = ActivityLog.objects.filter(restaurant=restaurant, area__in=areas)
    return logs_page(
        request, base_qs=base_qs, template='hotel_admin/activity_logs.html', area='hotel', areas=areas,
        restaurant=restaurant, export_name=f'{restaurant.slug}-hotel-activity',
        extra_context={**ctx, 'active_menu': 'activity_logs'},
    )


def super_activity_logs(request):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return redirect('login')
    areas = tuple(ActivityLog.Area.values)
    return logs_page(
        request, base_qs=ActivityLog.objects.all(), template='super_admin/activity_logs.html', area='super',
        areas=areas, export_name='platform-activity',
        extra_context={'show_business_column': True},
    )
