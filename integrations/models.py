"""Integrations: everything that connects a branch to the outside world.

* ``Connection``      — a sales channel a branch is listed on (Zomato, Swiggy, a hotel channel manager…)
* ``ApiKey``          — a secret a partner uses to call the public API for exactly one branch
* ``WebhookEndpoint`` — a URL we notify when something happens in a branch
* ``WebhookDelivery`` — one attempt-tracked notification (the delivery log)
* ``ExternalOrderLink`` / ``ExternalBookingLink`` — make inbound orders/bookings idempotent

Every row belongs to a branch, so a group can run each outlet on its own channels and keys.
"""
import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

SCOPES = [
    ('menu:read', 'Read the menu'),
    ('menu:write', 'Change item availability and prices'),
    ('orders:read', 'Read orders'),
    ('orders:write', 'Create orders and update their status'),
    ('rooms:read', 'Read room availability'),
    ('bookings:read', 'Read room bookings'),
    ('bookings:write', 'Create and cancel room bookings'),
]
SCOPE_KEYS = [s for s, _ in SCOPES]

EVENTS = [
    ('order.created', 'Order created'),
    ('order.status_changed', 'Order status changed'),
    ('order.paid', 'Order paid'),
    ('order.cancelled', 'Order cancelled'),
    ('booking.created', 'Room booking created'),
    ('booking.checked_in', 'Guest checked in'),
    ('booking.checked_out', 'Guest checked out'),
    ('booking.cancelled', 'Room booking cancelled'),
    ('booking.modified', 'Room booking dates or room changed'),
    ('ari.changed', 'Room rates, restrictions or availability changed'),
    ('menu.item_updated', 'Menu item availability or price changed'),
    ('feedback.received', 'Guest feedback received'),
]
EVENT_KEYS = [e for e, _ in EVENTS]


