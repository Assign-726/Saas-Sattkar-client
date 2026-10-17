"""Integrations screens: channels, API keys, webhooks, delivery log and the developer guide.

Everything is per branch. Group managers can see every branch (or filter to one);
a branch-locked admin only ever sees and creates items for their own branch.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from integrations import dispatch
from integrations.models import (EVENTS, EVENT_KEYS, SCOPE_KEYS, SCOPES, ApiKey, Connection, ExternalBookingLink,
                                 ExternalOrderLink, WebhookDelivery, WebhookEndpoint)
from super_admin.activity import log_activity, user_role

MANAGE_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN', 'CLUB_ADMIN'}


def can_manage(user):
    return user.is_authenticated and (user.is_superuser or user_role(user) in MANAGE_ROLES)


# --------------------------------------------------------------------------
# Shell + branch scoping
# --------------------------------------------------------------------------

def _locked_branch(user):
    profile = getattr(user, 'restaurant_profile', None)
    return getattr(profile, 'branch', None) if profile else None


def _scope(request, restaurant):
    """Return (branches the user may use, selected branch or None for all, locked?)."""
    locked = _locked_branch(request.user)
    if locked:
        return [locked], locked, True
    branches = list(restaurant.branches.all().order_by('name'))
    wanted = request.GET.get('branch', '')
    if wanted == 'all':
        return branches, None, False
    if wanted.isdigit():
        chosen = next((b for b in branches if b.id == int(wanted)), None)
        return branches, chosen, False
    active = getattr(request, 'active_branch', None)
    if active and any(b.id == active.id for b in branches) and len(branches) > 1:
        return branches, active, False
    return branches, None, False


def _branch_from_post(request, branches, field='branch'):
    raw = request.POST.get(field, '')
    return next((b for b in branches if str(b.id) == raw), None)


def _restaurant_env(request, restaurant_slug):
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    if not request.user.is_staff or not can_manage(request.user):
        return HttpResponseForbidden('Integrations are managed by administrators.')
    return {'restaurant': restaurant, 'shell': 'guests/shell_restaurant.html', 'area': 'restaurant', 'ns': 'restaurant_admin',
            'slug': restaurant_slug, 'ctx': {'restaurant': restaurant}}


def _hotel_env(request, restaurant_slug, ctx):
    if not can_manage(request.user):
        return HttpResponseForbidden('Integrations are managed by administrators.')
    return {'restaurant': ctx['restaurant'], 'shell': 'guests/shell_hotel.html', 'area': 'hotel', 'ns': 'hotel_admin',
            'slug': restaurant_slug, 'ctx': ctx}


def _module(env):
    from super_admin import modules
    m = env['area']
    return {
        'providers': [(v, l) for v, l in Connection.Provider.choices if v in modules.PROVIDERS[m]],
        'scopes': [(v, l) for v, l in SCOPES if v in modules.API_SCOPES[m]],
        'events': [(v, l) for v, l in EVENTS if v in modules.WEBHOOK_EVENTS[m]],
    }


def _module_connections(env, qs):
    """Channels of this shell's module: food aggregators in the restaurant, OTAs & channel managers in the hotel."""
    from super_admin import modules
    return qs.filter(provider__in=modules.PROVIDERS[env['area']])


def _module_keys(env, qs):
    """Keys whose permissions touch this module."""
    from super_admin import modules
    wanted = set(modules.API_SCOPES[env['area']])
    return qs.filter(pk__in=[k.pk for k in qs if wanted & set(k.scopes or [])])


def _module_hooks(env, qs):
    from super_admin import modules
    wanted = set(modules.WEBHOOK_EVENTS[env['area']]) - {'feedback.received'}
    return qs.filter(pk__in=[h.pk for h in qs if wanted & set(h.events or [])] + [h.pk for h in qs if set(h.events or []) <= {'feedback.received'}])


def _url(env, name, **kw):
    return reverse(f'{env["ns"]}:{name}', kwargs={'restaurant_slug': env['slug'], **kw})


