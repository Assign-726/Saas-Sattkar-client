"""API security regressions: tenant isolation, payment integrity, public look-ups and logins."""
import datetime
import hashlib
import hmac
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category
from core.models import GatewayPayment, MenuItem, Order, RestaurantSettings, SubCategory
from events.models import ClubEvent
from hotel.models import CabBooking, CabServicePackage, Room, RoomBooking, RoomCategory
from super_admin.models import CustomerInquiry, Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


def sign(secret, order_id, payment_id):
    return hmac.new(secret.encode(), f'{order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()


class Tenants(TestCase):
    """Two businesses, A and B, each with a hotel branch, a manager and some data."""

    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.today = timezone.localdate()
        self.a, self.ba, self.ua = self._tenant('alpha')
        self.b, self.bb, self.ub = self._tenant('bravo')

    def _tenant(self, slug):
        r = Restaurant.objects.create(name=slug.title(), slug=slug)
        b = RestaurantBranch.objects.create(restaurant=r, name=slug.title(), slug=f'{slug}-main', has_hotel_module=True,
                                            has_club_module=True)
        u = User.objects.create_user(f'{slug}_mgr', password='pw-123456', is_staff=True)
        RestaurantUser.objects.create(user=u, restaurant=r, role=RestaurantUser.Role.GENERAL_MANAGER)
        cat = RoomCategory.objects.create(branch=b, name='Deluxe', base_price_per_night=D('4000'))
        room = Room.objects.create(branch=b, category=cat, room_number='101')
        setattr(self, f'room_{slug}', room)
        return r, b, u

    def order(self, restaurant, **kw):
        return Order.objects.create(restaurant=restaurant, customer_name='Guest', phone='9876543210', total_amount=D('500'), **kw)

    def booking(self, branch, room, **kw):
        kw.setdefault('check_in_date', self.today)
        kw.setdefault('check_out_date', self.today + datetime.timedelta(days=1))
        return RoomBooking.objects.create(branch=branch, room=room, guest_name='Guest', guest_email='g@x.com',
                                          guest_phone='9876543210', **kw)


class TenantIsolationTests(Tenants):
    def test_staff_only_see_their_own_orders(self):
        mine, theirs = self.order(self.a), self.order(self.b)
        self.api.force_authenticate(self.ua)
        ids = [o['id'] for o in self.api.get('/api/orders/').json()]
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids)
        self.assertEqual(self.api.get(f'/api/orders/{theirs.id}/').status_code, 404)
        self.assertEqual(self.api.patch(f'/api/orders/{theirs.id}/', {'notes': 'x'}, format='json').status_code, 404)

    def test_inquiries_are_scoped(self):
        CustomerInquiry.objects.create(restaurant=self.b, name='Spy', email='s@x.com', message='hello there friend')
        self.api.force_authenticate(self.ua)
        self.assertEqual(self.api.get('/api/contact/').json(), [])

    def test_hotel_api_ignores_other_tenants_branch(self):
        self.booking(self.bb, self.room_bravo)
        self.api.force_authenticate(self.ua)
        resp = self.api.get('/api/hotel/bookings/', {'branch_id': self.bb.id})
        self.assertIn(resp.status_code, (403, 200))
        if resp.status_code == 200:
            self.assertEqual(resp.json(), [])

    def test_hotel_actions_on_other_tenants_booking_are_404(self):
        theirs = self.booking(self.bb, self.room_bravo)
        self.api.force_authenticate(self.ua)
        for action in ('check-in', 'check-out', 'cancel'):
            self.assertEqual(self.api.patch(f'/api/hotel/bookings/{theirs.id}/{action}/').status_code, 404)
        self.assertEqual(self.api.patch(f'/api/hotel/rooms/{self.room_bravo.id}/status/', {'status': 'MAINTENANCE'}).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.booking_status, RoomBooking.BookingStatus.RESERVED)

    def test_cannot_book_into_another_hotels_room(self):
        self.api.force_authenticate(self.ua)
        resp = self.api.post('/api/hotel/bookings/', {
            'room': self.room_bravo.id, 'guest_name': 'X', 'guest_email': 'x@x.com', 'guest_phone': '9999999999',
            'check_in_date': self.today.isoformat(), 'check_out_date': (self.today + datetime.timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(RoomBooking.objects.filter(room=self.room_bravo).exists())

    def test_waiter_cannot_use_hotel_api(self):
        waiter = User.objects.create_user('waiter', password='x', is_staff=True)
        RestaurantUser.objects.create(user=waiter, restaurant=self.a, role=RestaurantUser.Role.WAITER)
        self.api.force_authenticate(waiter)
        self.assertEqual(self.api.get('/api/hotel/bookings/').status_code, 403)

    def test_hotel_admin_pages_refuse_other_tenants(self):
        self.client.force_login(self.ua)
        self.assertEqual(self.client.get(f'/hotel-admin/{self.b.slug}/').status_code, 403)
        self.assertEqual(self.client.get(f'/hotel-admin/{self.a.slug}/').status_code, 200)

    def test_restaurant_dashboard_refuses_other_tenants(self):
        self.client.force_login(self.ua)
        self.assertEqual(self.client.get(f'/dashboard/{self.b.slug}/').status_code, 403)

    def test_cannot_add_tickets_to_another_venues_event(self):
        event = ClubEvent.objects.create(branch=self.bb, title='Night', start_datetime=timezone.now() + datetime.timedelta(days=3),
                                         end_datetime=timezone.now() + datetime.timedelta(days=3, hours=4))
        self.api.force_authenticate(self.ua)
        resp = self.api.post('/api/events/admin/tickets/', {'event': event.id, 'name': 'VIP', 'base_price': '100',
                                                            'total_allocated': 10, 'branch_id': self.ba.id}, format='json')
        self.assertIn(resp.status_code, (400, 403))
        self.assertFalse(event.ticket_types.exists())


class OrderPaymentTests(Tenants):
    def test_guest_cannot_settle_their_own_order(self):
        o = self.order(self.a, payment_status='unpaid')
        resp = self.api.post(f'/api/orders/{o.security_token}/settle/', {'payment_method': 'cash'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.api.force_authenticate(self.ub)
        self.assertIn(self.api.post(f'/api/orders/{o.id}/settle/', format='json').status_code, (403, 404))
        o.refresh_from_db()
        self.assertEqual(o.payment_status, 'unpaid')
        self.api.force_authenticate(self.ua)
        self.assertEqual(self.api.post(f'/api/orders/{o.id}/settle/', {'payment_method': 'upi'}, format='json').status_code, 200)
        o.refresh_from_db()
        self.assertEqual((o.payment_status, o.payment_method), ('paid', 'upi'))

    def test_service_charge_needs_the_order_token(self):
        o = self.order(self.a, payment_status='unpaid')
        self.assertEqual(self.api.post(f'/api/orders/{o.id}/toggle-service-charge/', {'waive': True}, format='json').status_code, 403)
        self.assertEqual(self.api.post(f'/api/orders/{o.security_token}/toggle-service-charge/', {'waive': True}, format='json').status_code, 200)

    def test_bad_signature_cannot_mark_a_paid_order_failed(self):
        o = self.order(self.a, payment_status='paid', payment_method='O', razorpay_order_id='order_1', razorpay_payment_id='pay_1')
        resp = self.api.post(f'/api/orders/{o.id}/verify-payment/', {'razorpay_payment_id': 'pay_x', 'razorpay_signature': 'junk'}, format='json')
        self.assertEqual(resp.status_code, 409)
        o.refresh_from_db()
        self.assertEqual(o.payment_status, 'paid')

    def test_verify_payment_rejects_bad_signature_and_replays(self):
        s = RestaurantSettings.get_settings(self.a)
        s.razorpay_key_secret = 'sek'
        s.save()
        o1 = self.order(self.a, payment_status='unpaid', payment_method='O', razorpay_order_id='order_1')
        o2 = self.order(self.a, payment_status='unpaid', payment_method='O', razorpay_order_id='order_2')
        bad = self.api.post(f'/api/orders/{o1.id}/verify-payment/', {'razorpay_payment_id': 'pay_1', 'razorpay_signature': 'junk'}, format='json')
        self.assertEqual(bad.status_code, 400)
        ok = self.api.post(f'/api/orders/{o1.id}/verify-payment/',
                           {'razorpay_payment_id': 'pay_1', 'razorpay_signature': sign('sek', 'order_1', 'pay_1')}, format='json')
        self.assertEqual(ok.status_code, 200)
        GatewayPayment.objects.filter(payment_id='pay_2').delete()
        GatewayPayment.claim('pay_2', 'order', o1.pk)  # pay_2 already spent on o1
        replay = self.api.post(f'/api/orders/{o2.id}/verify-payment/',
                               {'razorpay_payment_id': 'pay_2', 'razorpay_signature': sign('sek', 'order_2', 'pay_2')}, format='json')
        self.assertEqual(replay.status_code, 409)

    def test_listing_by_bare_ids_hides_contact_details_and_old_orders(self):
        recent, old = self.order(self.a), self.order(self.a)
        Order.objects.filter(pk=old.pk).update(order_time=timezone.now() - datetime.timedelta(days=3))
        data = self.api.get('/api/orders/', {'ids': f'{recent.id},{old.id}'}).json()
        self.assertEqual([o['id'] for o in data], [recent.id])
        self.assertNotIn('security_token', data[0])
        self.assertTrue(data[0]['phone'].endswith('3210') and '9876' not in data[0]['phone'])
        full = self.api.get('/api/orders/', {'tokens': recent.security_token}).json()
        self.assertEqual(full[0]['phone'], '9876543210')


class OnlineOrderAmountTests(TestCase):
    def setUp(self):
        cache.clear()
        self.r = Restaurant.objects.create(name='Cafe', slug='cafe')
        self.branch = RestaurantBranch.objects.create(restaurant=self.r, name='Cafe', slug='cafe-main',
                                                      enable_online_payment=True, enable_customer_ordering=True)
        cat = Category.objects.create(name='Mains', restaurant=self.r)
        sub = SubCategory.objects.create(name='Curry', category=cat)
        self.item = MenuItem.objects.create(name='Thali', price=D('500'), category=cat, subcategory=sub, restaurant=self.r,
                                            availability=True, publishing_channels=['qr', 'online'])
        s = RestaurantSettings.get_settings(self.r)
        s.enable_online_payment, s.razorpay_key_id, s.razorpay_key_secret = True, 'rzp_live_x', 'sek'
        s.save()
        self.api = APIClient()

    def place(self, qty, order_id, payment_id):
        return self.api.post('/api/orders/', {
            'customer_name': 'Asha', 'phone': '9999999999', 'payment_method': 'O', 'branch_id': self.branch.id,
            'items': [{'menu_item': self.item.id, 'quantity': qty}],
            'razorpay_order_id': order_id, 'razorpay_payment_id': payment_id,
            'razorpay_signature': sign('sek', order_id, payment_id),
        }, format='json')

    def test_underpaid_razorpay_order_is_rejected(self):
        with mock.patch('core.payments.fetch_order', return_value={'amount': 100, 'status': 'paid'}):
            resp = self.place(4, 'order_cheap', 'pay_cheap')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_payment_cannot_be_reused_for_a_second_order(self):
        with mock.patch('core.payments.fetch_order', return_value={'amount': 10_000_000, 'status': 'paid'}):
            first = self.place(1, 'order_a', 'pay_a')
            second = self.place(1, 'order_a', 'pay_a')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)

    def test_quantity_must_be_positive(self):
        resp = self.api.post('/api/orders/initiate-razorpay/', {'items': [{'menu_item': self.item.id, 'quantity': -5}]}, format='json')
        self.assertEqual(resp.status_code, 400)


class PublicHotelTests(Tenants):
    def setUp(self):
        super().setUp()
        from hotel.models import HotelSettings
        hs = HotelSettings.get_settings(self.ba)
        hs.razorpay_key_id, hs.razorpay_key_secret = 'rzp_live_x', 'hsek'
        hs.save()

    def test_verify_without_signature_does_not_mark_paid(self):
        bk = self.booking(self.ba, self.room_alpha, payment_method='ONLINE_RAZORPAY', razorpay_order_id='order_h1')
        resp = self.api.post('/api/rooms/verify-payment/', {'booking_id': bk.booking_id, 'razorpay_payment_id': 'pay_h1'}, format='json')
        self.assertEqual(resp.status_code, 400)
        bk.refresh_from_db()
        self.assertNotEqual(bk.payment_status, RoomBooking.PaymentStatus.PAID)

    def test_verify_uses_our_order_id_and_blocks_replays(self):
        bk1 = self.booking(self.ba, self.room_alpha, payment_method='ONLINE_RAZORPAY', razorpay_order_id='order_h1')
        # A valid signature for a *different* (cheap) Razorpay order must not confirm this booking.
        resp = self.api.post('/api/rooms/verify-payment/', {'booking_id': bk1.booking_id, 'razorpay_order_id': 'order_cheap',
                                                            'razorpay_payment_id': 'pay_c', 'razorpay_signature': sign('hsek', 'order_cheap', 'pay_c')}, format='json')
        self.assertEqual(resp.status_code, 400)
        ok = self.api.post('/api/rooms/verify-payment/', {'booking_id': bk1.booking_id, 'razorpay_payment_id': 'pay_h1',
                                                          'razorpay_signature': sign('hsek', 'order_h1', 'pay_h1')}, format='json')
        self.assertEqual(ok.status_code, 200, ok.content)
        bk1.refresh_from_db()
        self.assertEqual(bk1.payment_status, RoomBooking.PaymentStatus.PAID)

    def test_public_cancel_only_releases_fresh_online_checkouts(self):
        desk = self.booking(self.ba, self.room_alpha, payment_method='PAY_AT_DESK')
        self.assertEqual(self.api.post('/api/rooms/cancel-unpaid/', {'booking_id': desk.id}, format='json').status_code, 400)
        desk.refresh_from_db()
        self.assertEqual(desk.booking_status, RoomBooking.BookingStatus.RESERVED)
        web = self.booking(self.ba, self.room_alpha, payment_method='ONLINE_RAZORPAY',
                           check_in_date=self.today + datetime.timedelta(days=5), check_out_date=self.today + datetime.timedelta(days=6))
        self.assertEqual(self.api.post('/api/rooms/cancel-unpaid/', {'booking_id': web.booking_id}, format='json').status_code, 200)

    def test_cab_lookup_needs_the_booking_code(self):
        pkg = CabServicePackage.objects.create(branch=self.ba, title='Airport', service_type='AIRPORT_TRANSFER', base_price=D('900'))
        cab = CabBooking.objects.create(branch=self.ba, package=pkg, guest_name='Guest', guest_phone='9999999999',
                                        pickup_location='A', drop_location='B', pickup_datetime=timezone.now())
        self.assertEqual(self.api.get(f'/api/cabs/bookings/{cab.id}/').status_code, 404)
        self.assertEqual(self.api.get(f'/api/cabs/bookings/{cab.booking_id}/').status_code, 200)


class LoginLockoutTests(Tenants):
    def test_jwt_login_locks_after_repeated_failures(self):
        for _ in range(8):
            self.api.post('/api/token/', {'username': 'alpha_mgr', 'password': 'wrong'}, format='json', REMOTE_ADDR=f'10.0.0.{_}')
        resp = self.api.post('/api/token/', {'username': 'alpha_mgr', 'password': 'pw-123456'}, format='json', REMOTE_ADDR='10.0.1.1')
        self.assertEqual(resp.status_code, 429)

    def test_successful_login_still_works_and_clears_failures(self):
        self.api.post('/api/token/', {'username': 'alpha_mgr', 'password': 'wrong'}, format='json')
        resp = self.api.post('/api/token/', {'username': 'alpha_mgr', 'password': 'pw-123456'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.json())

    def test_hotel_login_locks_too(self):
        for _ in range(8):
            self.api.post('/api/hotel/auth/login/', {'username': 'bravo_mgr', 'password': 'nope'}, format='json', REMOTE_ADDR=f'10.0.2.{_}')
        resp = self.api.post('/api/hotel/auth/login/', {'username': 'bravo_mgr', 'password': 'pw-123456'}, format='json', REMOTE_ADDR='10.0.3.1')
        self.assertEqual(resp.status_code, 429)


class TenantSlugGuardTests(TestCase):
    """/dashboard/<slug>/ and /hotel-admin/<slug>/ only open your own business."""

    def setUp(self):
        from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
        self.a = Restaurant.objects.create(name='Alpha', slug='alpha-co')
        self.a1 = RestaurantBranch.objects.create(restaurant=self.a, name='A1', slug='alpha-one', has_hotel_module=True)
        self.a2 = RestaurantBranch.objects.create(restaurant=self.a, name='A2', slug='alpha-two', has_hotel_module=True)
        self.b = Restaurant.objects.create(name='Beta', slug='beta-co')
        RestaurantBranch.objects.create(restaurant=self.b, name='B1', slug='beta-one', has_hotel_module=True)
        self.owner = User.objects.create_user('alpha_owner', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.owner, restaurant=self.a, role='GENERAL_MANAGER', is_primary_admin=True)
        self.desk = User.objects.create_user('alpha_desk', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.desk, restaurant=self.a, branch=self.a1, role='CASHIER')

    def test_other_business_is_refused(self):
        self.client.force_login(self.owner)
        for url in ['/dashboard/beta-co/menu/', '/dashboard/beta-one/floor-plan/', '/hotel-admin/beta-one/',
                    '/dashboard/beta-co/api/floor-plan/save-position/']:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertNotEqual(self.client.get('/dashboard/alpha-co/menu/').status_code, 403)
        self.assertNotEqual(self.client.get('/dashboard/alpha-two/menu/').status_code, 403)

    def test_branch_bound_staff_stay_on_their_branch(self):
        self.client.force_login(self.desk)
        self.assertEqual(self.client.get('/dashboard/alpha-two/orders/create/').status_code, 403)
        self.assertNotEqual(self.client.get('/dashboard/alpha-one/orders/create/').status_code, 403)

    def test_platform_admin_may_open_any_business(self):
        admin = User.objects.create_superuser('platform', 'p@x.com', 'x')
        self.client.force_login(admin)
        self.assertEqual(self.client.get('/dashboard/beta-co/menu/').status_code, 200)

    @override_settings(SSE_STREAMS='on')
    def test_live_order_stream_accepts_branch_slugs(self):
        """POS/KDS/floor plan links use the branch slug; the live stream must accept it."""
        self.client.force_login(self.owner)
        for slug in ('alpha-co', 'alpha-one'):
            r = self.client.get(f'/dashboard/{slug}/orders/stream/')
            self.assertEqual(r.status_code, 200, slug)  # not consumed: closing a stream ends the test DB connection
        self.assertEqual(self.client.get('/dashboard/beta-one/orders/stream/').status_code, 403)
