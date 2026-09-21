import hmac
import hashlib
import uuid
from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from io import BytesIO
from django.core.files import File


class ClubEvent(models.Model):
    """
    Ticketed club/venue event (live music, DJ night, club night, party).
    Scoped to a specific RestaurantBranch with has_club_module enabled.
    """
    branch = models.ForeignKey(
        'super_admin.RestaurantBranch',
        on_delete=models.CASCADE,
        related_name='club_events'
    )
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, blank=True)
    description = models.TextField(blank=True)
    cover_image = models.ImageField(upload_to='events/covers/', blank=True, null=True)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    venue_name = models.CharField(max_length=200, blank=True)
    venue_address = models.TextField(blank=True)
    age_restriction = models.CharField(max_length=50, blank=True, default="21+")
    dress_code = models.CharField(max_length=100, blank=True, default="Smart Casual")
    is_published = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)
    terms_and_conditions = models.TextField(
        blank=True,
        default="Entry strictly allowed with a valid QR pass. Venue rules and age restrictions apply. Rights of admission reserved."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_datetime']
        verbose_name = 'Club Event'
        verbose_name_plural = 'Club Events'

    def __str__(self):
        return f"{self.title} ({self.branch.name})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title) or f"event-{uuid.uuid4().hex[:6]}"
            candidate = base_slug
            counter = 1
            while ClubEvent.objects.filter(branch=self.branch, slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            self.slug = candidate
        if not self.venue_name and self.branch:
            self.venue_name = self.branch.name
        if not self.venue_address and self.branch:
            self.venue_address = getattr(self.branch, 'address', '') or f"{self.branch.city}, {self.branch.state}".strip(', ')
        super().save(*args, **kwargs)

    @property
    def is_upcoming(self):
        return self.end_datetime >= timezone.now()

    @property
    def total_tickets_allocated(self):
        return sum(tt.total_allocated for tt in self.ticket_types.all())

    @property
    def total_tickets_sold(self):
        return sum(tt.quantity_sold for tt in self.ticket_types.all())

    @property
    def total_revenue(self):
        from django.db.models import Sum
        val = self.bookings.filter(
            payment_status=EventBooking.PaymentStatus.SUCCESS
        ).aggregate(Sum('total_amount'))['total_amount__sum']
        return val or Decimal('0.00')

    @property
    def total_attendees_checked_in(self):
        return self.bookings.filter(
            payment_status=EventBooking.PaymentStatus.SUCCESS,
            checked_in=True
        ).count()


class TicketType(models.Model):
    """
    Tiered ticket categories for a ClubEvent (e.g. Early Bird, Female Stag, Couple, VIP Table).
    """
    event = models.ForeignKey(
        ClubEvent,
        on_delete=models.CASCADE,
        related_name='ticket_types'
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_allocated = models.PositiveIntegerField(help_text="Maximum tickets available for this tier")
    quantity_sold = models.PositiveIntegerField(default=0, help_text="Number of confirmed tickets sold")
    max_per_order = models.PositiveIntegerField(default=10, help_text="Maximum quantity allowed per checkout")
    sales_start_datetime = models.DateTimeField(null=True, blank=True)
    sales_end_datetime = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'base_price', 'id']
        verbose_name = 'Ticket Type'
        verbose_name_plural = 'Ticket Types'

    def __str__(self):
        return f"{self.event.title} - {self.name} (₹{self.base_price})"

    @property
    def active_holds_count(self):
        """Returns number of tickets temporarily locked in PENDING checkout within 15-min window."""
        now = timezone.now()
        holds = self.bookings.filter(
            payment_status=EventBooking.PaymentStatus.PENDING,
            expires_at__gt=now
        ).aggregate(models.Sum('quantity'))['quantity__sum']
        return holds or 0

    @property
    def available_quantity(self):
        """Current real-time tickets remaining for purchase."""
        remaining = self.total_allocated - self.quantity_sold - self.active_holds_count
        return max(0, remaining)

    @property
    def is_sold_out(self):
        return self.available_quantity <= 0

    @property
    def is_sales_open(self):
        if not self.is_active:
            return False
        now = timezone.now()
        if self.sales_start_datetime and now < self.sales_start_datetime:
            return False
        if self.sales_end_datetime and now > self.sales_end_datetime:
            return False
        if self.event.end_datetime and now > self.event.end_datetime:
            return False
        return not self.is_sold_out


class EventBooking(models.Model):
    """
    Ticket purchase / guestlist reservation for an event.
    Zero tax currently applied: total_amount = unit_price * quantity.
    """
    class PaymentStatus(models.TextChoices):
        PENDING = 'PENDING', _('Pending Payment')
        SUCCESS = 'SUCCESS', _('Confirmed / Paid')
        FAILED = 'FAILED', _('Payment Failed')
        EXPIRED = 'EXPIRED', _('Expired (Unpaid Hold)')
        CANCELLED = 'CANCELLED', _('Cancelled')

    class PaymentMethod(models.TextChoices):
        ONLINE = 'ONLINE', _('Online (Razorpay)')
        DOOR_CASH = 'DOOR_CASH', _('Cash at Door')
        COMPLIMENTARY = 'COMPLIMENTARY', _('Complimentary / Guestlist')

    booking_reference = models.CharField(max_length=50, unique=True, db_index=True)
    event = models.ForeignKey(
        ClubEvent,
        on_delete=models.CASCADE,
        related_name='bookings'
    )
    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.PROTECT,
        related_name='bookings'
    )
    guest_name = models.CharField(max_length=150)
    guest_email = models.EmailField()
    guest_phone = models.CharField(max_length=20)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.ONLINE
    )

    # Razorpay Transaction details
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=255, blank=True, null=True)

    # Scannable entry QR code & security token
    security_token = models.CharField(max_length=64, blank=True, null=True)
    booking_qr_code = models.ImageField(upload_to='events/qrs/', blank=True, null=True)

    # Gate entry / check-in tracking
    checked_in = models.BooleanField(default=False, db_index=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='scanned_bookings'
    )
    notes = models.TextField(blank=True)

    # Visual Floor Plan & Table Allocation
    assigned_table = models.ForeignKey(
        'core.Table',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='event_bookings'
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    # 15-minute temporary inventory reservation expiry
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Event Booking'
        verbose_name_plural = 'Event Bookings'

    def __str__(self):
        return f"{self.booking_reference} - {self.guest_name} ({self.event.title})"

    @classmethod
    def generate_booking_reference(cls):
        """Generates a human-friendly unique booking reference e.g. EVT-BK-XXXXXX."""
        code = uuid.uuid4().hex[:8].upper()
        ref = f"EVT-BK-{code}"
        while cls.objects.filter(booking_reference=ref).exists():
            code = uuid.uuid4().hex[:8].upper()
            ref = f"EVT-BK-{code}"
        return ref

    def generate_security_token(self):
        """Cryptographic HMAC token binding booking reference, event ID, and phone."""
        secret = getattr(settings, 'SECRET_KEY', 'club_event_secret_key_12345')
        raw = f"{self.booking_reference}:{self.event_id}:{self.guest_phone}"
        return hmac.new(secret.encode('utf-8'), raw.encode('utf-8'), hashlib.sha256).hexdigest()[:24]

    def generate_qr_code(self, force=False):
        """Generates scannable QR pass containing JSON verification payload."""
        if self.booking_qr_code and not force:
            return

        try:
            import qrcode
            import json

            if not self.security_token:
                self.security_token = self.generate_security_token()

            qr_payload = json.dumps({
                'ref': self.booking_reference,
                'event_id': self.event_id,
                'event': self.event.title,
                'tier': self.ticket_type.name,
                'qty': self.quantity,
                'guest': self.guest_name,
                'phone': self.guest_phone,
                'tok': self.security_token
            })

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            canvas = BytesIO()
            # pyrefly: ignore [unexpected-keyword]
            img.save(canvas, format='PNG')
            filename = f"pass_{self.booking_reference}_{uuid.uuid4().hex[:6]}.png"
            self.booking_qr_code.save(filename, File(canvas), save=False)
        except Exception as e:
            pass

    def mark_checked_in(self, staff_user=None):
        """Marks attendee pass as checked-in at the gate."""
        if self.checked_in:
            return False, "Pass already scanned and checked in."
        self.checked_in = True
        self.checked_in_at = timezone.now()
        if staff_user and staff_user.is_authenticated:
            self.checked_in_by = staff_user
        self.save(update_fields=['checked_in', 'checked_in_at', 'checked_in_by'])
        return True, "Check-in successful! Welcome to the event."