def _render(request, env, partial, context, tab):
    branches, selected, locked = _scope(request, env['restaurant'])
    nav = {t: _url(env, n) for t, n in (('overview', 'integrations'), ('keys', 'integration_keys'),
                                          ('webhooks', 'integration_webhooks'), ('docs', 'integration_docs'))}
    if env['area'] == 'hotel':
        nav['distribution'] = _url(env, 'integration_distribution')
    data = {**env['ctx'], **context, 'partial': partial, 'tab': tab, 'nav': nav, 'branches': branches,
            'selected_branch': selected, 'branch_locked': locked, 'active_menu': 'integrations',
            'page_title': 'Integrations', 'ns': env['ns'], 'slug': env['slug'], 'module': env['area'],
            'api_base': request.build_absolute_uri('/api/v1/')}
    return render(request, env['shell'], data)


def _filter_branch(qs, selected, field='branch'):
    return qs.filter(**{field: selected}) if selected else qs


def _redirect_back(request, fallback):
    nxt = request.POST.get('next') or ''
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else fallback)


# --------------------------------------------------------------------------
# Overview & channels
# --------------------------------------------------------------------------

def _overview(request, env):
    r = env['restaurant']
    branches, selected, locked = _scope(request, r)
    here = _url(env, 'integrations')

    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'add_channel':
            branch = _branch_from_post(request, branches)
            provider = request.POST.get('provider', '')
            if not branch or provider not in dict(_module(env)['providers']):
                messages.error(request, 'Pick a branch and a channel.')
                return redirect(here)
            try:
                pct = float(request.POST.get('commission_pct') or 0)
            except ValueError:
                pct = -1
            if not 0 <= pct <= 100:
                messages.error(request, 'Commission must be between 0 and 100%.')
                return redirect(here)
            store = request.POST.get('external_store_id', '').strip()[:120]
            if Connection.objects.filter(branch=branch, provider=provider, external_store_id=store).exists():
                messages.error(request, 'That channel is already connected for this branch.')
                return redirect(here)
            conn = Connection.objects.create(restaurant=r, branch=branch, provider=provider, external_store_id=store,
                                             name=request.POST.get('name', '').strip()[:120], commission_pct=pct, created_by=request.user)
            msg = f'Connected {conn.get_provider_display()} for {branch.name}'
            created_key = None
            if request.POST.get('issue_key') == 'on':
                key, raw = ApiKey.issue(restaurant=r, branch=branch, connection=conn, name=f'{conn.get_provider_display()} – {branch.name}',
                                        scopes=conn.default_scopes, user=request.user)
                request.session['integrations_new_secret'] = {'kind': 'API key', 'name': key.name, 'value': raw}
                created_key = key
                msg += ' and issued its API key'
            log_activity(request, action='CREATE', message=msg, area=env['area'], restaurant=r, branch=branch,
                         object_type='Connection', object_id=conn.pk, object_repr=conn.label)
            messages.success(request, msg + '.')
            return redirect(_url(env, 'integration_keys') if created_key else here)
        conn = get_object_or_404(_module_connections(env, Connection.objects.filter(restaurant=r, branch__in=branches)), pk=request.POST.get('pk'))
        if op == 'toggle_channel':
            conn.is_active = not conn.is_active
            conn.save(update_fields=['is_active'])
            if not conn.is_active:
                n = conn.api_keys.active().update(revoked_at=timezone.now())
                note = f' and revoked {n} key{"s" if n != 1 else ""}' if n else ''
            else:
                note = ''
            log_activity(request, action='UPDATE', message=f'{"Resumed" if conn.is_active else "Paused"} {conn.label}{note}', area=env['area'],
                         restaurant=r, branch=conn.branch, object_type='Connection', object_id=conn.pk)
            messages.success(request, f'{conn.label} {"resumed" if conn.is_active else "paused"}{note}.')
        elif op == 'update_channel':
            try:
                pct = float(request.POST.get('commission_pct') or 0)
            except ValueError:
                pct = -1
            if not 0 <= pct <= 100:
                messages.error(request, 'Commission must be between 0 and 100%.')
            else:
                conn.commission_pct = pct
                conn.external_store_id = request.POST.get('external_store_id', conn.external_store_id).strip()[:120]
                conn.name = request.POST.get('name', conn.name).strip()[:120]
                conn.notes = request.POST.get('notes', conn.notes)[:2000]
                conn.save()
                log_activity(request, action='UPDATE', message=f'Updated channel {conn.label}', area=env['area'], restaurant=r, branch=conn.branch,
                             object_type='Connection', object_id=conn.pk)
                messages.success(request, 'Channel updated.')
        elif op == 'delete_channel':
            label = conn.label
            conn.api_keys.active().update(revoked_at=timezone.now())
            conn.delete()
            log_activity(request, action='DELETE', message=f'Removed channel {label} and revoked its keys', area=env['area'], restaurant=r)
            messages.success(request, f'{label} removed and its keys revoked.')
        return _redirect_back(request, here)

    conns = _module_connections(env, _filter_branch(Connection.objects.filter(restaurant=r, branch__in=branches), selected)).select_related('branch')
    since = timezone.now() - timedelta(days=30)
    order_counts = dict(ExternalOrderLink.objects.filter(connection__in=conns, created_at__gte=since).values_list('connection').annotate(n=Count('id')))
    booking_counts = dict(ExternalBookingLink.objects.filter(connection__in=conns, created_at__gte=since).values_list('connection').annotate(n=Count('id')))
    rows = []
    for c in conns:
        rows.append({'c': c, 'orders_30d': order_counts.get(c.id, 0) + booking_counts.get(c.id, 0),
                     'keys': c.api_keys.active().count()})

    keys = _module_keys(env, _filter_branch(ApiKey.objects.filter(restaurant=r, branch__in=branches), selected).active())
    hooks = WebhookEndpoint.objects.filter(restaurant=r).filter(Q(branch__in=branches) | Q(branch__isnull=True) if not locked else Q(branch__in=branches))
    if selected:
        hooks = hooks.filter(Q(branch=selected) | Q(branch__isnull=True))
    hooks = _module_hooks(env, hooks)
    day = timezone.now() - timedelta(hours=24)
    recent = WebhookDelivery.objects.filter(endpoint__in=hooks, created_at__gte=day)
    total = recent.count()
    ok = recent.filter(status=WebhookDelivery.Status.SUCCESS).count()
    kpis = {
        'channels': conns.filter(is_active=True).count(), 'keys': keys.active().count(),
        'webhooks': hooks.filter(is_active=True).count(), 'deliveries': total,
        'success_rate': round(ok * 100 / total) if total else None,
        'failing': hooks.filter(Q(is_active=False) & ~Q(disabled_reason='')).count(),
        'orders_30d': (ExternalBookingLink.objects.filter(booking__branch__restaurant=r) if env['area'] == 'hotel' else ExternalOrderLink.objects.filter(order__restaurant=r))
        .filter(branch__in=[b.id for b in ([selected] if selected else branches)], created_at__gte=since).count(),
    }
    return _render(request, env, 'integrations/_overview.html', {
        'rows': rows, 'kpis': kpis, 'providers': _module(env)['providers'], 'here': request.get_full_path(),
    }, 'overview')


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------

