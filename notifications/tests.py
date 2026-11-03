import json
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import InventoryItem, MenuItem, Order
from guests.models import Feedback
from integrations.models import ApiKey, Connection, PlatformMenuMapping
from notifications.models import Notification, NotificationState
from notifications import service as S
from reservations.models import Reservation
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class NotificationTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Bell Bistro', slug='bell-bistro', is_active=True)
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='bell-main')
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='Annex', slug='bell-annex')
        self.user = User.objects.create_user('nt_admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, role='RESTAURANT_ADMIN', branch=self.b)
        self.mate = User.objects.create_user('nt_cashier', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.mate, restaurant=self.r, role='CASHIER', branch=self.b)
        self.item = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Masala Dosa', price=Decimal('120'), sku='MD1')

    def url(self, name):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug})

    # ---------------------------------------------------------------- sources

    def test_pos_orders_do_not_notify(self):
        with self.captureOnCommitCallbacks(execute=True):
            Order.objects.create(restaurant=self.r, branch=self.b, customer_name='Walk-in', status='P', cashier=self.user)
        self.assertFalse(Notification.objects.exists())

    def test_api_order_notifies(self):
        key, raw = ApiKey.issue(restaurant=self.r, branch=self.b, name='Website', scopes=['orders:write'])
        resp = self.client.post('/api/v1/orders', json.dumps({'external_id': 'W-1', 'source': 'website',
                                                               'items': [{'sku': 'MD1', 'quantity': 2}]}),
                                content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {raw}')
        self.assertEqual(resp.status_code, 201, resp.content)
        n = Notification.objects.get()
        self.assertEqual((n.kind, n.source, n.branch), ('order_new', 'website', self.b))
        self.assertIn('2 items', n.body)

    def test_zomato_order_and_cancellation_notify(self):
        import hashlib, hmac
        conn = Connection.objects.create(restaurant=self.r, branch=self.b, provider='zomato', external_store_id='Z1')
        PlatformMenuMapping.objects.create(connection=conn, menu_item=self.item, external_item_id='ZMD')
        def send(payload):
            raw = json.dumps(payload).encode()
            sig = hmac.new(conn.get_webhook_secret().encode(), raw, hashlib.sha256).hexdigest()
            self.client.post(reverse('delivery_webhook', kwargs={'provider': 'zomato', 'public_id': conn.public_id}), raw,
                             content_type='application/json', HTTP_X_SIGNATURE=sig)
        send({'event': 'order.placed', 'event_id': 'e1', 'order': {'id': 'ZO-9', 'items': [{'item_id': 'ZMD', 'quantity': 1, 'unit_price': '120'}]}})
        send({'event': 'order.cancelled', 'event_id': 'e2', 'order_id': 'ZO-9', 'reason': 'Customer cancelled'})
        send({'event': 'order.placed', 'event_id': 'e3', 'order': {'id': 'ZO-10', 'items': [{'item_id': 'NOPE', 'name': 'Lassi', 'quantity': 1}]}})
        kinds = list(Notification.objects.order_by('id').values_list('kind', 'level', 'source'))
        self.assertEqual(kinds, [('delivery_new', 'warning', 'zomato'), ('delivery_cancelled', 'critical', 'zomato'),
                                 ('delivery_attention', 'critical', 'zomato')])
        self.assertIn('/delivery/zomato/orders/', Notification.objects.get(kind='delivery_new').url)

    def test_kitchen_ready_notifies_once(self):
        o = Order.objects.create(restaurant=self.r, branch=self.b, customer_name='T1', status='PR')
        with self.captureOnCommitCallbacks(execute=True):
            o.status = 'READY'
            o.save()
        with self.captureOnCommitCallbacks(execute=True):
            o.save()  # saving again while still READY is not a new event
        self.assertEqual(Notification.objects.filter(kind='order_ready').count(), 1)

    def test_reservation_feedback_and_stock_alerts(self):
        with self.captureOnCommitCallbacks(execute=True):
            Reservation.objects.create(restaurant=self.r, branch=self.b, customer_name='Rao', phone='98', party_size=4,
                                       date=date(2026, 10, 20), start_time=time(20, 0))
            Feedback.objects.create(restaurant=self.r, branch=self.b, rating=1, comment='Cold food', name='Asha')
            Feedback.objects.create(restaurant=self.r, branch=self.b, rating=5, name='Ravi')
            stock = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Rice', quantity=10, reorder_level=5, unit='kg')
        with self.captureOnCommitCallbacks(execute=True):
            stock.quantity = 4
            stock.save()
        with self.captureOnCommitCallbacks(execute=True):
            stock.quantity = 3  # already below: no second alert today
            stock.save()
        got = {n.kind: n for n in Notification.objects.all()}
        self.assertIn('4 guests', got['reservation'].body)
        low = Notification.objects.get(kind='feedback', level='warning')
        self.assertTrue(low.sound)
        self.assertFalse(Notification.objects.get(kind='feedback', level='info').sound)
        self.assertEqual(Notification.objects.filter(kind='low_stock').count(), 1)

    # ---------------------------------------------------------------- inbox

    def make(self, n=3, branch=None):
        return [S.notify(self.r, 'order_new', f'Order {i}', branch=branch or self.b, source='qr') for i in range(n)]

    def test_poll_reports_only_new_items_and_unread(self):
        self.client.force_login(self.user)
        first = self.client.get(self.url('order_notifications_poll')).json()
        self.assertEqual((first['latest_id'], first['unread'], first['new']), (0, 0, []))
        made = self.make(2)
        d = self.client.get(self.url('order_notifications_poll') + '?since=0').json()
        self.assertEqual(d['unread'], 2)
        d = self.client.get(self.url('order_notifications_poll') + f'?since={made[0].pk}').json()
        self.assertEqual([n['title'] for n in d['new']], ['Order 1'])

    def test_read_archive_tabs_and_bulk(self):
        a, b, c = self.make(3)
        self.client.force_login(self.user)
        def tab(t):
            return [i['id'] for i in self.client.get(self.url('notifications_list') + f'?tab={t}').json()['items']]
        self.assertEqual(tab('inbox'), [c.pk, b.pk, a.pk])
        self.client.post(self.url('notifications_update'), {'op': 'read', 'ids': f'{a.pk}'})
        self.client.post(self.url('notifications_update'), {'op': 'archive', 'ids': f'{b.pk}'})
        self.assertEqual(tab('unread'), [c.pk])
        self.assertEqual(tab('read'), [a.pk])
        self.assertEqual(tab('archived'), [b.pk])
        self.assertEqual(tab('inbox'), [c.pk, a.pk])
        resp = self.client.post(self.url('notifications_update'), {'op': 'read_all'}).json()
        self.assertEqual(resp['unread'], 0)
        self.client.post(self.url('notifications_update'), {'op': 'archive_read'})
        self.assertEqual(tab('inbox'), [])
        self.assertEqual(sorted(tab('archived')), sorted([a.pk, b.pk, c.pk]))
        self.client.post(self.url('notifications_update'), {'op': 'unarchive', 'ids': f'{c.pk}'})
        self.client.post(self.url('notifications_update'), {'op': 'unread', 'ids': f'{c.pk}'})
        self.assertEqual(tab('unread'), [c.pk])

    def test_state_is_per_person(self):
        a, = self.make(1)
        self.client.force_login(self.user)
        self.client.post(self.url('notifications_update'), {'op': 'archive', 'ids': f'{a.pk}'})
        self.client.force_login(self.mate)
        items = self.client.get(self.url('notifications_list')).json()['items']
        self.assertEqual([(i['id'], i['read']) for i in items], [(a.pk, False)])

    def test_category_filter_and_branch_scope(self):
        self.make(1)
        S.notify(self.r, 'delivery_new', 'Swiggy order', branch=self.b, source='swiggy')
        S.notify(self.r, 'order_new', 'Other branch', branch=self.b2)
        S.notify(self.r, 'system', 'Everyone')
        self.client.force_login(self.user)
        titles = [i['title'] for i in self.client.get(self.url('notifications_list')).json()['items']]
        self.assertNotIn('Other branch', titles)
        self.assertIn('Everyone', titles)
        online = [i['title'] for i in self.client.get(self.url('notifications_list') + '?cat=online').json()['items']]
        self.assertEqual(online, ['Swiggy order'])

    def test_cannot_touch_other_business_notifications(self):
        other = Restaurant.objects.create(name='Elsewhere', slug='elsewhere')
        n = S.notify(other, 'order_new', 'Not yours')
        self.client.force_login(self.user)
        self.client.post(self.url('notifications_update'), {'op': 'archive', 'ids': f'{n.pk}'})
        self.assertFalse(NotificationState.objects.exists())

    def test_pagination(self):
        self.make(35)
        self.client.force_login(self.user)
        first = self.client.get(self.url('notifications_list')).json()
        self.assertEqual((len(first['items']), first['has_more']), (30, True))
        more = self.client.get(self.url('notifications_list') + f'?before={first["items"][-1]["id"]}').json()
        self.assertEqual((len(more['items']), more['has_more']), (5, False))

    def test_dedupe(self):
        a = S.notify(self.r, 'order_new', 'x', dedupe_key='k1')
        b = S.notify(self.r, 'order_new', 'x', dedupe_key='k1')
        self.assertEqual(a.pk, b.pk)

    def test_drawer_is_on_admin_pages(self):
        self.client.force_login(self.user)
        page = self.client.get(self.url('dashboard'))
        self.assertContains(page, 'id="ntDrawer"')
        self.assertContains(page, 'data-nt-open')

    @override_settings(SSE_STREAMS='on')
    def test_notification_stream_endpoint(self):
        self.client.force_login(self.user)
        resp = self.client.get(self.url('order_notifications_stream'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/event-stream')

    @override_settings(SSE_STREAMS='auto')
    def test_stream_declines_on_single_threaded_server(self):
        # gunicorn's sync worker (like the test client) runs one request per process: an open stream
        # would pin the worker, so the endpoint answers 204 and the page polls instead.
        self.client.force_login(self.user)
        resp = self.client.get(self.url('order_notifications_stream'))
        self.assertEqual(resp.status_code, 204)
        resp = self.client.get(self.url('order_notifications_stream'), **{'wsgi.multithread': True})
        self.assertEqual(resp.status_code, 200)

    def test_notify_publishes_sse(self):
        from core.sse import subscribe, unsubscribe
        q, closed = subscribe(self.r.slug)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                S.notify(self.r, 'order_new', 'New Test Order')
            data = q.get(timeout=2.0)
            self.assertIn('"type": "notification"', data)
            self.assertIn('"kind": "order_new"', data)
        finally:
            unsubscribe(self.r.slug, q, closed)

