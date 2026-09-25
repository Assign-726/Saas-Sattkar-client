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
        'ENABLE_ALCOHOL_VAT': constants.ENABLE_ALCOHOL_VAT,
        'SHOW_ALCOHOL_MODULE': constants.ENABLE_ALCOHOL_VAT,
        'ENABLE_CLUB_EVENTS_MODULE': constants.ENABLE_CLUB_EVENTS_MODULE,
        'SHOW_CLUB_EVENTS_MODULE': constants.ENABLE_CLUB_EVENTS_MODULE,
        'ENABLE_HOTEL_MODULE': constants.ENABLE_HOTEL_MODULE,
        'SHOW_HOTEL_MODULE': constants.ENABLE_HOTEL_MODULE,
        'UNREAD_INQUIRIES_COUNT': unread_inquiries,
        'PENDING_APPLICATIONS_COUNT': pending_applications,
    }

