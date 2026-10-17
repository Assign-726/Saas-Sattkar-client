"""Wastage, stock counts, menu engineering, waitlist and charging a restaurant bill to a hotel room."""
import datetime
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (InventoryItem, InventoryTransaction, MenuItem, MenuItemIngredient, Order, OrderItem, StockCount, Table)
from hotel.models import FolioCharge, Room, RoomBooking, RoomCategory
from reservations.models import WaitlistEntry
from restaurant_admin.views_controls import menu_engineering
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class ControlsBase(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice Route', slug='spice-route')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main', has_hotel_module=True)
        self.user = User.objects.create_user('mgr', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='GENERAL_MANAGER')
        self.client.force_login(self.user)
        self.rice = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Rice', unit='kg', quantity=50, cost_price=D('60'))
        self.paneer = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Paneer', unit='kg', quantity=10, cost_price=D('400'))

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})


class WastageAndCountTests(ControlsBase):
    def test_log_wastage_reduces_stock_and_reports_value(self):
        self.client.post(self.url('inventory_wastage_add'), {'item': self.paneer.pk, 'quantity': '2', 'reason': 'SPOILED', 'note': 'Fridge'})
        self.paneer.refresh_from_db()
        self.assertEqual(self.paneer.quantity, 8)
        t = InventoryTransaction.objects.get(transaction_type='W')
        self.assertEqual((t.delta, t.reason, t.unit_cost), (-2, 'SPOILED', D('400.00')))
        page = self.client.get(self.url('inventory_wastage'))
        self.assertContains(page, '₹800')
        self.assertContains(page, 'Spoiled / expired')
        self.client.post(self.url('inventory_wastage_add'), {'item': self.paneer.pk, 'quantity': '1'})  # no reason
        self.assertEqual(InventoryTransaction.objects.filter(transaction_type='W').count(), 1)

    def test_stock_count_applies_only_the_difference(self):
        self.client.post(self.url('inventory_count_new'), {'title': 'Week 42'})
        count = StockCount.objects.get()
        lines = {l.item.name: l for l in count.lines.all()}
        self.assertEqual(lines['Rice'].expected, 50)
        # A sale happens while counting: books drop to 45
        self.rice.adjust(-5)
        self.client.post(self.url('inventory_count_save', pk=count.pk), {f'c_{lines["Rice"].pk}': '47', f'c_{lines["Paneer"].pk}': '', 'op': 'save'})
        self.assertEqual(StockCount.objects.get().status, 'DRAFT')
        page = self.client.get(self.url('inventory_count_detail', pk=count.pk))
        self.assertContains(page, '−₹180')
        self.client.post(self.url('inventory_count_save', pk=count.pk), {f'c_{lines["Rice"].pk}': '47', 'op': 'post'})
        self.rice.refresh_from_db()
        self.paneer.refresh_from_db()
        self.assertEqual((self.rice.quantity, self.paneer.quantity), (42, 10))  # 45 − 3 missing; paneer not counted
        self.assertEqual(StockCount.objects.get().status, 'POSTED')
        self.assertEqual(InventoryTransaction.objects.get(transaction_type='S').delta, -3)


class MenuEngineeringTests(ControlsBase):
    def test_classifies_dishes(self):
        star = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Paneer Tikka', price=D('300'))
        plow = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Jeera Rice', price=D('120'))
        dog = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Plain Rice', price=D('90'))
        nore = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Lassi', price=D('80'))
        MenuItemIngredient.objects.create(menu_item=star, inventory_item=self.paneer, quantity_needed=D('0.2'))   # cost 80
        MenuItemIngredient.objects.create(menu_item=plow, inventory_item=self.rice, quantity_needed=D('1'))       # cost 60
        MenuItemIngredient.objects.create(menu_item=dog, inventory_item=self.rice, quantity_needed=D('1'))        # cost 60
        order = Order.objects.create(restaurant=self.r, branch=self.b, customer_name='T1', status='COMPLETED', payment_status='paid')
        for item, qty in ((star, 10), (plow, 10), (dog, 1), (nore, 3)):
            OrderItem.objects.create(order=order, menu_item=item, quantity=qty, unit_price=item.price)
        today = timezone.localdate()
        d = menu_engineering(self.r, [self.b], today, today)
        cls = {r['name']: r['class'] for r in d['rows']}
        self.assertEqual(cls, {'Paneer Tikka': 'star', 'Jeera Rice': 'plowhorse', 'Plain Rice': 'dog', 'Lassi': 'unknown'})
        self.assertEqual(d['uncosted'], 1)
        tikka = next(r for r in d['rows'] if r['name'] == 'Paneer Tikka')
        self.assertEqual((tikka['cost'], tikka['margin'], tikka['food_cost_pct']), (D('80.00'), D('220.00'), 26.7))
        page = self.client.get(self.url('report_menu_engineering'), {'period': 'today'})
        self.assertContains(page, 'Plowhorse')
        self.assertEqual(self.client.get(self.url('report_menu_engineering'), {'period': 'today', 'export': 'csv'})['Content-Type'], 'text/csv')


