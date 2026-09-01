from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from super_admin.models import Restaurant, RestaurantUser, UserSecuritySetting
from core.models import EmailOTP
from core.emails import mask_email

class EmailOTPAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(name="Royal Palace", slug="royal-palace")
        self.user = User.objects.create_user(
            username="adminuser",
            email="admin@royalpalace.com",
            password="StrongPassword123!"
        )
        self.user.is_staff = True
        self.user.save()

        self.restaurant_user = RestaurantUser.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            is_primary_admin=True
        )

        # Create superuser for super_admin tests
        self.superuser = User.objects.create_superuser(
            username="superadmin",
            email="super@domain.com",
            password="SuperPassword123!"
        )

    def test_mask_email(self):
        self.assertEqual(mask_email("admin@royalpalace.com"), "a***n@royalpalace.com")
        self.assertEqual(mask_email("ab@royalpalace.com"), "a***@royalpalace.com")
        self.assertEqual(mask_email(""), "")

    def test_auth_backend_with_username_and_email(self):
        # Authenticate with username
        login_with_username = self.client.post(reverse('login'), {
            'username': 'adminuser',
            'password': 'StrongPassword123!'
        })
        self.assertEqual(login_with_username.status_code, 302)
        self.assertEqual(login_with_username.url, reverse('verify_otp'))

        # Check OTP generated
        otp = EmailOTP.objects.filter(user=self.user, purpose=EmailOTP.Purpose.LOGIN, is_used=False).first()
        self.assertIsNotNone(otp)
        self.assertEqual(len(otp.otp_code), 6)

        # Authenticate with email
        login_with_email = self.client.post(reverse('login'), {
            'username': 'admin@royalpalace.com',
            'password': 'StrongPassword123!'
        })
        self.assertEqual(login_with_email.status_code, 302)
        self.assertEqual(login_with_email.url, reverse('verify_otp'))

    def test_otp_verification_success(self):
        # Step 1: Initial Login
        self.client.post(reverse('login'), {
            'username': 'adminuser',
            'password': 'StrongPassword123!'
        })

        otp = EmailOTP.objects.filter(user=self.user, purpose=EmailOTP.Purpose.LOGIN, is_used=False).first()
        self.assertIsNotNone(otp)

        # Step 2: Verify OTP
        response = self.client.post(reverse('verify_otp'), {
            'otp_code': otp.otp_code
        })
        # Should redirect to restaurant dashboard
        expected_url = reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': 'royal-palace'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, expected_url)

        # Verify OTP is marked used
        otp.refresh_from_db()
        self.assertTrue(otp.is_used)

    def test_otp_verification_invalid_code(self):
        # Step 1: Initial Login
        self.client.post(reverse('login'), {
            'username': 'adminuser',
            'password': 'StrongPassword123!'
        })

        # Step 2: Verify with wrong code
        response = self.client.post(reverse('verify_otp'), {
            'otp_code': '000000'
        })
        self.assertEqual(response.status_code, 200) # Re-renders template with error

        otp = EmailOTP.objects.filter(user=self.user, purpose=EmailOTP.Purpose.LOGIN, is_used=False).first()
        self.assertEqual(otp.attempts, 1)
        self.assertFalse(otp.is_used)

    def test_otp_resend_cooldown(self):
        # Step 1: Initial Login
        self.client.post(reverse('login'), {
            'username': 'adminuser',
            'password': 'StrongPassword123!'
        })

        # Immediate resend should trigger cooldown
        resend_resp = self.client.post(reverse('resend_otp'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resend_resp.status_code, 429)

        # Fast forward time to bypass cooldown
        old_otp = EmailOTP.objects.filter(user=self.user).first()
        old_otp.created_at = timezone.now() - timedelta(seconds=70)
        old_otp.save()

        resend_resp_ok = self.client.post(reverse('resend_otp'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resend_resp_ok.status_code, 200)
        data = resend_resp_ok.json()
        self.assertTrue(data['success'])

    def test_super_admin_setup_page_and_2fa_toggle(self):
        self.client.force_login(self.superuser)

        # GET setup page
        resp = self.client.get(reverse('super_admin:setup'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Administrator Setup")
        self.assertContains(resp, "super@domain.com")

        # POST to disable 2FA
        post_data = {
            'first_name': 'Super',
            'last_name': 'User',
            'username': 'superadmin',
            # is_2fa_enabled omitted means unchecked/False
        }
        resp = self.client.post(reverse('super_admin:setup'), post_data)
        self.assertEqual(resp.status_code, 302)

        sec_setting = UserSecuritySetting.objects.get(user=self.superuser)
        self.assertFalse(sec_setting.is_2fa_enabled)

        # Logout and test sign in with 2FA disabled -> directly logs in to super_admin dashboard
        self.client.logout()
        login_resp = self.client.post(reverse('login'), {
            'username': 'superadmin',
            'password': 'SuperPassword123!'
        })
        self.assertEqual(login_resp.status_code, 302)
        self.assertEqual(login_resp.url, reverse('super_admin:dashboard'))

    def test_super_admin_setup_password_change(self):
        self.client.force_login(self.superuser)

        # POST with wrong old password
        bad_pwd_data = {
            'first_name': 'Super',
            'last_name': 'User',
            'username': 'superadmin',
            'is_2fa_enabled': 'on',
            'old_password': 'WrongOldPassword!',
            'new_password': 'BrandNewPassword123!',
            'confirm_password': 'BrandNewPassword123!',
        }
        resp = self.client.post(reverse('super_admin:setup'), bad_pwd_data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Current password is incorrect")

        # POST with correct old password
        good_pwd_data = {
            'first_name': 'Super',
            'last_name': 'User',
            'username': 'superadmin',
            'is_2fa_enabled': 'on',
            'old_password': 'SuperPassword123!',
            'new_password': 'BrandNewPassword123!',
            'confirm_password': 'BrandNewPassword123!',
        }
        resp = self.client.post(reverse('super_admin:setup'), good_pwd_data)
        self.assertEqual(resp.status_code, 302)

        # Verify password changed
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.check_password('BrandNewPassword123!'))

    def test_super_admin_create_restaurant_admin_with_and_without_2fa(self):
        self.client.force_login(self.superuser)

        # 1. Validation test: 2FA checked but email is empty -> should show error
        bad_post = {
            'username': 'branch_no_email',
            'email': '',
            'password': 'BranchPassword123!',
            'restaurant': self.restaurant.pk,
            'is_2fa_enabled': 'on',
        }
        resp = self.client.post(reverse('super_admin:restaurant_admin_create'), bad_post)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "An email address is required when Two-Factor Authentication (OTP) is enabled")

        # 2. Creation test: 2FA unchecked (disabled) -> created successfully
        good_post_no_2fa = {
            'username': 'branch_direct_user',
            'email': 'direct@royalpalace.com',
            'password': 'BranchPassword123!',
            'restaurant': self.restaurant.pk,
            # is_2fa_enabled omitted -> False
        }
        resp2 = self.client.post(reverse('super_admin:restaurant_admin_create'), good_post_no_2fa)
        self.assertEqual(resp2.status_code, 302)

        branch_user = User.objects.get(username='branch_direct_user')
        sec = UserSecuritySetting.objects.get(user=branch_user)
        self.assertFalse(sec.is_2fa_enabled)

        # 3. Test direct login for branch admin without OTP
        self.client.logout()
        login_resp = self.client.post(reverse('login'), {
            'username': 'branch_direct_user',
            'password': 'BranchPassword123!'
        })
        self.assertEqual(login_resp.status_code, 302)
        # Directly goes to restaurant admin dashboard, NOT verify_otp
        expected_url = reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': 'royal-palace'})
        self.assertEqual(login_resp.url, expected_url)

    def test_super_admin_update_restaurant_admin_2fa_toggle(self):
        self.client.force_login(self.superuser)

        # Update existing self.restaurant_user (adminuser) to disable 2FA
        resp = self.client.post(reverse('super_admin:restaurant_admin_edit', kwargs={'pk': self.restaurant_user.pk}), {
            'username': 'adminuser',
            'email': 'admin@royalpalace.com',
            'restaurant': self.restaurant.pk,
            'is_primary_admin': 'on',
            # is_2fa_enabled omitted -> disable 2FA
        })
        self.assertEqual(resp.status_code, 302)

        sec = UserSecuritySetting.objects.get(user=self.user)
        self.assertFalse(sec.is_2fa_enabled)

        # Now test login: should sign in directly without OTP
        self.client.logout()
        login_resp = self.client.post(reverse('login'), {
            'username': 'adminuser',
            'password': 'StrongPassword123!'
        })
        self.assertEqual(login_resp.status_code, 302)
        expected_url = reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': 'royal-palace'})
        self.assertEqual(login_resp.url, expected_url)

