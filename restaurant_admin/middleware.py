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

                # 1. Branch-locked admin (e.g. a single-outlet manager)
                if profile.branch:
                    request.active_branch = profile.branch
                elif restaurant:
                    # 2. Primary admin — read from session
                    session_key = f'active_branch_{restaurant.id}'
                    branch_id = request.session.get(session_key)

                    branch = None
                    if branch_id:
                        branch = restaurant.branches.filter(id=branch_id).first()

                    # 3. Fallback to first branch
                    if not branch:
                        branch = restaurant.branches.first()
                        if branch:
                            # Persist so context_processor and other views agree
                            request.session[session_key] = branch.id

                    request.active_branch = branch
            except Exception:
                # Never crash a request due to middleware errors
                pass

        return self.get_response(request)
