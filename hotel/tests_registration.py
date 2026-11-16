"""Guest registration rules (ID before check-in / check-out), guest form validation and list pagination."""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from hotel import folio as F
from hotel import tests as hotel_tests  # module import so the runner doesn't re-collect its TestCases
from hotel.models import HotelSettings, RoomBooking


class RegistrationTests(TestCase):
    setUp_base = hotel_tests.HotelModuleTests.setUp

    def setUp(self):
        self.setUp_base()
        HotelSettings.objects.filter(branch=self.branch).update(require_id_at_check_in=True, require_id_document=True)
        today = timezone.localdate()
        self.b = RoomBooking.objects.create(
            room=self.room101, branch=self.branch, guest_name='Lakhveer Singh', guest_email='l@x.com',
            guest_phone='9876543210', check_in_date=today, check_out_date=today + timedelta(days=2))
        self.client.force_login(self.hotel_admin_user)

    def url(self, path):
        return f'/hotel-admin/{self.restaurant.slug}/bookings/{self.b.pk}/{path}'

    def test_new_hotels_require_id_by_default(self):
        s = HotelSettings._meta.get_field('require_id_at_check_in')
        self.assertTrue(s.default)

    def test_check_in_blocked_until_id_and_scan(self):
        resp = self.client.post(self.url('check-in/'))
        self.assertRedirects(resp, self.url('') + '?tab=guest', fetch_redirect_response=False)
        self.b.refresh_from_db()
        self.assertEqual(self.b.booking_status, 'RESERVED')

        self.client.post(self.url('guest/'), {
            'guest_name': 'Lakhveer Singh', 'guest_phone': '9876543210', 'nationality': 'Indian',
            'id_type': 'AADHAAR', 'id_number': '2345 6789 0123', 'adults': '1', 'children': '0',
            'id_document': SimpleUploadedFile('id.png', b'\x89PNG', content_type='image/png'),
        })
        self.b.refresh_from_db()
        self.assertEqual(self.b.id_number, '234567890123')
        self.client.post(self.url('check-in/'))
        self.b.refresh_from_db()
        self.assertEqual(self.b.booking_status, 'CHECKED_IN')

    def test_check_out_blocked_for_guest_let_in_without_id(self):
        RoomBooking.objects.filter(pk=self.b.pk).update(booking_status='CHECKED_IN')
        self.b.refresh_from_db()
        self.assertIn('registration is incomplete', F.check_out(self.b, self.hotel_admin_user, allow_balance=True))

    def test_invalid_fields_are_reported_and_nothing_is_saved(self):
        resp = self.client.post(self.url('guest/'), {
            'guest_name': 'New Name', 'guest_phone': '12345', 'guest_email': 'nope',
            'id_type': 'AADHAAR', 'id_number': '1234', 'adults': '1', 'children': '0',
        })
        self.assertEqual(resp.status_code, 200)
        for msg in ('Enter a valid phone number', 'Enter a valid email address', 'Aadhaar numbers have 12 digits'):
            self.assertContains(resp, msg)
        self.assertContains(resp, 'value="New Name"')  # typed values are kept
        self.b.refresh_from_db()
        self.assertEqual(self.b.guest_name, 'Lakhveer Singh')

    def test_foreign_guest_needs_passport(self):
        resp = self.client.post(self.url('guest/'), {
            'guest_name': 'Anna Muller', 'guest_phone': '+491512345678', 'nationality': 'German',
            'id_type': 'AADHAAR', 'id_number': '234567890123', 'adults': '1', 'children': '0',
        })
        self.assertContains(resp, 'Foreign nationals must register with a passport')

    def test_occupancy_limit(self):
        resp = self.client.post(self.url('guest/'), {
            'guest_name': 'Big Family', 'guest_phone': '9876543210', 'adults': '3', 'children': '2',
        })
        self.assertContains(resp, 'takes at most 3 guests')


