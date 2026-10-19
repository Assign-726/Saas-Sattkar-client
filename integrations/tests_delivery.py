import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Category, MenuItem, Order
from integrations.delivery import service
from integrations.models import (Connection, ExternalOrderLink, MenuSyncJob, PlatformAccount, PlatformAction, PlatformEvent,
                                 PlatformMenuMapping)
from integrations.delivery import credentials as C
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


def order_payload(order_id='ZO-1', items=None, outlet='Z-100', **extra):
    return {
        'event': 'order.placed', 'event_id': f'evt-{order_id}', 'outlet_id': outlet,
        'order': {'id': order_id, 'payment_type': 'prepaid', 'customer': {'name': 'Aditi', 'phone': '+91 98XXXXXX10'},
                  'instructions': 'Less spicy',
                  'items': items or [{'item_id': 'ZPT', 'name': 'Paneer Tikka', 'quantity': 2, 'unit_price': '300.00'}],
                  'amounts': {'item_total': '600.00', 'tax_total': '30.00', 'discount_total': '50.00', 'merchant_discount': '20.00',
                              'packaging_fee': '20.00', 'customer_payable': '600.00'}, **extra},
    }


class DeliveryBase(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice Route', slug='spice-route', is_active=True)
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='MG Road', slug='mg-road')
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='Harbour', slug='harbour')
        self.admin = User.objects.create_user('dl_admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.admin, restaurant=self.r, role='RESTAURANT_ADMIN', branch=self.b)
        cat = Category.objects.create(restaurant=self.r, branch=self.b, name='Starters')
        self.item = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Paneer Tikka', price=Decimal('280'), sku='PT1', category=cat)
        self.zomato = Connection.objects.create(restaurant=self.r, branch=self.b, provider='zomato', external_store_id='Z-100',
                                                commission_pct=Decimal('20'))
        PlatformMenuMapping.objects.create(connection=self.zomato, menu_item=self.item, external_item_id='ZPT')

    def post_hook(self, conn, payload, secret=None, header=None):
        raw = json.dumps(payload).encode()
        sig = hmac.new((secret or conn.get_webhook_secret()).encode(), raw, hashlib.sha256).hexdigest()
        url = reverse('delivery_webhook', kwargs={'provider': conn.provider, 'public_id': conn.public_id})
        return self.client.post(url, raw, content_type='application/json', **{header or 'HTTP_X_SIGNATURE': sig})

    def place(self, order_id='ZO-1', **kw):
        resp = self.post_hook(self.zomato, order_payload(order_id, **kw))
        self.assertEqual(resp.status_code, 200, resp.content)
        return ExternalOrderLink.objects.filter(external_id=order_id).first()


class PipelineTests(DeliveryBase):
    def test_signed_order_creates_one_kitchen_order(self):
        link = self.place()
        self.assertEqual(link.platform_status, 'placed')
        o = link.order
        self.assertEqual((o.status, o.order_type, o.payment_status), ('P', 'delivery', 'paid'))
        self.assertEqual(o.total_amount, Decimal('600.00'))
        self.assertEqual(o.items.get().quantity, 2)
        # commission on food + packaging − restaurant-funded discount = 600 → 20% = 120
        self.assertEqual(link.commission_amount, Decimal('120.00'))
        self.assertEqual(link.expected_payout, Decimal('480.00'))
        self.assertTrue(link.accept_by)
        # replay of the same message is acknowledged but changes nothing
        resp = self.post_hook(self.zomato, order_payload('ZO-1'))
        self.assertEqual(resp.json()['duplicate'], True)
        self.assertEqual(Order.objects.filter(restaurant=self.r).count(), 1)

    def test_bad_signature_wrong_outlet_and_unknown_outlet(self):
        self.assertEqual(self.post_hook(self.zomato, order_payload(), secret='nope').status_code, 401)
        self.assertEqual(self.post_hook(self.zomato, order_payload(outlet='OTHER')).status_code, 422)
        url = reverse('delivery_webhook', kwargs={'provider': 'zomato', 'public_id': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(self.client.post(url, b'{}', content_type='application/json').status_code, 404)
        self.assertFalse(Order.objects.exists())

    def test_bearer_secret_is_accepted(self):
        raw = json.dumps(order_payload('ZO-9')).encode()
        url = reverse('delivery_webhook', kwargs={'provider': 'zomato', 'public_id': self.zomato.public_id})
        resp = self.client.post(url, raw, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.zomato.get_webhook_secret()}')
        self.assertEqual(resp.status_code, 200)

    def test_unknown_item_waits_for_mapping_then_creates_order(self):
        other = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Dal Makhani', price=Decimal('250'), sku='DM1')
        payload = order_payload('ZO-2', items=[{'item_id': 'ZDM', 'name': 'Dal Makhani', 'quantity': 1, 'unit_price': '250'}])
        self.post_hook(self.zomato, payload)
        ev = PlatformEvent.objects.get(external_order_id='ZO-2')
        self.assertEqual(ev.state, 'needs_mapping')
        self.assertFalse(ExternalOrderLink.objects.filter(external_id='ZO-2').exists())
        ev = service.map_unmapped(ev, {'ZDM': other.pk}, self.admin)
        self.assertEqual(ev.state, 'processed')
        self.assertTrue(ExternalOrderLink.objects.filter(external_id='ZO-2').exists())
        self.assertEqual(PlatformMenuMapping.objects.get(connection=self.zomato, menu_item=other).external_item_id, 'ZDM')

    def test_sku_works_as_item_id_but_names_alone_never_match(self):
        payload = order_payload('ZO-3', items=[{'item_id': 'PT1', 'name': 'x', 'quantity': 1, 'unit_price': '280'}])
        self.post_hook(self.zomato, payload)
        self.assertTrue(ExternalOrderLink.objects.filter(external_id='ZO-3').exists())
        payload = order_payload('ZO-4', items=[{'name': 'Paneer Tikka', 'quantity': 1, 'unit_price': '280'}])
        self.post_hook(self.zomato, payload)
        self.assertEqual(PlatformEvent.objects.get(external_order_id='ZO-4').state, 'needs_mapping')

    def test_platform_cancellation_and_out_of_order_updates(self):
        link = self.place()
        service.perform(link, 'accept', self.admin, prep_minutes=20)
        # a stale "accepted" after "ready" must not move the order back
        link.refresh_from_db()
        service.set_status(link, 'ready', 'staff')
        self.post_hook(self.zomato, {'event': 'order.status', 'event_id': 'e-old', 'order_id': 'ZO-1', 'status': 'accepted'})
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'ready')
        self.post_hook(self.zomato, {'event': 'order.cancelled', 'event_id': 'e-c', 'order_id': 'ZO-1', 'reason': 'Customer cancelled',
                                     'cancelled_by': 'customer'})
        link.refresh_from_db()
        self.assertEqual((link.platform_status, link.cancelled_by, link.order.status), ('cancelled', 'customer', 'C'))

    def test_rider_and_delivery_updates(self):
        link = self.place()
        service.perform(link, 'accept', self.admin)
        self.post_hook(self.zomato, {'event': 'rider.assigned', 'event_id': 'r1', 'order_id': 'ZO-1', 'rider': {'name': 'Imran', 'phone': '98'},
                                     'pickup_code': '4821'})
        self.post_hook(self.zomato, {'event': 'order.status', 'event_id': 'p1', 'order_id': 'ZO-1', 'status': 'picked_up'})
        self.post_hook(self.zomato, {'event': 'order.status', 'event_id': 'd1', 'order_id': 'ZO-1', 'status': 'delivered'})
        link.refresh_from_db()
        self.assertEqual((link.rider_name, link.pickup_code, link.platform_status, link.order.status), ('Imran', '4821', 'delivered', 'COMPLETED'))
        self.assertEqual(list(link.history.values_list('to_status', flat=True)), ['placed', 'accepted', 'picked_up', 'delivered'])


class ActionTests(DeliveryBase):
    def test_manual_mode_records_and_moves_the_order(self):
        link = self.place()
        ok, msg = service.perform(link, 'accept', self.admin, prep_minutes=25)
        link.refresh_from_db()
        self.assertTrue(ok)
        self.assertIn('partner app', msg)
        self.assertEqual((link.platform_status, link.prep_minutes, link.order.status), ('accepted', 25, 'PR'))
        self.assertEqual(PlatformAction.objects.get(action='accept').state, 'manual')
        self.assertEqual(service.perform(link, 'accept', self.admin)[0], False)  # already accepted

    def test_reject_needs_a_reason_and_restores_the_order(self):
        link = self.place()
        self.assertEqual(service.perform(link, 'reject', self.admin, reason='')[0], False)
        ok, _ = service.perform(link, 'reject', self.admin, reason='kitchen_busy')
        link.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual((link.platform_status, link.cancel_reason, link.order.status), ('rejected', 'Kitchen is too busy', 'C'))

    def api_mode(self):
        self.zomato.mode = 'api'
        self.zomato.api_base_url = 'https://partner.example.com/v1'
        self.zomato.api_paths = {'accept': '/orders/{order_id}/confirm', 'mark_ready': '/orders/{order_id}/ready'}
        self.zomato.set_credentials({'api_key': 'secret-token'})
        self.zomato.save()

    @override_settings(DEBUG=True)
    def test_api_mode_success_and_retry(self):
        self.api_mode()
        link = self.place()
        fail = mock.Mock(status_code=503, text='busy')
        with mock.patch('requests.post', return_value=fail) as post:
            ok, msg = service.perform(link, 'accept', self.admin)
        self.assertFalse(ok)
        url, = post.call_args.args
        self.assertEqual(url, 'https://partner.example.com/v1/orders/ZO-1/confirm')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer secret-token')
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'placed')  # never assumed done
        act = PlatformAction.objects.get(action='accept')
        self.assertEqual(act.state, 'retry')
        PlatformAction.objects.filter(pk=act.pk).update(next_attempt_at=act.created_at)
        good = mock.Mock(status_code=200, text='{"ok": true}', json=lambda: {'ok': True})
        with mock.patch('requests.post', return_value=good):
            service.run_jobs()
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'accepted')
        self.assertEqual(PlatformAction.objects.get(pk=act.pk).state, 'succeeded')

    @override_settings(DEBUG=True)
    def test_unconfigured_or_unsupported_action_updates_locally_only(self):
        self.api_mode()
        link = self.place()
        good = mock.Mock(status_code=200, text='{}', json=lambda: {})
        with mock.patch('requests.post', return_value=good):
            service.perform(link, 'accept', self.admin)
        with mock.patch('requests.post') as post:
            # Zomato riders mark pickup themselves; no endpoint configured either
            ok, msg = service.perform(ExternalOrderLink.objects.get(pk=link.pk), 'mark_picked_up', self.admin)
        post.assert_not_called()
        self.assertTrue(ok)
        self.assertIn('Updated here only', msg)

    def test_kitchen_ready_on_kds_tells_the_platform(self):
        link = self.place()
        service.perform(link, 'accept', self.admin)
        order = Order.objects.get(pk=link.order_id)
        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'READY'
            order.save()
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'ready')
        self.assertTrue(PlatformAction.objects.filter(action='mark_ready', link=link).exists())

    def test_switching_an_item_off_updates_listed_platforms(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.item.availability = False
            self.item.save()
        m = PlatformMenuMapping.objects.get(connection=self.zomato, menu_item=self.item)
        self.assertFalse(m.is_available)
        act = PlatformAction.objects.get(action='item_availability')
        self.assertEqual(act.payload, {'item_id': 'ZPT', 'available': False})

    def test_store_offline(self):
        ok, _ = service.set_store_open(self.zomato, False, self.admin)
        self.zomato.refresh_from_db()
        self.assertTrue(ok)
        self.assertFalse(self.zomato.is_store_open)


class MenuAndMoneyTests(DeliveryBase):
    def test_menu_sync_manual_exports_and_marks_synced(self):
        job, msg = service.sync_menu(self.zomato, self.admin)
        self.assertEqual(job.state, MenuSyncJob.State.MANUAL)
        self.assertEqual(job.menu_snapshot['items'][0]['item_id'], 'ZPT')
        self.assertEqual(PlatformMenuMapping.objects.get(connection=self.zomato).sync_status, 'synced')

    def test_menu_sync_validates_first(self):
        self.item.category = None
        self.item.save()
        job, msg = service.sync_menu(self.zomato, self.admin)
        self.assertEqual(job.state, MenuSyncJob.State.FAILED)
        self.assertIn('category', job.errors[0])

    def test_settlement_reconciliation(self):
        self.place('ZO-1')
        self.place('ZO-2')
        csv_text = 'Order ID,Order Value,Commission,TDS,Net Payout\nZO-1,600,118.00,6,480.00\nZO-2,600,130,6,450.00\nZO-404,100,10,0,90\n'
        batch = service.import_settlement(self.zomato, csv_text, file_name='payout.csv', reference='UTR1')
        self.assertEqual((batch.matched, batch.mismatched, batch.unmatched, batch.lines_count), (1, 1, 1, 3))
        self.assertEqual(batch.net_payout, Decimal('1020.00'))
        # the unknown order's ₹90 is not counted against what we expected for our own orders
        self.assertEqual((batch.known_paid, batch.expected_payout, batch.difference), (Decimal('930.00'), Decimal('960.00'), Decimal('-30.00')))
        l1 = ExternalOrderLink.objects.get(external_id='ZO-1')
        self.assertEqual((l1.settled_amount, l1.commission_amount), (Decimal('480.00'), Decimal('118.00')))
        with self.assertRaises(ValueError):
            service.import_settlement(self.zomato, 'foo,bar\n1,2\n')

    def test_credentials_are_encrypted(self):
        self.zomato.set_credentials({'api_key': 'plain-key-123'})
        self.zomato.save()
        raw = Connection.objects.filter(pk=self.zomato.pk).values_list('credentials_encrypted', flat=True).get()
        self.assertNotIn('plain-key-123', raw)
        self.assertEqual(Connection.objects.get(pk=self.zomato.pk).get_credentials()['api_key'], 'plain-key-123')


class ScreenTests(DeliveryBase):
    def url(self, name, platform='zomato', **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, 'platform': platform, **kw})

    def test_every_screen_renders(self):
        link = self.place()
        self.client.force_login(self.admin)
        for name in ['delivery_dashboard', 'delivery_orders', 'delivery_menu', 'delivery_payouts', 'delivery_logs', 'delivery_settings']:
            resp = self.client.get(self.url(name))
            self.assertEqual(resp.status_code, 200, name)
        self.assertContains(self.client.get(self.url('delivery_dashboard')), '#ZO-1')
        self.assertEqual(self.client.get(self.url('delivery_order_detail', pk=link.pk)).status_code, 200)
        live = self.client.get(self.url('delivery_live')).json()
        self.assertEqual(live['placed'], 1)
        self.assertEqual(self.client.get(self.url('delivery_orders') + '?export=csv')['Content-Type'], 'text/csv')
        self.assertEqual(self.client.get(self.url('delivery_menu_export') + '?format=json').status_code, 200)

    def test_swiggy_is_separate_and_connects_from_settings(self):
        self.place()
        self.client.force_login(self.admin)
        resp = self.client.get(self.url('delivery_dashboard', 'swiggy'))
        self.assertRedirects(resp, self.url('delivery_settings', 'swiggy'))
        resp = self.client.post(self.url('delivery_settings', 'swiggy'), {'external_store_id': 'SW-77', 'commission_pct': '22',
                                                                         'default_prep_minutes': '15', 'accept_window_seconds': '180',
                                                                         'is_active': '1', 'mode': 'manual', 'environment': 'sandbox',
                                                                         'api_key': 'k-1'})
        self.assertEqual(resp.status_code, 302)
        sw = Connection.objects.get(provider='swiggy', branch=self.b)
        self.assertEqual(sw.get_credentials()['access_token'], 'k-1')
        resp = self.client.get(self.url('delivery_dashboard', 'swiggy'))
        self.assertNotContains(resp, '#ZO-1')  # Zomato orders never show on the Swiggy board

    def test_accept_from_the_board(self):
        link = self.place()
        self.client.force_login(self.admin)
        resp = self.client.post(self.url('delivery_order_action', pk=link.pk), {'action': 'accept', 'prep_minutes': '20'})
        self.assertEqual(resp.status_code, 302)
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'accepted')

    def test_simulator_uses_the_real_pipeline(self):
        self.client.force_login(self.admin)
        self.client.post(self.url('delivery_simulate'), {'kind': 'order'})
        link = ExternalOrderLink.objects.get(connection=self.zomato)
        self.assertTrue(link.external_id.startswith('ZO-'))
        self.zomato.environment = 'live'
        self.zomato.save()
        self.client.post(self.url('delivery_simulate'), {'kind': 'order'})
        self.assertEqual(ExternalOrderLink.objects.filter(connection=self.zomato).count(), 1)

    def test_cashier_operates_but_cannot_configure(self):
        link = self.place()
        cashier = User.objects.create_user('dl_cash', password='x', is_staff=True)
        RestaurantUser.objects.create(user=cashier, restaurant=self.r, role='CASHIER', branch=self.b)
        self.client.force_login(cashier)
        self.assertEqual(self.client.get(self.url('delivery_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(self.url('delivery_settings')).status_code, 403)
        self.assertEqual(self.client.get(self.url('delivery_payouts')).status_code, 403)
        self.client.post(self.url('delivery_order_action', pk=link.pk), {'action': 'accept'})
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'accepted')

    def test_other_branch_cannot_see_the_order(self):
        link = self.place()
        other = User.objects.create_user('dl_other', password='x', is_staff=True)
        RestaurantUser.objects.create(user=other, restaurant=self.r, role='RESTAURANT_ADMIN', branch=self.b2)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url('delivery_order_detail', pk=link.pk)).status_code, 302)  # no connection here → setup
        Connection.objects.create(restaurant=self.r, branch=self.b2, provider='zomato', external_store_id='Z-200')
        self.assertEqual(self.client.get(self.url('delivery_order_detail', pk=link.pk)).status_code, 404)

    def test_unknown_platform_is_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url('delivery_dashboard', 'ubereats')).status_code, 404)


