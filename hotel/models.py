import uuid
import datetime
from decimal import Decimal
from django.db import models
from django.utils.text import slugify
from django.utils import timezone


class RoomCategory(models.Model):
    """
    Room Category/Type model representing a class of rooms (e.g., Deluxe Suite, Executive Villa).
    Scoped to a specific RestaurantBranch with has_hotel_module enabled.
    """
    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='room_categories'
    )
    name = models.CharField(max_length=150, verbose_name="Category Name")
    slug = models.SlugField(max_length=180, blank=True)
    description = models.TextField(blank=True)
    base_price_per_night = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Base Price / Night (₹)"
    )
    max_occupancy = models.PositiveIntegerField(default=2, verbose_name="Max Total Occupancy")
    max_adults = models.PositiveIntegerField(default=2, verbose_name="Max Adults")
    max_children = models.PositiveIntegerField(default=1, verbose_name="Max Children")
    bed_type = models.CharField(max_length=100, default="King Bed", blank=True)
    room_size_sqft = models.PositiveIntegerField(default=350, blank=True, null=True, verbose_name="Room Size (sq ft)")
    amenities = models.JSONField(default=list, blank=True, help_text="List of amenities e.g. ['Wi-Fi', 'AC', 'Mini Bar']")
    cover_image = models.ImageField(upload_to='hotel/categories/', blank=True, null=True)
    cover_image_url = models.URLField(blank=True, default="", help_text="Fallback external or CDN image URL")
    
    # Pricing & Tax Structure
    price_is_tax_inclusive = models.BooleanField(
        default=False,
        verbose_name="Price is Tax Inclusive",
        help_text="If checked, base price already includes all taxes/GST"
    )
    original_price_per_night = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Original / Rack Rate (₹)",
        help_text="Higher reference/rack rate displayed struck out (e.g. ₹6,000)"
    )
    custom_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Custom Tax Rate (%)",
        help_text="Optional custom tax % for this category; if blank, branch default GST applies"
    )
    
    # Banner & Promotional Highlights
    show_on_banner = models.BooleanField(
        default=False,
        verbose_name="Show on Banner / Hero Highlights",
        help_text="Prominently highlight this category on the public hero banner"
    )
    banner_badge_text = models.CharField(
        max_length=60,
        blank=True,
        default="",
        verbose_name="Banner Promo Badge",
        help_text="e.g. 'Best Seller', 'Special Deal', 'Flat 20% Off', 'Exclusive Suite'"
    )

    # Gallery Images & Videos (Max 5)
    gallery_images = models.JSONField(
        default=list,
        blank=True,
        help_text="List of external gallery image URLs or JSON objects"
    )
    video_links = models.JSONField(
        default=list,
        blank=True,
        help_text="List of up to 5 video links/embed objects: [{'url': '...', 'title': '...'}]"
    )

    # Travel Desk & Complimentary Transfers
    includes_free_transfer = models.BooleanField(
        default=False,
        verbose_name="Includes Free Airport / Station Transfer",
        help_text="If checked, guests booking this tier receive complimentary pickup, drop, or both"
    )
    transfer_type = models.CharField(
        max_length=20,
        choices=[
            ('NONE', 'None'),
            ('PICKUP_ONLY', 'Pickup Only'),
            ('DROP_ONLY', 'Drop Only'),
            ('BOTH', 'Both'),
        ],
        default='NONE',
        verbose_name="Complimentary Transfer Type"
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Room Category'
        verbose_name_plural = 'Room Categories'
        unique_together = ('branch', 'name')

    def __str__(self):
        return f"{self.name} - ₹{self.base_price_per_night} ({self.branch.name})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or f"category-{uuid.uuid4().hex[:6]}"
            slug = base
            counter = 1
            while RoomCategory.objects.filter(branch=self.branch, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def total_rooms_count(self):
        return self.rooms.filter(is_active=True).count()

    @property
    def effective_image_url(self):
        if self.cover_image:
            return self.cover_image.url
        if self.cover_image_url:
            return self.cover_image_url
        first_img = self.images.first()
        if first_img:
            return first_img.effective_url
        if self.gallery_images and isinstance(self.gallery_images, list) and len(self.gallery_images) > 0:
            first = self.gallery_images[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict) and first.get('url'):
                return first['url']
        return 'https://images.unsplash.com/photo-1582719478250-c89cae4dc85b?q=80&w=1200&auto=format&fit=crop'

    @property
    def discount_percent(self):
        if self.original_price_per_night and self.base_price_per_night:
            if self.original_price_per_night > self.base_price_per_night:
                diff = self.original_price_per_night - self.base_price_per_night
                return int(round((diff / self.original_price_per_night) * 100))
        return 0

    @property
    def all_images_list(self):
        items = []
        if self.cover_image:
            items.append({'url': self.cover_image.url, 'caption': 'Cover Photo', 'is_cover': True})
        elif self.cover_image_url:
            items.append({'url': self.cover_image_url, 'caption': 'Cover Photo', 'is_cover': True})
            
        for img in self.images.all():
            u = img.effective_url
            if u and not any(x['url'] == u for x in items):
                items.append({'id': img.id, 'url': u, 'caption': img.caption or 'Gallery Photo', 'is_cover': False})
                
        if isinstance(self.gallery_images, list):
            for g in self.gallery_images:
                if isinstance(g, str) and g.strip() and not any(x['url'] == g.strip() for x in items):
                    items.append({'url': g.strip(), 'caption': 'Gallery Photo', 'is_cover': False})
                elif isinstance(g, dict) and g.get('url') and not any(x['url'] == g['url'].strip() for x in items):
                    items.append({'url': g['url'].strip(), 'caption': g.get('caption', 'Gallery Photo'), 'is_cover': False})
        return items

    @property
    def video_links_list(self):
        if not isinstance(self.video_links, list):
            return []
        res = []
        for idx, v in enumerate(self.video_links[:5], start=1):
            if isinstance(v, str) and v.strip():
                res.append({'url': v.strip(), 'title': f'Video Tour #{idx}'})
            elif isinstance(v, dict) and v.get('url'):
                res.append({'url': v['url'].strip(), 'title': v.get('title') or f'Video Tour #{idx}'})
        return res


class RoomCategoryImage(models.Model):
    """
    Individual uploaded photo or gallery image for a RoomCategory.
    """
    category = models.ForeignKey(
        RoomCategory,
        on_delete=models.CASCADE,
        related_name='images'
    )
    image = models.ImageField(upload_to='hotel/categories/gallery/', blank=True, null=True)
    image_url = models.URLField(blank=True, default="", help_text="External image URL")
    caption = models.CharField(max_length=150, blank=True, default="")
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']
        verbose_name = 'Room Category Image'
        verbose_name_plural = 'Room Category Images'

    def __str__(self):
        return f"{self.category.name} - Image #{self.id}"

    @property
    def effective_url(self):
        if self.image:
            return self.image.url
        return self.image_url


class Room(models.Model):
    """
    Physical Room Inventory instance (e.g., Room 101, Room 202).
    """
    class RoomStatus(models.TextChoices):
        AVAILABLE = 'AVAILABLE', 'Available'
        OCCUPIED = 'OCCUPIED', 'Occupied'
        MAINTENANCE = 'MAINTENANCE', 'Maintenance'
        CLEANING = 'CLEANING', 'Cleaning'

    category = models.ForeignKey(
        RoomCategory,
        on_delete=models.CASCADE,
        related_name='rooms'
    )
    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='hotel_rooms'
    )
    room_number = models.CharField(max_length=50, verbose_name="Room Number (e.g. 101A)")
    floor = models.CharField(max_length=50, default="1st Floor", blank=True)
    status = models.CharField(
        max_length=20,
        choices=RoomStatus.choices,
        default=RoomStatus.AVAILABLE,
        verbose_name="Room Status"
    )
    notes = models.TextField(blank=True, help_text="Housekeeping or operational notes")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['room_number']
        verbose_name = 'Room'
        verbose_name_plural = 'Rooms'
        unique_together = ('branch', 'room_number')

    def __str__(self):
        return f"Room {self.room_number} [{self.category.name}] - {self.get_status_display()}"


class RoomBooking(models.Model):
    """
    Room Booking Reservation model to block dates, capture guest folios, and track billing.
    """
    class PaymentStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PAID = 'PAID', 'Paid'
        PARTIALLY_PAID = 'PARTIALLY_PAID', 'Partially Paid'
        REFUNDED = 'REFUNDED', 'Refunded'

    class BookingStatus(models.TextChoices):
        RESERVED = 'RESERVED', 'Reserved'
        CHECKED_IN = 'CHECKED_IN', 'Checked-In'
        CHECKED_OUT = 'CHECKED_OUT', 'Checked-Out'
        CANCELLED = 'CANCELLED', 'Cancelled'
        NO_SHOW = 'NO_SHOW', 'No-show'

    class Source(models.TextChoices):
        DIRECT = 'DIRECT', 'Front desk / phone'
        WALK_IN = 'WALK_IN', 'Walk-in'
        WEBSITE = 'WEBSITE', 'Website'
        OTA = 'OTA', 'OTA / channel'
        CORPORATE = 'CORPORATE', 'Corporate'
        AGENT = 'AGENT', 'Travel agent'

    class IdType(models.TextChoices):
        AADHAAR = 'AADHAAR', 'Aadhaar'
        PASSPORT = 'PASSPORT', 'Passport'
        DRIVING_LICENCE = 'DRIVING_LICENCE', 'Driving licence'
        VOTER_ID = 'VOTER_ID', 'Voter ID'
        PAN = 'PAN', 'PAN card'
        OTHER = 'OTHER', 'Other'

    booking_id = models.CharField(max_length=50, unique=True, editable=False)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name='bookings'
    )
    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='hotel_bookings'
    )
    guest_name = models.CharField(max_length=150, verbose_name="Guest Name")
    guest_email = models.EmailField(verbose_name="Guest Email")
    guest_phone = models.CharField(max_length=30, verbose_name="Guest Phone")
    check_in_date = models.DateField(verbose_name="Check-In Date")
    check_out_date = models.DateField(verbose_name="Check-Out Date")
    check_in_time = models.DateTimeField(null=True, blank=True, verbose_name="Actual Check-In Timestamp")
    check_out_time = models.DateTimeField(null=True, blank=True, verbose_name="Actual Check-Out Timestamp")
    num_guests = models.PositiveIntegerField(default=1, verbose_name="Number of Guests")

    # Financials
    total_room_charges = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    taxes = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING
    )
    payment_method = models.CharField(max_length=50, default="PAY_AT_DESK", blank=True)
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Razorpay Order ID")
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Razorpay Payment ID")
    razorpay_signature = models.CharField(max_length=255, blank=True, null=True, verbose_name="Razorpay Signature")
    upi_transaction_ref = models.CharField(max_length=100, blank=True, null=True, verbose_name="UPI UTR / Ref Number")
    booking_status = models.CharField(
        max_length=20,
        choices=BookingStatus.choices,
        default=BookingStatus.RESERVED
    )
    special_requests = models.TextField(blank=True)

    # Front office
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.DIRECT)
    adults = models.PositiveSmallIntegerField(default=1)
    children = models.PositiveSmallIntegerField(default=0)
    internal_notes = models.TextField(blank=True, help_text="Staff-only notes, never shown to the guest")
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    invoice_number = models.CharField(max_length=40, blank=True, db_index=True)
    invoiced_at = models.DateTimeField(null=True, blank=True)

    # Guest registration (KYC / Form C)
    id_type = models.CharField(max_length=20, choices=IdType.choices, blank=True)
    id_number = models.CharField(max_length=60, blank=True)
    id_document = models.FileField(upload_to='hotel/kyc/', blank=True, null=True)
    nationality = models.CharField(max_length=60, blank=True, default='Indian')
    guest_address = models.TextField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    purpose_of_visit = models.CharField(max_length=100, blank=True)
    arriving_from = models.CharField(max_length=120, blank=True)
    going_to = models.CharField(max_length=120, blank=True)
    visa_number = models.CharField(max_length=60, blank=True)
    visa_valid_till = models.DateField(null=True, blank=True)
    company_name = models.CharField(max_length=150, blank=True, help_text="Bill to company (B2B invoice)")
    company_gstin = models.CharField(max_length=20, blank=True)
    company_account = models.ForeignKey('hotel.CompanyAccount', on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='bookings', help_text="City-ledger account the balance can be billed to")
    group = models.ForeignKey('hotel.BookingGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Room Booking'
        verbose_name_plural = 'Room Bookings'

    def __str__(self):
        return f"{self.booking_id}: {self.guest_name} -> Room {self.room.room_number} ({self.get_booking_status_display()})"

    def save(self, *args, **kwargs):
        if not self.booking_id:
            self.booking_id = f"BK-HTL-{uuid.uuid4().hex[:8].upper()}"

        # Automatic branch synchronization from room
        if self.room and not self.branch_id:
            self.branch = self.room.branch

        # Auto-calculate totals if not provided or 0
        if (not self.grand_total or self.grand_total == Decimal('0.00')) and self.room and self.check_in_date and self.check_out_date \
                and self.booking_status not in (self.BookingStatus.NO_SHOW, self.BookingStatus.CANCELLED):
            from hotel.pricing import quote
            q = quote(self.room.category, self.check_in_date, max(self.check_out_date, self.check_in_date + datetime.timedelta(days=1)),
                      HotelSettings.objects.filter(branch_id=self.branch_id or self.room.branch_id).first())
            self.total_room_charges = q['net']
            self.taxes = q['tax']
            self.grand_total = q['total']

        super().save(*args, **kwargs)

    @property
    def nights_count(self):
        if self.check_in_date and self.check_out_date:
            return max(1, (self.check_out_date - self.check_in_date).days)
        return 1

    @property
    def is_foreign_national(self):
        return bool(self.nationality) and self.nationality.strip().lower() not in ('indian', 'india', 'in')

    @property
    def kyc_complete(self):
        if not (self.id_type and self.id_number):
            return False
        if self.is_foreign_national:
            # Form C needs the passport and visa of every foreign guest
            return self.id_type == self.IdType.PASSPORT and bool(self.visa_number)
        return True

    def folio(self):
        from hotel.folio import Folio
        return Folio(self)


class HotelSettings(models.Model):
    """
    Branch-level configuration for Hotel PMS, including Payment Gateway credentials,
    GST/Taxation compliance, check-in/out policies, and receipt customization.
    """
    class PaymentGateway(models.TextChoices):
        RAZORPAY = 'RAZORPAY', 'Razorpay (Cards, UPI, NetBanking)'
        STRIPE = 'STRIPE', 'Stripe (Global Credit/Debit Cards)'
        PHONEPE = 'PHONEPE', 'PhonePe PG'
        PAYU = 'PAYU', 'PayU'

    class EnvironmentMode(models.TextChoices):
        TEST = 'TEST', 'Sandbox / Test Mode'
        LIVE = 'LIVE', 'Live Production Mode'

    branch = models.OneToOneField(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='hotel_settings'
    )

    # General Hotel & Property Information
    hotel_name = models.CharField(max_length=150, blank=True, verbose_name="Hotel Property Display Name")
    contact_email = models.EmailField(blank=True, verbose_name="Reservation Desk Email")
    contact_phone = models.CharField(max_length=30, blank=True, verbose_name="Front Desk Helpline")
    address = models.TextField(blank=True, verbose_name="Property Full Address")
    check_in_time = models.TimeField(default=datetime.time(14, 0), verbose_name="Standard Check-In Time")
    check_out_time = models.TimeField(default=datetime.time(11, 0), verbose_name="Standard Check-Out Time")
    cancellation_grace_hours = models.PositiveIntegerField(
        default=24,
        verbose_name="Free Cancellation Grace (Hours)",
        help_text="Hours before check-in date when free cancellation is permitted"
    )
    cancellation_policy = models.TextField(
        blank=True,
        default="Free cancellation is allowed up to 24 hours prior to scheduled check-in. Cancellations made within 24 hours are subject to a 1-night retention fee.",
        verbose_name="Cancellation & Refund Terms"
    )

    # Taxes & GST Compliance
    gstin = models.CharField(max_length=25, blank=True, verbose_name="GSTIN / Tax Registration Number")
    enable_dynamic_gst = models.BooleanField(
        default=True,
        verbose_name="Enable Dynamic Indian Hospitality GST Slabs",
        help_text="Automatically applies 12% GST for tariff <= ₹7,500/night and 18% GST for tariff > ₹7,500/night"
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('12.00'),
        verbose_name="Custom Flat Tax Rate (%)",
        help_text="Used when dynamic GST slabs are disabled"
    )
    service_charge_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Hotel Service Charge (%)"
    )
    tax_inclusive_pricing = models.BooleanField(
        default=False,
        verbose_name="Tax-Inclusive Room Pricing",
        help_text="Check if room base tariffs displayed to guests already include GST taxes"
    )

    # Online Payment Gateway Settings
    enable_online_payment = models.BooleanField(
        default=True,
        verbose_name="Enable Online Card / UPI Payment Gateway"
    )
    enable_pay_at_desk = models.BooleanField(
        default=True,
        verbose_name="Enable Pay at Front Desk (Cash / Card on Arrival)"
    )
    payment_gateway_provider = models.CharField(
        max_length=30,
        choices=PaymentGateway.choices,
        default=PaymentGateway.RAZORPAY,
        verbose_name="Primary Online Payment Provider"
    )
    payment_mode = models.CharField(
        max_length=10,
        choices=EnvironmentMode.choices,
        default=EnvironmentMode.TEST,
        verbose_name="Gateway Environment Mode"
    )
    razorpay_key_id = models.CharField(max_length=255, blank=True, verbose_name="Razorpay Key ID")
    razorpay_key_secret = models.CharField(max_length=255, blank=True, verbose_name="Razorpay Key Secret")
    stripe_publishable_key = models.CharField(max_length=255, blank=True, verbose_name="Stripe Publishable Key")
    stripe_secret_key = models.CharField(max_length=255, blank=True, verbose_name="Stripe Secret Key")
    upi_id = models.CharField(max_length=100, blank=True, verbose_name="Direct UPI VPA / QR ID", help_text="e.g. sharmahotel@okhdfcbank")

    # Invoicing & Guest Folio Formatting
    invoice_prefix = models.CharField(max_length=20, default="INV-HTL", verbose_name="Invoice Number Prefix")
    invoice_next_number = models.PositiveIntegerField(default=1, verbose_name="Next Invoice Number")
    require_id_at_check_in = models.BooleanField(
        default=False, verbose_name="Require guest ID before check-in",
        help_text="Block check-in until the ID type and number (and passport + visa for foreign nationals) are recorded"
    )
    block_checkout_with_balance = models.BooleanField(
        default=True, verbose_name="Ask before checking out a guest with a balance due"
    )
    # Energy & utilities
    tariff_electricity = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('8.50'), verbose_name="Electricity ₹ per kWh")
    tariff_water = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('45'), verbose_name="Water ₹ per kL")
    tariff_lpg = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('95'), verbose_name="LPG ₹ per kg")
    tariff_diesel = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('92'), verbose_name="Diesel ₹ per litre")
    energy_target_kwh = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('35'),
                                            verbose_name="Target kWh per occupied room")
    invoice_footer_notes = models.TextField(
        blank=True,
        default="Thank you for staying with us! We hope you enjoyed your luxury experience and look forward to welcoming you back.",
        verbose_name="Invoice Footer Note"
    )
    currency_symbol = models.CharField(max_length=10, default="₹", verbose_name="Currency Symbol")

    # Hotel Custom SMTP / Mailing Credentials
    email_is_active = models.BooleanField(
        default=True,
        verbose_name="Enable Hotel Custom SMTP",
        help_text="Use hotel-specific email account for guest confirmations and invoices"
    )
    email_sender_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        verbose_name="Hotel Sender Display Name",
        help_text="Name displayed to guests (e.g. 'Royal Heritage Palace')"
    )
    email_host_user = models.EmailField(
        blank=True,
        default="",
        verbose_name="Hotel SMTP Email Address",
        help_text="e.g. reservations@royalheritage.com or hotel.jaipur@gmail.com"
    )
    email_host_password = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Hotel SMTP Password / App Password",
        help_text="16-character Google App Password or SMTP password"
    )
    email_host = models.CharField(
        max_length=150,
        default="smtp.gmail.com",
        blank=True,
        verbose_name="SMTP Host"
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Hotel Settings'
        verbose_name_plural = 'Hotel Settings'

    def __str__(self):
        return f"Hotel Settings - {self.branch.name}"

    @classmethod
    def get_settings(cls, branch):
        """
        Retrieves or initializes default HotelSettings for the specified branch.
        """
        if not branch:
            return None
        settings_obj, _ = cls.objects.get_or_create(
            branch=branch,
            defaults={
                'hotel_name': branch.name,
                'contact_phone': getattr(branch, 'phone', '') or getattr(branch, 'phone_number', '') or '',
                'address': getattr(branch, 'address', '') or '',
            }
        )
        return settings_obj


class CabServicePackage(models.Model):
    """
    Catalog for airport/station transfers, local sightseeing/tourism packages,
    hourly rentals, and point-to-point transfers.
    """
    class ServiceType(models.TextChoices):
        AIRPORT_TRANSFER = 'AIRPORT_TRANSFER', 'Airport Transfer'
        TOURISM_PACKAGE = 'TOURISM_PACKAGE', 'Tourism / Sightseeing Package'
        POINT_TO_POINT = 'POINT_TO_POINT', 'Point to Point / City Ride'
        HOURLY_RENTAL = 'HOURLY_RENTAL', 'Hourly Rental'
        OUTSTATION = 'OUTSTATION', 'Outstation Tour'

    class VehicleCategory(models.TextChoices):
        SEDAN = 'SEDAN', 'Luxury Sedan'
        SUV = 'SUV', 'Executive SUV'
        LUXURY_VAN = 'LUXURY_VAN', 'Royal MPV / Luxury Van'
        ROYAL_MPV = 'ROYAL_MPV', 'Royal MPV'
        ELECTRIC = 'ELECTRIC', 'Electric Luxury'
        LIMOUSINE = 'LIMOUSINE', 'Grand Limousine'
        TEMPO_TRAVELLER = 'TEMPO_TRAVELLER', 'Tempo Traveller'

    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='cab_packages'
    )
    title = models.CharField(max_length=200, verbose_name="Package Title")
    service_type = models.CharField(
        max_length=30,
        choices=ServiceType.choices,
        default=ServiceType.AIRPORT_TRANSFER,
        verbose_name="Service Type"
    )
    vehicle_category = models.CharField(
        max_length=30,
        choices=VehicleCategory.choices,
        default=VehicleCategory.SEDAN,
        verbose_name="Vehicle Category"
    )
    base_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Base Price (₹)"
    )
    included_km = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name="Included Distance (km)",
        help_text="Included kilometers (e.g. 80 km)"
    )
    included_hours = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name="Included Duration (Hours)",
        help_text="Included duration in hours (e.g. 8 hours)"
    )
    itinerary_description = models.TextField(
        blank=True,
        verbose_name="Itinerary / Description",
        help_text="Package itinerary, sightseeing spots, or route details"
    )
    is_active = models.BooleanField(default=True, verbose_name="Is Active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']
        verbose_name = 'Cab Service Package'
        verbose_name_plural = 'Cab Service Packages'

    def __str__(self):
        return f"{self.title} ({self.get_vehicle_category_display()}) - ₹{self.base_price}"


class FleetVehicle(models.Model):
    """
    Fleet Vehicle inventory tracking hotel-owned and outsourced vendor agency cabs.
    """
    class VehicleStatus(models.TextChoices):
        AVAILABLE = 'AVAILABLE', 'Available'
        ON_TRIP = 'ON_TRIP', 'On Trip'
        MAINTENANCE = 'MAINTENANCE', 'Maintenance'

    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='fleet_vehicles'
    )
    vehicle_number = models.CharField(max_length=50, verbose_name="Vehicle License Number")
    vehicle_model = models.CharField(max_length=150, blank=True, default='', verbose_name="Vehicle Model / Make")
    vehicle_category = models.CharField(
        max_length=30,
        choices=CabServicePackage.VehicleCategory.choices,
        default=CabServicePackage.VehicleCategory.SEDAN,
        verbose_name="Vehicle Category"
    )
    tag = models.CharField(max_length=100, blank=True, default='Most Popular', verbose_name="Tag / Highlight Badge")
    description = models.TextField(blank=True, default='', verbose_name="Overview / Editorial Description")

    # Vehicle Media & Gallery
    cover_image = models.URLField(blank=True, default='', max_length=500, verbose_name="Cover Image URL")
    gallery_images = models.JSONField(default=list, blank=True, verbose_name="Gallery Images (URLs list)")

    # Vehicle Specifications Matrix
    passengers = models.PositiveIntegerField(default=4, verbose_name="Max Guests / Seats")
    luggage = models.PositiveIntegerField(default=3, verbose_name="Luggage Capacity (Bags)")
    fuel_type = models.CharField(max_length=30, default='Petrol', verbose_name="Fuel Type / Drivetrain")
    transmission = models.CharField(max_length=30, default='Automatic', verbose_name="Transmission")

    # Journey Features & Cabin Amenities
    features = models.JSONField(default=list, blank=True, verbose_name="Key Journey Features (List)")
    amenities = models.JSONField(default=list, blank=True, verbose_name="Cabin Luxuries (List)")

    # Transparent Pricing Structure
    price_per_km = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('35.00'), verbose_name="Per KM Rate (₹/km)")
    base_price_airport = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('2500.00'), verbose_name="Airport Flat Rate (₹)")
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('800.00'), verbose_name="Hourly Rate 4h (₹)")
    full_day_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('7500.00'), verbose_name="Full Day Rate 8h (₹)")

    # Rating & Reviews
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('4.95'), verbose_name="Cab Rating")
    reviews_count = models.PositiveIntegerField(default=120, verbose_name="Verified Reviews Count")

    # Chauffeur Profile
    driver_name = models.CharField(max_length=150, verbose_name="Driver Name")
    driver_title = models.CharField(max_length=150, blank=True, default='Executive Chauffeur', verbose_name="Driver Title / Role")
    driver_phone = models.CharField(max_length=30, verbose_name="Driver Contact Phone")
    driver_photo = models.URLField(blank=True, default='', max_length=500, verbose_name="Driver Photo URL")
    driver_rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('4.95'), verbose_name="Driver Rating")
    driver_experience_years = models.PositiveIntegerField(default=5, verbose_name="Experience (Years)")
    trips_completed = models.PositiveIntegerField(default=150, verbose_name="Trips Completed")
    driver_bio = models.TextField(blank=True, default='', verbose_name="Chauffeur Bio")
    driver_languages = models.CharField(max_length=255, blank=True, default='English, Hindi', verbose_name="Languages Spoken (comma separated)")
    driver_badges = models.CharField(max_length=255, blank=True, default='Executive Lead, Defensive Driving Certified', verbose_name="Badges / Certifications (comma separated)")
    driver_availability = models.CharField(max_length=30, default='Available Now', verbose_name="Chauffeur Availability")

    is_vendor_vehicle = models.BooleanField(
        default=False,
        verbose_name="Is Vendor / Outsourced Vehicle",
        help_text="Checked if sourced from an external taxi agency or third-party vendor"
    )
    status = models.CharField(
        max_length=20,
        choices=VehicleStatus.choices,
        default=VehicleStatus.AVAILABLE,
        verbose_name="Operational Status"
    )
    notes = models.TextField(blank=True, verbose_name="Notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['vehicle_number']
        verbose_name = 'Fleet Vehicle'
        verbose_name_plural = 'Fleet Vehicles'
        unique_together = ('branch', 'vehicle_number')

    def __str__(self):
        v_type = "Vendor" if self.is_vendor_vehicle else "In-House"
        model_str = f" ({self.vehicle_model})" if self.vehicle_model else ""
        return f"{self.vehicle_number}{model_str} [{self.get_vehicle_category_display()}] - {self.driver_name} ({v_type})"

    @property
    def features_text(self):
        if isinstance(self.features, list):
            return "\n".join(self.features)
        return str(self.features or '')

    @property
    def amenities_text(self):
        if isinstance(self.amenities, list):
            return "\n".join(self.amenities)
        return str(self.amenities or '')

    @property
    def gallery_images_text(self):
        if isinstance(self.gallery_images, list):
            return "\n".join(self.gallery_images)
        return str(self.gallery_images or '')

    @property
    def languages_list(self):
        if not self.driver_languages:
            return ["English", "Hindi"]
        return [s.strip() for s in self.driver_languages.split(',') if s.strip()]

    @property
    def badges_list(self):
        if not self.driver_badges:
            return ["Executive Lead", "Defensive Driving Certified"]
        return [s.strip() for s in self.driver_badges.split(',') if s.strip()]


class CabBooking(models.Model):
    """
    Cab reservation & dispatch tracking. Supports both linked RoomBooking and direct Web/PMS bookings.
    """
    class PaymentStatus(models.TextChoices):
        COMPLIMENTARY = 'COMPLIMENTARY', 'Complimentary'
        PAID = 'PAID', 'Paid'
        ADDED_TO_FOLIO = 'ADDED_TO_FOLIO', 'Added to Folio'
        PENDING = 'PENDING', 'Pending'

    class TripStatus(models.TextChoices):
        SCHEDULED = 'SCHEDULED', 'Scheduled'
        DRIVER_ASSIGNED = 'DRIVER_ASSIGNED', 'Driver Assigned'
        EN_ROUTE = 'EN_ROUTE', 'En Route'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class BookingSource(models.TextChoices):
        FRONTEND_DIRECT = 'FRONTEND_DIRECT', 'Direct Web Booking'
        PMS_FRONT_DESK = 'PMS_FRONT_DESK', 'Front Desk Manual Ride'
        ROOM_ADDON = 'ROOM_ADDON', 'Room Reservation Add-on'

    booking_id = models.CharField(max_length=50, unique=True, editable=False)
    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='cab_bookings'
    )
    room_booking = models.ForeignKey(
        RoomBooking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cab_bookings'
    )
    package = models.ForeignKey(
        CabServicePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cab_bookings'
    )
    assigned_vehicle = models.ForeignKey(
        FleetVehicle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cab_bookings'
    )
    
    # Direct Guest Information
    guest_name = models.CharField(max_length=150, blank=True, default='', verbose_name="Guest / Passenger Name")
    guest_phone = models.CharField(max_length=30, blank=True, default='', verbose_name="Guest Phone")
    guest_email = models.EmailField(blank=True, default='', verbose_name="Guest Email")

    # Trip and Vehicle Specifications
    trip_type = models.CharField(
        max_length=30,
        choices=CabServicePackage.ServiceType.choices,
        default=CabServicePackage.ServiceType.AIRPORT_TRANSFER,
        verbose_name="Trip Type"
    )
    selected_cab_name = models.CharField(max_length=150, blank=True, default='', verbose_name="Selected Vehicle / Class")
    preferred_chauffeur = models.CharField(max_length=150, blank=True, default='', verbose_name="Preferred Chauffeur")
    passengers = models.PositiveIntegerField(default=1, verbose_name="Passengers")
    luggage = models.PositiveIntegerField(default=1, verbose_name="Luggage (Bags)")
    duration_hours = models.PositiveIntegerField(default=0, blank=True, null=True, verbose_name="Duration (Hours)")
    special_requests = models.TextField(blank=True, default='', verbose_name="Special Requests & Amenities")

    source = models.CharField(
        max_length=30,
        choices=BookingSource.choices,
        default=BookingSource.FRONTEND_DIRECT,
        verbose_name="Booking Origin"
    )

    is_complimentary = models.BooleanField(
        default=False,
        verbose_name="Is Complimentary (₹0)"
    )
    pickup_datetime = models.DateTimeField(verbose_name="Pickup Date & Time")
    pickup_location = models.CharField(max_length=255, verbose_name="Pickup Location")
    drop_location = models.CharField(max_length=255, verbose_name="Drop Location")
    flight_or_train_number = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        verbose_name="Flight / Train Number"
    )
    base_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Base Fare (₹)"
    )
    tax_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Tax / GST (₹)"
    )
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Fare (₹)"
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        verbose_name="Payment Status"
    )
    trip_status = models.CharField(
        max_length=20,
        choices=TripStatus.choices,
        default=TripStatus.SCHEDULED,
        verbose_name="Trip Status"
    )
    guest_notes = models.TextField(blank=True, verbose_name="Guest Notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pickup_datetime']
        verbose_name = 'Cab Booking'
        verbose_name_plural = 'Cab Bookings'

    def __str__(self):
        return f"{self.booking_id}: {self.pickup_location} -> {self.drop_location} ({self.get_trip_status_display()})"

    @property
    def display_guest_name(self):
        if self.guest_name:
            return self.guest_name
        if self.room_booking and self.room_booking.guest_name:
            return self.room_booking.guest_name
        return "Valued Guest"

    @property
    def display_guest_phone(self):
        if self.guest_phone:
            return self.guest_phone
        if self.room_booking and self.room_booking.guest_phone:
            return self.room_booking.guest_phone
        return ""

    @property
    def display_guest_email(self):
        if self.guest_email:
            return self.guest_email
        if self.room_booking and self.room_booking.guest_email:
            return self.room_booking.guest_email
        return ""

    @property
    def package_title(self):
        if self.package:
            return self.package.title
        if self.selected_cab_name:
            return self.selected_cab_name
        return "Custom Transfer"

    @property
    def package_vehicle_category(self):
        if self.package:
            return self.package.get_vehicle_category_display()
        if self.assigned_vehicle:
            return self.assigned_vehicle.get_vehicle_category_display()
        return "Sedan"

    def save(self, *args, **kwargs):
        if not self.booking_id:
            self.booking_id = f"CB-HTL-{uuid.uuid4().hex[:8].upper()}"
        if not self.branch_id and self.room_booking:
            self.branch = self.room_booking.branch
        if self.room_booking:
            if not self.guest_name and self.room_booking.guest_name:
                self.guest_name = self.room_booking.guest_name
            if not self.guest_phone and self.room_booking.guest_phone:
                self.guest_phone = self.room_booking.guest_phone
            if not self.guest_email and self.room_booking.guest_email:
                self.guest_email = self.room_booking.guest_email
        super().save(*args, **kwargs)


# Signal for Room Cancellation Cascade
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=RoomBooking)
def cascade_cancel_cab_bookings(sender, instance, **kwargs):
    """
    If a RoomBooking is cancelled, automatically trigger cancellation of
    all linked CabBooking records and release any assigned FleetVehicle.
    """
    if instance.booking_status == RoomBooking.BookingStatus.CANCELLED:
        linked_cabs = instance.cab_bookings.exclude(trip_status=CabBooking.TripStatus.CANCELLED)
        for cab in linked_cabs:
            if cab.assigned_vehicle:
                vehicle = cab.assigned_vehicle
                # Release vehicle if no other active ride is assigned to it
                other_active = CabBooking.objects.filter(
                    assigned_vehicle=vehicle,
                    trip_status__in=[CabBooking.TripStatus.DRIVER_ASSIGNED, CabBooking.TripStatus.EN_ROUTE]
                ).exclude(pk=cab.pk).exists()
                if not other_active and vehicle.status == FleetVehicle.VehicleStatus.ON_TRIP:
                    vehicle.status = FleetVehicle.VehicleStatus.AVAILABLE
                    vehicle.save(update_fields=['status', 'updated_at'])
            cab.trip_status = CabBooking.TripStatus.CANCELLED
            cab.save(update_fields=['trip_status', 'updated_at'])


