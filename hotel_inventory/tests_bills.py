"""Purchase bills: billing deliveries, cost corrections, direct purchases, cancelling, payables and screens."""
import datetime
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from hotel_inventory import services as S
from hotel_inventory.models import GoodsReceipt, PurchaseBill, PurchaseOrder, StockLevel, StockMove, Vendor, VendorPayment
from hotel_inventory.tests_pages import PagesBase

D = Decimal


class BillBase(PagesBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        # soap: 300 pc on hand at ₹10 (opening). Deliver 2 cases (200 pc) at ₹1100/case = ₹11/pc → WAC 10.40
        self.grn = S.receive_grn(self.b, vendor=self.vendor, location=self.store, user=self.user,
                                 lines_data=[{'item': self.soap, 'unit_name': 'case', 'received_qty': 2, 'unit_cost': 1100}])
        self.gl = self.grn.lines.get()
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.wac, D('10.4000'))

    def bill(self, rate=1100, tax=18, **kw):
        kw.setdefault('invoice_number', 'AS-501')
        kw.setdefault('invoice_date', self.today)
        return S.post_bill(self.b, vendor=self.vendor, grns=[self.grn],
                           lines=[{'grn_line': self.gl, 'rate': rate, 'tax_percent': tax}], user=self.user, **kw)


