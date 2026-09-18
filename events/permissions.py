from rest_framework import permissions
from super_admin.models import RestaurantBranch


class HasClubModule(permissions.BasePermission):
    """
    Permission check to ensure that the target RestaurantBranch has the 'has_club_module' feature flag enabled.
    Blocks unauthorized API access for standard restaurants without the club add-on.
    """
    message = "The Club & Events module is not enabled for this branch. Please upgrade your subscription to access this feature."
    code = "FEATURE_GATED_CLUB_MODULE"

    def has_permission(self, request, view):
        # Superusers bypass module gating
        if request.user and request.user.is_superuser:
            return True

        # Unauthenticated public endpoints (like browsing published events or checkout)
        # will have their own permission classes (e.g., AllowAny) or branch checks in the view.
        if not request.user or not request.user.is_authenticated:
            return False

        # Attempt to resolve the active branch:
        branch = None

        # 1. From view URL kwargs or request query/body
        branch_id = (
            view.kwargs.get('branch_id') or
            view.kwargs.get('branch_pk') or
            request.query_params.get('branch_id') or
            request.data.get('branch_id') or
            request.data.get('branch')
        )
        if branch_id:
            try:
                branch = RestaurantBranch.objects.filter(id=branch_id).first()
            except Exception:
                pass

        # 2. From user's restaurant profile
        if not branch and hasattr(request.user, 'restaurant_profile'):
            profile = request.user.restaurant_profile
            if profile.branch:
                branch = profile.branch
            elif profile.restaurant:
                # Primary admin fallback: active session branch or first branch
                if hasattr(request, 'active_branch') and request.active_branch:
                    branch = request.active_branch
                else:
                    branch = profile.restaurant.branches.first()

        # 3. From middleware active branch
        if not branch and hasattr(request, 'active_branch') and request.active_branch:
            branch = request.active_branch

        if not branch:
            return False

        return bool(getattr(branch, 'has_club_module', False))

    def has_object_permission(self, request, view, obj):
        if request.user and request.user.is_superuser:
            return True

        # Resolve branch from obj
        branch = getattr(obj, 'branch', None)
        if not branch and hasattr(obj, 'event'):
            branch = getattr(obj.event, 'branch', None)

        if not branch:
            return False

        return bool(getattr(branch, 'has_club_module', False))
