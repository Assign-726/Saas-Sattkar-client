from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
import requests
import hmac
import hashlib
import time
import json
import logging

logger = logging.getLogger(__name__)


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

    # Master Dashboard Integration
    master_license_key = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        verbose_name="Master License Key",
        help_text="Auto-assigned by MasterPanel on onboarding"
    )
    master_sync_status = models.CharField(
        max_length=20,
        blank=True,
        default="PENDING",
        verbose_name="Master Sync Status",
        help_text="PENDING / SYNCED / FAILED"
    )

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

    def sync_to_master_dashboard(self, master_url: str, api_secret: str = "") -> dict:
        """
        Cryptographically pushes complete business registration data to MasterPanel.
        Includes full legal, tax, address, contact, and branch hierarchy.
        Uses HMAC-SHA256 signature and Bearer token for end-to-end security.
        """
        url = master_url.rstrip("/") + "/api/register"

        # Serialize GST registrations
        gst_list = []
        try:
            for g in self.gst_registrations.all():
                gst_list.append({
                    "gstin": g.gstin,
                    "state_code": g.state_code,
                    "reg_type": g.reg_type,
                })
        except Exception:
            pass

        # Serialize Branches
        branch_list = []
        try:
            for b in self.branches.all():
                branch_list.append({
                    "id": b.id,
                    "name": b.name,
                    "phone": b.phone,
                    "address_line_1": b.address_line_1,
                    "address_line_2": b.address_line_2,
                    "city": b.city,
                    "state": b.state or self.state,
                    "pincode": b.pincode,
                    "fssai_license_no": b.fssai_license_no,
                    "serves_alcohol": b.serves_alcohol,
                    "gstin": b.gstin or (b.gst_registration.gstin if b.gst_registration else ""),
                    "gst_registration_type": b.gst_registration_type,
                    "outlet_type": getattr(b, 'outlet_type', 'dine_in'),
                    "vat_tin": b.vat_tin,
                    "excise_license_no": b.excise_license_no,
                })
        except Exception:
            pass

        payload = {
            # Core Identifiers
            "business_name": self.trade_name or self.legal_name or self.name,
            "legal_name": self.legal_name or "",
            "trade_name": self.trade_name or "",
            "domain": self.slug,

            # Legal & Tax
            "business_type": self.business_type or "",
            "pan": self.pan or "",
            "cin_llpin": self.cin_llpin or "",
            "is_gst_registered": bool(self.is_gst_registered),
            "gst_type": self.gst_type or "U",
            "gst_registrations": gst_list,

            # Address
            "address_line_1": self.address_line_1 or "",
            "address_line_2": self.address_line_2 or "",
            "city": self.city or "",
            "state": self.state or "",
            "pincode": self.pincode or "",
            "mailing_address": self.mailing_address or "",

            # Contact
            "email": self.email or "",
            "phone": self.phone or "",

            # Outlets
            "branch_count": len(branch_list),
            "branches": branch_list,
            "api_secret": api_secret,
        }

        # Build JSON body
        body_json = json.dumps(payload, separators=(',', ':'))
        timestamp = str(int(time.time()))

        # Generate HMAC-SHA256 signature
        message = f"{timestamp}.{body_json}".encode('utf-8')
        secret_bytes = (api_secret or "").encode('utf-8')
        signature = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_secret}",
            "X-Signature": signature,
            "X-Timestamp": timestamp,
        }

        try:
            resp = requests.post(url, data=body_json, headers=headers, timeout=10)
            data = resp.json()
            if resp.ok and data.get("license_key"):
                Restaurant.objects.filter(pk=self.pk).update(
                    master_license_key=data["license_key"],
                    master_sync_status="SYNCED",
                )
                self.master_license_key = data["license_key"]
                self.master_sync_status = "SYNCED"
                logger.info(f"[MasterPanel] Synced '{self.name}' - key: {data['license_key']}")
            else:
                Restaurant.objects.filter(pk=self.pk).update(master_sync_status="FAILED")
                self.master_sync_status = "FAILED"
                logger.error(f"[MasterPanel] Sync failed for '{self.name}': {data}")
            return data
        except Exception as exc:
            Restaurant.objects.filter(pk=self.pk).update(master_sync_status="FAILED")
            self.master_sync_status = "FAILED"
            logger.error(f"[MasterPanel] Request error for '{self.name}': {exc}")
            return {"error": str(exc)}


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

    class OutletType(models.TextChoices):
        DINE_IN = 'dine_in', 'Dine-in'
        TAKEAWAY_QSR = 'takeaway_qsr', 'Takeaway / QSR'
        CLOUD_KITCHEN = 'cloud_kitchen', 'Cloud Kitchen'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='branches')
    gst_registration = models.ForeignKey(
        GstRegistration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='branches'
    )
    name = models.CharField(max_length=200)
    outlet_type = models.CharField(
        max_length=20,
        choices=OutletType.choices,
        default=OutletType.DINE_IN,
        verbose_name="Outlet Type"
    )
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
    service_charge_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        verbose_name="Service Charge (%)",
        help_text="Service charge percentage specific to this branch"
    )
    
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
    
    # Digital ordering & payment settings per branch
    enable_customer_ordering = models.BooleanField(default=True, help_text="Allow customers to place orders directly from the digital menu for this branch")
    enable_online_payment = models.BooleanField(default=False, help_text="Enable online payment for this branch")
    enable_pay_at_counter = models.BooleanField(default=True, help_text="Enable pay at counter for this branch")
    
    # Storefront contact, timing & coordinates per branch
    email = models.EmailField(blank=True, default="")
    opening_hours = models.CharField(max_length=200, blank=True, default="")
    map_iframe_url = models.TextField(blank=True, default="")

    # Social media channels per branch
    facebook_url = models.URLField(blank=True, default="")
    instagram_url = models.URLField(blank=True, default="")
    twitter_url = models.URLField(blank=True, default="")
    youtube_url = models.URLField(blank=True, default="")
    
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