def _keys(request, env):
    r = env['restaurant']
    branches, selected, locked = _scope(request, r)
    here = _url(env, 'integration_keys')
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'create':
            branch = _branch_from_post(request, branches)
            name = request.POST.get('name', '').strip()[:120]
            scopes = [s for s in request.POST.getlist('scopes') if s in dict(_module(env)['scopes'])]
            if not branch or not name or not scopes:
                messages.error(request, 'Give the key a name, a branch and at least one permission.')
                return redirect(here)
            conn = None
            if request.POST.get('connection'):
                conn = _module_connections(env, Connection.objects.filter(pk=request.POST['connection'], restaurant=r, branch=branch)).first()
            expires = parse_date(request.POST.get('expires_on', '') or '')
            expires_at = timezone.make_aware(datetime.combine(expires, time.max)) if expires else None
            if expires and expires <= timezone.localdate():
                messages.error(request, 'The expiry date must be in the future.')
                return redirect(here)
            key, raw = ApiKey.issue(restaurant=r, branch=branch, name=name, scopes=scopes, connection=conn, user=request.user, expires_at=expires_at)
            request.session['integrations_new_secret'] = {'kind': 'API key', 'name': key.name, 'value': raw}
            log_activity(request, action='CREATE', message=f'Issued API key "{key.name}" for {branch.name} ({", ".join(scopes)})', area=env['area'],
                         restaurant=r, branch=branch, object_type='ApiKey', object_id=key.pk, object_repr=key.prefix)
            messages.success(request, 'API key created. Copy it now; it will not be shown again.')
            return redirect(here)
        key = get_object_or_404(_module_keys(env, ApiKey.objects.filter(restaurant=r, branch__in=branches)), pk=request.POST.get('pk'))
        if op == 'revoke' and not key.revoked_at:
            key.revoked_at = timezone.now()
            key.save(update_fields=['revoked_at'])
            log_activity(request, action='UPDATE', message=f'Revoked API key "{key.name}"', area=env['area'], restaurant=r, branch=key.branch,
                         object_type='ApiKey', object_id=key.pk, object_repr=key.prefix)
            messages.success(request, f'"{key.name}" revoked. Calls with it now get 401.')
        elif op == 'roll':
            key.revoked_at = timezone.now()
            key.save(update_fields=['revoked_at'])
            new, raw = ApiKey.issue(restaurant=r, branch=key.branch, name=key.name, scopes=key.scopes, connection=key.connection,
                                    user=request.user, expires_at=key.expires_at)
            request.session['integrations_new_secret'] = {'kind': 'API key', 'name': new.name, 'value': raw}
            log_activity(request, action='UPDATE', message=f'Rolled API key "{key.name}"', area=env['area'], restaurant=r, branch=key.branch,
                         object_type='ApiKey', object_id=new.pk, object_repr=new.prefix)
            messages.success(request, 'Key rolled. The old one stopped working; copy the new one now.')
        elif op == 'delete' and key.revoked_at:
            key.delete()
            messages.success(request, 'Revoked key removed from the list.')
        return _redirect_back(request, here)

    qs = _module_keys(env, _filter_branch(ApiKey.objects.filter(restaurant=r, branch__in=branches), selected)).select_related('branch', 'connection', 'created_by')
    show = request.GET.get('show', 'active')
    if show == 'active':
        qs = qs.active()
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    conns = _module_connections(env, _filter_branch(Connection.objects.filter(restaurant=r, branch__in=branches, is_active=True), selected)).select_related('branch')
    return _render(request, env, 'integrations/_keys.html', {
        'page': page, 'scopes': _module(env)['scopes'], 'connections': conns, 'show': show,
        'new_secret': request.session.pop('integrations_new_secret', None), 'here': request.get_full_path(),
    }, 'keys')


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------