class BillRules(BillBase):
    def test_bill_at_grn_rate_moves_the_payable_and_keeps_cost(self):
        bill = self.bill()
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.wac, D('10.4000'))
        self.assertEqual(bill.cost_difference, D('0'))
        self.assertEqual(bill.total_amount, D('2596.00'))  # 2200 + 18%
        self.assertEqual((bill.cgst_amount, bill.sgst_amount, bill.igst_amount), (D('198.00'), D('198.00'), D('0')))
        self.assertEqual(bill.due_date, self.today + datetime.timedelta(days=30))  # vendor's credit days
        self.grn.refresh_from_db()
        self.assertEqual(self.grn.bill, bill)
        self.assertEqual(self.grn.due_amount, D('0'))
        row = next(p for p in S.vendor_payables(self.b) if p['vendor'] == self.vendor)
        self.assertEqual(row['balance'], D('2596.00'))  # counted once — on the bill, not the GRN too
        self.assertEqual(row['unbilled_grns'], 0)

    def test_higher_bill_rate_and_delivery_raise_stock_cost_without_moving_stock(self):
        before = self.soap.on_hand()
        bill = self.bill(rate=1200, other_charges=100)
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.on_hand(), before)
        self.assertEqual(bill.cost_difference, D('300.00'))  # (2400 + 100) − 2200
        self.assertEqual(self.soap.wac, D('11.0000'))  # (500 × 10.40 + 300) / 500
        mv = StockMove.objects.get(ref_type='bill', ref_id=bill.pk)
        self.assertEqual((mv.move_type, mv.quantity, mv.value), (StockMove.Type.COST_ADJ, D('0'), D('300.00')))
        self.assertEqual(bill.total_amount, D('2932.00'))  # 2400 + 432 GST + 100

    def test_gst_that_cannot_be_claimed_is_cost(self):
        bill = self.bill(is_itc_eligible=False)
        self.assertEqual(bill.cost_difference, D('396.00'))
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.wac, D('11.1920'))  # (5200 + 396) / 500

    def test_interstate_vendor_gets_igst(self):
        Vendor.objects.filter(pk=self.vendor.pk).update(state='Gujarat')
        self.vendor.refresh_from_db()
        bill = self.bill()
        self.assertEqual((bill.cgst_amount, bill.igst_amount), (D('0'), D('396.00')))

    def test_same_bill_twice_and_double_billing_are_refused(self):
        self.bill()
        with self.assertRaisesMessage(S.StockError, 'already been entered'):
            self.bill()
        with self.assertRaisesMessage(S.StockError, 'already been billed'):
            self.bill(invoice_number='AS-502')

    def test_deliveries_must_match_the_vendor_and_be_billed_whole(self):
        other = Vendor.objects.create(branch=self.b, name='Other Co')
        with self.assertRaisesMessage(S.StockError, 'not Other Co'):
            S.post_bill(self.b, vendor=other, grns=[self.grn], invoice_number='X1', invoice_date=self.today,
                        lines=[{'grn_line': self.gl, 'rate': 1100}])
        g2 = S.receive_grn(self.b, vendor=self.vendor, location=self.store, lines_data=[
            {'item': self.soap, 'received_qty': 10, 'unit_cost': 11}, {'item': self.water, 'received_qty': 12, 'unit_cost': 15}])
        with self.assertRaisesMessage(S.StockError, 'must be on the bill'):
            S.post_bill(self.b, vendor=self.vendor, grns=[g2], invoice_number='X2', invoice_date=self.today,
                        lines=[{'grn_line': g2.lines.first(), 'rate': 11}])
        with self.assertRaisesMessage(S.StockError, 'future'):
            self.bill(invoice_date=self.today + datetime.timedelta(days=2))
        self.assertFalse(PurchaseBill.objects.exists(), 'a refused bill leaves nothing behind')

    def test_advance_paid_on_a_delivery_counts_towards_its_bill(self):
        S.record_vendor_payment(self.b, self.vendor, 1000, grn=self.grn, user=self.user)
        bill = self.bill()
        self.assertEqual(bill.paid_amount, D('1000'))
        self.assertEqual(bill.due_amount, D('1596.00'))
        with self.assertRaisesMessage(S.StockError, 'still due'):
            S.record_vendor_payment(self.b, self.vendor, 2000, bill=bill)
        S.record_vendor_payment(self.b, self.vendor, D('1596.00'), bill=bill)
        self.assertTrue(PurchaseBill.objects.get(pk=bill.pk).is_paid)

    def test_cancel_undoes_the_cost_and_releases_the_delivery(self):
        bill = self.bill(rate=1200)
        S.cancel_bill(bill, self.user, 'Wrong rate')
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.wac, D('10.4000'))
        self.grn.refresh_from_db()
        self.assertIsNone(self.grn.bill)
        again = self.bill(rate=1150)  # the same vendor bill number can be entered again after cancelling
        self.assertEqual(again.cost_difference, D('100.00'))

    def test_bill_with_payments_cannot_be_cancelled(self):
        bill = self.bill()
        S.record_vendor_payment(self.b, self.vendor, 100, bill=bill)
        with self.assertRaisesMessage(S.StockError, 'Payments have been recorded'):
            S.cancel_bill(bill, self.user, 'Oops')

    def test_direct_bill_receives_stock_and_spreads_delivery(self):
        bill = S.post_bill(self.b, vendor=self.vendor, location=self.pantry, invoice_number='CASH-9', invoice_date=self.today,
                           other_charges=60, user=self.user,
                           lines=[{'item': self.water, 'quantity': 24, 'rate': 15, 'tax_percent': 0},
                                  {'item': self.soap, 'quantity': 1, 'unit_name': 'case', 'rate': 1200, 'tax_percent': 18}])
        self.assertTrue(bill.is_direct)
        self.assertEqual(StockLevel.objects.get(item=self.water, location=self.pantry).quantity, D('24'))
        self.assertEqual(StockLevel.objects.get(item=self.soap, location=self.pantry).quantity, D('100'))
        shares = {l.item_id: l.charges_share for l in bill.lines.all()}
        self.assertEqual(shares[self.water.pk] + shares[self.soap.pk], D('60'))
        self.assertEqual(shares[self.water.pk], D('13.85'))  # 60 × 360 / 1560
        self.assertEqual(bill.cost_difference, D('60.00'))
        grn = bill.grns.get()
        self.assertEqual(grn.status, GoodsReceipt.Status.RECEIVED)
        S.cancel_bill(bill, self.user, 'Entered twice')
        self.assertEqual(StockLevel.objects.get(item=self.water, location=self.pantry).quantity, D('0'))
        grn.refresh_from_db()
        self.assertEqual(grn.status, GoodsReceipt.Status.CANCELLED)

    def test_gst_register_reads_bills_and_unbilled_deliveries(self):
        self.bill(rate=1200)
        S.receive_grn(self.b, vendor=self.vendor, location=self.store,
                      lines_data=[{'item': self.water, 'received_qty': 10, 'unit_cost': 15, 'tax_percent': 12}])
        rows, summary = S.gst_input_register(self.b)
        self.assertEqual(len(rows), 2)
        self.assertEqual(summary['total_tax'], D('432.00') + D('18.00'))


