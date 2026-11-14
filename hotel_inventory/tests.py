import datetime
from decimal import Decimal
import threading
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase

from super_admin.models import Restaurant, RestaurantBranch
from hotel.models import Room, RoomCategory, RoomBooking, FolioCharge
from hotel import folio as F
from hotel_inventory import services as S
from hotel_inventory.models import (
    StoreCategory, Vendor, StoreItem, ItemUnit, Location, StockLevel, StockMove,
    PurchaseOrder, POLine, GoodsReceipt, GRNLine, VendorPayment,
    Requisition, RequisitionLine, StockAdjustment, StockCount, StockCountLine,
    MinibarProfile, LinenPar, LaundryManifest, LaundryManifestLine
)


class HotelInventoryTestCase(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Hotel Royal Palace", slug="royal-palace")
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Main Hotel Branch",
            slug="main-hotel",
            has_hotel_module=True,
            state="Rajasthan"
        )
        self.user = User.objects.create_user(username="storemanager", password="password123")
        self.main_store = Location.objects.create(
            branch=self.branch, name="Main Store", kind=Location.Kind.STORE
        )
        self.pantry = Location.objects.create(
            branch=self.branch, name="Floor 1 Pantry", kind=Location.Kind.PANTRY, parent=self.main_store
        )
        self.vendor = Vendor.objects.create(
            branch=self.branch, name="A-One Hotel Supplies", gstin="08AAAAA0000A1Z5", state="Rajasthan", payment_terms_days=30
        )
        self.soap = StoreItem.objects.create(
            branch=self.branch, name="Herbal Soap 30g", unit="pc", kind=StoreItem.Kind.CONSUMABLE,
            reorder_level=Decimal('50'), reorder_qty=Decimal('200'), preferred_vendor=self.vendor
        )
        self.water = StoreItem.objects.create(
            branch=self.branch, name="Mineral Water 500ml", unit="bottle", kind=StoreItem.Kind.CHARGEABLE,
            sale_price=Decimal('40.00'), reorder_level=Decimal('20'), preferred_vendor=self.vendor
        )
        self.towel = StoreItem.objects.create(
            branch=self.branch, name="Bath Towel Large", unit="pc", kind=StoreItem.Kind.CIRCULATING,
            reorder_level=Decimal('30'), preferred_vendor=self.vendor
        )


