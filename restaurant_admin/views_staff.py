import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.db import transaction

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser, UserSecuritySetting
from restaurant_admin.views import is_admin, resolve_restaurant, get_active_branch

logger = logging.getLogger(__name__)


def is_management_user(user):
    """Only managers and administrators can access staff management."""
    if not (user.is_authenticated and user.is_staff):
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'restaurant_profile', None)
    if not profile:
        return False
    role = getattr(user, 'role', None) or profile.role
    return role in [
        RestaurantUser.Role.GENERAL_MANAGER,
        RestaurantUser.Role.RESTAURANT_ADMIN,
        RestaurantUser.Role.HOTEL_ADMIN,
        RestaurantUser.Role.CLUB_ADMIN,
    ]


def redirect_to_home(user, restaurant_slug):
    """Safely route a user to their dedicated portal screen."""
    profile = getattr(user, 'restaurant_profile', None)
    role = getattr(user, 'role', None) or (profile.role if profile else None)
    if role == RestaurantUser.Role.CLUB_ADMIN:
        return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant_slug)
    if role in [RestaurantUser.Role.HOTEL_ADMIN, RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)
    if role == RestaurantUser.Role.KITCHEN:
        return redirect('restaurant_admin:kitchen_kds', restaurant_slug=restaurant_slug)
    if role == RestaurantUser.Role.WAITER:
        return redirect('restaurant_admin:waiter_tables', restaurant_slug=restaurant_slug)
    if role == RestaurantUser.Role.CASHIER:
        return redirect('restaurant_admin:transaction_desk_create', restaurant_slug=restaurant_slug)
    return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)


