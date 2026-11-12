import json
import hmac
import hashlib
import threading
from decimal import Decimal
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.db import connection
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from events.models import ClubEvent, TicketType, EventBooking
from events.views import public_checkout_initiate, public_verify_payment


@override_settings(RAZORPAY_KEY_ID='rzp_mock_key', RAZORPAY_KEY_SECRET='rzp_secret_test')
class EventTicketingConcurrencyTests(TransactionTestCase):
    """
    Concurrency test suite ensuring zero-vulnerability handling of race conditions:
    1. Simultaneous checkouts for the last remaining ticket(s).
    2. Late payment on expired checkout holds when sold out (anti-overselling).
    3. Simultaneous gate / bouncer pass scanning (preventing duplicate entry).
    4. Concurrent payment verification idempotency (preventing double increment of quantity_sold).
    """

    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="Apex Nightclub Corp",
            slug="apex-nightclub"
        )
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Apex Club Downtown",
            slug="apex-downtown",
            has_club_module=True,
            address_line_1="500 Party Boulevard",
            city="Mumbai",
            state="Maharashtra"
        )

        # Admin / Bouncer user
        self.bouncer_user = User.objects.create_user(
            username="bouncer1",
            password="password123",
            is_staff=True
        )
        self.bouncer_profile = RestaurantUser.objects.create(
            user=self.bouncer_user,
            restaurant=self.restaurant,
            branch=self.branch,
            is_primary_admin=False
        )

        # Create live club event
        now = timezone.now()
        self.event = ClubEvent.objects.create(
            branch=self.branch,
            title="Bass & Beats Ultra Night",
            description="High capacity EDM concert",
            start_datetime=now + timedelta(days=1),
            end_datetime=now + timedelta(days=1, hours=6),
            is_published=True
        )

        # Ticket tier with strictly 1 available ticket
        self.vip_ticket = TicketType.objects.create(
            event=self.event,
            name="Ultra VIP Pass",
            base_price=Decimal("2500.00"),
            total_allocated=1,
            quantity_sold=0,
            max_per_order=1,
            is_active=True
        )

    def _generate_signature(self, order_id, payment_id, secret='rzp_secret_test'):
        return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

    def test_concurrent_checkout_single_ticket_remaining(self):
        """
        Simulate 6 buyers simultaneously clicking 'Checkout' for the last remaining VIP ticket.
        Pessimistic locking must ensure EXACTLY 1 buyer gets the ticket hold, and 5 receive 400 Bad Request.
        """
        num_buyers = 6
        barrier = threading.Barrier(num_buyers)
        successes = []
        failures = []
        lock = threading.Lock()

        def buyer_checkout_task(buyer_id):
            connection.close()
            try:
                factory = APIRequestFactory()
                payload = {
                    'ticket_type_id': self.vip_ticket.id,
                    'quantity': 1,
                    'guest_name': f"Buyer {buyer_id}",
                    'guest_email': f"buyer{buyer_id}@example.com",
                    'guest_phone': f"998877660{buyer_id}"
                }
                request = factory.post('/api/events/public/checkout/', payload, format='json')

                # Synchronize threads at barrier
                barrier.wait(timeout=10)

                response = public_checkout_initiate(request)
                with lock:
                    if response.status_code == status.HTTP_201_CREATED:
                        successes.append((buyer_id, response.data))
                    else:
                        failures.append((buyer_id, response.status_code, response.data))
            except Exception as e:
                with lock:
                    failures.append((buyer_id, 500, str(e)))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_buyers) as executor:
            futures = [executor.submit(buyer_checkout_task, i) for i in range(num_buyers)]
            for f in futures:
                f.result()

        # Re-fetch ticket from database
        self.vip_ticket.refresh_from_db()

        # Assertions
        self.assertEqual(len(successes), 1, f"Expected exactly 1 successful checkout hold, got {len(successes)}")
        self.assertEqual(len(failures), 5, f"Expected 5 rejected checkout requests, got {len(failures)}")
        self.assertEqual(self.vip_ticket.active_holds_count, 1)
        self.assertEqual(self.vip_ticket.available_quantity, 0)
        self.assertTrue(self.vip_ticket.is_sold_out)

    def test_expired_hold_late_payment_oversell_prevention(self):
        """
        Scenario:
        1. Buyer A creates a 15-minute checkout hold for the last VIP ticket.
        2. Buyer A's hold expires before they complete payment.
        3. Buyer B discovers the released ticket and completes payment successfully (quantity_sold = 1).
        4. Buyer A completes payment on Razorpay 1 minute late and submits verification.
        Verify: System refuses to oversell, marks Buyer A's booking as EXPIRED with an auto-refund note,
                returns HTTP 409 Conflict, and leaves quantity_sold at 1 (never 2).
        """
        factory = APIRequestFactory()
        now = timezone.now()

        # Step 1: Buyer A initiates checkout
        req_a = factory.post('/api/events/public/checkout/', {
            'ticket_type_id': self.vip_ticket.id,
            'quantity': 1,
            'guest_name': "Late Buyer A",
            'guest_email': "buyera@example.com",
            'guest_phone': "9111111111"
        }, format='json')
        resp_a = public_checkout_initiate(req_a)
        self.assertEqual(resp_a.status_code, status.HTTP_201_CREATED)
        booking_ref_a = resp_a.data['booking_reference']
        order_id_a = resp_a.data['razorpay_order_id']
        booking_a = EventBooking.objects.get(booking_reference=booking_ref_a)

        # Step 2: Simulate hold expiration for Buyer A
        booking_a.expires_at = now - timedelta(minutes=5)
        booking_a.payment_status = EventBooking.PaymentStatus.EXPIRED
        booking_a.save(update_fields=['expires_at', 'payment_status'])

        # Step 3: Buyer B buys the released ticket and completes payment
        req_b = factory.post('/api/events/public/checkout/', {
            'ticket_type_id': self.vip_ticket.id,
            'quantity': 1,
            'guest_name': "Fast Buyer B",
            'guest_email': "buyerb@example.com",
            'guest_phone': "9222222222"
        }, format='json')
        resp_b = public_checkout_initiate(req_b)
        self.assertEqual(resp_b.status_code, status.HTTP_201_CREATED)
        booking_ref_b = resp_b.data['booking_reference']
        order_id_b = resp_b.data['razorpay_order_id']

        # Buyer B completes payment
        pay_id_b = "pay_mock_buyer_b_12345"
        sig_b = self._generate_signature(order_id_b, pay_id_b)
        verify_req_b = factory.post('/api/events/public/verify-payment/', {
            'booking_reference': booking_ref_b,
            'razorpay_order_id': order_id_b,
            'razorpay_payment_id': pay_id_b,
            'razorpay_signature': sig_b
        }, format='json')
        verify_resp_b = public_verify_payment(verify_req_b)
        self.assertEqual(verify_resp_b.status_code, status.HTTP_200_OK)

        self.vip_ticket.refresh_from_db()
        self.assertEqual(self.vip_ticket.quantity_sold, 1)

        # Step 4: Buyer A submits late payment verification
        pay_id_a = "pay_mock_buyer_a_99999"
        sig_a = self._generate_signature(order_id_a, pay_id_a)
        verify_req_a = factory.post('/api/events/public/verify-payment/', {
            'booking_reference': booking_ref_a,
            'razorpay_order_id': order_id_a,
            'razorpay_payment_id': pay_id_a,
            'razorpay_signature': sig_a
        }, format='json')
        verify_resp_a = public_verify_payment(verify_req_a)

        # Anti-oversell assertion: Must return 409 Conflict with refund indication
        self.assertEqual(verify_resp_a.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(verify_resp_a.data.get('refund_required'))
        self.assertIn('expired', verify_resp_a.data.get('error', '').lower())

        # Database safety check: Ticket quantity_sold MUST still be 1 (NOT 2)
        self.vip_ticket.refresh_from_db()
        self.assertEqual(self.vip_ticket.quantity_sold, 1)

        booking_a.refresh_from_db()
        self.assertEqual(booking_a.payment_status, EventBooking.PaymentStatus.EXPIRED)
        self.assertIn('[AUTO-REFUND REQUIRED]', booking_a.notes)

    def test_simultaneous_gate_scan_race_condition(self):
        """
        Scenario:
        A buyer shares a screenshot of their valid QR code pass with a friend.
        Both arrive at two different door bouncers (Door 1 & Door 2) at the exact same millisecond.
        Row-level locking must ensure EXACTLY 1 scan grants access (200 OK), while the other
        receives 400 Bad Request ('ALREADY SCANNED').
        """
        # Create a confirmed, paid booking
        booking = EventBooking.objects.create(
            booking_reference=EventBooking.generate_booking_reference(),
            event=self.event,
            ticket_type=self.vip_ticket,
            guest_name="Party Guest",
            guest_email="guest@example.com",
            guest_phone="9876543210",
            quantity=1,
            unit_price=Decimal("2500.00"),
            total_amount=Decimal("2500.00"),
            payment_status=EventBooking.PaymentStatus.SUCCESS,
            payment_method=EventBooking.PaymentMethod.ONLINE,
            checked_in=False
        )
        booking.security_token = booking.generate_security_token()
        booking.save(update_fields=['security_token'])

        num_scanners = 2
        barrier = threading.Barrier(num_scanners)
        responses = []
        lock = threading.Lock()

        def scan_door_task(door_idx):
            connection.close()
            try:
                client = APIClient()
                client.force_authenticate(user=self.bouncer_user)

                # Barrier synchronization to hit scan simultaneously
                barrier.wait(timeout=10)

                resp = client.post('/api/events/admin/events/scan-pass/', {
                    'qr_payload': json.dumps({
                        'ref': booking.booking_reference,
                        'event_id': self.event.id,
                        'tok': booking.security_token
                    })
                }, format='json')

                with lock:
                    responses.append((door_idx, resp.status_code, resp.data))
            except Exception as e:
                with lock:
                    responses.append((door_idx, 500, str(e)))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_scanners) as executor:
            futures = [executor.submit(scan_door_task, i) for i in range(num_scanners)]
            for f in futures:
                f.result()

        status_codes = [r[1] for r in responses]
        self.assertIn(status.HTTP_200_OK, status_codes)
        self.assertIn(status.HTTP_400_BAD_REQUEST, status_codes)

        # Only one should have succeeded
        success_count = sum(1 for s in status_codes if s == status.HTTP_200_OK)
        rejected_count = sum(1 for s in status_codes if s == status.HTTP_400_BAD_REQUEST)
        self.assertEqual(success_count, 1)
        self.assertEqual(rejected_count, 1)

        # Database state: checked_in is True
        booking.refresh_from_db()
        self.assertTrue(booking.checked_in)

    def test_concurrent_payment_verification_idempotency(self):
        """
        Scenario:
        Customer browser redirect and gateway webhook call public_verify_payment concurrently.
        Row-level locking and idempotency guard ensure quantity_sold is incremented only once.
        """
        # Create ticket tier with capacity 5
        general_tier = TicketType.objects.create(
            event=self.event,
            name="General Admission",
            base_price=Decimal("1000.00"),
            total_allocated=5,
            quantity_sold=0,
            max_per_order=2,
            is_active=True
        )

        order_id = "order_evt_test_concurrent_idem"
        booking = EventBooking.objects.create(
            booking_reference=EventBooking.generate_booking_reference(),
            event=self.event,
            ticket_type=general_tier,
            guest_name="Concurrent Payer",
            guest_email="payer@example.com",
            guest_phone="9898989898",
            quantity=2,
            unit_price=Decimal("1000.00"),
            total_amount=Decimal("2000.00"),
            payment_status=EventBooking.PaymentStatus.PENDING,
            payment_method=EventBooking.PaymentMethod.ONLINE,
            razorpay_order_id=order_id,
            expires_at=timezone.now() + timedelta(minutes=15)
        )

        num_threads = 2
        barrier = threading.Barrier(num_threads)
        responses = []
        lock = threading.Lock()

        def verify_task(idx):
            connection.close()
            try:
                factory = APIRequestFactory()
                pay_id = f"pay_mock_idem_{idx}"
                sig = self._generate_signature(order_id, pay_id)
                req = factory.post('/api/events/public/verify-payment/', {
                    'booking_reference': booking.booking_reference,
                    'razorpay_order_id': order_id,
                    'razorpay_payment_id': pay_id,
                    'razorpay_signature': sig
                }, format='json')

                barrier.wait(timeout=10)
                resp = public_verify_payment(req)
                with lock:
                    responses.append(resp.status_code)
            except Exception as e:
                with lock:
                    responses.append(str(e))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(verify_task, i) for i in range(num_threads)]
            for f in futures:
                f.result()

        # Both should report OK (one completed verification, one returned idempotent success)
        for code in responses:
            self.assertEqual(code, status.HTTP_200_OK)

        # Quantity sold must be incremented by 2, NOT 4
        general_tier.refresh_from_db()
        self.assertEqual(general_tier.quantity_sold, 2)
