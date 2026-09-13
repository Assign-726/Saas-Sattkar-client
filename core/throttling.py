import time
from rest_framework.throttling import SimpleRateThrottle
from django.core.cache import cache


def get_client_ip(request):
    """
    Extract client IP address, respecting X-Forwarded-For if behind a reverse proxy.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or '127.0.0.1'


# -------------------------------------------------------------------
# DRF ViewSet Throttles
# -------------------------------------------------------------------

class ContactAnonRateThrottle(SimpleRateThrottle):
    """Throttle contact inquiries: 5 requests per minute per IP."""
    scope = 'contact_anon'
    rate = '5/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_contact_min_{self.get_ident(request)}"


class ContactDayRateThrottle(SimpleRateThrottle):
    """Throttle contact inquiries: 25 requests per day per IP."""
    scope = 'contact_day'
    rate = '25/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_contact_day_{self.get_ident(request)}"


class NewsletterAnonRateThrottle(SimpleRateThrottle):
    """Throttle newsletter subscriptions: 5 requests per minute per IP."""
    scope = 'newsletter_anon'
    rate = '5/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_newsletter_min_{self.get_ident(request)}"


class NewsletterDayRateThrottle(SimpleRateThrottle):
    """Throttle newsletter subscriptions: 20 requests per day per IP."""
    scope = 'newsletter_day'
    rate = '20/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_newsletter_day_{self.get_ident(request)}"


class JobApplicationAnonRateThrottle(SimpleRateThrottle):
    """Throttle job applications: 5 requests per minute per IP."""
    scope = 'job_app_anon'
    rate = '5/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_job_app_min_{self.get_ident(request)}"


class JobApplicationDayRateThrottle(SimpleRateThrottle):
    """Throttle job applications: 20 requests per day per IP."""
    scope = 'job_app_day'
    rate = '20/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_job_app_day_{self.get_ident(request)}"


class BlogCommentAnonRateThrottle(SimpleRateThrottle):
    """Throttle blog comments: 3 comments per minute per IP."""
    scope = 'blog_comment_anon'
    rate = '3/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_blog_comment_min_{self.get_ident(request)}"


class BlogCommentDayRateThrottle(SimpleRateThrottle):
    """Throttle blog comments: 15 comments per day per IP."""
    scope = 'blog_comment_day'
    rate = '15/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_blog_comment_day_{self.get_ident(request)}"


# -------------------------------------------------------------------
# Standard Django View Throttle Helper
# -------------------------------------------------------------------

def check_view_rate_limit(request, scope, rate_string):
    """
    Enforces sliding-window rate limiting on standard Django Views (non-DRF).
    rate_string format: '<count>/<period>' (e.g. '5/min', '25/day').
    Returns (is_allowed: bool, wait_seconds: int).
    """
    user = getattr(request, 'user', None)
    if user and user.is_authenticated and user.is_staff:
        return True, 0

    try:
        num_str, period_str = rate_string.split('/')
        max_requests = int(num_str)
        duration_map = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        duration = duration_map.get(period_str[0].lower(), 60)
    except Exception:
        return True, 0

    ip = get_client_ip(request)
    cache_key = f"view_throttle_{scope}_{period_str[0]}_{ip}"

    now = time.time()
    history = cache.get(cache_key, [])

    # Prune expired timestamps
    while history and history[-1] <= now - duration:
        history.pop()

    if len(history) >= max_requests:
        wait = duration - (now - history[-1])
        return False, max(1, int(wait) + 1)

    history.insert(0, now)
    cache.set(cache_key, history, duration)
    return True, 0
