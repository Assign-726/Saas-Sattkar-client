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
                            if request.path.startswith(f'/dashboard/{slug}/staff/'):
                                return redirect('hotel_admin:staff_list', restaurant_slug=slug)
                            elif not request.path.endswith('/logout/'):
                                return redirect('hotel_admin:dashboard', restaurant_slug=slug)

                    # 2. Gate Scanner Isolation (strictly door scanner & attendee guestlist)
                    if user_role == 'GATE_SCANNER' and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        scanner_prefix = f'/dashboard/{slug}/club-events/gate-scanner/'
                        is_allowed = (
                            request.path.startswith(scanner_prefix) or
                            f'/dashboard/{slug}/club-events/guestlist/' in request.path or
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
                        club_prefix = f'/dashboard/{slug}/club-events/'
                        staff_prefix = f'/dashboard/{slug}/staff/'
                        if not request.path.startswith(club_prefix) and not request.path.startswith(staff_prefix) and not '/api/' in request.path and not request.path.endswith('/switch-branch/') and not request.path.endswith('/logout/'):
                            from django.shortcuts import redirect
                            return redirect('restaurant_admin:club_events_list', restaurant_slug=slug)

                    # 4. Kitchen KDS Isolation & Permitted Features
                    if user_role == 'KITCHEN' and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        kds_prefix = f'/dashboard/{slug}/kitchen/'
                        is_kitchen_allowed = (
                            request.path.startswith(kds_prefix) or
                            request.path.startswith('/api/') or
                            request.path.endswith('/logout/') or
                            (getattr(profile, 'can_view_analytics', False) and request.path.startswith(f'/dashboard/{slug}/analytics/')) or
                            (getattr(profile, 'can_view_inventory', False) and any(request.path.startswith(p) for p in [
                                f'/dashboard/{slug}/inventory/',
                                f'/dashboard/{slug}/purchases/',
                                f'/dashboard/{slug}/parties/',
                                f'/dashboard/{slug}/challans/',
                            ])) or
                            (getattr(profile, 'can_manage_menu', False) and request.path.startswith(f'/dashboard/{slug}/menu/')) or
                            (getattr(profile, 'can_manage_settings', False) and request.path.startswith(f'/dashboard/{slug}/settings/'))
                        )
                        if not is_kitchen_allowed:
                            from django.shortcuts import redirect
                            return redirect('restaurant_admin:kitchen_kds', restaurant_slug=slug)

                    # 5. Cashier & Waiter restrictions (respect granular permissions)
                    if user_role in ['CASHIER', 'WAITER'] and request.path.startswith('/dashboard/'):
                        slug = restaurant.slug if restaurant else 'default'
                        restricted_paths = [f'/dashboard/{slug}/staff/']
                        if not getattr(profile, 'can_view_analytics', False):
                            restricted_paths.append(f'/dashboard/{slug}/analytics/')
                        if not getattr(profile, 'can_manage_settings', False):
                            restricted_paths.append(f'/dashboard/{slug}/settings/')
                        if not getattr(profile, 'can_view_inventory', False):
                            restricted_paths.extend([
                                f'/dashboard/{slug}/inventory/',
                                f'/dashboard/{slug}/purchases/',
                                f'/dashboard/{slug}/parties/',
                                f'/dashboard/{slug}/challans/',
                            ])
                        if not getattr(profile, 'can_manage_menu', False):
                            restricted_paths.append(f'/dashboard/{slug}/menu/')

                        if any(request.path.startswith(p) for p in restricted_paths):
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