SATTKAR = {'zomato': {'client_id': '', 'client_secret': '', 'access_token': 'env-token', 'webhook_secret': 'env-hook',
                      'api_base_url': 'https://env.example.com/api', 'token_url': '', 'auth_header': '', 'signature_header': '',
                      'api_paths': {'accept': '/env/{order_id}/accept'}},
           'swiggy': {}}


class BrandCredentialTests(DeliveryBase):
    def brand(self, **secrets):
        acc = PlatformAccount.objects.create(restaurant=self.r, provider='zomato', api_base_url='https://brand.example.com/v2',
                                             token_url='https://brand.example.com/oauth/token', api_paths={'accept': '/orders/{order_id}/accept'})
        acc.set_secrets(secrets)
        acc.save()
        return acc

    def test_secrets_are_encrypted_at_rest(self):
        self.brand(client_id='cid-123', client_secret='very-secret-456', webhook_secret='hook-789')
        raw = PlatformAccount.objects.values_list('secrets_encrypted', flat=True).get()
        for plain in ('cid-123', 'very-secret-456', 'hook-789'):
            self.assertNotIn(plain, raw)
        stored = Connection.objects.filter(pk=self.zomato.pk).values_list('webhook_secret', flat=True).get()
        self.assertNotIn(self.zomato.get_webhook_secret(), stored)

    @override_settings(DELIVERY_PARTNERS=SATTKAR)
    def test_resolution_order_outlet_then_brand_then_env(self):
        conn = Connection.objects.get(pk=self.zomato.pk)
        r = C.resolve(conn)
        self.assertEqual((r.get('access_token'), r.sources['access_token']), ('env-token', 'sattkar'))
        self.assertEqual(r.paths['accept'], '/env/{order_id}/accept')
        self.brand(access_token='brand-token')
        conn = Connection.objects.get(pk=self.zomato.pk)
        r = C.resolve(conn)
        self.assertEqual((r.get('access_token'), r.sources['access_token'], r.get('api_base_url')), ('brand-token', 'brand', 'https://brand.example.com/v2'))
        self.assertEqual(r.paths['accept'], '/orders/{order_id}/accept')
        conn.set_credentials({'access_token': 'outlet-token'})
        conn.api_paths = {'accept': '/x/{order_id}'}
        conn.save()
        r = C.resolve(Connection.objects.get(pk=conn.pk))
        self.assertEqual((r.get('access_token'), r.sources['access_token'], r.paths['accept']), ('outlet-token', 'outlet', '/x/{order_id}'))

    @override_settings(DEBUG=True)
    def test_oauth_token_is_fetched_cached_and_refreshed_on_401(self):
        acc = self.brand(client_id='cid', client_secret='csecret')
        self.zomato.mode = 'api'
        self.zomato.save()
        link = self.place()
        token_resp = mock.Mock(status_code=200, json=lambda: {'access_token': 'tok-1', 'expires_in': 3600})
        ok_resp = mock.Mock(status_code=200, text='{}', json=lambda: {})
        with mock.patch('requests.post', side_effect=[token_resp, ok_resp]) as post:
            ok, _ = service.perform(link, 'accept', self.admin)
        self.assertTrue(ok)
        token_call, api_call = post.call_args_list
        self.assertEqual(token_call.args[0], 'https://brand.example.com/oauth/token')
        self.assertEqual(token_call.kwargs['data']['grant_type'], 'client_credentials')
        self.assertEqual(api_call.args[0], 'https://brand.example.com/v2/orders/ZO-1/accept')
        self.assertEqual(api_call.kwargs['headers']['Authorization'], 'Bearer tok-1')
        acc.refresh_from_db()
        self.assertNotIn('tok-1', acc.token_encrypted)
        self.assertEqual(acc.get_cached_token(), 'tok-1')
        # cached token reused; a 401 triggers one refresh and retry
        acc.api_paths = {'accept': '/orders/{order_id}/accept', 'mark_ready': '/orders/{order_id}/ready'}
        acc.save()
        expired = mock.Mock(status_code=401, text='expired')
        token2 = mock.Mock(status_code=200, json=lambda: {'access_token': 'tok-2', 'expires_in': 3600})
        with mock.patch('requests.post', side_effect=[expired, token2, ok_resp]) as post:
            ok, _ = service.perform(ExternalOrderLink.objects.get(pk=link.pk), 'mark_ready', self.admin)
        self.assertTrue(ok)
        self.assertEqual(post.call_args_list[0].kwargs['headers']['Authorization'], 'Bearer tok-1')
        self.assertEqual(post.call_args_list[2].kwargs['headers']['Authorization'], 'Bearer tok-2')

    @override_settings(DEBUG=True)
    def test_refused_client_secret_fails_cleanly(self):
        self.brand(client_id='cid', client_secret='wrong')
        self.zomato.mode = 'api'
        self.zomato.save()
        link = self.place()
        with mock.patch('requests.post', return_value=mock.Mock(status_code=401, text='bad')):
            ok, msg = service.perform(link, 'accept', self.admin)
        self.assertFalse(ok)
        self.assertIn('refused the client ID', msg)
        link.refresh_from_db()
        self.assertEqual(link.platform_status, 'placed')

    def shared(self, payload, secret):
        raw = json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(reverse('delivery_webhook_shared', kwargs={'provider': 'zomato'}), raw,
                                content_type='application/json', HTTP_X_SIGNATURE=sig)

    def test_shared_webhook_routes_by_outlet_and_accepts_brand_secret(self):
        self.brand(webhook_secret='brand-hook')
        resp = self.shared(order_payload('ZO-77'), 'brand-hook')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(ExternalOrderLink.objects.get(external_id='ZO-77').branch, self.b)
        self.assertEqual(self.shared(order_payload('ZO-78'), 'not-it').status_code, 401)
        self.assertEqual(self.shared(order_payload('ZO-79', outlet='NOPE'), 'brand-hook').status_code, 404)
        # status updates without an outlet ID are routed through the order
        resp = self.shared({'event': 'order.cancelled', 'event_id': 'c77', 'order_id': 'ZO-77', 'reason': 'x'}, 'brand-hook')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ExternalOrderLink.objects.get(external_id='ZO-77').platform_status, 'cancelled')

    @override_settings(DELIVERY_PARTNERS=SATTKAR)
    def test_sattkar_webhook_secret_is_accepted(self):
        self.assertEqual(self.shared(order_payload('ZO-80'), 'env-hook').status_code, 200)

    def settings_url(self):
        return reverse('restaurant_admin:delivery_settings', kwargs={'restaurant_slug': self.r.slug, 'platform': 'zomato'})

    @override_settings(DEBUG=True)
    def test_only_brand_admins_change_brand_keys(self):
        self.client.force_login(self.admin)  # restaurant admin locked to one branch
        resp = self.client.post(self.settings_url(), {'op': 'account', 'acc_access_token': 'x'})
        self.assertEqual(resp.status_code, 403)
        gm = User.objects.create_user('dl_gm', password='x', is_staff=True)
        RestaurantUser.objects.create(user=gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.client.force_login(gm)
        resp = self.client.post(self.settings_url(), {'op': 'account', 'acc_client_id': 'cid-1', 'acc_client_secret': 'sec-1',
                                                      'acc_api_base_url': 'https://brand.example.com', 'acc_path_accept': '/a/{order_id}'})
        self.assertEqual(resp.status_code, 302)
        acc = PlatformAccount.objects.get(restaurant=self.r, provider='zomato')
        self.assertEqual(acc.get_secrets(), {'client_id': 'cid-1', 'client_secret': 'sec-1'})
        # blank fields keep saved secrets; "remove" clears one
        self.client.post(self.settings_url(), {'op': 'account', 'acc_clear_client_secret': '1'})
        acc.refresh_from_db()
        self.assertEqual(acc.get_secrets(), {'client_id': 'cid-1'})
        page = self.client.get(self.settings_url())
        self.assertContains(page, 'Brand account')
        self.assertNotContains(page, 'sec-1')

    def test_platform_issued_outlet_secret_can_be_pasted(self):
        self.client.force_login(self.admin)
        self.client.post(self.settings_url(), {'op': 'save', 'external_store_id': 'Z-100', 'commission_pct': '20', 'default_prep_minutes': '15',
                                               'accept_window_seconds': '180', 'is_active': '1', 'mode': 'manual', 'environment': 'sandbox',
                                               'platform_webhook_secret': 'zomato-issued-secret'})
        conn = Connection.objects.get(pk=self.zomato.pk)
        self.assertEqual(conn.get_webhook_secret(), 'zomato-issued-secret')
        self.assertEqual(self.post_hook(conn, order_payload('ZO-81'), secret='zomato-issued-secret').status_code, 200)

    @override_settings(DEBUG=True)
    def test_test_connection(self):
        self.brand(client_id='cid', client_secret='sec')
        gm = User.objects.create_user('dl_gm2', password='x', is_staff=True)
        RestaurantUser.objects.create(user=gm, restaurant=self.r, role='GENERAL_MANAGER', branch=self.b)
        self.client.force_login(gm)
        with mock.patch('requests.post', return_value=mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 600})):
            self.client.post(self.settings_url(), {'op': 'test'})
        acc = PlatformAccount.objects.get()
        self.assertTrue(acc.last_test_ok)


