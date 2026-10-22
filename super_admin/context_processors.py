from super_admin.activity import user_role

RESTAURANT_LOG_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'CLUB_ADMIN'}
HOTEL_LOG_ROLES = {'GENERAL_MANAGER', 'HOTEL_ADMIN'}


def can_view_logs(user, area):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if area == 'super':
        return False
    return user_role(user) in (HOTEL_LOG_ROLES if area == 'hotel' else RESTAURANT_LOG_ROLES)


def activity_log_access(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    from super_admin.permissions import NONE, access_map, custom_role_of
    levels = access_map(user)
    access = {k: v != NONE for k, v in levels.items()}

    # Resolve active branch to narrow access if not superuser
    branch = None
    if not getattr(user, 'is_superuser', False):
        if hasattr(user, 'restaurant_profile') and user.restaurant_profile:
            branch = user.restaurant_profile.branch
            if not branch and user.restaurant_profile.restaurant:
                branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{user.restaurant_profile.restaurant.id}')
                if branch_id and str(branch_id).isdigit():
                    branch = user.restaurant_profile.restaurant.branches.filter(id=int(branch_id)).first()
                if not branch:
                    branch = user.restaurant_profile.restaurant.branches.first()

        if branch and hasattr(branch, 'is_feature_enabled'):
            for area_key in list(access.keys()):
                if not branch.is_feature_enabled(area_key):
                    access[area_key] = False

    access['guests_group'] = access.get('guests', False) or access.get('feedback', False) or access.get('campaigns', False)
    can_fin = _can_manage_integrations(user)
    if not getattr(user, 'is_superuser', False) and branch and hasattr(branch, 'is_feature_enabled'):
        if not branch.is_feature_enabled('finance'):
            can_fin = False

    return {
        'access': access, 'access_level': levels, 'custom_role': custom_role_of(user),
        'can_manage_roles': _can_manage_roles(user),
        'can_view_restaurant_logs': can_view_logs(user, 'restaurant'),
        'can_view_hotel_logs': can_view_logs(user, 'hotel'),
        'can_view_guests': _can_view_guests(user),
        'can_manage_integrations': _can_manage_integrations(user),
        'can_manage_finance': can_fin,
        'can_run_campaigns': _can_manage_guests(user),
    }


def _can_manage_guests(user):
    from guests.views import can_manage
    return can_manage(user)


def _can_manage_roles(user):
    from super_admin.views_roles import can_manage_roles
    return can_manage_roles(user)


def _can_manage_integrations(user):
    from integrations.views import can_manage
    return can_manage(user)


def _can_view_guests(user):
    from guests.views import can_view
    return can_view(user)
