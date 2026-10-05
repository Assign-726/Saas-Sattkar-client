"""
super_admin.middleware
======================
Middleware to enforce Subscription Overdue & Grace Period Lockout on Super Admin.

If a restaurant's subscription grace period has expired, or the account has been
manually suspended by Master Admin, this middleware redirects all Super Admin traffic
to the Billing & Subscription page (/super/billing/) so the administrator can settle
the renewal or request plan changes.
"""

import logging
from django.shortcuts import redirect
from django.http import JsonResponse
from django.utils import timezone
from super_admin.models import Restaurant
from super_admin.services import MasterBillingService

logger = logging.getLogger(__name__)


class SuperAdminSubscriptionLockMiddleware:
    """
    Guards the Super Admin portal against unpaid, overdue subscriptions whose
    grace period has expired.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ''

        # Only inspect requests targeting the Super Admin panel
        if path.startswith('/super/'):
            # Always permit the Billing portal, receipts, and synchronization endpoints
            is_billing_allowed = (
                path.startswith('/super/billing/') or
                path == '/super/billing/' or
                path.startswith('/super/billing') or
                path.startswith('/static/') or
                path.startswith('/media/')
            )

            user = getattr(request, 'user', None)
            if user and user.is_authenticated and user.is_superuser:
                try:
                    restaurant = Restaurant.objects.first()
                except Exception:
                    restaurant = None

                if restaurant:
                    now = timezone.now()
                    last_synced = restaurant.features_last_synced_at
                    # Fast sync from Master Admin if never synced or synced > 30 seconds ago
                    # or if the account is currently locked (to detect immediate renewals/activations)
                    if not last_synced or (now - last_synced).total_seconds() > 30 or (restaurant.subscription_is_locked and (now - last_synced).total_seconds() > 10):
                        try:
                            MasterBillingService.fetch_subscription_details(restaurant)
                            restaurant.refresh_from_db()
                        except Exception as sync_err:
                            logger.debug(f"[SubscriptionLockMiddleware] Sync skipped: {sync_err}")

                    # If subscription is locked (grace period expired OR manually suspended)
                    if restaurant.subscription_is_locked:
                        if not is_billing_allowed:
                            # If AJAX/JSON, return structured redirect response
                            is_ajax = (
                                request.headers.get('x-requested-with') == 'XMLHttpRequest' or
                                'application/json' in request.headers.get('accept', '')
                            )
                            if is_ajax:
                                return JsonResponse({
                                    'status': 'locked',
                                    'redirect': '/super/billing/?overdue_lock=1',
                                    'message': 'Your subscription is overdue and grace period has expired. Please renew plan to restore access.'
                                }, status=403)

                            return redirect('/super/billing/?overdue_lock=1')

        response = self.get_response(request)
        return response