@receiver(post_save, sender=RoomBooking)
def open_folio_payment(sender, instance, **kwargs):
    """A booking marked paid outside the folio (website checkout, OTA, older screens) gets a matching payment line."""
    if instance.payment_status == RoomBooking.PaymentStatus.PAID:
        from hotel.folio import opening_payment
        opening_payment(instance)



# ---------------------------------------------------------------------------
# Revenue: seasonal rates and restrictions
# ---------------------------------------------------------------------------

class RateOverride(models.Model):
    """A price and/or restriction for a room type over a date range (seasons, events, weekends)."""
    WEEKDAYS = [(0, 'Mon'), (1, 'Tue'), (2, 'Wed'), (3, 'Thu'), (4, 'Fri'), (5, 'Sat'), (6, 'Sun')]

    category = models.ForeignKey(RoomCategory, on_delete=models.CASCADE, related_name='rate_overrides')
    label = models.CharField(max_length=80, blank=True, help_text="e.g. Diwali peak, Weekend, Monsoon offer")
    start_date = models.DateField()
    end_date = models.DateField(help_text="Last night the rule applies to (inclusive)")
    weekdays = models.JSONField(default=list, blank=True, help_text="Nights of the week it applies to; empty = every night")
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                          help_text="Leave blank to keep the base price and only apply restrictions")
    min_stay = models.PositiveSmallIntegerField(default=1)
    stop_sell = models.BooleanField(default=False, help_text="Close this room type for these nights")
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"{self.category.name} {self.start_date}–{self.end_date} {self.label}"

    def applies_to(self, night):
        return self.start_date <= night <= self.end_date and (not self.weekdays or night.weekday() in self.weekdays)


