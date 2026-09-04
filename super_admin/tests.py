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
        self.order_item = OrderItem.objects.create(order=self.order, menu_item=self.menu_item, quantity=1, price=50.0)
        self.inv = InventoryItem.objects.create(restaurant=self.restaurant, branch=self.branch, name="Milk", current_stock=10.0, cost_per_unit=25.0)
        self.po = PurchaseOrder.objects.create(restaurant=self.restaurant, branch=self.branch, total_amount=100.0)
        self.po_item = PurchaseOrderItem.objects.create(purchase_order=self.po, inventory_item=self.inv, quantity=4, unit_price=25.0)

    def test_safe_delete_branch_success(self):
        branch_id = self.branch.id
        safe_delete_branch(self.branch)
        self.assertFalse(RestaurantBranch.objects.filter(id=branch_id).exists())
        self.assertFalse(Table.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(Zone.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(MenuItem.objects.filter(branch_id=branch_id).exists())
        self.assertFalse(InventoryItem.objects.filter(branch_id=branch_id).exists())
