def restaurant_context(request):
    """
    Context processor to provide `current_restaurant`, `current_branch`,
    `available_branches`, and `is_primary_admin` to all templates.
    """
    if not request.user.is_authenticated:
        return {}
    if not hasattr(request.user, 'restaurant_profile'):
        if not request.user.is_superuser:
            return {}
        # A platform admin opening a business: take it from the URL (/dashboard/<slug>/…)
        profile = _platform_admin_profile(request)
        if profile is None:
            return {}
    else:
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
            if active_branch:
                request.session[f'active_branch_{restaurant.id}'] = active_branch.id

    if active_branch and not active_branch.slug:
        active_branch.save()

    branches = restaurant.branches.all() if restaurant else []
    rslug = active_branch.slug if (active_branch and active_branch.slug) else (restaurant.slug if restaurant else '')
    
    from core.models import CreditNote, RestaurantSettings
    from restaurant_project import constants
    settings_obj = RestaurantSettings.get_settings(restaurant) if restaurant else None
    next_cn_number = CreditNote.generate_credit_note_number(restaurant, branch=active_branch) if restaurant else 'CN-PREVIEW'
    manager_pin_val = getattr(settings_obj, 'manager_pin', '1234') if settings_obj else '1234'

    # Role & Permission resolution
    user = request.user
    user_role = getattr(user, 'role', 'RESTAURANT_ADMIN')
    if not user_role and profile:
        user_role = profile.role
    user_role = user_role or 'RESTAURANT_ADMIN'

    is_super = getattr(user, 'is_superuser', False)
    is_gm = (user_role == 'GENERAL_MANAGER' or is_super)
    is_club_user = (user_role in ['CLUB_ADMIN', 'GATE_SCANNER'])
    is_hotel_user = (user_role in ['HOTEL_ADMIN', 'FRONT_DESK', 'HOUSEKEEPING'])
    is_kitchen_user = (user_role == 'KITCHEN')
    is_cashier_user = (user_role == 'CASHIER')
    is_waiter_user = (user_role == 'WAITER')
    is_dining_mgmt = (user_role in ['RESTAURANT_ADMIN', 'GENERAL_MANAGER'] or is_super)

    # Permission flags from profile
    can_analytics = is_dining_mgmt or getattr(profile, 'can_view_analytics', False)
    can_inventory = is_dining_mgmt or getattr(profile, 'can_view_inventory', False)
    can_menu = is_dining_mgmt or getattr(profile, 'can_manage_menu', True)
    can_settings = is_dining_mgmt or getattr(profile, 'can_manage_settings', False)

    # Restaurant module visibility
    enable_restaurant_globally = getattr(constants, 'ENABLE_RESTAURANT_MODULE', True)
    branch_has_restaurant = (getattr(active_branch, 'has_restaurant_module', True) if active_branch else True) and enable_restaurant_globally

    # Branch Feature Check helper
    branch_feat = lambda k: (active_branch.is_feature_enabled(k) if (active_branch and hasattr(active_branch, 'is_feature_enabled')) else True)

    # Feature Section Visibility Flags:
    show_dining_dashboard = is_dining_mgmt and branch_has_restaurant and not is_club_user and not is_kitchen_user and not is_hotel_user
    show_dining_analytics = can_analytics and branch_has_restaurant and not is_club_user and not is_hotel_user and branch_feat('analytics')
    show_pos_group = (is_dining_mgmt or is_cashier_user or is_waiter_user) and branch_has_restaurant and not is_club_user and not is_kitchen_user and not is_hotel_user and branch_feat('pos')
    show_dinein_group = (is_dining_mgmt or is_cashier_user or is_waiter_user) and branch_has_restaurant and not is_club_user and not is_kitchen_user and not is_hotel_user and branch_feat('dine_in')
    show_menu_group = (is_dining_mgmt or can_menu or is_cashier_user or is_waiter_user) and branch_has_restaurant and not is_club_user and not is_hotel_user and branch_feat('menu')
    show_stock_group = can_inventory and branch_has_restaurant and not is_club_user and not is_hotel_user and branch_feat('inventory')
    show_parties = is_dining_mgmt and branch_has_restaurant and not is_club_user and not is_kitchen_user and not is_cashier_user and not is_waiter_user and not is_hotel_user and branch_feat('inventory')
    show_staff = (is_dining_mgmt or user_role == 'CLUB_ADMIN') and not is_kitchen_user and not is_cashier_user and not is_waiter_user and not is_hotel_user and not (user_role in ['GATE_SCANNER', 'FRONT_DESK', 'HOUSEKEEPING']) and branch_feat('staff')
    show_settings = can_settings and not is_club_user and not is_hotel_user
    show_delivery = (is_dining_mgmt or is_cashier_user) and branch_has_restaurant and not is_club_user and not is_kitchen_user and not is_hotel_user and branch_feat('delivery')

    # Club module visibility
    enable_club_globally = getattr(constants, 'ENABLE_CLUB_EVENTS_MODULE', True)
    branch_has_club = getattr(active_branch, 'has_club_module', False) if active_branch else False
    show_club_events = enable_club_globally and (is_club_user or (is_dining_mgmt and branch_has_club) or is_super)

    # Custom roles narrow what the built-in role shows (super_admin.permissions)
    if getattr(profile, 'custom_role_id', None) and not is_super:
        from super_admin.permissions import NONE
        role = profile.custom_role
        opened = lambda area: role.level(area) != NONE
        show_dining_dashboard = show_dining_dashboard and opened('dashboard')
        show_dining_analytics = show_dining_analytics and opened('analytics')
        show_pos_group = show_pos_group and (opened('pos') or opened('kitchen'))
        show_dinein_group = show_dinein_group and (opened('dine_in') or opened('kitchen'))
        show_menu_group = show_menu_group and opened('menu')
        show_stock_group = show_stock_group and opened('inventory')
        show_parties = show_parties and opened('inventory')
        show_staff = show_staff and opened('staff')
        show_settings = show_settings and opened('settings')
        show_club_events = show_club_events and opened('events')
        show_delivery = show_delivery and opened('delivery')

    # New Zomato / Swiggy orders waiting for acceptance, for the sidebar badges
    delivery_pending = {}
    if show_delivery and active_branch:
        from django.db.models import Count
        from integrations.models import ExternalOrderLink
        delivery_pending = dict(ExternalOrderLink.objects.filter(branch=active_branch, source__in=['zomato', 'swiggy'], platform_status='placed')
                                .values_list('source').annotate(n=Count('id')))

    return {
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'available_branches': branches,
        'is_primary_admin': profile.is_primary_admin or (profile.branch is None),
        'rslug': rslug,
        'next_credit_note_number': next_cn_number,
        'manager_pin': manager_pin_val,
        # Role & Permissions Context
        'user_role': user_role,
        'is_gm': is_gm,
        'is_club_user': is_club_user,
        'is_hotel_user': is_hotel_user,
        'is_kitchen_user': is_kitchen_user,
        'is_cashier_user': is_cashier_user,
        'is_waiter_user': is_waiter_user,
        'is_dining_mgmt': is_dining_mgmt,
        # Visibility flags
        'show_dining_dashboard': show_dining_dashboard,
        'show_dining_analytics': show_dining_analytics,
        'show_pos_group': show_pos_group,
        'show_dinein_group': show_dinein_group,
        'show_menu_group': show_menu_group,
        'show_stock_group': show_stock_group,
        'show_parties': show_parties,
        'show_staff': show_staff,
        'show_settings': show_settings,
        'show_club_events': show_club_events,
        'show_delivery': show_delivery,
        'delivery_pending': delivery_pending,
        'branch_has_restaurant': branch_has_restaurant,
    }


def _platform_admin_profile(request):
    """An unsaved stand-in profile so templates work when a superuser opens a business admin."""
    match = getattr(request, 'resolver_match', None)
    slug = (match.kwargs.get('restaurant_slug') if match else None) if match else None
    if not slug:
        return None
    from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
    branch = RestaurantBranch.objects.filter(slug__iexact=slug).select_related('restaurant').first()
    restaurant = branch.restaurant if branch else Restaurant.objects.filter(slug=slug).first()
    if restaurant is None:
        return None
    return RestaurantUser(user=request.user, restaurant=restaurant, branch=branch,
                          role=RestaurantUser.Role.GENERAL_MANAGER, is_primary_admin=True)