# ---------------------------------------------------------------------------
# Guest folio: extra charges and payments
# ---------------------------------------------------------------------------

class FolioCharge(models.Model):
    class Kind(models.TextChoices):
        ROOM_SERVICE = 'ROOM_SERVICE', 'Room service'
        RESTAURANT = 'RESTAURANT', 'Restaurant / bar'
        MINIBAR = 'MINIBAR', 'Minibar'
        LAUNDRY = 'LAUNDRY', 'Laundry'
        SPA = 'SPA', 'Spa & wellness'
        TRANSPORT = 'TRANSPORT', 'Transport'
        EXTRA_BED = 'EXTRA_BED', 'Extra bed / person'
        EARLY_CHECKIN = 'EARLY_CHECKIN', 'Early check-in'
        LATE_CHECKOUT = 'LATE_CHECKOUT', 'Late check-out'
        ROOM_ADJUSTMENT = 'ROOM_ADJUSTMENT', 'Room charge adjustment'
        DAMAGE = 'DAMAGE', 'Damage / loss'
        MISC = 'MISC', 'Miscellaneous'
        DISCOUNT = 'DISCOUNT', 'Discount / allowance'

    booking = models.ForeignKey(RoomBooking, on_delete=models.CASCADE, related_name='folio_charges')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MISC)
    description = models.CharField(max_length=200)
    service_date = models.DateField(default=datetime.date.today)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Before tax; negative for discounts")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    reference = models.CharField(max_length=80, blank=True, help_text="Bill / order number this came from")
    source_order = models.ForeignKey('core.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='room_charges',
                                     help_text="Restaurant bill charged to the room from the POS")
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    voided_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    voided_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['service_date', 'id']

    def __str__(self):
        return f"{self.booking.booking_id} {self.get_kind_display()} {self.total}"

    @property
    def total(self):
        return (self.amount or Decimal('0')) + (self.tax_amount or Decimal('0'))

    def save(self, *args, **kwargs):
        sign = Decimal('-1') if self.kind == self.Kind.DISCOUNT else Decimal('1')
        self.amount = (sign * abs(Decimal(self.quantity) * Decimal(self.unit_price))).quantize(Decimal('0.01'))
        self.tax_amount = (self.amount * Decimal(self.tax_percent) / Decimal('100')).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)


