"""Import / export across Stock & Supply Chain."""
import csv
import datetime
import io
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import (DeliveryChallan, DeliveryChallanItem, InventoryCategory, InventoryItem, InventoryTransaction,
                         StockCount, StockCountLine)
from super_admin.models import ActivityLog, CustomRole, Restaurant, RestaurantBranch, RestaurantUser

B = '/dashboard/spice-co/'


def csv_file(rows, name='f.csv'):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return SimpleUploadedFile(name, buf.getvalue().encode('utf-8'), content_type='text/csv')


def sheet(resp, index=0):
    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    return wb, list(wb.worksheets[index].iter_rows(values_only=True))


class StockImportExportTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main')
        self.raw = InventoryCategory.objects.create(restaurant=self.r, branch=self.b, name='Raw')
        self.rice = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Basmati Rice', sku='RM-1', unit='kg',
                                                 quantity=40, reorder_level=10, cost_price=Decimal('95'), category=self.raw)
        self.oil = InventoryItem.objects.create(restaurant=self.r, branch=self.b, name='Sunflower Oil', sku='RM-2', unit='litre',
                                                quantity=5, cost_price=Decimal('150'))
        self.user = User.objects.create_user('admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='RESTAURANT_ADMIN')
        self.client.force_login(self.user)

    def imp(self, path, f, step='preview', mode='upsert'):
        return self.client.post(B + path, {'file': f, 'step': step, 'mode': mode})

    # stock items ----------------------------------------------------------------------------
    def test_item_export_follows_search_and_imports_back_unchanged(self):
        r = self.client.get(B + 'inventory/export/')
        _, rows = sheet(r)
        self.assertEqual(rows[0][:5], ('SKU', 'Name *', 'Category', 'Unit *', 'Stock'))
        self.assertEqual(len(rows), 3)
        _, rows = sheet(self.client.get(B + 'inventory/export/', {'q': 'oil'}))
        self.assertEqual([x[1] for x in rows[1:]], ['Sunflower Oil'])
        out = self.imp('inventory/import/', SimpleUploadedFile('s.xlsx', r.content)).json()
        self.assertEqual(out['summary'], {'create': 0, 'update': 0, 'unchanged': 2, 'skip': 0, 'error': 0})
        tpl = self.client.get(B + 'inventory/export/', {'template': 1})
        wb, rows = sheet(tpl)
        self.assertNotIn('GST used', rows[0])  # export-only columns are not in the template
        self.assertEqual(self.imp('inventory/import/', SimpleUploadedFile('t.xlsx', tpl.content)).json()['summary']['error'], 0)

    def test_item_import_creates_with_opening_stock_but_never_overwrites_stock(self):
        rows = [['SKU', 'Name', 'Category', 'Unit', 'Stock', 'Cost price', 'GST %', 'HSN/SAC'],
                ['RM-1', 'Basmati Rice', '', '', '999', '99', '', ''],          # cost update; stock ignored
                ['', 'Paneer', 'Raw > Dairy', 'Kgs', '8', '380', '5%', '0406'],  # new, subcategory, item GST
                ['', 'Ghee', 'Raw', 'jar', '1', '1', '', ''],                   # bad unit
                ['RM-2', 'Basmati Rice', '', 'kg', '', '', '', ''],             # SKU of another item
                ['', 'Salt', 'Raw', '', '2', '', '', '']]                       # unit missing
        out = self.imp('inventory/import/', csv_file(rows)).json()
        self.assertEqual(out['summary'], {'create': 1, 'update': 1, 'unchanged': 0, 'skip': 0, 'error': 3})
        self.assertEqual(out['new_categories'], ['Raw > Dairy'])
        rice = next(r for r in out['rows'] if r['name'] == 'Basmati Rice' and r['action'] == 'update')
        self.assertTrue(any('stock count' in w for w in rice['warnings']))
        errors = ' '.join(m for r in out['rows'] for m in r['messages'])
        self.assertIn('Unit “jar”', errors)
        self.assertIn('Unit is missing', errors)

        self.imp('inventory/import/', csv_file(rows), step='apply')
        self.rice.refresh_from_db()
        self.assertEqual((self.rice.quantity, self.rice.cost_price), (40, Decimal('99.00')))
        paneer = InventoryItem.objects.get(name='Paneer')
        self.assertEqual((paneer.unit, paneer.quantity, paneer.branch), ('kg', 8, self.b))
        self.assertEqual((str(paneer.category), paneer.tax_configuration, paneer.effective_gst_rate), ('Raw > Dairy', 'override', Decimal('5.00')))
        opening = InventoryTransaction.objects.get(inventory_item=paneer)
        self.assertEqual((opening.transaction_type, opening.delta, opening.note), ('R', 8, 'Opening stock (import)'))
        self.assertEqual(InventoryTransaction.objects.filter(inventory_item=self.rice).count(), 0)
        self.assertTrue(ActivityLog.objects.filter(action='IMPORT').exists())

    # stock categories -----------------------------------------------------------------------
    def test_category_import_with_parents_and_tax(self):
        rows = [['Category', 'Parent category', 'GST %', 'HSN/SAC', 'Taxability'],
                ['Dairy', 'Perishables', '5', '0401', 'Taxable'],
                ['Perishables', '', '', '', ''],
                ['Raw', '', '0', '', 'nil rated'],
                ['Bad', '', '7', '', '']]
        out = self.imp('inventory/categories/import/', csv_file(rows)).json()
        self.assertEqual(out['summary'], {'create': 2, 'update': 1, 'unchanged': 0, 'skip': 0, 'error': 1})
        self.imp('inventory/categories/import/', csv_file(rows), step='apply')
        dairy = InventoryCategory.objects.get(name='Dairy')
        self.assertEqual((dairy.parent.name, dairy.tax_configuration, dairy.default_gst_rate, dairy.default_hsn_code),
                         ('Perishables', 'override', Decimal('5.00'), '0401'))
        self.assertEqual(InventoryCategory.objects.filter(name='Perishables').count(), 1)
        self.raw.refresh_from_db()
        self.assertEqual((self.raw.taxability, self.raw.gst_rate), ('nil_rated', Decimal('0.00')))
        _, exp = sheet(self.client.get(B + 'inventory/categories/export/'))
        self.assertIn(('Dairy', 'Perishables', 5, '0401', 'Taxable', 0), exp)

    # wastage ----------------------------------------------------------------------------------
    def test_wastage_import_reduces_stock_and_logs_it(self):
        rows = [['SKU', 'Item', 'Quantity', 'Reason', 'Note'],
                ['RM-2', '', '2', 'Spoiled', 'rancid'],
                ['', 'basmati rice', '45', 'prep / trim waste', ''],
                ['', 'Saffron', '1', 'Spoiled', ''],
                ['RM-1', '', '1', 'Lost in the mail', '']]
        out = self.imp('inventory/wastage/import/', csv_file(rows)).json()
        self.assertEqual(out['summary'], {'create': 2, 'update': 0, 'unchanged': 0, 'skip': 0, 'error': 2})
        rice = next(r for r in out['rows'] if r['name'] == 'Basmati Rice')
        self.assertTrue(any('below zero' in w for w in rice['warnings']))
        self.imp('inventory/wastage/import/', csv_file(rows), step='apply')
        self.oil.refresh_from_db()
        self.rice.refresh_from_db()
        self.assertEqual((self.oil.quantity, self.rice.quantity), (3, -5))
        w = InventoryTransaction.objects.get(inventory_item=self.oil)
        self.assertEqual((w.transaction_type, w.delta, w.reason, w.note, w.performed_by), ('W', -2, 'SPOILED', 'rancid', self.user))
        _, exp = sheet(self.client.get(B + 'inventory/wastage/export/'))
        self.assertEqual(exp[0][:4], ('SKU', 'Item *', 'Quantity *', 'Reason *'))
        self.assertEqual(len(exp), 3)

    # stock counts -------------------------------------------------------------------------------
    def test_count_sheet_round_trip_fills_counted_without_posting(self):
        count = StockCount.objects.create(restaurant=self.r, branch=self.b, title='Week 1')
        StockCountLine.objects.create(count=count, item=self.rice, expected=40, unit_cost=95)
        StockCountLine.objects.create(count=count, item=self.oil, expected=5, unit_cost=150)
        url = f'inventory/counts/{count.pk}/'
        r = self.client.get(B + url + 'export/')
        wb, rows = sheet(r)
        ws = wb.worksheets[0]
        ws.cell(row=2, column=6, value=38)  # Basmati counted
        buf = io.BytesIO()
        wb.save(buf)
        out = self.imp(url + 'import/', SimpleUploadedFile('c.xlsx', buf.getvalue())).json()
        self.assertEqual(out['summary'], {'create': 0, 'update': 1, 'unchanged': 0, 'skip': 1, 'error': 0})
        self.imp(url + 'import/', csv_file([['Item', 'Counted'], ['Saffron', '1']]))  # not in the count
        self.assertIn('Not part of this count',
                      self.imp(url + 'import/', csv_file([['Item', 'Counted'], ['Saffron', '1']])).json()['rows'][0]['messages'])
        self.imp(url + 'import/', SimpleUploadedFile('c.xlsx', buf.getvalue()), step='apply')
        self.assertEqual(count.lines.get(item=self.rice).counted, 38)
        count.refresh_from_db()
        self.rice.refresh_from_db()
        self.assertEqual((count.status, self.rice.quantity), ('DRAFT', 40))  # nothing posted
        count.status = 'POSTED'
        count.save()
        self.assertIn('closed', self.imp(url + 'import/', SimpleUploadedFile('c.xlsx', buf.getvalue())).json()['error'])

    # export-only pages --------------------------------------------------------------------------
    def test_logs_and_challans_export(self):
        InventoryTransaction.objects.create(inventory_item=self.rice, transaction_type='P', delta=10, unit_cost=95)
        InventoryTransaction.objects.create(inventory_item=self.oil, transaction_type='W', delta=-1, reason='SPOILED')
        _, rows = sheet(self.client.get(B + 'inventory/logs/export/', {'type': 'P'}))
        self.assertEqual([(x[1], x[4], x[5]) for x in rows[1:]], [('Basmati Rice', 'Inward Purchase', 10)])
        ch = DeliveryChallan.objects.create(restaurant=self.r, challan_no='DC-1', dispatch_date=datetime.date(2026, 10, 1),
                                            from_branch=self.b, total_value=Decimal('950'))
        DeliveryChallanItem.objects.create(challan=ch, inventory_item=self.rice, quantity=10, unit='kg', unit_value=95, line_total=950)
        wb, rows = sheet(self.client.get(B + 'challans/export/'))
        self.assertEqual(wb.sheetnames[:2], ['Delivery Challans', 'Items'])
        self.assertEqual(rows[1][0], 'DC-1')
        self.assertEqual(list(wb['Items'].iter_rows(values_only=True))[1][2], 'Basmati Rice')
        body = self.client.get(B + 'challans/export/', {'format': 'csv'}).content.decode('utf-8-sig')
        self.assertIn('DC-1,2026-10-01,Basmati Rice', body)

    def test_pages_show_the_controls(self):
        count = StockCount.objects.create(restaurant=self.r, branch=self.b, title='C')
        for path, has_import in [('inventory/', True), ('inventory/categories/', True), ('inventory/logs/', False),
                                 ('challans/', False), ('inventory/wastage/', True), (f'inventory/counts/{count.pk}/', True)]:
            r = self.client.get(B + path)
            self.assertContains(r, 'mio-export', msg_prefix=path)
            (self.assertContains if has_import else self.assertNotContains)(r, 'id="mioModal"', msg_prefix=path)

    # access ---------------------------------------------------------------------------------------
    def test_view_only_role_can_export_but_not_import(self):
        role = CustomRole.objects.create(restaurant=self.r, name='Viewer', permissions={'inventory': 'view'})
        prof = self.user.restaurant_profile
        prof.custom_role = role
        prof.save()
        self.assertEqual(self.client.get(B + 'inventory/export/').status_code, 200)
        r = self.imp('inventory/wastage/import/', csv_file([['Item', 'Quantity', 'Reason'], ['Basmati Rice', '1', 'Spoiled']]),
                     step='apply')
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_other_restaurants_cannot_be_reached(self):
        other = Restaurant.objects.create(name='Other', slug='other-co')
        theirs = StockCount.objects.create(restaurant=other, title='Theirs')
        self.assertNotEqual(self.client.get('/dashboard/other-co/inventory/export/').status_code, 200)
        self.assertEqual(self.client.get(B + f'inventory/counts/{theirs.pk}/export/').status_code, 404)
        r = self.imp(f'inventory/counts/{theirs.pk}/import/', csv_file([['Item', 'Counted'], ['x', '1']]))
        self.assertNotEqual(r.status_code, 200)
