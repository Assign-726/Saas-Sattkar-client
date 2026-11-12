"""JSON endpoints behind the notification drawer in the restaurant admin."""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST

from notifications import service as S

PAGE = 30


def _scope(request, restaurant_slug):
    from restaurant_admin.views import get_active_branch, resolve_restaurant
    from super_admin.models import Restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return None, None
    return restaurant, get_active_branch(request, restaurant)


def _module(request):
    """The hotel shell sees room and channel notifications; kitchen logins see kitchen tickets;
    the restaurant shell sees the rest."""
    profile = getattr(request.user, 'restaurant_profile', None)
    if profile is not None and profile.role == 'KITCHEN' and not request.user.is_superuser:
        return 'kitchen'  # decided by role, never by a query parameter
    if profile is not None and profile.role == 'WAITER' and not request.user.is_superuser:
        return 'waiter'
    match = getattr(request, 'resolver_match', None)
    if (match and match.namespace == 'hotel_admin') or 'hotel' in (request.GET.get('module'), request.POST.get('module')):
        return 'hotel'
    return 'restaurant'


def _sound_url(request, restaurant):
    from core.models import RestaurantSettings
    try:
        s = RestaurantSettings.get_settings(restaurant)
        if s and s.notification_sound and s.notification_sound.name:
            ts = int(s.updated_at.timestamp()) if getattr(s, 'updated_at', None) else int(datetime.now().timestamp())
            return f'{request.build_absolute_uri(s.notification_sound.url)}?v={ts}'
    except Exception:
        pass
    return ''


@login_required
def poll(request, restaurant_slug=None):
    """Called every ~12 s: what arrived since ``since`` (a notification id) and the unread count."""
    restaurant, branch = _scope(request, restaurant_slug)
    if restaurant is None:
        return JsonResponse({'success': False}, status=403)
    qs = S.visible(request.user, restaurant, branch, _module(request))
    latest = qs.values_list('id', flat=True).order_by('-id').first() or 0
    try:
        since = int(request.GET.get('since') or request.GET.get('since_id') or 0)
    except ValueError:
        since = 0
    new = []
    module = _module(request)
    if since and latest > since:
        new = [S.serialize(n) for n in qs.filter(id__gt=since, archived_at__isnull=True).order_by('id')[:10]]
        if module != 'kitchen':
            for n in new:  # the cashier fired it: show the KOT, but only the kitchen gets the chime
                if n['kind'] == 'kot_new':
                    n['sound'] = False
    data = {'success': True, 'latest_id': latest, 'unread': S.unread_count(qs), 'new': new}
    if not since or new:
        data['sound_url'] = _sound_url(request, restaurant)
    return JsonResponse(data)


@login_required
def stream(request, restaurant_slug=None):
    """Server-Sent Events stream for real-time notification alerts (orders, tickets, system alerts)."""
    restaurant, branch = _scope(request, restaurant_slug)
    if restaurant is None:
        return HttpResponse('Forbidden', status=403)
    slug = getattr(restaurant, 'slug', None)
    if not slug:
        return HttpResponse('Missing restaurant slug', status=400)

    from core.sse import stream_response
    return stream_response(request, slug, 'notification')


@login_required
def listing(request, restaurant_slug=None):
    restaurant, branch = _scope(request, restaurant_slug)
    if restaurant is None:
        return JsonResponse({'success': False}, status=403)
    tab = request.GET.get('tab', 'inbox')
    cat = request.GET.get('cat', 'all')
    base = S.visible(request.user, restaurant, branch, _module(request))
    qs = S.filtered(base, tab, cat).select_related(None)
    before = request.GET.get('before', '')
    if before.isdigit():
        qs = qs.filter(id__lt=int(before))
    rows = list(qs.order_by('-id')[:PAGE + 1])
    live = base.filter(archived_at__isnull=True)
    return JsonResponse({
        'success': True, 'items': [S.serialize(n) for n in rows[:PAGE]], 'has_more': len(rows) > PAGE,
        'counts': {'unread': S.unread_count(base), 'inbox': live.count(), 'archived': base.filter(archived_at__isnull=False).count()},
    })


@login_required
@require_POST
def update(request, restaurant_slug=None):
    """op = read | unread | archive | unarchive (with ids=1,2,3) · read_all | archive_read (for the current tab/category)."""
    restaurant, branch = _scope(request, restaurant_slug)
    if restaurant is None:
        return JsonResponse({'success': False}, status=403)
    op = request.POST.get('op', '')
    base = S.visible(request.user, restaurant, branch, _module(request))
    if op in ('read_all', 'archive_read'):
        cat = request.POST.get('cat', 'all')
        if op == 'read_all':
            ids = S.filtered(base, 'unread', cat).values_list('id', flat=True)
            n = S.set_state(request.user, ids, read=True)
        else:
            ids = S.filtered(base, 'read', cat).values_list('id', flat=True)
            n = S.set_state(request.user, ids, archived=True)
    else:
        raw = [i for i in request.POST.get('ids', '').split(',') if i.strip().isdigit()][:200]
        ids = list(base.filter(id__in=[int(i) for i in raw]).values_list('id', flat=True))  # only notifications this user can see
        changes = {'read': {'read': True}, 'unread': {'read': False}, 'archive': {'archived': True}, 'unarchive': {'archived': False}}.get(op)
        if changes is None:
            return JsonResponse({'success': False, 'error': 'unknown op'}, status=400)
        n = S.set_state(request.user, ids, **changes)
    return JsonResponse({'success': True, 'changed': n, 'unread': S.unread_count(S.visible(request.user, restaurant, branch, _module(request)))})
