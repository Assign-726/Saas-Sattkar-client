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
    return {
        'can_view_restaurant_logs': can_view_logs(user, 'restaurant'),
        'can_view_hotel_logs': can_view_logs(user, 'hotel'),
    }