class TestCoreLedgerAndWAC(HotelInventoryTestCase):
    def test_opening_stock_and_wac_calculation(self):
        # 1. Opening move: 100 pcs at ₹10.00
        mv1 = S.move(
            self.branch, self.soap, Decimal('100'), move_type=StockMove.Type.OPENING,
            dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user
        )
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.on_hand(), Decimal('100.000'))
        self.assertEqual(self.soap.wac, Decimal('10.0000'))

        # 2. Inward Receipt: 100 pcs at ₹20.00
        # WAC should become: (100*10 + 100*20) / 200 = 3000 / 200 = ₹15.00
        mv2 = S.move(
            self.branch, self.soap, Decimal('100'), move_type=StockMove.Type.RECEIPT,
            dst=self.main_store, unit_cost=Decimal('20.00'), user=self.user
        )
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.on_hand(), Decimal('200.000'))
        self.assertEqual(self.soap.wac, Decimal('15.0000'))

        # 3. Transfer: 50 pcs from Main Store to Pantry
        # WAC must remain unchanged at ₹15.00
        mv3 = S.move(
            self.branch, self.soap, Decimal('50'), move_type=StockMove.Type.TRANSFER,
            src=self.main_store, dst=self.pantry, user=self.user
        )
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.wac, Decimal('15.0000'))
        lvl_store = StockLevel.objects.get(item=self.soap, location=self.main_store)
        lvl_pantry = StockLevel.objects.get(item=self.soap, location=self.pantry)
        self.assertEqual(lvl_store.quantity, Decimal('150.000'))
        self.assertEqual(lvl_pantry.quantity, Decimal('50.000'))

    def test_negative_stock_blocking_and_minibar_override(self):
        # Stock is 0. Moving out should raise StockError
        with self.assertRaises(S.StockError):
            S.move(
                self.branch, self.soap, Decimal('10'), move_type=StockMove.Type.ISSUE,
                src=self.main_store, user=self.user
            )

        # Minibar allows negative stock so guest charges are never refused
        mv = S.move(
            self.branch, self.water, Decimal('2'), move_type=StockMove.Type.MINIBAR,
            src=self.main_store, allow_negative=True, user=self.user
        )
        lvl = StockLevel.objects.get(item=self.water, location=self.main_store)
        self.assertEqual(lvl.quantity, Decimal('-2.000'))

    def test_stock_move_immutability(self):
        mv = S.move(
            self.branch, self.soap, Decimal('50'), move_type=StockMove.Type.OPENING,
            dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user
        )
        with self.assertRaises(ValueError):
            mv.quantity = Decimal('60')
            mv.save()
        with self.assertRaises(ValueError):
            mv.delete()

    def test_reverse_move(self):
        mv = S.move(
            self.branch, self.soap, Decimal('50'), move_type=StockMove.Type.OPENING,
            dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user
        )
        self.assertEqual(self.soap.on_hand(), Decimal('50.000'))

        rev = S.reverse_move(mv, user=self.user, note="Undoing opening move")
        self.assertEqual(self.soap.on_hand(), Decimal('0.000'))
        self.assertEqual(rev.move_type, StockMove.Type.REVERSAL)
        self.assertEqual(rev.reverses, mv)


class TestProcurementWorkflows(HotelInventoryTestCase):
    def test_po_approval_and_grn_receipt_lifecycle(self):
        # 1. Create PO
        po = S.create_po(
            self.branch, self.vendor,
            lines_data=[
                {'item': self.soap, 'quantity': Decimal('100'), 'unit_price': Decimal('12.00'), 'tax_percent': Decimal('18')},
                {'item': self.water, 'quantity': Decimal('50'), 'unit_price': Decimal('15.00'), 'tax_percent': Decimal('18')}
            ],
            user=self.user, notes="Urgent hotel restocking"
        )
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.lines.count(), 2)

        # 2. Approve PO
        S.approve_po(po, user=self.user)
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)

        # 3. Receive GRN against PO
        line_soap = po.lines.get(item=self.soap)
        line_water = po.lines.get(item=self.water)
        grn = S.receive_grn(
            self.branch, vendor=self.vendor, location=self.main_store, po=po,
            invoice_number="INV-2026-001", invoice_date=datetime.date.today(),
            lines_data=[
                {'item': self.soap, 'po_line': line_soap, 'received_qty': Decimal('100'), 'unit_cost': Decimal('12.00'), 'tax_percent': Decimal('18'), 'batch_number': 'B101'},
                {'item': self.water, 'po_line': line_water, 'received_qty': Decimal('50'), 'unit_cost': Decimal('15.00'), 'tax_percent': Decimal('18'), 'batch_number': 'W202'}
            ],
            user=self.user
        )
        self.assertEqual(grn.status, GoodsReceipt.Status.RECEIVED)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(self.soap.on_hand(), Decimal('100.000'))
        self.assertEqual(self.water.on_hand(), Decimal('50.000'))

        # Check vendor payment & balance
        self.assertTrue(grn.total_amount > 0)
        self.assertEqual(grn.due_amount, grn.total_amount)

        # Record payment
        S.record_vendor_payment(self.branch, self.vendor, grn.total_amount, grn=grn, user=self.user)
        grn.refresh_from_db()
        self.assertTrue(grn.is_paid)


