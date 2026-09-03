"""
Global Template Context Processors for Application Constants.
"""
from restaurant_project import constants

def app_constants(request):
    """
    Exposes centralized constants to all Django templates.
    """
    return {
        'APP_NAME': constants.APP_NAME,
        'APP_NAME_PREFIX': constants.APP_NAME_PREFIX,
        'APP_NAME_SUFFIX': constants.APP_NAME_SUFFIX,
        'APP_TAGLINE': constants.APP_TAGLINE,
        'APP_SUPPORT_URL': constants.APP_SUPPORT_URL,
        'MAX_ALLOWED_BUSINESSES': constants.MAX_ALLOWED_BUSINESSES,
        'MAX_ALLOWED_BRANCHES': constants.MAX_ALLOWED_BRANCHES,
        'DEFAULT_CURRENCY': constants.DEFAULT_CURRENCY,
        'DEFAULT_STATE': constants.DEFAULT_STATE,
        'SHOW_WEBSITE_PAGES': constants.SHOW_WEBSITE_PAGES,
        'ENABLE_WEBSITE_CMS': constants.SHOW_WEBSITE_PAGES,
    }

