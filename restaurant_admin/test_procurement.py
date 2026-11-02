"""Purchase cycle: PO -> GRN -> bill -> debit note / payment, plus the purchase reports."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (GoodsReceiptNote, InventoryItem, InventoryTransaction, Party, ProcurementOrder, PurchaseOrder,
                         PurchaseReturn)
from finance.pnl import compute_pnl
from restaurant_admin import procurement as P
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class ProcurementBase(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice Route', slug='spice-route')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main', state='Rajasthan', gstin='08ABCDE1234F1Z5')
        self.user = User.objects.create_user('mgr', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='GENERAL_MANAGER')
        self.client.force_login(self.user)
        self.rice = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Rice', unit='kg', quantity=10, reorder_level=20,
                                                 cost_price=D('60'), gst_rate=D('5'))
        self.oil = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Oil', unit='ltr', quantity=50, reorder_level=5,
                                                cost_price=D('150'), gst_rate=D('18'))
        self.local = Party.objects.create(restaurant=self.r, branch=self.b, name='Jaipur Traders', gstin='08AAAAA0000A1Z1', state='Rajasthan')
        self.delhi = Party.objects.create(restaurant=self.r, branch=self.b, name='Delhi Foods', gstin='07BBBBB1111B1Z2', state='Delhi')

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def make_po(self, party=None, op='approve', **extra):
        data = {'party': (party or self.local).pk, 'order_date': timezone.localdate().isoformat(), 'credit_days': '15', 'op': op,
                'item': [self.rice.pk, self.oil.pk], 'quantity': ['100', '10'], 'rate': ['50', '140'],
                'discount_pct': ['10', '0'], 'gst_rate': ['5', '18'], **extra}
        self.client.post(self.url('purchase_order_new'), data)
        return ProcurementOrder.objects.latest('id')

    def receive(self, po, **rows):
        data = {'receipt_date': timezone.localdate().isoformat(), 'challan_no': 'CH-1'}
        for line in po.lines.all():
            got = rows.get(line.inventory_item.name, ('0', '0'))
            data[f'l{line.pk}_received'], data[f'l{line.pk}_rejected'] = got
        return self.client.post(self.url('purchase_grn_new', pk=po.pk), data)


class PurchaseOrderTests(ProcurementBase):
    def test_po_totals_tax_split_and_numbering(self):
        po = self.make_po()
        self.assertEqual(po.status, 'open')
        self.assertEqual(po.number, 'PORD-001')
        # Rice 100 x 50 less 10% = 4500 @5% ; Oil 10 x 140 = 1400 @18%
        self.assertEqual(po.subtotal, D('5900.00'))
        self.assertEqual(po.tax_amount, D('477.00'))
        self.assertEqual((po.cgst_amount, po.sgst_amount, po.igst_amount), (D('238.50'), D('238.50'), D('0')))
        self.assertEqual(po.grand_total, D('6377.00'))
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.quantity, 10)  # an order moves no stock
        page = self.client.get(self.url('purchase_order_detail', pk=po.pk))
        self.assertContains(page, 'PORD-001')
        self.assertContains(self.client.get(self.url('purchase_order_detail', pk=po.pk) + '?print=1'), 'PURCHASE ORDER')

    def test_interstate_supplier_gets_igst_and_rcm_excludes_tax(self):
        po = self.make_po(party=self.delhi, reverse_charge='1')
        self.assertTrue(po.is_interstate)
        self.assertEqual(po.igst_amount, D('477.00'))
        self.assertEqual(po.grand_total, D('5900.00'))

    def test_draft_needs_approval_before_receiving(self):
        po = self.make_po(op='draft')
        self.assertEqual(po.status, 'draft')
        self.receive(po, Rice=('10', '0'))
        self.assertFalse(GoodsReceiptNote.objects.exists())
        self.client.post(self.url('purchase_order_action', pk=po.pk), {'op': 'approve'})
        po.refresh_from_db()
        self.assertEqual((po.status, po.approved_by), ('open', self.user))

    def test_new_supplier_typed_in_creates_vendor(self):
        self.client.post(self.url('purchase_order_new'), {'party': '', 'supplier_name': 'Fresh Farms', 'op': 'draft',
                                                          'order_date': timezone.localdate().isoformat(),
                                                          'item': [self.rice.pk], 'quantity': ['5'], 'rate': ['55'], 'discount_pct': ['0'], 'gst_rate': ['5']})
        self.assertTrue(Party.objects.filter(name='Fresh Farms', party_type='vendor', restaurant=self.r).exists())

    def test_other_business_cannot_open_order(self):
        po = self.make_po()
        other = Restaurant.objects.create(name='Other', slug='other')
        u = User.objects.create_user('o', password='x', is_staff=True)
        RestaurantUser.objects.create(user=u, restaurant=other, role='GENERAL_MANAGER')
        self.client.force_login(u)
        resp = self.client.get(self.url('purchase_order_detail', pk=po.pk))
        self.assertNotEqual(resp.status_code, 200)


class ReceiptBillReturnTests(ProcurementBase):
    def test_full_cycle(self):
        po = self.make_po()
        # Part delivery: 60 rice arrive, 5 rejected at the gate
        self.receive(po, Rice=('60', '5'))
        grn = GoodsReceiptNote.objects.get()
        self.assertEqual(grn.number, 'GRN-001')
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.quantity, 65)  # 10 + 55 accepted
        self.assertEqual(self.rice.cost_price, D('45.00'))  # net of the 10% discount
        txn = InventoryTransaction.objects.get(goods_receipt=grn)
        self.assertEqual((txn.transaction_type, txn.delta), ('P', 55))
        po.refresh_from_db()
        self.assertEqual(po.status, 'partial')
        self.assertEqual(grn.value, D('2475.00'))
        # Over-receipt is refused
        self.receive(po, Rice=('50', '0'))
        self.assertEqual(GoodsReceiptNote.objects.count(), 1)

        # Book the supplier bill: no extra stock, due date from credit days, input GST split
        self.client.post(self.url('purchase_grn_bill', pk=grn.pk), {
            'invoice_number': 'JT/889', 'bill_date': grn.receipt_date.isoformat(), 'itc_eligible': '1', 'freight': '100',
            'payment_method': 'credit', f'rate_{grn.lines.get().pk}': '50'})
        bill = PurchaseOrder.objects.get()
        self.assertTrue(bill.stock_via_grn)
        self.assertEqual(bill.subtotal, D('2475.00'))
        self.assertEqual(bill.tax_amount, D('123.75'))
        self.assertEqual(bill.grand_total, D('2698.75'))
        self.assertEqual(bill.due_date, grn.receipt_date + datetime.timedelta(days=15))
        self.assertEqual(bill.payment_status, 'unpaid')
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.quantity, 65)
        grn.refresh_from_db()
        self.assertEqual((grn.status, grn.bill), ('billed', bill))
        self.assertEqual(InventoryTransaction.objects.get(goods_receipt=grn).purchase_order, bill)
        # Same supplier bill number cannot be booked twice
        self.receive(po, Rice=('5', '0'))
        grn2 = GoodsReceiptNote.objects.latest('id')
        self.client.post(self.url('purchase_grn_bill', pk=grn2.pk), {'invoice_number': 'jt/889', 'bill_date': grn2.receipt_date.isoformat()})
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertContains(self.client.get(self.url('purchase_invoice', pk=bill.pk)), 'Balance due')

        # Debit note for 5 kg; payable and stock drop
        line = bill.items.get()
        self.client.post(self.url('purchase_return_new', pk=bill.pk), {'reason': 'quality', 'return_date': timezone.localdate().isoformat(),
                                                                       f'qty_{line.pk}': '5'})
        ret = PurchaseReturn.objects.get()
        self.assertEqual((ret.number, ret.subtotal, ret.tax_amount, ret.grand_total), ('DN-001', D('225.00'), D('11.25'), D('236.25')))
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.quantity, 65)  # 65 + 5 (grn2) - 5 returned
        bill.refresh_from_db()
        self.assertEqual(bill.payment_status, 'partial')
        self.assertEqual(bill.balance_due, D('2462.50'))
        # Can't return more than is left
        self.client.post(self.url('purchase_return_new', pk=bill.pk), {f'qty_{line.pk}': '51'})
        self.assertEqual(PurchaseReturn.objects.count(), 1)

        # Pay part, then the rest
        self.client.post(self.url('purchase_bill_pay', pk=bill.pk), {'amount': '1000', 'method': 'upi', 'reference': 'UTR1'})
        bill.refresh_from_db()
        self.assertEqual((bill.paid_amount, bill.payment_status, bill.balance_due), (D('1000.00'), 'partial', D('1462.50')))
        self.client.post(self.url('purchase_bill_pay', pk=bill.pk), {'amount': '5000', 'method': 'upi'})  # more than due
        bill.refresh_from_db()
        self.assertEqual(bill.paid_amount, D('1000.00'))
        self.client.post(self.url('purchase_bill_pay', pk=bill.pk), {'amount': '1462.50', 'method': 'bank_transfer'})
        bill.refresh_from_db()
        self.assertEqual((bill.payment_status, bill.balance_due), ('paid', D('0.00')))
        self.local.refresh_from_db()
        self.assertEqual(self.local.current_balance, D('0'))

    def test_short_close_stops_outstanding(self):
        po = self.make_po()
        self.receive(po, Rice=('90', '0'), Oil=('10', '0'))
        self.assertEqual(len(P.outstanding_orders(self.r, self.b)), 1)
        self.client.post(self.url('purchase_order_action', pk=po.pk), {'op': 'cancel'})
        po.refresh_from_db()
        self.assertEqual(po.status, 'partial')  # goods received: cancel refused
        self.client.post(self.url('purchase_order_action', pk=po.pk), {'op': 'close', 'reason': 'Out of stock'})
        po.refresh_from_db()
        self.assertEqual(po.status, 'closed')
        self.assertEqual(P.outstanding_orders(self.r, self.b), [])

    def test_vendor_balance_and_pnl_net_of_returns(self):
        po = self.make_po()
        self.receive(po, Rice=('100', '0'), Oil=('10', '0'))
        grn = GoodsReceiptNote.objects.get()
        self.client.post(self.url('purchase_grn_bill', pk=grn.pk), {'invoice_number': 'B1', 'bill_date': grn.receipt_date.isoformat(),
                                                                    'itc_eligible': '1', 'freight': '50'})
        bill = PurchaseOrder.objects.get()
        self.assertEqual(bill.grand_total, D('6427.00'))
        self.local.refresh_from_db()
        self.assertEqual(self.local.current_balance, D('6427.00'))
        oil_line = bill.items.get(inventory_item=self.oil)
        self.client.post(self.url('purchase_return_new', pk=bill.pk), {'reason': 'damaged', f'qty_{oil_line.pk}': '2'})
        self.local.refresh_from_db()
        self.assertEqual(self.local.current_balance, D('6427.00') - D('330.40'))
        today = timezone.localdate()
        pnl = compute_pnl(self.r, today, today, branch=self.b, module='restaurant')
        self.assertEqual(pnl['purchases'], D('5900.00') + D('50') - D('280.00'))


class ReportTests(ProcurementBase):
    def test_reports_render_and_export(self):
        po = self.make_po()
        self.receive(po, Rice=('50', '2'))
        grn = GoodsReceiptNote.objects.get()
        for tab in ('outstanding', 'pending', 'payables', 'gst', 'scorecard', 'prices', 'reorder'):
            resp = self.client.get(self.url('purchase_reports') + f'?tab={tab}')
            self.assertEqual(resp.status_code, 200, tab)
        self.assertContains(self.client.get(self.url('purchase_reports') + '?tab=pending'), grn.number)
        csv_resp = self.client.get(self.url('purchase_reports') + '?tab=outstanding&export=csv')
        self.assertIn('Rice', csv_resp.content.decode())
        self.client.post(self.url('purchase_grn_bill', pk=grn.pk), {'invoice_number': 'B9', 'bill_date': grn.receipt_date.isoformat(), 'itc_eligible': '1'})
        slabs, summary = P.input_gst(self.r, self.b, timezone.localdate(), timezone.localdate())
        self.assertEqual(summary['eligible'], D('108.00'))  # 48 kg x 45 = 2160 @ 5%
        rows, buckets = P.payables(self.r, self.b)
        self.assertEqual(rows[0]['due'], D('2268.00'))
        for page in ('purchase_orders', 'purchase_grns', 'purchase_returns'):
            self.assertEqual(self.client.get(self.url(page)).status_code, 200)

    def test_reorder_suggestion_feeds_new_po(self):
        rows = P.reorder_suggestions(self.r, self.b)
        self.assertEqual([(x['item'].name, x['suggest']) for x in rows], [('Rice', D('30'))])
        page = self.client.get(self.url('purchase_order_new') + '?reorder=1')
        self.assertContains(page, '"quantity": "30.000"')
        self.make_po()  # 100 rice now on order
        self.assertEqual(P.reorder_suggestions(self.r, self.b), [])


class NumberingSettingsTests(ProcurementBase):
    def test_settings_page_offers_purchase_cycle_numbering(self):
        page = self.client.get(self.url('settings'))
        self.assertEqual(page.status_code, 200)
        for dt in ('purchase_order', 'grn', 'debit_note'):
            self.assertContains(page, f'name="seq_{dt}_prefix"')


class SearchableDropdownTests(ProcurementBase):
    def test_stock_pages_turn_on_searchable_dropdowns(self):
        for name in ('purchase_orders', 'purchase_order_new', 'purchase_reports', 'inventory_wastage'):
            self.assertContains(self.client.get(self.url(name)), 'data-smart-select="8"', msg_prefix=name)
        self.assertNotContains(self.client.get(self.url('settings')), 'data-smart-select=')
