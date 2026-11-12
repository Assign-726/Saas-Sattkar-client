import datetime
from decimal import Decimal
import threading
from concurrent.futures import ThreadPoolExecutor
from django.test import TransactionTestCase
from django.db import connection
from rest_framework import status
from rest_framework.test import APIClient

from super_admin.models import Restaurant, RestaurantBranch
from hotel.models import RoomCategory, Room, RoomBooking, HotelSettings
from hotel.serializers import PublicHotelCheckoutSerializer


class RoomBookingConcurrencyTests(TransactionTestCase):
    """
    Test suite verifying that race conditions are prevented when multiple users
    attempt to book the same single room at the exact same millisecond.
    """

    def setUp(self):
        # Create Restaurant & Branch
        self.restaurant = Restaurant.objects.create(name="Concurrency Hotel Resort", slug="concurrency-resort")
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="North Wing",
            slug="north-wing",
            has_hotel_module=True,
        )
        self.settings = HotelSettings.objects.create(
            branch=self.branch,
            hotel_name="Concurrency Hotel",
            enable_online_payment=True,
            enable_pay_at_desk=True,
        )

        # Create a single room category
        self.category = RoomCategory.objects.create(
            branch=self.branch,
            name="Exclusive Penthouse Suite",
            base_price_per_night=Decimal('6000.00'),
            max_occupancy=2,
            is_active=True,
        )

        # CRITICAL FOR TEST: Exactly ONE room exists in this category
        self.only_room = Room.objects.create(
            branch=self.branch,
            category=self.category,
            room_number="999",
            status=Room.RoomStatus.AVAILABLE,
            is_active=True,
        )

    def test_multiple_concurrent_users_single_room_serializer(self):
        """
        Simulate 5 users submitting checkout simultaneously for the same single room
        using PublicHotelCheckoutSerializer with thread synchronization barrier.
        """
        check_in = datetime.date.today() + datetime.timedelta(days=15)
        check_out = datetime.date.today() + datetime.timedelta(days=18)
        num_users = 5

        # Barrier to release all threads at the EXACT same millisecond
        barrier = threading.Barrier(num_users)
        results = []
        errors = []
        lock = threading.Lock()

        def user_booking_task(user_idx):
            # Close connection to ensure clean thread-local DB connection
            connection.close()
            try:
                # Synchronize all threads so they start checkout simultaneously
                barrier.wait(timeout=10)
                
                payload = {
                    "branch_id": self.branch.id,
                    "category_id": self.category.id,
                    "check_in_date": check_in.isoformat(),
                    "check_out_date": check_out.isoformat(),
                    "guest_name": f"Concurrent User {user_idx}",
                    "guest_email": f"user{user_idx}@test.com",
                    "guest_phone": f"980000000{user_idx}",
                    "payment_method": "PAY_AT_DESK",
                }
                
                serializer = PublicHotelCheckoutSerializer(data=payload)
                if serializer.is_valid():
                    res = serializer.save()
                    with lock:
                        results.append((user_idx, res['room_booking'].booking_id))
                else:
                    with lock:
                        errors.append((user_idx, serializer.errors))
            except Exception as e:
                with lock:
                    errors.append((user_idx, str(e)))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_users) as executor:
            futures = [executor.submit(user_booking_task, i) for i in range(1, num_users + 1)]
            for f in futures:
                f.result()

        print(f"\n[Test Result] Serializer Concurrency: {len(results)} success, {len(errors)} rejected.")
        for uid, bid in results:
            print(f"  -> SUCCESS: User {uid} secured booking {bid}")
        for uid, err in errors:
            print(f"  -> REJECTED: User {uid} received expected rejection: {err}")

        # 1. Exactly ONE user must succeed
        self.assertEqual(len(results), 1, f"Expected exactly 1 successful booking, but got {len(results)}: {results}")

        # 2. All remaining users (4 users) must fail
        self.assertEqual(len(errors), num_users - 1, f"Expected {num_users - 1} rejections, got: {errors}")

        # 3. Check database integrity: Exactly ONE booking in the database for Room 999
        bookings_in_db = RoomBooking.objects.filter(
            room=self.only_room,
            check_in_date=check_in,
            check_out_date=check_out,
            booking_status=RoomBooking.BookingStatus.RESERVED,
        )
        self.assertEqual(bookings_in_db.count(), 1, "Database contains duplicate bookings for the same room!")

        # 4. Verify the successful booking details
        winner_user_idx, winning_booking_id = results[0]
        winning_booking = bookings_in_db.first()
        self.assertEqual(winning_booking.booking_id, winning_booking_id)
        self.assertEqual(winning_booking.guest_name, f"Concurrent User {winner_user_idx}")
        self.assertEqual(winning_booking.room.room_number, "999")

    def test_multiple_concurrent_users_single_room_api_view(self):
        """
        Simulate concurrent users hitting the PublicHotelCheckoutView API endpoint
        simultaneously for the only available room.
        """
        check_in = datetime.date.today() + datetime.timedelta(days=25)
        check_out = datetime.date.today() + datetime.timedelta(days=28)
        num_users = 4

        barrier = threading.Barrier(num_users)
        responses = []
        lock = threading.Lock()

        def user_api_request(user_idx):
            connection.close()
            try:
                barrier.wait(timeout=10)
                client = APIClient()
                payload = {
                    "branch_id": self.branch.id,
                    "category_id": self.category.id,
                    "check_in_date": check_in.isoformat(),
                    "check_out_date": check_out.isoformat(),
                    "guest_name": f"API Guest {user_idx}",
                    "guest_phone": f"991111110{user_idx}",
                    "guest_email": f"api_guest_{user_idx}@test.com",
                    "payment_method": "PAY_AT_DESK",
                }
                resp = client.post('/api/rooms/checkout/', payload, format='json')
                with lock:
                    responses.append((user_idx, resp.status_code, resp.data))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_users) as executor:
            futures = [executor.submit(user_api_request, i) for i in range(1, num_users + 1)]
            for f in futures:
                f.result()

        successes = [r for r in responses if r[1] == status.HTTP_201_CREATED]
        rejections = [r for r in responses if r[1] == status.HTTP_400_BAD_REQUEST]

        print(f"\n[Test Result] API View Concurrency: {len(successes)} success (201), {len(rejections)} rejected (400).")
        for uid, code, data in successes:
            print(f"  -> SUCCESS (HTTP {code}): Guest {uid} created booking {data.get('booking_id')}")
        for uid, code, data in rejections:
            print(f"  -> REJECTED (HTTP {code}): Guest {uid} error: {data.get('error') or data.get('details')}")

        # Exactly 1 success (201 Created)
        self.assertEqual(len(successes), 1, f"Expected 1 successful 201 response, got {len(successes)}: {successes}")
        # The remaining 3 rejected (400 Bad Request)
        self.assertEqual(len(rejections), num_users - 1, f"Expected {num_users - 1} 400 Bad Request responses, got {len(rejections)}: {rejections}")

        # Verify DB has exactly 1 booking for those dates
        db_count = RoomBooking.objects.filter(
            room=self.only_room,
            check_in_date=check_in,
            check_out_date=check_out,
            booking_status=RoomBooking.BookingStatus.RESERVED,
        ).count()
        self.assertEqual(db_count, 1, "Duplicate bookings found in database via API!")