class FolioPayment(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = 'PAYMENT', 'Payment'
        REFUND = 'REFUND', 'Refund'

    class Method(models.TextChoices):
        CASH = 'CASH', 'Cash'
        CARD = 'CARD', 'Card'
        UPI = 'UPI', 'UPI'
        BANK = 'BANK', 'Bank transfer'
        ONLINE = 'ONLINE', 'Online (gateway)'
        COMPANY = 'COMPANY', 'Bill to company'
        OTA = 'OTA', 'Collected by OTA'

    booking = models.ForeignKey(RoomBooking, on_delete=models.CASCADE, related_name='folio_payments')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CASH)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True, help_text="UTR, card slip, receipt number")
    note = models.CharField(max_length=200, blank=True)
    company_account = models.ForeignKey('hotel.CompanyAccount', on_delete=models.PROTECT, null=True, blank=True,
                                        related_name='charges', help_text="Set when the amount was billed to a company (city ledger)")
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['received_at', 'id']

    def __str__(self):
        return f"{self.booking.booking_id} {self.kind} {self.amount}"

    @property
    def signed_amount(self):
        return -self.amount if self.kind == self.Kind.REFUND else self.amount


# ---------------------------------------------------------------------------
# Housekeeping & maintenance
# ---------------------------------------------------------------------------

