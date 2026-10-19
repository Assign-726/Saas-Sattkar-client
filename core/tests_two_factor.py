"""Settings → Two-factor sign-in, in the restaurant and hotel admins."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from core import two_factor
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser, UserSecuritySetting


class TwoFactorSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.r = Restaurant.objects.create(name='Spice', slug='spice')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main', has_hotel_module=True)
        self.owner = self.make('owner', 'RESTAURANT_ADMIN')
        self.hotel = self.make('hoteladmin', 'HOTEL_ADMIN', branch=self.b)

    def make(self, name, role, branch=None, email=True):
        u = User.objects.create_user(name, email=f'{name}@x.com' if email else '', password='pw-123456', is_staff=True)
        RestaurantUser.objects.create(user=u, restaurant=self.r, role=role, branch=branch)
        return u

    def test_defaults_follow_the_role(self):
        self.assertTrue(two_factor.is_enabled(self.owner))
        self.assertFalse(two_factor.is_enabled(self.make('cashier', 'CASHIER')))

    def test_restaurant_settings_turn_off_needs_password_then_on_again(self):
        self.client.force_login(self.owner)
        page = self.client.get('/dashboard/spice/settings/')
        self.assertContains(page, 'Require Email OTP at Sign In')
        url = '/dashboard/spice/settings/two-factor/'
        r = self.client.post(url, {}, follow=True)
        self.assertContains(r, 'Enter your current password')
        self.assertTrue(two_factor.is_enabled(self.owner))
        self.client.post(url, {'current_password': 'pw-123456'})
        self.assertFalse(two_factor.is_enabled(self.owner))
        self.client.post(url, {'is_2fa_enabled': 'on'})
        self.assertTrue(UserSecuritySetting.objects.get(user=self.owner).is_2fa_enabled)

    def test_hotel_settings_toggle(self):
        self.client.force_login(self.hotel)
        self.assertContains(self.client.get('/hotel-admin/spice/settings/'), 'Two-Factor Auth (2FA)')
        r = self.client.post('/hotel-admin/spice/settings/two-factor/', {'current_password': 'pw-123456'})
        self.assertRedirects(r, '/hotel-admin/spice/settings/?tab=security', fetch_redirect_response=False)
        self.assertFalse(two_factor.is_enabled(self.hotel))

    def test_cannot_turn_on_without_email(self):
        u = self.make('noemail', 'RESTAURANT_ADMIN', email=False)
        self.assertIn('Add an email', two_factor.set_enabled(u, True))

    def test_login_respects_the_choice(self):
        two_factor.set_enabled(self.owner, False, 'pw-123456')
        r = self.client.post('/login/', {'username': 'owner', 'password': 'pw-123456'})
        self.assertNotIn('verify-otp', r.get('Location', ''))
        self.client.logout()
        two_factor.set_enabled(self.owner, True)
        r = self.client.post('/login/', {'username': 'owner', 'password': 'pw-123456'})
        self.assertIn('verify-otp', r.get('Location', ''))