def _clean_events(request, env):
    allowed = dict(_module(env)['events'])
    return [e for e in request.POST.getlist('events') if e in allowed]


def _webhooks(request, env):
    r = env['restaurant']
    branches, selected, locked = _scope(request, r)
    here = _url(env, 'integration_webhooks')
    if request.method == 'POST':
        name, url = request.POST.get('name', '').strip()[:120], request.POST.get('url', '').strip()
        events = _clean_events(request, env)
        branch = _branch_from_post(request, branches)
        if locked and not branch:
            branch = branches[0]
        if request.POST.get('branch') and not branch:
            messages.error(request, 'Pick a valid branch.')
            return redirect(here)
        if not name or not url or not events:
            messages.error(request, 'Give the webhook a name, a URL and at least one event.')
            return redirect(here)
        try:
            dispatch.check_url(url)
        except dispatch.UnsafeURL as exc:
            messages.error(request, str(exc))
            return redirect(here)
        ep = WebhookEndpoint.objects.create(restaurant=r, branch=branch, name=name, url=url, events=events, created_by=request.user)
        request.session['integrations_new_secret'] = {'kind': 'Signing secret', 'name': ep.name, 'value': ep.secret}
        log_activity(request, action='CREATE', message=f'Added webhook "{name}" → {url}', area=env['area'], restaurant=r, branch=branch,
                     object_type='WebhookEndpoint', object_id=ep.pk)
        messages.success(request, 'Webhook added. Use the signing secret to verify our requests.')
        return redirect(_url(env, 'integration_webhook_detail', pk=ep.pk))

    qs = WebhookEndpoint.objects.filter(restaurant=r).select_related('branch')
    qs = qs.filter(branch__in=branches) if locked else qs
    if selected:
        qs = qs.filter(Q(branch=selected) | Q(branch__isnull=True))
    qs = _module_hooks(env, qs)
    day = timezone.now() - timedelta(hours=24)
    stats = {row['endpoint']: row for row in WebhookDelivery.objects.filter(endpoint__in=qs, created_at__gte=day).values('endpoint')
             .annotate(total=Count('id'), ok=Count('id', filter=Q(status='SUCCESS')))}
    rows = [{'ep': ep, 'total': stats.get(ep.id, {}).get('total', 0), 'ok': stats.get(ep.id, {}).get('ok', 0)} for ep in qs]
    return _render(request, env, 'integrations/_webhooks.html', {
        'rows': rows, 'events': _module(env)['events'], 'here': request.get_full_path(),
    }, 'webhooks')


