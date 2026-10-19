"""Guest relationship layer shared by Restaurant, Hotel PMS and Club & Events.

A ``Guest`` is one person across every touchpoint of a business (POS orders,
room stays, table reservations, event tickets), matched by phone number and
falling back to email.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class Guest(models.Model):
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='guests')
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, blank=True, db_index=True, help_text='Digits only, last 10 for Indian numbers')
    email = models.EmailField(blank=True, db_index=True)
    birthday = models.DateField(null=True, blank=True)
    anniversary = models.DateField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    preferences = models.TextField(blank=True, help_text='Allergies, seating, room or dietary preferences')
    is_vip = models.BooleanField(default=False)
    marketing_opt_in = models.BooleanField(default=True)

    # Rolled-up stats (refreshed from orders / stays / reservations / tickets)
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)
    restaurant_visits = models.PositiveIntegerField(default=0)
    hotel_stays = models.PositiveIntegerField(default=0)
    reservations = models.PositiveIntegerField(default=0)
    event_bookings = models.PositiveIntegerField(default=0)
    lifetime_spend = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    loyalty_points = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen', '-id']
        indexes = [models.Index(fields=['restaurant', 'phone']), models.Index(fields=['restaurant', '-lifetime_spend'])]

    def __str__(self):
        return f'{self.name} ({self.phone or self.email})'

    @property
    def total_visits(self):
        return self.restaurant_visits + self.hotel_stays + self.reservations + self.event_bookings

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        return ''.join(p[0] for p in parts[:2]).upper() or '?'

    @property
    def segment(self):
        """Lifecycle segment used for filters and badges."""
        if self.is_vip:
            return 'vip'
        now = timezone.now()
        if self.last_seen and self.last_seen < now - timedelta(days=90):
            return 'lapsed'
        if self.total_visits >= 5:
            return 'regular'
        if self.first_seen and self.first_seen >= now - timedelta(days=30):
            return 'new'
        return 'occasional'


class GuestNote(models.Model):
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    author_name = models.CharField(max_length=150, blank=True)
    text = models.TextField()
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-pinned', '-created_at']


class LoyaltySettings(models.Model):
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='loyalty_settings')
    enabled = models.BooleanField(default=True)
    points_per_100 = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('5'),
                                         help_text='Points earned for every ₹100 spent')
    point_value = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
                                      help_text='Rupee value of one point when redeemed')
    min_redeem_points = models.PositiveIntegerField(default=100)
    earn_on_hotel_stays = models.BooleanField(default=True)

    @classmethod
    def for_restaurant(cls, restaurant):
        obj, _ = cls.objects.get_or_create(restaurant=restaurant)
        return obj

    def points_for(self, amount):
        return int((Decimal(amount or 0) / Decimal('100')) * self.points_per_100)


class LoyaltyTransaction(models.Model):
    class Kind(models.TextChoices):
        EARN = 'EARN', 'Earned'
        REDEEM = 'REDEEM', 'Redeemed'
        ADJUST = 'ADJUST', 'Adjusted'
        REVERSE = 'REVERSE', 'Reversed'

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='loyalty_transactions')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    points = models.IntegerField(help_text='Positive to add, negative to deduct')
    balance_after = models.IntegerField(default=0)
    reason = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=20, blank=True, help_text='order / stay / manual')
    source_ref = models.CharField(max_length=64, blank=True, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['guest', 'kind', 'source_type', 'source_ref'],
                                    condition=models.Q(kind='EARN') & ~models.Q(source_ref=''),
                                    name='uniq_loyalty_earn_per_source'),
        ]


def _gift_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    raw = ''.join(secrets.choice(alphabet) for _ in range(12))
    return f'{raw[:4]}-{raw[4:8]}-{raw[8:]}'


class GiftCard(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        REDEEMED = 'REDEEMED', 'Fully used'
        EXPIRED = 'EXPIRED', 'Expired'
        VOID = 'VOID', 'Void'

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='gift_cards')
    code = models.CharField(max_length=20, unique=True, default=_gift_code)
    initial_value = models.DecimalField(max_digits=10, decimal_places=2)
    balance = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    guest = models.ForeignKey(Guest, on_delete=models.SET_NULL, null=True, blank=True, related_name='gift_cards')
    recipient_name = models.CharField(max_length=150, blank=True)
    recipient_phone = models.CharField(max_length=20, blank=True)
    recipient_email = models.EmailField(blank=True)
    expires_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_gift_cards')
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.code} (₹{self.balance})'

    @property
    def is_expired(self):
        return bool(self.expires_on and self.expires_on < timezone.localdate())

    def refresh_status(self):
        if self.status == self.Status.VOID:
            return
        if self.is_expired and self.balance > 0:
            self.status = self.Status.EXPIRED
        elif self.balance <= 0:
            self.status = self.Status.REDEEMED
        else:
            self.status = self.Status.ACTIVE


class GiftCardTransaction(models.Model):
    class Kind(models.TextChoices):
        ISSUE = 'ISSUE', 'Issued'
        REDEEM = 'REDEEM', 'Redeemed'
        TOPUP = 'TOPUP', 'Topped up'
        VOID = 'VOID', 'Voided'

    card = models.ForeignKey(GiftCard, on_delete=models.CASCADE, related_name='transactions')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=60, blank=True, db_index=True, help_text='e.g. order:123 when used at the POS')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']


# --------------------------------------------------------------------------
# Guest feedback & complaints desk
# --------------------------------------------------------------------------

class Feedback(models.Model):
    class Status(models.TextChoices):
        NEW = 'NEW', 'New'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        RESOLVED = 'RESOLVED', 'Resolved'
        CLOSED = 'CLOSED', 'Closed'

    class Area(models.TextChoices):
        DINING = 'dining', 'Dining'
        DELIVERY = 'delivery', 'Delivery / takeaway'
        STAY = 'stay', 'Stay'
        EVENT = 'event', 'Event'
        OTHER = 'other', 'Other'

    class Source(models.TextChoices):
        QR = 'qr', 'QR / link'
        STAFF = 'staff', 'Logged by staff'
        API = 'api', 'Partner / API'

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='feedback')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.SET_NULL, null=True, blank=True, related_name='feedback')
    guest = models.ForeignKey(Guest, on_delete=models.SET_NULL, null=True, blank=True, related_name='feedback')
    order = models.ForeignKey('core.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='feedback')
    area = models.CharField(max_length=12, choices=Area.choices, default=Area.DINING)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.QR)
    rating = models.PositiveSmallIntegerField(help_text='Overall, 1–5')
    food = models.PositiveSmallIntegerField(null=True, blank=True)
    service = models.PositiveSmallIntegerField(null=True, blank=True)
    ambience = models.PositiveSmallIntegerField(null=True, blank=True)
    value = models.PositiveSmallIntegerField(null=True, blank=True)
    recommend = models.PositiveSmallIntegerField(null=True, blank=True, help_text='0–10: how likely to recommend us')
    comment = models.TextField(blank=True)
    name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    may_contact = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW, db_index=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    ASPECTS = [('food', 'Food'), ('service', 'Service'), ('ambience', 'Ambience'), ('value', 'Value for money')]

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['restaurant', 'status', 'created_at'])]

    def __str__(self):
        return f'{self.rating}★ from {self.name or "guest"}'

    @property
    def is_complaint(self):
        return self.rating <= 2

    @property
    def is_open(self):
        return self.status in (self.Status.NEW, self.Status.IN_PROGRESS)

    @property
    def sentiment(self):
        return 'negative' if self.rating <= 2 else ('neutral' if self.rating == 3 else 'positive')

    @property
    def aspects(self):
        return [(label, getattr(self, key)) for key, label in self.ASPECTS if getattr(self, key)]


class FeedbackUpdate(models.Model):
    """Timeline of a feedback ticket: internal notes, replies to the guest, status changes, make-goods."""
    feedback = models.ForeignKey(Feedback, on_delete=models.CASCADE, related_name='updates')
    kind = models.CharField(max_length=12, default='note')  # note | status | make_good | assigned
    text = models.TextField()
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


# --------------------------------------------------------------------------
# Campaigns (email, WhatsApp, SMS) to guest segments
# --------------------------------------------------------------------------

class Campaign(models.Model):
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        WHATSAPP = 'whatsapp', 'WhatsApp'
        SMS = 'sms', 'SMS'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SENDING = 'SENDING', 'Sending'
        SENT = 'SENT', 'Sent'

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='campaigns')
    name = models.CharField(max_length=150)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.WHATSAPP)
    segment = models.CharField(max_length=20, default='')
    subject = models.CharField(max_length=150, blank=True)
    message = models.TextField()
    offer_code = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    audience_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    automation = models.CharField(max_length=20, blank=True, help_text='Set for automatic birthday / anniversary / win-back sends')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class CampaignRecipient(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'To send'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='recipients')
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='campaign_messages')
    address = models.CharField(max_length=254)
    message = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['campaign', 'guest'], name='uniq_campaign_guest')]


class MarketingSettings(models.Model):
    """Automatic messages, sent by the daily ``run_guest_automations`` command (email)."""
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='marketing_settings')
    birthday_enabled = models.BooleanField(default=False)
    birthday_message = models.TextField(default='Happy birthday, {first_name}! 🎂 Celebrate with us this week and enjoy a treat on the house. Show this message when you visit {business}.')
    anniversary_enabled = models.BooleanField(default=False)
    anniversary_message = models.TextField(default='Happy anniversary, {first_name}! Make it special at {business} — a complimentary dessert is waiting for you this week.')
    winback_enabled = models.BooleanField(default=False)
    winback_days = models.PositiveIntegerField(default=60)
    winback_message = models.TextField(default='We miss you, {first_name}! It has been a while since your last visit to {business}. Here is a little something to welcome you back: {offer_code}')
    winback_offer_code = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_restaurant(cls, restaurant):
        obj, _ = cls.objects.get_or_create(restaurant=restaurant)
        return obj
