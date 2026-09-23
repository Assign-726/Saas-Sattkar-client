import datetime
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from hotel.models import RoomCategory, Room, RoomBooking


class HotelModuleTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create Restaurant & Branch
        self.restaurant = Restaurant.objects.create(name="Grand Palace Resort", slug="grand-palace")
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Main Wing",
            slug="main-wing",
            has_hotel_module=True,
            has_club_module=False,
        )

        # Create Category
        self.category = RoomCategory.objects.create(
            branch=self.branch,
            name="Deluxe Suite",
            base_price_per_night=Decimal('5000.00'),
            max_occupancy=3,
            amenities=["Wi-Fi", "AC", "Mini Bar"]
        )

        # Create Rooms
        self.room101 = Room.objects.create(
            branch=self.branch,
            category=self.category,
            room_number="101",
            floor="1st Floor",
            status=Room.RoomStatus.AVAILABLE
        )
        self.room102 = Room.objects.create(
            branch=self.branch,
            category=self.category,
            room_number="102",
            floor="1st Floor",
            status=Room.RoomStatus.AVAILABLE
        )

        # Create Hotel Admin User
        self.hotel_admin_user = User.objects.create_user(username="hotel_admin", password="password123", email="hotel@test.com")
        self.hotel_profile = RestaurantUser.objects.create(
            user=self.hotel_admin_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.HOTEL_ADMIN
        )

        # Create Restaurant Admin User
        self.rest_admin_user = User.objects.create_user(username="rest_admin", password="password123", email="rest@test.com")
        self.rest_profile = RestaurantUser.objects.create(
            user=self.rest_admin_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.RESTAURANT_ADMIN
        )

        # Create General Manager User
        self.gm_user = User.objects.create_user(username="general_manager", password="password123", email="gm@test.com")
        self.gm_profile = RestaurantUser.objects.create(
            user=self.gm_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.GENERAL_MANAGER
        )

    def test_room_booking_overlap_validation(self):
        """
        Verify that overlapping bookings for the same room are blocked.
        """
        self.client.force_authenticate(user=self.hotel_admin_user)

        today = datetime.date.today()
        d_in = today + datetime.timedelta(days=2)
        d_out = today + datetime.timedelta(days=5)

        # 1. Create first valid booking
        b1_data = {
            "room": self.room101.id,
            "guest_name": "Alice Green",
            "guest_email": "alice@test.com",
            "guest_phone": "9876543210",
            "check_in_date": d_in.isoformat(),
            "check_out_date": d_out.isoformat(),
        }
        resp1 = self.client.post('/api/hotel/bookings/', b1_data)
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        # 2. Attempt overlapping booking for room 101 (days 3 to 6)
        overlap_data = {
            "room": self.room101.id,
            "guest_name": "Bob Blue",
            "guest_email": "bob@test.com",
            "guest_phone": "9876543211",
            "check_in_date": (today + datetime.timedelta(days=3)).isoformat(),
            "check_out_date": (today + datetime.timedelta(days=6)).isoformat(),
        }
        resp2 = self.client.post('/api/hotel/bookings/', overlap_data)
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already booked", str(resp2.data))

        # 3. Non-overlapping booking for room 101 (days 5 to 7) is allowed
        valid_next = {
            "room": self.room101.id,
            "guest_name": "Charlie Brown",
            "guest_email": "charlie@test.com",
            "guest_phone": "9876543212",
            "check_in_date": d_out.isoformat(),
            "check_out_date": (today + datetime.timedelta(days=7)).isoformat(),
        }
        resp3 = self.client.post('/api/hotel/bookings/', valid_next)
        self.assertEqual(resp3.status_code, status.HTTP_201_CREATED)

    def test_hotel_access_permissions_rbac(self):
        """
        Verify IsHotelAdminOrManager permission class:
        - Hotel Admin and General Manager allowed
        - Restaurant Admin denied
        - Denied if branch.has_hotel_module == False
        """
        # 1. Hotel Admin -> Allowed
        self.client.force_authenticate(user=self.hotel_admin_user)
        resp = self.client.get('/api/hotel/stats/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 2. General Manager -> Allowed
        self.client.force_authenticate(user=self.gm_user)
        resp = self.client.get('/api/hotel/stats/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 3. Restaurant Admin -> Denied
        self.client.force_authenticate(user=self.rest_admin_user)
        resp = self.client.get('/api/hotel/stats/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Turn off hotel module on branch -> Hotel admin now denied
        self.branch.has_hotel_module = False
        self.branch.save()
        self.client.force_authenticate(user=self.hotel_admin_user)
        resp = self.client.get('/api/hotel/stats/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_front_desk_check_in_and_check_out_lifecycle(self):
        """
        Verify Check-In alters Room status to OCCUPIED and Check-Out alters Room status to CLEANING.
        """
        self.client.force_authenticate(user=self.hotel_admin_user)
        today = datetime.date.today()

        booking = RoomBooking.objects.create(
            room=self.room101,
            branch=self.branch,
            guest_name="David Warner",
            guest_email="david@test.com",
            guest_phone="9988776655",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=2),
            booking_status=RoomBooking.BookingStatus.RESERVED
        )
        self.assertEqual(self.room101.status, Room.RoomStatus.AVAILABLE)

        # 1. Trigger Check-In
        resp_ci = self.client.patch(f'/api/hotel/bookings/{booking.id}/check-in/')
        self.assertEqual(resp_ci.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.room101.refresh_from_db()
        self.assertEqual(booking.booking_status, RoomBooking.BookingStatus.CHECKED_IN)
        self.assertEqual(self.room101.status, Room.RoomStatus.OCCUPIED)

        # 2. Trigger Check-Out
        resp_co = self.client.patch(f'/api/hotel/bookings/{booking.id}/check-out/')
        self.assertEqual(resp_co.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.room101.refresh_from_db()
        self.assertEqual(booking.booking_status, RoomBooking.BookingStatus.CHECKED_OUT)
        self.assertEqual(self.room101.status, Room.RoomStatus.CLEANING)

        # 3. Mark Clean
        resp_clean = self.client.patch(f'/api/hotel/rooms/{self.room101.id}/status/', {"status": "AVAILABLE"})
        self.assertEqual(resp_clean.status_code, status.HTTP_200_OK)
        self.room101.refresh_from_db()
        self.assertEqual(self.room101.status, Room.RoomStatus.AVAILABLE)

    def test_public_availability_api(self):
        """
        Verify GET /api/public/branches/{id}/availability/?start=...&end=...
        filters out categories where all associated rooms are booked.
        """
        today = datetime.date.today()
        d_in = today + datetime.timedelta(days=10)
        d_out = today + datetime.timedelta(days=12)

        # Both room 101 and 102 are available -> 2 available rooms
        url = f'/api/public/branches/{self.branch.id}/availability/?start={d_in.isoformat()}&end={d_out.isoformat()}'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['categories']), 1)
        self.assertEqual(resp.data['categories'][0]['available_rooms_count'], 2)

        # Book room 101 for those dates
        RoomBooking.objects.create(
            room=self.room101,
            branch=self.branch,
            guest_name="Guest 1",
            guest_email="g1@test.com",
            guest_phone="1234567890",
            check_in_date=d_in,
            check_out_date=d_out,
            booking_status=RoomBooking.BookingStatus.RESERVED
        )

        resp2 = self.client.get(url)
        self.assertEqual(resp2.data['categories'][0]['available_rooms_count'], 1)

        # Book room 102 for those dates as well
        RoomBooking.objects.create(
            room=self.room102,
            branch=self.branch,
            guest_name="Guest 2",
            guest_email="g2@test.com",
            guest_phone="1234567891",
            check_in_date=d_in,
            check_out_date=d_out,
            booking_status=RoomBooking.BookingStatus.RESERVED
        )

        # Now all rooms in category are locked -> category is filtered out!
        resp3 = self.client.get(url)
        self.assertEqual(len(resp3.data['categories']), 0)

    def test_tape_chart_api(self):
        """
        Verify GET /api/hotel/tape-chart/ returns rooms and overlapping bookings for timeline.
        """
        self.client.force_authenticate(user=self.hotel_admin_user)
        today = datetime.date.today()

        RoomBooking.objects.create(
            room=self.room101,
            branch=self.branch,
            guest_name="Elena Rostova",
            guest_email="elena@test.com",
            guest_phone="5544332211",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=3),
            booking_status=RoomBooking.BookingStatus.RESERVED
        )

        resp = self.client.get(f'/api/hotel/tape-chart/?start_date={today.isoformat()}&days=7')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['days_count'], 7)
        self.assertEqual(len(resp.data['rooms']), 2)
        # Verify room 101 has the booking
        r101_data = next(r for r in resp.data['rooms'] if r['room_number'] == "101")
        self.assertEqual(len(r101_data['bookings']), 1)
        self.assertEqual(r101_data['bookings'][0]['guest_name'], "Elena Rostova")

    def test_staff_auth_login(self):
        """
        Verify POST /api/hotel/auth/login/ returns JWT tokens, role, and branch capabilities.
        """
        resp = self.client.post('/api/hotel/auth/login/', {
            "username": "hotel_admin",
            "password": "password123",
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)
        self.assertEqual(resp.data['user']['role'], "HOTEL_ADMIN")
        self.assertTrue(resp.data['branch']['has_hotel_module'])

    def test_public_booking_flow(self):
        """
        Verify POST /api/hotel/public/bookings/ auto-allocates an available room in the category.
        """
        today = datetime.date.today()
        d_in = today + datetime.timedelta(days=20)
        d_out = today + datetime.timedelta(days=22)

        booking_payload = {
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "guest_name": "Sophia Loren",
            "guest_email": "sophia@test.com",
            "guest_phone": "9911223344",
            "check_in_date": d_in.isoformat(),
            "check_out_date": d_out.isoformat(),
            "special_requests": "High floor room please",
        }

        resp = self.client.post('/api/hotel/public/bookings/', booking_payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data['success'])
        self.assertTrue(resp.data['booking_id'].startswith("BK-HTL-"))
        self.assertEqual(resp.data['nights'], 2)
        # Allocated one of our rooms
        self.assertIn(resp.data['room_number'], ["101", "102"])

    def test_hotel_admin_template_views(self):
        """
        Verify Django template views for Hotel Admin:
        - dashboard, tape_chart, rooms, categories, bookings
        """
        self.client.force_login(self.hotel_admin_user)

        # 1. Dashboard
        resp_dash = self.client.get(f'/hotel-admin/{self.restaurant.slug}/')
        self.assertEqual(resp_dash.status_code, 200)
        self.assertContains(resp_dash, "Front Desk &amp; Hotel Operations")
        self.assertContains(resp_dash, "Deluxe Suite")

        # 2. Tape Chart
        resp_tape = self.client.get(f'/hotel-admin/{self.restaurant.slug}/tape-chart/')
        self.assertEqual(resp_tape.status_code, 200)
        self.assertContains(resp_tape, "Tape Chart")

        # 3. Rooms & Housekeeping
        resp_rooms = self.client.get(f'/hotel-admin/{self.restaurant.slug}/rooms/')
        self.assertEqual(resp_rooms.status_code, 200)
        self.assertContains(resp_rooms, "Physical Rooms &amp; Housekeeping")
        self.assertContains(resp_rooms, "101")

        # 4. Categories & Tariffs
        resp_cats = self.client.get(f'/hotel-admin/{self.restaurant.slug}/categories/')
        self.assertEqual(resp_cats.status_code, 200)
        self.assertContains(resp_cats, "Room Categories")

        # 5. Bookings
        resp_bk = self.client.get(f'/hotel-admin/{self.restaurant.slug}/bookings/')
        self.assertEqual(resp_bk.status_code, 200)

        # 6. Hotel Settings
        resp_settings = self.client.get(f'/hotel-admin/{self.restaurant.slug}/settings/')
        self.assertEqual(resp_settings.status_code, 200)
        self.assertContains(resp_settings, "Hotel PMS Configuration")
        self.assertContains(resp_settings, "Online Payment Gateway")
        self.assertContains(resp_bk, "Guest Bookings")

    def test_restaurant_admin_blocked_from_hotel_admin(self):
        """
        Verify that a RESTAURANT_ADMIN user cannot access Hotel Admin and is redirected.
        """
        self.client.force_login(self.rest_admin_user)
        resp = self.client.get(f'/hotel-admin/{self.restaurant.slug}/', follow=False)
        # Redirected to restaurant_admin:dashboard
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/dashboard/{self.restaurant.slug}/', resp.url)