def _webhook_detail(request, env, pk):
    r = env['restaurant']
    branches, selected, locked = _scope(request, r)
    qs = WebhookEndpoint.objects.filter(restaurant=r)
    if locked:
        qs = qs.filter(branch__in=branches)
    ep = get_object_or_404(_module_hooks(env, qs), pk=pk)
    here = _url(env, 'integration_webhook_detail', pk=ep.pk)
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'update':
            events, url = _clean_events(request, env), request.POST.get('url', '').strip()
            try:
                dispatch.check_url(url)
            except dispatch.UnsafeURL as exc:
                messages.error(request, str(exc))
                return redirect(here)
            if not events:
                messages.error(request, 'Subscribe to at least one event.')
                return redirect(here)
            ep.name = request.POST.get('name', ep.name).strip()[:120] or ep.name
            ep.url, ep.events = url, events
            if not locked:
                ep.branch = _branch_from_post(request, branches)
            ep.save()
            messages.success(request, 'Webhook updated.')
        elif op == 'toggle':
            ep.is_active = not ep.is_active
            if ep.is_active:
                ep.failure_streak, ep.disabled_reason = 0, ''
            ep.save(update_fields=['is_active', 'failure_streak', 'disabled_reason'])
            messages.success(request, f'Webhook {"turned on" if ep.is_active else "paused"}.')
        elif op == 'rotate':
            ep.secret = WebhookEndpoint.new_secret()
            ep.save(update_fields=['secret'])
            request.session['integrations_new_secret'] = {'kind': 'Signing secret', 'name': ep.name, 'value': ep.secret}
            messages.success(request, 'New signing secret created. Update your receiver now; the old one no longer matches.')
        elif op == 'test':
            d = dispatch.send_test(ep)
            if d.status == WebhookDelivery.Status.SUCCESS:
                messages.success(request, f'Test delivered · HTTP {d.response_code} in {d.duration_ms} ms.')
            else:
                messages.error(request, f'Test failed · {d.error or "HTTP " + str(d.response_code)}.')
        elif op == 'retry':
            d = get_object_or_404(WebhookDelivery, pk=request.POST.get('delivery'), endpoint=ep)
            if dispatch.deliver(d):
                messages.success(request, 'Delivered.')
            else:
                messages.error(request, f'Still failing · {d.error or "HTTP " + str(d.response_code)}.')
        elif op == 'delete':
            name = ep.name
            ep.delete()
            log_activity(request, action='DELETE', message=f'Deleted webhook "{name}"', area=env['area'], restaurant=r)
            messages.success(request, f'Webhook "{name}" deleted.')
            return redirect(_url(env, 'integration_webhooks'))
        if op in ('update', 'toggle', 'rotate'):
            log_activity(request, action='UPDATE', message=f'Webhook "{ep.name}": {op}', area=env['area'], restaurant=r, branch=ep.branch,
                         object_type='WebhookEndpoint', object_id=ep.pk)
        return redirect(here + ('?' + request.GET.urlencode() if request.GET else ''))

    deliveries = ep.deliveries.all()
    status = request.GET.get('status', '')
    if status in WebhookDelivery.Status.values:
        deliveries = deliveries.filter(status=status)
    page = Paginator(deliveries, 25).get_page(request.GET.get('page'))
    return _render(request, env, 'integrations/_webhook_detail.html', {
        'ep': ep, 'page': page, 'events': _module(env)['events'], 'status': status, 'statuses': WebhookDelivery.Status.choices,
        'new_secret': request.session.pop('integrations_new_secret', None),
        'secret_hint': ep.secret[:10] + '…' + ep.secret[-4:],
    }, 'webhooks')


