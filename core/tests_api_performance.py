"""Query budgets for the hot JSON endpoints.

Each test seeds a realistic outlet (dozens of items, tables and orders) and asserts the
endpoint's query count stays flat: if a serializer starts querying per row (N+1), the
count grows with the data and the test fails.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category, CustomCharge, MenuItem, Order, OrderItem, SubCategory, Table, Zone
from hotel.models import Room, RoomBooking, RoomCategory
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class Outlet(TestCase):
    ITEMS, TABLES, ORDERS = 40, 24, 30

    @classmethod
    def setUpTestData(cls):
        cls.r = Restaurant.objects.create(name='Spice Route', slug='spice-route', feature_website_pages=True)
        cls.b = RestaurantBranch.objects.create(restaurant=cls.r, name='Main', slug='spice-main', has_hotel_module=True)
        cats = [Category.objects.create(name=f'Cat {i}', restaurant=cls.r) for i in range(4)]
        subs = [SubCategory.objects.create(name=f'Sub {i}', category=c) for i, c in enumerate(cats)]
        cls.items = [MenuItem.objects.create(name=f'Dish {i}', price=D('100') + i, category=cats[i % 4], subcategory=subs[i % 4],
                                             restaurant=cls.r, availability=True, publishing_channels=['qr', 'online'])
                     for i in range(cls.ITEMS)]
        zones = [Zone.objects.create(name=f'Zone {i}', restaurant=cls.r, branch=cls.b) for i in range(4)]
        for i in range(cls.TABLES):
            Table.objects.create(restaurant=cls.r, branch=cls.b, zone=zones[i % 4], table_number=i + 1, name=f'T{i + 1}', capacity=4)
        CustomCharge.objects.create(restaurant=cls.r, charge_name='Packing', charge_type='fixed', value=D('20'), is_active=True)
        for i in range(cls.ORDERS):
            o = Order.objects.create(restaurant=cls.r, branch=cls.b, customer_name='G', phone='9999999999', payment_status='paid')
            for it in cls.items[i % 10: i % 10 + 3]:
                OrderItem.objects.create(order=o, menu_item=it, quantity=2, unit_price=it.price)
        cat = RoomCategory.objects.create(branch=cls.b, name='Deluxe', base_price_per_night=D('4000'))
        today = timezone.localdate()
        for i in range(20):
            room = Room.objects.create(branch=cls.b, category=cat, room_number=str(100 + i))
            RoomBooking.objects.create(room=room, branch=cls.b, guest_name='G', guest_email='g@x.com', guest_phone='1',
                                       check_in_date=today, check_out_date=today + datetime.timedelta(days=2))
        cls.mgr = User.objects.create_user('perf_mgr', password='x', is_staff=True)
        RestaurantUser.objects.create(user=cls.mgr, restaurant=cls.r, role=RestaurantUser.Role.GENERAL_MANAGER)

    def setUp(self):
        cache.clear()
        self.api = APIClient()

    def queries(self, path, user=None, **params):
        if user:
            self.api.force_authenticate(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api.get(path, params)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return len(ctx.captured_queries), resp


class QueryBudgetTests(Outlet):
    def test_public_menu(self):
        n, resp = self.queries('/api/menu-items/', restaurant_slug=self.r.slug)
        self.assertEqual(len(resp.json()), self.ITEMS)
        self.assertLessEqual(n, 4)

    def test_public_settings_with_zones_and_tables(self):
        n, resp = self.queries('/api/settings/', restaurant_slug=self.r.slug, branch_slug=self.b.slug)
        self.assertEqual(sum(len(z['tables']) for z in resp.json()['zones']), self.TABLES)
        self.assertLessEqual(n, 12)

    def test_public_tables_and_zones(self):
        n, _ = self.queries('/api/tables/', restaurant_slug=self.r.slug)
        self.assertLessEqual(n, 3)
        n, _ = self.queries('/api/zones/', restaurant_slug=self.r.slug)
        self.assertLessEqual(n, 4)

    def test_staff_order_list(self):
        n, resp = self.queries('/api/orders/', user=self.mgr)
        self.assertEqual(len(resp.json()), self.ORDERS)
        self.assertLessEqual(n, 8)

    def test_hotel_booking_list(self):
        n, resp = self.queries('/api/hotel/bookings/', user=self.mgr)
        self.assertEqual(len(resp.json()), 20)
        self.assertLessEqual(n, 6)


class CatalogCacheTests(Outlet):
    def test_menu_is_served_from_cache_until_an_item_changes(self):
        path = '/api/menu-items/'
        first = self.api.get(path, {'restaurant_slug': self.r.slug})
        self.assertEqual(first['X-Cache'], 'MISS')
        with CaptureQueriesContext(connection) as ctx:
            second = self.api.get(path, {'restaurant_slug': self.r.slug})
        self.assertEqual((second['X-Cache'], len(ctx.captured_queries)), ('HIT', 0))
        self.assertEqual(second.json(), first.json())

        item = self.items[0]
        item.availability = False
        item.save()
        third = self.api.get(path, {'restaurant_slug': self.r.slug})
        self.assertEqual(third['X-Cache'], 'MISS')
        self.assertEqual(len(third.json()), self.ITEMS - 1)

    def test_logged_in_users_bypass_the_cache(self):
        self.api.force_authenticate(self.mgr)
        resp = self.api.get('/api/menu-items/', {'restaurant_slug': self.r.slug})
        self.assertNotIn('X-Cache', resp)
