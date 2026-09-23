from django.test import TestCase
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from core.models import Zone, Table, Category, MenuItem, Order, OrderItem, InventoryItem, PurchaseOrder, PurchaseOrderItem, DeliveryChallan, DeliveryChallanItem
from super_admin.forms import safe_delete_branch

class SafeDeleteBranchTest(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Test Restro", slug="test-restro")
        self.branch = RestaurantBranch.objects.create(restaurant=self.restaurant, name="Branch To Delete", slug="b-del")
        self.zone = Zone.objects.create(restaurant=self.restaurant, branch=self.branch, name="AC Zone")
        self.table = Table.objects.create(restaurant=self.restaurant, branch=self.branch, zone=self.zone, name="T1", table_number=101)
        self.cat = Category.objects.create(restaurant=self.restaurant, branch=self.branch, name="Beverages")
        self.menu_item = MenuItem.objects.create(restaurant=self.restaurant, branch=self.branch, category=self.cat, name="Lassi", price=50.0)
        self.order = Order.objects.create(restaurant=self.restaurant, branch=self.branch, table=self.table, total_amount=50.0)
        self.order_item = OrderItem.objects.create(order=self.order, menu_item=self.menu_item, quantity=1, unit_price=50.0)
        self.inv = InventoryItem.objects.create(restaurant=self.restaurant, branch=self.branch, name="Milk", quantity=10, cost_price=25.0)
        self.po = PurchaseOrder.objects.create(restaurant=self.restaurant, branch=self.branch, supplier_name="Dairy", invoice_number="INV-01", purchase_date="2026-09-01", grand_total=100.0)
        self.po_item = PurchaseOrderItem.objects.create(purchase_order=self.po, inventory_item=self.inv, quantity=4, unit_cost=25.0, line_total=100.0)

    def test_safe_delete_branch_success(self):
        branch_id = self.branch.id
        safe_delete_branch(self.branch)
        self.assertFalse(RestaurantBranch.objects.filter(id=branch_id).exists())
        self.assertFalse(Table.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(Zone.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(MenuItem.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(InventoryItem.objects.filter(branch_id=branch_id).exists())


class ValidationSpecTestCase(TestCase):
    def test_normalizers(self):
        from super_admin.forms import (
            normalize_pan_or_gstin,
            normalize_phone,
            normalize_digits_only,
            normalize_email,
            normalize_text_spaces,
            normalize_city,
        )

        # 1. PAN / GSTIN normalisation
        self.assertEqual(normalize_pan_or_gstin("  aabc u9603r1zn "), "AABCU9603R1ZN")

        # 2. Phone normalisation
        self.assertEqual(normalize_phone("+91 98765-43210"), "9876543210")
        self.assertEqual(normalize_phone("09876543210"), "9876543210")
        self.assertEqual(normalize_phone("(987) 654-3210"), "9876543210")

        # 3. Digits only
        self.assertEqual(normalize_digits_only(" 110-001 "), "110001")
        self.assertEqual(normalize_digits_only("FSSAI 10012011000123"), "10012011000123")

        # 4. Email normalisation
        self.assertEqual(normalize_email("  Admin@EXAMPLE.Com  "), "admin@example.com")

        # 5. Text spaces collapse
        self.assertEqual(normalize_text_spaces("  Grand   Royal   Cafe  "), "Grand Royal Cafe")

        # 6. City Title Case
        self.assertEqual(normalize_city("  new  delhi  "), "New Delhi")
        self.assertEqual(normalize_city("bengaluru-north"), "Bengaluru-North")

    def test_gstin_checksum_and_state_codes(self):
        from super_admin.forms import is_valid_gstin_checksum, get_gst_state_code, GST_STATE_CODE_MAP, REJECTED_STATE_CODES

        # Known valid GSTINs from validation-spec.md §5.7
        self.assertTrue(is_valid_gstin_checksum("27AABCU9603R1ZN"))
        self.assertTrue(is_valid_gstin_checksum("29AAAGM0289C1ZF"))
        # Invalid checksum
        self.assertFalse(is_valid_gstin_checksum("27AABCU9603R1Z0"))

        # State code lookup
        self.assertEqual(get_gst_state_code("Maharashtra"), "27")
        self.assertEqual(get_gst_state_code("Delhi"), "07")
        self.assertEqual(get_gst_state_code("Karnataka"), "29")
        for rejected in REJECTED_STATE_CODES:
            self.assertNotIn(rejected, GST_STATE_CODE_MAP)

    def test_pan_and_business_type_validation(self):
        from super_admin.forms import OnboardRestaurantForm

        # Mismatched 4th character: Business Type 'Sole Proprietorship' requires 'P', got 'C'
        form = OnboardRestaurantForm(data={
            "legal_name": "John Doe",
            "trade_name": "JD Cafe",
            "business_type": "Sole Proprietorship",
            "pan": "ABCDE1234C",  # 'C' is Company
            "restaurant_address_line_1": "123 Food Street",
            "restaurant_city": "Mumbai",
            "restaurant_state": "Maharashtra",
            "restaurant_pincode": "400001",
            "restaurant_email": "info@jdcafe.com",
            "restaurant_phone": "9876543210",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("pan", form.errors)

        # Mismatched Legal Name suffix vs PAN: 'Pvt Ltd' requires 'C'
        form_suffix = OnboardRestaurantForm(data={
            "legal_name": "Royal Hospitality Pvt Ltd",
            "trade_name": "Royal Treat",
            "business_type": "Sole Proprietorship",
            "pan": "ABCPD1234P",  # 'P' conflicts with Pvt Ltd suffix
            "restaurant_address_line_1": "123 Food Street",
            "restaurant_city": "Mumbai",
            "restaurant_state": "Maharashtra",
            "restaurant_pincode": "400001",
            "restaurant_email": "info@royal.com",
            "restaurant_phone": "9876543210",
        })
        self.assertFalse(form_suffix.is_valid())
        self.assertIn("legal_name", form_suffix.errors)

        # Invalid Pincode starting with 0
        form_pin = OnboardRestaurantForm(data={
            "legal_name": "John Doe",
            "business_type": "Sole Proprietorship",
            "pan": "ABCPD1234P",
            "restaurant_address_line_1": "123 Food Street",
            "restaurant_city": "Mumbai",
            "restaurant_state": "Maharashtra",
            "restaurant_pincode": "012345",  # Invalid
            "restaurant_email": "info@jdcafe.com",
            "restaurant_phone": "9876543210",
        })
        self.assertFalse(form_pin.is_valid())
        self.assertIn("restaurant_pincode", form_pin.errors)

        # Valid form
        form_valid = OnboardRestaurantForm(data={
            "legal_name": "John Doe",
            "trade_name": "JD Cafe",
            "business_type": "Sole Proprietorship",
            "pan": "ABCPD1234P",
            "restaurant_address_line_1": "123 Food Street",
            "restaurant_city": "Mumbai",
            "restaurant_state": "Maharashtra",
            "restaurant_pincode": "400001",
            "restaurant_email": "info@jdcafe.com",
            "restaurant_phone": "9876543210",
            "is_gst_registered": True,
        })
        self.assertTrue(form_valid.is_valid(), form_valid.errors)

    def test_clean_branches_data_validation(self):
        from super_admin.forms import OnboardRestaurantForm
        from django.core.exceptions import ValidationError

        form = OnboardRestaurantForm()
        company_pan = "AABCU9603R"
        company_defaults = {
            "address_line_1": "123 Main Road",
            "address_line_2": "",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
        }

        # 1. Invalid FSSAI (must be 14 digits starting with 1 or 2)
        branches_bad_fssai = [{
            "name": "Central Outlet",
            "outlet_type": "dine_in",
            "use_company_address": True,
            "fssai_license_no": "30012011000123",  # Starts with 3
            "gstin": "27AABCU9603R1ZN",
            "gst_registration_type": "R5",
        }]
        with self.assertRaises(ValidationError) as cm:
            form.clean_branches_data(branches_bad_fssai, company_pan, True, company_defaults)
        self.assertIn("FSSAI", str(cm.exception))

        # 2. GSTIN embedded PAN mismatch (§5.4)
        branches_bad_pan = [{
            "name": "Central Outlet",
            "outlet_type": "dine_in",
            "use_company_address": True,
            "fssai_license_no": "10012011000123",
            "gstin": "27XYZPA1234R1ZN",  # PAN does not match company_pan
            "gst_registration_type": "R5",
        }]
        with self.assertRaises(ValidationError) as cm:
            form.clean_branches_data(branches_bad_pan, company_pan, True, company_defaults)
        self.assertIn("embedded PAN", str(cm.exception))

        # 3. GSTIN state mismatch (§5.5)
        branches_bad_state = [{
            "name": "Central Outlet",
            "outlet_type": "dine_in",
            "use_company_address": False,
            "address_line_1": "456 Connaught Place",
            "city": "New Delhi",
            "state": "Delhi",  # Code 07
            "pincode": "110001",
            "fssai_license_no": "10012011000123",
            "gstin": "27AABCU9603R1ZN",  # Code 27 is Maharashtra
            "gst_registration_type": "R5",
        }]
        with self.assertRaises(ValidationError) as cm:
            form.clean_branches_data(branches_bad_state, company_pan, True, company_defaults)
        self.assertIn("state code", str(cm.exception))

        # 4. GSTIN Registration Type Uniformity (§5.9)
        branches_conflicting_reg = [
            {
                "name": "Bandra Dine-in",
                "outlet_type": "dine_in",
                "use_company_address": True,
                "fssai_license_no": "10012011000123",
                "gstin": "27AABCU9603R1ZN",
                "gst_registration_type": "R5",
            },
            {
                "name": "Andheri QSR",
                "outlet_type": "takeaway_qsr",
                "use_company_address": True,
                "fssai_license_no": "20012011000123",
                "gstin": "27AABCU9603R1ZN",
                "gst_registration_type": "CMP",  # Conflicting with R5!
            }
        ]
        with self.assertRaises(ValidationError) as cm:
            form.clean_branches_data(branches_conflicting_reg, company_pan, True, company_defaults)
        self.assertIn("conflicting Registration Types", str(cm.exception))

        # 5. Valid branches
        branches_valid = [
            {
                "name": "Bandra Dine-in",
                "outlet_type": "dine_in",
                "use_company_address": True,
                "fssai_license_no": "10012011000123",
                "gstin": "27AABCU9603R1ZN",
                "gst_registration_type": "R5",
            },
            {
                "name": "Andheri Cloud Kitchen",
                "outlet_type": "cloud_kitchen",
                "use_company_address": True,
                "fssai_license_no": "20012011000123",
                "gstin": "27AABCU9603R1ZN",
                "gst_registration_type": "R5",
            }
        ]
        cleaned = form.clean_branches_data(branches_valid, company_pan, True, company_defaults)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]['outlet_type'], 'dine_in')
        self.assertEqual(cleaned[1]['outlet_type'], 'cloud_kitchen')

    def test_form_save_full_pipeline(self):
        from super_admin.forms import OnboardRestaurantForm
        from super_admin.models import Restaurant, RestaurantBranch, GstRegistration

        form_data = {
            "legal_name": "Antigravity Gourmet Pvt Ltd",
            "trade_name": "Antigravity Kitchen",
            "business_type": "Private Limited",
            "pan": "AABCA1234C",
            "cin_llpin": "U55101MH2026PTC123456",
            "restaurant_address_line_1": "Level 5, Sky Tower",
            "restaurant_address_line_2": "BKC",
            "restaurant_city": "mumbai",
            "restaurant_state": "Maharashtra",
            "restaurant_pincode": "400051",
            "restaurant_email": "hello@antigravity.in",
            "restaurant_phone": "+91 98765-43210",
            "is_gst_registered": True,
            "restaurant_is_active": True,
        }
        form = OnboardRestaurantForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)

        branches_payload = [
            {
                "name": "BKC Dine In",
                "outlet_type": "dine_in",
                "use_company_address": True,
                "fssai_license_no": "10012011000123",
                "gstin": "27AABCA1234C1ZN",
                "gst_registration_type": "R5",
            },
            {
                "name": "Powai Express",
                "outlet_type": "takeaway_qsr",
                "use_company_address": False,
                "address_line_1": "Galleria Mall, Hiranandani",
                "address_line_2": "",
                "city": "mumbai",
                "state": "Maharashtra",
                "pincode": "400076",
                "phone": "9876543211",
                "fssai_license_no": "20012011000123",
                "gstin": "27AABCA1234C1ZN",
                "gst_registration_type": "R5",
            }
        ]

        restaurant = form.save(branches_data=branches_payload)
        self.assertIsNotNone(restaurant)
        self.assertEqual(restaurant.name, "Antigravity Kitchen")
        self.assertEqual(restaurant.legal_name, "Antigravity Gourmet Pvt Ltd")
        self.assertEqual(restaurant.pan, "AABCA1234C")
        self.assertEqual(restaurant.city, "Mumbai")  # Normalized title case
        self.assertEqual(restaurant.phone, "9876543210")  # Normalized phone

        # Verify branches created
        branches = RestaurantBranch.objects.filter(restaurant=restaurant).order_by('id')
        self.assertEqual(branches.count(), 2)
        b1, b2 = branches[0], branches[1]

        self.assertEqual(b1.name, "BKC Dine In")
        self.assertEqual(b1.outlet_type, "dine_in")
        self.assertEqual(b1.fssai_license_no, "10012011000123")

        self.assertEqual(b2.name, "Powai Express")
        self.assertEqual(b2.outlet_type, "takeaway_qsr")
        self.assertEqual(b2.fssai_license_no, "20012011000123")
        self.assertEqual(b2.city, "Mumbai")

        # Verify GST registration created
        gst_regs = GstRegistration.objects.filter(restaurant=restaurant)
        self.assertEqual(gst_regs.count(), 1)
        self.assertEqual(gst_regs.first().gstin, "27AABCA1234C1ZN")


class RoleAssignmentTestCase(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Royal Palace", slug="royal-palace")
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Palace Suites & Dine",
            slug="palace-suites",
            has_hotel_module=False
        )

    def test_super_admin_create_hotel_admin_auto_activates_module(self):
        from super_admin.forms import RestaurantAdminCreateForm
        form_data = {
            'username': 'hotel_manager_1',
            'email': 'hotel1@royalpalace.com',
            'password': 'Secr3tPassword123!',
            'restaurant': self.restaurant.id,
            'branch': self.branch.id,
            'role': RestaurantUser.Role.HOTEL_ADMIN,
            'is_primary_admin': False,
            'is_2fa_enabled': False,
        }
        form = RestaurantAdminCreateForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        ru = form.save()

        self.assertEqual(ru.role, RestaurantUser.Role.HOTEL_ADMIN)
        self.assertEqual(ru.user.role, 'HOTEL_ADMIN')
        self.branch.refresh_from_db()
        self.assertTrue(self.branch.has_hotel_module, "Assigning HOTEL_ADMIN should auto-activate branch hotel module")

    def test_super_admin_create_general_manager(self):
        from super_admin.forms import RestaurantAdminCreateForm
        form_data = {
            'username': 'gm_director',
            'email': 'gm@royalpalace.com',
            'password': 'Secr3tPassword123!',
            'restaurant': self.restaurant.id,
            'branch': '',
            'role': RestaurantUser.Role.GENERAL_MANAGER,
            'is_primary_admin': True,
            'is_2fa_enabled': False,
        }
        form = RestaurantAdminCreateForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        ru = form.save()

        self.assertEqual(ru.role, RestaurantUser.Role.GENERAL_MANAGER)
        self.assertEqual(ru.user.role, 'GENERAL_MANAGER')
        self.assertTrue(ru.is_primary_admin)

    def test_super_admin_update_admin_role(self):
        from super_admin.forms import RestaurantAdminCreateForm, RestaurantAdminUpdateForm
        from django.contrib.auth.models import User
        # First create as RESTAURANT_ADMIN
        cform = RestaurantAdminCreateForm(data={
            'username': 'staff_user',
            'email': 'staff@royalpalace.com',
            'password': 'Secr3tPassword123!',
            'restaurant': self.restaurant.id,
            'branch': self.branch.id,
            'role': RestaurantUser.Role.RESTAURANT_ADMIN,
            'is_primary_admin': False,
            'is_2fa_enabled': False,
        })
        self.assertTrue(cform.is_valid())
        ru = cform.save()
        self.assertEqual(ru.role, RestaurantUser.Role.RESTAURANT_ADMIN)

        # Update role to HOTEL_ADMIN
        uform = RestaurantAdminUpdateForm(data={
            'username': 'staff_user',
            'email': 'staff@royalpalace.com',
            'restaurant': self.restaurant.id,
            'branch': self.branch.id,
            'role': RestaurantUser.Role.HOTEL_ADMIN,
            'is_primary_admin': True,
            'is_2fa_enabled': False,
        }, instance=ru)
        self.assertTrue(uform.is_valid(), uform.errors)
        updated_ru = uform.save()

        self.assertEqual(updated_ru.role, RestaurantUser.Role.HOTEL_ADMIN)
        self.assertEqual(updated_ru.user.role, 'HOTEL_ADMIN')
        self.assertTrue(updated_ru.is_primary_admin)