def _delivery_json(request, env, pk, delivery_pk):
    r = env['restaurant']
    branches, _, locked = _scope(request, r)
    qs = WebhookDelivery.objects.filter(endpoint__restaurant=r, endpoint_id=pk)
    if locked:
        qs = qs.filter(endpoint__branch__in=branches)
    d = get_object_or_404(qs, pk=delivery_pk)
    return JsonResponse({'event': d.event, 'event_id': d.event_id, 'status': d.get_status_display(), 'attempts': d.attempts,
                         'response_code': d.response_code, 'response_body': d.response_body, 'error': d.error,
                         'payload': d.payload})


def _docs(request, env):
    r = env['restaurant']
    branches, selected, _ = _scope(request, r)
    return _render(request, env, 'integrations/_docs.html', {'scopes': _module(env)['scopes'], 'events': _module(env)['events'], 'module': env['area'],
                                                             'example_branch': selected or (branches[0] if branches else None)}, 'docs')


# --------------------------------------------------------------------------
# Public wrappers
# --------------------------------------------------------------------------

def _wrap(impl):
    @login_required
    def view(request, restaurant_slug, *args, **kwargs):
        env = _restaurant_env(request, restaurant_slug)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, *args, **kwargs)
    return view


def _wrap_hotel(impl):
    from hotel.views_admin import hotel_admin_required

    @hotel_admin_required
    def view(request, restaurant_slug, ctx, *args, **kwargs):
        env = _hotel_env(request, restaurant_slug, ctx)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, *args, **kwargs)
    return view


overview, keys, webhooks = _wrap(_overview), _wrap(_keys), _wrap(_webhooks)
webhook_detail, delivery_json, docs = _wrap(_webhook_detail), _wrap(_delivery_json), _wrap(_docs)
hotel_overview, hotel_keys, hotel_webhooks = _wrap_hotel(_overview), _wrap_hotel(_keys), _wrap_hotel(_webhooks)
hotel_webhook_detail, hotel_delivery_json, hotel_docs = _wrap_hotel(_webhook_detail), _wrap_hotel(_delivery_json), _wrap_hotel(_docs)


# --------------------------------------------------------------------------
# Super Admin — platform view
# --------------------------------------------------------------------------

def super_integrations(request):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return redirect('login')
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'revoke_key':
            key = get_object_or_404(ApiKey, pk=request.POST.get('pk'))
            key.revoked_at = key.revoked_at or timezone.now()
            key.save(update_fields=['revoked_at'])
            log_activity(request, action='UPDATE', message=f'Revoked API key "{key.name}" of {key.restaurant.name}', area='super', restaurant=key.restaurant)
            messages.success(request, f'Revoked "{key.name}".')
        elif op == 'pause_webhook':
            ep = get_object_or_404(WebhookEndpoint, pk=request.POST.get('pk'))
            ep.is_active = False
            ep.disabled_reason = ep.disabled_reason or 'Paused by the platform team.'
            ep.save(update_fields=['is_active', 'disabled_reason'])
            log_activity(request, action='UPDATE', message=f'Paused webhook "{ep.name}" of {ep.restaurant.name}', area='super', restaurant=ep.restaurant)
            messages.success(request, f'Paused "{ep.name}".')
        return redirect('super_admin:integrations')

    from super_admin.models import Restaurant
    day = timezone.now() - timedelta(hours=24)
    month = timezone.now() - timedelta(days=30)
    businesses = []
    for r in Restaurant.objects.order_by('name'):
        conns = Connection.objects.filter(restaurant=r)
        keys = ApiKey.objects.filter(restaurant=r).active()
        hooks = WebhookEndpoint.objects.filter(restaurant=r)
        if not (conns.exists() or keys.exists() or hooks.exists()):
            continue
        recent = WebhookDelivery.objects.filter(endpoint__restaurant=r, created_at__gte=day)
        total, ok = recent.count(), recent.filter(status='SUCCESS').count()
        businesses.append({
            'r': r, 'channels': conns.filter(is_active=True).count(), 'providers': sorted({c.get_provider_display() for c in conns}),
            'keys': keys.count(), 'webhooks': hooks.filter(is_active=True).count(),
            'disabled': hooks.filter(is_active=False).exclude(disabled_reason='').count(),
            'deliveries': total, 'success_rate': round(ok * 100 / total) if total else None,
            'orders_30d': ExternalOrderLink.objects.filter(order__restaurant=r, created_at__gte=month).count(),
        })
    failing = WebhookEndpoint.objects.filter(Q(failure_streak__gte=3) | (Q(is_active=False) & ~Q(disabled_reason=''))).select_related('restaurant', 'branch')[:20]
    keys = ApiKey.objects.active().select_related('restaurant', 'branch', 'connection').order_by('-last_used_at')[:30]
    recent = WebhookDelivery.objects.filter(created_at__gte=day)
    total = recent.count()
    kpis = {
        'businesses': len(businesses), 'channels': Connection.objects.filter(is_active=True).count(),
        'keys': ApiKey.objects.active().count(), 'deliveries': total,
        'success_rate': round(recent.filter(status='SUCCESS').count() * 100 / total) if total else None,
        'orders_30d': ExternalOrderLink.objects.filter(created_at__gte=month).count(),
    }
    return render(request, 'guests/shell_super.html', {
        'partial': 'integrations/_super.html', 'page_title': 'Integrations', 'businesses': businesses,
        'failing': failing, 'keys': keys, 'kpis': kpis,
    })


