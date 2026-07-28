def restaurant_context(request):
    """
    Context processor to provide `current_restaurant`, `current_branch`,
    `available_branches`, and `is_primary_admin` to all templates.
    """
    if not request.user.is_authenticated or not hasattr(request.user, 'restaurant_profile'):
        return {}
    
    profile = request.user.restaurant_profile
    restaurant = profile.restaurant
    
    # Branch resolution:
    # If user is locked to a specific branch (Branch Admin), active_branch is profile.branch.
    # Otherwise (Primary Business Admin), allow branch switching via GET parameter or session.
    active_branch = profile.branch
    if not active_branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            active_branch = restaurant.branches.filter(id=int(branch_id)).first()
            if active_branch:
                request.session[f'active_branch_{restaurant.id}'] = active_branch.id
        if not active_branch:
            active_branch = restaurant.branches.first()

    branches = restaurant.branches.all() if restaurant else []
    
    return {
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'available_branches': branches,
        'is_primary_admin': profile.is_primary_admin or (profile.branch is None),
    }
