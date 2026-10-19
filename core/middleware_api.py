"""Cross-cutting behaviour for the JSON APIs.

* ``X-Request-ID`` on every response (reuses a sane incoming one from the proxy), so a
  guest's error report can be matched to the server log line.
* ``Server-Timing`` on API responses and a warning log for slow API calls
  (``API_SLOW_REQUEST_MS``, default 800 ms).
* ``Cache-Control: no-store`` on API responses that didn't choose their own caching, so
  orders, bookings and guest details are never kept by browsers or shared proxies.
* gzip for JSON API responses only. HTML pages carrying CSRF tokens are left alone
  (BREACH); nginx can still compress static files.
"""
import logging
import re
import time
import uuid

from django.conf import settings
from django.middleware.gzip import GZipMiddleware

logger = logging.getLogger('api')

_SAFE_ID = re.compile(r'^[A-Za-z0-9._-]{8,64}$')
API_PREFIXES = ('/api/',)


def _is_api(path):
    return path.startswith(API_PREFIXES)


class ApiObservabilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = getattr(settings, 'API_SLOW_REQUEST_MS', 800)

    def __call__(self, request):
        incoming = request.META.get('HTTP_X_REQUEST_ID', '')
        request.request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed = (time.perf_counter() - start) * 1000

        response['X-Request-ID'] = request.request_id
        if _is_api(request.path):
            response['Server-Timing'] = f'app;dur={elapsed:.1f}'
            if not response.has_header('Cache-Control'):
                response['Cache-Control'] = 'no-store'
            if elapsed >= self.slow_ms:
                logger.warning('slow api %s %s -> %s in %.0f ms [%s]', request.method, request.path,
                               response.status_code, elapsed, request.request_id)
            elif response.status_code >= 500:
                logger.error('api error %s %s -> %s [%s]', request.method, request.path, response.status_code, request.request_id)
        return response


class ApiGZipMiddleware(GZipMiddleware):
    def process_response(self, request, response):
        if not _is_api(request.path) or 'json' not in response.get('Content-Type', ''):
            return response
        return super().process_response(request, response)
