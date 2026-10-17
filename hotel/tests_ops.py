"""Guest services, banquets, sales, packages & promos, cashier, back office, action centre and forecast."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hotel import folio as F
from hotel import ops
from hotel.models import (BanquetEvent, BanquetMenu, BanquetVenue, CashierShift, CompanyAccount, ComplianceLicence, FolioCharge,
                          GuestRequest, LaundryOrder, LaundryRate, PaidOut, PromoCode, RateOverride, Room, RoomBooking, RoomCategory,
                          RoomPackage, SafetyCheck, SalesLead, SpaAppointment, SpaTherapist, SpaTreatment, StatutoryFiling,
                          UtilityReading)
from notifications.models import Notification
from super_admin.models import CustomRole, Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.today = timezone.localdate()
        self.r = Restaurant.objects.create(name='Lake Palace', slug='lake-palace')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Lake', slug='lake-palace-main', has_hotel_module=True)
        self.cat = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=D('4000'))
        self.rooms = [Room.objects.create(branch=self.b, category=self.cat, room_number=str(101 + i)) for i in range(4)]
        self.user = User.objects.create_user('ops', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role=RestaurantUser.Role.HOTEL_ADMIN)
        self.client.force_login(self.user)

    def stay(self, room=0, nights=2, status='CHECKED_IN', start=0, **kw):
        ci = self.today + datetime.timedelta(days=start)
        return RoomBooking.objects.create(room=self.rooms[room], branch=self.b, guest_name=kw.pop('guest_name', 'Asha Rao'),
                                          guest_email='a@x.com', guest_phone='9876543210', check_in_date=ci,
                                          check_out_date=ci + datetime.timedelta(days=nights), booking_status=status, **kw)

    def url(self, name, **kw):
        return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})


class GuestRequestTests(Base):
    def test_complaint_alerts_and_allowance_credits_the_folio(self):
        b = self.stay()
        before = F.Folio(b).balance
        req, err = ops.log_request(self.b, self.user, description='AC rattling all night', kind='COMPLAINT',
                                   department='ENGINEERING', priority='HIGH', booking=b)
        self.assertIsNone(err)
        self.assertEqual(req.sla_minutes, 10)  # high priority halves the 20-minute target
        self.assertEqual(req.room, b.room)
        self.assertTrue(Notification.objects.filter(kind='room_alert', branch=self.b).exists())
        self.assertIsNone(ops.resolve_request(req, self.user, 'Fan motor replaced', D('500')))
        req.refresh_from_db()
        self.assertEqual(req.status, 'RESOLVED')
        self.assertEqual(req.allowance.kind, FolioCharge.Kind.DISCOUNT)
        self.assertEqual(F.Folio(RoomBooking.objects.get(pk=b.pk)).balance, before - D('500'))

    def test_breach_is_measured_against_first_response(self):
        req, _ = ops.log_request(self.b, self.user, description='Towels', kind='REQUEST', department='HOUSEKEEPING', booking=self.stay())
        GuestRequest.objects.filter(pk=req.pk).update(created_at=timezone.now() - datetime.timedelta(minutes=45))
        req.refresh_from_db()
        self.assertTrue(req.is_breached)
        self.assertEqual(ops.resolve_request(req, self.user, ''), 'Say what was done to resolve it.')
        self.assertIn('allowance', ops.resolve_request(req, self.user, 'Delivered', 200))  # not a complaint

    def test_compliment_is_closed_immediately(self):
        req, _ = ops.log_request(self.b, self.user, description='Lovely staff', kind='COMPLIMENT', department='FRONT_OFFICE')
        self.assertEqual((req.status, req.sla_minutes, req.is_breached), ('RESOLVED', 0, False))


class LaundryTests(Base):
    def test_delivery_charges_the_room_with_express_rate(self):
        ops.seed_laundry_rates(self.b)
        shirt = LaundryRate.objects.get(branch=self.b, item='Shirt')
        b = self.stay()
        order, err = ops.create_laundry(self.b, self.user, b, {(shirt.pk, 'WASH'): 2, (shirt.pk, 'PRESS'): 1}, express=True)
        self.assertIsNone(err)
        self.assertEqual(order.amount, D('450.00'))  # (2×120 + 60) × 1.5
        for _ in range(3):
            self.assertIsNone(ops.advance_laundry(order, self.user))
        order.refresh_from_db()
        self.assertEqual(order.status, 'DELIVERED')
        self.assertEqual((order.charge.kind, order.charge.amount, order.charge.tax_percent), ('LAUNDRY', D('450.00'), D('18')))
        self.assertEqual(ops.advance_laundry(order, self.user), 'This order is already finished.')

    def test_no_order_for_checked_out_guest_or_empty_bag(self):
        ops.seed_laundry_rates(self.b)
        shirt = LaundryRate.objects.get(branch=self.b, item='Shirt')
        _, err = ops.create_laundry(self.b, self.user, self.stay(status='CHECKED_OUT'), {(shirt.pk, 'WASH'): 1})
        self.assertIn('booked or in house', err)
        _, err = ops.create_laundry(self.b, self.user, self.stay(room=1), {(shirt.pk, 'DRY_CLEAN'): 0})
        self.assertEqual(err, 'Enter at least one item.')


class SpaTests(Base):
    def setUp(self):
        super().setUp()
        self.t = SpaTherapist.objects.create(branch=self.b, name='Ritika')
        self.massage = SpaTreatment.objects.create(branch=self.b, name='Abhyanga', duration_minutes=60, price=D('3000'))
        self.day = self.today + datetime.timedelta(days=1)

    def book(self, at, **kw):
        return ops.book_spa(self.b, self.user, treatment=self.massage, therapist=self.t, date=self.day, start=at,
                            guest_name=kw.pop('guest_name', 'Walk-in'), **kw)

    def test_clash_and_opening_hours(self):
        a, err = self.book(datetime.time(10, 0))
        self.assertIsNone(err)
        _, err = self.book(datetime.time(10, 30))
        self.assertIn('Ritika is busy 10:00–11:00', err)
        _, err = self.book(datetime.time(20, 30))
        self.assertIn('would not finish', err)
        self.assertIsNone(ops.spa_status(a, 'CANCELLED'))
        _, err = self.book(datetime.time(10, 30))
        self.assertIsNone(err)  # cancelled slot is free again

    def test_charge_to_room_only_for_in_house_guests(self):
        walk_in, _ = self.book(datetime.time(11, 0))
        self.assertIn('in-house', ops.complete_spa(walk_in, self.user, 'ROOM'))
        self.assertIsNone(ops.complete_spa(walk_in, self.user, 'CASH'))
        guest, _ = self.book(datetime.time(13, 0), booking=self.stay())
        self.assertIsNone(ops.complete_spa(guest, self.user, 'ROOM'))
        guest.refresh_from_db()
        self.assertEqual((guest.status, guest.charge.kind, guest.charge.amount), ('COMPLETED', 'SPA', D('3000.00')))

    def test_schedule_grid_spans_rows(self):
        self.book(datetime.time(9, 0))
        rows = ops.spa_grid(list(SpaAppointment.objects.all()), [self.t])
        self.assertEqual(rows[0]['cells'][0]['span'], 2)
        self.assertTrue(rows[1]['cells'][0]['covered'])
        self.assertTrue(rows[2]['cells'][0]['free'])


class BanquetTests(Base):
    def setUp(self):
        super().setUp()
        self.hall = BanquetVenue.objects.create(branch=self.b, name='Durbar Hall', capacity=300, full_day_rate=D('100000'))
        self.menu = BanquetMenu.objects.create(branch=self.b, name='Veg feast', price_per_cover=D('1000'))
        self.day = self.today + datetime.timedelta(days=20)

    def event(self, **kw):
        defaults = dict(branch=self.b, name='Sangeet', client_name='Mehta', venue=self.hall, date=self.day, session='DINNER',
                        pax=200, menu=self.menu, decor=D('20000'))
        defaults.update(kw)
        return BanquetEvent.objects.create(**defaults)

    def test_pricing(self):
        e = self.event()
        self.assertEqual(e.hire, D('60000'))           # one session = 60% of the day rate
        self.assertEqual(e.subtotal, D('280000'))      # 60k + 200×1000 + 20k
        self.assertEqual(e.total, D('330400.00'))      # + 18% GST
        self.assertEqual(e.cgst + e.sgst, e.tax)

    def test_capacity_and_venue_clash(self):
        self.event(status='CONFIRMED')
        other = BanquetEvent(branch=self.b, name='Reception', client_name='Rao', venue=self.hall, date=self.day, session='FULL_DAY',
                             pax=100, status='TENTATIVE')
        self.assertIn('already held for “Sangeet”', ops.check_event(other))
        other.session = 'LUNCH'
        self.assertIsNone(ops.check_event(other))
        other.pax = 500
        self.assertIn('seats up to 300', ops.check_event(other))

    def test_advance_holds_venue_and_completion_invoices(self):
        e = self.event()
        p, err = ops.event_payment(e, self.user, '100000', 'BANK', 'UTR123')
        self.assertIsNone(err)
        e.refresh_from_db()
        self.assertEqual(e.status, 'TENTATIVE')
        self.assertEqual(e.balance, D('230400.00'))
        self.assertIn('Give a reason', self.client.post(self.url('banquet_action', pk=e.pk), {'op': 'cancel'}, follow=True).content.decode())
        self.assertIsNone(ops.set_event_status(e, self.user, 'CONFIRMED'))
        self.assertIsNone(ops.set_event_status(e, self.user, 'COMPLETED'))
        self.assertTrue(e.invoice_number)
        self.assertIn('already completed', ops.set_event_status(e, self.user, 'CANCELLED'))
        _, err = ops.event_payment(e, self.user, '999999', 'CASH', kind='REFUND')
        self.assertIn('can’t refund more', err)

    def test_beo_and_invoice_render(self):
        e = self.event(status='CONFIRMED')
        self.assertContains(self.client.get(self.url('banquet_beo', pk=e.pk)), 'Banquet event order')
        self.assertContains(self.client.get(self.url('banquet_beo', pk=e.pk) + '?doc=invoice'), 'Quotation')


class SalesTests(Base):
    def test_pipeline_and_win_creates_company_account(self):
        lead = SalesLead.objects.create(branch=self.b, company='Aravali Infotech', lead_type='CORPORATE', value=D('500000'))
        self.assertIsNone(ops.move_lead(lead, 1))
        self.assertEqual((lead.stage, lead.weighted), ('QUALIFIED', D('150000')))
        target, err = ops.win_lead(lead, self.user, 'account')
        self.assertIsNone(err)
        self.assertEqual(target, CompanyAccount.objects.get(restaurant=self.r, name='Aravali Infotech'))
        self.assertEqual(ops.lose_lead(lead, 'x'), 'This deal is already closed.')

    def test_win_wedding_creates_event_enquiry(self):
        BanquetVenue.objects.create(branch=self.b, name='Lawn', capacity=500, full_day_rate=D('150000'))
        lead = SalesLead.objects.create(branch=self.b, company='Kapoor wedding', lead_type='WEDDING', requirement='Feb, 400 guests')
        event, err = ops.win_lead(lead, self.user, 'event')
        self.assertIsNone(err)
        self.assertEqual((event.status, event.notes), ('ENQUIRY', 'Feb, 400 guests'))

    def test_lost_needs_reason(self):
        lead = SalesLead.objects.create(branch=self.b, company='X')
        self.assertEqual(ops.lose_lead(lead, ''), 'Say why the deal was lost.')
        self.assertIsNone(ops.lose_lead(lead, 'Price / budget'))


class OfferTests(Base):
    def test_package_posts_per_night(self):
        b = self.stay(nights=3)
        pkg = RoomPackage.objects.create(branch=self.b, name='Honeymoon', inclusions='Dinner', price_per_night=D('2500'), min_nights=2)
        charge, err = ops.apply_package(b, self.user, pkg)
        self.assertIsNone(err)
        self.assertEqual((charge.quantity, charge.amount), (D('3'), D('7500.00')))
        _, err = ops.apply_package(b, self.user, pkg)
        self.assertIn('already on the folio', err)
        short = self.stay(room=1, nights=1)
        self.assertIn('at least 2 nights', ops.apply_package(short, self.user, pkg)[1])

    def test_promo_code_rules(self):
        b = self.stay(nights=2)
        promo = PromoCode.objects.create(branch=self.b, code='DIRECT10', percent=D('10'), max_uses=1)
        charge, err = ops.apply_promo(b, self.user, 'direct10')
        self.assertIsNone(err)
        self.assertEqual(charge.amount, -(D(b.total_room_charges) * D('0.10')).quantize(D('0.01')))
        promo.refresh_from_db()
        self.assertEqual(promo.uses, 1)
        self.assertIn('usage limit', ops.apply_promo(self.stay(room=1), self.user, 'DIRECT10')[1])
        PromoCode.objects.create(branch=self.b, code='OLD', percent=D('5'), valid_until=self.today - datetime.timedelta(days=1))
        self.assertIn('expired', ops.apply_promo(self.stay(room=2), self.user, 'OLD')[1])
        self.assertIn('no promo code', ops.apply_promo(b, self.user, 'NOPE')[1])

    def test_folio_page_applies_offer(self):
        b = self.stay()
        PromoCode.objects.create(branch=self.b, code='SAVE5', percent=D('5'))
        r = self.client.post(self.url('booking_offer_add', booking_id=b.pk), {'promo': 'save5'}, follow=True)
        self.assertContains(r, 'Promo applied')


class CashierTests(Base):
    def test_shift_expected_cash_and_variance(self):
        shift = CashierShift.objects.create(branch=self.b, cashier=self.user, opening_float=D('5000'),
                                            opened_at=timezone.now() - datetime.timedelta(minutes=5))
        b = self.stay()
        F.record_payment(b, self.user, '3000', 'CASH')
        F.record_payment(b, self.user, '2000', 'UPI')
        PaidOut.objects.create(shift=shift, amount=D('500'), purpose='Courier')
        t = ops.shift_takings(shift)
        self.assertEqual(t['expected'], D('7500.00'))
        self.assertEqual(t['by_method']['UPI'], D('2000.00'))
        self.assertIsNone(ops.close_shift(shift, {'500': 15}))  # ₹7,500 counted
        self.assertEqual(shift.variance, D('0'))
        self.assertEqual(ops.close_shift(shift, {}), 'This shift is already closed.')

    def test_only_one_open_shift(self):
        self.client.post(self.url('cashier_open'), {'opening_float': '1000'})
        r = self.client.post(self.url('cashier_open'), {'opening_float': '1000'}, follow=True)
        self.assertContains(r, 'already open')
        self.assertEqual(CashierShift.objects.filter(branch=self.b).count(), 1)


class BackOfficeTests(Base):
    def test_seed_and_monthly_filings_are_idempotent(self):
        self.client.post(self.url('compliance_action'), {'op': 'seed'})
        self.assertEqual(ComplianceLicence.objects.filter(branch=self.b).count(), len(ops.STANDARD_LICENCES))
        self.assertEqual(ops.monthly_filings(self.b, self.today.replace(day=1)), 4)
        self.assertEqual(ops.monthly_filings(self.b, self.today.replace(day=1)), 0)

    def test_action_centre_flags_what_needs_attention(self):
        ComplianceLicence.objects.create(branch=self.b, name='Fire NOC', expires_on=self.today + datetime.timedelta(days=5))
        SafetyCheck.objects.create(branch=self.b, name='Fire drill', every_days=90)
        r = self.client.get(self.url('action_centre'))
        self.assertContains(r, 'Licences expired or expiring within 30 days')
        self.assertContains(r, 'Fire NOC')
        self.assertContains(r, 'Safety checks overdue')

    def test_energy_reading_counts_occupied_rooms(self):
        self.stay(room=0, start=-1)
        self.stay(room=1, start=-1)
        self.client.post(self.url('energy_save'), {'date': self.today.isoformat(), 'electricity_kwh': '120', 'water_kl': '3'})
        r = UtilityReading.objects.get(branch=self.b, date=self.today)
        self.assertEqual((r.occupied_rooms, r.kwh_per_room), (2, D('60.0')))

    def test_visitor_keeps_only_last_four_of_id(self):
        self.client.post(self.url('security_visitor_add'), {'name': 'Ravi', 'meeting': 'Room 101', 'id_number': '1234 5678 9012'})
        from hotel.models import Visitor
        self.assertEqual(Visitor.objects.get().id_last4, '9012')


class ForecastTests(Base):
    def test_on_the_books_and_applying_a_suggestion(self):
        for i in range(4):
            self.stay(room=i, status='RESERVED', start=3, nights=1)
        data = ops.forecast(self.b)
        night = data['rows'][3]
        self.assertEqual((night['otb'], night['pct'], night['change']), (4, 100, 15))
        self.client.post(self.url('rate_suggest_apply'), {'date': night['date'].isoformat(), 'change': '15'})
        o = RateOverride.objects.get(category=self.cat)
        self.assertEqual((o.start_date, o.price_per_night), (night['date'], D('4600')))


class PagesAndAccessTests(Base):
    PAGES = ['action_centre', 'requests', 'lostfound', 'laundry', 'spa', 'spa_setup', 'banquets', 'banquet_setup', 'sales',
             'packages', 'cashier', 'security', 'compliance', 'assets', 'energy', 'reports_forecast']

    def test_every_page_renders(self):
        BanquetVenue.objects.create(branch=self.b, name='Hall', capacity=100, full_day_rate=D('50000'))
        SpaTherapist.objects.create(branch=self.b, name='Anjali')
        SpaTreatment.objects.create(branch=self.b, name='Facial', price=D('2000'))
        ops.seed_laundry_rates(self.b)
        for name in self.PAGES:
            for q in ('', '?tab=calendar', '?tab=prices', '?tab=incidents', '?tab=filings', '?tab=safety', '?tab=contracts',
                      '?tab=activities', '?tab=leads', '?tab=resolved'):
                r = self.client.get(self.url(name) + q)
                self.assertEqual(r.status_code, 200, f'{name}{q}')
        lead = SalesLead.objects.create(branch=self.b, company='Acme')
        event = BanquetEvent.objects.create(branch=self.b, name='Gala', client_name='Acme', venue=BanquetVenue.objects.first(),
                                            date=self.today, pax=50)
        self.assertEqual(self.client.get(self.url('sales_lead', pk=lead.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.url('banquet_detail', pk=event.pk)).status_code, 200)

    def test_other_hotels_rows_are_404(self):
        other = RestaurantBranch.objects.create(restaurant=Restaurant.objects.create(name='B', slug='b'), name='B', slug='b-main',
                                                has_hotel_module=True)
        venue = BanquetVenue.objects.create(branch=other, name='Hall', capacity=100)
        event = BanquetEvent.objects.create(branch=other, name='X', client_name='Y', venue=venue, date=self.today, pax=10)
        self.assertEqual(self.client.get(self.url('banquet_detail', pk=event.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url('banquet_action', pk=event.pk), {'op': 'confirm'}).status_code, 404)

    def test_custom_role_without_guest_services_is_kept_out(self):
        role = CustomRole.objects.create(restaurant=self.r, name='Night auditor', permissions={'revenue': 'manage', 'front_desk': 'view'})
        profile = self.user.restaurant_profile
        profile.custom_role = role
        profile.save()
        r = self.client.get(self.url('laundry'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.post(self.url('request_add'), {'description': 'x'}).status_code, 403)
        # the action centre opens for everyone but only shows areas the role can use
        self.assertEqual(self.client.get(self.url('action_centre')).status_code, 200)


class ForecastPaceTests(Base):
    def test_no_history_means_no_invented_pickup(self):
        data = ops.forecast(self.b)
        self.assertTrue(all(r['pickup'] == 0 and r['pct'] == 0 for r in data['rows']))

    def test_pickup_follows_weekday_history_and_lead_time(self):
        # Every past night had 3 rooms sold, booked 10 days ahead
        for wk in range(1, 9):
            for i in range(3):
                b = self.stay(room=i, status='CHECKED_OUT', start=-7 * wk, nights=7)
                RoomBooking.objects.filter(pk=b.pk).update(created_at=timezone.now() - datetime.timedelta(days=7 * wk + 10))
        rows = ops.forecast(self.b)['rows']
        self.assertEqual(rows[2]['pickup'], 0)     # bookings arrive 10 days ahead, so none expected 2 days out
        self.assertEqual(rows[20]['pickup'], 3)    # 20 days out, the usual 3 rooms are still to come


class ProfitAndLossTests(Base):
    def test_banquets_and_direct_spa_count_as_revenue(self):
        from finance.pnl import compute_pnl
        hall = BanquetVenue.objects.create(branch=self.b, name='Hall', capacity=200, full_day_rate=D('50000'))
        BanquetEvent.objects.create(branch=self.b, name='Gala', client_name='Acme', venue=hall, date=self.today, session='FULL_DAY',
                                    pax=50, status='COMPLETED')
        t = SpaTherapist.objects.create(branch=self.b, name='A')
        tr = SpaTreatment.objects.create(branch=self.b, name='Facial', price=D('2000'))
        a, _ = ops.book_spa(self.b, self.user, treatment=tr, therapist=t, date=self.today, start=datetime.time(20, 0), guest_name='Walk-in')
        SpaAppointment.objects.filter(pk=a.pk).update(status='COMPLETED', settled_by='CASH')
        p = compute_pnl(self.r, self.today, self.today, module='hotel')
        lines = {l['key']: l['amount'] for l in p['revenue_lines']}
        self.assertEqual((lines['banquets'], lines['spa']), (D('50000'), D('2000')))