class HousekeepingTask(models.Model):
    class Kind(models.TextChoices):
        CHECKOUT_CLEAN = 'CHECKOUT_CLEAN', 'Departure clean'
        STAYOVER = 'STAYOVER', 'Stay-over service'
        DEEP_CLEAN = 'DEEP_CLEAN', 'Deep clean'
        TURNDOWN = 'TURNDOWN', 'Turndown'
        INSPECTION = 'INSPECTION', 'Inspection'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'To do'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        DONE = 'DONE', 'Done'
        SKIPPED = 'SKIPPED', 'Skipped'

    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='hk_tasks')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='hk_tasks')
    booking = models.ForeignKey(RoomBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='hk_tasks')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CHECKOUT_CLEAN)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    is_rush = models.BooleanField(default=False, help_text="A guest is arriving in this room today")
    task_date = models.DateField(default=datetime.date.today, db_index=True)
    assigned_to = models.ForeignKey('restaurant_admin.StaffMember', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='hk_tasks')
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-is_rush', 'task_date', 'room__room_number']

    def __str__(self):
        return f"Room {self.room.room_number} {self.get_kind_display()} ({self.get_status_display()})"

    @property
    def minutes_taken(self):
        if self.started_at and self.completed_at:
            return max(0, int((self.completed_at - self.started_at).total_seconds() // 60))
        return None


class MaintenanceIssue(models.Model):
    class Priority(models.TextChoices):
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'
        URGENT = 'URGENT', 'Urgent'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        RESOLVED = 'RESOLVED', 'Resolved'

    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='maintenance_issues')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='maintenance_issues')
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN, db_index=True)
    out_of_order = models.BooleanField(default=False, help_text="Take the room off sale while this is open")
    block_from = models.DateField(null=True, blank=True)
    block_until = models.DateField(null=True, blank=True, help_text="Last night the room is off sale (inclusive)")
    assigned_to = models.CharField(max_length=120, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    resolution_note = models.CharField(max_length=255, blank=True)
    reported_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['status', '-created_at']

    def __str__(self):
        return f"Room {self.room.room_number}: {self.title}"

    def blocks(self, start, end):
        """True when this issue keeps the room off sale for any night in [start, end)."""
        if not self.out_of_order or self.status == self.Status.RESOLVED:
            return False
        first = self.block_from or datetime.date.min
        last = self.block_until or datetime.date.max
        return first < end and last >= start


# ---------------------------------------------------------------------------
# Night audit
# ---------------------------------------------------------------------------

class NightAudit(models.Model):
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='night_audits')
    business_date = models.DateField()
    summary = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    closed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-business_date']
        unique_together = ('branch', 'business_date')

    def __str__(self):
        return f"Night audit {self.branch} {self.business_date}"