# --------------------------------------------------------------------------
# Hotel distribution: map room types to each OTA / channel manager
# --------------------------------------------------------------------------

def _distribution(request, env):
    import datetime as _dt
    from django.contrib import messages
    from django.utils import timezone
    from hotel.models import RoomCategory
    from integrations import channels
    from integrations.models import ChannelRoomMapping
    branches, selected, locked = _scope(request, env['restaurant'])
    branch = selected or env['ctx'].get('current_branch')
    conns = _module_connections(env, Connection.objects.filter(restaurant=env['restaurant'], branch=branch, is_active=True)
                                .exclude(provider='custom')).order_by('provider', 'name') if branch else Connection.objects.none()
    conn = conns.filter(pk=request.GET.get('connection') or request.POST.get('connection') or 0).first() or conns.first()
    cats = list(RoomCategory.objects.filter(branch=branch, is_active=True).order_by('base_price_per_night')) if branch else []
    if request.method == 'POST' and conn is not None:
        if not can_manage(request.user):
            return HttpResponseForbidden('Only administrators can change channel settings.')
        saved = 0
        for cat in cats:
            p = f'c{cat.id}_'
            enabled = request.POST.get(p + 'on') == '1'
            m = ChannelRoomMapping.objects.filter(connection=conn, category=cat).first()
            if not enabled:
                if m:
                    m.delete()
                continue
            try:
                adjust = Decimal(str(request.POST.get(p + 'adjust') or '0'))
            except Exception:
                adjust = Decimal('0')
            adjust = max(Decimal('-50'), min(Decimal('100'), adjust))
            allot = (request.POST.get(p + 'allotment') or '').strip()
            m = m or ChannelRoomMapping(connection=conn, category=cat)
            m.external_room_id = (request.POST.get(p + 'room') or '').strip()[:120]
            m.external_rate_plan_id = (request.POST.get(p + 'plan') or '').strip()[:120]
            m.rate_adjust_pct = adjust
            m.allotment = int(allot) if allot.isdigit() else None
            m.is_closed = request.POST.get(p + 'closed') == '1'
            m.save()
            saved += 1
        messages.success(request, f'{conn.name or conn.get_provider_display()}: {saved} room type{"s" if saved != 1 else ""} on sale. '
                                  'Channels that read the rates feed pick this up straight away.')
        return redirect(_url(env, 'integration_distribution') + f'?connection={conn.pk}' + (f'&branch={branch.pk}' if selected else ''))
    maps = channels.mappings(conn)
    rows = [{'cat': c, 'm': maps.get(c.id)} for c in cats]
    today = timezone.localdate()
    preview = channels.ari(branch, today, today + _dt.timedelta(days=7), conn, only_mapped=True) if conn and maps else []
    stats = channels.channel_stats(conn, timezone.now() - _dt.timedelta(days=30)) if conn else None
    return _render(request, env, 'integrations/_distribution.html', {
        'conns': conns, 'conn': conn, 'rows': rows, 'preview': preview, 'stats': stats, 'branch': branch,
        'preview_dates': [today + _dt.timedelta(days=i) for i in range(7)],
        'rates_url': request.build_absolute_uri('/api/v1/rates'),
    }, 'distribution')


hotel_distribution = _wrap_hotel(_distribution)
