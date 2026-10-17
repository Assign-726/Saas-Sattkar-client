"""Tenant scoping for the JSON APIs.

Every business login (owner, manager, cashier, front desk…) is ``is_staff`` so it can open
the admin panels, which means ``is_staff`` says nothing about *which* business a user
belongs to. Only ``is_superuser`` is a platform admin. Everything here answers "may this
user touch this restaurant / branch?" so API views never trust ``is_staff`` or a
``?branch_id=`` from the request on their own.
"""
from rest_framework.permissions import BasePermission

from super_admin.models import RestaurantBranch


def is_platform_admin(user):
    return bool(user and user.is_authenticated and user.is_active and user.is_superuser)


def profile(user):
    if not (user and user.is_authenticated):
        return None
    return getattr(user, 'restaurant_profile', None)


def user_restaurant_id(user):
    p = profile(user)
    return p.restaurant_id if p else None


def allowed_branches(user):
    """Branches the user may act on: all for platform admins, the assigned branch for
    branch-bound staff, otherwise every branch of their restaurant."""
    if is_platform_admin(user):
        return RestaurantBranch.objects.all()
    p = profile(user)
    if not p:
        return RestaurantBranch.objects.none()
    if p.branch_id:
        return RestaurantBranch.objects.filter(pk=p.branch_id)
    return RestaurantBranch.objects.filter(restaurant_id=p.restaurant_id)


def can_access_branch(user, branch):
    if branch is None:
        return False
    if is_platform_admin(user):
        return True
    p = profile(user)
    if not p or branch.restaurant_id != p.restaurant_id:
        return False
    return not p.branch_id or p.branch_id == branch.pk


def is_business_staff(user, restaurant=None):
    """A logged-in admin-panel user of ``restaurant`` (any restaurant when None),
    or a platform admin."""
    if not (user and user.is_authenticated and user.is_active and user.is_staff):
        return False
    if user.is_superuser:
        return True
    rid = user_restaurant_id(user)
    if rid is None:
        return False
    return restaurant is None or getattr(restaurant, 'pk', restaurant) == rid


def scope(qs, user, field='restaurant'):
    """Limit ``qs`` to the user's restaurant through ``field`` (a lookup path to the
    restaurant FK). Platform admins see everything; anyone else without a business sees nothing."""
    if is_platform_admin(user):
        return qs
    rid = user_restaurant_id(user)
    if rid is None:
        return qs.none()
    return qs.filter(**{f'{field}_id' if '__' not in field else f'{field}__id': rid})


class IsBusinessStaff(BasePermission):
    """Admin-panel users of some business, or platform admins. Pair with ``scope()`` so
    they only ever see their own rows."""
    message = 'Staff access only.'

    def has_permission(self, request, view):
        return is_business_staff(request.user)


class TenantSlugMiddleware:
    """Admin URLs carry the business in the path (/dashboard/<slug>/…, /hotel-admin/<slug>/…).
    Some views load the restaurant straight from that slug, so check it here once: the slug must
    be the user's own restaurant or one of its branches (only their own branch for branch-bound
    staff). Platform admins may open any business."""

    NAMESPACES = ('restaurant_admin', 'hotel_admin')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        slug = view_kwargs.get('restaurant_slug')
        match = getattr(request, 'resolver_match', None)
        if not slug or not match or match.namespace not in self.NAMESPACES:
            return None
        user = request.user
        if not user.is_authenticated or is_platform_admin(user):
            return None  # the view sends anonymous users to the login page
        p = profile(user)
        if p is None:
            return None  # the view answers "no restaurant associated with this account"
        slug = slug.lower()
        restaurant = p.restaurant
        allowed = {(restaurant.slug or '').lower()} if restaurant else set()
        branches = RestaurantBranch.objects.filter(pk=p.branch_id) if p.branch_id else \
            RestaurantBranch.objects.filter(restaurant_id=p.restaurant_id)
        allowed.update(s.lower() for s in branches.values_list('slug', flat=True) if s)
        if slug in allowed:
            return None
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('You do not have access to this business.')
