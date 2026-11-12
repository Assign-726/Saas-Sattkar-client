"""Hotel PMS: pricing, folio, stay changes, housekeeping, maintenance, rates, reports and night audit."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hotel import folio as F
from hotel import pricing, reports
from hotel.models import (FolioCharge, FolioPayment, HotelSettings, HousekeepingTask, MaintenanceIssue, NightAudit,
                          RateOverride, Room, RoomBooking, RoomCategory)
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class PMSTestBase(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.r = Restaurant.objects.create(name='Lake View', slug='lake-view')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Lake', slug='lake', has_hotel_module=True)
        self.settings = HotelSettings.get_settings(self.b)
        # These tests are about stays, not guest registration: switch the (default-on) ID rule off
        self.settings.require_id_at_check_in = False
        self.settings.save()
        self.cat = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=D('4000'))
        self.suite = RoomCategory.objects.create(branch=self.b, name='Suite', base_price_per_night=D('9000'))
        self.r101 = Room.objects.create(branch=self.b, category=self.cat, room_number='101')
        self.r102 = Room.objects.create(branch=self.b, category=self.cat, room_number='102')
        self.s201 = Room.objects.create(branch=self.b, category=self.suite, room_number='201')
        self.user = User.objects.create_user('fo', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role=RestaurantUser.Role.HOTEL_ADMIN)
        self.client.force_login(self.user)

    def url(self, name, **kw):
        return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def book(self, room=None, nights=2, start=None, status='RESERVED', **kw):
        start = start or self.today
        room = room or self.r101
        return RoomBooking.objects.create(room=room, branch=room.branch, guest_name=kw.pop('name', 'Asha Rao'),
                                          guest_email='a@x.com', guest_phone=kw.pop('phone', '9800000001'),
                                          check_in_date=start, check_out_date=start + datetime.timedelta(days=nights),
                                          booking_status=status, **kw)


class PricingTests(PMSTestBase):
    def test_base_price_and_gst_slab(self):
        q = pricing.quote(self.cat, self.today, self.today + datetime.timedelta(days=2), self.settings)
        self.assertEqual((q['net'], q['tax'], q['total']), (D('8000.00'), D('960.00'), D('8960.00')))
        q = pricing.quote(self.suite, self.today, self.today + datetime.timedelta(days=1), self.settings)
        self.assertEqual(q['tax'], D('1620.00'))  # above ₹7,500 a night → 18%

    def test_seasonal_rate_weekdays_and_newest_wins(self):
        start = self.today
        RateOverride.objects.create(category=self.cat, start_date=start, end_date=start + datetime.timedelta(days=30), price_per_night=D('5000'))
        RateOverride.objects.create(category=self.cat, start_date=start, end_date=start + datetime.timedelta(days=30), price_per_night=D('6000'),
                                    weekdays=[start.weekday()])
        rows = pricing.nightly_rates(self.cat, start, start + datetime.timedelta(days=2))
        self.assertEqual([p for _, p, _ in rows], [D('6000'), D('5000')])

    def test_stop_sell_and_min_stay(self):
        RateOverride.objects.create(category=self.cat, start_date=self.today, end_date=self.today, min_stay=3)
        self.assertIn('minimum stay of 3', pricing.quote(self.cat, self.today, self.today + datetime.timedelta(days=2))['restriction'])
        RateOverride.objects.create(category=self.suite, start_date=self.today, end_date=self.today, stop_sell=True)
        self.assertIn('closed for sale', pricing.quote(self.suite, self.today, self.today + datetime.timedelta(days=1))['restriction'])

    def test_out_of_order_room_is_not_sold(self):
        MaintenanceIssue.objects.create(branch=self.b, room=self.r101, title='Leak', out_of_order=True,
                                        block_from=self.today, block_until=self.today + datetime.timedelta(days=3))
        free = pricing.free_rooms(Room.objects.filter(category=self.cat), self.b, self.today, self.today + datetime.timedelta(days=1))
        self.assertEqual(list(free), [self.r102])
        self.assertIn('out of order', pricing.room_conflict(self.r101, self.today, self.today + datetime.timedelta(days=1)))

    def test_admin_booking_uses_seasonal_price(self):
        RateOverride.objects.create(category=self.cat, start_date=self.today, end_date=self.today + datetime.timedelta(days=5), price_per_night=D('5000'))
        self.client.post(self.url('booking_create'), {'room_id': self.r102.pk, 'guest_name': 'Ravi', 'guest_phone': '98',
                                                      'check_in_date': self.today.isoformat(),
                                                      'check_out_date': (self.today + datetime.timedelta(days=2)).isoformat(),
                                                      'payment_status': 'PARTIALLY_PAID', 'advance_amount': '3000', 'payment_method': 'UPI',
                                                      'source': 'WALK_IN'})
        b = RoomBooking.objects.get(guest_name='Ravi')
        self.assertEqual((b.total_room_charges, b.grand_total, b.source), (D('10000.00'), D('11200.00'), 'WALK_IN'))
        self.assertEqual(b.payment_status, 'PARTIALLY_PAID')
        self.assertEqual(b.folio().paid, D('3000.00'))

    def test_public_availability_hides_closed_type_and_shows_nightly_rates(self):
        RateOverride.objects.create(category=self.suite, start_date=self.today, end_date=self.today, stop_sell=True)
        RateOverride.objects.create(category=self.cat, start_date=self.today, end_date=self.today, price_per_night=D('4500'), label='Festival')
        resp = self.client.get('/api/hotel/public/availability/', {'branch_id': self.b.pk, 'start': self.today.isoformat(),
                                                                   'end': (self.today + datetime.timedelta(days=1)).isoformat()})
        if resp.status_code == 404:
            self.skipTest('public availability URL differs')
        cats = {c['name']: c for c in resp.json()['categories']}
        self.assertNotIn('Suite', cats)
        self.assertEqual(cats['Deluxe']['nightly_rates'][0], {'date': self.today.isoformat(), 'price': '4500.00', 'label': 'Festival'})


class FolioTests(PMSTestBase):
    def test_charges_payments_balance_and_status(self):
        b = self.book()
        self.assertEqual(b.grand_total, D('8960.00'))
        F.post_charge(b, self.user, 'RESTAURANT', 'Dinner', D('1000'), 1, 5)
        F.post_charge(b, self.user, 'DISCOUNT', 'Loyal guest', D('500'), 1, 0)
        folio = F.Folio(b)
        self.assertEqual(folio.total, D('8960.00') + D('1050.00') - D('500.00'))
        F.record_payment(b, self.user, D('5000'), 'UPI')
        b.refresh_from_db()
        self.assertEqual(b.payment_status, 'PARTIALLY_PAID')
        F.record_payment(b, self.user, F.Folio(b).balance, 'CASH')
        b.refresh_from_db()
        self.assertEqual((b.payment_status, F.Folio(b).balance), ('PAID', D('0.00')))

    def test_void_needs_reason_and_refund_capped(self):
        b = self.book()
        charge, _ = F.post_charge(b, self.user, 'MINIBAR', 'Water', D('100'), 2, 18)
        self.assertEqual(F.void_charge(charge, self.user, ''), 'Give a reason for voiding the charge.')
        self.assertIsNone(F.void_charge(charge, self.user, 'Wrong room'))
        self.assertEqual(F.Folio(b).extras_total, D('0'))
        F.record_payment(b, self.user, D('1000'), 'CASH')
        _, err = F.record_payment(b, self.user, D('2000'), 'CASH', kind='REFUND')
        self.assertIn('can’t refund more', err)

    def test_paid_online_booking_gets_opening_payment(self):
        b = self.book(payment_status='PAID', payment_method='ONLINE_RAZORPAY', razorpay_payment_id='pay_1')
        p = FolioPayment.objects.get(booking=b)
        self.assertEqual((p.method, p.amount, p.reference), ('ONLINE', b.grand_total, 'pay_1'))
        F.post_charge(b, self.user, 'LAUNDRY', 'Shirts', D('200'), 1, 18)
        b.refresh_from_db()
        self.assertEqual((b.payment_status, F.Folio(b).balance), ('PARTIALLY_PAID', D('236.00')))

    def test_cab_billed_to_room_is_on_the_folio(self):
        from hotel.models import CabBooking
        b = self.book()
        CabBooking.objects.create(room_booking=b, branch=self.b, pickup_datetime=timezone.now(), pickup_location='Airport',
                                  drop_location='Hotel', base_amount=D('1000'), tax_amount=D('50'), total_amount=D('1050'),
                                  payment_status='ADDED_TO_FOLIO')
        self.assertEqual(F.Folio(b).extras_total, D('1050'))


class StayTests(PMSTestBase):
    def test_check_in_requires_id_when_configured(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.settings.require_id_at_check_in = True
        self.settings.save()
        b = self.book()
        self.assertIn('ID type, ID number and ID scan', F.check_in(b, self.user))
        b.id_type, b.id_number = 'AADHAAR', '234567890123'
        b.save()
        self.assertIn('ID scan', F.check_in(b, self.user))
        b.id_document = SimpleUploadedFile('id.png', b'\x89PNG', content_type='image/png')
        b.save()
        self.assertIsNone(F.check_in(b, self.user))
        self.r101.refresh_from_db()
        self.assertEqual(self.r101.status, 'OCCUPIED')

    def test_foreign_guest_needs_passport_and_visa(self):
        b = self.book(nationality='German', id_type='AADHAAR', id_number='1')
        self.assertFalse(b.kyc_complete)
        b.id_type, b.visa_number = 'PASSPORT', 'V1'
        self.assertTrue(b.kyc_complete)

    def test_checkout_blocks_balance_issues_invoice_and_creates_cleaning(self):
        b = self.book()
        F.check_in(b, self.user)
        self.assertIn('still due', F.check_out(b, self.user))
        self.assertIsNone(F.check_out(b, self.user, allow_balance=True))
        b.refresh_from_db()
        self.assertTrue(b.invoice_number.startswith('INV-HTL-'))
        self.r101.refresh_from_db()
        self.assertEqual(self.r101.status, 'CLEANING')
        task = HousekeepingTask.objects.get(room=self.r101)
        self.assertEqual(task.kind, 'CHECKOUT_CLEAN')
        b2 = self.book(room=self.r102)
        F.check_in(b2, self.user)
        F.record_payment(b2, self.user, F.Folio(b2).balance, 'CARD')
        F.check_out(b2, self.user)
        b2.refresh_from_db()
        self.assertEqual(int(b2.invoice_number[-5:]), int(b.invoice_number[-5:]) + 1)

    def test_extend_prices_added_nights_and_detects_clash(self):
        b = self.book(nights=1)
        RateOverride.objects.create(category=self.cat, start_date=self.today + datetime.timedelta(days=1),
                                    end_date=self.today + datetime.timedelta(days=1), price_per_night=D('5000'))
        self.assertIsNone(F.change_dates(b, self.user, b.check_in_date, self.today + datetime.timedelta(days=2)))
        b.refresh_from_db()
        self.assertEqual(b.total_room_charges, D('9000.00'))
        self.book(room=self.r101, start=self.today + datetime.timedelta(days=3), nights=1, name='Next guest')
        self.assertIn('already booked', F.change_dates(b, self.user, b.check_in_date, self.today + datetime.timedelta(days=4)))
        self.assertIsNone(F.change_dates(b, self.user, b.check_in_date, self.today + datetime.timedelta(days=1)))
        b.refresh_from_db()
        self.assertEqual(b.total_room_charges, D('4500.00'))  # one night removed at the stay's average

    def test_room_move_in_house(self):
        b = self.book()
        F.check_in(b, self.user)
        self.assertIsNone(F.move_room(b, self.user, self.r102))
        self.r101.refresh_from_db()
        self.r102.refresh_from_db()
        self.assertEqual((self.r101.status, self.r102.status), ('CLEANING', 'OCCUPIED'))
        self.assertIn('Moved from room 101', RoomBooking.objects.get(pk=b.pk).internal_notes)

    def test_no_show_with_first_night_charge(self):
        b = self.book(start=self.today - datetime.timedelta(days=1))
        self.assertIsNone(F.mark_no_show(b, self.user, charge_first_night=True))
        b.refresh_from_db()
        self.assertEqual(b.booking_status, 'NO_SHOW')
        self.assertEqual(F.Folio(b).total, D('4480.00'))

    def test_get_on_state_change_urls_does_not_change_state(self):
        b = self.book()
        self.client.get(self.url('booking_check_in', booking_id=b.pk))
        b.refresh_from_db()
        self.assertEqual(b.booking_status, 'RESERVED')


class ScreensTests(PMSTestBase):
    def test_booking_page_and_actions(self):
        b = self.book()
        page = self.client.get(self.url('booking_detail', booking_id=b.pk))
        self.assertContains(page, 'Post charge')
        self.assertContains(page, 'Guest registration incomplete')
        self.client.post(self.url('booking_charge_add', booking_id=b.pk), {'kind': 'ROOM_SERVICE', 'description': 'Tea', 'unit_price': '100', 'quantity': '2', 'tax_percent': '5'})
        self.client.post(self.url('booking_payment_add', booking_id=b.pk), {'kind': 'PAYMENT', 'amount': '1000', 'method': 'UPI', 'reference': 'UTR1'})
        resp = self.client.post(self.url('booking_guest_update', booking_id=b.pk), {'guest_name': 'Asha Rao', 'company_gstin': 'BAD'})
        self.assertEqual(FolioCharge.objects.get(booking=b).total, D('210.00'))
        self.assertEqual(FolioPayment.objects.get(booking=b).reference, 'UTR1')
        b.refresh_from_db()
        self.assertEqual(b.company_gstin, '')
        self.client.post(self.url('booking_guest_update', booking_id=b.pk), {'guest_name': 'Asha Rao', 'company_gstin': '27aapfu0939f1zv', 'guest_phone': '98765 43210',
                                                                            'id_type': 'PASSPORT', 'id_number': 'P1234567', 'nationality': 'Indian', 'adults': '2'})
        b.refresh_from_db()
        self.assertEqual((b.company_gstin, b.id_type, b.adults), ('27AAPFU0939F1ZV', 'PASSPORT', 2))
        inv = self.client.get(self.url('booking_invoice', booking_id=b.pk))
        self.assertContains(inv, 'Proforma bill')
        self.assertContains(inv, '27AAPFU0939F1ZV')
        self.assertContains(self.client.get(self.url('booking_registration_card', booking_id=b.pk)), 'Guest registration card')

    def test_other_branch_booking_is_hidden(self):
        other_b = RestaurantBranch.objects.create(restaurant=self.r, name='Hill', slug='hill', has_hotel_module=True)
        other_cat = RoomCategory.objects.create(branch=other_b, name='X', base_price_per_night=D('1000'))
        room = Room.objects.create(branch=other_b, category=other_cat, room_number='9')
        theirs = self.book(room=room)
        self.assertEqual(self.client.get(self.url('booking_detail', booking_id=theirs.pk)).status_code, 404)

    def test_housekeeping_flow(self):
        b = self.book()
        F.check_in(b, self.user)
        F.check_out(b, self.user, allow_balance=True)
        task = HousekeepingTask.objects.get()
        self.assertContains(self.client.get(self.url('room_housekeeping')), 'Room 101')
        self.client.post(self.url('room_task_update', task_id=task.pk), {'op': 'start'})
        self.client.post(self.url('room_task_update', task_id=task.pk), {'op': 'done'})
        self.r101.refresh_from_db()
        self.assertEqual(self.r101.status, 'AVAILABLE')
        self.client.post(self.url('room_task_create'), {'room_id': [self.r102.pk, self.s201.pk], 'kind': 'DEEP_CLEAN'})
        self.assertEqual(HousekeepingTask.objects.filter(kind='DEEP_CLEAN').count(), 2)
        self.r102.refresh_from_db()
        self.assertEqual(self.r102.status, 'CLEANING')
        self.client.post(self.url('room_status_update', room_id=self.r102.pk), {'status': 'AVAILABLE'})
        self.assertEqual(HousekeepingTask.objects.get(room=self.r102).status, 'DONE')

    def test_maintenance_blocks_and_resolves(self):
        self.book(room=self.r102, start=self.today + datetime.timedelta(days=1))
        self.client.post(self.url('room_issue_create'), {'room_id': self.r102.pk, 'title': 'AC broken', 'priority': 'HIGH', 'out_of_order': '1',
                                                         'block_from': self.today.isoformat(),
                                                         'block_until': (self.today + datetime.timedelta(days=2)).isoformat()})
        self.r102.refresh_from_db()
        self.assertEqual(self.r102.status, 'MAINTENANCE')
        self.assertContains(self.client.get(self.url('room_maintenance')), 'Guests are booked into this room')
        issue = MaintenanceIssue.objects.get()
        self.client.post(self.url('room_issue_update', issue_id=issue.pk), {'op': 'resolve', 'resolution_note': 'Gas refilled', 'cost': '1500'})
        self.r102.refresh_from_db()
        self.assertEqual(self.r102.status, 'AVAILABLE')

    def test_rates_calendar_and_rule(self):
        self.client.post(self.url('rate_save'), {'category': [self.cat.pk, self.suite.pk], 'label': 'Peak', 'start_date': self.today.isoformat(),
                                                 'end_date': (self.today + datetime.timedelta(days=3)).isoformat(), 'price': '7000', 'min_stay': '2'})
        self.assertEqual(RateOverride.objects.count(), 2)
        page = self.client.get(self.url('rates'))
        self.assertContains(page, '₹7,000')
        self.assertContains(page, '2N min')
        resp = self.client.post(self.url('rate_save'), {'category': [self.cat.pk], 'start_date': self.today.isoformat(),
                                                        'end_date': self.today.isoformat()})
        self.assertEqual(RateOverride.objects.count(), 2)  # a rule that changes nothing is refused

    def test_reports_numbers_and_exports(self):
        y = self.today - datetime.timedelta(days=1)
        b = self.book(start=y, nights=1, status='CHECKED_OUT')
        F.post_charge(b, self.user, 'SPA', 'Massage', D('2000'), 1, 18, y)
        s = reports.summary(self.b, y, y)
        self.assertEqual((s['sold'], s['sellable']), (1, 3))
        self.assertEqual((s['room_revenue'], s['adr'], s['revpar']), (D('4000.00'), D('4000.00'), D('1333.33')))
        self.assertEqual(s['extras_revenue'], D('2000.00'))
        page = self.client.get(self.url('reports'), {'period': 'custom', 'from': y.isoformat(), 'to': y.isoformat()})
        self.assertContains(page, '33.3%')
        csv = self.client.get(self.url('reports'), {'period': '7d', 'export': 'csv'})
        self.assertEqual(csv['Content-Type'], 'text/csv')
        self.book(room=self.r102, nationality='French', id_type='PASSPORT', id_number='FR1')
        self.assertContains(self.client.get(self.url('reports'), {'tab': 'formc', 'period': 'next_30'}), 'French')
        self.assertContains(self.client.get(self.url('reports'), {'tab': 'lists'}), 'Asha Rao')

    def test_night_audit_flow(self):
        late = self.book(start=self.today, nights=1)
        page = self.client.get(self.url('night_audit'))
        self.assertContains(page, 'Arrivals not checked in')
        resp = self.client.post(self.url('night_audit_close'), {'business_date': self.today.isoformat(), 'mark_no_shows': '1', 'stayovers': '1'})
        late.refresh_from_db()
        self.assertEqual(late.booking_status, 'NO_SHOW')
        audit = NightAudit.objects.get()
        self.assertEqual(audit.summary['no_shows_marked'], 1)
        self.assertContains(self.client.get(self.url('night_audit')), 'is closed')
        self.assertEqual(self.client.get(self.url('night_audit_detail', audit_id=audit.pk)).status_code, 200)

    def test_night_audit_refuses_with_overdue_departure(self):
        b = self.book(start=self.today - datetime.timedelta(days=2), nights=1)
        F.check_in(b, self.user)
        self.client.post(self.url('night_audit_close'), {'business_date': self.today.isoformat()})
        self.assertFalse(NightAudit.objects.exists())

    def test_custom_role_permissions_cover_new_screens(self):
        from super_admin.permissions import area_for, is_mutating
        self.assertEqual(area_for('hotel_admin', 'booking_charge_add'), 'front_desk')
        self.assertEqual(area_for('hotel_admin', 'room_housekeeping'), 'rooms')
        self.assertEqual(area_for('hotel_admin', 'night_audit_close'), 'revenue')
        self.assertTrue(is_mutating('night_audit_close'))
        self.assertFalse(is_mutating('booking_invoice'))
