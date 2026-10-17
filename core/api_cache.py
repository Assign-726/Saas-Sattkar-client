"""Short-lived response cache for the public catalog endpoints (menu, categories,
restaurant / branch / landing pages, blog).

Those endpoints are hit on every guest page view but change rarely. Anonymous GETs are
served from the cache for ``CATALOG_CACHE_SECONDS`` (default 60). Saving or deleting any
catalog model bumps a version number, so the next request rebuilds. With the default
in-process cache each worker has its own copy, so other workers can lag by at most the
TTL; set ``REDIS_URL`` for one shared cache and instant invalidation.

Live data (table status, orders, settings with zones) is never cached here.
"""
import hashlib
import json

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

VERSION_KEY = 'apicache:catalog:v'


def _version():
    v = cache.get(VERSION_KEY)
    if v is None:
        cache.add(VERSION_KEY, 1, None)
        v = cache.get(VERSION_KEY, 1)
    return v


def bump(*args, **kwargs):
    try:
        cache.incr(VERSION_KEY)
    except ValueError:
        cache.set(VERSION_KEY, 2, None)


def _key(request):
    raw = f'{request.get_host()}|{request.get_full_path()}'
    return f'apicache:catalog:{_version()}:{hashlib.sha1(raw.encode()).hexdigest()}'


class CatalogCacheMixin:
    """For read-only viewsets: caches anonymous ``list`` / ``retrieve`` responses."""

    def _cached(self, request, produce):
        seconds = getattr(settings, 'CATALOG_CACHE_SECONDS', 60)
        if not seconds or request.method != 'GET' or request.user.is_authenticated:
            return produce()
        key = _key(request)
        data = cache.get(key)
        hit = data is not None
        if not hit:
            response = produce()
            if response.status_code != 200:
                return response
            data = json.loads(JSONRenderer().render(response.data))
            cache.set(key, data, seconds)
        response = Response(data)
        response['Cache-Control'] = f'public, max-age={min(seconds, 30)}'
        response['X-Cache'] = 'HIT' if hit else 'MISS'
        return response

    def list(self, request, *args, **kwargs):
        return self._cached(request, lambda: super(CatalogCacheMixin, self).list(request, *args, **kwargs))

    def retrieve(self, request, *args, **kwargs):
        return self._cached(request, lambda: super(CatalogCacheMixin, self).retrieve(request, *args, **kwargs))


CATALOG_MODELS = (
    'core.Category', 'core.SubCategory', 'core.MenuItem', 'core.RestaurantLanding', 'core.Event', 'core.GalleryImage',
    'core.TeamMember', 'core.Review', 'core.WhyChooseUs', 'core.BlogPost', 'core.BlogComment', 'core.RestaurantSettings',
    'super_admin.Restaurant', 'super_admin.RestaurantBranch',
)


def connect():
    for label in CATALOG_MODELS:
        post_save.connect(bump, sender=label, dispatch_uid=f'apicache_save_{label}')
        post_delete.connect(bump, sender=label, dispatch_uid=f'apicache_delete_{label}')
