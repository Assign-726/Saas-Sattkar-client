"""
super_admin.middleware
======================
Middleware to enforce Subscription Overdue & Grace Period Lockout on every admin panel.

If a restaurant's subscription grace period has expired, or the account has been
manually suspended by Master Admin, signed-in users are kept out of the admin panels:

- Super Admin (superusers) is redirected to /super/billing/ so the owner can pay.
- Restaurant Admin, Hotel Admin and Django admin (branch managers, staff) get a
  "service paused" page asking them to contact the owner — they cannot pay themselves.

Guest-facing pages and public APIs are not affected.
"""

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from super_admin.models import Restaurant
from super_admin.services import MasterBillingService
from core.request_cache import first_restaurant

logger = logging.getLogger(__name__)

# Admin panels covered by the lock.
LOCKED_PREFIXES = ('/super/', '/dashboard/', '/hotel-admin/', '/dj-admin/')
# Always reachable so the owner can pay and anyone can sign out.
ALLOWED_PREFIXES = ('/super/billing', '/static/', '/media/', '/dj-admin/logout/')

BILLING_URL = '/super/billing/?overdue_lock=1'


_sync_guard = threading.Lock()
_sync_running = False
_sync_last_started = float('-inf')
SYNC_RETRY_SECONDS = 10   # at most one background attempt per process this often (lock check re-syncs after 10 s)


def _sync_with_master(restaurant):
    """
    Refresh the subscription from Master Admin. In the background by default: the page being served is never held
    up by it (an unreachable Master Admin costs seconds per attempt), and the lock check uses the last synced state,
    so a renewal or a lock still shows within ~30 s. One sync at a time per process.
    """
    global _sync_running, _sync_last_started
    if not getattr(settings, 'MASTER_SYNC_IN_BACKGROUND', True):
        MasterBillingService.fetch_subscription_details(restaurant)
        restaurant.refresh_from_db()
        return
    with _sync_guard:
        # A failed sync leaves features_last_synced_at stale, so without this every admin request
        # would start another attempt while Master Admin is down.
        if _sync_running or time.monotonic() - _sync_last_started < SYNC_RETRY_SECONDS:
            return
        _sync_running = True
        _sync_last_started = time.monotonic()

    def run():
        global _sync_running
        try:
            MasterBillingService.fetch_subscription_details(restaurant)
        except Exception as sync_err:
            logger.debug(f"[SubscriptionLockMiddleware] Background sync failed: {sync_err}")
        finally:
            connections.close_all()  # this thread's own DB connection (it ends here)
            with _sync_guard:
                _sync_running = False

    threading.Thread(target=run, name='master-billing-sync', daemon=True).start()


def _offline_lock(restaurant, now) -> bool:
    """
    Master Admin unreachable: fall back to the last synced renewal date so blocking
    the Master Admin URL cannot lift an overdue lock.
    """
    if restaurant.master_sync_status in ('CONNECTED', 'SYNCED'):
        return False
    due = restaurant.subscription_next_billing_date
    if not due or restaurant.subscription_is_trial:
        return False
    grace_days = getattr(settings, 'SUBSCRIPTION_OFFLINE_GRACE_DAYS', 3)
    return now > due + timedelta(days=grace_days)


class SuperAdminSubscriptionLockMiddleware:
    """
    Guards the admin panels against unpaid, overdue subscriptions whose grace
    period has expired.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ''
        user = getattr(request, 'user', None)

        if (
            path.startswith(LOCKED_PREFIXES)
            and not path.startswith(ALLOWED_PREFIXES)
            and user and user.is_authenticated
        ):
            try:
                restaurant = first_restaurant()
            except Exception:
                restaurant = None

            if restaurant:
                now = timezone.now()
                last_synced = restaurant.features_last_synced_at
                # Fast sync from Master Admin if never synced or synced > 30 seconds ago
                # or if the account is currently locked (to detect immediate renewals/activations)
                if not last_synced or (now - last_synced).total_seconds() > 30 or (restaurant.subscription_is_locked and (now - last_synced).total_seconds() > 10):
                    try:
                        _sync_with_master(restaurant)
                    except Exception as sync_err:
                        logger.debug(f"[SubscriptionLockMiddleware] Sync skipped: {sync_err}")

                if restaurant.subscription_is_locked or _offline_lock(restaurant, now):
                    return self._locked_response(request, user, restaurant)

        return self.get_response(request)

    @staticmethod
    def _locked_response(request, user, restaurant):
        is_owner = user.is_superuser
        message = (
            'Your subscription is overdue and grace period has expired. Please renew plan to restore access.'
            if is_owner else
            'Access is paused because the subscription is overdue. Please ask the account owner to renew.'
        )
        is_ajax = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest' or
            'application/json' in request.headers.get('accept', '')
        )
        if is_ajax:
            return JsonResponse({
                'status': 'locked',
                'redirect': BILLING_URL if is_owner else None,
                'message': message,
            }, status=403)

        if is_owner:
            return redirect(BILLING_URL)
        return render(request, 'super_admin/subscription_locked.html', {
            'restaurant': restaurant,
            'message': message,
        }, status=403)
