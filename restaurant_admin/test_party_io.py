"""Import / export of Parties."""
import csv
import io
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import Party
from super_admin.forms import compute_gstin_check_char
from super_admin.models import CustomRole, Restaurant, RestaurantBranch, RestaurantUser

P = '/dashboard/spice-co/parties/'


def gstin(body):
    return body + compute_gstin_check_char(body)


def csv_file(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return SimpleUploadedFile('p.csv', buf.getvalue().encode('utf-8'), content_type='text/csv')


class PartyImportExportTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main')
        self.g1 = gstin('27AAACM1234F1Z')
        self.metro = Party.objects.create(restaurant=self.r, branch=self.b, name='Metro Foods', party_type='vendor',
                                          phone='+91 98765 43210', gstin=self.g1, gst_registration_type='regular',
                                          opening_balance=Decimal('1500'), balance_type='payable')
        self.acme = Party.objects.create(restaurant=self.r, branch=self.b, name='Acme', party_type='customer', phone='9822012345')
        self.user = User.objects.create_user('admin', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='RESTAURANT_ADMIN')
        self.client.force_login(self.user)

    def imp(self, f, step='preview', mode='upsert'):
        return self.client.post(P + 'import/', {'file': f, 'step': step, 'mode': mode})

    def test_export_follows_tab_and_imports_back_unchanged(self):
        r = self.client.get(P + 'export/')
        rows = list(openpyxl.load_workbook(io.BytesIO(r.content)).worksheets[0].iter_rows(values_only=True))
        self.assertEqual(rows[0][:2], ('Name *', 'Type *'))
        self.assertEqual(len(rows), 3)
        body = self.client.get(P + 'export/', {'format': 'csv', 'tab': 'customer'}).content.decode('utf-8-sig')
        self.assertIn('Acme', body)
        self.assertNotIn('Metro', body)
        out = self.imp(SimpleUploadedFile('p.xlsx', r.content)).json()
        self.assertEqual(out['summary'], {'create': 0, 'update': 0, 'unchanged': 2, 'skip': 0, 'error': 0})
        tpl = self.client.get(P + 'export/', {'template': 1})
        self.assertEqual(self.imp(SimpleUploadedFile('t.xlsx', tpl.content)).json()['summary']['error'], 0)

    def test_import_matches_by_gstin_phone_then_name_and_validates(self):
        g2 = gstin('29AABCI1234F1Z')
        rows = [['Name', 'Type', 'Phone', 'GSTIN', 'GST registration', 'Opening balance', 'Balance type', 'PIN code'],
                ['Metro Food Supplies', '', '', self.g1, '', '2000', '', ''],       # renamed via GSTIN, balance change
                ['ACME Corp', '', '98220 12345', '', '', '', '', ''],                # matched by phone
                ['Infosys', 'buyer', '', g2.lower(), '', '5000', 'Dr', '560100'],    # new, state from GSTIN
                ['Walk Inn', 'customer', '', g2[:-1] + 'A', 'Unregistered', '', '', ''],  # unregistered: GSTIN dropped
                ['Bad GSTIN', 'vendor', '', '27AAACM1234F1ZZ', '', '', '', ''],
                ['No Type', '', '', '', '', '', '', ''],
                ['Bad PIN', 'vendor', '', '', '', '', '', '4000'],
                ['metro foods', 'vendor', '', '', '', '', '', '']]                   # same party as row 2 (by name)
        out = self.imp(csv_file(rows)).json()
        s = out['summary']
        self.assertEqual((s['create'], s['update'], s['error']), (2, 2, 4), out['rows'])
        errors = ' '.join(m for r in out['rows'] for m in r['messages'])
        self.assertIn('check digit', errors)
        self.assertIn('Type is missing', errors)
        self.assertIn('6 digits', errors)
        self.assertIn('Same party as row 2', errors)
        metro = next(r for r in out['rows'] if r['name'] == 'Metro Food Supplies')
        self.assertTrue(any('ledger balance' in w for w in metro['warnings']))

        self.imp(csv_file(rows), step='apply')
        self.metro.refresh_from_db()
        self.acme.refresh_from_db()
        self.assertEqual((self.metro.name, self.metro.opening_balance), ('Metro Food Supplies', Decimal('2000.00')))
        self.assertEqual(self.acme.name, 'ACME Corp')
        infy = Party.objects.get(name='Infosys')
        self.assertEqual((infy.party_type, infy.gstin, infy.state, infy.place_of_supply, infy.balance_type, infy.branch),
                         ('customer', g2, 'Karnataka', 'Karnataka', 'receivable', self.b))
        walk = Party.objects.get(name='Walk Inn')
        self.assertEqual((walk.gstin, walk.gst_registration_type), ('', 'unregistered'))

    def test_modes_and_access(self):
        rows = [['Name', 'Type'], ['Acme', 'customer'], ['New Co', 'vendor']]
        self.assertEqual(self.imp(csv_file(rows), mode='create').json()['summary']['skip'], 1)
        self.assertEqual(self.imp(csv_file(rows), mode='update').json()['summary']['skip'], 1)
        self.assertContains(self.client.get(P), 'id="mioModal"')
        role = CustomRole.objects.create(restaurant=self.r, name='Viewer', permissions={'inventory': 'view'})
        prof = self.user.restaurant_profile
        prof.custom_role = role
        prof.save()
        self.assertEqual(self.client.get(P + 'export/').status_code, 200)
        self.assertNotEqual(self.imp(csv_file(rows), step='apply').status_code, 200)
        self.assertFalse(Party.objects.filter(name='New Co').exists())
        Restaurant.objects.create(name='Other', slug='other-co')
        self.assertNotEqual(self.client.get('/dashboard/other-co/parties/export/').status_code, 200)
