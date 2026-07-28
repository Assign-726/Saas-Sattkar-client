from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch
from rest_framework.exceptions import ValidationError
from super_admin.models import Restaurant
from core.models import RestaurantSettings, Order, Category, SubCategory, MenuItem
from core.serializers import OrderSerializer
from restaurant_admin.forms import RestaurantSettingsForm, verify_razorpay_credentials
from rest_framework.test import APIClient

class RazorpayPaymentTests(TestCase):
    def setUp(self):
        # Create restaurant and items
        self.restaurant = Restaurant.objects.create(name="Tipsy Caterer", slug="tipsy-caterer")
        self.category = Category.objects.create(name="Main Course", restaurant=self.restaurant)
        self.subcategory = SubCategory.objects.create(name="Curry", category=self.category)
        self.menu_item = MenuItem.objects.create(
            name="Paneer Butter Masala",
            price=250.00,
            category=self.category,
            subcategory=self.subcategory,
            restaurant=self.restaurant,
            availability=True
        )

    @patch('urllib.request.urlopen')
    def test_verify_credentials_api_call(self, mock_urlopen):
        # Mock successful API response
        mock_urlopen.return_value.__enter__.return_value.status = 200
        result = verify_razorpay_credentials("valid_id", "valid_secret")
        self.assertTrue(result)

        # Mock unauthorized response (401)
        from urllib.error import HTTPError
        import io
        mock_urlopen.side_effect = HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b""))
        result = verify_razorpay_credentials("invalid_id", "invalid_secret")
        self.assertFalse(result)

    def test_settings_form_validation(self):
        # Test validation fails when online is checked but credentials are blank
        form_data = {
            'cgst_percentage': 2.5,
            'sgst_percentage': 2.5,
            'service_charge_percentage': 0,
            'enable_online_payment': True,
            'enable_pay_at_counter': False,
            'razorpay_key_id': '',
            'razorpay_key_secret': ''
        }
        form = RestaurantSettingsForm(data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn('razorpay_key_id', form.errors)
        self.assertIn('razorpay_key_secret', form.errors)

        # Test validation fails if both payment methods are unchecked
        form_data = {
            'cgst_percentage': 2.5,
            'sgst_percentage': 2.5,
            'service_charge_percentage': 0,
            'enable_online_payment': False,
            'enable_pay_at_counter': False,
            'razorpay_key_id': '',
            'razorpay_key_secret': ''
        }
        form = RestaurantSettingsForm(data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn('__all__', form.errors)

    @patch('restaurant_admin.forms.verify_razorpay_credentials')
    def test_settings_form_saves_on_success(self, mock_verify):
        mock_verify.return_value = True
        
        form_data = {
            'cgst_percentage': 2.5,
            'sgst_percentage': 2.5,
            'service_charge_percentage': 0.0,
            'enable_online_payment': True,
            'enable_pay_at_counter': True,
            'razorpay_key_id': 'rzp_test_abc123',
            'razorpay_key_secret': 'some_secret'
        }
        
        settings_obj = RestaurantSettings.get_settings(self.restaurant)
        form = RestaurantSettingsForm(data=form_data, instance=settings_obj)
        self.assertTrue(form.is_valid())
        
        saved_obj = form.save(commit=False)
        saved_obj.restaurant = self.restaurant
        saved_obj.save()
        
        updated_settings = RestaurantSettings.objects.get(restaurant=self.restaurant)
        self.assertEqual(updated_settings.razorpay_key_id, 'rzp_test_abc123')
        self.assertTrue(updated_settings.enable_online_payment)

    @patch('urllib.request.urlopen')
    def test_order_serializer_online_payment(self, mock_urlopen):
        # Configure restaurant settings to enable online payment
        settings_obj = RestaurantSettings.get_settings(self.restaurant)
        settings_obj.enable_online_payment = True
        settings_obj.enable_pay_at_counter = True
        settings_obj.razorpay_key_id = "rzp_test_abc"
        settings_obj.razorpay_key_secret = "secret"
        settings_obj.save()

        # Mock Razorpay order response
        import json
        import io
        mock_response = io.BytesIO(json.dumps({'id': 'order_rzp_mock123'}).encode('utf-8'))
        mock_urlopen.return_value.__enter__.return_value.status = 201
        mock_urlopen.return_value.__enter__.return_value.read.return_value = mock_response.read()

        order_data = {
            'customer_name': 'Ritik Kumar',
            'phone': '9999999999',
            'table_number': 4,
            'payment_method': 'O',
            'items': [
                {'menu_item': self.menu_item.id, 'quantity': 2}
            ]
        }

        serializer = OrderSerializer(data=order_data)
        self.assertTrue(serializer.is_valid())
        order = serializer.save()

        self.assertEqual(order.payment_method, 'O')
        self.assertEqual(order.payment_status, 'U') # Unpaid initially
        self.assertEqual(order.razorpay_order_id, 'order_rzp_mock123')
        self.assertEqual(order.total_amount, 500.00) # 250.00 * 2

    def test_security_token_generation(self):
        # Place order
        order = Order.objects.create(
            customer_name="John Doe",
            phone="1234567890",
            restaurant=self.restaurant,
            total_amount=100.00
        )
        self.assertIsNotNone(order.security_token)
        self.assertTrue(len(order.security_token) > 0)

    def test_order_access_security(self):
        client = APIClient()
        # Create two orders
        order1 = Order.objects.create(
            customer_name="Client A",
            phone="1234567890",
            restaurant=self.restaurant,
            total_amount=100.00
        )
        order2 = Order.objects.create(
            customer_name="Client B",
            phone="0987654321",
            restaurant=self.restaurant,
            total_amount=200.00
        )

        # 1. Non-admin listing without ids should return empty list
        response = client.get(f'/api/orders/?restaurant_slug={self.restaurant.slug}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)

        # 2. Non-admin listing with ids should filter and return only matching orders
        response = client.get(f'/api/orders/?restaurant_slug={self.restaurant.slug}&ids={order1.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], order1.id)

        # 3. Non-admin retrieve without token should return 403 Forbidden
        response = client.get(f'/api/orders/{order1.id}/')
        self.assertEqual(response.status_code, 403)

        # 4. Non-admin retrieve with invalid token should return 403 Forbidden
        response = client.get(f'/api/orders/{order1.id}/?token=invalid_token')
        self.assertEqual(response.status_code, 403)

        # 5. Non-admin retrieve with correct token should return 200 OK
        response = client.get(f'/api/orders/{order1.id}/?token={order1.security_token}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['customer_name'], "Client A")

        # 6. Non-admin receipt action check
        response = client.get(f'/api/orders/{order1.id}/receipt/')
        self.assertEqual(response.status_code, 403)

        response = client.get(f'/api/orders/{order1.id}/receipt/?token={order1.security_token}')
        self.assertEqual(response.status_code, 200)

        # 6.1 Guest retrieve via UUID directly should return 200 OK without token param
        response = client.get(f'/api/orders/{order1.security_token}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['customer_name'], "Client A")

        # 6.2 Guest receipt via UUID directly should return 200 OK without token param
        response = client.get(f'/api/orders/{order1.security_token}/receipt/')
        self.assertEqual(response.status_code, 200)

        # 7. Admin (staff) user check - should bypass token constraints
        staff_user = User.objects.create_superuser('admin', 'admin@test.com', 'adminpass')
        client.force_authenticate(user=staff_user)
        
        # Admin can list all orders
        response = client.get(f'/api/orders/?restaurant_slug={self.restaurant.slug}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

        # Admin can retrieve order without token
        response = client.get(f'/api/orders/{order1.id}/')
        self.assertEqual(response.status_code, 200)
