"""Settle → "Company (has GSTIN)": the company is saved to Parties and the bill is linked to it."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import MenuItem, Order, Party
from super_admin.forms import compute_gstin_check_char
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


def gstin(body='27AAACT2727Q1Z'):
    return body + compute_gstin_check_char(body)


class GstCompanyTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main')
        self.dal = MenuItem.objects.create(restaurant=self.r, branch=self.b, name='Dal', price=Decimal('200'), sku='D')
        self.user = User.objects.create_user('cash', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role='RESTAURANT_ADMIN')
        self.client.force_login(self.user)
        self.url = '/dashboard/spice-main/orders/gst-company/'

    def post(self, url, body):
        return self.client.post(url, data=json.dumps(body), content_type='application/json',
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_adds_company_to_parties_as_customer_with_state_from_gstin(self):
        r = self.post(self.url, {'name': '  Tata   Consultancy ', 'gstin': gstin().lower(), 'email': 'ap@tcs.com'})
        self.assertEqual(r.status_code, 200, r.content)
        out = r.json()
        self.assertTrue(out['created'])
        p = Party.objects.get(pk=out['party']['id'])
        self.assertEqual((p.name, p.gstin, p.party_type, p.gst_registration_type), ('Tata Consultancy', gstin(), 'customer', 'regular'))
        self.assertEqual((p.state, p.place_of_supply), ('Maharashtra', 'Maharashtra'))
        self.assertEqual(p.restaurant, self.r)

    def test_rejects_missing_name_and_bad_gstins(self):
        bad_check = gstin()[:14] + ('A' if gstin()[14] != 'A' else 'B')
        for body, field in [({'name': '', 'gstin': gstin()}, 'name'),
                            ({'name': 'X', 'gstin': '27AAACT2727'}, 'gstin'),
                            ({'name': 'X', 'gstin': bad_check}, 'gstin'),
                            ({'name': 'X', 'gstin': gstin('99AAACT2727Q1Z')}, 'gstin'),
                            ({'name': 'X', 'gstin': gstin(), 'email': 'nope'}, 'email')]:
            r = self.post(self.url, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(field, r.json()['errors'])
        self.assertFalse(Party.objects.exists())

    def test_same_gstin_updates_the_party_and_keeps_a_vendor_a_vendor(self):
        v = Party.objects.create(restaurant=self.r, name='Old Name', party_type='vendor', gstin=gstin(), phone='111')
        r = self.post(self.url, {'name': 'New Name', 'gstin': gstin()})
        self.assertFalse(r.json()['created'])
        v.refresh_from_db()
        self.assertEqual((v.name, v.party_type, v.phone), ('New Name', 'vendor', '111'))
        self.assertEqual(Party.objects.count(), 1)

    def test_settled_bill_is_linked_to_the_picked_company(self):
        party = Party.objects.get(pk=self.post(self.url, {'name': 'Acme Pvt Ltd', 'gstin': gstin()}).json()['party']['id'])
        r = self.post('/dashboard/spice-main/orders/create/', {
            'action': 'settle_order', 'order_type': 'takeaway', 'customer_name': 'Acme Pvt Ltd',
            'customer_gstin': gstin(), 'party_id': party.pk, 'payment_method': 'cash', 'payment_status': 'paid',
            'payments': [{'method': 'cash', 'amount': 210}], 'paid_amount': '210.00', 'due_amount': '0.00',
            'items': [{'menu_item_id': self.dal.pk, 'quantity': 1, 'unit_price': 200}]})
        self.assertTrue(r.json()['success'], r.content)
        order = Order.objects.get(pk=r.json()['order_id'])
        self.assertEqual(order.party, party)
        self.assertEqual(order.customer_name, 'Acme Pvt Ltd')
        self.assertNotEqual(order.gstin_no, gstin())  # the buyer's GSTIN never becomes our invoice number
        self.assertEqual(Party.objects.count(), 1)  # no duplicate party guessed from the name

    def test_another_restaurants_party_cannot_be_linked_or_reached(self):
        other = Restaurant.objects.create(name='Other', slug='other-co')
        theirs = Party.objects.create(restaurant=other, name='Theirs', gstin=gstin())
        r = self.post('/dashboard/spice-main/orders/create/', {
            'action': 'settle_order', 'order_type': 'takeaway', 'party_id': theirs.pk, 'payment_method': 'cash',
            'items': [{'menu_item_id': self.dal.pk, 'quantity': 1, 'unit_price': 200}]})
        self.assertNotEqual(Order.objects.get(pk=r.json()['order_id']).party, theirs)
        r = self.post('/dashboard/other-co/orders/gst-company/', {'name': 'X', 'gstin': gstin()})
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(Party.objects.filter(name='X').count(), 0)
