"""Activity logging for every admin (Restaurant, Hotel PMS, Super Admin).

* ``ActivityLogMiddleware`` records each state-changing request made in an
  admin area (create / update / delete / settle / export ...).
* Auth signals record sign-in, sign-out and failed sign-in attempts.
* ``purge_activity_logs`` applies the retention rule: once the oldest log is
  older than ``ACTIVITY_LOG_TRIGGER_DAYS`` (default 90), everything older than
  ``ACTIVITY_LOG_KEEP_DAYS`` (default 30) is deleted — i.e. at three months the
  older two months are removed and the latest month is kept, and so on.
"""
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)

TRIGGER_DAYS = int(getattr(settings, 'ACTIVITY_LOG_TRIGGER_DAYS', 90))
KEEP_DAYS = int(getattr(settings, 'ACTIVITY_LOG_KEEP_DAYS', 30))
PURGE_INTERVAL_SECONDS = 6 * 60 * 60

AREA_PREFIXES = (
    ('/dashboard/', 'restaurant'),
    ('/hotel-admin/', 'hotel'),
    ('/super/', 'super'),
)

# High-frequency background calls that would drown the log
NOISY_URL_NAMES = {
    'pos_draft_order_api', 'pos_sync_draft_items_api', 'order_notifications_poll',
    'kitchen_kds_orders_api', 'order_omni_search_api', 'detect_location_ip_api',
    'floor_plan_api', 'floor_plan_save_position_api', 'pos_active_shift_api',
    'pos_shift_summary_api', 'inventory_category_api', 'get_subcategories',
    'receipt_detail_api', 'club_floor_plan_api', 'club_ticket_grid_api',
    'api_branches_by_restaurant', 'api_gstins_by_state', 'api_billing_addons_catalog',
    'api_billing_addon_quote', 'pos_counter_list', 'switch_branch',
}

# Friendlier wording for the most important operations
LABELS = {
    'pos_finalize_order_api': ('STATUS', 'Finalized POS order'),
    'order_settle_api': ('STATUS', 'Settled order payment'),
    'order_credit_note_api': ('STATUS', 'Issued credit note'),
    'order_update_status': ('STATUS', 'Updated order status'),
    'pos_open_shift_api': ('STATUS', 'Opened cash register shift'),
    'pos_close_shift_api': ('STATUS', 'Closed cash register shift'),
    'pos_close_shift_id_api': ('STATUS', 'Closed cash register shift'),
    'booking_check_in': ('STATUS', 'Checked in guest'),
    'booking_check_out': ('STATUS', 'Checked out guest'),
    'kitchen_kds_action_api': ('STATUS', 'Updated kitchen ticket'),
    'kitchen_kds_item_toggle_api': ('STATUS', 'Updated kitchen item'),
    'receipt_cancel_api': ('STATUS', 'Cancelled payment receipt'),
    'receipt_create_api': ('CREATE', 'Created payment receipt'),
    'menu_quick_add_api': ('CREATE', 'Quick-added menu item'),
    'order_create_manual': ('CREATE', 'Created POS order'),
    'order_invoice': ('EXPORT', 'Printed invoice'),
    'order_invoice_thermal': ('EXPORT', 'Printed thermal invoice'),
    'order_kot': ('EXPORT', 'Printed KOT'),
    'order_kot_thermal': ('EXPORT', 'Printed KOT'),
    'inventory_quick_add_api': ('CREATE', 'Quick-added stock item'),
}

ACTION_TOKENS = (
    ('DELETE', ('delete', 'remove', 'destroy', 'void', 'purge')),
    ('EXPORT', ('export', 'download', 'csv', 'print', 'z_report', 'invoice')),
    ('IMPORT', ('import', 'bulk', 'upload', 'seed')),
    ('SECURITY', ('password', '2fa', 'otp', 'pin', 'permission', 'role')),
    ('SETTINGS', ('settings', 'setup', 'config', 'numbering', 'smtp')),
    ('CREATE', ('create', 'add', 'new', 'onboard', 'generate')),
    ('STATUS', ('toggle', 'status', 'approve', 'reject', 'close', 'open', 'settle',
                'check_in', 'checkin', 'check_out', 'checkout', 'mark', 'assign',
                'transfer', 'cancel', 'refund', 'pay', 'finalize', 'sync', 'regenerate')),
    ('UPDATE', ('edit', 'update', 'save', 'rename', 'adjust', 'change')),
)
VERBS = {
    'CREATE': 'Created', 'UPDATE': 'Updated', 'DELETE': 'Deleted', 'STATUS': 'Changed',
    'EXPORT': 'Exported', 'IMPORT': 'Imported', 'SETTINGS': 'Updated settings for',
    'SECURITY': 'Updated security for', 'OTHER': 'Action on',
}
STRIP_TOKENS = {'api', 'view', 'page', 'id'}
SAFE_FIELDS = ('name', 'title', 'code', 'username', 'order_number', 'invoice_no',
               'receipt_no', 'table_number', 'room_number', 'sku', 'email')


