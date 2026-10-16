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

    # Master Dashboard Module Entitlements & Remote Feature Flags
    feature_website_pages = models.BooleanField(
        default=True,
        verbose_name="Website CMS Module",
        help_text="Controls SHOW_WEBSITE_PAGES (Public website, menu showcase & CMS)"
    )
    feature_alcohol_vat = models.BooleanField(
        default=True,
        verbose_name="Alcohol & VAT Module",
        help_text="Controls ENABLE_ALCOHOL_VAT (Liquor billing & State VAT)"
    )
    feature_club_events = models.BooleanField(
        default=True,
        verbose_name="Club & Events Module",
        help_text="Controls ENABLE_CLUB_EVENTS_MODULE (Nightclub passes & live events)"
    )
    feature_hotel_pms = models.BooleanField(
        default=True,
        verbose_name="Hotel PMS Module",
        help_text="Controls ENABLE_HOTEL_MODULE (Hotel rooms & front-desk PMS)"
    )
    feature_restaurant_pos = models.BooleanField(
        default=True,
        verbose_name="Restaurant POS Module",
        help_text="Controls ENABLE_RESTAURANT_MODULE (Dining menu, tables, POS, kitchen KDS)"
    )
    feature_hotel_website = models.BooleanField(
        default=False,
        verbose_name="Dedicated Hotel Website Mode",
        help_text="Controls ENABLE_HOTEL_WEBSITE mode across branches on public frontend"
    )
    features_last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Features Last Synced At",
        help_text="Timestamp when module flags were last refreshed from Master Admin"
    )

    # Master Dashboard Subscription & Overdue Lockout Status
    subscription_status = models.CharField(
        max_length=20,
        blank=True,
        default="ACTIVE",
        verbose_name="Subscription Status",
        help_text="ACTIVE / GRACE_PERIOD / SUSPENDED"
    )
    subscription_is_overdue = models.BooleanField(
        default=False,
        verbose_name="Is Overdue"
    )
    subscription_grace_period_expired = models.BooleanField(
        default=False,
        verbose_name="Grace Period Expired"
    )
    subscription_grace_days_remaining = models.IntegerField(
        default=0,
        verbose_name="Grace Days Remaining"
    )
    subscription_overdue_days = models.IntegerField(
        default=0,
        verbose_name="Overdue Days"
    )
    subscription_is_locked = models.BooleanField(
        default=False,
        verbose_name="Access Locked",
        help_text="True if grace period has expired or manually suspended by Master Admin"
    )
    subscription_plan_name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        default="",
        verbose_name="Plan Name"
    )
    subscription_next_billing_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Next Billing Date"
    )
    subscription_is_trial = models.BooleanField(
        default=False,
        verbose_name="Is Free Trial"
    )
    subscription_trial_days_remaining = models.IntegerField(
        default=0,
        verbose_name="Trial Days Remaining"
    )
    subscription_trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Trial Ends At"
    )

    # Master Dashboard Licensed Quotas (0 means fallback to system/plan default)
    max_branches = models.IntegerField(
        default=0,
        verbose_name="Allowed Outlets / Branches",
        help_text="Custom branch quota synced from Master Admin. 0 = use system/plan default."
    )
    max_businesses = models.IntegerField(
        default=0,
        verbose_name="Allowed Businesses",
        help_text="Custom business quota synced from Master Admin. 0 = use system default."
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
    def gstin(self):
        reg = getattr(self, 'gst_registrations', None)
        if reg and hasattr(reg, 'first'):
            first_reg = reg.first()
            if first_reg and getattr(first_reg, 'gstin', None):
                return first_reg.gstin
        first_branch = self.branches.first() if hasattr(self, 'branches') else None
        if first_branch:
            if getattr(first_branch, 'gst_registration', None) and first_branch.gst_registration.gstin:
                return first_branch.gst_registration.gstin
            if getattr(first_branch, 'gstin', None):
                return first_branch.gstin
        return ''

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

    def get_max_allowed_branches(self) -> int:
        from restaurant_project import constants
        if not constants.is_master_connected(self):
            return constants.MAX_ALLOWED_BRANCHES
        if self.max_branches and self.max_branches > 0:
            return self.max_branches
        return constants.MAX_ALLOWED_BRANCHES

    def get_max_allowed_businesses(self) -> int:
        from restaurant_project import constants
        if not constants.is_master_connected(self):
            return constants.MAX_ALLOWED_BUSINESSES
        if self.max_businesses and self.max_businesses > 0:
            return self.max_businesses
        return constants.MAX_ALLOWED_BUSINESSES

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
                from django.utils import timezone
                update_fields = {
                    "master_license_key": data["license_key"],
                    "master_sync_status": "SYNCED",
                }
                modules = data.get("modules")
                if isinstance(modules, dict):
                    if "SHOW_WEBSITE_PAGES" in modules:
                        has_website = bool(modules["SHOW_WEBSITE_PAGES"])
                        update_fields["feature_website_pages"] = has_website
                        self.feature_website_pages = has_website
                        update_fields["is_active"] = has_website
                        self.is_active = has_website
                    if "ENABLE_ALCOHOL_VAT" in modules:
                        update_fields["feature_alcohol_vat"] = bool(modules["ENABLE_ALCOHOL_VAT"])
                        self.feature_alcohol_vat = update_fields["feature_alcohol_vat"]
                    if "ENABLE_CLUB_EVENTS_MODULE" in modules:
                        update_fields["feature_club_events"] = bool(modules["ENABLE_CLUB_EVENTS_MODULE"])
                        self.feature_club_events = update_fields["feature_club_events"]
                    if "ENABLE_HOTEL_MODULE" in modules:
                        update_fields["feature_hotel_pms"] = bool(modules["ENABLE_HOTEL_MODULE"])
                        self.feature_hotel_pms = update_fields["feature_hotel_pms"]
                    if "ENABLE_RESTAURANT_MODULE" in modules:
                        update_fields["feature_restaurant_pos"] = bool(modules["ENABLE_RESTAURANT_MODULE"])
                        self.feature_restaurant_pos = update_fields["feature_restaurant_pos"]
                    if "ENABLE_HOTEL_WEBSITE" in modules:
                        update_fields["feature_hotel_website"] = bool(modules["ENABLE_HOTEL_WEBSITE"])
                        self.feature_hotel_website = update_fields["feature_hotel_website"]
                    update_fields["features_last_synced_at"] = timezone.now()
                    self.features_last_synced_at = update_fields["features_last_synced_at"]

                # Licensed quota limits from MasterPanel
                if "max_branches" in data and str(data["max_branches"]).strip().isdigit():
                    update_fields["max_branches"] = int(str(data["max_branches"]).strip())
                    self.max_branches = update_fields["max_branches"]
                if "max_businesses" in data and str(data["max_businesses"]).strip().isdigit():
                    update_fields["max_businesses"] = int(str(data["max_businesses"]).strip())
                    self.max_businesses = update_fields["max_businesses"]

                Restaurant.objects.filter(pk=self.pk).update(**update_fields)
                if "ENABLE_RESTAURANT_MODULE" in modules:
                    if not self.feature_restaurant_pos:
                        self.branches.all().update(has_restaurant_module=False)
                if "ENABLE_HOTEL_WEBSITE" in modules:
                    if not self.feature_hotel_website:
                        self.branches.all().update(has_hotel_website=False)
                        try:
                            from core.models import RestaurantLanding
                            RestaurantLanding.objects.filter(restaurant=self).update(has_hotel_website=False)
                        except Exception:
                            pass
                self.master_license_key = data["license_key"]
                self.master_sync_status = "SYNCED"
                logger.info(f"[MasterPanel] Synced '{self.name}' - key: {data['license_key']}, modules: {modules}")
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
    vat_gen_pct = models.DecimalField(max_digits=5, decimal_places=2, default=20.00, verbose_name="General Liquor VAT Rate (%)", help_text="General/unclassified liquor rate")
    vat_surcharge_name = models.CharField(max_length=100, default='VAT Surcharge', blank=True, verbose_name="VAT Surcharge / Cess Name", help_text="State-specific name for VAT surcharge/cess e.g. Cow Cess, Surcharge")
    vat_surcharge_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name="VAT Surcharge / Cess (%)", help_text="Government Cess/Surcharge applied on the VAT amount")

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
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name="Latitude", help_text="GPS Latitude of the restaurant outlet e.g. 19.076090")
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name="Longitude", help_text="GPS Longitude of the restaurant outlet e.g. 72.877426")
    geofence_radius_meters = models.PositiveIntegerField(default=100, verbose_name="Geofence Radius (meters)", help_text="Permitted distance from outlet for Table QR ordering (default: 100m)")
    enforce_geofence = models.BooleanField(default=False, verbose_name="Enforce Geofence", help_text="Block Table QR orders if customer GPS location is outside the allowed radius")

    # Social media channels per branch
    facebook_url = models.URLField(blank=True, default="")
    instagram_url = models.URLField(blank=True, default="")
    twitter_url = models.URLField(blank=True, default="")
    youtube_url = models.URLField(blank=True, default="")

    # Branch Custom SMTP / Mailing Credentials
    email_is_active = models.BooleanField(
        default=True,
        verbose_name="Enable Branch Custom SMTP",
        help_text="Use branch-specific email account for outgoing emails instead of Super Admin email"
    )
    email_sender_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        verbose_name="Email Sender Display Name",
        help_text="Name displayed as sender (e.g. 'Royal Dine & Club')"
    )
    email_host_user = models.EmailField(
        blank=True,
        default="",
        verbose_name="SMTP Username / Sender Email",
        help_text="e.g. branch.restaurant@gmail.com"
    )
    email_host_password = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="SMTP Password / App Password",
        help_text="16-character Google App Password or SMTP password"
    )
    email_host = models.CharField(
        max_length=150,
        default="smtp.gmail.com",
        blank=True,
        verbose_name="SMTP Host Server"
    )
    email_port = models.PositiveIntegerField(
        default=587,
        verbose_name="SMTP Port"
    )
    email_use_tls = models.BooleanField(
        default=True,
        verbose_name="Use TLS"
    )
    email_use_ssl = models.BooleanField(
        default=False,
        verbose_name="Use SSL"
    )
    
    # Feature gating: Club & Events Ticketing module
    has_club_module = models.BooleanField(
        default=False,
        verbose_name="Enable Club & Events Module",
        help_text="Enables ticketed club events, ticket tiers, and guestlist management for this branch"
    )

    # Feature gating: Hotel PMS module
    has_hotel_module = models.BooleanField(
        default=False,
        verbose_name="Enable Hotel Module",
        help_text="Enables hotel room inventory, tape chart, guest folios, and booking engine for this branch"
    )

    # Feature gating: Restaurant & POS module
    has_restaurant_module = models.BooleanField(
        default=True,
        verbose_name="Enable Restaurant / Dining Module",
        help_text="Enables dining menu, tables, POS, kitchen KDS, and restaurant billing for this branch"
    )

    # Feature gating: Dedicated Hotel Website Mode
    has_hotel_website = models.BooleanField(
        default=False,
        verbose_name="Dedicated Hotel Website Mode",
        help_text="When enabled, public frontend operates in Hotel Website mode (suppresses restaurant menus and chefs)"
    )
    
    # Branch Operating Hours & Online Ordering Restrictions (QR & Takeaway)
    enforce_operating_hours = models.BooleanField(
        default=False,
        verbose_name="Enforce Strict Operating Hours",
        help_text="Restrict digital ordering (QR and takeaway) to branch operating hours"
    )
    opening_time = models.TimeField(
        null=True,
        blank=True,
        verbose_name="Daily Opening Time",
        help_text="Time branch opens for orders e.g. 11:00 AM"
    )
    closing_time = models.TimeField(
        null=True,
        blank=True,
        verbose_name="Daily Closing Time",
        help_text="Time branch closes for orders e.g. 11:00 PM"
    )
    closed_message = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Closed Announcement Note",
        help_text="Optional notice message displayed to customers when ordering is closed"
    )
    weekly_schedule = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        verbose_name="Weekly Operating Schedule",
        help_text="Custom day-wise schedule JSON e.g. {'0': [{'label': 'Lunch', 'from': '12:00', 'to': '15:30'}]}"
    )

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
    def address(self):
        parts = [self.address_line_1, self.address_line_2, self.city, self.state, self.pincode]
        addr = ", ".join([p for p in parts if p])
        if addr:
            return addr
        return self.restaurant.address if self.restaurant else ""

    @property
    def is_vat_config_complete(self):
        if not self.serves_alcohol:
            return True
        return bool(self.vat_tin and self.excise_license_no)

    def get_effective_schedule(self):
        """
        Returns a normalized weekly schedule dict with keys 0..6 (0=Sunday ... 6=Saturday)
        matching JavaScript Date.getDay() convention.
        """
        raw = self.weekly_schedule or {}
        schedule = {}
        has_custom = False
        for d in range(7):
            val = raw.get(str(d)) if isinstance(raw, dict) else None
            if val is None and isinstance(raw, dict):
                val = raw.get(d)
            if isinstance(val, list) and len(val) > 0:
                has_custom = True
            schedule[d] = val if isinstance(val, list) else []

        if not has_custom:
            # Fallback to opening_time and closing_time for all days
            if self.opening_time and self.closing_time:
                o_str = self.opening_time.strftime("%H:%M")
                c_str = self.closing_time.strftime("%H:%M")
                for d in range(7):
                    schedule[d] = [{"label": "Regular Hours", "from": o_str, "to": c_str}]
            else:
                for d in range(7):
                    schedule[d] = []
        return schedule

    def get_operating_status(self, dt=None):
        """
        Checks whether this branch is currently open for orders.
        Returns a rich status dict with countdown data, labels, and formatted weekly schedule.
        """
        from django.utils import timezone
        import datetime

        if dt is None:
            dt = timezone.localtime()

        # If the parent business is inactive or the public website module is revoked by Master Admin
        if self.restaurant and (not self.restaurant.is_active or not getattr(self.restaurant, 'feature_website_pages', True)):
            return {
                "is_open": False,
                "enforce_operating_hours": True,
                "status_label": "Website & Business Inactive",
                "service_label": "Inactive",
                "opens_at": None,
                "closes_at": None,
                "next_open_label": None,
                "next_open_time": None,
                "seconds_until_next": None,
                "message": "This business and its public website are currently inactive.",
                "schedule": self.get_effective_schedule(),
            }

        # If strict operating hours are not enforced, outlet is open 24/7
        if not self.enforce_operating_hours:
            return {
                "is_open": True,
                "enforce_operating_hours": False,
                "status_label": "Operating Hours Not Enforced (24/7 Active)",
                "service_label": "24/7 Unrestricted",
                "opens_at": None,
                "closes_at": None,
                "next_open_label": None,
                "next_open_time": None,
                "seconds_until_next": None,
                "message": "",
                "schedule": self.get_effective_schedule(),
            }

        schedule = self.get_effective_schedule()
        DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        today_js_day = (dt.weekday() + 1) % 7

        def parse_time(t_str):
            try:
                parts = [int(p) for p in str(t_str).split(":")]
                return datetime.time(parts[0], parts[1])
            except Exception:
                return None

        def fmt_12h(hhmm_str):
            try:
                t = parse_time(hhmm_str)
                if not t:
                    return hhmm_str
                return t.strftime("%I:%M %p").lstrip("0")
            except Exception:
                return hhmm_str

        yesterday_js_day = (today_js_day - 1) % 7
        current_time = dt.time()

        is_open = False
        active_service = None
        closes_at_dt = None

        # Check yesterday's overnight slots
        for s in schedule.get(yesterday_js_day, []):
            f_time = parse_time(s.get("from", ""))
            t_time = parse_time(s.get("to", ""))
            if f_time and t_time and t_time < f_time:
                if current_time < t_time:
                    is_open = True
                    active_service = s
                    closes_at_dt = dt.replace(hour=t_time.hour, minute=t_time.minute, second=0, microsecond=0)
                    break

        # Check today's slots
        if not is_open:
            for s in schedule.get(today_js_day, []):
                f_time = parse_time(s.get("from", ""))
                t_time = parse_time(s.get("to", ""))
                if not f_time or not t_time:
                    continue
                if f_time <= t_time:
                    if f_time <= current_time < t_time:
                        is_open = True
                        active_service = s
                        closes_at_dt = dt.replace(hour=t_time.hour, minute=t_time.minute, second=0, microsecond=0)
                        break
                else:
                    if current_time >= f_time:
                        is_open = True
                        active_service = s
                        tomorrow = dt + datetime.timedelta(days=1)
                        closes_at_dt = tomorrow.replace(hour=t_time.hour, minute=t_time.minute, second=0, microsecond=0)
                        break

        if is_open and closes_at_dt:
            seconds_until_close = max(0, int((closes_at_dt - dt).total_seconds()))
            return {
                "is_open": True,
                "enforce_operating_hours": True,
                "status_label": f"Open now for {active_service.get('label', 'Service').lower()}",
                "service_label": active_service.get("label", "Regular Service"),
                "opens_at": fmt_12h(active_service.get("from", "")),
                "closes_at": fmt_12h(active_service.get("to", "")),
                "closes_at_iso": closes_at_dt.isoformat(),
                "seconds_until_next": seconds_until_close,
                "next_open_label": None,
                "next_open_time": None,
                "message": self.closed_message or "",
                "schedule": schedule,
            }

        # Otherwise outlet is closed: Find next opening in the next 7 days
        next_open_dt = None
        next_service = None
        when_label = "today"

        for offset in range(8):
            check_day = (today_js_day + offset) % 7
            for s in schedule.get(check_day, []):
                f_time = parse_time(s.get("from", ""))
                if not f_time:
                    continue
                slot_date = dt.date() + datetime.timedelta(days=offset)
                slot_start_dt = timezone.make_aware(
                    datetime.datetime.combine(slot_date, f_time),
                    timezone.get_current_timezone()
                )
                if slot_start_dt > dt:
                    if offset == 0:
                        when_label = "today"
                    elif offset == 1:
                        when_label = "tomorrow"
                    else:
                        when_label = f"on {DAYS[check_day]}"
                    next_open_dt = slot_start_dt
                    next_service = s
                    break
            if next_open_dt:
                break

        seconds_until_open = max(0, int((next_open_dt - dt).total_seconds())) if next_open_dt else None
        next_label = (
            f"Doors open {when_label} at {fmt_12h(next_service.get('from', ''))}"
            if (next_open_dt and next_service)
            else "Currently closed"
        )

        return {
            "is_open": False,
            "enforce_operating_hours": True,
            "status_label": "We're closed right now",
            "service_label": next_service.get("label", "Service") if next_service else "Closed",
            "opens_at": fmt_12h(next_service.get("from", "")) if next_service else None,
            "closes_at": fmt_12h(next_service.get("to", "")) if next_service else None,
            "next_open_label": next_label,
            "next_open_time": next_open_dt.isoformat() if next_open_dt else None,
            "seconds_until_next": seconds_until_open,
            "message": self.closed_message or "We are currently closed for orders.",
            "schedule": schedule,
        }



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
    class Role(models.TextChoices):
        # 1. Management Roles
        GENERAL_MANAGER = 'GENERAL_MANAGER', 'General Manager'
        RESTAURANT_ADMIN = 'RESTAURANT_ADMIN', 'Restaurant Admin'
        HOTEL_ADMIN = 'HOTEL_ADMIN', 'Hotel Admin'
        CLUB_ADMIN = 'CLUB_ADMIN', 'Club Admin'

        # 2. Restaurant & F&B Sub-Roles
        CASHIER = 'CASHIER', 'Cashier / Biller'
        KITCHEN = 'KITCHEN', 'Kitchen / KDS Station'
        WAITER = 'WAITER', 'Waiter / Captain'

        # 3. Hotel Sub-Roles
        FRONT_DESK = 'FRONT_DESK', 'Hotel Front Desk'
        HOUSEKEEPING = 'HOUSEKEEPING', 'Housekeeping Staff'

        # 4. Club & Events Sub-Roles
        GATE_SCANNER = 'GATE_SCANNER', 'Gate Scanner / Door Staff'

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='restaurant_profile')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='admins')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='admins')
    role = models.CharField(
        max_length=30,
        choices=Role.choices,
        default=Role.RESTAURANT_ADMIN,
        verbose_name="User Role"
    )
    is_primary_admin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Granular Permissions (For Sub-Roles & Operational Staff)
    can_apply_discounts = models.BooleanField(
        default=True,
        help_text="Permission to apply manual discounts on bills"
    )
    can_issue_refunds = models.BooleanField(
        default=True,
        help_text="Permission to cancel orders or issue credit notes"
    )
    can_manage_menu = models.BooleanField(
        default=True,
        help_text="Permission to add or modify menu items, prices, and categories"
    )
    can_manage_settings = models.BooleanField(
        default=True,
        help_text="Permission to modify venue/branch settings"
    )
    can_view_analytics = models.BooleanField(
        default=True,
        help_text="Permission to view sales analytics and financial reports"
    )
    can_view_inventory = models.BooleanField(
        default=True,
        help_text="Permission to access stock, purchase register, and adjustments"
    )
    pos_pin = models.CharField(
        max_length=8,
        blank=True,
        null=True,
        help_text="Quick 4-6 digit numeric PIN for fast terminal access"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_staff_profiles'
    )

    class Meta:
        unique_together = ('user', 'restaurant')
        verbose_name = 'Restaurant Staff & Admin'
        verbose_name_plural = 'Restaurant Staff & Admins'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else ""
        return f"{self.user.username} [{self.get_role_display()}] -> {self.restaurant.name}{branch_str}"

    @property
    def is_management(self):
        return self.role in [
            self.Role.GENERAL_MANAGER,
            self.Role.RESTAURANT_ADMIN,
            self.Role.HOTEL_ADMIN,
            self.Role.CLUB_ADMIN,
        ]

    @property
    def is_sub_role(self):
        return not self.is_management

    @property
    def is_pos_staff(self):
        return self.role in [self.Role.RESTAURANT_ADMIN, self.Role.CASHIER, self.Role.WAITER, self.Role.GENERAL_MANAGER]

    @property
    def is_kitchen_staff(self):
        return self.role in [self.Role.KITCHEN, self.Role.RESTAURANT_ADMIN, self.Role.GENERAL_MANAGER]

    @property
    def is_hotel_staff(self):
        return self.role in [self.Role.HOTEL_ADMIN, self.Role.FRONT_DESK, self.Role.HOUSEKEEPING, self.Role.GENERAL_MANAGER]

    @property
    def is_club_staff(self):
        return self.role in [self.Role.CLUB_ADMIN, self.Role.GATE_SCANNER, self.Role.GENERAL_MANAGER]