# ---------------------------------------------------------------------------
# City ledger (company accounts) and group bookings
# ---------------------------------------------------------------------------

class CompanyAccount(models.Model):
    """A company that settles its guests' bills later (corporate / travel agent / city ledger)."""
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='company_accounts')
    name = models.CharField(max_length=150)
    gstin = models.CharField(max_length=20, blank=True)
    contact_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Leave blank for no limit")
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('restaurant', 'name')

    def __str__(self):
        return self.name


class LedgerPayment(models.Model):
    """Money received from a company against its city-ledger balance."""
    account = models.ForeignKey(CompanyAccount, on_delete=models.CASCADE, related_name='settlements')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=FolioPayment.Method.choices, default=FolioPayment.Method.BANK)
    reference = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=200, blank=True)
    received_on = models.DateField(default=datetime.date.today)
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_on', '-id']


class BookingGroup(models.Model):
    """Several rooms booked together (wedding, tour, conference) with one contact."""
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='booking_groups')
    code = models.CharField(max_length=20, unique=True, editable=False)
    name = models.CharField(max_length=150)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    contact_email = models.EmailField(blank=True)
    company_account = models.ForeignKey(CompanyAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='groups')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.code} {self.name}"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = f"GRP-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)


# ====================================================================== guest services

BRANCH = 'super_admin.RestaurantBranch'


class GuestRequest(models.Model):
    """A guest request, complaint or compliment, routed to a department with a response target."""
    class Kind(models.TextChoices):
        REQUEST = 'REQUEST', 'Request'
        COMPLAINT = 'COMPLAINT', 'Complaint'
        COMPLIMENT = 'COMPLIMENT', 'Compliment'

    class Department(models.TextChoices):
        HOUSEKEEPING = 'HOUSEKEEPING', 'Housekeeping'
        FNB = 'FNB', 'Food & beverage'
        ENGINEERING = 'ENGINEERING', 'Engineering'
        FRONT_OFFICE = 'FRONT_OFFICE', 'Front office'
        IT = 'IT', 'IT / Wi-Fi'
        CONCIERGE = 'CONCIERGE', 'Concierge'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        ACKNOWLEDGED = 'ACKNOWLEDGED', 'Acknowledged'
        RESOLVED = 'RESOLVED', 'Resolved'

    SLA_MINUTES = {'HOUSEKEEPING': 30, 'FNB': 45, 'ENGINEERING': 20, 'FRONT_OFFICE': 30, 'IT': 20, 'CONCIERGE': 30}

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='guest_requests')
    booking = models.ForeignKey(RoomBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='guest_requests')
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name='guest_requests')
    guest_name = models.CharField(max_length=150, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.REQUEST)
    department = models.CharField(max_length=14, choices=Department.choices, default=Department.HOUSEKEEPING)
    priority = models.CharField(max_length=8, choices=[('NORMAL', 'Normal'), ('HIGH', 'High')], default='NORMAL')
    description = models.TextField()
    sla_minutes = models.PositiveSmallIntegerField(default=30, help_text="0 = no response target")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True)
    allowance = models.ForeignKey(FolioCharge, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                  help_text="Service-recovery credit posted to the folio")
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['branch', 'status'])]

    def __str__(self):
        return f"#{self.pk} {self.get_kind_display()} · {self.description[:40]}"

    @property
    def age_minutes(self):
        end = self.resolved_at or timezone.now()
        return max(0, int((end - self.created_at).total_seconds() // 60))

    @property
    def is_breached(self):
        if not self.sla_minutes or self.kind == self.Kind.COMPLIMENT:
            return False
        responded = self.acknowledged_at or self.resolved_at
        if responded:
            return (responded - self.created_at).total_seconds() > self.sla_minutes * 60
        return self.age_minutes > self.sla_minutes


class LostFoundItem(models.Model):
    class Status(models.TextChoices):
        STORED = 'STORED', 'In storage'
        RETURNED = 'RETURNED', 'Returned'
        DISPOSED = 'DISPOSED', 'Donated / disposed'

    HOLD_DAYS = 90

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='lost_found')
    reference = models.CharField(max_length=20, editable=False)
    found_on = models.DateField(default=datetime.date.today)
    location = models.CharField(max_length=120, help_text="Room number or area")
    booking = models.ForeignKey(RoomBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='lost_items',
                                help_text="Guest who probably left it")
    item = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True)
    is_valuable = models.BooleanField(default=False, help_text="Cash, jewellery, documents or electronics")
    found_by = models.CharField(max_length=120, blank=True)
    storage_location = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.STORED)
    returned_to = models.CharField(max_length=150, blank=True)
    returned_on = models.DateField(null=True, blank=True)
    closing_note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-found_on', '-id']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"LF-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def dispose_after(self):
        return self.found_on + datetime.timedelta(days=self.HOLD_DAYS)


class LaundryRate(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='laundry_rates')
    item = models.CharField(max_length=80)
    wash_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    dry_clean_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    press_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'item']
        unique_together = ('branch', 'item')

    def price(self, service):
        return {'WASH': self.wash_price, 'DRY_CLEAN': self.dry_clean_price, 'PRESS': self.press_price}.get(service)

    @property
    def cells(self):
        return [('WASH', self.wash_price), ('DRY_CLEAN', self.dry_clean_price), ('PRESS', self.press_price)]


class LaundryOrder(models.Model):
    class Status(models.TextChoices):
        COLLECTED = 'COLLECTED', 'Collected'
        PROCESSING = 'PROCESSING', 'Processing'
        READY = 'READY', 'Ready'
        DELIVERED = 'DELIVERED', 'Delivered'
        CANCELLED = 'CANCELLED', 'Cancelled'

    FLOW = ['COLLECTED', 'PROCESSING', 'READY', 'DELIVERED']
    EXPRESS_FACTOR = Decimal('1.5')
    GST_PERCENT = Decimal('18')

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='laundry_orders')
    reference = models.CharField(max_length=20, editable=False)
    booking = models.ForeignKey(RoomBooking, on_delete=models.CASCADE, related_name='laundry_orders')
    is_express = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COLLECTED)
    notes = models.CharField(max_length=255, blank=True)
    charge = models.ForeignKey(FolioCharge, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"LD-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def amount(self):
        base = sum((l.amount for l in self.lines.all()), Decimal('0'))
        return (base * (self.EXPRESS_FACTOR if self.is_express else 1)).quantize(Decimal('0.01'))

    @property
    def next_status(self):
        if self.status not in self.FLOW:
            return None
        i = self.FLOW.index(self.status)
        return self.FLOW[i + 1] if i + 1 < len(self.FLOW) else None


class LaundryLine(models.Model):
    SERVICES = [('WASH', 'Laundry'), ('DRY_CLEAN', 'Dry clean'), ('PRESS', 'Press only')]

    order = models.ForeignKey(LaundryOrder, on_delete=models.CASCADE, related_name='lines')
    item = models.CharField(max_length=80)
    service = models.CharField(max_length=10, choices=SERVICES)
    quantity = models.PositiveSmallIntegerField(default=1)
    rate = models.DecimalField(max_digits=8, decimal_places=2)

    @property
    def amount(self):
        return self.rate * self.quantity


# ====================================================================== spa & wellness

class SpaTherapist(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='spa_therapists')
    name = models.CharField(max_length=150)
    title = models.CharField(max_length=150, blank=True, default='Certified Wellness Specialist')
    experience_years = models.PositiveIntegerField(default=5)
    photo = models.URLField(max_length=500, blank=True, default='')
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('4.95'))
    sessions_completed = models.PositiveIntegerField(default=100)
    skills = models.CharField(max_length=255, blank=True, default='')
    languages = models.CharField(max_length=255, blank=True, default='English, Hindi')
    categories = models.JSONField(default=list, blank=True)
    certifications = models.JSONField(default=list, blank=True)
    specialties = models.JSONField(default=list, blank=True)
    bio = models.TextField(blank=True, default='')
    availability = models.CharField(max_length=50, default='Available Today')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.title})"

    def can_perform_category(self, cat):
        if not self.categories:
            return True
        return str(cat).lower().strip() in [str(c).lower().strip() for c in self.categories]

    @property
    def languages_list(self):
        if not self.languages:
            return ["English", "Hindi"]
        return [s.strip() for s in self.languages.split(',') if s.strip()]