@login_required
def staff_list(request, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Staff management requires manager or administrator privileges.")
        return redirect_to_home(request.user, restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    profile = getattr(request.user, 'restaurant_profile', None)
    user_role = getattr(request.user, 'role', None) or (profile.role if profile else 'RESTAURANT_ADMIN')
    user_role = user_role or 'RESTAURANT_ADMIN'

    # If Hotel Admin accesses restaurant staff list, redirect to native Hotel PMS staff management
    if user_role == RestaurantUser.Role.HOTEL_ADMIN:
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant.slug)

    is_super = getattr(request.user, 'is_superuser', False)
    is_gm = (user_role == RestaurantUser.Role.GENERAL_MANAGER or is_super)

    # Base query for restaurant's staff profiles
    staff_qs = RestaurantUser.objects.filter(restaurant=restaurant).select_related('user', 'branch', 'created_by')

    # Strict scoping based on manager role
    if is_gm:
        allowed_roles = RestaurantUser.Role.choices
        available_branches = restaurant.branches.all()
    elif user_role == RestaurantUser.Role.CLUB_ADMIN:
        allowed_roles = [
            (RestaurantUser.Role.GATE_SCANNER, 'Gate Scanner / Door Staff'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []
        staff_qs = staff_qs.filter(role=RestaurantUser.Role.GATE_SCANNER)
        if profile and profile.branch:
            staff_qs = staff_qs.filter(branch=profile.branch)
    else:  # RESTAURANT_ADMIN
        # Restaurant branch admin strictly manages their branch's operational floor & kitchen staff
        allowed_roles = [
            (RestaurantUser.Role.CASHIER, 'Cashier / Biller'),
            (RestaurantUser.Role.KITCHEN, 'Kitchen / KDS Station'),
            (RestaurantUser.Role.WAITER, 'Waiter / Captain'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []
        staff_qs = staff_qs.filter(role__in=[RestaurantUser.Role.CASHIER, RestaurantUser.Role.KITCHEN, RestaurantUser.Role.WAITER])
        if profile and profile.branch:
            staff_qs = staff_qs.filter(branch=profile.branch)

    # Optional role filter
    role_filter = request.GET.get('role', '').strip()
    if role_filter:
        valid_keys = [r[0] for r in allowed_roles]
        if role_filter in valid_keys:
            staff_qs = staff_qs.filter(role=role_filter)

    # Optional branch filter (only available for General Manager across outlets)
    branch_filter = request.GET.get('branch', '').strip()
    if is_gm and branch_filter and branch_filter.isdigit():
        staff_qs = staff_qs.filter(branch_id=int(branch_filter))

    staff_members = staff_qs.order_by('-created_at')

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'staff_members': staff_members,
        'available_branches': available_branches,
        'roles': allowed_roles,
        'selected_role': role_filter,
        'selected_branch': branch_filter,
        'user_role': user_role,
        'is_gm': is_gm,
        'is_club_user': (user_role == RestaurantUser.Role.CLUB_ADMIN),
        'is_hotel_user': (user_role == RestaurantUser.Role.HOTEL_ADMIN),
        'active_menu': 'staff',
        'active_group': 'settings',
    }
    return render(request, 'restaurant_admin/staff/staff_list.html', context)


@login_required
def staff_create(request, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Staff management requires manager or administrator privileges.")
        return redirect_to_home(request.user, restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    profile = getattr(request.user, 'restaurant_profile', None)
    user_role = getattr(request.user, 'role', None) or (profile.role if profile else 'RESTAURANT_ADMIN')
    user_role = user_role or 'RESTAURANT_ADMIN'

    # Hotel Admin should create staff via PMS-native Hotel Staff Management
    if user_role == RestaurantUser.Role.HOTEL_ADMIN:
        return redirect('hotel_admin:staff_create', restaurant_slug=restaurant.slug)

    is_super = getattr(request.user, 'is_superuser', False)
    is_gm = (user_role == RestaurantUser.Role.GENERAL_MANAGER or is_super)

    # Determine permitted roles and branch choices
    if is_gm:
        allowed_roles = RestaurantUser.Role.choices
        available_branches = restaurant.branches.all()
    elif user_role == RestaurantUser.Role.CLUB_ADMIN:
        allowed_roles = [
            (RestaurantUser.Role.GATE_SCANNER, 'Gate Scanner / Door Staff'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []
    else:  # RESTAURANT_ADMIN
        allowed_roles = [
            (RestaurantUser.Role.CASHIER, 'Cashier / Biller'),
            (RestaurantUser.Role.KITCHEN, 'Kitchen / KDS Station'),
            (RestaurantUser.Role.WAITER, 'Waiter / Captain'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []

    default_role = allowed_roles[0][0] if allowed_roles else RestaurantUser.Role.CASHIER
    default_branch = profile.branch if (profile and profile.branch) else active_branch

    errors = {}
    form_data = {
        'username': '',
        'first_name': '',
        'last_name': '',
        'email': '',
        'pos_pin': '',
        'role': default_role,
        'branch_id': str(default_branch.id) if default_branch else '',
        'can_apply_discounts': True if default_role == RestaurantUser.Role.CASHIER else False,
        'can_issue_refunds': True if default_role == RestaurantUser.Role.CASHIER else False,
        'can_manage_menu': False,
        'can_manage_settings': False,
        'can_view_analytics': False,
        'can_view_inventory': False,
        'is_2fa_enabled': False,
    }

    if request.method == 'POST':
        form_data['username'] = request.POST.get('username', '').strip()
        form_data['first_name'] = request.POST.get('first_name', '').strip()
        form_data['last_name'] = request.POST.get('last_name', '').strip()
        form_data['email'] = request.POST.get('email', '').strip()
        password = request.POST.get('password', '').strip()
        form_data['pos_pin'] = request.POST.get('pos_pin', '').strip()
        form_data['role'] = request.POST.get('role', '').strip()
        form_data['branch_id'] = request.POST.get('branch_id', '').strip()

        # Role-specific permissions parsing
        role = form_data['role']
        if role == RestaurantUser.Role.GATE_SCANNER:
            # Gate Scanner permissions (Door / Event specific)
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'  # View Sales & Ticket Prices
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'    # Undo Check-Ins
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on' # Issue Door Passes
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'        # View QR Pass Details
            form_data['can_manage_settings'] = False
            form_data['can_view_inventory'] = False
            form_data['pos_pin'] = None
        elif role == RestaurantUser.Role.KITCHEN:
            # Kitchen permissions
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'  # Kitchen & Sales Analytics
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'  # Stock & Inventory
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'        # Menu Item Availability
            form_data['can_apply_discounts'] = False
            form_data['can_issue_refunds'] = False
            form_data['can_manage_settings'] = False
            form_data['pos_pin'] = None
        elif role == RestaurantUser.Role.WAITER:
            # Waiter permissions
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_issue_refunds'] = False
            form_data['can_manage_settings'] = False
            form_data['can_view_inventory'] = False
        elif role == RestaurantUser.Role.CASHIER:
            # Cashier permissions
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_manage_settings'] = False
        else:
            # Admin / GM roles
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_manage_settings'] = request.POST.get('can_manage_settings') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'
        form_data['is_2fa_enabled'] = request.POST.get('is_2fa_enabled') == 'on'

        # Validation
        if not form_data['username']:
            errors['username'] = "Username is required."
        elif User.objects.filter(username__iexact=form_data['username']).exists():
            errors['username'] = "This username is already taken. Please choose another."

        if not password:
            errors['password'] = "Password is required."
        elif len(password) < 6:
            errors['password'] = "Password must be at least 6 characters."

        valid_role_keys = [r[0] for r in allowed_roles]
        if form_data['role'] not in valid_role_keys:
            errors['role'] = "Selected role is not authorized."

        if form_data['is_2fa_enabled'] and not form_data['email']:
            errors['email'] = "Email is required when Two-Factor Authentication (2FA) is turned ON."

        # Branch resolution
        if is_gm:
            branch = None
            if form_data['branch_id'] and form_data['branch_id'].isdigit():
                branch = restaurant.branches.filter(id=int(form_data['branch_id'])).first()
        else:
            branch = default_branch

        if not errors:
            try:
                with transaction.atomic():
                    # Create User
                    new_user = User.objects.create_user(
                        username=form_data['username'],
                        email=form_data['email'],
                        password=password,
                        first_name=form_data['first_name'],
                        last_name=form_data['last_name']
                    )
                    new_user.is_staff = True
                    new_user.save()

                    # Add to RestaurantAdmin group
                    try:
                        grp, _ = Group.objects.get_or_create(name="RestaurantAdmin")
                        new_user.groups.add(grp)
                    except Exception:
                        pass

                    # Create RestaurantUser profile
                    ru = RestaurantUser.objects.create(
                        user=new_user,
                        restaurant=restaurant,
                        branch=branch,
                        role=form_data['role'],
                        is_primary_admin=False,
                        can_apply_discounts=form_data['can_apply_discounts'],
                        can_issue_refunds=form_data['can_issue_refunds'],
                        can_manage_menu=form_data['can_manage_menu'],
                        can_manage_settings=form_data['can_manage_settings'],
                        can_view_analytics=form_data['can_view_analytics'],
                        can_view_inventory=form_data['can_view_inventory'],
                        pos_pin=form_data['pos_pin'] or None,
                        created_by=request.user,
                    )

                    # Security / 2FA Setting
                    UserSecuritySetting.objects.update_or_create(
                        user=new_user,
                        defaults={'is_2fa_enabled': form_data['is_2fa_enabled']}
                    )

                messages.success(request, f"Staff member '{new_user.username}' created successfully as [{ru.get_role_display()}].")
                return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
            except Exception as e:
                logger.error(f"Error creating staff user: {e}", exc_info=True)
                errors['general'] = f"Failed to create staff member: {e}"

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'available_branches': available_branches,
        'allowed_roles': allowed_roles,
        'form_data': form_data,
        'errors': errors,
        'is_edit': False,
        'is_gm': is_gm,
        'is_club_user': (user_role == RestaurantUser.Role.CLUB_ADMIN),
        'is_hotel_user': (user_role == RestaurantUser.Role.HOTEL_ADMIN),
        'active_menu': 'staff',
        'active_group': 'settings',
    }
    return render(request, 'restaurant_admin/staff/staff_form.html', context)


@login_required
def staff_edit(request, pk, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Staff management requires manager or administrator privileges.")
        return redirect_to_home(request.user, restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    profile = getattr(request.user, 'restaurant_profile', None)
    user_role = getattr(request.user, 'role', None) or (profile.role if profile else 'RESTAURANT_ADMIN')
    user_role = user_role or 'RESTAURANT_ADMIN'

    staff_user = get_object_or_404(RestaurantUser, pk=pk, restaurant=restaurant)
    target_user = staff_user.user

    # Hotel Admin edits staff via PMS-native Hotel Staff Management
    if user_role == RestaurantUser.Role.HOTEL_ADMIN:
        return redirect('hotel_admin:staff_edit', restaurant_slug=restaurant.slug, user_id=staff_user.user_id)

    is_super = getattr(request.user, 'is_superuser', False)
    is_gm = (user_role == RestaurantUser.Role.GENERAL_MANAGER or is_super)

    # Disallow editing superusers by non-superusers
    if target_user.is_superuser and not request.user.is_superuser:
        messages.error(request, "You cannot edit superuser accounts.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    # Branch boundary: Non-GM cannot edit staff from another branch
    if not is_gm and profile and profile.branch:
        if staff_user.branch_id != profile.branch_id:
            messages.error(request, "Permission denied: You can only manage staff belonging to your assigned branch outlet.")
            return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    # Role boundary:
    if is_gm:
        allowed_roles = RestaurantUser.Role.choices
        available_branches = restaurant.branches.all()
    elif user_role == RestaurantUser.Role.CLUB_ADMIN:
        allowed_roles = [
            (RestaurantUser.Role.GATE_SCANNER, 'Gate Scanner / Door Staff'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []
        if staff_user.role != RestaurantUser.Role.GATE_SCANNER:
            messages.error(request, "Permission denied: Event admins can only manage gate scanner door staff.")
            return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
    else:  # RESTAURANT_ADMIN
        allowed_roles = [
            (RestaurantUser.Role.CASHIER, 'Cashier / Biller'),
            (RestaurantUser.Role.KITCHEN, 'Kitchen / KDS Station'),
            (RestaurantUser.Role.WAITER, 'Waiter / Captain'),
        ]
        available_branches = restaurant.branches.filter(id=profile.branch_id) if (profile and profile.branch) else []
        if staff_user.role not in [RestaurantUser.Role.CASHIER, RestaurantUser.Role.KITCHEN, RestaurantUser.Role.WAITER]:
            messages.error(request, "Permission denied: Restaurant branch admins can only manage restaurant operational staff.")
            return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    sec_setting = UserSecuritySetting.objects.filter(user=target_user).first()
    is_2fa = sec_setting.is_2fa_enabled if sec_setting else False

    errors = {}
    form_data = {
        'username': target_user.username,
        'first_name': target_user.first_name,
        'last_name': target_user.last_name,
        'email': target_user.email,
        'pos_pin': staff_user.pos_pin or '',
        'role': staff_user.role,
        'branch_id': str(staff_user.branch_id) if staff_user.branch_id else '',
        'can_apply_discounts': staff_user.can_apply_discounts,
        'can_issue_refunds': staff_user.can_issue_refunds,
        'can_manage_menu': staff_user.can_manage_menu,
        'can_manage_settings': staff_user.can_manage_settings,
        'can_view_analytics': staff_user.can_view_analytics,
        'can_view_inventory': staff_user.can_view_inventory,
        'is_2fa_enabled': is_2fa,
        'is_active': target_user.is_active,
    }

    if request.method == 'POST':
        form_data['first_name'] = request.POST.get('first_name', '').strip()
        form_data['last_name'] = request.POST.get('last_name', '').strip()
        form_data['email'] = request.POST.get('email', '').strip()
        new_password = request.POST.get('password', '').strip()
        form_data['pos_pin'] = request.POST.get('pos_pin', '').strip()
        form_data['role'] = request.POST.get('role', '').strip()
        form_data['branch_id'] = request.POST.get('branch_id', '').strip()

        # Role-specific permissions parsing
        role = form_data['role']
        if role == RestaurantUser.Role.GATE_SCANNER:
            # Gate Scanner permissions (Door / Event specific)
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'  # View Sales & Ticket Prices
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'    # Undo Check-Ins
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on' # Issue Door Passes
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'        # View QR Pass Details
            form_data['can_manage_settings'] = False
            form_data['can_view_inventory'] = False
            form_data['pos_pin'] = None
        elif role == RestaurantUser.Role.KITCHEN:
            # Kitchen permissions
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'  # Kitchen & Sales Analytics
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'  # Stock & Inventory
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'        # Menu Item Availability
            form_data['can_apply_discounts'] = False
            form_data['can_issue_refunds'] = False
            form_data['can_manage_settings'] = False
            form_data['pos_pin'] = None
        elif role == RestaurantUser.Role.WAITER:
            # Waiter permissions
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_issue_refunds'] = False
            form_data['can_manage_settings'] = False
            form_data['can_view_inventory'] = False
        elif role == RestaurantUser.Role.CASHIER:
            # Cashier permissions
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_manage_settings'] = False
        else:
            # Admin / GM roles
            form_data['can_apply_discounts'] = request.POST.get('can_apply_discounts') == 'on'
            form_data['can_issue_refunds'] = request.POST.get('can_issue_refunds') == 'on'
            form_data['can_manage_menu'] = request.POST.get('can_manage_menu') == 'on'
            form_data['can_manage_settings'] = request.POST.get('can_manage_settings') == 'on'
            form_data['can_view_analytics'] = request.POST.get('can_view_analytics') == 'on'
            form_data['can_view_inventory'] = request.POST.get('can_view_inventory') == 'on'
        form_data['is_2fa_enabled'] = request.POST.get('is_2fa_enabled') == 'on'
        form_data['is_active'] = request.POST.get('is_active') == 'on'

        if new_password and len(new_password) < 6:
            errors['password'] = "Password must be at least 6 characters."

        valid_role_keys = [r[0] for r in allowed_roles]
        if form_data['role'] not in valid_role_keys:
            errors['role'] = "Selected role is not authorized."

        if form_data['is_2fa_enabled'] and not form_data['email']:
            errors['email'] = "Email is required when Two-Factor Authentication (2FA) is turned ON."

        if not errors:
            try:
                with transaction.atomic():
                    target_user.first_name = form_data['first_name']
                    target_user.last_name = form_data['last_name']
                    target_user.email = form_data['email']
                    target_user.is_active = form_data['is_active']
                    if new_password:
                        target_user.set_password(new_password)
                    target_user.save()

                    if is_gm:
                        branch = None
                        if form_data['branch_id'] and form_data['branch_id'].isdigit():
                            branch = restaurant.branches.filter(id=int(form_data['branch_id'])).first()
                        staff_user.branch = branch

                    staff_user.role = form_data['role']
                    staff_user.pos_pin = form_data['pos_pin'] or None
                    staff_user.can_apply_discounts = form_data['can_apply_discounts']
                    staff_user.can_issue_refunds = form_data['can_issue_refunds']
                    staff_user.can_manage_menu = form_data['can_manage_menu']
                    staff_user.can_manage_settings = form_data['can_manage_settings']
                    staff_user.can_view_analytics = form_data['can_view_analytics']
                    staff_user.can_view_inventory = form_data['can_view_inventory']
                    staff_user.save()

                    UserSecuritySetting.objects.update_or_create(
                        user=target_user,
                        defaults={'is_2fa_enabled': form_data['is_2fa_enabled']}
                    )

                messages.success(request, f"Staff member '{target_user.username}' updated successfully.")
                return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
            except Exception as e:
                logger.error(f"Error updating staff member: {e}", exc_info=True)
                errors['general'] = f"Failed to update staff member: {e}"

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'available_branches': available_branches,
        'allowed_roles': allowed_roles,
        'form_data': form_data,
        'staff_user': staff_user,
        'target_user': target_user,
        'errors': errors,
        'is_edit': True,
        'is_gm': is_gm,
        'is_club_user': (user_role == RestaurantUser.Role.CLUB_ADMIN),
        'is_hotel_user': (user_role == RestaurantUser.Role.HOTEL_ADMIN),
        'active_menu': 'staff',
        'active_group': 'settings',
    }
    return render(request, 'restaurant_admin/staff/staff_form.html', context)


@login_required
def staff_toggle_active(request, pk, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied.")
        return redirect_to_home(request.user, restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    staff_user = get_object_or_404(RestaurantUser, pk=pk, restaurant=restaurant)
    target_user = staff_user.user

    profile = getattr(request.user, 'restaurant_profile', None)
    user_role = getattr(request.user, 'role', None) or (profile.role if profile else 'RESTAURANT_ADMIN')
    user_role = user_role or 'RESTAURANT_ADMIN'
    is_super = getattr(request.user, 'is_superuser', False)
    is_gm = (user_role == RestaurantUser.Role.GENERAL_MANAGER or is_super)

    if target_user == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if target_user.is_superuser:
        messages.error(request, "Cannot deactivate superuser accounts.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if not is_gm and profile and profile.branch:
        if staff_user.branch_id != profile.branch_id:
            messages.error(request, "Permission denied: You cannot modify staff of another branch outlet.")
            return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if user_role == RestaurantUser.Role.CLUB_ADMIN and staff_user.role != RestaurantUser.Role.GATE_SCANNER:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
    elif user_role == RestaurantUser.Role.HOTEL_ADMIN and staff_user.role not in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
    elif user_role == RestaurantUser.Role.RESTAURANT_ADMIN and staff_user.role not in [RestaurantUser.Role.CASHIER, RestaurantUser.Role.KITCHEN, RestaurantUser.Role.WAITER]:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    status_str = "activated" if target_user.is_active else "deactivated"
    messages.success(request, f"Account '{target_user.username}' has been {status_str}.")
    return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)


@login_required
def staff_delete(request, pk, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied.")
        return redirect_to_home(request.user, restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    staff_user = get_object_or_404(RestaurantUser, pk=pk, restaurant=restaurant)
    target_user = staff_user.user

    profile = getattr(request.user, 'restaurant_profile', None)
    user_role = getattr(request.user, 'role', None) or (profile.role if profile else 'RESTAURANT_ADMIN')
    user_role = user_role or 'RESTAURANT_ADMIN'
    is_super = getattr(request.user, 'is_superuser', False)
    is_gm = (user_role == RestaurantUser.Role.GENERAL_MANAGER or is_super)

    if target_user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if target_user.is_superuser:
        messages.error(request, "Cannot delete superuser accounts.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if not is_gm and profile and profile.branch:
        if staff_user.branch_id != profile.branch_id:
            messages.error(request, "Permission denied: You cannot delete staff of another branch outlet.")
            return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    if user_role == RestaurantUser.Role.CLUB_ADMIN and staff_user.role != RestaurantUser.Role.GATE_SCANNER:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
    elif user_role == RestaurantUser.Role.HOTEL_ADMIN and staff_user.role not in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
    elif user_role == RestaurantUser.Role.RESTAURANT_ADMIN and staff_user.role not in [RestaurantUser.Role.CASHIER, RestaurantUser.Role.KITCHEN, RestaurantUser.Role.WAITER]:
        messages.error(request, "Permission denied.")
        return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)

    username = target_user.username
    target_user.delete()  # Cascades to RestaurantUser
    messages.success(request, f"Staff account '{username}' was permanently deleted.")
    return redirect('restaurant_admin:staff_list', restaurant_slug=restaurant.slug)
