"""Channel manager / OTA distribution, city ledger, group bookings and hotel notifications."""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hotel import folio as F
from hotel import groups as G
from hotel import ledger as L
from hotel.models import (HotelSettings, BookingGroup, CompanyAccount, FolioPayment, MaintenanceIssue, RateOverride, Room, RoomBooking,
                          RoomCategory)
from integrations import dispatch
from integrations.models import ApiKey, ChannelRoomMapping, Connection, ExternalBookingLink, WebhookDelivery, WebhookEndpoint
from notifications.models import Notification
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.today = timezone.localdate()
        self.r = Restaurant.objects.create(name='Hill Retreat', slug='hill-retreat')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Hill', slug='hill-retreat-main', has_hotel_module=True)
        self.dlx = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=D('4000'))
        self.ste = RoomCategory.objects.create(branch=self.b, name='Suite', base_price_per_night=D('9000'))
        self.rooms = [Room.objects.create(branch=self.b, category=self.dlx, room_number=str(101 + i)) for i in range(3)]
        self.suite = Room.objects.create(branch=self.b, category=self.ste, room_number='301')
        # These tests are about stays, not guest registration: switch the (default-on) ID rule off
        HotelSettings.objects.filter(pk=HotelSettings.get_settings(self.b).pk).update(require_id_at_check_in=False)
        self.user = User.objects.create_user('fo2', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role=RestaurantUser.Role.HOTEL_ADMIN)
        self.client.force_login(self.user)

    def url(self, name, **kw):
        return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def d(self, n):
        return self.today + datetime.timedelta(days=n)