def _user_role(self):
    if getattr(self, 'is_superuser', False):
        return 'GENERAL_MANAGER'
    profile = getattr(self, 'restaurant_profile', None)
    if profile:
        return profile.role
    return 'RESTAURANT_ADMIN'


def _user_branch(self):
    profile = getattr(self, 'restaurant_profile', None)
    if profile and profile.branch:
        return profile.branch
    if profile and profile.restaurant:
        return profile.restaurant.branches.filter(has_hotel_module=True).first() or profile.restaurant.branches.first()
    return RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()


if not hasattr(User, 'role'):
    User.add_to_class('role', property(_user_role))
if not hasattr(User, 'branch'):
    User.add_to_class('branch', property(_user_branch))


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



class ActivityLog(models.Model):
    """Audit trail of admin activity across Restaurant, Hotel PMS and Super Admin."""

    class Action(models.TextChoices):
        CREATE = 'CREATE', 'Created'
        UPDATE = 'UPDATE', 'Updated'
        DELETE = 'DELETE', 'Deleted'
        STATUS = 'STATUS', 'Status change'
        EXPORT = 'EXPORT', 'Export'
        IMPORT = 'IMPORT', 'Import'
        SETTINGS = 'SETTINGS', 'Settings'
        SECURITY = 'SECURITY', 'Security'
        LOGIN = 'LOGIN', 'Signed in'
        LOGOUT = 'LOGOUT', 'Signed out'
        LOGIN_FAILED = 'LOGIN_FAILED', 'Failed sign-in'
        OTHER = 'OTHER', 'Other'

    class Area(models.TextChoices):
        RESTAURANT = 'restaurant', 'Restaurant'
        HOTEL = 'hotel', 'Hotel PMS'
        SUPER = 'super', 'Super Admin'
        AUTH = 'auth', 'Sign-in'

    class Outcome(models.TextChoices):
        SUCCESS = 'success', 'Success'
        INVALID = 'invalid', 'Not saved'
        FAILED = 'failed', 'Failed'

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_logs')
    username = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=40, blank=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name='activity_logs')
    branch = models.ForeignKey('RestaurantBranch', on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_logs')
    area = models.CharField(max_length=20, choices=Area.choices, blank=True, db_index=True)
    action = models.CharField(max_length=20, choices=Action.choices, default=Action.OTHER, db_index=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.SUCCESS)
    message = models.CharField(max_length=255, blank=True)
    method = models.CharField(max_length=10, blank=True)
    path = models.CharField(max_length=255, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    ip_address = models.CharField(max_length=45, blank=True)
    device = models.CharField(max_length=120, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['restaurant', '-created_at']),
            models.Index(fields=['area', '-created_at']),
        ]
        verbose_name = 'Activity Log'
        verbose_name_plural = 'Activity Logs'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.username or "system"} {self.action} {self.message}'
