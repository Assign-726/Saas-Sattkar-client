from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import MenuItem, Order, OrderItem
from integrations.models import ExternalOrderLink
from restaurant_admin import dashboard_metrics as DM
from restaurant_admin.templatetags.restaurant_admin_extras import inr
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class DashboardTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Dash Kitchen', slug='dash-kitchen', is_active=True)
        self.b1 = RestaurantBranch.objects.create(restaurant=self.r, name='Central', slug='central')
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='Harbour', slug='harbour')
        self.admin = User.objects.create_user('dash_admin', password='x', is_staff=True, first_name='Asha')
        RestaurantUser.objects.create(user=self.admin, restaurant=self.r, role='RESTAURANT_ADMIN', is_primary_admin=True)
        self.dish = MenuItem.objects.create(restaurant=self.r, branch=self.b1, name='Dal Makhani', price=Decimal('300'), sku='D1')
        self.unsold = MenuItem.objects.create(restaurant=self.r, branch=self.b1, name='Kulfi', price=Decimal('120'), sku='K1')
        self.url = f'/dashboard/{self.r.slug}/'

    def order(self, branch, total, when=None, status='COMPLETED', otype='dine_in', phone='', pay='paid', qty=1):
        o = Order.objects.create(restaurant=self.r, branch=branch, customer_name='Guest', phone=phone, order_type=otype,
                                 status=status, payment_status=pay, payment_method='upi')
        OrderItem.objects.create(order=o, menu_item=self.dish, quantity=qty, unit_price=self.dish.price)
        Order.objects.filter(pk=o.pk).update(order_time=when or timezone.now(), total_amount=Decimal(total),
                                             tax_amount=Decimal(total) * Decimal('0.05'), paid_amount=Decimal(total) if pay == 'paid' else 0)
        return o

    def test_sales_exclude_cancelled_refunded_and_drafts(self):
        self.order(self.b1, 1000)
        self.order(self.b1, 500, status='C')
        self.order(self.b1, 700, pay='refunded')
        self.order(self.b1, 900, status='DRAFT')
        today = timezone.localdate()
        d = DM.compute(self.r, [self.b1], today, today)
        self.assertEqual(d['cur']['orders'], 1)
        self.assertEqual(d['cur']['gross'], Decimal('1000'))
        self.assertEqual(d['cancels']['cancelled'], 1)
        self.assertEqual(d['cancels']['refunded'], 1)
        self.assertEqual(d['menu']['unsold'], ['Kulfi'])

    def test_branch_scope_and_previous_period_delta(self):
        today = timezone.localdate()
        self.order(self.b1, 1200)
        self.order(self.b2, 5000)
        self.order(self.b1, 1000, when=timezone.now() - timedelta(days=7))
        d = DM.compute(self.r, [self.b1], today - timedelta(days=6), today)
        self.assertEqual(d['cur']['gross'], Decimal('1200'))
        self.assertEqual(d['prev']['gross'], Decimal('1000'))
        self.assertEqual(d['deltas']['sales'], 20.0)
        self.assertEqual(DM.compute(self.r, None, today, today)['cur']['gross'], Decimal('6200'))

    def test_partner_orders_are_their_own_channel(self):
        o = self.order(self.b1, 800, otype='delivery')
        ExternalOrderLink.objects.create(branch=self.b1, source='swiggy', external_id='S1', order=o, commission_amount=Decimal('176'))
        self.order(self.b1, 400, otype='takeaway')
        today = timezone.localdate()
        channels = {c['label']: c for c in DM.compute(self.r, [self.b1], today, today)['channels']}
        self.assertEqual(set(channels), {'Swiggy', 'Takeaway'})
        self.assertEqual(channels['Swiggy']['commission'], Decimal('176'))

    def test_returning_guests(self):
        self.order(self.b1, 300, phone='9800000001', when=timezone.now() - timedelta(days=40))
        self.order(self.b1, 300, phone='9800000001')
        self.order(self.b1, 300, phone='9800000002')
        today = timezone.localdate()
        g = DM.compute(self.r, [self.b1], today, today)['guests']
        self.assertEqual((g['unique'], g['returning'], g['new']), (2, 1, 1))

    def test_page_renders_for_every_period_and_scope(self):
        self.order(self.b1, 1000, phone='9800000003')
        self.client.force_login(self.admin)
        for q in ['', '?period=last_7', '?period=this_year', '?period=custom&from=2026-01-01&to=2026-01-31', '?period=custom&from=bad',
                  f'?branch_id={self.b2.id}', '?branch_id=all']:
            resp = self.client.get(self.url + q)
            self.assertEqual(resp.status_code, 200, q)
        self.assertContains(resp, 'All branches')
        self.assertContains(resp, 'Branches')  # comparison table in the all-branches view

    def test_empty_business_renders(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self.url + '?period=last_30')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No dishes sold in this period yet.')

    def test_csv_export(self):
        self.order(self.b1, 1000)
        self.client.force_login(self.admin)
        resp = self.client.get(self.url + f'?period=today&branch_id={self.b1.id}&export=csv')
        self.assertEqual(resp['Content-Type'], 'text/csv')
        self.assertIn('Total,1,1000.00', resp.content.decode())

    def test_branch_bound_login_cannot_widen_scope(self):
        self.order(self.b2, 9999)
        mgr = User.objects.create_user('dash_mgr', password='x', is_staff=True)
        RestaurantUser.objects.create(user=mgr, restaurant=self.r, role='RESTAURANT_ADMIN', branch=self.b1)
        self.client.force_login(mgr)
        resp = self.client.get(self.url + '?branch_id=all&period=today')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['dash_branch'], self.b1)
        self.assertEqual(resp.context['d']['cur']['gross'], Decimal('0'))
        resp = self.client.get(self.url + f'?branch_id={self.b2.id}&period=today')
        self.assertEqual(resp.context['dash_branch'], self.b1)

    def test_period_resolution(self):
        today = date(2026, 3, 15)
        self.assertEqual(DM.resolve_period('last_month', today=today)[2:], (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(DM.resolve_period('nonsense', today=today)[0], 'today')
        key, _, s, e = DM.resolve_period('custom', '2026-03-20', '2026-03-01', today=today)
        self.assertEqual((key, s, e), ('custom', date(2026, 3, 1), today))
        self.assertEqual(DM.previous_period(date(2026, 3, 1), date(2026, 3, 7)), (date(2026, 2, 22), date(2026, 2, 28)))

    def test_inr_grouping(self):
        self.assertEqual(inr(1234567), '12,34,567')
        self.assertEqual(inr(Decimal('999.6')), '1,000')
        self.assertEqual(inr(-45000), '-45,000')
        self.assertEqual(inr(None), '0')
