"""Elite Zen purchase voucher: the page gets the live masters, and a saved voucher brings stock in at landed cost."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (InventoryItem, InventoryTransaction, Party, PurchaseLedgerAccount, PurchaseOrder,
                         VendorAdvance)
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal


class PurchaseZenTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Elite Zen', slug='elite-zen', state='Rajasthan')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='zen-main', state='Rajasthan')
        self.user = User.objects.create_user('mgr', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='GENERAL_MANAGER')
        self.client.force_login(self.user)
        self.atta = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Wheat Atta', unit='katta',
                                                 alias='aata atta', purchase_units=[{'u': 'kg', 'f': 0.02}], gst_rate=0)
        self.butter = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Butter', unit='kg', quantity=D('1.5'), tax_configuration='override',
                                                   gst_rate=5, track_expiry=True, shelf_life_days=180)
        self.party = Party.objects.create(restaurant=self.r, branch=self.b, name='Shree Ram Dairy', gstin='08AAKFS1234L1Z5',
                                          gst_registration_type='regular', credit_days=7, is_msme=True)
        self.adv = VendorAdvance.objects.create(restaurant=self.r, branch=self.b, party=self.party, amount=D('5000'),
                                                mode='cash', paid_at=timezone.now(), note='Advance')

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def boot(self, resp):
        html = resp.content.decode()
        start = html.index('<script id="zen-boot" type="application/json">') + len('<script id="zen-boot" type="application/json">')
        return json.loads(html[start:html.index('</script>', start)])

    def voucher(self, boot, atta_qty=10):
        cash = next(e for e in boot['ledger']['cash'])
        freight = next(e for e in boot['ledger']['charge'] if e['name'] == 'Freight Inward')
        atta_amt = D(1450 * atta_qty)
        items_taxable = atta_amt + D('1040')
        share_butter = (D('200') * D('1040') / items_taxable).quantize(D('0.01'))
        share_atta = D('200') - share_butter
        ctax = (share_butter * D('0.05')).quantize(D('0.01'))
        tax = D('52') + ctax
        cgst = (tax / 2).quantize(D('0.01'))
        gross = items_taxable + D('200') + tax
        grand = gross.quantize(D('1'))
        round_off = grand - gross
        credit = grand - D('10000')
        return {
            'purchaseNo': boot['nextPurchaseNo'], 'purchaseNoManual': False, 'partyId': f'P{self.party.id}',
            'supplier': self.party.name, 'billNo': 'SRD-2232', 'billDate': '2026-10-30', 'purchaseDate': '2026-10-31',
            'taxRegime': 'intra', 'ratesInclusive': False, 'itcClaimed': 0,
            'items': [
                {'itemId': f'I{self.atta.id}', 'name': 'Wheat Atta', 'qty': atta_qty, 'unit': 'katta', 'qtyInBase': atta_qty,
                 'rate': 1450, 'amount': float(atta_amt), 'taxable': float(atta_amt), 'gstRate': 0, 'cessRate': 0, 'tax': 0,
                 'charges': float(share_atta), 'chargesTax': 0, 'free': 1, 'freeUnit': 'katta', 'freeInBase': 1,
                 'stockQty': atta_qty + 1, 'cost': float(atta_amt + share_atta),
                 'landedRate': float(((atta_amt + share_atta) / (atta_qty + 1)).quantize(D('0.01'))), 'remark': ''},
                {'itemId': f'I{self.butter.id}', 'name': 'Butter', 'qty': 2.5, 'unit': 'kg', 'qtyInBase': 2.5, 'rate': 416,
                 'amount': 1040, 'taxable': 1040, 'gstRate': 5, 'cessRate': 0, 'tax': 52, 'charges': float(share_butter),
                 'chargesTax': float(ctax), 'free': 0, 'freeInBase': 0, 'stockQty': 2.5,
                 'cost': float(D('1092') + share_butter + ctax), 'landedRate': float(((D('1092') + share_butter + ctax) / D('2.5')).quantize(D('0.01'))),
                 'expiry': '2027-04-28', 'expiryEstimated': True, 'remark': ''},
            ],
            'extras': [{'ledgerId': freight['id'], 'name': 'Freight Inward', 'type': 'charge', 'stage': None, 'method': 'fixed',
                        'rate': None, 'base': None, 'amount': 200, 'appliesTo': 'all', 'gst': 'items', 'tax': float(ctax),
                        'split': [], 'inCost': True}],
            'totals': {'itemsGross': float(items_taxable), 'discountBeforeTax': 0, 'itemsTaxable': float(items_taxable),
                       'charges': 200, 'cgst': float(cgst), 'sgst': float(tax - cgst), 'igst': 0, 'cess': 0,
                       'discountAfterTax': 0, 'deposits': 0, 'roundOff': float(round_off), 'grandTotal': float(grand)},
            'payments': [
                {'mode': 'linked', 'amount': 5000, 'linkedPaymentId': f'U{self.adv.id}', 'ledgerId': f'U{self.adv.id}', 'ledger': 'Paid earlier'},
                {'mode': 'cash', 'amount': 5000, 'ledgerId': cash['id'], 'ledger': cash['name'], 'ref': None, 'date': '2026-10-31'},
            ],
            'paid': 10000,
            'credit': {'amount': float(credit), 'dueDays': 7, 'dueDate': '2026-11-07', 'interestPA': 0, 'lateFee': 0},
            'remarks': 'test',
        }

    def test_admin_menu_drawer(self):
        # The voucher has the drawer, pointing at the sidebar-only page
        resp = self.client.get(self.url('purchase_create'))
        self.assertContains(resp, 'id="zen-nav-handle"')
        self.assertContains(resp, self.url('purchase_sidebar_panel'))
        # That page is the real sidebar, framable by this site only, its links opening in the whole window
        panel = self.client.get(self.url('purchase_sidebar_panel'))
        self.assertEqual(panel.status_code, 200)
        self.assertEqual(panel['X-Frame-Options'], 'SAMEORIGIN')
        self.assertContains(panel, '<base target="_top">')
        self.assertContains(panel, 'class="sidebar"')
        self.assertContains(panel, self.url('purchase_list'))
        self.assertNotContains(panel, 'id="ntDrawer"')  # no notifications of its own

    def test_page_sends_live_masters(self):
        resp = self.client.get(self.url('purchase_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'purchase_zen/js/app.js')
        boot = self.boot(resp)
        party = boot['ledger']['party'][0]
        self.assertEqual((party['state'], party['type'], party['msme'], party['creditDays']), ('Rajasthan', 'regular', True, 7))
        atta = next(i for i in boot['ledger']['item'] if i['name'] == 'Wheat Atta')
        self.assertEqual(atta['units'], [{'u': 'katta', 'f': 1}, {'u': 'kg', 'f': 0.02}])
        butter = next(i for i in boot['ledger']['item'] if i['name'] == 'Butter')
        self.assertEqual((butter['expiry'], butter['life'], butter['gst']), (True, 180, 5.0))
        self.assertEqual(boot['company']['state'], 'Rajasthan')
        self.assertEqual(boot['unlinked'][0]['amount'], 5000.0)
        self.assertTrue(PurchaseLedgerAccount.objects.filter(restaurant=self.r, kind='charge', name='Trade Discount').exists())

    def test_save_brings_stock_in_at_landed_cost_and_links_advance(self):
        boot = self.boot(self.client.get(self.url('purchase_create')))
        inv = self.voucher(boot)
        bill = SimpleUploadedFile('bill.jpg', b'\xff\xd8\xff fake', content_type='image/jpeg')
        resp = self.client.post(self.url('purchase_create'), {'voucher': json.dumps(inv), 'snapshot': json.dumps({'party': {}}),
                                                             'attachments': bill}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content)
        po = PurchaseOrder.objects.get()
        self.assertEqual((po.payment_status, po.credit_amount, po.amount_paid), ('partial', D(str(inv['credit']['amount'])), D('10000')))
        self.assertEqual(po.items.count(), 2)
        self.assertEqual(po.charges.get().name, 'Freight Inward')
        self.assertEqual(po.attachments.count(), 1)
        self.atta.refresh_from_db()
        self.butter.refresh_from_db()
        self.assertEqual(self.atta.quantity, D('11'))          # 10 billed + 1 free
        self.assertEqual(self.butter.quantity, D('4'))         # 1.5 + 2.5 kg
        self.assertEqual(self.atta.cost_price, D(str(inv['items'][0]['landedRate'])))
        self.assertLess(self.atta.cost_price, D('1450'))       # free goods + freight: each katta costs less than billed
        self.assertEqual(self.adv.amount_left(), D('0'))
        self.assertEqual(InventoryTransaction.objects.filter(purchase_order=po, transaction_type='P').count(), 2)

        # The same supplier bill twice is refused
        resp = self.client.post(self.url('purchase_create'), {'voucher': json.dumps(inv), 'snapshot': 'null'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already entered', resp.json()['error'])

        # Edit: reopened as it was entered, and saving again replaces its stock instead of adding it twice
        page = self.client.get(self.url('purchase_voucher_edit', pk=po.pk))
        self.assertEqual(page.status_code, 200)
        eboot = self.boot(page)
        self.assertTrue(eboot['editing'])
        self.assertEqual(eboot['unlinked'][0]['amount'], 5000.0)   # its own link counts as still available
        inv2 = self.voucher(eboot, atta_qty=8)
        resp = self.client.post(self.url('purchase_voucher_edit', pk=po.pk), {'voucher': json.dumps(inv2), 'snapshot': '{}'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.atta.refresh_from_db()
        self.assertEqual(self.atta.quantity, D('9'))
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_tampered_totals_are_refused(self):
        boot = self.boot(self.client.get(self.url('purchase_create')))
        inv = self.voucher(boot)
        inv['totals']['grandTotal'] -= 500
        resp = self.client.post(self.url('purchase_create'), {'voucher': json.dumps(inv), 'snapshot': 'null'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PurchaseOrder.objects.exists())
        self.atta.refresh_from_db()
        self.assertEqual(self.atta.quantity, 0)

    def test_boot_reads_saved_bills_and_does_not_slow_down_with_more_bills(self):
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext
        from restaurant_admin import purchase_zen

        boot = self.boot(self.client.get(self.url('purchase_create')))
        inv = self.voucher(boot)
        inv['items'][1]['mrp'] = 520
        resp = self.client.post(self.url('purchase_create'), {'voucher': json.dumps(inv), 'snapshot': '{}'})
        self.assertEqual(resp.status_code, 200, resp.content)
        # An old-screen bill: all of it is owed while unpaid
        PurchaseOrder.objects.create(restaurant=self.r, branch=self.b, party=self.party, supplier_name=self.party.name,
                                     invoice_number='OLD-1', purchase_date=timezone.localdate(), grand_total=D('300'),
                                     payment_status='unpaid')

        def open_screen():
            request = RequestFactory().get(self.url('purchase_create'))
            request.user = self.user
            with CaptureQueriesContext(connection) as ctx:
                result = purchase_zen.build_boot(request, self.r, self.b)
            return result, len(ctx.captured_queries)

        boot, queries = open_screen()
        party = boot['ledger']['party'][0]
        self.assertEqual(party['lastBillNo'], 'OLD-1')
        self.assertEqual(party['balance'], float(D(str(inv['credit']['amount'])) + D('300')))
        self.assertEqual(boot['unlinked'], [])                        # the advance is fully used by the bill
        saved = next(s for s in boot['saved'] if s['billNo'] == 'SRD-2232')
        self.assertEqual(saved['items'], [{'itemId': f'I{self.butter.id}', 'mrp': 520.0}])

        # Many more bills and advances: the screen still opens in the same number of queries
        for n in range(30):
            PurchaseOrder.objects.create(restaurant=self.r, branch=self.b, party=self.party, supplier_name=self.party.name,
                                         invoice_number=f'B-{n}', purchase_date=timezone.localdate(), grand_total=D('10'),
                                         voucher_data={'items': [{'itemId': f'I{self.atta.id}', 'mrp': 5}]}, voucher_snapshot={})
            VendorAdvance.objects.create(restaurant=self.r, branch=self.b, party=self.party, amount=D('10'),
                                         mode='cash', paid_at=timezone.now())
        boot, more_queries = open_screen()
        self.assertEqual(more_queries, queries)
        self.assertEqual(len(boot['unlinked']), 30)

    def test_create_ledger_from_list(self):
        resp = self.client.post(self.url('purchase_zen_ledger_create'), json.dumps({'kind': 'party', 'name': 'Gupta Kirana'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['entry']['type'], 'unregistered')
        self.assertTrue(Party.objects.filter(name='Gupta Kirana', restaurant=self.r).exists())
