from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import get_resolver, reverse
from django.utils import timezone

from core.models import Order
from finance import pnl
from guests import growth
from guests.brief import compute_brief
from guests.models import Feedback
from hotel.models import Room, RoomBooking, RoomCategory
from super_admin import permissions as P
from super_admin.models import CustomRole, Restaurant, RestaurantBranch, RestaurantUser


class RegistryTests(TestCase):
    def test_every_admin_url_is_mapped_to_an_area(self):
        resolver = get_resolver()
        missing = []
        for ns in ('restaurant_admin', 'hotel_admin'):
            for pattern in resolver.namespace_dict[ns][1].url_patterns:
                name = getattr(pattern, 'name', None)
                if name and (ns, name) not in P.ALWAYS and P.area_for(ns, name) is None:
                    missing.append(f'{ns}:{name}')
        self.assertEqual(missing, [], 'Map these URLs to an area in super_admin/permissions.py')

    def test_base_role_follows_modules(self):
        self.assertEqual(P.base_role_for({'finance': 'manage'}), 'RESTAURANT_ADMIN')
        self.assertEqual(P.base_role_for({'front_desk': 'view'}), 'HOTEL_ADMIN')
        self.assertEqual(P.base_role_for({'pos': 'manage', 'rooms': 'view'}), 'GENERAL_MANAGER')

    def test_mutating_names(self):
        for name in ('menu_add', 'inventory_delete', 'booking_check_in', 'order_create_manual', 'pos_open_shift_api'):
            self.assertTrue(P.is_mutating(name), name)
        for name in ('menu_list', 'order_detail', 'reservation_settings', 'kitchen_kds_orders_api', 'profit_loss'):
            self.assertFalse(P.is_mutating(name), name)


