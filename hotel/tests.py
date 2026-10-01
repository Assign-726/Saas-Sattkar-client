import datetime
from decimal import Decimal
from django.utils import timezone
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from hotel.models import (
    RoomCategory,
    Room,
    RoomBooking,
    CabServicePackage,
    FleetVehicle,
    CabBooking,
)



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

    def test_hotel_admin_staff_management(self):
        """
        Verify Hotel PMS-native staff management:
        - View staff directory inside hotel-admin
        - Create Front Desk & Housekeeping staff
        - Toggle active status
        """
        self.client.force_login(self.hotel_admin_user)

        # 1. Staff directory
        resp_list = self.client.get(f'/hotel-admin/{self.restaurant.slug}/staff/')
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "Hotel Staff & Operations Team")
        self.assertEqual(resp_list.context['active_menu'], 'staff')

        # 2. Staff create GET
        resp_create = self.client.get(f'/hotel-admin/{self.restaurant.slug}/staff/create/')
        self.assertEqual(resp_create.status_code, 200)
        allowed_roles = [r[0] for r in resp_create.context['allowed_roles']]
        self.assertIn(RestaurantUser.Role.FRONT_DESK, allowed_roles)
        self.assertIn(RestaurantUser.Role.HOUSEKEEPING, allowed_roles)
        self.assertNotIn(RestaurantUser.Role.CASHIER, allowed_roles)

        # 3. Staff create POST
        post_data = {
            'username': 'hotel_fd_priya',
            'first_name': 'Priya',
            'last_name': 'Sharma',
            'email': 'priya@hotel.com',
            'password': 'PriyaPassword123!',
            'pos_pin': '1234',
            'role': RestaurantUser.Role.FRONT_DESK,
            'is_active': 'on',
        }
        resp_post = self.client.post(f'/hotel-admin/{self.restaurant.slug}/staff/create/', post_data)
        self.assertEqual(resp_post.status_code, 302)
        self.assertIn(f'/hotel-admin/{self.restaurant.slug}/staff/', resp_post.url)

        # Verify created staff in DB
        created_ru = RestaurantUser.objects.get(user__username='hotel_fd_priya')
        self.assertEqual(created_ru.role, RestaurantUser.Role.FRONT_DESK)
        self.assertEqual(created_ru.branch, self.branch)
        self.assertEqual(created_ru.pos_pin, '1234')
        self.assertTrue(created_ru.user.is_active)

        # 4. Toggle active status POST
        toggle_url = f'/hotel-admin/{self.restaurant.slug}/staff/{created_ru.user.id}/toggle-active/'
        resp_toggle = self.client.post(toggle_url)
        self.assertEqual(resp_toggle.status_code, 302)
        created_ru.user.refresh_from_db()
        self.assertFalse(created_ru.user.is_active)

    def test_hotel_admin_visiting_restaurant_staff_redirects_to_hotel_staff(self):
        """
        Verify that if a HOTEL_ADMIN attempts to access the restaurant admin staff URL,
        they are seamlessly redirected to their native Hotel PMS staff management.
        """
        self.client.force_login(self.hotel_admin_user)
        resp = self.client.get(f'/dashboard/{self.restaurant.slug}/staff/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/hotel-admin/{self.restaurant.slug}/staff/', resp.url)

    def test_housekeeping_sidebar_and_feature_isolation(self):
        """
        Verify that a HOUSEKEEPING user:
        1. Only sees 'Housekeeping Operations' -> 'Rooms & Housekeeping' in the sidebar.
        2. All other groups (Front Desk Operations, Room Categories, Hotel Configuration) are hidden.
        3. '+ New Reservation' and '+ Add Physical Room' are completely hidden.
        4. Direct URL access to dashboard, categories, settings is blocked/redirected.
        5. Updating room status is allowed.
        """
        hk_user = User.objects.create_user(username="hk_ramesh", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=hk_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.HOUSEKEEPING,
        )

        self.client.force_login(hk_user)

        # 1. Rooms page access
        resp_rooms = self.client.get(f'/hotel-admin/{self.restaurant.slug}/rooms/')
        self.assertEqual(resp_rooms.status_code, 200)
        self.assertTrue(resp_rooms.context['is_housekeeping'])
        self.assertFalse(resp_rooms.context['show_front_desk_ops'])
        self.assertFalse(resp_rooms.context['show_room_categories'])
        self.assertFalse(resp_rooms.context['show_hotel_config'])
        self.assertFalse(resp_rooms.context['can_create_reservation'])
        self.assertFalse(resp_rooms.context['can_manage_physical_rooms'])

        # Sidebar content checks
        self.assertContains(resp_rooms, "Housekeeping Operations")
        self.assertContains(resp_rooms, "Rooms &amp; Housekeeping")
        self.assertContains(resp_rooms, "Housekeeping Desk")

        # Hidden elements checks
        self.assertNotContains(resp_rooms, "Front Desk Operations")
        self.assertNotContains(resp_rooms, "Front Desk Overview")
        self.assertNotContains(resp_rooms, "Tape Chart Timeline")
        self.assertNotContains(resp_rooms, "Guest Folios &amp; Bookings")
        self.assertNotContains(resp_rooms, "Room Categories &amp; Tariffs")
        self.assertNotContains(resp_rooms, "Hotel Settings")
        self.assertNotContains(resp_rooms, "Staff &amp; Roles")
        self.assertNotContains(resp_rooms, "Add Physical Room")
        self.assertNotContains(resp_rooms, "New Reservation")

        # 2. Blocked from dashboard
        resp_dash = self.client.get(f'/hotel-admin/{self.restaurant.slug}/', follow=False)
        self.assertEqual(resp_dash.status_code, 302)
        self.assertIn('/rooms/', resp_dash.url)

        # 3. Blocked from categories
        resp_cats = self.client.get(f'/hotel-admin/{self.restaurant.slug}/categories/', follow=False)
        self.assertEqual(resp_cats.status_code, 302)
        self.assertIn('/rooms/', resp_cats.url)

        # 4. Blocked from settings
        resp_set = self.client.get(f'/hotel-admin/{self.restaurant.slug}/settings/', follow=False)
        self.assertEqual(resp_set.status_code, 302)
        self.assertIn('/rooms/', resp_set.url)

        # 5. Can update room status
        status_url = f'/hotel-admin/{self.restaurant.slug}/rooms/{self.room101.id}/status/'
        resp_status = self.client.post(status_url, {'status': Room.RoomStatus.CLEANING})
        self.assertEqual(resp_status.status_code, 302)
        self.room101.refresh_from_db()
        self.assertEqual(self.room101.status, Room.RoomStatus.CLEANING)

    def test_front_desk_sidebar_and_feature_isolation(self):
        """
        Verify that a FRONT_DESK user:
        1. Sees Front Desk Operations and Rooms & Housekeeping.
        2. Does NOT see Room Categories & Tariffs or Hotel Configuration.
        3. Can take New Reservations, but cannot Add Physical Rooms.
        """
        fd_user = User.objects.create_user(username="fd_anita", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=fd_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.FRONT_DESK,
        )

        self.client.force_login(fd_user)

        resp_dash = self.client.get(f'/hotel-admin/{self.restaurant.slug}/')
        self.assertEqual(resp_dash.status_code, 200)
        self.assertTrue(resp_dash.context['is_front_desk'])
        self.assertTrue(resp_dash.context['show_front_desk_ops'])
        self.assertFalse(resp_dash.context['show_room_categories'])
        self.assertFalse(resp_dash.context['show_hotel_config'])
        self.assertTrue(resp_dash.context['can_create_reservation'])
        self.assertFalse(resp_dash.context['can_manage_physical_rooms'])

        self.assertContains(resp_dash, "Front Desk Operations")
        self.assertContains(resp_dash, "Front Desk Overview")
        self.assertContains(resp_dash, "Tape Chart Timeline")
        self.assertContains(resp_dash, "Guest Folios &amp; Bookings")
        self.assertContains(resp_dash, "Rooms &amp; Housekeeping")
        self.assertContains(resp_dash, "New Reservation")

        self.assertNotContains(resp_dash, "Room Categories &amp; Tariffs")
        self.assertNotContains(resp_dash, "Hotel Settings")
        self.assertNotContains(resp_dash, "Staff &amp; Roles")


class TravelDeskAndCabManagementTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Restaurant & Hotel Branches
        self.restaurant = Restaurant.objects.create(name="Royal Rajputana Palace", slug="royal-rajputana")
        self.hotel_branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Heritage Wing",
            slug="heritage-wing",
            has_hotel_module=True,
        )
        self.non_hotel_branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Dine-in Only Wing",
            slug="dinein-wing",
            has_hotel_module=False,
        )

        # 2. Room Categories (One Standard, One Luxury with free airport transfer)
        self.standard_category = RoomCategory.objects.create(
            branch=self.hotel_branch,
            name="Standard Deluxe",
            base_price_per_night=Decimal('4000.00'),
            max_occupancy=2,
            includes_free_transfer=False,
            transfer_type='NONE',
        )
        self.luxury_category = RoomCategory.objects.create(
            branch=self.hotel_branch,
            name="Presidential Villa",
            base_price_per_night=Decimal('12000.00'),
            max_occupancy=4,
            includes_free_transfer=True,
            transfer_type='BOTH',
        )

        # 3. Physical Rooms
        self.room_std = Room.objects.create(
            branch=self.hotel_branch,
            category=self.standard_category,
            room_number="101",
            status=Room.RoomStatus.AVAILABLE,
        )
        self.room_lux = Room.objects.create(
            branch=self.hotel_branch,
            category=self.luxury_category,
            room_number="VILLA-1",
            status=Room.RoomStatus.AVAILABLE,
        )

        # 4. Cab Packages (Airport Transfer & Sightseeing Tour)
        self.pkg_airport_sedan = CabServicePackage.objects.create(
            branch=self.hotel_branch,
            title="Airport Pickup (Sedan)",
            service_type=CabServicePackage.ServiceType.AIRPORT_TRANSFER,
            vehicle_category=CabServicePackage.VehicleCategory.SEDAN,
            base_price=Decimal('1500.00'),
            included_km=40,
            included_hours=2,
            is_active=True,
        )
        self.pkg_sightseeing = CabServicePackage.objects.create(
            branch=self.hotel_branch,
            title="Full-Day Heritage Sightseeing Tour",
            service_type=CabServicePackage.ServiceType.TOURISM_PACKAGE,
            vehicle_category=CabServicePackage.VehicleCategory.SUV,
            base_price=Decimal('4500.00'),
            included_km=80,
            included_hours=8,
            itinerary_description="Amber Fort, Hawa Mahal, City Palace, Jal Mahal",
            is_active=True,
        )

        # 5. Fleet Vehicles (In-house and Vendor)
        self.fleet_inhouse = FleetVehicle.objects.create(
            branch=self.hotel_branch,
            vehicle_number="RJ14 TA 1111",
            vehicle_category=CabServicePackage.VehicleCategory.SEDAN,
            driver_name="Raju Driver",
            driver_phone="9829011111",
            is_vendor_vehicle=False,
            status=FleetVehicle.VehicleStatus.AVAILABLE,
        )
        self.fleet_vendor = FleetVehicle.objects.create(
            branch=self.hotel_branch,
            vehicle_number="RJ14 TA 2222",
            vehicle_category=CabServicePackage.VehicleCategory.SUV,
            driver_name="Sanjay Taxi Agency",
            driver_phone="9829022222",
            is_vendor_vehicle=True,
            status=FleetVehicle.VehicleStatus.AVAILABLE,
        )

        # 6. Staff Users
        self.hotel_admin_user = User.objects.create_user(username="hotel_mgr", password="password123", email="mgr@royal.com")
        self.hotel_profile = RestaurantUser.objects.create(
            user=self.hotel_admin_user,
            restaurant=self.restaurant,
            branch=self.hotel_branch,
            role=RestaurantUser.Role.HOTEL_ADMIN,
        )

        self.restaurant_admin_user = User.objects.create_user(username="rest_user", password="password123", email="rest@royal.com")
        self.rest_profile = RestaurantUser.objects.create(
            user=self.restaurant_admin_user,
            restaurant=self.restaurant,
            branch=self.non_hotel_branch,
            role=RestaurantUser.Role.RESTAURANT_ADMIN,
        )

    def test_room_category_complimentary_transfer_fields(self):
        """Verify RoomCategory complimentary transfer attributes."""
        self.assertTrue(self.luxury_category.includes_free_transfer)
        self.assertEqual(self.luxury_category.transfer_type, 'BOTH')
        self.assertFalse(self.standard_category.includes_free_transfer)
        self.assertEqual(self.standard_category.transfer_type, 'NONE')

    def test_public_cab_packages_gating(self):
        """Verify cab packages are available only for branches with has_hotel_module=True."""
        # 1. Hotel enabled branch -> HTTP 200 with packages
        url_enabled = f'/api/public/hotel/{self.hotel_branch.id}/cab-packages/'
        resp1 = self.client.get(url_enabled)
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertEqual(resp1.data['count'], 2)

        # 2. Non-hotel branch -> HTTP 403
        url_disabled = f'/api/public/hotel/{self.non_hotel_branch.id}/cab-packages/'
        resp2 = self.client.get(url_disabled)
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

    def test_cab_packages_admin_access_control(self):
        """Verify cab package CRUD is restricted to HOTEL_ADMIN and GENERAL_MANAGER."""
        self.client.force_authenticate(user=self.restaurant_admin_user)
        resp_denied = self.client.get('/api/hotel/cabs/packages/')
        self.assertEqual(resp_denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.hotel_admin_user)
        resp_allowed = self.client.get('/api/hotel/cabs/packages/')
        self.assertEqual(resp_allowed.status_code, status.HTTP_200_OK)

    def test_unified_atomic_checkout_complimentary_airport_transfer(self):
        """
        When booking a tier with includes_free_transfer=True, claiming an airport pickup
        forces base_amount=0, tax_amount=0, total_amount=0, payment_status='COMPLIMENTARY'.
        """
        today = datetime.date.today()
        d_in = today + datetime.timedelta(days=7)
        d_out = today + datetime.timedelta(days=9)
        pickup_time = (timezone.now() + datetime.timedelta(days=7, hours=10)).isoformat()

        payload = {
            "category_id": self.luxury_category.id,
            "check_in_date": d_in.isoformat(),
            "check_out_date": d_out.isoformat(),
            "guest_name": "Vikramaditya Singh",
            "guest_phone": "9876543210",
            "guest_email": "vikram@example.com",
            "num_guests": 2,
            "cab_bookings": [
                {
                    "package_id": self.pkg_airport_sedan.id,
                    "pickup_datetime": pickup_time,
                    "pickup_location": "Jaipur International Airport Terminal 2",
                    "drop_location": "Royal Rajputana Palace",
                    "flight_or_train_number": "6E-204",
                    "is_complimentary": True,
                }
            ]
        }

        resp = self.client.post('/api/public/hotel/checkout/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data['success'])

        # Verify RoomBooking
        room_booking_id = resp.data['room_booking_id']
        rb = RoomBooking.objects.get(id=room_booking_id)
        self.assertEqual(rb.room, self.room_lux)
        self.assertEqual(rb.payment_status, RoomBooking.PaymentStatus.PENDING)

        # Verify complimentary CabBooking
        cabs = rb.cab_bookings.all()
        self.assertEqual(cabs.count(), 1)
        cab = cabs.first()
        self.assertTrue(cab.is_complimentary)
        self.assertEqual(cab.base_amount, Decimal('0.00'))
        self.assertEqual(cab.tax_amount, Decimal('0.00'))
        self.assertEqual(cab.total_amount, Decimal('0.00'))
        self.assertEqual(cab.payment_status, CabBooking.PaymentStatus.COMPLIMENTARY)
        self.assertEqual(cab.trip_status, CabBooking.TripStatus.SCHEDULED)
        self.assertEqual(cab.flight_or_train_number, "6E-204")

        # Verify grand total does NOT add charges for complimentary cab
        self.assertEqual(Decimal(str(resp.data['grand_total'])), rb.grand_total)
        self.assertEqual(Decimal(str(resp.data['cab_total'])), Decimal('0.00'))

    def test_unified_atomic_checkout_paid_transfer_and_tour(self):
        """
        Standard room guests purchasing airport transfer + sightseeing tour:
        - Transfers and tours calculate base_price + dynamic 5% transport tax
        - Grand total combines Room + Paid Cabs into a single Razorpay order
        """
        today = datetime.date.today()
        d_in = today + datetime.timedelta(days=10)
        d_out = today + datetime.timedelta(days=12)
        pickup_time_1 = (timezone.now() + datetime.timedelta(days=10, hours=12)).isoformat()
        pickup_time_2 = (timezone.now() + datetime.timedelta(days=11, hours=9)).isoformat()

        payload = {
            "category_id": self.standard_category.id,
            "check_in_date": d_in.isoformat(),
            "check_out_date": d_out.isoformat(),
            "guest_name": "Rohan Mehra",
            "guest_phone": "9811223344",
            "guest_email": "rohan@example.com",
            "num_guests": 2,
            "cab_Bookings": [
                {
                    "package_id": self.pkg_airport_sedan.id,
                    "pickup_datetime": pickup_time_1,
                    "pickup_location": "Jaipur Railway Station",
                    "drop_location": "Royal Rajputana Palace",
                    "flight_or_train_number": "12956 Jaipur Superfast",
                    "is_complimentary": False,
                },
                {
                    "package_id": self.pkg_sightseeing.id,
                    "pickup_datetime": pickup_time_2,
                    "pickup_location": "Royal Rajputana Palace Porch",
                    "drop_location": "Heritage Circuit",
                    "is_complimentary": False,
                }
            ]
        }

        resp = self.client.post('/api/public/hotel/checkout/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        rb = RoomBooking.objects.get(id=resp.data['room_booking_id'])
        cabs = rb.cab_bookings.order_by('id')
        self.assertEqual(cabs.count(), 2)

        # Ride 1: Airport Sedan -> 1500 + 75 (5%) = 1575
        cab1 = cabs[0]
        self.assertFalse(cab1.is_complimentary)
        self.assertEqual(cab1.base_amount, Decimal('1500.00'))
        self.assertEqual(cab1.tax_amount, Decimal('75.00'))
        self.assertEqual(cab1.total_amount, Decimal('1575.00'))
        self.assertEqual(cab1.payment_status, CabBooking.PaymentStatus.PENDING)

        # Ride 2: Sightseeing Tour -> 4500 + 225 (5%) = 4725
        cab2 = cabs[1]
        self.assertFalse(cab2.is_complimentary)
        self.assertEqual(cab2.base_amount, Decimal('4500.00'))
        self.assertEqual(cab2.tax_amount, Decimal('225.00'))
        self.assertEqual(cab2.total_amount, Decimal('4725.00'))
        self.assertEqual(cab2.payment_status, CabBooking.PaymentStatus.PENDING)

        expected_cab_total = Decimal('1575.00') + Decimal('4725.00')
        self.assertEqual(Decimal(str(resp.data['cab_total'])), expected_cab_total)
        self.assertEqual(Decimal(str(resp.data['grand_total'])), rb.grand_total + expected_cab_total)
        self.assertTrue(resp.data['razorpay_order_id'].startswith("order_"))

    def test_front_desk_manual_cab_booking_added_to_folio(self):
        """Front desk can book a ride for an in-house guest and add it to their folio."""
        self.client.force_authenticate(user=self.hotel_admin_user)

        today = datetime.date.today()
        booking = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Kavita Sharma",
            guest_email="kavita@test.com",
            guest_phone="9876500000",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=2),
            booking_status=RoomBooking.BookingStatus.CHECKED_IN,
        )

        pickup_time = (timezone.now() + datetime.timedelta(hours=4)).isoformat()
        payload = {
            "room_booking_id": booking.id,
            "package_id": self.pkg_sightseeing.id,
            "pickup_datetime": pickup_time,
            "pickup_location": "Hotel Lobby",
            "drop_location": "Nahargarh Fort Sunset Point",
            "payment_status": "ADDED_TO_FOLIO",
            "is_complimentary": False,
        }

        resp = self.client.post('/api/hotel/cabs/bookings/manual/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data['success'])

        cb_data = resp.data['cab_booking']
        self.assertEqual(cb_data['payment_status'], 'ADDED_TO_FOLIO')
        self.assertEqual(Decimal(str(cb_data['total_amount'])), Decimal('4725.00'))

    def test_dispatch_board_filtering_and_chronology(self):
        """Verify Dispatch Board returns rides sorted by time with summary metrics and unassigned filter."""
        self.client.force_authenticate(user=self.hotel_admin_user)
        today = timezone.localdate()

        rb = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Guest A",
            guest_email="ga@test.com",
            guest_phone="9999911111",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
        )

        # Create 2 rides today at different hours
        dt1 = timezone.make_aware(datetime.datetime.combine(today, datetime.time(15, 0)))
        dt2 = timezone.make_aware(datetime.datetime.combine(today, datetime.time(9, 30)))

        cab_late = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_airport_sedan,
            pickup_datetime=dt1,
            pickup_location="Airport",
            drop_location="Hotel",
            trip_status=CabBooking.TripStatus.SCHEDULED,
        )
        cab_early = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_sightseeing,
            assigned_vehicle=self.fleet_inhouse,
            pickup_datetime=dt2,
            pickup_location="Hotel",
            drop_location="Tour",
            trip_status=CabBooking.TripStatus.DRIVER_ASSIGNED,
        )

        # GET dispatch-board
        resp = self.client.get(f'/api/hotel/cabs/dispatch-board/?date={today.isoformat()}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['summary']['total_rides'], 2)
        self.assertEqual(resp.data['summary']['unassigned_rides'], 1)
        self.assertEqual(resp.data['summary']['assigned_rides'], 1)

        # Verify chronological order (9:30 AM first, then 3:00 PM)
        rides = resp.data['rides']
        self.assertEqual(rides[0]['id'], cab_early.id)
        self.assertEqual(rides[1]['id'], cab_late.id)

        # Test unassigned_only filter
        resp_unassigned = self.client.get(f'/api/hotel/cabs/dispatch-board/?date={today.isoformat()}&unassigned_only=true')
        self.assertEqual(resp_unassigned.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp_unassigned.data['rides']), 1)
        self.assertEqual(resp_unassigned.data['rides'][0]['id'], cab_late.id)

    def test_assign_driver_inhouse_and_quick_add_vendor_cab(self):
        """Test assigning in-house vehicle and quick-adding an outsourced vendor cab."""
        self.client.force_authenticate(user=self.hotel_admin_user)
        today = timezone.localdate()
        rb = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Guest B",
            guest_email="gb@test.com",
            guest_phone="9999922222",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
        )
        cab1 = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_airport_sedan,
            pickup_datetime=timezone.now(),
            pickup_location="Airport",
            drop_location="Hotel",
            trip_status=CabBooking.TripStatus.SCHEDULED,
        )
        cab2 = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_sightseeing,
            pickup_datetime=timezone.now(),
            pickup_location="Hotel",
            drop_location="City",
            trip_status=CabBooking.TripStatus.SCHEDULED,
        )

        # 1. Assign in-house vehicle
        assign_url_1 = f'/api/hotel/cabs/bookings/{cab1.id}/assign-driver/'
        resp1 = self.client.patch(assign_url_1, {'vehicle_id': self.fleet_inhouse.id}, format='json')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        cab1.refresh_from_db()
        self.assertEqual(cab1.trip_status, CabBooking.TripStatus.DRIVER_ASSIGNED)
        self.assertEqual(cab1.assigned_vehicle, self.fleet_inhouse)
        self.fleet_inhouse.refresh_from_db()
        self.assertEqual(self.fleet_inhouse.status, FleetVehicle.VehicleStatus.ON_TRIP)

        # 2. Quick-add vendor cab on the fly for busy day
        assign_url_2 = f'/api/hotel/cabs/bookings/{cab2.id}/assign-driver/'
        quick_payload = {
            "quick_add_vendor": True,
            "vehicle_number": "RJ14 TA 9999",
            "driver_name": "Mahesh Vendor",
            "driver_phone": "9829099999",
            "vehicle_category": "SUV",
        }
        resp2 = self.client.patch(assign_url_2, quick_payload, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        cab2.refresh_from_db()
        self.assertEqual(cab2.trip_status, CabBooking.TripStatus.DRIVER_ASSIGNED)
        self.assertTrue(cab2.assigned_vehicle.is_vendor_vehicle)
        self.assertEqual(cab2.assigned_vehicle.vehicle_number, "RJ14 TA 9999")
        self.assertEqual(cab2.assigned_vehicle.status, FleetVehicle.VehicleStatus.ON_TRIP)

    def test_delayed_flights_reschedule_preserves_pricing(self):
        """Hotel admin can edit pickup time without altering booking price or payment status."""
        self.client.force_authenticate(user=self.hotel_admin_user)
        today = timezone.localdate()
        rb = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Guest C",
            guest_email="gc@test.com",
            guest_phone="9999933333",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
        )
        cab = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_airport_sedan,
            pickup_datetime=timezone.now(),
            pickup_location="Airport T2",
            drop_location="Hotel",
            flight_or_train_number="6E-101",
            base_amount=Decimal('1500.00'),
            tax_amount=Decimal('75.00'),
            total_amount=Decimal('1575.00'),
            payment_status=CabBooking.PaymentStatus.PAID,
            trip_status=CabBooking.TripStatus.SCHEDULED,
        )

        # Flight is delayed by 3 hours
        new_time = timezone.now() + datetime.timedelta(hours=3)
        resched_url = f'/api/hotel/cabs/bookings/{cab.id}/reschedule/'
        payload = {
            "pickup_datetime": new_time.isoformat(),
            "flight_or_train_number": "6E-101 (Delayed)",
        }
        resp = self.client.patch(resched_url, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        cab.refresh_from_db()
        self.assertEqual(cab.flight_or_train_number, "6E-101 (Delayed)")
        # Price and payment must remain unchanged
        self.assertEqual(cab.base_amount, Decimal('1500.00'))
        self.assertEqual(cab.tax_amount, Decimal('75.00'))
        self.assertEqual(cab.total_amount, Decimal('1575.00'))
        self.assertEqual(cab.payment_status, CabBooking.PaymentStatus.PAID)

    def test_trip_status_completed_releases_vehicle(self):
        """When a ride is completed, its assigned vehicle is released back to AVAILABLE."""
        self.client.force_authenticate(user=self.hotel_admin_user)
        self.fleet_inhouse.status = FleetVehicle.VehicleStatus.ON_TRIP
        self.fleet_inhouse.save()

        today = timezone.localdate()
        rb = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Guest D",
            guest_email="gd@test.com",
            guest_phone="9999944444",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
        )
        cab = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_airport_sedan,
            assigned_vehicle=self.fleet_inhouse,
            pickup_datetime=timezone.now(),
            pickup_location="Airport",
            drop_location="Hotel",
            trip_status=CabBooking.TripStatus.EN_ROUTE,
        )

        status_url = f'/api/hotel/cabs/bookings/{cab.id}/status/'
        resp = self.client.patch(status_url, {'status': 'COMPLETED'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        cab.refresh_from_db()
        self.assertEqual(cab.trip_status, CabBooking.TripStatus.COMPLETED)
        self.fleet_inhouse.refresh_from_db()
        self.assertEqual(self.fleet_inhouse.status, FleetVehicle.VehicleStatus.AVAILABLE)

    def test_room_cancellation_cascade_cancels_cabs_and_releases_fleet(self):
        """When a RoomBooking is cancelled, all linked CabBookings are cancelled and vehicles released."""
        today = timezone.localdate()
        self.fleet_inhouse.status = FleetVehicle.VehicleStatus.ON_TRIP
        self.fleet_inhouse.save()

        rb = RoomBooking.objects.create(
            room=self.room_std,
            branch=self.hotel_branch,
            guest_name="Guest E",
            guest_email="ge@test.com",
            guest_phone="9999955555",
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
            booking_status=RoomBooking.BookingStatus.RESERVED,
        )
        cab = CabBooking.objects.create(
            branch=self.hotel_branch,
            room_booking=rb,
            package=self.pkg_airport_sedan,
            assigned_vehicle=self.fleet_inhouse,
            pickup_datetime=timezone.now(),
            pickup_location="Airport",
            drop_location="Hotel",
            trip_status=CabBooking.TripStatus.DRIVER_ASSIGNED,
        )

        # Cancel RoomBooking
        rb.booking_status = RoomBooking.BookingStatus.CANCELLED
        rb.save()

        cab.refresh_from_db()
        self.assertEqual(cab.trip_status, CabBooking.TripStatus.CANCELLED)

        self.fleet_inhouse.refresh_from_db()
        self.assertEqual(self.fleet_inhouse.status, FleetVehicle.VehicleStatus.AVAILABLE)