class TestInternalMovement(HotelInventoryTestCase):
    def test_requisition_and_issue_workflow(self):
        # Stock 200 soaps in main store
        S.move(self.branch, self.soap, Decimal('200'), move_type=StockMove.Type.OPENING, dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user)

        # Create Requisition
        req = S.create_requisition(
            self.branch, department=Location.Department.HOUSEKEEPING,
            from_location=self.main_store, to_location=self.pantry,
            lines_data=[{'item': self.soap, 'quantity': Decimal('30')}],
            user=self.user
        )
        self.assertEqual(req.status, Requisition.Status.DRAFT)

        # Submit & Approve
        S.submit_requisition(req, user=self.user)
        self.assertEqual(req.status, Requisition.Status.SUBMITTED)
        S.approve_requisition(req, user=self.user)
        self.assertEqual(req.status, Requisition.Status.APPROVED)

        # Issue
        S.issue_requisition(req, user=self.user)
        req.refresh_from_db()
        self.assertEqual(req.status, Requisition.Status.ISSUED)

        # Verify quantities moved
        lvl_store = StockLevel.objects.get(item=self.soap, location=self.main_store)
        lvl_pantry = StockLevel.objects.get(item=self.soap, location=self.pantry)
        self.assertEqual(lvl_store.quantity, Decimal('170.000'))
        self.assertEqual(lvl_pantry.quantity, Decimal('30.000'))

    def test_stock_adjustment_and_expense_sync(self):
        S.move(self.branch, self.soap, Decimal('50'), move_type=StockMove.Type.OPENING, dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user)
        adj = S.adjust(
            self.branch, item=self.soap, location=self.main_store, qty=Decimal('5'),
            reason=StockAdjustment.Reason.BREAKAGE, notes="Crushed during transport",
            user=self.user, post_to_expense=True
        )
        self.assertEqual(self.soap.on_hand(), Decimal('45.000'))
        self.assertIsNotNone(adj.expense)
        self.assertEqual(adj.expense.amount, Decimal('50.00'))  # 5 * 10.00 = 50.00

    def test_stock_count_variances(self):
        S.move(self.branch, self.soap, Decimal('100'), move_type=StockMove.Type.OPENING, dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user)
        cnt = S.create_stock_count(self.branch, self.main_store, user=self.user)
        line = cnt.lines.get(item=self.soap)
        self.assertEqual(line.expected_qty, Decimal('100.000'))

        # Count found 92 pcs (deficit of 8)
        S.post_stock_count(cnt, counts_dict={str(line.pk): Decimal('92')}, user=self.user)
        cnt.refresh_from_db()
        self.assertEqual(cnt.status, StockCount.Status.COMPLETED)
        self.assertEqual(self.soap.on_hand(), Decimal('92.000'))