class ChannelTests(Base):
    def setUp(self):
        super().setUp()
        self.conn = Connection.objects.create(restaurant=self.r, branch=self.b, provider='ota', name='Booking.com', commission_pct=D('18'))
        self.key, self.raw = ApiKey.issue(restaurant=self.r, branch=self.b, name='BDC', connection=self.conn,
                                          scopes=['rooms:read', 'bookings:read', 'bookings:write'])
        ChannelRoomMapping.objects.create(connection=self.conn, category=self.dlx, external_room_id='DLX', rate_adjust_pct=D('15'), allotment=2)

    def api(self, method, path, body=None, **params):
        h = {'HTTP_AUTHORIZATION': f'Bearer {self.raw}'}
        if method == 'get':
            return self.client.get(f'/api/v1/{path}', params, **h)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f'/api/v1/{path}', json.dumps(body or {}), content_type='application/json', **h)

    def test_rates_feed_applies_hotel_rules_and_channel_settings(self):
        RateOverride.objects.create(category=self.dlx, start_date=self.d(1), end_date=self.d(1), price_per_night=D('5000'), min_stay=2)
        RateOverride.objects.create(category=self.dlx, start_date=self.d(2), end_date=self.d(2), stop_sell=True)
        MaintenanceIssue.objects.create(branch=self.b, room=self.rooms[0], title='Leak', out_of_order=True, block_from=self.d(3), block_until=self.d(3))
        RoomBooking.objects.create(room=self.rooms[1], branch=self.b, guest_name='X', guest_email='x@x.com', guest_phone='1',
                                   check_in_date=self.d(3), check_out_date=self.d(4))
        data = self.api('get', 'rates', **{'from': self.today.isoformat(), 'to': self.d(4).isoformat()}).json()
        self.assertEqual([rt['name'] for rt in data['room_types']], ['Deluxe'])  # suite not mapped
        n = data['room_types'][0]['nights']
        self.assertEqual((n[0]['price'], n[0]['available']), ('4600', 2))       # +15%, allotment caps 3 free rooms at 2
        self.assertEqual((n[1]['price'], n[1]['min_stay']), ('5750', 2))
        self.assertTrue(n[2]['closed'])
        self.assertEqual(n[3]['available'], 1)                                    # one booked, one out of order
        self.assertEqual(data['room_types'][0]['external_room_id'], 'DLX')

    def test_availability_ignores_no_shows_and_honours_restrictions(self):
        RoomBooking.objects.create(room=self.rooms[0], branch=self.b, guest_name='Gone', guest_email='g@x.com', guest_phone='1',
                                   check_in_date=self.today, check_out_date=self.d(1), booking_status='NO_SHOW')
        d = self.api('get', 'rooms/availability', check_in=self.today.isoformat(), check_out=self.d(1).isoformat()).json()
        self.assertEqual(d['room_types'][0]['available'], 2)  # 3 free, allotment 2
        RateOverride.objects.create(category=self.dlx, start_date=self.today, end_date=self.today, min_stay=3)
        d = self.api('get', 'rooms/availability', check_in=self.today.isoformat(), check_out=self.d(1).isoformat()).json()
        self.assertEqual(d['room_types'][0]['available'], 0)
        self.assertIn('minimum stay', d['room_types'][0]['closed_reason'])

    def test_channel_booking_by_room_code_then_modify_and_cancel(self):
        body = {'external_id': 'BDC-1', 'external_room_id': 'DLX', 'check_in': self.d(1).isoformat(), 'check_out': self.d(3).isoformat(),
                'guest': {'name': 'Lena Fischer', 'phone': '+49 151'}, 'guests': 2, 'payment_status': 'paid',
                'amount': {'total': '10000', 'taxes': '1071.43'}}
        resp = self.api('post', 'bookings', body)
        self.assertEqual(resp.status_code, 201, resp.content)
        b = RoomBooking.objects.get(guest_name='Lena Fischer')
        self.assertEqual((b.source, b.adults, b.grand_total), ('OTA', 2, D('10000.00')))
        self.assertEqual(FolioPayment.objects.get(booking=b).method, 'OTA')
        self.assertEqual(ExternalBookingLink.objects.get(booking=b).commission_amount, D('1800.00'))
        self.assertTrue(Notification.objects.filter(kind='room_booking').exists())
        resp = self.api('post', f'bookings/{b.booking_id}/modify', {'check_in': self.d(1).isoformat(), 'check_out': self.d(4).isoformat()})
        self.assertEqual(resp.status_code, 200, resp.content)
        b.refresh_from_db()
        self.assertEqual(b.check_out_date, self.d(4))
        resp = self.api('post', f'bookings/{b.booking_id}/cancel', {'reason': 'Guest changed plans'})
        b.refresh_from_db()
        self.assertEqual((b.booking_status, b.cancellation_reason), ('CANCELLED', 'Guest changed plans'))

    def test_sold_out_alerts_staff(self):
        for room in self.rooms:
            RoomBooking.objects.create(room=room, branch=self.b, guest_name='Full', guest_email='f@x.com', guest_phone='1',
                                       check_in_date=self.d(1), check_out_date=self.d(2))
        resp = self.api('post', 'bookings', {'external_id': 'BDC-2', 'external_room_id': 'DLX', 'check_in': self.d(1).isoformat(),
                                             'check_out': self.d(2).isoformat(), 'guest': {'name': 'Late'}})
        self.assertEqual(resp.status_code, 409)
        n = Notification.objects.get(kind='room_alert')
        self.assertEqual(n.level, 'critical')

    def test_ari_changed_webhook(self):
        WebhookEndpoint.objects.create(restaurant=self.r, branch=self.b, name='CM', url='https://cm.example.com/hook', events=['ari.changed'])
        cache.clear()
        with mock.patch.object(dispatch, 'check_url', lambda url: None), mock.patch('requests.post', return_value=mock.Mock(status_code=200, text='ok')):
            with self.captureOnCommitCallbacks(execute=True):
                RateOverride.objects.create(category=self.dlx, start_date=self.d(5), end_date=self.d(7), price_per_night=D('4500'))
            with self.captureOnCommitCallbacks(execute=True):
                RoomBooking.objects.create(room=self.rooms[0], branch=self.b, guest_name='Walk', guest_email='w@x.com', guest_phone='1',
                                           check_in_date=self.today, check_out_date=self.d(1))
        events = list(WebhookDelivery.objects.filter(event='ari.changed').values_list('payload', flat=True))
        self.assertEqual(len(events), 2)
        reasons = sorted(e['data']['reason'] for e in events)
        self.assertEqual(reasons, ['availability', 'rates'])

    def test_distribution_screen_saves_mapping(self):
        page = self.client.get(self.url('integration_distribution'))
        self.assertContains(page, 'What Booking.com may sell')
        self.client.post(self.url('integration_distribution'), {'connection': self.conn.pk, f'c{self.ste.pk}_on': '1', f'c{self.ste.pk}_room': 'STE',
                                                                 f'c{self.ste.pk}_adjust': '10', f'c{self.ste.pk}_allotment': ''})
        m = ChannelRoomMapping.objects.get(connection=self.conn, category=self.ste)
        self.assertEqual((m.external_room_id, m.rate_adjust_pct, m.allotment), ('STE', D('10.00'), None))
        self.assertFalse(ChannelRoomMapping.objects.filter(category=self.dlx).exists())  # unticked → removed


