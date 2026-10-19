from rest_framework import permissions


class IsHotelAdminOrManager(permissions.BasePermission):
    """
    Permission class to verify:
    1. request.user.branch.has_hotel_module == True
    2. request.user.role in ['HOTEL_ADMIN', 'GENERAL_MANAGER']
    """
    message = "Access restricted: requires Hotel Admin or General Manager role with Hotel PMS enabled for this branch."
    code = "FEATURE_GATED_HOTEL_MODULE"

    def has_permission(self, request, view):
        from restaurant_project import constants
        from core import tenancy
        if not getattr(constants, 'ENABLE_HOTEL_MODULE', True):
            return False

        # Only platform superusers bypass. Every business login is ``is_staff``, so staff
        # status must never open another business's hotel.
        if tenancy.is_platform_admin(request.user):
            return True
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
            return False
        if getattr(request.user, 'role', None) not in ('HOTEL_ADMIN', 'GENERAL_MANAGER'):
            return False

        from hotel.views import resolve_branch_for_request
        branch = resolve_branch_for_request(request)
        return bool(branch and branch.has_hotel_module and tenancy.can_access_branch(request.user, branch))

    def has_object_permission(self, request, view, obj):
        from core import tenancy
        if tenancy.is_platform_admin(request.user):
            return True
        branch = getattr(obj, 'branch', None)
        if branch is None and hasattr(obj, 'room'):
            branch = getattr(obj.room, 'branch', None)
        return bool(branch and tenancy.can_access_branch(request.user, branch) and branch.has_hotel_module)


# Alias for explicit Travel Desk & Cab Management specification
IsActiveHotelAdmin = IsHotelAdminOrManager