class PaginationTests(TestCase):
    setUp_base = hotel_tests.HotelModuleTests.setUp

    def setUp(self):
        self.setUp_base()
        self.client.force_login(self.hotel_admin_user)
        today = timezone.localdate()
        RoomBooking.objects.bulk_create([
            RoomBooking(room=self.room101, branch=self.branch, guest_name=f'Guest {i:02d}', guest_email='g@x.com',
                        guest_phone='9876543210', check_in_date=today + timedelta(days=i), booking_id=f'BK-T-{i:03d}',
                        check_out_date=today + timedelta(days=i + 1), booking_status='CANCELLED')
            for i in range(30)])

    def test_guest_folios_paginate_and_keep_filters(self):
        url = f'/hotel-admin/{self.restaurant.slug}/bookings/'
        first = self.client.get(url, {'status': 'CANCELLED'})
        self.assertEqual(len(first.context['bookings']), 25)
        self.assertContains(first, 'of <strong>30</strong> bookings')
        self.assertContains(first, 'status=CANCELLED&amp;page=2')
        second = self.client.get(url, {'status': 'CANCELLED', 'page': 2})
        self.assertEqual(len(second.context['bookings']), 5)
        small = self.client.get(url, {'per_page': 10})
        self.assertEqual(len(small.context['bookings']), 10)


class WalkInReservationTests(TestCase):
    setUp_base = hotel_tests.HotelModuleTests.setUp

    def setUp(self):
        self.setUp_base()
        HotelSettings.objects.filter(branch=self.branch).update(require_id_at_check_in=True, require_id_document=True)
        self.client.force_login(self.hotel_admin_user)
        self.url = f'/hotel-admin/{self.restaurant.slug}/bookings/create/'
        self.out = (timezone.localdate() + timedelta(days=2)).isoformat()

    def base(self, **extra):
        data = {'mode': 'walk_in', 'room_id': self.room101.id, 'check_out_date': self.out, 'adults': '2', 'children': '0',
                'guest_name': 'Lakhveer Singh', 'guest_phone': '98765 43210', 'nationality': 'Indian',
                'next': f'/hotel-admin/{self.restaurant.slug}/bookings/'}
        data.update(extra)
        return data

    def test_walk_in_without_id_is_rejected_and_draft_kept(self):
        resp = self.client.post(self.url, self.base())
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(RoomBooking.objects.exists())
        draft = self.client.session['nb_draft']
        self.assertEqual(draft['values']['guest_name'], 'Lakhveer Singh')
        self.assertIn('id_type', draft['errors'])
        self.assertIn('id_document', draft['errors'])
        self.assertIn('guest_address', draft['errors'])
        # The next page shows the modal again with the values (and clears the draft)
        page = self.client.get(f'/hotel-admin/{self.restaurant.slug}/bookings/')
        self.assertContains(page, 'Lakhveer Singh')
        self.assertNotIn('nb_draft', self.client.session)

    def test_walk_in_with_registration_is_checked_in(self):
        resp = self.client.post(self.url, self.base(
            id_type='AADHAAR', id_number='2345 6789 0123', guest_address='12 MG Road, Jalandhar',
            id_document=SimpleUploadedFile('id.jpg', b'\xff\xd8\xff', content_type='image/jpeg')))
        b = RoomBooking.objects.get()
        self.assertRedirects(resp, f'/hotel-admin/{self.restaurant.slug}/bookings/{b.pk}/', fetch_redirect_response=False)
        self.assertEqual((b.booking_status, b.source, b.check_in_date, b.adults), ('CHECKED_IN', 'WALK_IN', timezone.localdate(), 2))
        self.assertEqual(b.id_number, '234567890123')
        self.assertTrue(b.id_document)

    def test_occupancy_and_phone_checked(self):
        self.client.post(self.url, self.base(adults='3', children='2', guest_phone='12345'))
        errs = self.client.session['nb_draft']['errors']
        self.assertIn('adults', errs)
        self.assertIn('guest_phone', errs)

    def test_advance_reservation_does_not_need_id(self):
        self.client.post(self.url, {'mode': 'reserve', 'room_id': self.room102.id,
                                    'check_in_date': (timezone.localdate() + timedelta(days=5)).isoformat(),
                                    'check_out_date': (timezone.localdate() + timedelta(days=6)).isoformat(),
                                    'adults': '1', 'guest_name': 'Phone Guest', 'guest_phone': '9123456789'})
        b = RoomBooking.objects.get()
        self.assertEqual((b.booking_status, b.id_type), ('RESERVED', ''))