class TestHotelSpecificWorkflows(HotelInventoryTestCase):
    def setUp(self):
        super().setUp()
        self.room_cat = RoomCategory.objects.create(
            branch=self.branch, name="Deluxe Sea View", base_price_per_night=Decimal('4500.00'), max_occupancy=2
        )
        self.room = Room.objects.create(
            branch=self.branch, category=self.room_cat, room_number="301", status=Room.RoomStatus.AVAILABLE
        )
        self.booking = RoomBooking.objects.create(
            branch=self.branch, room=self.room, guest_name="Mr. Sharma",
            check_in_date=datetime.date.today(), check_out_date=datetime.date.today() + datetime.timedelta(days=2),
            booking_status=RoomBooking.BookingStatus.CHECKED_IN
        )

    def test_minibar_folio_hook_charge_and_void(self):
        # Initial water stock in room minibar = 4
        minibar_loc, _ = Location.objects.get_or_create(
            branch=self.branch, kind=Location.Kind.MINIBAR, room=self.room, defaults={'name': 'Minibar 301'}
        )
        S.move(self.branch, self.water, Decimal('4'), move_type=StockMove.Type.OPENING, dst=minibar_loc, unit_cost=Decimal('15.00'), user=self.user)
        self.assertEqual(StockLevel.objects.get(item=self.water, location=minibar_loc).quantity, Decimal('4.000'))

        # Post Minibar Charge via Folio
        charge, err = F.post_charge(
            self.booking, self.user, kind=FolioCharge.Kind.MINIBAR,
            description="Mineral Water 500ml", unit_price=Decimal('40.00'), quantity=2,
            reference=str(self.water.pk)
        )
        self.assertIsNone(err)
        self.assertIsNotNone(charge)

        # Minibar stock should now be 4 - 2 = 2
        lvl = StockLevel.objects.get(item=self.water, location=minibar_loc)
        self.assertEqual(lvl.quantity, Decimal('2.000'))

        # Void charge
        void_err = F.void_charge(charge, self.user, reason="Charged by mistake")
        self.assertIsNone(void_err)

        # Minibar stock should be restored to 4
        lvl.refresh_from_db()
        self.assertEqual(lvl.quantity, Decimal('4.000'))

    def test_laundry_manifest_cycle(self):
        soiled = S.ensure_special_location(self.branch, Location.Kind.LINEN_SOILED, 'Linen — soiled')
        clean = S.ensure_special_location(self.branch, Location.Kind.LINEN_CLEAN, 'Linen — clean store')
        scrap = S.ensure_special_location(self.branch, Location.Kind.SCRAP, 'Scrap / retired')

        # Put 50 soiled towels in soiled store
        S.move(self.branch, self.towel, Decimal('50'), move_type=StockMove.Type.OPENING, dst=soiled, unit_cost=Decimal('200.00'), user=self.user)

        # 1. Send 50 towels to laundry
        outbound = S.send_linen_to_laundry(
            self.branch, self.vendor, lines_data=[{'item': self.towel, 'quantity': Decimal('50')}], user=self.user
        )
        self.assertEqual(outbound.status, LaundryManifest.Status.SENT)
        self.assertEqual(StockLevel.objects.get(item=self.towel, location=soiled).quantity, Decimal('0.000'))

        # 2. Receive from laundry: 45 clean, 3 damaged, 2 lost/missing
        inbound = S.receive_linen_from_laundry(
            self.branch, outbound, lines_data=[{
                'item': self.towel,
                'returned_qty': Decimal('45'),
                'damaged_qty': Decimal('3'),
                'missing_qty': Decimal('2')
            }], user=self.user
        )
        self.assertEqual(inbound.status, LaundryManifest.Status.RECEIVED)
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, LaundryManifest.Status.RECONCILED)

        # Check clean store got 45
        self.assertEqual(StockLevel.objects.get(item=self.towel, location=clean).quantity, Decimal('45.000'))
        # Check scrap got 3
        self.assertEqual(StockLevel.objects.get(item=self.towel, location=scrap).quantity, Decimal('3.000'))


class TestConcurrencyLocking(TransactionTestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Concurrency Hotel", slug="concurrency-hotel")
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant, name="Main", slug="main", has_hotel_module=True
        )
        self.user = User.objects.create_user(username="concurrent_user", password="pwd")
        self.main_store = Location.objects.create(branch=self.branch, name="Main Store", kind=Location.Kind.STORE)
        self.item = StoreItem.objects.create(branch=self.branch, name="Soap Bar", unit="pc")
        # Initialize stock with 100 pcs
        S.move(self.branch, self.item, Decimal('100'), move_type=StockMove.Type.OPENING, dst=self.main_store, unit_cost=Decimal('10.00'), user=self.user)

    def test_concurrent_moves_consistency(self):
        """10 threads concurrently issue 5 items each from 100 on-hand."""
        errors = []

        def worker():
            try:
                S.move(
                    self.branch, self.item, Decimal('5'), move_type=StockMove.Type.ISSUE,
                    src=self.main_store, user=self.user
                )
            except Exception as e:
                errors.append(e)
            finally:
                from django.db import connection
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        lvl = StockLevel.objects.get(item=self.item, location=self.main_store)
        # 100 - (10 * 5) = 50
        self.assertEqual(lvl.quantity, Decimal('50.000'))
