"""Notifications and alerts for the restaurant admin.

A ``Notification`` is created once per event for a business (optionally one branch). Each
staff member then has their own read / archived state in ``NotificationState`` — one person
archiving an alert never hides it from a colleague.
"""
from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        ORDER_NEW = 'order_new', 'New order'
        ORDER_ADDON = 'order_addon', 'Items added to an order'
        ORDER_READY = 'order_ready', 'Food ready'
        KOT_NEW = 'kot_new', 'Kitchen ticket (KOT)'
        DELIVERY_NEW = 'delivery_new', 'New Zomato / Swiggy order'
        DELIVERY_CANCELLED = 'delivery_cancelled', 'Online order cancelled'
        DELIVERY_ATTENTION = 'delivery_attention', 'Online order needs attention'
        RESERVATION = 'reservation', 'New reservation'
        ROOM_BOOKING = 'room_booking', 'Channel room booking'
        ROOM_ALERT = 'room_alert', 'Channel booking needs attention'
        FEEDBACK = 'feedback', 'Guest feedback'
        LOW_STOCK = 'low_stock', 'Low stock'
        SYSTEM = 'system', 'System'

    class Level(models.TextChoices):
        INFO = 'info', 'Info'
        SUCCESS = 'success', 'Success'
        WARNING = 'warning', 'Warning'
        CRITICAL = 'critical', 'Critical'

    CATEGORY = {
        'order_new': 'orders', 'order_addon': 'orders', 'order_ready': 'kitchen', 'kot_new': 'kitchen',
        'delivery_new': 'online', 'delivery_cancelled': 'online', 'delivery_attention': 'online',
        'reservation': 'alerts', 'room_booking': 'rooms', 'room_alert': 'rooms', 'feedback': 'alerts', 'low_stock': 'alerts', 'system': 'alerts',
    }

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='admin_notifications')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='admin_notifications', help_text='Empty = every branch')
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=20, blank=True, help_text='qr, online, api, zomato, swiggy, kitchen, …')
    order = models.ForeignKey('core.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    data = models.JSONField(default=dict, blank=True)
    sound = models.BooleanField(default=True, help_text='Chime when it arrives')
    dedupe_key = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['restaurant', 'branch', '-id'])]
        constraints = [models.UniqueConstraint(fields=['restaurant', 'dedupe_key'], condition=~models.Q(dedupe_key=''),
                                               name='uniq_notification_dedupe')]

    def __str__(self):
        return self.title

    @property
    def category(self):
        return self.CATEGORY.get(self.kind, 'alerts')


class NotificationState(models.Model):
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name='states')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_states')
    read_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['notification', 'user'], name='uniq_notification_state')]
        indexes = [models.Index(fields=['user', 'notification'])]