class SpaTreatment(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='spa_treatments')
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=150, blank=True, default='')
    category = models.CharField(max_length=60, blank=True, default='massages')
    tag = models.CharField(max_length=100, blank=True, default='')
    duration_minutes = models.PositiveSmallIntegerField(default=60)
    duration_options = models.JSONField(default=list, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18.00'))
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('4.95'))
    reviews_count = models.PositiveIntegerField(default=100)
    suite_type = models.CharField(max_length=100, blank=True, default='VIP Private Suite')
    short_description = models.TextField(blank=True, default='')
    full_description = models.TextField(blank=True, default='')
    ritual_steps = models.JSONField(default=list, blank=True)
    benefits = models.JSONField(default=list, blank=True)
    suitable_for = models.JSONField(default=list, blank=True)
    contraindications = models.JSONField(default=list, blank=True)
    included_amenities = models.JSONField(default=list, blank=True)
    cover_image = models.URLField(max_length=500, blank=True, default='')
    gallery_images = models.JSONField(default=list, blank=True)
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} [{self.category}] - ₹{self.price}"


class SpaPackage(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='spa_packages')
    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=180, blank=True, default='')
    tag = models.CharField(max_length=100, blank=True, default='Signature Ritual')
    duration = models.CharField(max_length=60, default='150 Mins')
    duration_minutes = models.PositiveSmallIntegerField(default=150)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    image = models.URLField(max_length=500, blank=True, default='')
    description = models.TextField(blank=True, default='')
    inclusions = models.JSONField(default=list, blank=True)
    popular = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return f"{self.title} - ₹{self.price}"


class SpaAppointment(models.Model):
    class Status(models.TextChoices):
        BOOKED = 'BOOKED', 'Booked'
        IN_TREATMENT = 'IN_TREATMENT', 'In treatment'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        NO_SHOW = 'NO_SHOW', 'No-show'

    class Settlement(models.TextChoices):
        ROOM = 'ROOM', 'Charged to room'
        CASH = 'CASH', 'Cash'
        CARD = 'CARD', 'Card'
        UPI = 'UPI', 'UPI'
        COMPLIMENTARY = 'COMPLIMENTARY', 'Complimentary'

    OPEN_TIME, CLOSE_TIME = datetime.time(9, 0), datetime.time(21, 0)
    LIVE = ('BOOKED', 'IN_TREATMENT')

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='spa_appointments')
    treatment = models.ForeignKey(SpaTreatment, on_delete=models.PROTECT, related_name='appointments')
    therapist = models.ForeignKey(SpaTherapist, on_delete=models.PROTECT, related_name='appointments')
    date = models.DateField()
    start_time = models.TimeField()
    duration_minutes = models.PositiveSmallIntegerField()
    booking = models.ForeignKey(RoomBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='spa_appointments')
    guest_name = models.CharField(max_length=150)
    guest_phone = models.CharField(max_length=30, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18'))
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.BOOKED)
    settled_by = models.CharField(max_length=14, choices=Settlement.choices, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    charge = models.ForeignKey(FolioCharge, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'start_time']
        indexes = [models.Index(fields=['branch', 'date'])]

    @property
    def start_minutes(self):
        return self.start_time.hour * 60 + self.start_time.minute

    @property
    def end_time(self):
        m = self.start_minutes + self.duration_minutes
        return datetime.time(min(23, m // 60), m % 60)

    @property
    def total(self):
        return (self.price * (1 + self.gst_percent / 100)).quantize(Decimal('0.01'))

    @property
    def reference_id(self):
        return f"SPA-{self.date.strftime('%Y%m%d')}-{self.id:04d}"


# ====================================================================== banquets & events

class BanquetVenue(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='banquet_venues')
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=80, blank=True, help_text="e.g. Indoor ballroom, outdoor lawn")
    capacity = models.PositiveIntegerField(default=100)
    full_day_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class BanquetMenu(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='banquet_menus')
    name = models.CharField(max_length=120)
    price_per_cover = models.DecimalField(max_digits=8, decimal_places=2)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['price_per_cover']

    def __str__(self):
        return self.name


class BanquetEvent(models.Model):
    class Session(models.TextChoices):
        BREAKFAST = 'BREAKFAST', 'Breakfast'
        LUNCH = 'LUNCH', 'Lunch'
        HI_TEA = 'HI_TEA', 'Hi-tea'
        DINNER = 'DINNER', 'Dinner'
        FULL_DAY = 'FULL_DAY', 'Full day'

    class Status(models.TextChoices):
        ENQUIRY = 'ENQUIRY', 'Enquiry'
        TENTATIVE = 'TENTATIVE', 'Tentative'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    TYPES = ['Wedding', 'Conference', 'Meeting', 'Birthday / social', 'Exhibition', 'Other']
    LAYOUTS = ['Round tables', 'Theatre', 'Classroom', 'U-shape', 'Boardroom', 'Cocktail']
    HOLDS_VENUE = ('TENTATIVE', 'CONFIRMED')
    SESSION_SHARE = Decimal('0.6')

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='banquet_events')
    code = models.CharField(max_length=20, editable=False)
    name = models.CharField(max_length=150)
    event_type = models.CharField(max_length=40, default='Wedding')
    client_name = models.CharField(max_length=150)
    contact_phone = models.CharField(max_length=30, blank=True)
    contact_email = models.EmailField(blank=True)
    company_account = models.ForeignKey(CompanyAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    client_gstin = models.CharField(max_length=20, blank=True)
    venue = models.ForeignKey(BanquetVenue, on_delete=models.PROTECT, related_name='events')
    date = models.DateField()
    session = models.CharField(max_length=10, choices=Session.choices, default=Session.DINNER)
    pax = models.PositiveIntegerField(default=100, help_text="Guaranteed covers")
    menu = models.ForeignKey(BanquetMenu, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    layout = models.CharField(max_length=30, default='Round tables')
    venue_charge = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                       help_text="Leave blank to use the venue's rate")
    decor = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    av = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    other_charges = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18'))
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ENQUIRY)
    kitchen_notes = models.TextField(blank=True)
    housekeeping_notes = models.TextField(blank=True)
    engineering_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    invoice_number = models.CharField(max_length=30, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'venue__name']
        indexes = [models.Index(fields=['branch', 'date'])]

    def __str__(self):
        return f"{self.code} {self.name}"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = f"EV-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def hire(self):
        if self.venue_charge is not None:
            return self.venue_charge
        rate = self.venue.full_day_rate
        return rate if self.session == self.Session.FULL_DAY else (rate * self.SESSION_SHARE).quantize(Decimal('1'))

    @property
    def food(self):
        return (self.menu.price_per_cover * self.pax) if self.menu else Decimal('0')

    @property
    def subtotal(self):
        return self.hire + self.food + self.decor + self.av + self.other_charges

    @property
    def tax(self):
        return (self.subtotal * self.gst_percent / 100).quantize(Decimal('0.01'))

    @property
    def total(self):
        return self.subtotal + self.tax

    @property
    def cgst(self):
        return (self.tax / 2).quantize(Decimal('0.01'))

    @property
    def sgst(self):
        return self.tax - self.cgst

    @property
    def half_rate(self):
        return self.gst_percent / 2

    @property
    def paid(self):
        return sum((p.signed for p in self.payments.filter(is_void=False)), Decimal('0'))

    @property
    def balance(self):
        return self.total - self.paid


class BanquetPayment(models.Model):
    event = models.ForeignKey(BanquetEvent, on_delete=models.CASCADE, related_name='payments')
    kind = models.CharField(max_length=8, choices=[('PAYMENT', 'Advance / payment'), ('REFUND', 'Refund')], default='PAYMENT')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=FolioPayment.Method.choices, default=FolioPayment.Method.BANK)
    reference = models.CharField(max_length=100, blank=True)
    received_on = models.DateField(default=datetime.date.today)
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_on', '-id']

    @property
    def signed(self):
        return -self.amount if self.kind == 'REFUND' else self.amount


# ====================================================================== sales, packages & promotions

