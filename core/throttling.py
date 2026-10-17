import time
from rest_framework.throttling import SimpleRateThrottle
from django.core.cache import cache


def get_client_ip(request):
    """
    The client's IP. Behind ``TRUSTED_PROXY_COUNT`` reverse proxies (nginx = 1) the real
    address is that many entries from the *right* of X-Forwarded-For; the left-most entry
    is whatever the client typed, so trusting it would let anyone dodge rate limits.
    """
    from django.conf import settings
    proxies = getattr(settings, 'TRUSTED_PROXY_COUNT', 1)
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff and proxies > 0:
        hops = [h.strip() for h in xff.split(',') if h.strip()]
        if hops:
            return hops[-min(proxies, len(hops))]
    return request.META.get('REMOTE_ADDR') or '127.0.0.1'


class _IpThrottle(SimpleRateThrottle):
    """Per-IP throttle that also applies to logged-in users (staff included)."""

    def get_cache_key(self, request, view):
        return f"throttle_{self.scope}_{get_client_ip(request)}"


class PaymentRateThrottle(_IpThrottle):
    """Checkout, order placement and payment verification: 30 a minute per IP."""
    scope = 'payment'
    rate = '30/min'


class CouponRateThrottle(_IpThrottle):
    """Coupon checks: 20 a minute per IP, so codes can't be guessed by brute force."""
    scope = 'coupon'
    rate = '20/min'


class PublicLookupThrottle(_IpThrottle):
    """Public look-ups by booking reference: 60 a minute per IP."""
    scope = 'public_lookup'
    rate = '60/min'


class LoginRateThrottle(_IpThrottle):
    """API logins: 10 a minute per IP. Per-account lockout is ``login_locked`` below."""
    scope = 'login'
    rate = '10/min'


LOGIN_FAILURE_LIMIT = 8
LOGIN_LOCK_SECONDS = 15 * 60


def _login_key(username):
    return f"login_fail_{str(username or '').strip().lower()[:150]}"


def login_locked(username):
    """True after LOGIN_FAILURE_LIMIT wrong passwords for this account in 15 minutes.
    Only failures count, so nobody can lock a user out by logging in correctly."""
    return bool(username) and cache.get(_login_key(username), 0) >= LOGIN_FAILURE_LIMIT


def record_login_failure(username):
    if not username:
        return
    key = _login_key(username)
    cache.add(key, 0, LOGIN_LOCK_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, LOGIN_LOCK_SECONDS)


def clear_login_failures(username):
    if username:
        cache.delete(_login_key(username))


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


class ReviewAnonRateThrottle(SimpleRateThrottle):
    """Throttle customer review/feedback submissions: 5 submissions per minute per IP."""
    scope = 'review_anon'
    rate = '5/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_review_min_{self.get_ident(request)}"


class ReviewDayRateThrottle(SimpleRateThrottle):
    """Throttle customer review/feedback submissions: 25 submissions per day per IP."""
    scope = 'review_day'
    rate = '25/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"throttle_review_day_{self.get_ident(request)}"


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


# -------------------------------------------------------------------
# JWT endpoints with brute-force protection
# -------------------------------------------------------------------

from rest_framework.exceptions import AuthenticationFailed, Throttled  # noqa: E402
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView  # noqa: E402


class GuardedTokenObtainPairView(TokenObtainPairView):
    """/api/token/ with a per-IP rate limit and a per-account lockout after repeated
    wrong passwords."""
    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        username = request.data.get('username') if isinstance(request.data, dict) else None
        if login_locked(username):
            raise Throttled(wait=LOGIN_LOCK_SECONDS, detail='Too many failed attempts. Try again in 15 minutes.')
        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            record_login_failure(username)
            raise
        clear_login_failures(username)
        return response


class GuardedTokenRefreshView(TokenRefreshView):
    throttle_classes = [PublicLookupThrottle]
