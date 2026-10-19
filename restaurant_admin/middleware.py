"""
BranchContextMiddleware
=======================
Attaches ``request.restaurant`` and ``request.active_branch``
(both ``None`` for unauthenticated / non-admin requests) to every
incoming request so that views can simply read ``request.active_branch``
instead of repeating the same 10-line resolution block.

Resolution priority:
  1. ``profile.branch``  — branch-locked admin (cannot switch)
  2. Session key ``active_branch_<restaurant_id>``  — previously chosen
  3. First branch of the restaurant  — sensible default

The middleware **also** persists the resolved branch into the session so
the context processor and other views all agree on the same branch.
"""


import re

_KITCHEN_ORDER_PAGES = re.compile(r'^orders/\d+/(kot|mark-ready)/$')


def _dashboard_tail(path):
    """'/dashboard/<slug>/kitchen/api/' -> 'kitchen/api/' ('' outside the dashboard)."""
    if not path.startswith('/dashboard/'):
        return ''
    parts = path.split('/', 3)  # ['', 'dashboard', '<slug>', '<rest>']
    return parts[3] if len(parts) > 3 else ''


class BranchContextMiddleware:
    """Resolve and attach the active branch to each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Defaults — always set so views can rely on them
        request.restaurant = None
        request.active_branch = None

        user = request.user
        if user.is_authenticated and hasattr(user, 'restaurant_profile'):
            try:
                profile = user.restaurant_profile
                restaurant = profile.restaurant
                request.restaurant = restaurant

                # 1. Resolve Active Branch First so module & RBAC checks can inspect it
                if profile.branch:
                    request.active_branch = profile.branch
                elif restaurant:
                    session_key = f'active_branch_{restaurant.id}'
                    branch_id = request.session.get(session_key)

                    branch = None
                    if branch_id:
                        branch = restaurant.branches.filter(id=branch_id).first()

                    if not branch:
                        branch = restaurant.branches.first()
                        if branch:
                            request.session[session_key] = branch.id

                    request.active_branch = branch

                active_branch = request.active_branch
                slug = restaurant.slug if restaurant else 'default'
                # Admin links carry either the restaurant slug or a branch slug
                # (/dashboard/<slug>/…), so role rules look only at the part after it.
                tail = _dashboard_tail(request.path)

                # RBAC & Module Isolation:
                user_role = getattr(user, 'role', None) or profile.role
                if not user.is_superuser:
                    # 0. Branch-level Restaurant Module Restriction:
                    # If this branch does NOT have the restaurant/dining module enabled:
                    if active_branch and not getattr(active_branch, 'has_restaurant_module', True) and request.path.startswith('/dashboard/'):
                        from django.shortcuts import redirect
                        if not request.path.endswith('/logout/') and not request.path.endswith('/switch-branch/') and not '/api/' in request.path:
                            if getattr(active_branch, 'has_hotel_module', False):
                                return redirect('hotel_admin:dashboard', restaurant_slug=slug)
                            elif getattr(active_branch, 'has_club_module', False):
                                return redirect('restaurant_admin:club_events_list', restaurant_slug=slug)

                    # 1. Hotel Roles isolation (Front Desk & Housekeeping locked to PMS; Hotel Admin operates in Hotel PMS)
                    if user_role in ['HOTEL_ADMIN', 'FRONT_DESK', 'HOUSEKEEPING'] and request.path.startswith('/dashboard/'):
                        from django.shortcuts import redirect
                        if user_role == 'HOUSEKEEPING':
                            return redirect('hotel_admin:rooms', restaurant_slug=slug)
                        if user_role == 'FRONT_DESK':
                            return redirect('hotel_admin:dashboard', restaurant_slug=slug)
                        if user_role == 'HOTEL_ADMIN':
                            if tail.startswith('staff/'):
                                return redirect('hotel_admin:staff_list', restaurant_slug=slug)
                            elif not request.path.endswith('/logout/'):
                                return redirect('hotel_admin:dashboard', restaurant_slug=slug)

                    # 2. Gate Scanner Isolation (strictly door scanner & attendee guestlist)
                    if user_role == 'GATE_SCANNER' and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        is_allowed = (
                            tail.startswith('club-events/gate-scanner/') or
                            tail.startswith('club-events/guestlist/') or
                            '/guestlist/' in request.path or
                            '/booking/' in request.path or
                            '/api/' in request.path or
                            request.path.endswith('/logout/')
                        )
                        if not is_allowed:
                            from django.shortcuts import redirect
                            return redirect('restaurant_admin:club_gate_scanner', restaurant_slug=slug)

                    # 3. Club Admin is directed to Club Events and Staff Management (Gate Scanners)
                    if user_role == 'CLUB_ADMIN' and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        if not tail.startswith('club-events/') and not tail.startswith('staff/') and not '/api/' in request.path and not request.path.endswith('/switch-branch/') and not request.path.endswith('/logout/'):
                            from django.shortcuts import redirect
                            return redirect('restaurant_admin:club_events_list', restaurant_slug=slug)

                    # 4. Kitchen KDS Isolation & Permitted Features
                    if user_role == 'KITCHEN' and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        is_kitchen_allowed = (
                            tail.startswith('kitchen/') or
                            tail.startswith('orders/stream/') or          # live new-KOT stream for the KDS
                            tail.startswith('api/notifications/') or      # bell: new KOT alerts
                            bool(_KITCHEN_ORDER_PAGES.match(tail)) or     # print KOT / mark ready from the KDS
                            request.path.startswith('/api/') or
                            request.path.endswith('/logout/') or
                            (getattr(profile, 'can_view_analytics', False) and tail.startswith('analytics/')) or
                            (getattr(profile, 'can_view_inventory', False) and tail.startswith(('inventory/', 'purchases/', 'parties/', 'challans/'))) or
                            (getattr(profile, 'can_manage_menu', False) and tail.startswith('menu/')) or
                            (getattr(profile, 'can_manage_settings', False) and tail.startswith('settings/'))
                        )
                        if not is_kitchen_allowed:
                            from django.shortcuts import redirect
                            return redirect('restaurant_admin:kitchen_kds', restaurant_slug=slug)

                    # 5. Cashier & Waiter restrictions (respect granular permissions)
                    if user_role in ['CASHIER', 'WAITER'] and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        restricted = ['staff/']
                        if not getattr(profile, 'can_view_analytics', False):
                            restricted.append('analytics/')
                        if not getattr(profile, 'can_manage_settings', False):
                            restricted.append('settings/')
                        if not getattr(profile, 'can_view_inventory', False):
                            restricted.extend(['inventory/', 'purchases/', 'parties/', 'challans/'])
                        if not getattr(profile, 'can_manage_menu', False):
                            restricted.append('menu/')

                        if tail.startswith(tuple(restricted)):
                            from django.shortcuts import redirect
                            try:
                                from django.contrib import messages
                                messages.warning(request, "Staff accounts do not have permission to access management settings or analytics.")
                            except Exception:
                                pass
                            return redirect('restaurant_admin:transaction_desk_create', restaurant_slug=slug)


            except Exception:
                # Never crash a request due to middleware errors
                pass

        return self.get_response(request)