class BillScreens(BillBase):
    def test_screens_render(self):
        self.page('stores_bills')
        self.page('stores_bill_new')
        self.page('stores_bill_new', f'?vendor={self.vendor.pk}')
        self.page('stores_bill_new', f'?vendor={self.vendor.pk}&grn={self.grn.pk}')
        self.page('stores_bill_new', '?direct=1')
        bill = self.bill()
        self.page('stores_bill_detail', pk=bill.pk)
        self.page('stores_bills', '?show=unpaid')
        self.page('stores_grn_detail', pk=self.grn.pk)
        self.page('stores_grns')
        self.page('stores_payments')
        self.page('stores_ledger')
        self.page('stores_reports', '?tab=gst')
        S.cancel_bill(bill, self.user, 'Test')
        self.page('stores_bill_detail', pk=bill.pk)

    def test_post_bill_from_the_form_with_a_bill_copy(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            self._post_with_copy()

    def _post_with_copy(self):
        photo = SimpleUploadedFile('bill.jpg', b'\xff\xd8\xff\xe0fakejpeg', content_type='image/jpeg')
        _, m = self.post('stores_bill_save', {
            'direct': '0', 'vendor': self.vendor.pk, 'grn': [self.grn.pk], 'grn_line': [self.gl.pk], 'rate': ['1150'],
            'tax_percent': ['18'], 'invoice_number': 'AS-777', 'invoice_date': self.today.isoformat(), 'is_itc_eligible': 'on',
            'other_charges': '0', 'discount': '50', 'round_off': '0', 'attachment': photo})
        bill = PurchaseBill.objects.get(invoice_number='AS-777')
        self.assertTrue(bill.attachment.name.endswith('.jpg'), m)
        self.assertEqual(bill.cost_difference, D('50.00'))  # 2300 − 50 − 2200

    def test_direct_bill_from_the_form_and_pay_it(self):
        _, m = self.post('stores_bill_save', {
            'direct': '1', 'vendor': self.vendor.pk, 'location': self.store.pk, 'item_id': [self.water.pk], 'quantity': ['12'],
            'unit_name': ['bottle'], 'rate': ['16'], 'tax_percent': ['0'], 'invoice_number': 'MKT-1',
            'invoice_date': self.today.isoformat(), 'is_itc_eligible': 'on'})
        bill = PurchaseBill.objects.get(invoice_number='MKT-1')
        self.assertEqual(bill.total_amount, D('192.00'), m)
        self.post('stores_payment_save', {'vendor': self.vendor.pk, 'against': f'bill:{bill.pk}', 'amount': '192',
                                          'payment_method': 'CASH', 'payment_date': self.today.isoformat()})
        self.assertTrue(PurchaseBill.objects.get(pk=bill.pk).is_paid)

    def test_bad_form_input_reports_an_error_and_posts_nothing(self):
        _, m = self.post('stores_bill_save', {'direct': '1', 'vendor': self.vendor.pk, 'location': self.store.pk,
                                              'item_id': [self.water.pk], 'quantity': ['2'], 'unit_name': ['crate'],
                                              'rate': ['16'], 'tax_percent': ['0'], 'invoice_number': 'B-1',
                                              'invoice_date': self.today.isoformat()})
        self.assertTrue(any('not a unit' in x for x in m), m)
        bad = SimpleUploadedFile('bill.exe', b'MZ', content_type='application/octet-stream')
        _, m = self.post('stores_bill_save', {'direct': '0', 'vendor': self.vendor.pk, 'grn': [self.grn.pk], 'attachment': bad})
        self.assertTrue(any('photo or PDF' in x for x in m), m)
        self.assertFalse(PurchaseBill.objects.exists())

    def test_po_lines_prefill_data_reaches_the_grn_form(self):
        po = S.create_po(self.b, self.vendor, [{'item': self.water, 'quantity': 24, 'unit_price': 14}], user=self.user)
        S.approve_po(po, self.user)
        html = self.page('stores_grns')
        self.assertIn(f'"po_line": {po.lines.get().pk}', html)
        self.assertIn('name="po_line_id"', html)
        self.post('stores_grn_save', {'po': po.pk, 'vendor': self.vendor.pk, 'location': self.store.pk,
                                      'item_id': [self.water.pk], 'po_line_id': [po.lines.get().pk], 'received_qty': ['24'],
                                      'rejected_qty': ['0'], 'unit_cost': ['14'], 'unit_name': ['bottle']})
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(VendorPayment.objects.count(), 0)
