"""
Global Template Context Processors for Application Constants.
"""
from restaurant_project import constants

def app_constants(request):
    """
    Exposes centralized constants to all Django templates.
    """
    unread_inquiries = 0
    pending_applications = 0
    if getattr(request, 'user', None) and request.user.is_authenticated and request.user.is_superuser:
        try:
            from super_admin.models import CustomerInquiry, JobApplication
            unread_inquiries = CustomerInquiry.objects.filter(is_read=False).count()
            pending_applications = JobApplication.objects.filter(status='PENDING').count()
        except Exception:
            pass

    # Dynamic Master Admin Module Entitlements
    restaurant = getattr(request, 'restaurant', None)
    if not restaurant:
        try:
            from super_admin.models import Restaurant
            restaurant = Restaurant.objects.first()
        except Exception:
            restaurant = None

    show_website = constants.is_module_enabled('SHOW_WEBSITE_PAGES', restaurant)
    enable_website_cms = constants.is_module_enabled('ENABLE_WEBSITE_CMS', restaurant)
    is_menu_only_website = constants.is_module_enabled('IS_MENU_ONLY_WEBSITE', restaurant)
    has_full_website = bool(show_website and enable_website_cms and not is_menu_only_website)
    has_menu_website = bool(show_website and is_menu_only_website)
    enable_alcohol = constants.is_module_enabled('ENABLE_ALCOHOL_VAT', restaurant)
    enable_club = constants.is_module_enabled('ENABLE_CLUB_EVENTS_MODULE', restaurant)
    enable_hotel = constants.is_module_enabled('ENABLE_HOTEL_MODULE', restaurant)
    enable_restro = constants.is_module_enabled('ENABLE_RESTAURANT_MODULE', restaurant)
    enable_hotel_web = constants.is_module_enabled('ENABLE_HOTEL_WEBSITE', restaurant)

    return {
        'APP_NAME': constants.APP_NAME,
        'APP_NAME_PREFIX': constants.APP_NAME_PREFIX,
        'APP_NAME_SUFFIX': constants.APP_NAME_SUFFIX,
        'APP_TAGLINE': constants.APP_TAGLINE,
        'APP_SUPPORT_URL': constants.APP_SUPPORT_URL,
        'MAX_ALLOWED_BUSINESSES': constants.get_effective_max_businesses(restaurant),
        'MAX_ALLOWED_BRANCHES': constants.get_effective_max_branches(restaurant),
        'DEFAULT_CURRENCY': constants.DEFAULT_CURRENCY,
        'DEFAULT_STATE': constants.DEFAULT_STATE,
        'SHOW_WEBSITE_PAGES': show_website,
        'ENABLE_WEBSITE_CMS': enable_website_cms,
        'IS_MENU_ONLY_WEBSITE': is_menu_only_website,
        'has_full_website': has_full_website,
        'has_menu_website': has_menu_website,
        'WEBSITE_MODE': 'minimal_ordering' if is_menu_only_website else 'full',
        'ENABLE_ALCOHOL_VAT': enable_alcohol,
        'SHOW_ALCOHOL_MODULE': enable_alcohol,
        'ENABLE_CLUB_EVENTS_MODULE': enable_club,
        'SHOW_CLUB_EVENTS_MODULE': enable_club,
        'ENABLE_HOTEL_MODULE': enable_hotel,
        'SHOW_HOTEL_MODULE': enable_hotel,
        'ENABLE_RESTAURANT_MODULE': enable_restro,
        'SHOW_RESTAURANT_MODULE': enable_restro,
        'ENABLE_HOTEL_WEBSITE': enable_hotel_web,
        'SHOW_HOTEL_WEBSITE': enable_hotel_web,
        'UNREAD_INQUIRIES_COUNT': unread_inquiries,
        'PENDING_APPLICATIONS_COUNT': pending_applications,
        # Subscription & Overdue Lockout Status
        'IS_SUBSCRIPTION_LOCKED': bool(restaurant and restaurant.subscription_is_locked),
        'IS_SUBSCRIPTION_OVERDUE': bool(restaurant and restaurant.subscription_is_overdue),
        'IS_SUBSCRIPTION_SUSPENDED': bool(restaurant and restaurant.subscription_status == 'SUSPENDED'),
        'SUBSCRIPTION_GRACE_EXPIRED': bool(restaurant and restaurant.subscription_grace_period_expired),
        'SUBSCRIPTION_GRACE_DAYS_REMAINING': getattr(restaurant, 'subscription_grace_days_remaining', 0) if restaurant else 0,
        'SUBSCRIPTION_OVERDUE_DAYS': getattr(restaurant, 'subscription_overdue_days', 0) if restaurant else 0,
        'SUBSCRIPTION_STATUS': getattr(restaurant, 'subscription_status', 'ACTIVE') if restaurant else 'ACTIVE',
        'SUBSCRIPTION_PLAN_NAME': getattr(restaurant, 'subscription_plan_name', '') if restaurant else '',
        'SUBSCRIPTION_NEXT_BILLING_DATE': getattr(restaurant, 'subscription_next_billing_date', None) if restaurant else None,
        # Free Trial lifecycle
        'IS_SUBSCRIPTION_TRIAL': bool(restaurant and getattr(restaurant, 'subscription_is_trial', False)),
        'SUBSCRIPTION_TRIAL_DAYS_REMAINING': getattr(restaurant, 'subscription_trial_days_remaining', 0) if restaurant else 0,
        'SUBSCRIPTION_TRIAL_ENDS_AT': getattr(restaurant, 'subscription_trial_ends_at', None) if restaurant else None,
    }