def classify(url_name):
    """Return (action, human message) for a URL name like ``coupon_delete``."""
    if url_name in LABELS:
        return LABELS[url_name]
    name = (url_name or '').lower()
    action = 'OTHER'
    for act, tokens in ACTION_TOKENS:
        if any(re.search(r'(^|_)%s(_|$)' % re.escape(t), name) for t in tokens):
            action = act
            break
    words = [w for w in name.split('_') if w and w not in STRIP_TOKENS]
    action_words = {t for _, tokens in ACTION_TOKENS for t in tokens}
    resource = ' '.join(w for w in words if w not in action_words) or name.replace('_', ' ')
    verb = VERBS[action]
    if action == 'EXPORT' and re.search(r'(^|_)(print|invoice|kot|receipt|slip|thermal)(_|$)', name):
        verb = 'Printed'
    return action, f"{verb} {resource}".strip()


def client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    if fwd:
        return fwd.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR') or '')[:45]


def describe_device(ua):
    ua = ua or ''
    os_name = next((n for k, n in (('iPad', 'iPad'), ('iPhone', 'iPhone'), ('Android', 'Android'),
                                   ('Mac OS X', 'macOS'), ('Windows', 'Windows'), ('Linux', 'Linux')) if k in ua), '')
    browser = next((n for k, n in (('Edg/', 'Edge'), ('Chrome/', 'Chrome'), ('Firefox/', 'Firefox'),
                                   ('Safari/', 'Safari')) if k in ua), '')
    return ' · '.join(p for p in (browser, os_name) if p) or ua[:60]


