from rest_framework import permissions
from super_admin.models import RestaurantBranch


class IsHotelAdminOrManager(permissions.BasePermission):
    """
    Permission class to verify:
    1. request.user.branch.has_hotel_module == True
    2. request.user.role in ['HOTEL_ADMIN', 'GENERAL_MANAGER']
    """
    message = "Access restricted: requires Hotel Admin or General Manager role with Hotel PMS enabled for this branch."
    code = "FEATURE_GATED_HOTEL_MODULE"

    def has_permission(self, request, view):
        # 1. Superuser / platform staff bypass
        if request.user and (request.user.is_superuser or request.user.is_staff):
            return True

        # 2. Must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False

        # 3. Resolve user role
        user_role = getattr(request.user, 'role', None)
        if not user_role and hasattr(request.user, 'restaurant_profile'):
            user_role = getattr(request.user.restaurant_profile, 'role', None)

        if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER']:
            return False

        # 4. Resolve user branch
        branch = None

        # Check explicit branch in URL or request params
        branch_id = (
            view.kwargs.get('branch_id') or
            view.kwargs.get('branch_pk') or
            request.query_params.get('branch_id') or
            request.query_params.get('branch') or
            (request.data.get('branch_id') if hasattr(request, 'data') and isinstance(request.data, dict) else None) or
            (request.data.get('branch') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        )
        if branch_id:
            try:
                branch = RestaurantBranch.objects.filter(id=branch_id).first()
            except Exception:
                pass

        # Check request.user.branch property
        if not branch:
            branch = getattr(request.user, 'branch', None)

        # Fallback to restaurant profile branch
        if not branch and hasattr(request.user, 'restaurant_profile'):
            profile = request.user.restaurant_profile
            branch = profile.branch
            if not branch and profile.restaurant:
                branch = profile.restaurant.branches.filter(has_hotel_module=True).first() or profile.restaurant.branches.first()

        if not branch:
            return False

        # Check request.user.branch.has_hotel_module == True
        return bool(getattr(branch, 'has_hotel_module', False))

    def has_object_permission(self, request, view, obj):
        if request.user and request.user.is_superuser:
            return True

        branch = getattr(obj, 'branch', None)
        if not branch and hasattr(obj, 'room'):
            branch = getattr(obj.room, 'branch', None)

        if not branch:
            return True

        return bool(getattr(branch, 'has_hotel_module', False))
