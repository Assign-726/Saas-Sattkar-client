from decimal import Decimal
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from core.models import Category, MenuItem, InventoryItem, InventoryCategory, Order, Table, Zone
from restaurant_admin.middleware import BranchContextMiddleware
from restaurant_admin.forms import MenuItemForm, CategoryForm, InventoryItemForm, InventoryCategoryForm, TableForm, ZoneForm, BulkTableForm


class BranchIsolationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.restaurant = Restaurant.objects.create(name="Multi Branch Resto", slug="multi-branch")
        self.branch1 = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Outlet East", slug="outlet-east")
        self.branch2 = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Outlet West", slug="outlet-west")

        self.user = User.objects.create_user(username="admin_user", password="password123", is_staff=True)
        self.profile = RestaurantUser.objects.create(user=self.user, restaurant=self.restaurant, is_primary_admin=True)

    def test_middleware_attaches_active_branch(self):
        request = self.factory.get('/admin/')
        request.user = self.user

        # Add session
        middleware = SessionMiddleware(get_response=lambda req: None)
        middleware.process_request(request)
        request.session.save()

        # Run BranchContextMiddleware
        branch_middleware = BranchContextMiddleware(get_response=lambda req: None)
        branch_middleware(request)

        self.assertEqual(request.restaurant, self.restaurant)
        self.assertIsNotNone(request.active_branch)
        self.assertEqual(request.active_branch.id, self.branch1.id)

    def test_form_stamps_branch_on_save(self):
        # MenuItemForm
        cat = Category.objects.create(name="Starters", restaurant=self.restaurant, branch=self.branch1)
        menu_form = MenuItemForm(
            data={'name': 'Spring Roll', 'price': '150.00', 'tax_type': 'GST_FOOD', 'category': cat.id, 'availability': True},
            restaurant=self.restaurant,
            branch=self.branch1
        )
        self.assertTrue(menu_form.is_valid(), menu_form.errors)
        item = menu_form.save()
        self.assertEqual(item.branch, self.branch1)

        # MenuItemForm (without category)
        menu_form_no_cat = MenuItemForm(
            data={'name': 'French Fries', 'price': '100.00', 'tax_type': 'GST_FOOD', 'category': '', 'availability': True},
            restaurant=self.restaurant,
            branch=self.branch1
        )
        self.assertTrue(menu_form_no_cat.is_valid(), menu_form_no_cat.errors)
        item_no_cat = menu_form_no_cat.save()
        self.assertIsNone(item_no_cat.category)

        # CategoryForm
        cat_form = CategoryForm(
            data={'name': 'Beverages', 'description': 'Drinks'},
            restaurant=self.restaurant,
            branch=self.branch2
        )
        self.assertTrue(cat_form.is_valid(), cat_form.errors)
        new_cat = cat_form.save()
        self.assertEqual(new_cat.branch, self.branch2)

        # InventoryCategoryForm
        inv_cat_form = InventoryCategoryForm(
            data={'name': 'Raw Grains', 'taxability_type': 'taxable', 'gst_rate': '5.00', 'hsn_sac_code': '1006'},
            restaurant=self.restaurant,
            branch=self.branch1
        )
        self.assertTrue(inv_cat_form.is_valid(), inv_cat_form.errors)
        inv_cat = inv_cat_form.save()
        self.assertEqual(inv_cat.branch, self.branch1)

        # InventoryItemForm (with category)
        inv_form = InventoryItemForm(
            data={
                'name': 'Rice 25kg', 'category': inv_cat.id, 'unit': 'kg',
                'cost_price': '1200.00', 'price_per_unit': '48.00', 'quantity': 10,
                'reorder_level': 5, 'taxability_type': 'taxable'
            },
            restaurant=self.restaurant,
            branch=self.branch1
        )
        self.assertTrue(inv_form.is_valid(), inv_form.errors)
        inv_item = inv_form.save()
        self.assertEqual(inv_item.branch, self.branch1)
        self.assertEqual(inv_item.category, inv_cat)

        # InventoryItemForm (without category)
        inv_form_no_cat = InventoryItemForm(
            data={
                'name': 'Salt 1kg', 'category': '', 'unit': 'kg',
                'cost_price': '20.00', 'quantity': 5,
                'reorder_level': 2, 'taxability_type': 'taxable'
            },
            restaurant=self.restaurant,
            branch=self.branch1
        )
        self.assertTrue(inv_form_no_cat.is_valid(), inv_form_no_cat.errors)
        inv_item_no_cat = inv_form_no_cat.save()
        self.assertIsNone(inv_item_no_cat.category)

    def test_cross_branch_data_isolation(self):
        # Create items in branch 1 and branch 2
        item_b1 = MenuItem.objects.create(name="Dish B1", price=100, restaurant=self.restaurant, branch=self.branch1)
        item_b2 = MenuItem.objects.create(name="Dish B2", price=200, restaurant=self.restaurant, branch=self.branch2)

        # Scoped queryset for Branch 1 should only return item_b1
        b1_items = MenuItem.objects.filter(restaurant=self.restaurant, branch=self.branch1)
        self.assertIn(item_b1, b1_items)
        self.assertNotIn(item_b2, b1_items)

    def test_bulk_menu_import_scopes_to_active_branch(self):
        import json
        self.client.force_login(self.user)
        session = self.client.session
        session[f'active_branch_{self.restaurant.id}'] = self.branch1.id
        session.save()

        payload = [
            {
                "name": "Paneer Tikka",
                "description": "Roasted spiced cottage cheese",
                "price": 260.00,
                "tax_type": "GST_FOOD",
                "category": "Tandoor Starters",
                "subcategory": "Veg Tandoor",
                "availability": True
            },
            {
                "name": "Cold Coffee",
                "description": "Chilled blended coffee",
                "price": 120.00,
                "tax_type": "GST_FOOD",
                "category": "Beverages",
                "availability": True
            }
        ]

        from django.urls import reverse
        url = reverse('restaurant_admin:menu_bulk_add', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.post(url, {'json_text': json.dumps(payload)})
        self.assertEqual(response.status_code, 302)

        # Verify items created and stamped with branch1
        item1 = MenuItem.objects.get(name="Paneer Tikka", restaurant=self.restaurant)
        self.assertEqual(item1.branch, self.branch1)
        self.assertEqual(item1.category.branch, self.branch1)
        self.assertEqual(item1.category.name, "Tandoor Starters")
        self.assertEqual(item1.subcategory.name, "Veg Tandoor")

        item2 = MenuItem.objects.get(name="Cold Coffee", restaurant=self.restaurant)
        self.assertEqual(item2.branch, self.branch1)
        self.assertEqual(item2.category.branch, self.branch1)

        # Verify visible in branch1 menu list
        list_url = reverse('restaurant_admin:menu_list', kwargs={'restaurant_slug': self.restaurant.slug})
        list_resp = self.client.get(list_url)
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Paneer Tikka")
        self.assertContains(list_resp, "Cold Coffee")

        # Switch to branch2 and verify items are NOT present
        session = self.client.session
        session[f'active_branch_{self.restaurant.id}'] = self.branch2.id
        session.save()

        list_resp_b2 = self.client.get(list_url)
        self.assertEqual(list_resp_b2.status_code, 200)
        self.assertNotContains(list_resp_b2, "Paneer Tikka")
        self.assertNotContains(list_resp_b2, "Cold Coffee")


from core.models import AdvanceReceipt, Party
from django.urls import reverse

class ReceiptTypeTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Receipt Test Resto", slug="receipt-test")
        self.branch1 = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Branch One", slug="branch-one")
        self.user = User.objects.create_user(username="receipt_admin", password="password123", is_staff=True)
        self.profile = RestaurantUser.objects.create(user=self.user, restaurant=self.restaurant, is_primary_admin=True)

    def test_model_default_receipt_type(self):
        rc = AdvanceReceipt.objects.create(
            restaurant=self.restaurant,
            customer_name="Test Client",
            event_date="2026-08-01",
            advance_amount=500.00
        )
        self.assertEqual(rc.receipt_type, AdvanceReceipt.ReceiptType.ON_ACCOUNT)

    def test_create_on_account_receipt(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['active_branch_id'] = self.branch1.id
        session.save()

        url = reverse('restaurant_admin:receipt_create_page', kwargs={'restaurant_slug': self.restaurant.slug})
        data = {
            'receipt_type': 'on_account',
            'receipt_no': 'REC-5001',
            'reference_number': 'INV-101',
            'receipt_date': '2026-07-30',
            'customer_name': 'John Doe',
            'phone': '9876543210',
            'advance_amount': '1500.00',
            'payment_method': 'upi',
            'notes': 'On account test deposit'
        }
        res = self.client.post(url, data, follow=True)
        self.assertEqual(res.status_code, 200)

        rc = AdvanceReceipt.objects.get(receipt_no='REC-5001')
        self.assertEqual(rc.receipt_type, 'on_account')
        self.assertEqual(rc.reference_number, 'INV-101')
        self.assertIsNone(rc.order)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(rc.branch, self.branch1)

    def test_create_advance_booking_receipt(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['active_branch_id'] = self.branch1.id
        session.save()

        url = reverse('restaurant_admin:receipt_create_page', kwargs={'restaurant_slug': self.restaurant.slug})
        data = {
            'receipt_type': 'advance',
            'receipt_no': 'REC-5002',
            'receipt_date': '2026-07-30',
            'customer_name': 'Alice Smith',
            'phone': '9123456789',
            'advance_amount': '2500.00',
            'payment_method': 'cash',
            'notes': 'Advance table reservation deposit'
        }
        res = self.client.post(url, data, follow=True)
        self.assertEqual(res.status_code, 200)

        rc = AdvanceReceipt.objects.get(receipt_no='REC-5002')
        self.assertEqual(rc.receipt_type, 'advance')
        self.assertIsNotNone(rc.order)
        self.assertEqual(rc.order.status, Order.OrderStatus.DRAFT)
        self.assertEqual(rc.order.branch, self.branch1)
        self.assertEqual(rc.branch, self.branch1)
        self.assertEqual(rc.reference_number, rc.order.order_number)
        self.assertEqual(rc.order.advance_amount_paid, 2500.00)

    def test_party_ledger_and_total_paid_with_advance_receipt(self):
        party = Party.objects.create(
            restaurant=self.restaurant,
            name="Ritik Jangid",
            phone="7742114641",
            party_type=Party.PartyType.CUSTOMER
        )
        rc = AdvanceReceipt.objects.create(
            restaurant=self.restaurant,
            branch=self.branch1,
            party=party,
            receipt_no="REC-001/26-27",
            customer_name="Ritik Jangid",
            phone="7742114641",
            event_date="2026-08-03",
            advance_amount=1000.00,
            status=AdvanceReceipt.ReceiptStatus.REDEEMED
        )
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch1,
            party=party,
            customer_name="Ritik Jangid",
            phone="7742114641",
            total_amount=1050.00,
            advance_amount_paid=1000.00,
            paid_amount=50.00,
            due_amount=0.00,
            payment_status=Order.PaymentStatus.PAID,
            advance_receipt=rc,
            status=Order.OrderStatus.SERVED
        )
        rc.order = order
        rc.save()

        self.assertEqual(party.total_invoiced, Decimal('1050.00'))
        self.assertEqual(party.total_paid, Decimal('1050.00'))
        self.assertEqual(party.current_balance, Decimal('0.00'))

        self.client.force_login(self.user)
        session = self.client.session
        session['active_branch_id'] = self.branch1.id
        session.save()

        url = reverse('restaurant_admin:party_ledger_detail', kwargs={'restaurant_slug': self.restaurant.slug, 'pk': party.id})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "1050.00")
        self.assertContains(res, "REC-001/26-27")



from core.models import PurchaseOrder, PurchaseOrderItem

class MultiLevelInheritanceTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Tax Inheritance Resto", slug="tax-inherit")
        
        # Parent Category overrides tax settings: 18% GST, HSN 0401, Taxable
        self.parent_cat = InventoryCategory.objects.create(
            restaurant=self.restaurant,
            name="Dairy Products",
            tax_configuration="override",
            default_gst_rate=18.00,
            default_hsn_code="0401",
            taxability="taxable"
        )
        
        # Subcategory inherits from Parent Category
        self.sub_cat = InventoryCategory.objects.create(
            restaurant=self.restaurant,
            name="Milk & Cheese",
            parent=self.parent_cat,
            tax_configuration="inherit"
        )

        # Item 1: Inherits from Subcategory (which inherits from Parent Category)
        self.item_inherited = InventoryItem.objects.create(
            restaurant=self.restaurant,
            name="Amul Milk 1L",
            category=self.sub_cat,
            tax_configuration="inherit"
        )

        # Item 2: Overrides category with 5% GST & HSN 0402
        self.item_override = InventoryItem.objects.create(
            restaurant=self.restaurant,
            name="Special Cheese",
            category=self.sub_cat,
            tax_configuration="override",
            gst_rate=5.00,
            hsn_sac_code="0402"
        )

        # Item 3: Standalone item (no category) inheriting from Company Default
        self.item_company_default = InventoryItem.objects.create(
            restaurant=self.restaurant,
            name="Raw Spice Pack",
            tax_configuration="inherit"
        )

    def test_multi_level_category_resolution(self):
        # Subcategory inherits 18% GST and HSN 0401 from parent category
        self.assertEqual(self.sub_cat.resolve_category_value('default_gst_rate'), 18.00)
        self.assertEqual(self.sub_cat.resolve_category_value('default_hsn_code'), "0401")

        # Inherited item resolves to parent category values (18% and 0401)
        self.assertEqual(self.item_inherited.get_effective_value('default_gst_rate'), 18.00)
        self.assertEqual(self.item_inherited.get_effective_value('default_hsn_code'), "0401")

    def test_item_override_resolution(self):
        # Item with override returns its own explicit values (5% and 0402)
        self.assertEqual(self.item_override.get_effective_value('default_gst_rate'), 5.00)
        self.assertEqual(self.item_override.get_effective_value('default_hsn_code'), "0402")

    def test_company_default_fallback(self):
        # Item with no category and inherit returns company defaults (0.00 GST, empty HSN)
        from decimal import Decimal
        self.assertEqual(self.item_company_default.get_effective_value('default_gst_rate'), Decimal('0.00'))
        self.assertEqual(self.item_company_default.get_effective_value('default_hsn_code'), '')

    def test_purchase_order_line_tax_persistence(self):
        po = PurchaseOrder.objects.create(
            restaurant=self.restaurant,
            supplier_name="Vendor X",
            invoice_number="INV-999",
            purchase_date="2026-07-31",
            subtotal=100.00,
            tax_amount=18.00,
            grand_total=118.00
        )
        
        # Save line item using resolved GST rate (18%)
        resolved_gst = self.item_inherited.get_effective_value('default_gst_rate')
        resolved_hsn = self.item_inherited.get_effective_value('default_hsn_code')
        
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            inventory_item=self.item_inherited,
            quantity=1,
            unit_cost=100.00,
            gst_rate=resolved_gst,
            hsn_code=resolved_hsn,
            line_total=100.00
        )

        # Mutate parent category later
        self.parent_cat.default_gst_rate = 28.00
        self.parent_cat.save()

        # Item effective value updates to 28%, but historical PurchaseOrderItem remains immutable at 18%
        self.assertEqual(self.item_inherited.get_effective_value('default_gst_rate'), 28.00)
        po_item.refresh_from_db()
        self.assertEqual(po_item.gst_rate, 18.00)


class TableBulkAddTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Taj Dining", slug="taj-dining")
        self.branch = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Main Outlet", slug="main-outlet")
        self.user = User.objects.create_user(username="taj_admin", password="password123", is_staff=True)
        self.profile = RestaurantUser.objects.create(user=self.user, restaurant=self.restaurant, branch=self.branch, is_primary_admin=True)
        self.zone = Zone.objects.create(restaurant=self.restaurant, branch=self.branch, name="First Floor")
        self.client.login(username="taj_admin", password="password123")

    def test_single_table_form_with_shape(self):
        form = TableForm(
            data={
                'name': 'F-101',
                'table_number': 101,
                'capacity': 6,
                'shape': 'square',
                'zone': self.zone.id,
                'is_active': True,
            },
            restaurant=self.restaurant,
            branch=self.branch
        )
        self.assertTrue(form.is_valid(), form.errors)
        table = form.save()
        self.assertEqual(table.name, 'F-101')
        self.assertEqual(table.shape, 'square')
        self.assertEqual(table.branch, self.branch)
        self.assertEqual(table.zone, self.zone)

    def test_bulk_table_creation_view(self):
        from django.urls import reverse
        url = reverse('restaurant_admin:table_add', kwargs={'restaurant_slug': self.restaurant.slug})

        # Post 5 tables with prefix "T-"
        response = self.client.post(url, {
            'form_type': 'bulk',
            'zone': self.zone.id,
            'prefix': 'T-',
            'start_number': 1,
            'count': 5,
            'capacity': 4,
            'shape': 'round',
        })
        self.assertEqual(response.status_code, 302)

        # Verify tables created
        created_tables = Table.objects.filter(restaurant=self.restaurant, zone=self.zone).order_by('table_number')
        self.assertEqual(created_tables.count(), 5)
        self.assertEqual(list(created_tables.values_list('name', flat=True)), ['T-1', 'T-2', 'T-3', 'T-4', 'T-5'])
        self.assertEqual(list(created_tables.values_list('table_number', flat=True)), [1, 2, 3, 4, 5])
        self.assertTrue(all(t.shape == 'round' for t in created_tables))
        self.assertTrue(all(t.branch == self.branch for t in created_tables))

    def test_bulk_table_duplicate_handling(self):
        from django.urls import reverse
        # Pre-create table 1
        Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            zone=self.zone,
            name='T-1',
            table_number=1,
            capacity=4
        )

        url = reverse('restaurant_admin:table_add', kwargs={'restaurant_slug': self.restaurant.slug})
        # Attempt to create tables 1, 2, 3 (collision should abort entire batch)
        response = self.client.post(url, {
            'form_type': 'bulk',
            'zone': self.zone.id,
            'prefix': 'T-',
            'start_number': 1,
            'count': 3,
            'capacity': 4,
            'shape': 'rectangle',
        })
        self.assertEqual(response.status_code, 302)

        # Batch aborted: only the pre-existing table remains
        tables = Table.objects.filter(restaurant=self.restaurant, zone=self.zone).order_by('table_number')
        self.assertEqual(tables.count(), 1)
        self.assertEqual(list(tables.values_list('table_number', flat=True)), [1])

    def test_zone_delete_protection_with_tables(self):
        from django.urls import reverse
        Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            zone=self.zone,
            name='T-50',
            table_number=50,
            capacity=4
        )
        url = reverse('restaurant_admin:zone_delete', kwargs={'restaurant_slug': self.restaurant.slug, 'pk': self.zone.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        # Zone must not be deleted because it has assigned tables
        self.assertTrue(Zone.objects.filter(pk=self.zone.pk).exists())

    def test_default_zone_delete_blocked(self):
        from django.urls import reverse
        default_zone = Zone.objects.create(restaurant=self.restaurant, branch=self.branch, name="Main Area")
        url = reverse('restaurant_admin:zone_delete', kwargs={'restaurant_slug': self.restaurant.slug, 'pk': default_zone.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        # Default zone must not be deleted
        self.assertTrue(Zone.objects.filter(pk=default_zone.pk).exists())

    def test_table_name_validation(self):
        # 1. Test max 8 characters enforcement
        form_too_long = TableForm(
            data={
                'name': 'LONGTABLE123',
                'capacity': 4,
                'zone': self.zone.id,
            },
            restaurant=self.restaurant,
            branch=self.branch
        )
        self.assertFalse(form_too_long.is_valid())
        self.assertIn('name', form_too_long.errors)

        # 2. Test case-insensitive duplicate check
        Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            zone=self.zone,
            name='VIP-1',
            table_number=10
        )
        form_dup = TableForm(
            data={
                'name': 'vip-1',
                'capacity': 4,
                'zone': self.zone.id,
            },
            restaurant=self.restaurant,
            branch=self.branch
        )
        self.assertFalse(form_dup.is_valid())
        self.assertIn('name', form_dup.errors)

    def test_floor_plan_dashboard_active_menu(self):
        from django.urls import reverse
        url = reverse('restaurant_admin:floor_plan_dashboard', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_menu'], 'floor_plan')
        self.assertIn('zones', response.context)

    def test_floor_plan_save_position_api(self):
        import json
        from django.urls import reverse
        table = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            zone=self.zone,
            name='T-99',
            table_number=99,
            capacity=4,
            is_placed=False
        )

        url = reverse('restaurant_admin:floor_plan_save_position_api', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.post(
            url,
            data=json.dumps({
                'table_id': table.id,
                'is_placed': True,
                'position_x': 144,
                'position_y': 240,
                'rotation': 90
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')

        table.refresh_from_db()
        self.assertTrue(table.is_placed)
        self.assertEqual(table.position_x, 144)
        self.assertEqual(table.position_y, 240)
        self.assertEqual(table.rotation, 90)


class StaffManagementTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Royal Palace", slug="royal-palace")
        self.branch = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Main Outlet", slug="main-outlet")
        self.admin_user = User.objects.create_user(username="manager1", password="Password123!", is_staff=True)
        self.admin_profile = RestaurantUser.objects.create(
            user=self.admin_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.RESTAURANT_ADMIN,
            is_primary_admin=True,
        )

    def test_staff_list_accessible_to_manager(self):
        self.client.force_login(self.admin_user)
        url = reverse('restaurant_admin:staff_list', kwargs={'restaurant_slug': self.restaurant.slug})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Staff & Sub-Roles Management")

    def test_manager_creates_cashier_staff(self):
        self.client.force_login(self.admin_user)
        url = reverse('restaurant_admin:staff_create', kwargs={'restaurant_slug': self.restaurant.slug})
        post_data = {
            'username': 'cashier_ramesh',
            'first_name': 'Ramesh',
            'last_name': 'Kumar',
            'password': 'CashierSecret123',
            'pos_pin': '4321',
            'role': RestaurantUser.Role.CASHIER,
            'branch_id': str(self.branch.id),
            'can_apply_discounts': 'on',
            'can_issue_refunds': 'on',
            # can_view_analytics omitted -> False
        }
        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 302)

        # Verify created user in DB
        created_user = User.objects.get(username='cashier_ramesh')
        self.assertTrue(created_user.is_staff)
        self.assertEqual(created_user.first_name, 'Ramesh')

        ru = RestaurantUser.objects.get(user=created_user)
        self.assertEqual(ru.role, RestaurantUser.Role.CASHIER)
        self.assertEqual(ru.pos_pin, '4321')
        self.assertTrue(ru.can_apply_discounts)
        self.assertFalse(ru.can_view_analytics)
        self.assertEqual(ru.created_by, self.admin_user)

    def test_cashier_is_restricted_from_analytics(self):
        cashier_user = User.objects.create_user(username="cashier1", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=cashier_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.CASHIER,
            can_view_analytics=False,
        )
        self.client.force_login(cashier_user)
        analytics_url = reverse('restaurant_admin:sales_analytics', kwargs={'restaurant_slug': self.restaurant.slug})
        resp = self.client.get(analytics_url)
        # Should be redirected to transaction desk
        self.assertEqual(resp.status_code, 302)
        expected_url = reverse('restaurant_admin:transaction_desk_create', kwargs={'restaurant_slug': self.restaurant.slug})
        self.assertEqual(resp.url, expected_url)

    def test_branch_admin_does_not_see_other_branch_staff_or_admins(self):
        # Create second branch and its users
        branch2 = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Second Outlet", slug="second-outlet")
        manager2 = User.objects.create_user(username="manager_branch2", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=manager2,
            restaurant=self.restaurant,
            branch=branch2,
            role=RestaurantUser.Role.RESTAURANT_ADMIN,
        )
        cashier2 = User.objects.create_user(username="cashier_branch2", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=cashier2,
            restaurant=self.restaurant,
            branch=branch2,
            role=RestaurantUser.Role.CASHIER,
        )

        # Login as manager1 (self.branch)
        self.client.force_login(self.admin_user)
        url = reverse('restaurant_admin:staff_list', kwargs={'restaurant_slug': self.restaurant.slug})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Should NOT contain other branch admin or other branch staff
        self.assertNotContains(resp, "manager_branch2")
        self.assertNotContains(resp, "cashier_branch2")
        # Should NOT contain self admin in the staff list
        self.assertNotContains(resp, "manager1")

        # available_branches in context should only contain self.branch
        self.assertEqual(list(resp.context['available_branches']), [self.branch])
        self.assertFalse(resp.context['is_gm'])

    def test_club_admin_can_access_staff_and_create_gate_scanner(self):
        club_user = User.objects.create_user(username="club_mgr", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=club_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.CLUB_ADMIN,
        )

        self.client.force_login(club_user)
        list_url = reverse('restaurant_admin:staff_list', kwargs={'restaurant_slug': self.restaurant.slug})
        resp = self.client.get(list_url)
        self.assertEqual(resp.status_code, 200)

        create_url = reverse('restaurant_admin:staff_create', kwargs={'restaurant_slug': self.restaurant.slug})
        resp_create_get = self.client.get(create_url)
        self.assertEqual(resp_create_get.status_code, 200)
        allowed_roles = [r[0] for r in resp_create_get.context['allowed_roles']]
        self.assertEqual(allowed_roles, [RestaurantUser.Role.GATE_SCANNER])

        # Create gate scanner
        post_data = {
            'username': 'scanner_ram',
            'first_name': 'Ram',
            'password': 'ScannerPass123!',
            'pos_pin': '9988',
            'role': RestaurantUser.Role.GATE_SCANNER,
            'branch_id': str(self.branch.id),
        }
        resp_post = self.client.post(create_url, post_data)
        self.assertEqual(resp_post.status_code, 302)

        created_scanner = RestaurantUser.objects.get(user__username='scanner_ram')
        self.assertEqual(created_scanner.role, RestaurantUser.Role.GATE_SCANNER)
        self.assertEqual(created_scanner.branch, self.branch)
        self.assertEqual(created_scanner.created_by, club_user)

    def test_hotel_admin_redirects_to_native_hotel_staff(self):
        hotel_user = User.objects.create_user(username="hotel_mgr", password="Password123!", is_staff=True)
        RestaurantUser.objects.create(
            user=hotel_user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.HOTEL_ADMIN,
        )

        self.client.force_login(hotel_user)
        list_url = reverse('restaurant_admin:staff_list', kwargs={'restaurant_slug': self.restaurant.slug})
        resp = self.client.get(list_url)
        # Should redirect to hotel_admin:staff_list
        self.assertEqual(resp.status_code, 302)
        expected_url = reverse('hotel_admin:staff_list', kwargs={'restaurant_slug': self.restaurant.slug})
        self.assertEqual(resp.url, expected_url)