def user_role(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return ''
    if user.is_superuser:
        return 'SUPERUSER'
    profile = getattr(user, 'restaurant_profile', None)
    try:
        return getattr(profile, 'role', '') or ''
    except Exception:
        return ''


def _resolve_tenant(request, area, kwargs):
    """Find the Restaurant / Branch a request belongs to (best effort)."""
    from super_admin.models import Restaurant, RestaurantBranch
    restaurant = branch = None
    slug = kwargs.get('restaurant_slug')
    if slug:
        branch = RestaurantBranch.objects.filter(slug__iexact=slug).select_related('restaurant').first()
        restaurant = branch.restaurant if branch else Restaurant.objects.filter(slug=slug).first()
    if not restaurant and request.user.is_authenticated:
        profile = getattr(request.user, 'restaurant_profile', None)
        try:
            restaurant = profile.restaurant if profile else None
            branch = branch or (profile.branch if profile else None)
        except Exception:
            pass
    if restaurant and not branch:
        branch_id = request.session.get(f'active_branch_{restaurant.id}')
        if branch_id:
            branch = RestaurantBranch.objects.filter(id=branch_id, restaurant=restaurant).first()
    return restaurant, branch


def log_activity(request=None, *, action='OTHER', message='', area='', user=None, restaurant=None,
                 branch=None, outcome='success', status_code=None, object_type='', object_id='',
                 object_repr='', extra=None):
    """Write one ActivityLog row. Never raises."""
    from super_admin.models import ActivityLog
    try:
        user = user if user is not None else (getattr(request, 'user', None) if request else None)
        if user is not None and not getattr(user, 'is_authenticated', False):
            user = None
        ActivityLog.objects.create(
            user=user,
            username=(getattr(user, 'username', '') or (extra or {}).get('username', ''))[:150],
            role=user_role(user)[:40],
            restaurant=restaurant,
            branch=branch,
            area=area[:20],
            action=action,
            outcome=outcome,
            message=message[:255],
            method=(getattr(request, 'method', None) or '')[:10],
            path=(getattr(request, 'path', None) or '')[:255],
            status_code=status_code,
            object_type=object_type[:80],
            object_id=str(object_id or '')[:64],
            object_repr=object_repr[:200],
            ip_address=client_ip(request) if request is not None and hasattr(request, 'META') else '',
            device=describe_device(getattr(request, 'META', {}).get('HTTP_USER_AGENT', ''))[:120],
            extra=extra or {},
        )
    except Exception:  # logging must never break the request
        logger.exception('Could not write activity log')


def purge_activity_logs(queryset=None, now=None, force=False):
    """Apply the retention rule. Returns the number of rows deleted.

    Once the oldest log is older than TRIGGER_DAYS, delete everything older
    than KEEP_DAYS. ``force`` deletes older-than-KEEP_DAYS immediately.
    """
    from super_admin.models import ActivityLog
    qs = queryset if queryset is not None else ActivityLog.objects.all()
    now = now or timezone.now()
    oldest = qs.order_by('created_at').values_list('created_at', flat=True).first()
    if not oldest:
        return 0
    if not force and oldest > now - timedelta(days=TRIGGER_DAYS):
        return 0
    deleted, _ = qs.filter(created_at__lt=now - timedelta(days=KEEP_DAYS)).delete()
    return deleted


def maybe_auto_purge():
    """Run the global retention purge at most once every few hours."""
    try:
        if cache.add('activity_log_purge_lock', 1, PURGE_INTERVAL_SECONDS):
            deleted = purge_activity_logs()
            if deleted:
                logger.info('Activity log retention removed %s rows', deleted)
    except Exception:
        logger.exception('Activity log retention failed')


class ActivityLogMiddleware:
    """Record state-changing requests made inside the admin areas."""

    LOG_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self.record(request, response)
        except Exception:
            logger.exception('Activity log middleware error')
        maybe_auto_purge()
        return response

    def record(self, request, response):
        area = next((a for prefix, a in AREA_PREFIXES if request.path.startswith(prefix)), None)
        if not area or not getattr(request, 'user', None) or not request.user.is_authenticated:
            return
        match = getattr(request, 'resolver_match', None)
        url_name = match.url_name if match else ''
        if not url_name or url_name in NOISY_URL_NAMES or url_name.startswith('activity_log'):
            return
        action, message = classify(url_name)
        is_export_get = request.method == 'GET' and action == 'EXPORT'
        if request.method not in self.LOG_METHODS and not is_export_get:
            return

        status = response.status_code
        content_type = response.get('Content-Type', '')
        if status >= 400:
            outcome = 'failed'
        elif request.method == 'POST' and status == 200 and 'text/html' in content_type:
            outcome = 'invalid'  # form re-displayed — usually a validation error
        else:
            outcome = 'success'

        kwargs = dict(match.kwargs) if match else {}
        object_id = next((str(v) for k, v in kwargs.items() if k in ('pk', 'id') or k.endswith('_id')), '')
        object_repr = ''
        if request.method == 'POST':
            for field in SAFE_FIELDS:
                value = request.POST.get(field) if hasattr(request, 'POST') else None
                if value:
                    object_repr = str(value)[:200]
                    break
        if object_id:
            message = f'{message} #{object_id}'
        if object_repr:
            message = f'{message} · {object_repr}'

        restaurant, branch = _resolve_tenant(request, area, kwargs)
        log_activity(
            request, action=action, message=message[:1].upper() + message[1:], area=area,
            restaurant=restaurant, branch=branch, outcome=outcome, status_code=status,
            object_type=url_name, object_id=object_id, object_repr=object_repr,
        )


def _tenant_for_user(user):
    profile = getattr(user, 'restaurant_profile', None)
    try:
        return (profile.restaurant, profile.branch) if profile else (None, None)
    except Exception:
        return None, None


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    restaurant, branch = _tenant_for_user(user)
    log_activity(request, action='LOGIN', message='Signed in', area='auth', user=user,
                 restaurant=restaurant, branch=branch)


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    restaurant, branch = _tenant_for_user(user)
    log_activity(request, action='LOGOUT', message='Signed out', area='auth', user=user,
                 restaurant=restaurant, branch=branch)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get('username', '') or ''
    from django.contrib.auth import get_user_model
    known = get_user_model().objects.filter(username=username).first() if username else None
    restaurant, branch = _tenant_for_user(known) if known else (None, None)
    log_activity(request, action='LOGIN_FAILED', message=f'Failed sign-in for "{username[:60]}"',
                 area='auth', outcome='failed', restaurant=restaurant, branch=branch,
                 extra={'username': username[:150]})
