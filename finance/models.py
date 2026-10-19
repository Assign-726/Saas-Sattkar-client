"""Day-to-day expenses, per branch, feeding the profit & loss report."""
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


def receipt_path(instance, filename):
    """Unguessable name: receipts are only handed out through the logged-in receipt view."""
    ext = os.path.splitext(filename)[1].lower()[:6]
    return f'expenses/{instance.restaurant_id}/{uuid.uuid4().hex}{ext}'


class ExpenseCategory(models.Model):
    DEFAULTS = [
        ('Rent', 'fa-building'), ('Electricity & water', 'fa-bolt'), ('Gas & fuel', 'fa-fire-flame-simple'),
        ('Repairs & maintenance', 'fa-screwdriver-wrench'), ('Marketing & promotions', 'fa-bullhorn'),
        ('Staff meals & welfare', 'fa-mug-hot'), ('Housekeeping & laundry', 'fa-soap'), ('Licences & fees', 'fa-file-signature'),
        ('Internet & software', 'fa-wifi'), ('Transport & delivery', 'fa-truck'), ('Bank & payment charges', 'fa-building-columns'),
        ('Miscellaneous', 'fa-ellipsis'),
    ]

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='expense_categories')
    name = models.CharField(max_length=80)
    icon = models.CharField(max_length=40, default='fa-receipt')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Expense categories'
        constraints = [models.UniqueConstraint(fields=['restaurant', 'name'], name='uniq_expense_category')]

    def __str__(self):
        return self.name

    @classmethod
    def ensure_defaults(cls, restaurant):
        if not cls.objects.filter(restaurant=restaurant).exists():
            cls.objects.bulk_create([cls(restaurant=restaurant, name=n, icon=i) for n, i in cls.DEFAULTS], ignore_conflicts=True)
        return cls.objects.filter(restaurant=restaurant)


class Expense(models.Model):
    class Department(models.TextChoices):
        RESTAURANT = 'restaurant', 'Restaurant'
        HOTEL = 'hotel', 'Hotel'
        SHARED = 'shared', 'Shared (whole business)'

    class Method(models.TextChoices):
        CASH = 'cash', 'Cash'
        BANK = 'bank', 'Bank transfer'
        UPI = 'upi', 'UPI'
        CARD = 'card', 'Card'
        CREDIT = 'credit', 'Unpaid / on credit'

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='expenses')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses',
                               help_text='Empty = shared across branches')
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='expenses')
    department = models.CharField(max_length=12, choices=Department.choices, default=Department.RESTAURANT, db_index=True,
                                  help_text='Which side of the business the cost belongs to')
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='Total paid, including any tax')
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), help_text='GST included in the amount, if any')
    payment_method = models.CharField(max_length=10, choices=Method.choices, default=Method.CASH)
    paid_to = models.CharField(max_length=150, blank=True)
    reference = models.CharField(max_length=80, blank=True, help_text='Bill or invoice number')
    note = models.CharField(max_length=300, blank=True)
    receipt = models.FileField(upload_to=receipt_path, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-id']
        indexes = [models.Index(fields=['restaurant', 'date'])]

    def __str__(self):
        return f'{self.category} ₹{self.amount} on {self.date}'

    @property
    def net_amount(self):
        return (self.amount or 0) - (self.tax_amount or 0)
