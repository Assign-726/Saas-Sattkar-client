from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import MenuItem, Order, OrderItem, PurchaseOrder
from finance import pnl
from finance.models import Expense, ExpenseCategory
from integrations.models import Connection, ExternalOrderLink
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class FinanceTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.b1 = RestaurantBranch.objects.create(restaurant=self.r, name='North')
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='South')
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.cats = ExpenseCategory.ensure_defaults(self.r)
        self.today = timezone.localdate()

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def _order(self, branch, total, tax, status='COMPLETED'):
        return Order.objects.create(restaurant=self.r, branch=branch, customer_name='X', total_amount=Decimal(total), tax_amount=Decimal(tax),
                                    status=status, payment_status='paid')

    def test_pnl_math_by_branch(self):
        o1 = self._order(self.b1, '1050', '50')
        self._order(self.b2, '2100', '100')
        self._order(self.b1, '999', '0', status='C')  # cancelled: ignored
        conn = Connection.objects.create(restaurant=self.r, branch=self.b1, provider='swiggy', commission_pct=Decimal('20'))
        ExternalOrderLink.objects.create(branch=self.b1, connection=conn, source='swiggy', external_id='1', order=o1, commission_amount=Decimal('200'))
        PurchaseOrder.objects.create(restaurant=self.r, branch=self.b1, supplier_name='Veg Co', invoice_number='A1', purchase_date=self.today,
                                     subtotal=Decimal('300'), tax_amount=Decimal('15'), grand_total=Decimal('315'))
        Expense.objects.create(restaurant=self.r, branch=self.b1, category=self.cats.get(name='Rent'), date=self.today, amount=Decimal('118'), tax_amount=Decimal('18'))
        Expense.objects.create(restaurant=self.r, branch=None, category=self.cats.get(name='Miscellaneous'), date=self.today, amount=Decimal('50'))

        north = pnl.compute_pnl(self.r, self.today.replace(day=1), self.today, self.b1)
        self.assertEqual(north['revenue'], Decimal('1000'))
        self.assertEqual(north['commission'], Decimal('200'))
        self.assertEqual(north['purchases'], Decimal('300'))
        self.assertEqual(north['gross_profit'], Decimal('500'))
        self.assertEqual(north['expenses'], Decimal('100'))
        self.assertEqual(north['net_profit'], Decimal('400'))
        everything = pnl.compute_pnl(self.r, self.today.replace(day=1), self.today)
        self.assertEqual(everything['revenue'], Decimal('3000'))
        self.assertEqual(everything['expenses'], Decimal('150'))  # shared cost only in the total

    def test_expense_screens(self):
        self.client.force_login(self.gm)
        rent = self.cats.get(name='Rent')
        receipt = SimpleUploadedFile('bill.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        self.client.post(self.url('expenses'), {'op': 'add', 'amount': '25000', 'tax_amount': '0', 'date': self.today.isoformat(),
                                                'category': rent.pk, 'branch': self.b2.pk, 'paid_to': 'Landlord', 'receipt': receipt})
        e = Expense.objects.get()
        self.assertEqual((e.amount, e.branch), (Decimal('25000'), self.b2))
        self.assertNotIn('bill', e.receipt.name)  # stored under a random name
        resp = self.client.get(self.url('expense_receipt', pk=e.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content), b'%PDF-1.4 test')
        self.client.logout()
        self.assertNotEqual(self.client.get(self.url('expense_receipt', pk=e.pk)).status_code, 200)
        e.receipt.delete(save=False)

    def test_rejects_future_and_bad_values(self):
        self.client.force_login(self.gm)
        rent = self.cats.get(name='Rent')
        for bad in ({'amount': '-5'}, {'amount': '100', 'date': (self.today + timedelta(days=1)).isoformat()}, {'amount': '100', 'tax_amount': '500'}):
            data = {'op': 'add', 'amount': '100', 'tax_amount': '0', 'date': self.today.isoformat(), 'category': rent.pk, **bad}
            self.client.post(self.url('expenses'), data)
        self.assertFalse(Expense.objects.exists())

    def test_pages_render_and_csv(self):
        self.client.force_login(self.gm)
        self._order(self.b1, '1050', '50')
        for name in ('expenses', 'profit_loss'):
            self.assertEqual(self.client.get(self.url(name)).status_code, 200, name)
        for period in ('last_month', 'this_quarter', 'this_fy', 'last_30'):
            self.assertEqual(self.client.get(self.url('profit_loss'), {'period': period}).status_code, 200)
        csv_resp = self.client.get(self.url('profit_loss'), {'export': 'csv'})
        self.assertIn(b'Net profit', csv_resp.content)

    def test_cashier_blocked(self):
        cashier = User.objects.create_user('cash', 'c@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=cashier, restaurant=self.r, role='CASHIER')
        self.client.force_login(cashier)
        self.assertEqual(self.client.get(self.url('profit_loss')).status_code, 403)
