"""
Centralized Application Constants & Configuration Settings.
Reads values from environment variables (.env) with safe fallback defaults.
"""
import os

def _get_env_int(key: str, default: int) -> int:
    try:
        val = os.environ.get(key)
        if val is not None and str(val).strip().isdigit():
            return int(val.strip())
        return default
    except Exception:
        return default

def _get_env_bool(key: str, default: bool = True) -> bool:
    try:
        val = os.environ.get(key)
        if val is None:
            return default
        return str(val).strip().lower() in ('true', '1', 'yes', 'on', 't')
    except Exception:
        return default


# ==========================================
# 1. Branding & Identity
# ==========================================
APP_NAME = os.environ.get('APP_NAME', 'Restaurant Management Minku')
APP_NAME_PREFIX = os.environ.get('APP_NAME_PREFIX', 'Restaurant Management')
APP_NAME_SUFFIX = os.environ.get('APP_NAME_SUFFIX', 'Minku')
APP_TAGLINE = os.environ.get('APP_TAGLINE', 'Sign in to access your administrative dashboard')
APP_SUPPORT_URL = os.environ.get('APP_SUPPORT_URL', 'http://localhost:3000')

# ==========================================
# 2. License & Operational Limits
# ==========================================
# Maximum allowed business entities (default: 1)
MAX_ALLOWED_BUSINESSES = _get_env_int('MAX_ALLOWED_BUSINESSES', 1)

# Maximum allowed branch outlets per business (default: 2; set 0 for unlimited)
MAX_ALLOWED_BRANCHES = _get_env_int('MAX_ALLOWED_BRANCHES', 2)

def is_master_connected(restaurant=None) -> bool:
    """
    Returns True only if this Super Admin instance is actively connected and
    synchronized with the Master Admin Cloud.
    Returns False when Master Admin is unreachable, returned an error (404/500),
    or is not yet synced, causing the system to fall back to .env configuration.
    """
    try:
        from super_admin.services import MasterBillingService
        return MasterBillingService.is_connected(restaurant)
    except Exception:
        return False


def get_effective_max_branches(restaurant=None) -> int:
    """
    Returns dynamic branch quota synced from Master Admin WHEN CONNECTED.
    Falls back to MAX_ALLOWED_BRANCHES (.env) if offline or not connected.
    """
    if restaurant is None:
        try:
            from super_admin.models import Restaurant
            restaurant = Restaurant.objects.first()
        except Exception:
            restaurant = None

    if not is_master_connected(restaurant):
        return MAX_ALLOWED_BRANCHES

    if restaurant and hasattr(restaurant, 'get_max_allowed_branches'):
        return restaurant.get_max_allowed_branches()
    return MAX_ALLOWED_BRANCHES


def get_effective_max_businesses(restaurant=None) -> int:
    """
    Returns dynamic business quota synced from Master Admin WHEN CONNECTED.
    Falls back to MAX_ALLOWED_BUSINESSES (.env) if offline or not connected.
    """
    if restaurant is None:
        try:
            from super_admin.models import Restaurant
            restaurant = Restaurant.objects.first()
        except Exception:
            restaurant = None

    if not is_master_connected(restaurant):
        return MAX_ALLOWED_BUSINESSES

    if restaurant and hasattr(restaurant, 'get_max_allowed_businesses'):
        return restaurant.get_max_allowed_businesses()
    return MAX_ALLOWED_BUSINESSES


# ==========================================
# 3. Regional & Operational Defaults
# ==========================================
DEFAULT_CURRENCY = os.environ.get('DEFAULT_CURRENCY', '₹')
DEFAULT_STATE = os.environ.get('DEFAULT_STATE', 'Rajasthan')