class LedgerTests(Base):
    def setUp(self):
        super().setUp()
        self.acme = CompanyAccount.objects.create(restaurant=self.r, name='Acme Travels', gstin='27AAPFU0939F1ZV', credit_limit=D('15000'), payment_terms_days=15)

    def stay(self, room, nights=1, start=0):
        b = RoomBooking.objects.create(room=room, branch=self.b, guest_name='Corp guest', guest_email='c@x.com', guest_phone='1',
                                       check_in_date=self.d(start), check_out_date=self.d(start + nights))
        F.check_in(b, self.user) if start <= 0 else None
        return b

    def test_bill_to_company_limit_settlement_and_aging(self):
        b1 = self.stay(self.rooms[0])
        pay, err = L.bill_to_company(b1, self.user, self.acme)
        self.assertIsNone(err)
        b1.refresh_from_db()
        self.assertEqual((F.Folio(b1).balance, b1.company_name, b1.payment_status), (D('0.00'), 'Acme Travels', 'PAID'))
        self.assertIsNone(F.check_out(b1, self.user))                      # nothing due any more
        b2 = self.stay(self.rooms[1], nights=3)
        _, err = L.bill_to_company(b2, self.user, self.acme)
        self.assertIn('credit limit', err)                                  # 4,480 + 13,440 > 15,000
        _, err = L.bill_to_company(b2, self.user, self.acme, override_limit=True)
        self.assertIsNone(err)
        self.assertEqual(L.balance(self.acme), D('17920.00'))
        # The first bill is 20 days old and the terms are 15 days
        FolioPayment.objects.filter(pk=pay.pk).update(received_at=timezone.now() - datetime.timedelta(days=20))
        ag = L.aging(self.acme)
        self.assertEqual(ag['overdue'], D('4480.00'))
        L.record_settlement(self.acme, self.user, D('5000'), 'BANK', 'UTR9')
        ag = L.aging(self.acme)
        self.assertEqual((ag['overdue'], ag['open']), (D('0'), D('12920.00')))   # oldest bill cleared first
        lines = L.statement(self.acme)
        self.assertEqual(lines[-1]['balance'], D('12920.00'))

    def test_screens_and_company_payment_from_folio(self):
        b = self.stay(self.rooms[0])
        self.client.post(self.url('booking_payment_add', booking_id=b.pk), {'kind': 'PAYMENT', 'method': 'COMPANY', 'amount': '2000',
                                                                             'company_account': self.acme.pk})
        self.assertEqual(L.balance(self.acme), D('2000.00'))
        self.assertContains(self.client.get(self.url('ledger')), 'Acme Travels')
        page = self.client.get(self.url('ledger_account', pk=self.acme.pk))
        self.assertContains(page, '₹2,000.00')
        self.client.post(self.url('ledger_payment_add', pk=self.acme.pk), {'amount': '2000', 'method': 'BANK', 'reference': 'NEFT1'})
        self.assertEqual(L.balance(self.acme), D('0.00'))
        self.client.post(self.url('ledger_account_save'), {'name': 'Globex', 'gstin': 'bad'})
        self.assertFalse(CompanyAccount.objects.filter(name='Globex').exists())
        self.client.post(self.url('ledger_account_save'), {'name': 'Globex', 'payment_terms_days': '45'})
        self.assertEqual(CompanyAccount.objects.get(name='Globex').payment_terms_days, 45)

    def test_other_business_account_rejected(self):
        other = Restaurant.objects.create(name='Other', slug='other-biz')
        theirs = CompanyAccount.objects.create(restaurant=other, name='Theirs')
        b = self.stay(self.rooms[0])
        _, err = L.bill_to_company(b, self.user, theirs)
        self.assertIn('another business', err)
        self.assertEqual(self.client.get(self.url('ledger_account', pk=theirs.pk)).status_code, 404)