class Connection(models.Model):
    class Provider(models.TextChoices):
        ZOMATO = 'zomato', 'Zomato'
        SWIGGY = 'swiggy', 'Swiggy'
        MAGICPIN = 'magicpin', 'magicpin'
        ONDC = 'ondc', 'ONDC'
        WEBSITE = 'website', 'Own website / app'
        CHANNEL_MANAGER = 'channel_manager', 'Hotel channel manager'
        OTA = 'ota', 'Online travel agency'
        CUSTOM = 'custom', 'Custom integration'

    KIND = {
        'zomato': 'food', 'swiggy': 'food', 'magicpin': 'food', 'ondc': 'food', 'website': 'food',
        'channel_manager': 'rooms', 'ota': 'rooms', 'custom': 'any',
    }
    DEFAULT_SCOPES = {
        'food': ['menu:read', 'menu:write', 'orders:read', 'orders:write'],
        'rooms': ['rooms:read', 'bookings:read', 'bookings:write'],
        'any': ['menu:read', 'orders:read'],
    }

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='integration_connections')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='integration_connections')
    provider = models.CharField(max_length=30, choices=Provider.choices)
    name = models.CharField(max_length=120, blank=True, help_text='Optional label, e.g. "Swiggy – Koramangala"')
    external_store_id = models.CharField(max_length=120, blank=True, help_text='The outlet / property ID on the partner side')
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text='Partner commission, used in the profit & loss report')
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_event_at = models.DateTimeField(null=True, blank=True)

    # --- Delivery-platform operations (Zomato / Swiggy) -----------------------------------
    class Mode(models.TextChoices):
        MANUAL = 'manual', 'Manual – staff also act on the partner tablet'
        API = 'api', 'Partner API – actions are sent to the platform'

    class Environment(models.TextChoices):
        SANDBOX = 'sandbox', 'Sandbox / testing'
        LIVE = 'live', 'Live'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, help_text='Used in this outlet\'s webhook URL')
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.MANUAL)
    environment = models.CharField(max_length=10, choices=Environment.choices, default=Environment.SANDBOX)
    api_base_url = models.URLField(max_length=300, blank=True, help_text='Base URL from the partner\'s integration documentation')
    api_paths = models.JSONField(default=dict, blank=True, help_text='Partner endpoint path for each action, from their documentation')
    auth_header = models.CharField(max_length=60, blank=True, default='Authorization')
    credentials_encrypted = models.TextField(blank=True, help_text='Encrypted JSON; never shown again after saving')
    token_url = models.URLField(max_length=300, blank=True, help_text="This branch's OAuth token endpoint, if it differs from the brand's")
    token_encrypted = models.TextField(blank=True, help_text="Cached OAuth access token for this branch's own client ID")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    webhook_secret = models.CharField(max_length=400, blank=True, help_text='Encrypted. Generated by Sattkar unless the platform issues one')
    signature_header = models.CharField(max_length=60, blank=True, default='X-Signature',
                                        help_text='Header that carries the HMAC-SHA256 of the request body')
    capabilities = models.JSONField(default=dict, blank=True)
    auto_accept = models.BooleanField(default=False)
    default_prep_minutes = models.PositiveSmallIntegerField(default=15)
    accept_window_seconds = models.PositiveSmallIntegerField(default=180, help_text='How long staff have to accept a new order')
    is_store_open = models.BooleanField(default=True)
    store_status_changed_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['branch__name', 'provider']
        constraints = [models.UniqueConstraint(fields=['branch', 'provider', 'external_store_id'], name='uniq_connection_per_branch_store')]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return self.name or f'{self.get_provider_display()} · {self.branch.name}'

    @property
    def kind(self):
        return self.KIND.get(self.provider, 'any')

    @property
    def default_scopes(self):
        return self.DEFAULT_SCOPES[self.kind]

    @property
    def is_delivery_platform(self):
        return self.provider in ('zomato', 'swiggy')

    def save(self, *args, **kwargs):
        if not self.webhook_secret:
            self.set_webhook_secret('dlsec_' + secrets.token_urlsafe(24))
        super().save(*args, **kwargs)

    def get_webhook_secret(self):
        from integrations.delivery.crypto import decrypt_text
        return decrypt_text(self.webhook_secret)

    def set_webhook_secret(self, raw):
        from integrations.delivery.crypto import encrypt_text
        self.webhook_secret = encrypt_text(raw)

    @property
    def account(self):
        """The brand-wide partner account for this outlet's business, if one is set up."""
        if not hasattr(self, '_account_cache'):
            self._account_cache = PlatformAccount.objects.filter(restaurant_id=self.restaurant_id, provider=self.provider).first()
        return self._account_cache

    def get_credentials(self):
        from integrations.delivery.crypto import decrypt_json
        return decrypt_json(self.credentials_encrypted)

    def set_credentials(self, data):
        from integrations.delivery.crypto import encrypt_json
        clean = {k: v for k, v in (data or {}).items() if v}
        if clean != self.get_credentials():
            self.token_encrypted, self.token_expires_at = '', None  # new keys → fetch a new token
        self.credentials_encrypted = encrypt_json(clean) if clean else ''

    def get_cached_token(self):
        from integrations.delivery.crypto import decrypt_text
        if self.token_encrypted and self.token_expires_at and self.token_expires_at > timezone.now():
            return decrypt_text(self.token_encrypted)
        return ''

    def cache_token(self, token, expires_at):
        from integrations.delivery.crypto import encrypt_text
        self.token_encrypted, self.token_expires_at = encrypt_text(token), expires_at
        Connection.objects.filter(pk=self.pk).update(token_encrypted=self.token_encrypted, token_expires_at=expires_at)

    @property
    def health(self):
        if not self.is_active:
            return 'paused'
        if self.last_error_at and (not self.last_success_at or self.last_error_at > self.last_success_at):
            return 'error'
        return 'ok' if self.last_event_at else 'idle'

    def can(self, capability):
        from integrations.delivery.platforms import default_capabilities
        caps = {**default_capabilities(self.provider), **(self.capabilities or {})}
        return bool(caps.get(capability))


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKeyQuerySet(models.QuerySet):
    def active(self):
        now = timezone.now()
        return self.filter(revoked_at__isnull=True).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))


