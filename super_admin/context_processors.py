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
    access['guests_group'] = access['guests'] or access['feedback'] or access['campaigns']
    return {
        'access': access, 'access_level': levels, 'custom_role': custom_role_of(user),
        'can_manage_roles': _can_manage_roles(user),
        'can_view_restaurant_logs': can_view_logs(user, 'restaurant'),
        'can_view_hotel_logs': can_view_logs(user, 'hotel'),
        'can_view_guests': _can_view_guests(user),
        'can_manage_integrations': _can_manage_integrations(user),
        'can_manage_finance': _can_manage_integrations(user),
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