class SalesLead(models.Model):
    class Stage(models.TextChoices):
        NEW = 'NEW', 'New'
        QUALIFIED = 'QUALIFIED', 'Qualified'
        PROPOSAL = 'PROPOSAL', 'Proposal sent'
        NEGOTIATION = 'NEGOTIATION', 'Negotiation'
        WON = 'WON', 'Won'
        LOST = 'LOST', 'Lost'

    class Type(models.TextChoices):
        CORPORATE = 'CORPORATE', 'Corporate rate'
        WEDDING = 'WEDDING', 'Wedding'
        MICE = 'MICE', 'Conference / MICE'
        TRAVEL_AGENT = 'TRAVEL_AGENT', 'Travel agent'
        GROUP = 'GROUP', 'Leisure group'

    PIPELINE = ['NEW', 'QUALIFIED', 'PROPOSAL', 'NEGOTIATION']
    PROBABILITY = {'NEW': 10, 'QUALIFIED': 30, 'PROPOSAL': 50, 'NEGOTIATION': 70, 'WON': 100, 'LOST': 0}
    SOURCES = ['Inbound call', 'Website enquiry', 'Referral', 'Walk-in', 'Trade fair', 'Cold call', 'Social media', 'Repeat client']

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='sales_leads')
    company = models.CharField(max_length=150, help_text="Company, family or client name")
    contact_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    lead_type = models.CharField(max_length=14, choices=Type.choices, default=Type.CORPORATE)
    source = models.CharField(max_length=40, default='Inbound call')
    value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), help_text="Estimated deal value")
    stage = models.CharField(max_length=12, choices=Stage.choices, default=Stage.NEW)
    owner = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    requirement = models.TextField(blank=True)
    next_action = models.CharField(max_length=150, blank=True)
    next_action_on = models.DateField(null=True, blank=True)
    lost_reason = models.CharField(max_length=200, blank=True)
    closed_on = models.DateField(null=True, blank=True)
    company_account = models.ForeignKey(CompanyAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    event = models.ForeignKey(BanquetEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.company

    @property
    def probability(self):
        return self.PROBABILITY.get(self.stage, 0)

    @property
    def weighted(self):
        return (self.value * self.probability / 100).quantize(Decimal('1'))

    @property
    def is_open(self):
        return self.stage in self.PIPELINE


class SalesActivity(models.Model):
    KINDS = [('CALL', 'Call'), ('MEETING', 'Meeting'), ('EMAIL', 'Email'), ('SITE_VISIT', 'Site visit'), ('TASK', 'Task')]

    lead = models.ForeignKey(SalesLead, on_delete=models.CASCADE, related_name='activities')
    kind = models.CharField(max_length=12, choices=KINDS, default='CALL')
    subject = models.CharField(max_length=200)
    due_on = models.DateField(default=datetime.date.today)
    is_done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['is_done', 'due_on']


class RoomPackage(models.Model):
    """A bundled stay (e.g. honeymoon, business) posted to the folio as a per-night inclusion."""
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='room_packages')
    name = models.CharField(max_length=120)
    inclusions = models.TextField(help_text="What the guest gets")
    meal_plan = models.CharField(max_length=4, choices=[('EP', 'Room only'), ('CP', 'Breakfast'), ('MAP', 'Breakfast + one meal'),
                                                        ('AP', 'All meals')], default='CP')
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, help_text="Added to the room rate, per night, before GST")
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18'))
    min_nights = models.PositiveSmallIntegerField(default=1)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PromoCode(models.Model):
    """Percentage off the room charges, applied to a booking as a folio discount."""
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='promo_codes')
    code = models.CharField(max_length=30)
    description = models.CharField(max_length=150, blank=True)
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    uses = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('branch', 'code')

    def problem(self, on=None):
        """Why the code can't be used today, or None."""
        on = on or timezone.localdate()
        if not self.is_active:
            return 'is paused'
        if self.valid_from and on < self.valid_from:
            return f'starts on {self.valid_from:%d %b %Y}'
        if self.valid_until and on > self.valid_until:
            return 'has expired'
        if self.max_uses is not None and self.uses >= self.max_uses:
            return 'has reached its usage limit'
        return None


# ====================================================================== cashier

class CashierShift(models.Model):
    DENOMINATIONS = [500, 200, 100, 50, 20, 10, 5, 2, 1]

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='cashier_shifts')
    cashier = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, related_name='+')
    opened_at = models.DateTimeField(default=timezone.now)
    opening_float = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    closed_at = models.DateTimeField(null=True, blank=True)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    denominations = models.JSONField(default=dict, blank=True)
    handed_over_to = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-opened_at']

    @property
    def is_open(self):
        return self.closed_at is None

    @property
    def variance(self):
        if self.counted_cash is None or self.expected_cash is None:
            return None
        return self.counted_cash - self.expected_cash


class PaidOut(models.Model):
    shift = models.ForeignKey(CashierShift, on_delete=models.CASCADE, related_name='paid_outs')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    purpose = models.CharField(max_length=150)
    paid_to = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# ====================================================================== security, compliance, assets, energy

class Visitor(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='visitors')
    pass_number = models.CharField(max_length=20, editable=False)
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True)
    purpose = models.CharField(max_length=80)
    meeting = models.CharField(max_length=120, help_text="Room number, department or person")
    id_type = models.CharField(max_length=30, blank=True)
    id_last4 = models.CharField(max_length=4, blank=True, help_text="Last 4 characters of the ID only")
    vehicle = models.CharField(max_length=20, blank=True)
    time_in = models.DateTimeField(default=timezone.now)
    time_out = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-time_in']

    def save(self, *args, **kwargs):
        if not self.pass_number:
            self.pass_number = f"VP-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)


class SecurityIncident(models.Model):
    SEVERITY = [('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'), ('CRITICAL', 'Critical')]
    KINDS = ['Noise complaint', 'Theft / loss', 'Slip / fall', 'Medical emergency', 'Fire alarm', 'Fire',
             'Unauthorised entry', 'Property damage', 'Guest dispute', 'Other']

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='security_incidents')
    occurred_at = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=40)
    location = models.CharField(max_length=120)
    severity = models.CharField(max_length=8, choices=SEVERITY, default='LOW')
    description = models.TextField()
    action_taken = models.TextField(blank=True)
    reported_by = models.CharField(max_length=120, blank=True)
    is_closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-occurred_at']


class ComplianceLicence(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='licences')
    name = models.CharField(max_length=120)
    authority = models.CharField(max_length=150, blank=True)
    number = models.CharField(max_length=80, blank=True)
    expires_on = models.DateField()
    document = models.FileField(upload_to='hotel/licences/', null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['expires_on']


class StatutoryFiling(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='statutory_filings')
    name = models.CharField(max_length=120)
    due_on = models.DateField()
    filed_on = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['filed_on', 'due_on']


class SafetyCheck(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='safety_checks')
    name = models.CharField(max_length=120)
    every_days = models.PositiveSmallIntegerField(default=30)
    last_done = models.DateField(null=True, blank=True)
    done_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['name']

    @property
    def next_due(self):
        return (self.last_done + datetime.timedelta(days=self.every_days)) if self.last_done else None


class FixedAsset(models.Model):
    CATEGORIES = ['HVAC', 'Electrical', 'Kitchen', 'Laundry', 'IT / AV', 'Furniture', 'Vehicles', 'Security', 'Recreation', 'Other']

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='fixed_assets')
    tag = models.CharField(max_length=20, editable=False)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=30, default='Other')
    location = models.CharField(max_length=120, blank=True)
    purchased_on = models.DateField()
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    depreciation_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10'),
                                               help_text="Straight-line, per year")
    vendor = models.CharField(max_length=120, blank=True)
    warranty_until = models.DateField(null=True, blank=True)
    is_disposed = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['tag']

    def save(self, *args, **kwargs):
        if not self.tag:
            self.tag = f"FA-{uuid.uuid4().hex[:5].upper()}"
        super().save(*args, **kwargs)

    def book_value(self, on=None):
        on = on or timezone.localdate()
        years = Decimal(max(0, (on - self.purchased_on).days)) / Decimal('365.25')
        value = self.cost * (1 - self.depreciation_percent / 100 * years)
        return max(self.cost * Decimal('0.05'), value).quantize(Decimal('1'))  # 5% residual


class ServiceContract(models.Model):
    """AMC / service contract with a renewal date."""
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='service_contracts')
    vendor = models.CharField(max_length=150)
    scope = models.CharField(max_length=200)
    start_on = models.DateField()
    end_on = models.DateField()
    annual_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    contact = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ['end_on']


class UtilityReading(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='utility_readings')
    date = models.DateField()
    occupied_rooms = models.PositiveIntegerField(default=0)
    electricity_kwh = models.DecimalField(max_digits=10, decimal_places=1, default=Decimal('0'))
    water_kl = models.DecimalField(max_digits=8, decimal_places=1, default=Decimal('0'))
    lpg_kg = models.DecimalField(max_digits=8, decimal_places=1, default=Decimal('0'))
    diesel_l = models.DecimalField(max_digits=8, decimal_places=1, default=Decimal('0'))
    notes = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-date']
        unique_together = ('branch', 'date')

    def cost(self, s):
        return (self.electricity_kwh * s.tariff_electricity + self.water_kl * s.tariff_water + self.lpg_kg * s.tariff_lpg
                + self.diesel_l * s.tariff_diesel).quantize(Decimal('1'))

    @property
    def kwh_per_room(self):
        return (self.electricity_kwh / self.occupied_rooms).quantize(Decimal('0.1')) if self.occupied_rooms else None