class ApiKey(models.Model):
    PREFIX = 'hk_live_'

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='api_keys')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='api_keys')
    connection = models.ForeignKey(Connection, on_delete=models.SET_NULL, null=True, blank=True, related_name='api_keys')
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=20, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)

    objects = ApiKeyQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.prefix}…)'

    @classmethod
    def issue(cls, *, restaurant, branch, name, scopes, connection=None, user=None, expires_at=None):
        """Create a key and return ``(key, raw_secret)``. The secret is never stored and cannot be shown again."""
        raw = cls.PREFIX + secrets.token_urlsafe(32)
        key = cls.objects.create(
            restaurant=restaurant, branch=branch, connection=connection, name=name,
            prefix=raw[:len(cls.PREFIX) + 6], key_hash=_hash(raw),
            scopes=[s for s in scopes if s in SCOPE_KEYS], created_by=user, expires_at=expires_at)
        return key, raw

    @classmethod
    def authenticate(cls, raw):
        if not raw or not raw.startswith(cls.PREFIX):
            return None
        return cls.objects.active().select_related('branch', 'restaurant', 'connection').filter(key_hash=_hash(raw)).first()

    @property
    def status(self):
        if self.revoked_at:
            return 'revoked'
        if self.expires_at and self.expires_at <= timezone.now():
            return 'expired'
        return 'active'

    def has_scope(self, scope):
        return scope in (self.scopes or [])


class WebhookEndpoint(models.Model):
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='webhook_endpoints')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='webhook_endpoints', help_text='Empty = every branch of the business')
    connection = models.ForeignKey(Connection, on_delete=models.SET_NULL, null=True, blank=True, related_name='webhooks')
    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=80)
    events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    disabled_reason = models.CharField(max_length=200, blank=True)
    failure_streak = models.PositiveIntegerField(default=0)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    AUTO_DISABLE_AFTER = 15  # consecutive failed deliveries

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} → {self.url}'

    @staticmethod
    def new_secret():
        return 'whsec_' + secrets.token_urlsafe(24)

    def save(self, *args, **kwargs):
        if not self.secret:
            self.secret = self.new_secret()
        super().save(*args, **kwargs)


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SUCCESS = 'SUCCESS', 'Delivered'
        FAILED = 'FAILED', 'Failed – will retry'
        GAVE_UP = 'GAVE_UP', 'Failed'

    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='deliveries')
    event = models.CharField(max_length=50, db_index=True)
    event_id = models.CharField(max_length=40, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_code = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.CharField(max_length=300, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    MAX_ATTEMPTS = 6
    BACKOFF_MINUTES = [1, 5, 30, 120, 360]

    class Meta:
        ordering = ['-created_at']


class ExternalOrderLink(models.Model):
    """Ties a partner's order ID to our order so a retried push never creates a duplicate."""
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='+')
    connection = models.ForeignKey(Connection, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    source = models.CharField(max_length=30)
    external_id = models.CharField(max_length=120)
    order = models.OneToOneField('core.Order', on_delete=models.CASCADE, related_name='external_link')
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Platform order lifecycle (Zomato / Swiggy) ----------------------------------------
    class PlatformStatus(models.TextChoices):
        PLACED = 'placed', 'New – awaiting acceptance'
        ACCEPTED = 'accepted', 'Accepted'
        READY = 'ready', 'Ready for pickup'
        PICKED_UP = 'picked_up', 'Picked up'
        DELIVERED = 'delivered', 'Delivered'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    platform_status = models.CharField(max_length=12, choices=PlatformStatus.choices, blank=True, db_index=True)
    placed_at = models.DateTimeField(null=True, blank=True)
    accept_by = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    prep_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    cancelled_by = models.CharField(max_length=20, blank=True, help_text='customer, platform or restaurant')
    item_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    merchant_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text='Share of the discount the restaurant pays')
    packaging_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    customer_payable = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    expected_payout = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    settled_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    settled_at = models.DateField(null=True, blank=True)
    instructions = models.CharField(max_length=500, blank=True)
    rider_name = models.CharField(max_length=120, blank=True)
    rider_phone = models.CharField(max_length=20, blank=True)
    rider_eta = models.DateTimeField(null=True, blank=True)
    rider_status = models.CharField(max_length=30, blank=True)
    pickup_code = models.CharField(max_length=20, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['branch', 'source', 'external_id'], name='uniq_external_order')]
        indexes = [models.Index(fields=['source', 'platform_status', 'created_at'])]

    @property
    def is_open(self):
        return self.platform_status in ('placed', 'accepted', 'ready', 'picked_up')

    @property
    def ready_by(self):
        if self.accepted_at and self.prep_minutes:
            from datetime import timedelta
            return self.accepted_at + timedelta(minutes=self.prep_minutes)
        return None

    @property
    def is_late(self):
        rb = self.ready_by
        return bool(rb and self.platform_status == 'accepted' and timezone.now() > rb)

    @property
    def seconds_to_accept(self):
        if self.platform_status != 'placed' or not self.accept_by:
            return None
        return max(0, int((self.accept_by - timezone.now()).total_seconds()))


