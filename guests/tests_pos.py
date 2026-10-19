import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import MenuItem, Order
from guests import pos, services
from guests.models import GiftCard, LoyaltySettings
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class PosTenderTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main')
        LoyaltySettings.objects.create(restaurant=self.r, points_per_100=Decimal('10'), point_value=Decimal('1'), min_redeem_points=50)
        # Price includes GST, so the bill is exactly ₹1,000 (the server works the total out itself)
        self.item = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Thali', price=Decimal('1000'), is_tax_inclusive=True)
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.client.force_login(self.gm)
        self.guest = services.upsert_guest(self.r, 'Anita', '9876543210')
        services.adjust_points(self.guest, 300, 'Welcome', None)
        self.card = services.issue_gift_card(self.r, '500')

    def settle(self, payments, total='1000'):
        body = {'action': 'settle_order', 'customer_name': 'Anita', 'phone_number': '9876543210',
                'items': [{'menu_item_id': self.item.pk, 'quantity': 1, 'unit_price': '1000'}],
                'subtotal': total, 'tax_amount': '0', 'round_off_amount': '0', 'grand_total': total,
                'payment_method': 'cash', 'payment_status': 'paid', 'payments': payments}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('restaurant_admin:order_create_manual', kwargs={'restaurant_slug': self.r.slug}),
                                    data=json.dumps(body), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_lookup(self):
        url = reverse('restaurant_admin:guest_pos_lookup', kwargs={'restaurant_slug': self.r.slug})
        d = self.client.get(url, {'phone': '+91 98765 43210', 'code': self.card.code.lower()}).json()
        self.assertEqual(d['guest']['points'], 300)
        self.assertTrue(d['card']['usable'])

    def test_points_and_card_pay_part_of_the_bill(self):
        resp = self.settle([{'method': 'points', 'points': 200, 'amount': 999},  # client amount is ignored
                            {'method': 'gift_card', 'code': self.card.code, 'amount': 300},
                            {'method': 'cash', 'amount': 500}])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['success'], resp.content)
        order = Order.objects.get(pk=resp.json()['order_id'])
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(Decimal(str(sum(p['amount'] for p in order.payments))), Decimal('1000'))
        self.guest.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual(self.card.balance, Decimal('200'))
        # 300 - 200 redeemed + 80 earned on the ₹800 not paid with points
        self.assertEqual(self.guest.loyalty_points, 180)

        # Re-saving the same order never redeems twice
        pos.apply_tenders(order, [{'method': 'points', 'points': 200, 'amount': 200.0, 'guest_id': self.guest.pk}])
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.loyalty_points, 180)

        # Cancelling gives the points and card balance back
        order.status = 'C'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()
        self.guest.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual(self.card.balance, Decimal('500'))
        self.assertEqual(self.guest.loyalty_points, 300)

    def test_rejects_overdraw_and_minimum(self):
        self.assertEqual(self.settle([{'method': 'points', 'points': 400, 'amount': 400}]).status_code, 400)
        self.assertEqual(self.settle([{'method': 'points', 'points': 20, 'amount': 20}]).status_code, 400)
        self.assertEqual(self.settle([{'method': 'gift_card', 'code': self.card.code, 'amount': 600}]).status_code, 400)
        self.assertEqual(self.settle([{'method': 'gift_card', 'code': 'NOPE-NOPE-NOPE', 'amount': 10}]).status_code, 400)
        self.assertFalse(Order.objects.exclude(status='DRAFT').exists())
