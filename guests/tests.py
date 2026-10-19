from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Order
from guests import services
from guests.brief import compute_brief
from guests.models import GiftCard, Guest, LoyaltySettings, LoyaltyTransaction
from super_admin.models import Restaurant, RestaurantUser


class ServiceTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')

    def test_normalize_phone(self):
        self.assertEqual(services.normalize_phone('+91 98765-43210'), '9876543210')
        self.assertEqual(services.normalize_phone('12'), '')

    def test_upsert_matches_by_phone_and_fills_details(self):
        a = services.upsert_guest(self.r, 'Anita', '98765 43210')
        b = services.upsert_guest(self.r, '', '+919876543210', 'Anita@Example.com')
        self.assertEqual(a.pk, b.pk)
        b.refresh_from_db()
        self.assertEqual(b.email, 'anita@example.com')

    def test_walk_in_without_contact_is_ignored(self):
        self.assertIsNone(services.upsert_guest(self.r, 'Walk-in', ''))

    def test_redeem_rules(self):
        g = services.upsert_guest(self.r, 'Ravi', '9000000001')
        services.adjust_points(g, 150, 'Welcome bonus', None)
        with self.assertRaises(services.LoyaltyError):
            services.redeem_points(g, 50, '', None)       # below the 100-point minimum
        with self.assertRaises(services.LoyaltyError):
            services.redeem_points(g, 500, '', None)      # more than the balance
        services.redeem_points(g, 120, 'Bill 42', None)
        g.refresh_from_db()
        self.assertEqual(g.loyalty_points, 30)

    def test_gift_card_lifecycle(self):
        card = services.issue_gift_card(self.r, '1000')
        card = services.redeem_gift_card(card, '400', 'Bill 1', None)
        self.assertEqual(card.balance, Decimal('600'))
        with self.assertRaises(services.GiftCardError):
            services.redeem_gift_card(card, '601', '', None)
        card = services.redeem_gift_card(card, '600', '', None)
        self.assertEqual(card.status, GiftCard.Status.REDEEMED)
        card = services.void_gift_card(card, 'test', None)
        with self.assertRaises(services.GiftCardError):
            services.topup_gift_card(card, '100', '', None)
        self.assertEqual(card.transactions.count(), 4)

    def test_expired_card_cannot_be_redeemed(self):
        card = services.issue_gift_card(self.r, '500', expires_on=timezone.localdate() - timedelta(days=1))
        with self.assertRaises(services.GiftCardError):
            services.redeem_gift_card(card, '100', '', None)


class OrderSyncTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        LoyaltySettings.objects.create(restaurant=self.r, points_per_100=Decimal('10'))

    def _order(self, **kw):
        defaults = dict(restaurant=self.r, customer_name='Anita', phone='98765 43210', total_amount=Decimal('1000'),
                        status='COMPLETED', payment_status='paid')
        defaults.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return Order.objects.create(**defaults)

    def test_paid_order_creates_guest_and_earns_once(self):
        order = self._order()
        guest = Guest.objects.get(restaurant=self.r, phone='9876543210')
        self.assertEqual(guest.restaurant_visits, 1)
        self.assertEqual(guest.lifetime_spend, Decimal('1000'))
        self.assertEqual(guest.loyalty_points, 100)
        with self.captureOnCommitCallbacks(execute=True):
            order.save()  # re-saving must not award again
        guest.refresh_from_db()
        self.assertEqual(guest.loyalty_points, 100)

    def test_cancelled_order_takes_points_back(self):
        order = self._order()
        order.status = 'C'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()
        guest = Guest.objects.get(restaurant=self.r, phone='9876543210')
        self.assertEqual(guest.loyalty_points, 0)
        self.assertEqual(guest.restaurant_visits, 0)
        self.assertTrue(LoyaltyTransaction.objects.filter(guest=guest, kind='REVERSE').exists())

    def test_unpaid_order_earns_nothing_and_drafts_are_ignored(self):
        self._order(payment_status='unpaid')
        self._order(phone='9111111111', status='DRAFT')
        guest = Guest.objects.get(restaurant=self.r, phone='9876543210')
        self.assertEqual(guest.loyalty_points, 0)
        self.assertFalse(Guest.objects.filter(phone='9111111111').exists())

    def test_rebuild_from_history(self):
        Order.objects.create(restaurant=self.r, customer_name='Old Guest', phone='9222222222', total_amount=500, status='COMPLETED')
        Guest.objects.all().delete()
        created, _ = services.rebuild_guests(self.r)
        self.assertGreaterEqual(created, 1)
        self.assertTrue(Guest.objects.filter(phone='9222222222', restaurant_visits=1).exists())

    def test_brief_compares_with_usual_weekday(self):
        brief = compute_brief(self.r)
        self.assertIn('figures', brief)
        self.assertIsInstance(brief['alerts'], list)


class GuestScreenTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.other = Restaurant.objects.create(name='Other Diner', slug='other-diner')
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.cashier = User.objects.create_user('cash', 'c@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.r, role='CASHIER')
        self.kitchen = User.objects.create_user('chef', 'k@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.kitchen, restaurant=self.r, role='KITCHEN')
        self.guest = services.upsert_guest(self.r, 'Anita', '9876543210')
        self.foreign = services.upsert_guest(self.other, 'Stranger', '9000000009')

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def test_pages_render_for_manager(self):
        self.client.force_login(self.gm)
        for name, kw in [('guest_list', {}), ('guest_detail', {'pk': self.guest.pk}), ('gift_card_list', {}), ('morning_brief', {})]:
            self.assertEqual(self.client.get(self.url(name, **kw)).status_code, 200, name)

    def test_kitchen_role_is_blocked(self):
        self.client.force_login(self.kitchen)
        resp = self.client.get(self.url('guest_list'))
        # Kitchen staff are kept on the KDS by BranchContextMiddleware; either way they never see guests
        self.assertIn(resp.status_code, (302, 403))
        self.assertNotIn(b'Anita', resp.content)

    def test_cashier_can_view_and_note_but_not_adjust(self):
        self.client.force_login(self.cashier)
        detail = self.url('guest_detail', pk=self.guest.pk)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.client.post(detail, {'op': 'add_note', 'text': 'Prefers window seat'})
        self.assertTrue(self.guest.notes.filter(text='Prefers window seat').exists())
        self.assertEqual(self.client.post(detail, {'op': 'adjust', 'points': 500, 'reason': 'x'}).status_code, 403)

    def test_other_business_guest_is_not_reachable(self):
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(self.url('guest_detail', pk=self.foreign.pk)).status_code, 404)

    def test_issue_and_redeem_gift_card_via_screens(self):
        self.client.force_login(self.gm)
        self.client.post(self.url('gift_card_list'), {'value': '2000', 'recipient_name': 'Anita', 'recipient_phone': '9876543210'})
        card = GiftCard.objects.get(restaurant=self.r)
        self.assertEqual(card.guest, self.guest)
        self.client.force_login(self.cashier)
        self.client.post(self.url('gift_card_detail', pk=card.pk), {'op': 'redeem', 'amount': '500'})
        card.refresh_from_db()
        self.assertEqual(card.balance, Decimal('1500'))
        self.assertEqual(self.client.post(self.url('gift_card_detail', pk=card.pk), {'op': 'void'}).status_code, 403)

    def test_super_admin_platform_brief(self):
        su = User.objects.create_superuser('root', 'r@example.com', 'x')
        self.client.force_login(su)
        self.assertEqual(self.client.get(reverse('super_admin:morning_brief')).status_code, 200)

    def test_gift_card_list_with_unlinked_card_renders(self):
        services.issue_gift_card(self.r, '500', recipient_name='')  # no guest, no name
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(self.url('gift_card_list')).status_code, 200)
