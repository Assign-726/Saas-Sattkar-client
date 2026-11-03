"""Per-request memo for lookups that repeat for every row of a list (outlet settings,
auto-applied charges…). It only exists while ``RequestCacheMiddleware`` is handling a
request, so management commands, tasks and tests that call models directly always read
fresh data. Writers call ``forget()`` so a save is visible within the same request.
"""
import contextvars
from contextlib import contextmanager

_store = contextvars.ContextVar('request_cache', default=None)


@contextmanager
def scope():
    token = _store.set({})
    try:
        yield
    finally:
        _store.reset(token)


def memo(key, compute):
    store = _store.get()
    if store is None:
        return compute()
    if key not in store:
        store[key] = compute()
    return store[key]


def forget(prefix):
    """Drop every memo whose key starts with ``prefix`` (a tuple)."""
    store = _store.get()
    if store:
        for key in [k for k in store if k[:len(prefix)] == prefix]:
            del store[key]


class RequestCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with scope():
            return self.get_response(request)


# Lookups that the middlewares, context processors and views each repeated on every admin page.
def first_restaurant():
    def load():
        from super_admin.models import Restaurant
        return Restaurant.objects.first()
    return memo(('first_restaurant',), load)


def first_branch(restaurant):
    return memo(('first_branch', restaurant.pk), lambda: restaurant.branches.first())


def branch_by_id(restaurant, branch_id):
    branch_id = int(branch_id)
    return memo(('branch', restaurant.pk, branch_id), lambda: restaurant.branches.filter(id=branch_id).first())