class GroupTests(Base):
    def test_block_rooms_rooming_list_and_bulk_actions(self):
        acme = CompanyAccount.objects.create(restaurant=self.r, name='Acme Events')
        resp = self.client.post(self.url('booking_group_create'), {
            'name': 'Rao wedding', 'check_in_date': self.today.isoformat(), 'check_out_date': self.d(2).isoformat(),
            f'rooms_{self.dlx.pk}': '2', f'rooms_{self.ste.pk}': '1', 'negotiated_rate': '3500', 'company_account': acme.pk})
        g = BookingGroup.objects.get()
        rows = G.members(g)
        self.assertEqual(len(rows), 3)
        dlx = next(b for b in rows if b.room.category == self.dlx)
        self.assertEqual((dlx.total_room_charges, dlx.taxes), (D('7000.00'), D('840.00')))
        self.client.post(self.url('booking_group_update', pk=g.pk), {'op': 'rooming', f'name_{rows[0].pk}': 'Anil Rao', f'adults_{rows[0].pk}': '2'})
        rows[0].refresh_from_db()
        self.assertEqual((rows[0].guest_name, rows[0].adults), ('Anil Rao', 2))
        self.client.post(self.url('booking_group_update', pk=g.pk), {'op': 'checkin'})
        self.assertEqual(RoomBooking.objects.filter(group=g, booking_status='CHECKED_IN').count(), 3)
        self.client.post(self.url('booking_group_update', pk=g.pk), {'op': 'pay', 'amount': '5000', 'method': 'UPI'})
        self.assertEqual(sum(F.Folio(b).paid for b in G.members(g)), D('5000.00'))
        self.client.post(self.url('booking_group_update', pk=g.pk), {'op': 'checkout', 'bill_company': '1'})
        self.assertEqual(RoomBooking.objects.filter(group=g, booking_status='CHECKED_OUT').count(), 3)
        self.assertEqual(L.balance(acme), G.summary(g)['total'] - D('5000.00'))
        self.assertContains(self.client.get(self.url('booking_group_detail', pk=g.pk)), 'Anil Rao')

    def test_all_or_nothing_when_rooms_short(self):
        _, err = G.create_group(self.b, self.user, name='Tour', check_in=self.today, check_out=self.d(1), rooms_wanted=[(self.ste, 2)])
        self.assertIn('Only 1 Suite room is free', err)
        self.assertFalse(BookingGroup.objects.exists())
        self.assertFalse(RoomBooking.objects.exists())

    def test_cancel_group(self):
        g, _ = G.create_group(self.b, self.user, name='Conf', check_in=self.d(3), check_out=self.d(4), rooms_wanted=[(self.dlx, 2)])
        self.client.post(self.url('booking_group_update', pk=g.pk), {'op': 'cancel', 'reason': 'Event postponed'})
        self.assertEqual(set(RoomBooking.objects.filter(group=g).values_list('booking_status', flat=True)), {'CANCELLED'})


class HotelNotificationTests(Base):
    def test_hotel_drawer_shows_only_room_notifications(self):
        from notifications import service as S
        S.notify(self.r, 'room_booking', 'New OTA booking', branch=self.b)
        S.notify(self.r, 'order_new', 'New order', branch=self.b)
        items = self.client.get(self.url('notifications_list')).json()['items']
        self.assertEqual([i['title'] for i in items], ['New OTA booking'])
        page = self.client.get(self.url('dashboard'))
        self.assertContains(page, 'id="ntDrawer"')
        self.assertEqual(self.client.get(self.url('notifications_poll')).json()['unread'], 1)
