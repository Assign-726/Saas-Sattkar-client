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
    booking_status = models.CharField(
        max_length=20,
        choices=BookingStatus.choices,
        default=BookingStatus.RESERVED
    )
    special_requests = models.TextField(blank=True)
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
        if (not self.grand_total or self.grand_total == Decimal('0.00')) and self.room and self.check_in_date and self.check_out_date:
            nights = max(1, (self.check_out_date - self.check_in_date).days)
            base_rate = self.room.category.base_price_per_night if self.room.category else Decimal('0.00')
            room_charges = base_rate * Decimal(nights)
            tax_rate = Decimal('0.12') if base_rate <= 7500 else Decimal('0.18')
            tax_amount = round(room_charges * tax_rate, 2)
            self.total_room_charges = room_charges
            self.taxes = tax_amount
            self.grand_total = room_charges + tax_amount

        super().save(*args, **kwargs)

    @property
    def nights_count(self):
        if self.check_in_date and self.check_out_date:
            return max(1, (self.check_out_date - self.check_in_date).days)
        return 1


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
