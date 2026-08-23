from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify


class Restaurant(models.Model):
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=20, blank=True)
    legal_name = models.CharField(max_length=255, blank=True)
    trade_name = models.CharField(max_length=255, blank=True)
    business_type = models.CharField(max_length=100, blank=True)
    pan = models.CharField(max_length=20, blank=True)
    cin_llpin = models.CharField(max_length=50, blank=True, verbose_name="CIN / LLPIN")
    is_gst_registered = models.BooleanField(default=False)
    email = models.EmailField(blank=True)
    mailing_address = models.TextField(blank=True)

    class GSTType(models.TextChoices):
        REGULAR = 'R', 'Regular'
        COMPOSITION = 'C', 'Composition'
        UNREGISTERED = 'U', 'Unregistered'

    gst_type = models.CharField(
        max_length=1,
        choices=GSTType.choices,
        default=GSTType.UNREGISTERED,
        verbose_name="GST Type"
    )
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.trade_name or self.name

    @property
    def address(self):
        parts = [self.address_line_1, self.address_line_2, self.city, self.state, self.pincode]
        return ", ".join([p for p in parts if p])

    @property
    def invoice_heading(self):
        if self.gst_type == 'R':
            return "Tax Invoice"
        elif self.gst_type == 'C':
            return "Bill of supply"
        else:
            return "Invoice"

    def get_company_default(self, field_name):
        from decimal import Decimal
        defaults = {
            'default_gst_rate': Decimal('0.00'),
            'gst_rate': Decimal('0.00'),
            'default_hsn_code': '',
            'hsn_sac_code': '',
            'hsn_code': '',
            'taxability': 'taxable',
            'taxability_type': 'taxable',
            'default_unit': 'piece',
            'unit': 'piece',
            'default_warehouse': 'Main Store',
            'valuation_method': 'FIFO',
            'expense_account': 'Cost of Goods Sold',
            'permission_group': 'All Staff',
        }
        return defaults.get(field_name, None)

    def save(self, *args, **kwargs):
        if self.trade_name and not self.name:
            self.name = self.trade_name
        if not self.slug:
            self.slug = slugify(self.name or self.legal_name or 'restaurant')
        super().save(*args, **kwargs)


class GstRegistration(models.Model):
    class RegType(models.TextChoices):
        REGULAR = 'regular', 'Regular'
        COMPOSITION = 'composition', 'Composition'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='gst_registrations')
    gstin = models.CharField(max_length=15)
    state_code = models.CharField(max_length=2, blank=True)
    reg_type = models.CharField(max_length=20, choices=RegType.choices, default=RegType.REGULAR)
    specified_premises = models.BooleanField(
        default=False,
        help_text="Restaurant in hotel where room tariff > ₹7,500/night in previous FY (attracts 18% GST with ITC)"
    )
    einvoice_enabled = models.BooleanField(
        default=False,
        help_text="Turnover ₹5 Cr+ requires IRN + QR code on B2B invoices"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('restaurant', 'gstin')
        ordering = ['state_code', 'gstin']
        verbose_name = 'GST Registration'
        verbose_name_plural = 'GST Registrations'

    def save(self, *args, **kwargs):
        if self.gstin and len(self.gstin) >= 2:
            self.state_code = self.gstin[:2]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.gstin} ({self.get_reg_type_display()})"


