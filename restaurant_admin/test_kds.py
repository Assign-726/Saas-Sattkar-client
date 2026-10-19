import datetime
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from core.models import MenuItem, Order, OrderItem
from restaurant_admin import kds
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class KdsWindowTests(TestCase):
    def test_resolve_window(self):
        today = datetime.date(2026, 10, 17)
        self.assertEqual(kds.resolve_window({}, today), ('today', today, today))
        self.assertEqual(kds.resolve_window({'range': '7d'}, today)[1], datetime.date(2026, 10, 11))
        self.assertEqual(kds.resolve_window({'range': '30d'}, today)[1], datetime.date(2026, 9, 18))
        self.assertEqual(kds.resolve_window({'range': 'yesterday'}, today)[1:], (datetime.date(2026, 10, 16),) * 2)
        # reversed + future end is swapped and clamped to today
        self.assertEqual(kds.resolve_window({'range': 'custom', 'from': '2026-10-20', 'to': '2026-10-01'}, today),
                         ('custom', datetime.date(2026, 10, 1), today))
        # bad input falls back to today; very long spans are capped
        self.assertEqual(kds.resolve_window({'range': 'custom', 'from': 'nope'}, today)[0], 'today')
        key, start, end = kds.resolve_window({'range': 'custom', 'from': '2020-01-01', 'to': '2026-10-17'}, today)
        self.assertEqual((end - start).days, kds.MAX_CUSTOM_DAYS - 1)
        self.assertEqual(kds.resolve_window({'range': 'weird'}, today)[0], 'today')


class KdsOrdersApiTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Line Kitchen', slug='line-kitchen', is_active=True)
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Central', slug='central')
        self.admin = User.objects.create_user('kds_admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.admin, restaurant=self.r, branch=self.b, role='RESTAURANT_ADMIN', is_primary_admin=True)
        self.dal = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Dal Makhani', price=Decimal('300'), sku='D1')
        self.naan = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Garlic Naan', price=Decimal('60'), sku='N1')
        self.url = f'/dashboard/{self.r.slug}/kitchen/api/orders/'
        self.client.force_login(self.admin)

    def order(self, status, days_ago=0, elapsed=0, prep=10, qty=1, item=None):
        o = Order.objects.create(restaurant=self.r, branch=self.b, customer_name='Guest', order_type='dine_in', status=status)
        OrderItem.objects.create(order=o, menu_item=item or self.dal, quantity=qty, unit_price=Decimal('300'))
        when = timezone.now() - timedelta(days=days_ago)
        Order.objects.filter(pk=o.pk).update(order_time=when, prep_elapsed_seconds=elapsed, prep_minutes=prep,
                                             ready_at=when + timedelta(seconds=elapsed) if elapsed else None)
        return o

    def get(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_today_vs_ranges_and_summary(self):
        S = Order.OrderStatus
        self.order(S.SERVED, elapsed=300, qty=2)                    # on time
        self.order(S.SERVED, elapsed=900, qty=1, item=self.naan)    # late (target 10 min)
        self.order(S.SERVED, days_ago=3, elapsed=120, qty=5)
        self.order(S.SERVED, days_ago=40, elapsed=120)
        today = self.get()
        self.assertEqual(today['window']['range'], 'today')
        self.assertEqual(today['counts']['all'], 2)
        self.assertEqual(today['summary']['on_time_pct'], 50)
        self.assertEqual(today['summary']['late'], 1)
        self.assertEqual(today['summary']['avg_prep_seconds'], 600)
        week = self.get(range='7d')
        self.assertEqual(week['counts']['all'], 3)
        self.assertEqual(week['summary']['top_items'][0], {'name': 'Dal Makhani', 'qty': 7})
        self.assertEqual(self.get(range='30d')['counts']['all'], 3)
        past = timezone.localdate() - timedelta(days=45)
        self.assertEqual(self.get(range='custom', **{'from': past.isoformat(), 'to': timezone.localdate().isoformat()})['counts']['all'], 4)
        # finished tickets read as fully done
        self.assertTrue(all(it['is_prepared'] for o in today['orders'] for it in o['items']))

    def test_open_ticket_from_before_window_stays_on_today_board(self):
        old = self.order(Order.OrderStatus.PREPARING, days_ago=1)
        data = self.get()
        cards = {o['id']: o for o in data['orders']}
        self.assertIn(old.id, cards)
        self.assertTrue(cards[old.id]['is_carried_over'])
        self.assertEqual(data['summary']['tickets'], 0)  # counted on the day it was fired, not today
        # a past-only window is history: no carry-over
        self.assertNotIn(old.id, [o['id'] for o in self.get(range='custom', **{'from': '2020-01-01', 'to': '2020-01-02'})['orders']])

    def test_open_tickets_first_and_status_filter(self):
        S = Order.OrderStatus
        done = self.order(S.READY, elapsed=200)
        new = self.order(S.PENDING)
        data = self.get()
        self.assertEqual([o['id'] for o in data['orders']], [new.id, done.id])
        self.assertTrue(data['orders'][1]['is_ready_for_pickup'])
        self.assertEqual([o['id'] for o in self.get(status='completed')['orders']], [done.id])
        self.assertEqual(self.get(status='new', search='makhani')['orders'][0]['id'], new.id)

    def test_query_count_does_not_grow_per_ticket(self):
        for _ in range(3):
            self.order(Order.OrderStatus.SERVED, elapsed=100)
        with CaptureQueriesContext(connection) as small:
            self.get(range='7d')
        for _ in range(12):
            self.order(Order.OrderStatus.SERVED, elapsed=100)
        with CaptureQueriesContext(connection) as big:
            self.get(range='7d')
        self.assertEqual(len(small), len(big))

    def test_page_renders(self):
        resp = self.client.get(f'/dashboard/{self.r.slug}/kitchen/?range=7d')
        self.assertContains(resp, 'id="kdsRangeSeg"')
        self.assertContains(resp, 'id="kdsPerf"')
