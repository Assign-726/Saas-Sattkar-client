"""Liveness / readiness probe for load balancers, uptime monitors and deploy scripts.

GET /healthz/ → 200 {"status": "ok", ...} when the database and cache answer, else 503.
It reveals nothing about versions or configuration.
"""
import time

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def healthz(request):
    checks = {}
    ok = True
    start = time.perf_counter()
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'down'
        ok = False
    try:
        cache.set('healthz', 1, 10)
        checks['cache'] = 'ok' if cache.get('healthz') == 1 else 'degraded'
    except Exception:
        checks['cache'] = 'down'
    checks['ms'] = round((time.perf_counter() - start) * 1000, 1)
    return JsonResponse({'status': 'ok' if ok else 'unavailable', **checks}, status=200 if ok else 503)