class ExternalBookingLink(models.Model):
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='+')
    connection = models.ForeignKey(Connection, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    source = models.CharField(max_length=30)
    external_id = models.CharField(max_length=120)
    booking = models.OneToOneField('hotel.RoomBooking', on_delete=models.CASCADE, related_name='external_link')
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['branch', 'source', 'external_id'], name='uniq_external_booking')]


# ==========================================================================================
# Delivery platforms (Zomato / Swiggy): event ledger, actions, menu, settlements
# ==========================================================================================

class PlatformAccount(models.Model):
    """Brand-wide Zomato / Swiggy partner credentials, shared by every outlet of one business.

    All secrets (client ID & secret, access token, webhook secret, cached OAuth token) are
    stored encrypted in ``secrets_encrypted`` and are never sent back to the browser.
    Resolution order for any value: outlet override → this account → Sattkar default (.env).
    """
    SECRET_FIELDS = ['client_id', 'client_secret', 'access_token', 'webhook_secret']

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='platform_accounts')
    provider = models.CharField(max_length=20)
    environment = models.CharField(max_length=10, choices=Connection.Environment.choices, default=Connection.Environment.SANDBOX)
    api_base_url = models.URLField(max_length=300, blank=True)
    token_url = models.URLField(max_length=300, blank=True, help_text='OAuth token endpoint, when the platform issues tokens from client ID + secret')
    api_paths = models.JSONField(default=dict, blank=True)
    auth_header = models.CharField(max_length=60, blank=True)
    signature_header = models.CharField(max_length=60, blank=True)
    secrets_encrypted = models.TextField(blank=True)
    token_encrypted = models.TextField(blank=True, help_text='Cached OAuth access token')
    token_expires_at = models.DateTimeField(null=True, blank=True)
    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True)
    last_test_message = models.CharField(max_length=300, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['restaurant', 'provider'], name='uniq_platform_account')]

    def __str__(self):
        return f'{self.provider} · {self.restaurant}'

    def get_secrets(self):
        from integrations.delivery.crypto import decrypt_json
        return decrypt_json(self.secrets_encrypted)

    def set_secrets(self, data):
        from integrations.delivery.crypto import encrypt_json
        clean = {k: v for k, v in (data or {}).items() if v}
        self.secrets_encrypted = encrypt_json(clean) if clean else ''
        self.token_encrypted, self.token_expires_at = '', None  # new credentials → fetch a new token

    def get_cached_token(self):
        from integrations.delivery.crypto import decrypt_text
        if self.token_encrypted and self.token_expires_at and self.token_expires_at > timezone.now():
            return decrypt_text(self.token_encrypted)
        return ''

    def cache_token(self, token, expires_at):
        from integrations.delivery.crypto import encrypt_text
        self.token_encrypted, self.token_expires_at = encrypt_text(token), expires_at
        PlatformAccount.objects.filter(pk=self.pk).update(token_encrypted=self.token_encrypted, token_expires_at=expires_at)


