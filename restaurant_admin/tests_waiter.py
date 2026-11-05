"""Waiter / Captain: tables -> POS -> send to kitchen. Settling stays with the cashier / admin."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import MenuItem, Order, Table, Zone
from notifications.models import Notification
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class WaiterFlowTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Sharma Hotel', slug='sharma-w', is_active=True)
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Murlipura', slug='sharma-w-main')
        self.zone = Zone.objects.create(restaurant=self.r, branch=self.b, name='Main Area')
        self.t1 = Table.objects.create(restaurant=self.r, branch=self.b, zone=self.zone, name='T1', table_number=1, capacity=4)
        self.t2 = Table.objects.create(restaurant=self.r, branch=self.b, zone=self.zone, name='T2', table_number=2, capacity=2)
        self.item = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Paneer Tikka', price=Decimal('200'))

        self.waiter = User.objects.create_user('ramesh', password='x', is_staff=True, first_name='Ramesh')
        self.waiter_profile = RestaurantUser.objects.create(
            user=self.waiter, restaurant=self.r, branch=self.b, role='WAITER',
            can_apply_discounts=False, can_manage_menu=False, can_view_analytics=False,
            can_issue_refunds=False, can_manage_settings=False, can_view_inventory=False)
        self.waiter2 = User.objects.create_user('suresh', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.waiter2, restaurant=self.r, branch=self.b, role='WAITER',
                                      can_apply_discounts=False)
        self.cashier = User.objects.create_user('cash', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.r, branch=self.b, role='CASHIER')

    # ------------------------------------------------------------- helpers

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def post_pos(self, action, qty=2, order_id=None, table=None, **extra):
        body = {'action': action, 'order_id': order_id, 'table_id': (table or self.t1).pk if table is not False else '',
                'order_type': 'dine_in', 'customer_name': 'Guest',
                'items': [{'menu_item_id': self.item.pk, 'quantity': qty, 'unit_price': '200', 'sent': bool(order_id)}],
                'subtotal': str(200 * qty), 'tax_amount': '0', 'round_off_amount': '0', 'grand_total': str(200 * qty)}
        body.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url('order_create_manual'), data=json.dumps(body),
                                    content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def fire_as_waiter(self, qty=2):
        self.client.force_login(self.waiter)
        resp = self.post_pos('fire_kot', qty=qty)
        self.assertEqual(resp.status_code, 200, resp.content)
        return Order.objects.get(pk=resp.json()['order_id'])

    # ------------------------------------------------------------- routing

    def test_login_lands_on_tables(self):
        resp = self.client.post(reverse('login'), {'username': 'ramesh', 'password': 'x'})
        self.assertRedirects(resp, self.url('waiter_tables'), fetch_redirect_response=False)

    def test_middleware_keeps_waiter_off_admin_pages(self):
        self.client.force_login(self.waiter)
        for name in ('floor_plan_dashboard', 'order_records', 'transaction_desk_create', 'dashboard'):
            resp = self.client.get(self.url(name))
            self.assertRedirects(resp, self.url('waiter_tables'), fetch_redirect_response=False, msg_prefix=name)
        self.assertEqual(self.client.get(self.url('waiter_tables')).status_code, 200)
        self.assertEqual(self.client.get(self.url('order_create_manual')).status_code, 200)

    # ------------------------------------------------------------- ordering

    def test_waiter_sends_order_to_kitchen(self):
        order = self.fire_as_waiter()
        self.assertEqual(order.status, Order.OrderStatus.PENDING)
        self.assertEqual(order.waiter, self.waiter)
        self.assertIsNone(order.cashier)
        self.assertEqual(order.table, self.t1)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)

    def test_waiter_can_add_a_second_round(self):
        order = self.fire_as_waiter(qty=1)
        resp = self.post_pos('fire_kot', qty=3, order_id=order.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(sum(i.quantity for i in order.items.all()), 3)

    def test_other_waiter_can_add_but_owner_stays(self):
        order = self.fire_as_waiter(qty=1)
        self.client.force_login(self.waiter2)
        self.assertEqual(self.post_pos('fire_kot', qty=2, order_id=order.pk).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.waiter, self.waiter)

    def test_open_table_resumes_running_order(self):
        order = self.fire_as_waiter()
        resp = self.client.get(self.url('waiter_open_table', pk=self.t1.pk))
        self.assertIn(f'order_id={order.pk}', resp['Location'])
        resp = self.client.get(self.url('waiter_open_table', pk=self.t2.pk) + '?covers=3')
        self.assertIn(f'table_id={self.t2.pk}', resp['Location'])
        self.assertIn('covers=3', resp['Location'])

    def test_tables_api_shows_state_and_waiter(self):
        self.fire_as_waiter()
        data = self.client.get(self.url('waiter_tables_api')).json()
        rows = {t['name']: t for z in data['zones'] for t in z['tables']}
        self.assertEqual(rows['T1']['state'], 'kitchen')
        self.assertTrue(rows['T1']['mine'])
        self.assertEqual(rows['T1']['waiter'], 'Ramesh')
        self.assertEqual(rows['T2']['state'], 'free')

    # ------------------------------------------------------------- what a waiter may not do

    def test_waiter_cannot_settle_in_pos(self):
        self.client.force_login(self.waiter)
        for action in ('settle_order', 'print_close', 'save_invoice'):
            resp = self.post_pos(action, payment_status='paid', payment_method='cash')
            self.assertEqual(resp.status_code, 403, action)
        self.assertFalse(Order.objects.exists())

    def test_waiter_payment_fields_are_ignored(self):
        self.client.force_login(self.waiter)
        resp = self.post_pos('fire_kot', payment_status='paid', paid_amount='400',
                             payments=[{'method': 'cash', 'amount': 400}])
        order = Order.objects.get(pk=resp.json()['order_id'])
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.paid_amount, Decimal('0'))
        self.assertEqual(order.payments, [])

    def test_waiter_cannot_use_settle_or_credit_note_apis(self):
        order = self.fire_as_waiter()
        xhr = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
        self.assertEqual(self.client.post(self.url('order_settle_api', pk=order.pk),
                                          data=json.dumps({'payment_method': 'cash'}),
                                          content_type='application/json', **xhr).status_code, 403)
        self.assertEqual(self.client.post(self.url('order_credit_note_api', pk=order.pk), **xhr).status_code, 403)
        # the view itself refuses too, even if the middleware let the request through
        from django.test import RequestFactory
        from restaurant_admin.views import order_settle_api
        req = RequestFactory().post('/x/', data=json.dumps({'payment_method': 'cash'}), content_type='application/json')
        req.user = self.waiter
        self.assertEqual(order_settle_api(req, pk=order.pk, restaurant_slug=self.r.slug).status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)

    def test_waiter_cannot_settle_via_rest_api(self):
        from rest_framework.test import APIClient
        order = self.fire_as_waiter()
        api = APIClient()
        api.force_authenticate(self.waiter)
        resp = api.post(f'/api/orders/{order.pk}/settle/', data=json.dumps({'payment_method': 'cash'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        resp = api.patch(f'/api/orders/{order.pk}/', data=json.dumps({'payment_status': 'paid'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 403)

    def test_waiter_cannot_void_sent_items(self):
        order = self.fire_as_waiter(qty=3)
        self.assertEqual(self.post_pos('fire_kot', qty=1, order_id=order.pk).status_code, 403)
        self.assertEqual(self.post_pos('void_item', qty=1, order_id=order.pk,
                                       voided_item_name='Paneer Tikka', void_reason='Guest changed mind').status_code, 403)
        order.refresh_from_db()
        self.assertEqual(sum(i.quantity for i in order.items.all()), 3)

    def test_autosave_cannot_change_a_sent_order(self):
        order = self.fire_as_waiter(qty=3)
        resp = self.client.post(self.url('pos_sync_draft_items_api'), data=json.dumps({
            'order_id': order.pk, 'table_id': self.t1.pk, 'items': [{'menu_item_id': self.item.pk, 'quantity': 1}]}),
            content_type='application/json')
        self.assertTrue(resp.json().get('skipped'))
        self.assertEqual(sum(i.quantity for i in order.items.all()), 3)

    def test_waiter_takes_dine_in_only(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.post_pos('fire_kot', table=False).status_code, 403)
        self.assertEqual(self.post_pos('fire_kot', order_type='takeaway').status_code, 403)

    def test_discount_needs_permission(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.post_pos('fire_kot', discount_amount='40').status_code, 403)
        self.waiter_profile.can_apply_discounts = True
        self.waiter_profile.save()
        self.assertEqual(self.post_pos('fire_kot', discount_amount='40').status_code, 200)

    def test_waiter_cannot_complete_order_status(self):
        order = self.fire_as_waiter()
        self.client.post(self.url('order_update_status', pk=order.pk), {'status': 'COMPLETED'})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.OrderStatus.PENDING)

    # ------------------------------------------------------------- bill request & the cashier

    def test_request_bill_notifies_cashier_and_cashier_settles(self):
        order = self.fire_as_waiter()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.url('waiter_request_bill', pk=order.pk))
        self.assertTrue(resp.json()['success'])
        order.refresh_from_db()
        self.assertIsNotNone(order.bill_requested_at)
        self.assertTrue(Notification.objects.filter(kind='bill_request', order=order).exists())

        self.client.force_login(self.cashier)
        resp = self.client.post(self.url('order_settle_api', pk=order.pk), data=json.dumps({'payment_method': 'cash'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.waiter, self.waiter)  # who served stays on the bill

    def test_cashier_pos_does_not_resume_a_waiter_draft(self):
        Order.objects.create(restaurant=self.r, branch=self.b, table=self.t1, status=Order.OrderStatus.DRAFT,
                             waiter=self.waiter, customer_name='Guest')
        self.client.force_login(self.cashier)
        resp = self.client.get(self.url('order_create_manual'))
        self.assertIsNone(resp.context['order'])
