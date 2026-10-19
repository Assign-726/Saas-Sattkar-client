from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from super_admin.activity import classify, purge_activity_logs
from super_admin.models import ActivityLog, Restaurant, RestaurantUser


def make_log(restaurant=None, days_ago=0, **kw):
    log = ActivityLog.objects.create(restaurant=restaurant, area=kw.pop('area', 'restaurant'),
                                     action=kw.pop('action', 'UPDATE'), message=kw.pop('message', 'Updated coupon'), **kw)
    if days_ago:
        ActivityLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return log


class ClassifyTests(TestCase):
    def test_url_names_map_to_actions(self):
        self.assertEqual(classify('coupon_delete'), ('DELETE', 'Deleted coupon'))
        self.assertEqual(classify('menu_item_create')[0], 'CREATE')
        self.assertEqual(classify('staff_edit')[0], 'UPDATE')
        self.assertEqual(classify('order_settle_api'), ('STATUS', 'Settled order payment'))
        self.assertEqual(classify('orders_export_csv')[0], 'EXPORT')


class RetentionTests(TestCase):
    def test_nothing_deleted_before_three_months(self):
        make_log(days_ago=80)
        make_log(days_ago=10)
        self.assertEqual(purge_activity_logs(), 0)
        self.assertEqual(ActivityLog.objects.count(), 2)

    def test_three_months_keeps_latest_month(self):
        make_log(days_ago=95)
        make_log(days_ago=50)
        recent = make_log(days_ago=5)
        self.assertEqual(purge_activity_logs(), 2)
        self.assertEqual(list(ActivityLog.objects.values_list('pk', flat=True)), [recent.pk])

    def test_force_cleans_older_than_a_month(self):
        make_log(days_ago=40)
        make_log(days_ago=2)
        self.assertEqual(purge_activity_logs(force=True), 1)


class LoggingCaptureTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.user = User.objects.create_user('gm', 'gm@example.com', 'pass12345', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.restaurant, role='GENERAL_MANAGER')

    def test_failed_sign_in_is_logged(self):
        self.client.login(username='gm', password='wrong')
        log = ActivityLog.objects.get(action='LOGIN_FAILED')
        self.assertEqual(log.outcome, 'failed')
        self.assertEqual(log.restaurant, self.restaurant)

    def test_sign_in_and_admin_change_are_logged(self):
        self.assertTrue(self.client.login(username='gm', password='pass12345'))
        self.assertTrue(ActivityLog.objects.filter(action='LOGIN', user=self.user).exists())
        url = reverse('restaurant_admin:coupon_add', kwargs={'restaurant_slug': self.restaurant.slug})
        self.client.post(url, {'code': 'WELCOME10', 'discount_type': 'P', 'value': '10', 'min_spend': '0'})
        log = ActivityLog.objects.filter(area='restaurant').latest('created_at')
        self.assertEqual(log.action, 'CREATE')
        self.assertEqual(log.restaurant, self.restaurant)
        self.assertIn('WELCOME10', log.message)

    def test_get_requests_are_not_logged(self):
        self.client.force_login(self.user)
        self.client.get(reverse('restaurant_admin:coupon_list', kwargs={'restaurant_slug': self.restaurant.slug}))
        self.assertFalse(ActivityLog.objects.filter(area='restaurant').exists())


class LogScreenTests(TestCase):
    def setUp(self):
        self.mine = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.other = Restaurant.objects.create(name='Other Diner', slug='other-diner')
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.mine, role='GENERAL_MANAGER')
        self.cashier = User.objects.create_user('cash', 'c@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.mine, role='CASHIER')
        self.url = reverse('restaurant_admin:activity_logs', kwargs={'restaurant_slug': self.mine.slug})

    def test_only_own_business_logs_are_listed(self):
        make_log(self.mine, message='Mine')
        make_log(self.other, message='Theirs')
        self.client.force_login(self.gm)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Mine', html)
        self.assertNotIn('Theirs', html)

    def test_cashier_is_forbidden(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_delete_one_selected_and_page(self):
        a, b, c = (make_log(self.mine) for _ in range(3))
        theirs = make_log(self.other)
        self.client.force_login(self.gm)
        self.client.post(self.url, {'op': 'delete_one', 'id': a.pk})
        self.assertFalse(ActivityLog.objects.filter(pk=a.pk).exists())
        # Another business's id in the payload is ignored
        self.client.post(self.url, {'op': 'delete_selected', 'ids': [b.pk, theirs.pk]})
        self.assertFalse(ActivityLog.objects.filter(pk=b.pk).exists())
        self.assertTrue(ActivityLog.objects.filter(pk=theirs.pk).exists())
        self.client.post(self.url, {'op': 'delete_page', 'ids': [c.pk]})
        self.assertFalse(ActivityLog.objects.filter(pk=c.pk).exists())
        # Each deletion leaves an audit entry behind
        self.assertEqual(ActivityLog.objects.filter(restaurant=self.mine, action='DELETE').count(), 3)

    def test_delete_filtered_requires_filter_and_respects_it(self):
        make_log(self.mine, action='LOGIN', area='auth', message='Signed in')
        keep = make_log(self.mine, action='UPDATE')
        self.client.force_login(self.gm)  # also records a real "Signed in" entry
        before = ActivityLog.objects.filter(restaurant=self.mine).count()
        self.client.post(self.url, {'op': 'delete_filtered', 'qs': ''})
        self.assertEqual(ActivityLog.objects.filter(restaurant=self.mine).count(), before)
        self.client.post(self.url, {'op': 'delete_filtered', 'qs': 'action=LOGIN'})
        self.assertFalse(ActivityLog.objects.filter(action='LOGIN', restaurant=self.mine).exists())
        self.assertTrue(ActivityLog.objects.filter(pk=keep.pk).exists())

    def test_csv_export(self):
        make_log(self.mine, message='Exported row')
        self.client.force_login(self.gm)
        resp = self.client.get(self.url + '?export=csv')
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('Exported row', resp.content.decode())

    def test_super_admin_sees_everything(self):
        make_log(self.mine, message='Mine')
        make_log(self.other, message='Theirs')
        su = User.objects.create_superuser('root', 'r@example.com', 'x')
        self.client.force_login(su)
        html = self.client.get(reverse('super_admin:activity_logs')).content.decode()
        self.assertIn('Mine', html)
        self.assertIn('Theirs', html)