class RestaurantBranch(models.Model):
    class GSTRegType(models.TextChoices):
        REGULAR_5 = 'R5', 'Regular (5% GST on bills)'
        REGULAR_18 = 'R18', 'Regular (18% GST with ITC)'
        COMPOSITION = 'C', 'Composition'
        UNREGISTERED = 'U', 'Unregistered'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='branches')
    gst_registration = models.ForeignKey(
        GstRegistration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='branches'
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True)
    use_company_address = models.BooleanField(default=True)
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=20, blank=True)
    fssai_license_no = models.CharField(max_length=50, blank=True)
    serves_alcohol = models.BooleanField(default=False)
    
    # State VAT details (when serves_alcohol = True)
    vat_tin = models.CharField(max_length=50, blank=True, verbose_name="State VAT TIN")
    excise_license_no = models.CharField(max_length=50, blank=True, verbose_name="Excise License No.")
    vat_beer_rate = models.DecimalField(max_digits=5, decimal_places=2, default=60.00, verbose_name="Beer VAT Rate (%)")
    vat_imfl_rate = models.DecimalField(max_digits=5, decimal_places=2, default=65.00, verbose_name="IMFL VAT Rate (%)")
    vat_wine_rate = models.DecimalField(max_digits=5, decimal_places=2, default=60.00, verbose_name="Wine VAT Rate (%)")

    # Legacy / fallback fields
    gstin = models.CharField(max_length=15, blank=True)
    gst_registration_type = models.CharField(
        max_length=10,
        choices=GSTRegType.choices,
        default=GSTRegType.REGULAR_5
    )
    is_hotel_tariff_7500_plus = models.BooleanField(default=False)
    e_invoicing_enabled = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Restaurant Branch'
        verbose_name_plural = 'Restaurant Branches'

    def __str__(self):
        return f"{self.name} ({self.restaurant.name})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            if not base_slug:
                base_slug = f"branch-{self.pk or 'new'}"
            candidate = base_slug
            counter = 1
            while RestaurantBranch.objects.filter(slug=candidate).exclude(pk=self.pk).exists() or Restaurant.objects.filter(slug=candidate).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    @property
    def is_vat_config_complete(self):
        if not self.serves_alcohol:
            return True
        return bool(self.vat_tin and self.excise_license_no)



class DocumentSeries(models.Model):
    class VoucherType(models.TextChoices):
        SALE = 'SALE', 'Sale (Tax Invoice / Bill of Supply / Receipt)'
        CN = 'CN', 'Credit Note (GST)'
        RCPT = 'RCPT', 'Receipt / Payment Received'
        ADV = 'ADV', 'Advance Booking Payment'
        PO = 'PO', 'Purchase Order'
        GRN = 'GRN', 'Goods Receipt Note'
        PBILL = 'PBILL', 'Purchase Bill'
        DN = 'DN', 'Debit Note'
        PAY = 'PAY', 'Payment Voucher'
        STN = 'STN', 'Stock Transfer Note'
        ADJ = 'ADJ', 'Stock Adjustment / Wastage'
        VAT_SALE = 'VAT_SALE', 'VAT Liquor Bill'
        VAT_CN = 'VAT_CN', 'VAT Credit Note'

    class Method(models.TextChoices):
        AUTO = 'auto', 'Automatic'
        AUTO_OVERRIDE = 'auto_override', 'Automatic with Manual Override'
        MANUAL = 'manual', 'Manual'

    branch = models.ForeignKey(RestaurantBranch, on_delete=models.CASCADE, related_name='document_series')
    voucher_type = models.CharField(max_length=20, choices=VoucherType.choices)
    prefix = models.CharField(max_length=50, blank=True)
    suffix = models.CharField(max_length=50, blank=True)
    digits = models.PositiveIntegerField(default=4)
    next_no = models.PositiveIntegerField(default=1)
    fy_reset = models.BooleanField(default=True)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.AUTO)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('branch', 'voucher_type')
        ordering = ['voucher_type']
        verbose_name = 'Document Series'
        verbose_name_plural = 'Document Series'

    def __str__(self):
        return f"{self.branch.name} - {self.voucher_type} ({self.prefix})"


class RestaurantUser(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='restaurant_profile')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='admins')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='admins')
    is_primary_admin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'restaurant')
        verbose_name = 'Restaurant Admin'
        verbose_name_plural = 'Restaurant Admins'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else ""
        return f"{self.user.username} -> {self.restaurant.name}{branch_str}"


