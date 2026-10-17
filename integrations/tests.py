import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import MenuItem, Order
from hotel.models import Room, RoomBooking, RoomCategory
from integrations import dispatch
from integrations.models import ApiKey, Connection, ExternalOrderLink, WebhookDelivery, WebhookEndpoint
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class Fixture(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.b1 = RestaurantBranch.objects.create(restaurant=self.r, name='Koramangala')
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='Indiranagar')
        self.paneer = MenuItem.objects.create(restaurant=self.r, branch=self.b1, name='Paneer Tikka', price=Decimal('300'), sku='PT-1')
        self.naan = MenuItem.objects.create(restaurant=self.r, branch=self.b1, name='Garlic Naan', price=Decimal('60'), markup_price=Decimal('75'))
        self.other_branch_item = MenuItem.objects.create(restaurant=self.r, branch=self.b2, name='Dosa', price=Decimal('120'))
        self.swiggy = Connection.objects.create(restaurant=self.r, branch=self.b1, provider='swiggy', commission_pct=Decimal('20'))
        self.key, self.raw = ApiKey.issue(restaurant=self.r, branch=self.b1, name='Swiggy', connection=self.swiggy,
                                          scopes=['menu:read', 'menu:write', 'orders:read', 'orders:write'])

    def api(self, method, path, body=None, raw=None):
        kw = {'HTTP_AUTHORIZATION': f'Bearer {raw or self.raw}'}
        if body is not None:
            kw.update(data=json.dumps(body), content_type='application/json')
        return getattr(self.client, method)('/api/v1/' + path, **kw)

    def order_body(self, **extra):
        body = {'external_id': 'SWG-1', 'customer': {'name': 'Anita', 'phone': '9876543210'},
                'items': [{'sku': 'PT-1', 'quantity': 2}, {'name': 'garlic naan', 'quantity': 3}], 'payment': {'status': 'paid'}}
        body.update(extra)
        return body


class ApiAuthTests(Fixture):
    def test_missing_or_bad_key(self):
        self.assertEqual(self.client.get('/api/v1/menu').status_code, 401)
        self.assertEqual(self.api('get', 'menu', raw='hk_live_nope').status_code, 401)

    def test_revoked_and_expired_keys_stop_working(self):
        self.key.revoked_at = timezone.now()
        self.key.save()
        self.assertEqual(self.api('get', 'menu').status_code, 401)
        key, raw = ApiKey.issue(restaurant=self.r, branch=self.b1, name='old', scopes=['menu:read'], expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.api('get', 'menu', raw=raw).status_code, 401)

    def test_scope_is_enforced(self):
        _, raw = ApiKey.issue(restaurant=self.r, branch=self.b1, name='read only', scopes=['menu:read'])
        resp = self.api('post', 'orders', self.order_body(), raw=raw)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['error']['code'], 'insufficient_scope')

    def test_menu_is_scoped_to_the_keys_branch(self):
        names = {i['name'] for i in self.api('get', 'menu').json()['items']}
        self.assertIn('Paneer Tikka', names)
        self.assertNotIn('Dosa', names)
        self.assertEqual(self.api('patch', f'menu/items/{self.other_branch_item.pk}', {'available': False}).status_code, 404)

    def test_secret_is_hashed(self):
        self.assertNotIn(self.raw, self.key.key_hash)
        self.assertTrue(self.raw.startswith('hk_live_'))

    @override_settings(API_RATE_LIMIT_PER_MINUTE=2)
    def test_rate_limit(self):
        from integrations import api
        with mock.patch.object(api, 'RATE_LIMIT', 2):
            codes = [self.api('get', 'branch').status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class OrderApiTests(Fixture):
    def test_push_order_creates_pos_order_with_commission(self):
        resp = self.api('post', 'orders', self.order_body())
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()['order']
        order = Order.objects.get(pk=data['id'])
        self.assertEqual(order.branch, self.b1)
        self.assertEqual(order.status, 'P')
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.items.get(menu_item=self.naan).unit_price, Decimal('75'))  # aggregator price
        self.assertEqual(data['source'], 'swiggy')
        link = ExternalOrderLink.objects.get(order=order)
        self.assertEqual(link.commission_amount, (order.total_amount * Decimal('0.2')).quantize(Decimal('0.01')))

    def test_retry_with_same_external_id_is_idempotent(self):
        first = self.api('post', 'orders', self.order_body())
        again = self.api('post', 'orders', self.order_body())
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(first.json()['order']['id'], again.json()['order']['id'])
        self.assertEqual(Order.objects.filter(restaurant=self.r).count(), 1)

    def test_unknown_and_other_branch_items_are_rejected(self):
        resp = self.api('post', 'orders', self.order_body(items=[{'name': 'Dosa', 'quantity': 1}]))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()['error']['code'], 'unknown_items')
        self.assertFalse(Order.objects.exists())

    def test_unavailable_item_conflict(self):
        self.paneer.availability = False
        self.paneer.save()
        self.assertEqual(self.api('post', 'orders', self.order_body()).status_code, 409)

    def test_status_updates_and_closed_orders(self):
        oid = self.api('post', 'orders', self.order_body()).json()['order']['id']
        self.assertEqual(self.api('post', f'orders/{oid}/status', {'status': 'ready'}).json()['order']['status'], 'ready')
        self.api('post', f'orders/{oid}/status', {'status': 'cancelled', 'reason': 'Rider unavailable'})
        self.assertEqual(self.api('post', f'orders/{oid}/status', {'status': 'preparing'}).status_code, 409)
        self.assertIn('Rider unavailable', Order.objects.get(pk=oid).notes)

    def test_other_branch_key_cannot_see_order(self):
        oid = self.api('post', 'orders', self.order_body()).json()['order']['id']
        _, raw2 = ApiKey.issue(restaurant=self.r, branch=self.b2, name='b2', scopes=['orders:read'])
        self.assertEqual(self.api('get', f'orders/{oid}', raw=raw2).status_code, 404)
        self.assertEqual(self.api('get', 'orders', raw=raw2).json()['count'], 0)

    def test_menu_availability_toggle(self):
        resp = self.api('patch', f'menu/items/{self.paneer.pk}', {'available': False})
        self.assertEqual(resp.status_code, 200)
        self.paneer.refresh_from_db()
        self.assertFalse(self.paneer.availability)


