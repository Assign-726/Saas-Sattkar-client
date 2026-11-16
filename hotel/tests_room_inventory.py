from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hotel.models import Room, RoomBooking
from hotel import tests as hotel_tests  # module import so the runner doesn't re-collect its TestCases
from hotel.views_admin import _expand_room_numbers


class ExpandRoomNumbersTests(TestCase):
    def test_ranges_lists_and_dedup(self):
        self.assertEqual(_expand_room_numbers('101-103, 110, 2A, 101'), ['101', '102', '103', '110', '2A'])

    def test_bad_range_rejected(self):
        with self.assertRaises(ValueError):
            _expand_room_numbers('110-101')

    def test_limit(self):
        with self.assertRaises(ValueError):
            _expand_room_numbers('1-500')


class CategoryRoomManagementTests(TestCase):
    setUp = hotel_tests.HotelModuleTests.setUp

    def url(self, path):
        return f'/hotel-admin/{self.restaurant.slug}/{path}'

    def test_categories_page_lists_rooms(self):
        self.client.force_login(self.hotel_admin_user)
        resp = self.client.get(self.url('categories/'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'id="catRooms{self.category.id}"')
        self.assertContains(resp, '2 units')

    def test_bulk_add_skips_existing(self):
        self.client.force_login(self.hotel_admin_user)
        resp = self.client.post(self.url(f'categories/{self.category.id}/rooms/add/'),
                                {'room_numbers': '101-104', 'floor': '1st Floor'})
        self.assertEqual(resp.status_code, 302)
        numbers = set(Room.objects.filter(branch=self.branch).values_list('room_number', flat=True))
        self.assertEqual(numbers, {'101', '102', '103', '104'})

    def test_remove_blocked_by_live_booking(self):
        self.client.force_login(self.hotel_admin_user)
        today = timezone.localdate()
        RoomBooking.objects.create(
            room=self.room101, branch=self.branch, guest_name='A', guest_email='a@x.com', guest_phone='1',
            check_in_date=today, check_out_date=today + timedelta(days=1),
        )
        self.client.post(self.url(f'rooms/{self.room101.id}/remove/'))
        self.client.post(self.url(f'rooms/{self.room102.id}/remove/'))
        self.room101.refresh_from_db()
        self.room102.refresh_from_db()
        self.assertTrue(self.room101.is_active)
        self.assertFalse(self.room102.is_active)

    def test_rooms_board_renders_guest(self):
        self.client.force_login(self.hotel_admin_user)
        today = timezone.localdate()
        RoomBooking.objects.create(
            room=self.room101, branch=self.branch, guest_name='Nidish Kumar', guest_email='a@x.com', guest_phone='1',
            check_in_date=today, check_out_date=today + timedelta(days=1),
            booking_status=RoomBooking.BookingStatus.CHECKED_IN,
        )
        self.room101.status = Room.RoomStatus.OCCUPIED
        self.room101.save()
        resp = self.client.get(self.url('rooms/'))
        self.assertContains(resp, 'Nidish Kumar')


class EntirePropertyTests(TestCase):
    setUp = hotel_tests.HotelModuleTests.setUp

    def url(self, path):
        return f'/hotel-admin/{self.restaurant.slug}/{path}'

    def _create_villa(self):
        self.client.force_login(self.hotel_admin_user)
        self.client.post(self.url('categories/create-edit/'), {
            'name': 'Royal Garden Villa', 'base_price_per_night': '45000',
            'max_occupancy': '25', 'max_adults': '20', 'max_children': '5',
            'is_entire_property': 'on', 'bedrooms_count': '4',
        })
        from hotel.models import RoomCategory
        return RoomCategory.objects.get(branch=self.branch, name='Royal Garden Villa')

    def test_flag_creates_single_unit_and_blocks_more(self):
        villa = self._create_villa()
        self.assertTrue(villa.is_entire_property)
        self.assertEqual(villa.bedrooms_count, 4)
        self.assertEqual(villa.rooms.filter(is_active=True).count(), 1)
        self.client.post(self.url(f'categories/{villa.id}/rooms/add/'), {'room_numbers': 'V2'})
        self.assertEqual(villa.rooms.filter(is_active=True).count(), 1)
        # Re-saving must not create a second unit
        self.client.post(self.url('categories/create-edit/'), {
            'category_id': villa.id, 'name': villa.name, 'base_price_per_night': '45000', 'is_entire_property': 'on',
        })
        self.assertEqual(villa.rooms.filter(is_active=True).count(), 1)

    def test_booking_keeps_event_details_only_for_villas(self):
        villa = self._create_villa()
        unit = villa.rooms.get()
        today = timezone.localdate()
        common = {'guest_name': 'Party Host', 'guest_email': 'p@x.com', 'guest_phone': '9000000000',
                  'check_in_date': (today + timedelta(days=3)).isoformat(),
                  'check_out_date': (today + timedelta(days=4)).isoformat(), 'num_guests': '20',
                  'event_type': 'PARTY', 'expected_headcount': '40', 'event_notes': 'DJ till 11pm'}
        self.client.post(self.url('bookings/create/'), {**common, 'room_id': unit.id})
        b = RoomBooking.objects.get(room=unit)
        self.assertEqual((b.event_type, b.expected_headcount, b.event_notes), ('PARTY', 40, 'DJ till 11pm'))

        self.client.post(self.url('bookings/create/'), {**common, 'room_id': self.room101.id, 'num_guests': '2'})
        r = RoomBooking.objects.get(room=self.room101)
        self.assertEqual((r.event_type, r.expected_headcount), ('', None))

        resp = self.client.get(self.url(f'bookings/{b.id}/'))
        self.assertContains(resp, 'Party / celebration')
        self.assertContains(resp, 'DJ till 11pm')