class WaitlistTests(ControlsBase):
    def test_add_call_and_seat(self):
        t4 = Table.objects.create(restaurant=self.r, branch=self.b, name='T4', table_number=4, capacity=4)
        Table.objects.create(restaurant=self.r, branch=self.b, name='T2', table_number=2, capacity=2)
        self.client.post(self.url('reservation_waitlist_add'), {'name': 'Mehta', 'party_size': '4', 'phone': '98111 22333', 'quoted_minutes': '20'})
        w = WaitlistEntry.objects.get()
        page = self.client.get(self.url('reservation_waitlist'))
        self.assertContains(page, 'Mehta · 4 guests')
        self.assertContains(page, 'wa.me/919811122333')
        self.assertContains(page, 'T4 · 4 seats')
        self.assertNotContains(page, 'T2 · 2 seats')  # too small for the party
        self.client.post(self.url('reservation_waitlist_update', pk=w.pk), {'op': 'seat', 'table': t4.pk})
        w.refresh_from_db()
        self.assertEqual((w.status, w.table), ('SEATED', t4))


class ChargeToRoomTests(ControlsBase):
    def setUp(self):
        super().setUp()
        cat = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=D('4000'))
        self.room = Room.objects.create(branch=self.b, category=cat, room_number='305')
        today = timezone.localdate()
        self.stay = RoomBooking.objects.create(room=self.room, branch=self.b, guest_name='Kapoor', guest_email='k@x.com', guest_phone='9',
                                               check_in_date=today, check_out_date=today + datetime.timedelta(days=2), booking_status='CHECKED_IN')
        self.item = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Thali', price=D('1000'))

    def settle(self, payments):
        body = {'action': 'settle_order', 'customer_name': 'Kapoor', 'phone_number': '',
                'items': [{'menu_item_id': self.item.pk, 'quantity': 1, 'unit_price': '1000'}],
                'subtotal': '1000', 'tax_amount': '50', 'round_off_amount': '0', 'grand_total': '1050',
                'payment_method': 'cash', 'payment_status': 'paid', 'payments': payments}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url('order_create_manual'), data=json.dumps(body), content_type='application/json',
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_in_house_list_and_charge_then_cancel(self):
        rooms = self.client.get(self.url('guest_pos_lookup'), {'rooms': 1}).json()['rooms']
        self.assertEqual([(r['room'], r['guest']) for r in rooms], [('305', 'Kapoor')])
        resp = self.settle([{'method': 'room', 'booking_id': self.stay.pk, 'amount': 1050}])
        self.assertTrue(resp.json().get('success'), resp.content)
        order = Order.objects.get(pk=resp.json()['order_id'])
        charge = FolioCharge.objects.get(source_order=order)
        self.assertEqual((charge.kind, charge.booking, charge.total), ('RESTAURANT', self.stay, order.total_amount))
        self.assertEqual(order.payment_status, 'paid')
        order.status = 'C'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()
        charge.refresh_from_db()
        self.assertTrue(charge.is_void)

    def test_rejects_guest_not_in_house(self):
        self.stay.booking_status = 'CHECKED_OUT'
        self.stay.save()
        self.assertEqual(self.settle([{'method': 'room', 'booking_id': self.stay.pk, 'amount': 1050}]).status_code, 400)
        other = Restaurant.objects.create(name='Other', slug='other')
        ob = RestaurantBranch.objects.create(restaurant=other, name='O', slug='o')
        ocat = RoomCategory.objects.create(branch=ob, name='X', base_price_per_night=D('1'))
        ostay = RoomBooking.objects.create(room=Room.objects.create(branch=ob, category=ocat, room_number='1'), branch=ob, guest_name='Z',
                                           guest_email='z@x.com', guest_phone='1', check_in_date=timezone.localdate(),
                                           check_out_date=timezone.localdate() + datetime.timedelta(days=1), booking_status='CHECKED_IN')
        self.assertEqual(self.settle([{'method': 'room', 'booking_id': ostay.pk, 'amount': 1050}]).status_code, 400)
        self.assertFalse(FolioCharge.objects.exists())