class PlatformEvent(models.Model):
    """Every inbound request from a platform, stored before we act on it (durable + idempotent)."""
    class State(models.TextChoices):
        RECEIVED = 'received', 'Received'
        PROCESSED = 'processed', 'Processed'
        RETRY = 'retry', 'Will retry'
        NEEDS_MAPPING = 'needs_mapping', 'Needs menu mapping'
        FAILED = 'failed', 'Failed'
        IGNORED = 'ignored', 'Ignored'

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='platform_events')
    provider = models.CharField(max_length=20)
    dedupe_key = models.CharField(max_length=200)
    event_type = models.CharField(max_length=40, blank=True)
    external_order_id = models.CharField(max_length=120, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    payload_sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=15, choices=State.choices, default=State.RECEIVED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    unmapped = models.JSONField(default=list, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    MAX_ATTEMPTS = 8

    class Meta:
        ordering = ['-received_at']
        constraints = [models.UniqueConstraint(fields=['connection', 'dedupe_key'], name='uniq_platform_event')]


class PlatformOrderHistory(models.Model):
    link = models.ForeignKey(ExternalOrderLink, on_delete=models.CASCADE, related_name='history')
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    source = models.CharField(max_length=12, help_text='platform, staff, kitchen or system')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    LABELS = {'placed': 'Received', 'accepted': 'Accepted', 'ready': 'Food ready', 'picked_up': 'Picked up',
              'delivered': 'Delivered', 'rejected': 'Rejected', 'cancelled': 'Cancelled'}

    class Meta:
        ordering = ['at', 'id']

    @property
    def label(self):
        return self.LABELS.get(self.to_status, self.to_status)


class PlatformAction(models.Model):
    """Something we asked the platform to do, with its result. Never reported as done until confirmed."""
    class State(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SUCCEEDED = 'succeeded', 'Confirmed by platform'
        MANUAL = 'manual', 'Do it on the partner tablet'
        UNSUPPORTED = 'unsupported', 'Not available for this account'
        RETRY = 'retry', 'Failed – will retry'
        FAILED = 'failed', 'Failed'

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='platform_actions')
    link = models.ForeignKey(ExternalOrderLink, on_delete=models.CASCADE, null=True, blank=True, related_name='actions')
    action = models.CharField(max_length=30)
    idempotency_key = models.CharField(max_length=120, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_code = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.CharField(max_length=300, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    MAX_ATTEMPTS = 5
    BACKOFF_SECONDS = [15, 60, 300, 900]
    LABELS = {'accept': 'Accept order', 'reject': 'Reject order', 'mark_ready': 'Food ready', 'mark_picked_up': 'Handed to rider',
              'item_availability': 'Item stock', 'store_status': 'Outlet online / offline', 'menu_sync': 'Menu sync'}

    @property
    def label(self):
        return self.LABELS.get(self.action, self.action)

    class Meta:
        ordering = ['-created_at']


class PlatformMenuMapping(models.Model):
    """How one of our menu items is listed on one platform outlet."""
    class Sync(models.TextChoices):
        PENDING = 'pending', 'Not synced yet'
        SYNCED = 'synced', 'Synced'
        FAILED = 'failed', 'Sync failed'

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='menu_mappings')
    menu_item = models.ForeignKey('core.MenuItem', on_delete=models.CASCADE, related_name='platform_mappings')
    external_item_id = models.CharField(max_length=120, blank=True)
    external_variant_id = models.CharField(max_length=120, blank=True)
    is_listed = models.BooleanField(default=True)
    is_available = models.BooleanField(default=True, help_text='In stock on the platform')
    platform_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_synced_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sync_status = models.CharField(max_length=10, choices=Sync.choices, default=Sync.PENDING)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['connection', 'menu_item'], name='uniq_mapping_item'),
            models.UniqueConstraint(fields=['connection', 'external_item_id', 'external_variant_id'],
                                    condition=~models.Q(external_item_id=''), name='uniq_mapping_external_id'),
        ]

    @property
    def effective_price(self):
        if self.platform_price is not None:
            return self.platform_price
        return self.menu_item.markup_price or self.menu_item.price


class MenuSyncJob(models.Model):
    class State(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SUBMITTED = 'submitted', 'Submitted – waiting for platform'
        SUCCEEDED = 'succeeded', 'Live on platform'
        MANUAL = 'manual', 'Exported for manual upload'
        FAILED = 'failed', 'Failed'

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='menu_sync_jobs')
    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED)
    items_count = models.PositiveIntegerField(default=0)
    reference = models.CharField(max_length=120, blank=True)
    errors = models.JSONField(default=list, blank=True)
    menu_snapshot = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class SettlementBatch(models.Model):
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='settlements')
    reference = models.CharField(max_length=120, blank=True)
    payout_date = models.DateField(null=True, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    file_name = models.CharField(max_length=200, blank=True)
    order_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    taxes = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    adjustments = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_payout = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    expected_payout = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    lines_count = models.PositiveIntegerField(default=0)
    matched = models.PositiveIntegerField(default=0)
    mismatched = models.PositiveIntegerField(default=0)
    unmatched = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def known_paid(self):
        """Paid for orders we could match — the fair side of the comparison with ``expected_payout``."""
        from django.db.models import Sum
        return self.lines.exclude(link__isnull=True).aggregate(v=Sum('net_paid'))['v'] or 0

    @property
    def difference(self):
        return self.known_paid - self.expected_payout

    @property
    def unknown_paid(self):
        return self.net_payout - self.known_paid


class SettlementLine(models.Model):
    class Match(models.TextChoices):
        MATCHED = 'matched', 'Matched'
        MISMATCH = 'mismatch', 'Amount differs'
        UNMATCHED = 'unmatched', 'Order not found'

    batch = models.ForeignKey(SettlementBatch, on_delete=models.CASCADE, related_name='lines')
    link = models.ForeignKey(ExternalOrderLink, on_delete=models.SET_NULL, null=True, blank=True, related_name='settlement_lines')
    external_order_id = models.CharField(max_length=120)
    order_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    commission = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxes = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    adjustments = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    expected_net = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    difference = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    match = models.CharField(max_length=10, choices=Match.choices)

    class Meta:
        ordering = ['match', 'external_order_id']



# ==========================================================================================
# Hotel channels (OTAs / channel managers): room-type mapping, channel price and allotment
# ==========================================================================================

class ChannelRoomMapping(models.Model):
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='room_mappings')
    category = models.ForeignKey('hotel.RoomCategory', on_delete=models.CASCADE, related_name='channel_mappings')
    external_room_id = models.CharField(max_length=120, blank=True, help_text="Room type code on the channel")
    external_rate_plan_id = models.CharField(max_length=120, blank=True, help_text="Rate plan code on the channel")
    rate_adjust_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0,
                                          help_text="Channel price = your price ± this %, e.g. 15 to cover commission")
    allotment = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Most rooms of this type offered here; blank = all free rooms")
    is_closed = models.BooleanField(default=False, help_text="Stop selling this room type on this channel")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('connection', 'category')
        ordering = ['category__name']

    def __str__(self):
        return f"{self.connection} · {self.category.name}"
