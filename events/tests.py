import json
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from events.models import ClubEvent, TicketType, EventBooking


class ClubEventsTicketingTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create test restaurant & branches
        self.restaurant = Restaurant.objects.create(name="Skyline Lounge Corp", slug="skyline-lounge")
        
        # Branch 1: Has Club Module ENABLED
        self.club_branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Skyline Nightclub & Bar",
            slug="skyline-nightclub",
            has_club_module=True,
            address_line_1="100 Party Ave",
            city="Mumbai",
            state="Maharashtra"
        )

        # Branch 2: Standard restaurant WITHOUT Club Module
        self.standard_branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Skyline Family Diner",
            slug="skyline-diner",
            has_club_module=False,
            address_line_1="200 Quiet Lane",
            city="Mumbai",
            state="Maharashtra"
        )

        # Admin user for Club Branch
        self.club_admin_user = User.objects.create_user(username="clubmgr", password="password123", is_staff=True)
        self.club_profile = RestaurantUser.objects.create(
            user=self.club_admin_user,
            restaurant=self.restaurant,
            branch=self.club_branch,
            is_primary_admin=False
        )

        # Admin user for Standard Branch
        self.standard_admin_user = User.objects.create_user(username="dinermgr", password="password123", is_staff=True)
        self.standard_profile = RestaurantUser.objects.create(
            user=self.standard_admin_user,
            restaurant=self.restaurant,
            branch=self.standard_branch,
            is_primary_admin=False
        )

        # Create a test event on club branch
        self.start_time = timezone.now() + timedelta(days=2)
        self.end_time = timezone.now() + timedelta(days=2, hours=5)

        self.event = ClubEvent.objects.create(
            branch=self.club_branch,
            title="Saturday Neon Techno Night",
            description="Electrifying live DJ set",
            start_datetime=self.start_time,
            end_datetime=self.end_time,
            is_published=True
        )

        # Create 2 ticket tiers
        self.general_tier = TicketType.objects.create(
            event=self.event,
            name="General Admission",
            base_price=Decimal("500.00"),
            total_allocated=50,
            quantity_sold=0,
            max_per_order=5
        )

        self.vip_tier = TicketType.objects.create(
            event=self.event,
            name="VIP Table Pass",
            base_price=Decimal("1500.00"),
            total_allocated=10,
            quantity_sold=0,
            max_per_order=2
        )

    def test_branch_has_club_module_flag(self):
        """Verify the feature-gating flag exists and defaults correctly."""
        self.assertTrue(self.club_branch.has_club_module)
        self.assertFalse(self.standard_branch.has_club_module)

    def test_permission_has_club_module_allows_enabled_branch(self):
        """Branch admin with enabled module can list and create events."""
        self.client.force_authenticate(user=self.club_admin_user)
        response = self.client.get('/api/events/admin/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['title'], "Saturday Neon Techno Night")

    def test_permission_has_club_module_blocks_disabled_branch(self):
        """Branch admin with disabled module gets HTTP 403 Forbidden."""
        self.client.force_authenticate(user=self.standard_admin_user)
        response = self.client.get('/api/events/admin/events/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_zero_tax_flat_pricing_checkout(self):
        """Verify checkout calculates flat base_price * quantity with zero taxes added."""
        checkout_payload = {
            "ticket_type_id": self.general_tier.id,
            "quantity": 3,
            "guest_name": "Rohan Sharma",
            "guest_email": "rohan@example.com",
            "guest_phone": "9876543210"
        }

        response = self.client.post('/api/events/public/checkout/', checkout_payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data

        # 3 tickets @ 500 = 1500.00 exactly (zero tax)
        self.assertEqual(Decimal(data['unit_price']), Decimal('500.00'))
        self.assertEqual(Decimal(data['tax_amount']), Decimal('0.00'))
        self.assertEqual(Decimal(data['total_amount']), Decimal('1500.00'))
        self.assertEqual(data['amount_in_paise'], 150000)

        # Booking created in PENDING status
        booking = EventBooking.objects.get(booking_reference=data['booking_reference'])
        self.assertEqual(booking.payment_status, EventBooking.PaymentStatus.PENDING)
        self.assertEqual(booking.quantity, 3)
        self.assertIsNotNone(booking.expires_at)

    def test_inventory_locking_and_available_quantity(self):
        """Verify 15-minute pending holds decrease available inventory."""
        self.assertEqual(self.vip_tier.available_quantity, 10)

        # Customer books 2 VIP passes
        self.client.post('/api/events/public/checkout/', {
            "ticket_type_id": self.vip_tier.id,
            "quantity": 2,
            "guest_name": "VIP Guest 1",
            "guest_email": "vip1@example.com",
            "guest_phone": "9876543211"
        }, format='json')

        self.vip_tier.refresh_from_db()
        self.assertEqual(self.vip_tier.active_holds_count, 2)
        self.assertEqual(self.vip_tier.available_quantity, 8)

    def test_payment_verification_and_qr_code_generation(self):
        """Verify payment verification marks SUCCESS, increments quantity_sold, and creates QR pass."""
        init_res = self.client.post('/api/events/public/checkout/', {
            "ticket_type_id": self.general_tier.id,
            "quantity": 2,
            "guest_name": "Aarav Patel",
            "guest_email": "aarav@example.com",
            "guest_phone": "9998887776"
        }, format='json')
        ref = init_res.data['booking_reference']
        order_id = init_res.data['razorpay_order_id']

        verify_res = self.client.post('/api/events/public/verify-payment/', {
            "booking_reference": ref,
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_test_9999",
            "razorpay_signature": "sig_test"
        }, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)

        booking = EventBooking.objects.get(booking_reference=ref)
        self.assertEqual(booking.payment_status, EventBooking.PaymentStatus.SUCCESS)
        self.assertIsNotNone(booking.booking_qr_code)
        self.assertTrue(booking.booking_qr_code.name.endswith('.png'))

        # Check that quantity_sold incremented on ticket type
        self.general_tier.refresh_from_db()
        self.assertEqual(self.general_tier.quantity_sold, 2)

    def test_gate_scanner_checkin_flow(self):
        """Verify gate scanner marks checked-in once and blocks duplicate entry."""
        # Create a confirmed booking
        booking = EventBooking.objects.create(
            booking_reference="EVT-BK-TEST01",
            event=self.event,
            ticket_type=self.general_tier,
            guest_name="Karan Malhotra",
            guest_email="karan@example.com",
            guest_phone="9820098200",
            quantity=2,
            unit_price=Decimal('500.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('1000.00'),
            payment_status=EventBooking.PaymentStatus.SUCCESS
        )
        booking.generate_qr_code()
        booking.save()

        self.client.force_authenticate(user=self.club_admin_user)

        # 1st Scan: Should grant access
        scan_1 = self.client.post('/api/events/admin/events/scan-pass/', {
            "qr_payload": "EVT-BK-TEST01"
        }, format='json')
        self.assertEqual(scan_1.status_code, status.HTTP_200_OK)
        self.assertTrue(scan_1.data['valid'])
        self.assertIn("Access Granted", scan_1.data['message'])

        booking.refresh_from_db()
        self.assertTrue(booking.checked_in)

        # 2nd Scan: Must be rejected as ALREADY SCANNED
        scan_2 = self.client.post('/api/events/admin/events/scan-pass/', {
            "qr_payload": "EVT-BK-TEST01"
        }, format='json')
        self.assertEqual(scan_2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(scan_2.data['valid'])
        self.assertTrue(scan_2.data['already_checked_in'])
        self.assertIn("ALREADY SCANNED", scan_2.data['message'])

    def test_branch_admin_manual_door_booking(self):
        """Branch admin can issue cash-at-door passes directly from the admin desk."""
        self.client.force_authenticate(user=self.club_admin_user)

        res = self.client.post(f'/api/events/admin/events/{self.event.id}/manual-booking/', {
            "ticket_type_id": self.vip_tier.id,
            "quantity": 1,
            "guest_name": "Walkin VIP",
            "guest_phone": "9811198111",
            "payment_method": "DOOR_CASH",
            "notes": "Table 5 cash collected"
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['payment_status'], "SUCCESS")
        self.assertEqual(res.data['payment_method'], "DOOR_CASH")
        self.assertEqual(Decimal(res.data['total_amount']), Decimal('1500.00'))

        # Ticket quantity sold incremented
        self.vip_tier.refresh_from_db()
        self.assertEqual(self.vip_tier.quantity_sold, 1)

    def test_super_admin_toggle_club_module_api(self):
        """Super Admin endpoint toggles has_club_module on a branch."""
        superuser = User.objects.create_superuser(username="superadmin", password="password123")
        self.client.force_login(superuser)

        self.assertFalse(self.standard_branch.has_club_module)
        res = self.client.post(f'/super/api/branches/{self.standard_branch.id}/toggle-club-module/', {}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['has_club_module'])

        self.standard_branch.refresh_from_db()
        self.assertTrue(self.standard_branch.has_club_module)
