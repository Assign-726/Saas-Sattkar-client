"""Import / export of menu items and menu categories."""
import csv
import io
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import Category, MenuItem, SubCategory
from super_admin.models import ActivityLog, CustomRole, Restaurant, RestaurantBranch, RestaurantUser

M = '/dashboard/spice-main/menu/'
C = '/dashboard/spice-main/categories/'


def csv_file(rows, name='menu.csv'):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return SimpleUploadedFile(name, buf.getvalue().encode('utf-8'), content_type='text/csv')


class MenuImportExportTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main')
        self.starters = Category.objects.create(restaurant=self.r, branch=self.b, name='Starters')
        self.tandoor = SubCategory.objects.create(category=self.starters, name='Tandoor')
        self.tikka = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Paneer Tikka', short_code='101',
                                             sku='PAN-101', price=Decimal('299'), category=self.starters,
                                             subcategory=self.tandoor, allergens=['Dairy/Milk'],
                                             publishing_channels=['pos', 'qr'],
                                             variants_json=[{'type': 'Size', 'name': 'Half', 'price': 180.0}])
        self.soup = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Tomato Soup', short_code='102',
                                            price=Decimal('149'), category=self.starters, availability=False)
        self.user = User.objects.create_user('admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='RESTAURANT_ADMIN')
        self.client.force_login(self.user)

    def imp(self, url, f, step='preview', mode='upsert'):
        return self.client.post(url + 'import/', {'file': f, 'step': step, 'mode': mode})

    # export ------------------------------------------------------------------------------
    def test_export_excel_and_csv_follow_the_page_filters(self):
        r = self.client.get(M + 'export/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r['Content-Type'])
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0][:5], ('Code', 'Name *', 'Category *', 'Subcategory', 'Price *'))
        self.assertEqual(len(rows), 3)
        tikka = next(x for x in rows if x[1] == 'Paneer Tikka')
        self.assertIn('Size: Half = 180', tikka)
        r = self.client.get(M + 'export/', {'format': 'csv', 'status': 'unavailable'})
        self.assertIn('text/csv', r['Content-Type'])
        body = r.content.decode('utf-8-sig')
        self.assertIn('Tomato Soup', body)
        self.assertNotIn('Paneer Tikka', body)
        self.assertTrue(ActivityLog.objects.filter(action='EXPORT').exists())

    def test_template_has_samples_and_a_how_to_sheet(self):
        wb = openpyxl.load_workbook(io.BytesIO(self.client.get(M + 'export/', {'template': 1}).content))
        self.assertEqual(wb.sheetnames, ['Menu Items Template', 'How to import'])
        self.assertEqual(wb.worksheets[0].max_row, 3)  # headings + 2 sample rows
        # the samples import cleanly
        r = self.client.get(M + 'export/', {'template': 1})
        out = self.imp(M, SimpleUploadedFile('t.xlsx', r.content)).json()
        self.assertEqual(out['summary']['error'], 0)

    def test_exported_file_imports_back_with_no_changes(self):
        f = SimpleUploadedFile('m.xlsx', self.client.get(M + 'export/').content)
        out = self.imp(M, f).json()
        self.assertEqual(out['summary'], {'create': 0, 'update': 0, 'unchanged': 2, 'skip': 0, 'error': 0})

    # import ------------------------------------------------------------------------------
    def test_preview_saves_nothing_then_apply_creates_and_updates(self):
        rows = [['Code', 'Name', 'Category', 'Subcategory', 'Price', 'Food type', 'Allergens', 'Variants', 'Channels'],
                ['101', 'Paneer Tikka', '', '', '319', '', '', '', ''],                    # price update by code
                ['', 'Chicken 65', 'Non-Veg Starters', 'Fried', '299', 'Non-Veg', 'gluten, nuts', 'Size: Half = 180; Size: Full = 299', 'POS, Zomato'],
                ['', 'Bad Price', 'Starters', '', 'abc', '', '', '', ''],
                ['', 'chicken 65', 'X', '', '1', '', '', '', ''],                          # duplicate name in file
                ['', 'No Category', '', '', '99', '', '', '', '']]
        out = self.imp(M, csv_file(rows)).json()
        self.assertEqual(out['summary'], {'create': 1, 'update': 1, 'unchanged': 0, 'skip': 0, 'error': 3})
        self.assertEqual(out['new_categories'], ['Non-Veg Starters'])
        self.assertEqual(MenuItem.objects.count(), 2)  # preview saved nothing
        errors = {r['name']: r['messages'] for r in out['rows'] if r['action'] == 'error'}
        self.assertIn('not a number', errors['Bad Price'][0])
        self.assertIn('Same name as row 3', errors['chicken 65'][0])
        self.assertIn('Category is missing', errors['No Category'])

        out = self.imp(M, csv_file(rows), step='apply').json()
        self.assertEqual(out['counts'], {'create': 1, 'update': 1, 'categories': 1})
        self.tikka.refresh_from_db()
        self.assertEqual(self.tikka.price, Decimal('319.00'))
        # blank cells kept everything else
        self.assertEqual((self.tikka.category, self.tikka.subcategory, self.tikka.allergens), (self.starters, self.tandoor, ['Dairy/Milk']))
        c65 = MenuItem.objects.get(name='Chicken 65')
        self.assertEqual((c65.food_type, c65.branch, c65.category.name, c65.subcategory.name), ('non_veg', self.b, 'Non-Veg Starters', 'Fried'))
        self.assertEqual(c65.allergens, ['Gluten', 'Tree Nuts'])
        self.assertEqual(c65.publishing_channels, ['pos', 'online'])
        self.assertEqual(c65.variants_json, [{'type': 'Size', 'name': 'Half', 'price': 180.0}, {'type': 'Size', 'name': 'Full', 'price': 299.0}])
        self.assertTrue(c65.sku)
        self.assertTrue(ActivityLog.objects.filter(action='IMPORT').exists())

    def test_modes_and_code_conflicts(self):
        rows = [['Code', 'Name', 'Category', 'Price'], ['101', 'Paneer Tikka', 'Starters', '1'], ['', 'New Dish', 'Starters', '50']]
        self.assertEqual(self.imp(M, csv_file(rows), mode='create').json()['summary']['skip'], 1)
        self.assertEqual(self.imp(M, csv_file(rows), mode='update').json()['summary']['skip'], 1)
        clash = [['Code', 'Name', 'Category', 'Price'], ['102', 'Paneer Tikka', 'Starters', '1']]
        out = self.imp(M, csv_file(clash)).json()
        self.assertIn('belongs to “Tomato Soup”', out['rows'][0]['messages'][0])

    def test_alcohol_tax_is_refused_where_the_outlet_has_no_bar(self):
        rows = [['Name', 'Category', 'Price', 'Tax type', 'Food type', 'Jain'], ['Kingfisher', 'Bar', '250', 'VAT Beer', 'Egg', 'Yes']]
        out = self.imp(M, csv_file(rows)).json()
        warns = out['rows'][0]['warnings']
        self.assertTrue(any('does not serve alcohol' in w for w in warns))
        self.assertTrue(any('cannot be Jain' in w for w in warns))
        self.imp(M, csv_file(rows), step='apply')
        k = MenuItem.objects.get(name='Kingfisher')
        self.assertEqual((k.tax_type, k.is_jain), ('GST_FOOD', False))

    def test_bad_files_are_explained(self):
        self.assertIn('Excel (.xlsx) or CSV', self.imp(M, SimpleUploadedFile('m.pdf', b'x')).json()['error'])
        self.assertIn('column headings', self.imp(M, csv_file([['foo', 'bar'], ['1', '2']])).json()['error'])
        self.assertIn('no rows', self.imp(M, csv_file([['Name', 'Price']])).json()['error'])

    # categories ----------------------------------------------------------------------------
    def test_category_export_and_import(self):
        ws = openpyxl.load_workbook(io.BytesIO(self.client.get(C + 'export/').content)).worksheets[0]
        self.assertEqual(list(ws.iter_rows(values_only=True))[1], ('Starters', None, 'Tandoor', 2))
        rows = [['Category', 'Description', 'Subcategories'],
                ['starters', 'Hot starters', 'Tandoor; Fried'],
                ['Desserts', 'Sweet things', 'Indian; Western'],
                ['', 'x', '']]
        out = self.imp(C, csv_file(rows)).json()
        self.assertEqual(out['summary'], {'create': 1, 'update': 1, 'unchanged': 0, 'skip': 0, 'error': 1})
        out = self.imp(C, csv_file(rows), step='apply').json()
        self.assertEqual(out['counts'], {'create': 1, 'update': 1, 'subcategories': 3})
        self.starters.refresh_from_db()
        self.assertEqual(self.starters.description, 'Hot starters')
        self.assertEqual(sorted(self.starters.subcategories.values_list('name', flat=True)), ['Fried', 'Tandoor'])
        self.assertEqual(Category.objects.get(name='Desserts').branch, self.b)

    # access --------------------------------------------------------------------------------
    def test_view_only_role_can_export_but_not_import(self):
        role = CustomRole.objects.create(restaurant=self.r, name='Viewer', permissions={'menu': 'view'})
        prof = self.user.restaurant_profile
        prof.custom_role = role
        prof.save()
        self.assertEqual(self.client.get(M + 'export/').status_code, 200)
        r = self.imp(M, csv_file([['Name', 'Category', 'Price'], ['X', 'Y', '1']]), step='apply')
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(MenuItem.objects.filter(name='X').exists())

    def test_other_restaurants_cannot_be_reached(self):
        Restaurant.objects.create(name='Other', slug='other-co')
        self.assertNotEqual(self.client.get('/dashboard/other-co/menu/export/').status_code, 200)
        r = self.imp('/dashboard/other-co/menu/', csv_file([['Name', 'Category', 'Price'], ['X', 'Y', '1']]), step='apply')
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(MenuItem.objects.filter(name='X').exists())

    def test_pages_show_the_controls(self):
        for url in (M, C):
            r = self.client.get(url)
            self.assertContains(r, 'id="mioModal"')
            self.assertContains(r, 'Export')