# ==========================================
# 4. Feature Flags / Module Toggles
# ==========================================
# Module defaults loaded from environment (.env) as resilient fallback
_DEFAULT_SHOW_WEBSITE_PAGES = _get_env_bool(
    'SHOW_WEBSITE_PAGES',
    _get_env_bool('ENABLE_WEBSITE_CMS', _get_env_bool('ENABLE_LANDING_PAGE', True))
)
_DEFAULT_ENABLE_ALCOHOL_VAT = _get_env_bool(
    'ENABLE_ALCOHOL_VAT',
    _get_env_bool('ENABLE_LIQUOR_VAT', _get_env_bool('ENABLE_ALCOHOL_SERVICE', True))
)
_DEFAULT_ENABLE_CLUB_EVENTS_MODULE = _get_env_bool(
    'ENABLE_CLUB_EVENTS_MODULE',
    _get_env_bool('ENABLE_CLUB_MODULE', True)
)
_DEFAULT_ENABLE_HOTEL_MODULE = _get_env_bool(
    'ENABLE_HOTEL_MODULE',
    _get_env_bool('ENABLE_HOTEL_PMS_MODULE', True)
)
_DEFAULT_ENABLE_RESTAURANT_MODULE = _get_env_bool(
    'ENABLE_RESTAURANT_MODULE',
    _get_env_bool('ENABLE_POS_MODULE', True)
)
_DEFAULT_ENABLE_HOTEL_WEBSITE = _get_env_bool(
    'ENABLE_HOTEL_WEBSITE',
    False
)


def is_module_enabled(module_name: str, restaurant=None) -> bool:
    """
    Dynamically checks if a module is active for a restaurant.
    Prioritizes real-time database state synced from Master Admin WHEN CONNECTED.
    Falls back to .env defaults if offline or not connected to Master Admin.
    """
    target = restaurant
    if target is None:
        try:
            from super_admin.models import Restaurant
            target = Restaurant.objects.first()
        except Exception:
            target = None

    # Fallback to .env defaults when disconnected from Master Admin
    if not is_master_connected(target):
        name = module_name.upper()
        if name in ('SHOW_WEBSITE_PAGES', 'WEBSITE'):
            return _DEFAULT_SHOW_WEBSITE_PAGES
        if name in ('ENABLE_WEBSITE_CMS', 'WEBSITE_CMS'):
            return _DEFAULT_SHOW_WEBSITE_PAGES
        if name in ('IS_MENU_ONLY_WEBSITE', 'MENU_ONLY_WEBSITE'):
            return False
        if name in ('ENABLE_ALCOHOL_VAT', 'SHOW_ALCOHOL_MODULE', 'ALCOHOL'):
            return _DEFAULT_ENABLE_ALCOHOL_VAT
        if name in ('ENABLE_CLUB_EVENTS_MODULE', 'SHOW_CLUB_EVENTS_MODULE', 'CLUB'):
            return _DEFAULT_ENABLE_CLUB_EVENTS_MODULE
        if name in ('ENABLE_HOTEL_MODULE', 'SHOW_HOTEL_MODULE', 'HOTEL'):
            return _DEFAULT_ENABLE_HOTEL_MODULE
        if name in ('ENABLE_RESTAURANT_MODULE', 'SHOW_RESTAURANT_MODULE', 'RESTAURANT', 'POS'):
            return _DEFAULT_ENABLE_RESTAURANT_MODULE
        if name in ('ENABLE_HOTEL_WEBSITE', 'SHOW_HOTEL_WEBSITE', 'HOTEL_WEBSITE'):
            return _DEFAULT_ENABLE_HOTEL_WEBSITE
        return True

    # Connected: use real-time synced database fields
    name = module_name.upper()
    if name in ('SHOW_WEBSITE_PAGES', 'WEBSITE'):
        if target and hasattr(target, 'feature_website_pages') and target.feature_website_pages is not None:
            return bool(target.feature_website_pages)
        return _DEFAULT_SHOW_WEBSITE_PAGES

    if name in ('ENABLE_WEBSITE_CMS', 'WEBSITE_CMS'):
        if target and hasattr(target, 'feature_website_cms') and target.feature_website_cms is not None:
            return bool(target.feature_website_cms and getattr(target, 'feature_website_pages', True))
        return _DEFAULT_SHOW_WEBSITE_PAGES

    if name in ('IS_MENU_ONLY_WEBSITE', 'MENU_ONLY_WEBSITE'):
        if target and hasattr(target, 'is_menu_only_website'):
            return bool(target.is_menu_only_website)
        return False

    if name in ('ENABLE_ALCOHOL_VAT', 'SHOW_ALCOHOL_MODULE', 'ALCOHOL'):
        if target and hasattr(target, 'feature_alcohol_vat') and target.feature_alcohol_vat is not None:
            return bool(target.feature_alcohol_vat)
        return _DEFAULT_ENABLE_ALCOHOL_VAT

    if name in ('ENABLE_CLUB_EVENTS_MODULE', 'SHOW_CLUB_EVENTS_MODULE', 'CLUB'):
        if target and hasattr(target, 'feature_club_events') and target.feature_club_events is not None:
            return bool(target.feature_club_events)
        return _DEFAULT_ENABLE_CLUB_EVENTS_MODULE

    if name in ('ENABLE_HOTEL_MODULE', 'SHOW_HOTEL_MODULE', 'HOTEL'):
        if target and hasattr(target, 'feature_hotel_pms') and target.feature_hotel_pms is not None:
            return bool(target.feature_hotel_pms)
        return _DEFAULT_ENABLE_HOTEL_MODULE

    if name in ('ENABLE_RESTAURANT_MODULE', 'SHOW_RESTAURANT_MODULE', 'RESTAURANT', 'POS'):
        if target and hasattr(target, 'feature_restaurant_pos') and target.feature_restaurant_pos is not None:
            return bool(target.feature_restaurant_pos)
        return _DEFAULT_ENABLE_RESTAURANT_MODULE

    if name in ('ENABLE_HOTEL_WEBSITE', 'SHOW_HOTEL_WEBSITE', 'HOTEL_WEBSITE'):
        if target and hasattr(target, 'feature_hotel_website') and target.feature_hotel_website is not None:
            return bool(target.feature_hotel_website)
        return _DEFAULT_ENABLE_HOTEL_WEBSITE

    return True


