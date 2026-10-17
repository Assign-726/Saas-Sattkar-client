"""A KOT fired at the POS reaches the kitchen: KDS access for kitchen logins, the kitchen bell,
add-on rounds and the live push."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import MenuItem, Order
from notifications.models import Notification
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class KotToKitchenTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main')
        self.dal = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Dal', price=Decimal('200'), sku='D')
        self.naan = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Naan', price=Decimal('50'), sku='N')
        self.cashier = User.objects.create_user('cash', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.r, branch=self.b, role='CASHIER')
        self.chef = User.objects.create_user('chef', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.chef, restaurant=self.r, branch=self.b, role='KITCHEN')

    def fire(self, items, order_id=None):
        self.client.force_login(self.cashier)
        body = {'action': 'fire_kot', 'order_type': 'takeaway', 'items': items}
        if order_id:
            body['order_id'] = order_id
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post('/dashboard/spice-main/orders/create/', data=json.dumps(body), content_type='application/json',
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(resp.json()['success'], resp.content)
        return Order.objects.get(pk=resp.json()['order_id'])

    def test_kitchen_login_reaches_kds_data_stream_and_bell_with_any_slug(self):
        self.client.force_login(self.chef)
        for slug in ('spice-co', 'spice-main'):
            r = self.client.get(f'/dashboard/{slug}/kitchen/api/orders/')
            self.assertEqual(r.status_code, 200, slug)
            self.assertTrue(r.json()['success'])
            self.assertEqual(self.client.get(f'/dashboard/{slug}/orders/stream/').status_code, 200, slug)
            self.assertEqual(self.client.get(f'/dashboard/{slug}/api/notifications/poll/').status_code, 200, slug)
        # still locked out of everything else
        self.assertEqual(self.client.get('/dashboard/spice-main/orders/create/').status_code, 302)  # not the POS

    def test_cashier_restrictions_also_apply_on_branch_links(self):
        self.client.force_login(self.cashier)
        r = self.client.get('/dashboard/spice-main/staff/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/transactions/create/', r['Location'])

    def test_kot_notifies_the_kitchen_and_pushes_live(self):
        with mock.patch('core.sse.publish') as pub:
            order = self.fire([{'menu_item_id': self.dal.pk, 'quantity': 2, 'unit_price': 200}])
        kinds = [c.args[1].get('type') for c in pub.call_args_list]
        self.assertIn('kot', kinds)
        n = Notification.objects.get(kind='kot_new')
        self.assertIn('2× Dal', n.body)
        self.client.force_login(self.chef)
        poll = self.client.get('/dashboard/spice-main/api/notifications/poll/', {'since': n.pk - 1}).json()
        self.assertEqual([x['kind'] for x in poll['new']], ['kot_new'])
        self.assertTrue(poll['new'][0]['sound'])  # the kitchen hears it
        # the KDS lists the ticket for the kitchen login
        kds = self.client.get('/dashboard/spice-main/kitchen/api/orders/').json()
        self.assertIn(order.pk, [o['id'] for o in kds['orders']])
        # the cashier who fired it sees it in the bell without a chime
        self.client.force_login(self.cashier)
        poll = self.client.get('/dashboard/spice-main/api/notifications/poll/', {'since': n.pk - 1}).json()
        self.assertFalse(poll['new'][0]['sound'])

    def test_add_on_round_keeps_cooked_lines_and_kitchen_timer(self):
        order = self.fire([{'menu_item_id': self.dal.pk, 'quantity': 1, 'unit_price': 200}])
        order.items.update(is_prepared=True)
        Order.objects.filter(pk=order.pk).update(status='PR')
        self.fire([{'menu_item_id': self.dal.pk, 'quantity': 1, 'unit_price': 200, 'sent': True},
                   {'menu_item_id': self.naan.pk, 'quantity': 3, 'unit_price': 50}], order_id=order.pk)
        order.refresh_from_db()
        lines = {i.menu_item.name: i for i in order.items.all()}
        self.assertEqual((lines['Dal'].kot_batch, lines['Dal'].is_prepared), (1, True))
        self.assertEqual((lines['Naan'].kot_batch, lines['Naan'].is_prepared), (2, False))
        self.assertEqual(order.status, 'PR')
        self.assertEqual(Notification.objects.filter(kind='kot_new').count(), 2)
        self.assertIn('round 2', Notification.objects.filter(kind='kot_new').order_by('-id').first().title)