@override_settings(WEBHOOKS_SYNC=True)
class WebhookTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ep = WebhookEndpoint.objects.create(restaurant=self.r, branch=self.b1, name='ERP', url='https://erp.example.com/hook',
                                                 events=['order.created', 'order.status_changed', 'menu.item_updated'])
        self.other = WebhookEndpoint.objects.create(restaurant=self.r, branch=self.b2, name='B2', url='https://b2.example.com/hook', events=['order.created'])
        patcher = mock.patch.object(dispatch, 'check_url', lambda url: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, status=200):
        resp = mock.Mock(status_code=status, text='ok')
        return mock.patch('requests.post', return_value=resp)

    def test_order_events_are_signed_and_branch_scoped(self):
        with self._post() as post, self.captureOnCommitCallbacks(execute=True):
            self.api('post', 'orders', self.order_body())
        # captureOnCommitCallbacks runs nested on_commit callbacks too
        self.assertTrue(post.called)
        urls = {c.args[0] for c in post.call_args_list}
        self.assertEqual(urls, {'https://erp.example.com/hook'})
        call = post.call_args_list[0]
        sig = call.kwargs['headers']['X-Webhook-Signature']
        self.assertTrue(dispatch.verify(self.ep.secret, sig, call.kwargs['data']))
        self.assertFalse(dispatch.verify('whsec_wrong', sig, call.kwargs['data']))
        body = json.loads(call.kwargs['data'])
        self.assertEqual(body['event'], 'order.created')
        self.assertEqual(body['branch']['id'], self.b1.id)
        self.assertEqual(len(body['data']['items']), 2)

    def test_status_change_event(self):
        with self._post(), self.captureOnCommitCallbacks(execute=True):
            oid = self.api('post', 'orders', self.order_body()).json()['order']['id']
        with self._post() as post, self.captureOnCommitCallbacks(execute=True):
            self.api('post', f'orders/{oid}/status', {'status': 'ready'})
        body = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(body['event'], 'order.status_changed')
        self.assertEqual(body['data']['previous_status'], 'placed')

    def test_failure_schedules_retry_then_gives_up_and_disables(self):
        self.ep.events = ['order.created']
        self.ep.save()
        WebhookEndpoint.AUTO_DISABLE_AFTER, saved = 3, WebhookEndpoint.AUTO_DISABLE_AFTER
        self.addCleanup(setattr, WebhookEndpoint, 'AUTO_DISABLE_AFTER', saved)
        with self._post(500), self.captureOnCommitCallbacks(execute=True):
            self.api('post', 'orders', self.order_body())
        d = WebhookDelivery.objects.get(endpoint=self.ep)
        self.assertEqual(d.status, 'FAILED')
        self.assertIsNotNone(d.next_attempt_at)
        WebhookDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now())
        with self._post(500):
            dispatch.retry_due()
            WebhookDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now())
            dispatch.retry_due()
        self.ep.refresh_from_db()
        self.assertFalse(self.ep.is_active)
        self.assertIn('failed deliveries', self.ep.disabled_reason)

    def test_menu_change_event(self):
        with self._post() as post, self.captureOnCommitCallbacks(execute=True):
            self.paneer.availability = False
            self.paneer.save()
        body = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(body['event'], 'menu.item_updated')
        self.assertFalse(body['data']['available'])

    def test_url_guard_blocks_private_addresses(self):
        mock.patch.stopall()
        with override_settings(DEBUG=False):
            for bad in ('http://example.com/x', 'https://127.0.0.1/x', 'https://10.0.0.5/x', 'ftp://example.com'):
                with self.assertRaises(dispatch.UnsafeURL, msg=bad):
                    dispatch.check_url(bad)


class BookingApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.cat = RoomCategory.objects.create(branch=self.b1, name='Deluxe King', base_price_per_night=Decimal('4000'))
        self.room = Room.objects.create(category=self.cat, branch=self.b1, room_number='101')
        _, self.hraw = ApiKey.issue(restaurant=self.r, branch=self.b1, name='STAAH', scopes=['rooms:read', 'bookings:read', 'bookings:write'])

    def body(self, **extra):
        b = {'external_id': 'BDC-1', 'room_type': 'deluxe king', 'check_in': '2026-12-24', 'check_out': '2026-12-26',
             'guest': {'name': 'Rahul', 'email': 'r@example.com'}, 'amount': {'total': 9800, 'taxes': 1050}}
        b.update(extra)
        return b

    def test_availability_booking_and_sold_out(self):
        avail = self.api('get', 'rooms/availability?check_in=2026-12-24&check_out=2026-12-26', raw=self.hraw).json()
        self.assertEqual(avail['room_types'][0]['available'], 1)
        resp = self.api('post', 'bookings', self.body(), raw=self.hraw)
        self.assertEqual(resp.status_code, 201, resp.content)
        b = RoomBooking.objects.get()
        self.assertEqual(b.grand_total, Decimal('9800'))
        self.assertTrue(self.api('post', 'bookings', self.body(), raw=self.hraw).json()['duplicate'])
        self.assertEqual(self.api('post', 'bookings', self.body(external_id='BDC-2', check_in='2026-12-25', check_out='2026-12-27'), raw=self.hraw).status_code, 409)
        cancel = self.api('post', f'bookings/{b.booking_id}/cancel', raw=self.hraw)
        self.assertEqual(cancel.status_code, 200, cancel.content)
        self.assertEqual(cancel.json()['booking']['status'], 'cancelled')
        self.assertEqual(self.api('post', 'bookings', self.body(external_id='BDC-3'), raw=self.hraw).status_code, 201)


class ScreenTests(Fixture):
    def setUp(self):
        super().setUp()
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.b2_admin = User.objects.create_user('b2', 'b2@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.b2_admin, restaurant=self.r, branch=self.b2, role='RESTAURANT_ADMIN')
        self.cashier = User.objects.create_user('cash', 'c@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.r, role='CASHIER')

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def test_pages_render(self):
        self.client.force_login(self.gm)
        ep = WebhookEndpoint.objects.create(restaurant=self.r, branch=None, name='All', url='https://x.example.com', events=['order.created'])
        for name, kw in [('integrations', {}), ('integration_keys', {}), ('integration_webhooks', {}), ('integration_docs', {}),
                         ('integration_webhook_detail', {'pk': ep.pk})]:
            self.assertEqual(self.client.get(self.url(name, **kw) + '?branch=all').status_code, 200, name)

    def test_cashier_is_blocked(self):
        self.client.force_login(self.cashier)
        self.assertIn(self.client.get(self.url('integrations')).status_code, (302, 403))

    def test_create_key_shows_secret_once(self):
        self.client.force_login(self.gm)
        self.client.post(self.url('integration_keys'), {'op': 'create', 'name': 'Website', 'branch': self.b2.pk, 'scopes': ['menu:read']})
        key = ApiKey.objects.get(name='Website')
        self.assertEqual(key.branch, self.b2)
        first = self.client.get(self.url('integration_keys') + '?branch=all').content.decode()
        self.assertIn('hk_live_', first)
        second = self.client.get(self.url('integration_keys') + '?branch=all').content.decode()
        self.assertNotIn('Copy it now', second)

    def test_branch_locked_admin_only_sees_own_branch(self):
        self.client.force_login(self.b2_admin)
        page = self.client.get(self.url('integration_keys')).content.decode()
        self.assertNotIn(self.key.prefix, page)  # the b1 key
        # cannot revoke b1's key or create one for b1
        self.assertEqual(self.client.post(self.url('integration_keys'), {'op': 'revoke', 'pk': self.key.pk}).status_code, 404)
        self.client.post(self.url('integration_keys'), {'op': 'create', 'name': 'Sneaky', 'branch': self.b1.pk, 'scopes': ['menu:read']})
        self.assertFalse(ApiKey.objects.filter(name='Sneaky').exists())

    def test_pausing_channel_revokes_its_keys(self):
        self.client.force_login(self.gm)
        self.client.post(self.url('integrations'), {'op': 'toggle_channel', 'pk': self.swiggy.pk})
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.revoked_at)

    def test_super_admin_page(self):
        su = User.objects.create_superuser('root', 'r@example.com', 'x')
        self.client.force_login(su)
        self.assertEqual(self.client.get(reverse('super_admin:integrations')).status_code, 200)