class UserSecuritySetting(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='security_setting')
    is_2fa_enabled = models.BooleanField(default=True, help_text="Require 6-digit OTP verification code on login")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User Security Setting'
        verbose_name_plural = 'User Security Settings'

    def __str__(self):
        return f"{self.user.username} (2FA: {'ON' if self.is_2fa_enabled else 'OFF'})"


class CustomerInquiry(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name='inquiries')
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Customer Inquiry'
        verbose_name_plural = 'Customer Inquiries'
        ordering = ['-created_at']

    def __str__(self):
        rest_name = self.restaurant.name if self.restaurant else "General"
        return f"[{rest_name}] {self.name} - {self.subject or 'Inquiry'} ({self.created_at.strftime('%Y-%m-%d')})"


class NewsletterSubscriber(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name='subscribers')
    email = models.EmailField()
    source = models.CharField(max_length=50, default='web_footer', blank=True)
    is_active = models.BooleanField(default=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Newsletter Subscriber'
        verbose_name_plural = 'Newsletter Subscribers'
        unique_together = ('restaurant', 'email')
        ordering = ['-subscribed_at']

    def __str__(self):
        rest_name = self.restaurant.name if self.restaurant else "General"
        return f"[{rest_name}] {self.email}"


class JobPosting(models.Model):
    class JobType(models.TextChoices):
        FULL_TIME = 'Full Time', 'Full Time'
        PART_TIME = 'Part Time', 'Part Time'
        CONTRACT = 'Contract', 'Contract'
        INTERNSHIP = 'Internship', 'Internship'
        FREELANCE = 'Freelance', 'Freelance'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='job_postings')
    branch = models.ForeignKey(
        RestaurantBranch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_postings',
        help_text="Specific branch or All Outlets / Global"
    )
    title = models.CharField(max_length=200, verbose_name="Job Title / Position")
    slug = models.SlugField(max_length=220, blank=True)
    department = models.CharField(max_length=100, default="General", verbose_name="Department / Category")
    job_type = models.CharField(max_length=50, choices=JobType.choices, default=JobType.FULL_TIME, verbose_name="Job Type")
    experience_level = models.CharField(max_length=100, default='Freshers', verbose_name="Experience Level", help_text="e.g. Freshers, 1-3 Years, 3-5 Years")
    location = models.CharField(max_length=200, blank=True, verbose_name="Location", help_text="e.g. Jaipur, Rajasthan (leave blank to inherit branch/restaurant location)")
    eligibility = models.CharField(max_length=255, blank=True, verbose_name="Eligibility / Requirements", help_text="e.g. Graduation / Certification in UI/UX")
    salary_range = models.CharField(max_length=100, blank=True, verbose_name="Salary / CTC Range", help_text="e.g. ₹3,00,000 - ₹5,00,000 P.A. or Disclosed on Interview")
    roles_responsibilities = models.TextField(verbose_name="Roles and Responsibilities", help_text="List key roles and responsibilities (one per line)")
    requirements = models.TextField(blank=True, verbose_name="Skills & Qualifications", help_text="List required skills and qualifications (one per line)")
    openings_count = models.PositiveIntegerField(default=1, verbose_name="Open Positions")
    is_active = models.BooleanField(default=True, verbose_name="Active / Accepting Applications")
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', '-created_at']
        verbose_name = 'Job Posting'
        verbose_name_plural = 'Job Postings'

    def __str__(self):
        branch_str = f" - {self.branch.name}" if self.branch else " (All Branches)"
        return f"{self.title} [{self.restaurant.name}{branch_str}]"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title) or 'job'
            slug = base_slug
            counter = 1
            while JobPosting.objects.filter(restaurant=self.restaurant, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def location_display(self):
        if self.location:
            return self.location
        if self.branch:
            parts = [self.branch.city, self.branch.state]
            loc = ", ".join([p for p in parts if p])
            if loc:
                return loc
        if self.restaurant:
            parts = [self.restaurant.city, self.restaurant.state]
            loc = ", ".join([p for p in parts if p])
            if loc:
                return loc
        return "Not Specified"

    def get_roles_list(self):
        if not self.roles_responsibilities:
            return []
        lines = [line.strip().lstrip('•-*1234567890. ') for line in self.roles_responsibilities.splitlines() if line.strip()]
        return lines

    def get_requirements_list(self):
        if not self.requirements:
            return []
        lines = [line.strip().lstrip('•-*1234567890. ') for line in self.requirements.splitlines() if line.strip()]
        return lines

    @property
    def applications_count(self):
        return self.applications.count()


class JobApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending Review'
        REVIEWED = 'REVIEWED', 'Reviewed'
        SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
        INTERVIEWING = 'INTERVIEWING', 'Interviewing'
        HIRED = 'HIRED', 'Hired'
        REJECTED = 'REJECTED', 'Rejected'

    job = models.ForeignKey(JobPosting, on_delete=models.CASCADE, related_name='applications')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='job_applications')
    branch = models.ForeignKey(
        RestaurantBranch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_applications'
    )
    full_name = models.CharField(max_length=150, verbose_name="Full Name")
    email = models.EmailField(verbose_name="Email ID")
    country_code = models.CharField(max_length=10, default="+91", verbose_name="Country Code")
    phone = models.CharField(max_length=30, verbose_name="Contact Number")
    has_experience = models.BooleanField(default=False, verbose_name="Do you have any experience?")
    experience_years = models.PositiveIntegerField(null=True, blank=True, verbose_name="Experience Year")
    experience_months = models.PositiveIntegerField(null=True, blank=True, verbose_name="Experience Months")
    current_ctc = models.CharField(max_length=50, blank=True, verbose_name="Current CTC (LPA)")
    expected_ctc = models.CharField(max_length=50, blank=True, verbose_name="Expected CTC (LPA)")
    current_organization = models.CharField(max_length=200, blank=True, verbose_name="Current Organisation")
    notice_period_days = models.PositiveIntegerField(null=True, blank=True, verbose_name="Notice Period (Days)")
    reason_to_join = models.TextField(verbose_name="Why do you want to join?")
    resume = models.FileField(upload_to='resumes/%Y/%m/', verbose_name="Upload CV/Resume (PDF only)", null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name="Application Status"
    )
    is_read = models.BooleanField(default=False, verbose_name="Is Read")
    admin_notes = models.TextField(blank=True, verbose_name="Internal Admin Notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Job Application'
        verbose_name_plural = 'Job Applications'

    def __str__(self):
        return f"{self.full_name} - {self.job.title} ({self.get_status_display()})"

    @property
    def formatted_phone(self):
        if self.country_code:
            return f"{self.country_code} {self.phone}"
        return self.phone

    @property
    def experience_display(self):
        if not self.has_experience:
            return "Fresher"
        parts = []
        if self.experience_years:
            parts.append(f"{self.experience_years} yr{'s' if self.experience_years > 1 else ''}")
        if self.experience_months:
            parts.append(f"{self.experience_months} mo{'s' if self.experience_months > 1 else ''}")
        exp_str = " ".join(parts) if parts else "Experienced"
        if self.current_organization:
            return f"{exp_str} @ {self.current_organization}"
        return exp_str