class PerBranchKeyTests(DeliveryBase):
    """Every branch has its own Zomato / Swiggy keys — no .env involved."""
    def setUp(self):
        super().setUp()
        self.z2 = Connection.objects.create(restaurant=self.r, branch=self.b2, provider='zomato', external_store_id='Z-200')
        acc = PlatformAccount.objects.create(restaurant=self.r, provider='zomato', api_base_url='https://pos.example.com/v1',
                                             token_url='https://pos.example.com/oauth/token', api_paths={'accept': '/orders/{order_id}/accept'})
        for conn, cid in ((self.zomato, 'cid-mg'), (self.z2, 'cid-harbour')):
            conn.set_credentials({'client_id': cid, 'client_secret': f'{cid}-secret'})
            conn.mode = 'api'
            conn.save()

    def test_each_branch_resolves_its_own_keys_and_shares_the_rest(self):
        r1, r2 = C.resolve(Connection.objects.get(pk=self.zomato.pk)), C.resolve(Connection.objects.get(pk=self.z2.pk))
        self.assertEqual((r1.get('client_id'), r1.sources['client_id']), ('cid-mg', 'outlet'))
        self.assertEqual((r2.get('client_id'), r2.sources['client_id']), ('cid-harbour', 'outlet'))
        self.assertEqual((r1.get('token_url'), r1.sources['token_url']), ('https://pos.example.com/oauth/token', 'brand'))
        self.assertTrue(r1.api_ready and r2.api_ready)

    @override_settings(DEBUG=True)
    def test_tokens_are_fetched_and_cached_per_branch(self):
        def token_for(cid):
            return mock.Mock(status_code=200, json=lambda: {'access_token': f'tok-{cid}', 'expires_in': 3600})
        with mock.patch('requests.post', side_effect=[token_for('mg'), token_for('harbour')]) as post:
            t1 = C.access_token(Connection.objects.get(pk=self.zomato.pk))
            t2 = C.access_token(Connection.objects.get(pk=self.z2.pk))
        self.assertEqual((t1, t2), ('tok-mg', 'tok-harbour'))
        self.assertEqual([c.kwargs['data']['client_id'] for c in post.call_args_list], ['cid-mg', 'cid-harbour'])
        with mock.patch('requests.post') as post:  # cached (encrypted) on each branch
            self.assertEqual(C.access_token(Connection.objects.get(pk=self.zomato.pk)), 'tok-mg')
        post.assert_not_called()
        raw = Connection.objects.filter(pk=self.zomato.pk).values_list('token_encrypted', 'credentials_encrypted').get()
        self.assertNotIn('tok-mg', raw[0])
        self.assertNotIn('cid-mg-secret', raw[1])
        # changing a branch's keys throws its cached token away
        conn = Connection.objects.get(pk=self.zomato.pk)
        conn.set_credentials({'client_id': 'cid-new', 'client_secret': 's'})
        conn.save()
        self.assertEqual(Connection.objects.get(pk=self.zomato.pk).get_cached_token(), '')

    @override_settings(DEBUG=True)
    def test_settings_form_keeps_blank_keys_and_removes_on_request(self):
        self.client.force_login(self.admin)
        url = reverse('restaurant_admin:delivery_settings', kwargs={'restaurant_slug': self.r.slug, 'platform': 'zomato'})
        form = {'op': 'save', 'external_store_id': 'Z-100', 'commission_pct': '20', 'default_prep_minutes': '15', 'accept_window_seconds': '180',
                'is_active': '1', 'mode': 'api', 'environment': 'live'}
        self.client.post(url, {**form, 'api_key': 'branch-token', 'token_url': 'https://mg.example.com/token'})
        conn = Connection.objects.get(pk=self.zomato.pk)
        self.assertEqual(conn.get_credentials(), {'client_id': 'cid-mg', 'client_secret': 'cid-mg-secret', 'access_token': 'branch-token'})
        self.assertEqual(conn.token_url, 'https://mg.example.com/token')
        self.client.post(url, {**form, 'clear_client_secret': '1', 'token_url': ''})
        conn = Connection.objects.get(pk=self.zomato.pk)
        self.assertEqual(conn.get_credentials(), {'client_id': 'cid-mg', 'access_token': 'branch-token'})
        page = self.client.get(url)
        self.assertContains(page, 'API keys · MG Road')
        for secret in ('cid-mg-secret', 'branch-token'):
            self.assertNotContains(page, secret)