class Base(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', has_hotel_module=True)
        self.gm = self.login('gm', 'GENERAL_MANAGER')
        self.ra = self.login('ra', 'RESTAURANT_ADMIN')
        self.cashier = self.login('cash', 'CASHIER')
        self.priya = self.login('priya', 'CASHIER')

    def login(self, username, role):
        u = User.objects.create_user(username, f'{username}@example.com', 'x', is_staff=True, first_name=username.title())
        RestaurantUser.objects.create(user=u, restaurant=self.r, role=role)
        return u

    def url(self, name, ns='restaurant_admin', **kw):
        return reverse(f'{ns}:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def give(self, user, perms, name='Custom'):
        role = CustomRole.objects.create(restaurant=self.r, name=name, permissions=perms)
        profile = user.restaurant_profile
        profile.custom_role = role
        profile.save()
        role.sync_members()
        user.refresh_from_db()
        return role


class EnforcementTests(Base):
    def test_finance_only_role(self):
        role = self.give(self.priya, {'finance': P.MANAGE, 'inventory': P.VIEW})
        self.priya.restaurant_profile.refresh_from_db()
        self.assertEqual(self.priya.restaurant_profile.role, 'RESTAURANT_ADMIN')
        self.assertFalse(self.priya.restaurant_profile.can_manage_menu)
        self.client.force_login(self.priya)
        page = self.client.get(self.url('expenses'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, self.url('menu_list'))           # hidden from the sidebar
        self.assertContains(page, self.url('inventory_list'))
        self.assertRedirects(self.client.get(self.url('menu_list')), self.url('inventory_list'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url('inventory_list')).status_code, 200)   # view
        self.assertEqual(self.client.post(self.url('inventory_add'), {'name': 'x'}).status_code, 403)  # view is read-only
        self.assertEqual(self.client.get(self.url('inventory_add')).status_code, 403)
        api = self.client.post(self.url('pos_draft_order_api'), '{}', content_type='application/json')
        self.assertEqual(api.status_code, 403)
        self.assertIn('Custom', api.json()['error'])
        self.assertIn(self.client.get(self.url('roles')).status_code, (302, 403))   # custom roles never manage roles

    def test_role_changes_apply_immediately(self):
        role = self.give(self.priya, {'finance': P.MANAGE})
        self.client.force_login(self.priya)
        self.assertEqual(self.client.get(self.url('sales_analytics')).status_code, 302)
        role.permissions = {**role.permissions, 'analytics': P.VIEW}
        role.save()
        role.sync_members()
        self.assertNotEqual(self.client.get(self.url('sales_analytics')).status_code, 302)

    def test_hotel_role_signs_into_pms(self):
        self.give(self.priya, {'front_desk': P.MANAGE, 'finance': P.VIEW})
        self.priya.restaurant_profile.refresh_from_db()
        self.assertEqual(self.priya.restaurant_profile.role, 'HOTEL_ADMIN')
        self.client.force_login(self.priya)
        self.assertEqual(self.client.get(self.url('dashboard', ns='hotel_admin')).status_code, 200)
        self.assertEqual(self.client.get(self.url('expenses', ns='hotel_admin')).status_code, 200)
        self.assertEqual(self.client.post(self.url('expenses', ns='hotel_admin'), {'op': 'add'}).status_code, 403)
        self.assertNotEqual(self.client.get(self.url('rooms', ns='hotel_admin')).status_code, 200)


class RoleScreenTests(Base):
    def test_create_assign_and_move_off(self):
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(self.url('roles')).status_code, 200)
        self.assertEqual(self.client.get(self.url('role_new') + '?preset=Accountant').status_code, 200)
        data = {'name': 'Accountant', 'perm_finance': 'manage', 'perm_analytics': 'view', 'perm_rooms': 'view'}
        self.client.post(self.url('role_new'), data)
        role = CustomRole.objects.get(name='Accountant')
        self.assertEqual(role.level('finance'), 'manage')
        self.assertEqual(role.base_role, 'GENERAL_MANAGER')   # rooms opened → both modules
        edit = self.url('role_edit', pk=role.pk)
        self.assertEqual(self.client.get(edit).status_code, 200)
        target = self.priya.restaurant_profile
        self.client.post(edit, {'op': 'assign', 'logins': [target.pk]})
        target.refresh_from_db()
        self.assertEqual((target.custom_role, target.role), (role, 'GENERAL_MANAGER'))
        self.assertEqual(self.client.post(edit, {'op': 'delete'}).status_code, 302)
        self.assertTrue(CustomRole.objects.filter(pk=role.pk).exists())  # still has members
        self.client.post(edit, {'op': 'remove', 'member': target.pk, 'new_role': 'CASHIER'})
        target.refresh_from_db()
        self.assertEqual((target.custom_role, target.role), (None, 'CASHIER'))
        self.client.post(edit, {'op': 'delete'})
        self.assertFalse(CustomRole.objects.filter(pk=role.pk).exists())

    def test_restaurant_admin_cannot_grant_hotel_areas_or_promote(self):
        self.client.force_login(self.ra)
        self.client.post(self.url('role_new'), {'name': 'Sneaky', 'perm_pos': 'manage', 'perm_front_desk': 'manage'})
        role = CustomRole.objects.get(name='Sneaky')
        self.assertEqual(role.level('front_desk'), 'none')
        self.assertEqual(role.base_role, 'RESTAURANT_ADMIN')
        gm_profile = self.gm.restaurant_profile
        self.client.post(self.url('role_edit', pk=role.pk), {'op': 'assign', 'logins': [gm_profile.pk, self.ra.restaurant_profile.pk]})
        gm_profile.refresh_from_db()
        self.assertIsNone(gm_profile.custom_role)           # cannot demote the GM
        self.assertIsNone(self.ra.restaurant_profile.custom_role)  # nor themselves

    def test_cashier_cannot_manage_roles(self):
        self.client.force_login(self.cashier)
        self.assertIn(self.client.get(self.url('roles')).status_code, (302, 403))


class ModuleSplitTests(Base):
    def setUp(self):
        super().setUp()
        cat = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=Decimal('4000'))
        room = Room.objects.create(category=cat, branch=self.b, room_number='101')
        today = timezone.localdate()
        RoomBooking.objects.create(room=room, branch=self.b, guest_name='Stay Guest', guest_email='s@example.com', guest_phone='9000000000',
                                   check_in_date=today, check_out_date=today + timedelta(days=2))
        RoomBooking.objects.create(room=room, branch=self.b, guest_name='Past', guest_email='p@example.com', guest_phone='9000000001',
                                   check_in_date=today - timedelta(days=3), check_out_date=today - timedelta(days=1), booking_status='CHECKED_OUT',
                                   total_room_charges=Decimal('8000'), taxes=Decimal('960'), grand_total=Decimal('8960'))
        Order.objects.create(restaurant=self.r, branch=self.b, customer_name='Diner', total_amount=Decimal('1050'), tax_amount=Decimal('50'),
                             status='COMPLETED', payment_status='paid')

    def test_brief_and_pnl_are_split(self):
        rest, hotel = compute_brief(self.r, module='restaurant'), compute_brief(self.r, module='hotel')
        self.assertFalse(any('arrival' in a['title'] for a in rest['alerts']))
        self.assertTrue(any('arrival' in a['title'] for a in hotel['alerts']))
        self.assertEqual(hotel['top_items'], [])
        start, end = timezone.localdate().replace(day=1) - timedelta(days=5), timezone.localdate()
        r_pnl, h_pnl, all_pnl = (pnl.compute_pnl(self.r, start, end, module=m) for m in ('restaurant', 'hotel', 'all'))
        self.assertEqual(r_pnl['revenue'], Decimal('1000'))
        self.assertEqual(h_pnl['revenue'], Decimal('8000'))
        self.assertEqual(all_pnl['revenue'], Decimal('9000'))

    def test_feedback_desks_are_split(self):
        growth.submit_feedback(self.r, self.b, {'rating': 2, 'area': 'stay', 'comment': 'AC broken'})
        growth.submit_feedback(self.r, self.b, {'rating': 2, 'area': 'dining', 'comment': 'Cold soup'})
        self.client.force_login(self.gm)
        rest = self.client.get(self.url('feedback_list') + '?status=').content.decode()
        hotel = self.client.get(self.url('feedback_list', ns='hotel_admin') + '?status=').content.decode()
        self.assertIn('Cold soup', rest)
        self.assertNotIn('AC broken', rest)
        self.assertIn('AC broken', hotel)
        self.assertNotIn('Cold soup', hotel)

    def test_integrations_are_split(self):
        from integrations.models import Connection
        self.client.force_login(self.gm)
        self.client.post(self.url('integrations'), {'op': 'add_channel', 'provider': 'channel_manager', 'branch': self.b.pk})
        self.assertFalse(Connection.objects.exists())   # hotel channels are added in the Hotel PMS
        self.client.post(self.url('integrations', ns='hotel_admin'), {'op': 'add_channel', 'provider': 'channel_manager', 'branch': self.b.pk})
        self.assertTrue(Connection.objects.filter(provider='channel_manager').exists())
        self.assertNotContains(self.client.get(self.url('integrations') + '?branch=all'), 'Hotel channel manager ·')
