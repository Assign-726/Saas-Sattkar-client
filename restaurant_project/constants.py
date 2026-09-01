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


# ==========================================
# 1. Branding & Identity
# ==========================================
APP_NAME = os.environ.get('APP_NAME', 'Hotel Management System')
APP_NAME_PREFIX = os.environ.get('APP_NAME_PREFIX', 'Hotel Management')
APP_NAME_SUFFIX = os.environ.get('APP_NAME_SUFFIX', 'System')
APP_TAGLINE = os.environ.get('APP_TAGLINE', 'Sign in to access your administrative dashboard')
APP_SUPPORT_URL = os.environ.get('APP_SUPPORT_URL', 'http://localhost:3000')

# ==========================================
# 2. License & Operational Limits
# ==========================================
# Maximum allowed business entities (default: 1)
MAX_ALLOWED_BUSINESSES = _get_env_int('MAX_ALLOWED_BUSINESSES', 1)

# Maximum allowed branch outlets per business (default: 5; set 0 for unlimited)
MAX_ALLOWED_BRANCHES = _get_env_int('MAX_ALLOWED_BRANCHES', 5)

# ==========================================
# 3. Regional & Operational Defaults
# ==========================================
DEFAULT_CURRENCY = os.environ.get('DEFAULT_CURRENCY', '₹')
DEFAULT_STATE = os.environ.get('DEFAULT_STATE', 'Rajasthan')
