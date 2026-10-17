"""Roles & Permissions: build custom roles and assign them to logins.

Only full administrators (General Manager, Restaurant Admin, Hotel Admin — without a custom
role themselves) can manage roles, and only for their own side of the business: a Restaurant
Admin can grant restaurant and shared areas, a Hotel Admin hotel and shared areas, a General
Manager everything. That keeps anyone from creating a login more powerful than their own.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from super_admin import permissions as P
from super_admin.activity import log_activity, user_role
from super_admin.models import CustomRole, RestaurantUser

ROLE_MANAGERS = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN'}
BASE_LABELS = {'GENERAL_MANAGER': 'Restaurant & Hotel admin', 'RESTAURANT_ADMIN': 'Restaurant admin', 'HOTEL_ADMIN': 'Hotel admin'}
STANDARD_ROLES = {
    P.R: ['RESTAURANT_ADMIN', 'CASHIER', 'WAITER', 'KITCHEN', 'CLUB_ADMIN', 'GATE_SCANNER'],
    P.H: ['HOTEL_ADMIN', 'FRONT_DESK', 'HOUSEKEEPING'],
}


def can_manage_roles(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return P.custom_role_of(user) is None and user_role(user) in ROLE_MANAGERS


def _modules_for(user):
    role = 'GENERAL_MANAGER' if user.is_superuser else user_role(user)
    return {P.R, P.H, P.S} if role == 'GENERAL_MANAGER' else ({P.R, P.S} if role == 'RESTAURANT_ADMIN' else {P.H, P.S})


def _business_modules(restaurant):
    branches = list(restaurant.branches.all())
    mods = {P.S}
    if not branches or any(getattr(b, 'has_restaurant_module', True) for b in branches):
        mods.add(P.R)
    if any(getattr(b, 'has_hotel_module', False) for b in branches):
        mods.add(P.H)
    return mods


def _env(request, restaurant, shell, ns, slug, ctx):
    if not can_manage_roles(request.user):
        return HttpResponseForbidden('Roles are managed by the business administrators.')
    mods = _modules_for(request.user) & _business_modules(restaurant)
    return {'restaurant': restaurant, 'shell': shell, 'ns': ns, 'slug': slug, 'ctx': ctx, 'modules': mods,
            'area': 'hotel' if ns == 'hotel_admin' else 'restaurant'}


def _url(env, name, **kw):
    return reverse(f'{env["ns"]}:{name}', kwargs={'restaurant_slug': env['slug'], **kw})


def _render(request, env, partial, context):
    data = {**env['ctx'], **context, 'partial': partial, 'active_menu': 'roles', 'page_title': 'Roles & Permissions',
            'roles_url': _url(env, 'roles'), 'new_role_url': _url(env, 'role_new')}
    return render(request, env['shell'], data)


def _area_rows(env, role=None, preset=None):
    groups = []
    for module, title in ((P.R, 'Restaurant'), (P.H, 'Hotel'), (P.S, 'Whole business')):
        if module not in env['modules']:
            continue
        rows = []
        for key, label, mod, icon, desc in P.AREAS:
            if mod != module:
                continue
            current = role.level(key) if role else (preset or {}).get(key, P.NONE)
            rows.append({'key': key, 'label': label, 'icon': icon, 'desc': desc, 'level': current})
        groups.append({'module': module, 'title': title, 'rows': rows})
    return groups


def _eligible_logins(request, env):
    """Logins this manager may put on a custom role: never themselves, the primary admin or a superuser."""
    qs = (RestaurantUser.objects.filter(restaurant=env['restaurant']).select_related('user', 'branch', 'custom_role')
          .exclude(user=request.user).exclude(user__is_superuser=True).exclude(is_primary_admin=True))
    if not request.user.is_superuser and user_role(request.user) != 'GENERAL_MANAGER':
        qs = qs.exclude(role='GENERAL_MANAGER')
        if P.H not in env['modules']:
            qs = qs.exclude(role__in=STANDARD_ROLES[P.H])
        if P.R not in env['modules']:
            qs = qs.exclude(role__in=STANDARD_ROLES[P.R])
    return qs.order_by('user__first_name', 'user__username')


def _standard_choices(env):
    allowed = [r for m in (P.R, P.H) if m in env['modules'] for r in STANDARD_ROLES[m]]
    return [(v, l) for v, l in RestaurantUser.Role.choices if v in allowed]


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

def _roles(request, env):
    roles = CustomRole.objects.filter(restaurant=env['restaurant']).prefetch_related('members__user')
    cards = []
    for role in roles:
        manage, view = role.summary()
        cards.append({'role': role, 'manage': manage, 'view': view, 'base': BASE_LABELS.get(role.base_role, role.base_role),
                      'editable': role.modules <= env['modules']})
    return _render(request, env, 'roles/_list.html', {'cards': cards, 'presets': P.PRESETS})


def _save_role(request, env, role=None):
    name = (request.POST.get('name') or '').strip()[:60]
    if not name:
        messages.error(request, 'Give the role a name.')
        return None
    perms = {}
    for key, _, module, *_ in P.AREAS:
        value = request.POST.get(f'perm_{key}', P.NONE)
        perms[key] = value if (value in P.RANK and module in env['modules']) else P.NONE
    if role is not None:
        # Keep areas this manager cannot see exactly as they were
        for key, _, module, *_ in P.AREAS:
            if module not in env['modules']:
                perms[key] = role.level(key)
    if all(v == P.NONE for v in perms.values()):
        messages.error(request, 'Open at least one area, otherwise people with this role cannot use anything.')
        return None
    creating = role is None
    role = role or CustomRole(restaurant=env['restaurant'], created_by=request.user)
    role.name, role.description, role.permissions = name, (request.POST.get('description') or '').strip()[:200], perms
    try:
        with transaction.atomic():
            role.save()
            role.sync_members()
    except IntegrityError:
        messages.error(request, f'A role called "{name}" already exists.')
        return None
    manage, view = role.summary()
    log_activity(request, action='CREATE' if creating else 'SECURITY',
                 message=f'{"Created" if creating else "Updated"} role "{role.name}" · manage: {", ".join(manage) or "none"} · view: {", ".join(view) or "none"}',
                 area=env['area'], restaurant=env['restaurant'], object_type='CustomRole', object_id=role.pk, object_repr=role.name)
    return role


def _role_new(request, env):
    if request.method == 'POST':
        role = _save_role(request, env)
        if role:
            messages.success(request, f'Role "{role.name}" created. Now add people to it.')
            return redirect(_url(env, 'role_edit', pk=role.pk))
    preset = next((p for p in P.PRESETS if p[0] == request.GET.get('preset')), None)
    return _render(request, env, 'roles/_edit.html', {
        'role': None, 'groups': _area_rows(env, preset=preset[2] if preset else None), 'levels': P.LEVELS,
        'preset_name': preset[0] if preset else '', 'preset_desc': preset[1] if preset else '', 'presets': P.PRESETS,
    })


def _role_edit(request, env, pk):
    role = get_object_or_404(CustomRole, pk=pk, restaurant=env['restaurant'])
    if not role.modules <= env['modules']:
        return HttpResponseForbidden('This role includes areas outside your part of the business. Ask the General Manager.')
    if request.method == 'POST':
        op = request.POST.get('op', 'save')
        if op == 'save':
            if _save_role(request, env, role):
                messages.success(request, 'Role saved. Everyone on it has the new access straight away.')
        elif op == 'assign':
            ids = [i for i in request.POST.getlist('logins') if i.isdigit()]
            members = _eligible_logins(request, env).filter(pk__in=ids)
            names = []
            for m in members:
                m.custom_role = role
                m.save(update_fields=['custom_role'])
                names.append(m.user.get_full_name() or m.user.username)
            role.sync_members()
            if names:
                log_activity(request, action='SECURITY', message=f'Gave role "{role.name}" to {", ".join(names)}', area=env['area'],
                             restaurant=env['restaurant'], object_type='CustomRole', object_id=role.pk)
                messages.success(request, f'{", ".join(names)} now {"use" if len(names) > 1 else "uses"} "{role.name}".')
        elif op == 'remove':
            m = get_object_or_404(_eligible_logins(request, env), pk=request.POST.get('member'), custom_role=role)
            new_role = request.POST.get('new_role')
            if new_role not in dict(_standard_choices(env)):
                messages.error(request, 'Choose the standard role this person moves to.')
                return redirect(request.path)
            m.custom_role, m.role = None, new_role
            for flag in P.LEGACY_FLAGS:
                setattr(m, flag, RestaurantUser._meta.get_field(flag).default)
            m.save()
            log_activity(request, action='SECURITY', message=f'Moved {m.user.username} from role "{role.name}" to {m.get_role_display()}',
                         area=env['area'], restaurant=env['restaurant'])
            messages.success(request, f'{m.user.get_full_name() or m.user.username} is now {m.get_role_display()}.')
        elif op == 'delete':
            if role.members.exists():
                messages.error(request, 'Move everyone off this role before deleting it.')
            else:
                name = role.name
                role.delete()
                log_activity(request, action='DELETE', message=f'Deleted role "{name}"', area=env['area'], restaurant=env['restaurant'])
                messages.success(request, f'Role "{name}" deleted.')
                return redirect(_url(env, 'roles'))
        return redirect(request.path)

    members = role.members.select_related('user', 'branch')
    eligible = [m for m in _eligible_logins(request, env) if m.custom_role_id != role.pk]
    return _render(request, env, 'roles/_edit.html', {
        'role': role, 'groups': _area_rows(env, role=role), 'levels': P.LEVELS, 'members': members, 'eligible': eligible,
        'standard_roles': _standard_choices(env), 'base_label': BASE_LABELS.get(role.base_role),
    })


# --------------------------------------------------------------------------
# Shell wrappers
# --------------------------------------------------------------------------

def _wrap(impl):
    @login_required
    def view(request, restaurant_slug, *args, **kwargs):
        from restaurant_admin.views import resolve_restaurant
        restaurant = resolve_restaurant(request, restaurant_slug)
        if isinstance(restaurant, HttpResponse):
            return restaurant
        env = _env(request, restaurant, 'guests/shell_restaurant.html', 'restaurant_admin', restaurant_slug, {'restaurant': restaurant})
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, *args, **kwargs)
    return view


def _wrap_hotel(impl):
    from hotel.views_admin import hotel_admin_required

    @hotel_admin_required
    def view(request, restaurant_slug, ctx, *args, **kwargs):
        env = _env(request, ctx['restaurant'], 'guests/shell_hotel.html', 'hotel_admin', restaurant_slug, ctx)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, *args, **kwargs)
    return view


roles, role_new, role_edit = _wrap(_roles), _wrap(_role_new), _wrap(_role_edit)
hotel_roles, hotel_role_new, hotel_role_edit = _wrap_hotel(_roles), _wrap_hotel(_role_new), _wrap_hotel(_role_edit)
