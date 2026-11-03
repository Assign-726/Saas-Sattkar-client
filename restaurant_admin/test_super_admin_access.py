"""A platform superuser opens any business's admin from the URL, without a business login of their own."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Category, SubCategory
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

PAGES = ['', 'orders/', 'orders/create/', 'kitchen/', 'kitchen/api/orders/', 'analytics/', 'categories/add/',
         'subcategories/', 'receipts/', 'inventory/logs/', 'floor-plan/', 'tables/', 'purchases/create/']


class SuperAdminAccessTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main',
                                                 has_restaurant_module=True, has_hotel_module=True)
        self.other = Restaurant.objects.create(name='Other', slug='other-co')
        self.ob = RestaurantBranch.objects.create(restaurant=self.other, name='Other Main', slug='other-main')
        self.root = User.objects.create_superuser('root', 'root@x.in', 'x')

    def test_superuser_opens_every_admin_page_by_branch_slug(self):
        self.client.force_login(self.root)
        for page in PAGES:
            resp = self.client.get(f'/dashboard/spice-main/{page}', follow=True)
            self.assertEqual(resp.status_code, 200, page)
        self.assertContains(self.client.get('/dashboard/spice-main/'), 'Super Admin')
        self.assertEqual(self.client.get('/hotel-admin/spice-main/').status_code, 200)
        self.assertEqual(self.client.get('/dashboard/other-main/orders/', follow=True).status_code, 200)

    def test_super_dashboard_links_to_each_branch_admin(self):
        self.client.force_login(self.root)
        resp = self.client.get('/super/')
        self.assertContains(resp, 'href="/dashboard/spice-main/"')
        self.assertContains(resp, 'href="/dashboard/spice-main/kitchen/"')
        self.assertContains(resp, 'href="/hotel-admin/spice-main/"')

    def test_business_users_stay_in_their_own_business(self):
        owner = User.objects.create_user('owner', password='x', is_staff=True)
        RestaurantUser.objects.create(user=owner, restaurant=self.r, role='RESTAURANT_ADMIN')
        self.client.force_login(owner)
        self.assertEqual(self.client.get('/dashboard/spice-main/orders/', follow=True).status_code, 200)
        self.assertNotContains(self.client.get('/dashboard/spice-main/'), 'Back to all businesses')
        for page in ('orders/', 'floor-plan/', 'kitchen/'):
            self.assertEqual(self.client.get(f'/dashboard/other-main/{page}').status_code, 403, page)
        self.assertEqual(self.client.get('/super/').status_code, 302)

    def test_subcategory_lookup_needs_a_login_and_stays_in_the_business(self):
        cat = Category.objects.create(restaurant=self.other, name='Drinks')
        SubCategory.objects.create(category=cat, name='Mocktails')
        url = f'/dashboard/spice-main/ajax/get-subcategories/?category_id={cat.pk}'
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(url).json(), [])
        self.assertEqual(self.client.get(f'/dashboard/other-main/ajax/get-subcategories/?category_id={cat.pk}').json()[0]['name'], 'Mocktails')