def __getattr__(name: str):
    """
    PEP 562: Dynamically resolve module toggles against Master Admin state in DB.
    """
    if name in ('SHOW_WEBSITE_PAGES', 'ENABLE_WEBSITE'):
        return is_module_enabled('SHOW_WEBSITE_PAGES')
    if name in ('ENABLE_WEBSITE_CMS', 'WEBSITE_CMS'):
        return is_module_enabled('ENABLE_WEBSITE_CMS')
    if name in ('IS_MENU_ONLY_WEBSITE', 'MENU_ONLY_WEBSITE'):
        return is_module_enabled('IS_MENU_ONLY_WEBSITE')
    if name in ('ENABLE_ALCOHOL_VAT', 'SHOW_ALCOHOL_MODULE'):
        return is_module_enabled('ENABLE_ALCOHOL_VAT')
    if name in ('ENABLE_CLUB_EVENTS_MODULE', 'SHOW_CLUB_EVENTS_MODULE'):
        return is_module_enabled('ENABLE_CLUB_EVENTS_MODULE')
    if name in ('ENABLE_HOTEL_MODULE', 'SHOW_HOTEL_MODULE'):
        return is_module_enabled('ENABLE_HOTEL_MODULE')
    if name in ('ENABLE_RESTAURANT_MODULE', 'SHOW_RESTAURANT_MODULE'):
        return is_module_enabled('ENABLE_RESTAURANT_MODULE')
    if name in ('ENABLE_HOTEL_WEBSITE', 'SHOW_HOTEL_WEBSITE'):
        return is_module_enabled('ENABLE_HOTEL_WEBSITE')
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

