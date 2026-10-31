from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from super_admin.middleware import SuperAdminSubscriptionLockMiddleware
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


@mock.patch('super_admin.middleware.MasterBillingService.fetch_subscription_details')
class SubscriptionLockTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace', master_sync_status='CONNECTED')
        RestaurantBranch.objects.create(restaurant=self.r, name='Main')
        self.staff = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.staff, restaurant=self.r, role='GENERAL_MANAGER')
        self.owner = User.objects.create_superuser('owner', 'owner@example.com', 'x')
        self.dashboard = reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': self.r.slug})

    def lock(self, **fields):
        Restaurant.objects.filter(pk=self.r.pk).update(**fields)

    def test_staff_blocked_from_restaurant_admin_when_locked(self, _sync):
        self.lock(subscription_is_locked=True)
        self.client.force_login(self.staff)
        resp = self.client.get(self.dashboard)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, 'account owner', status_code=403)

    def test_staff_ajax_gets_json_lock(self, _sync):
        self.lock(subscription_is_locked=True)
        self.client.force_login(self.staff)
        resp = self.client.get(self.dashboard, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['status'], 'locked')

    def test_owner_redirected_to_billing_from_any_panel(self, _sync):
        self.lock(subscription_is_locked=True)
        self.client.force_login(self.owner)
        resp = self.client.get(self.dashboard)
        self.assertRedirects(resp, '/super/billing/?overdue_lock=1', fetch_redirect_response=False)

    def test_billing_page_stays_reachable(self, _sync):
        self.lock(subscription_is_locked=True)
        request = RequestFactory().get('/super/billing/')
        request.user = self.owner
        passed = mock.Mock(return_value='view')
        self.assertEqual(SuperAdminSubscriptionLockMiddleware(passed)(request), 'view')

    def test_unlocked_account_passes(self, _sync):
        self.client.force_login(self.staff)
        self.assertNotEqual(self.client.get(self.dashboard).status_code, 403)

    def test_offline_master_keeps_overdue_lock(self, _sync):
        self.lock(master_sync_status='OFFLINE', subscription_is_locked=False,
                  subscription_next_billing_date=timezone.now() - timedelta(days=10))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.dashboard).status_code, 403)

    def test_offline_master_within_grace_passes(self, _sync):
        self.lock(master_sync_status='OFFLINE', subscription_is_locked=False,
                  subscription_next_billing_date=timezone.now() - timedelta(days=1))
        self.client.force_login(self.staff)
        self.assertNotEqual(self.client.get(self.dashboard).status_code, 403)
