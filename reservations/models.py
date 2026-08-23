from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator


class Reservation(models.Model):
    class ReservationStatus(models.TextChoices):
        PENDING = 'P', _('Pending')
        CONFIRMED = 'C', _('Confirmed')
        CANCELLED = 'X', _('Cancelled')
        COMPLETED = 'D', _('Completed')
        NO_SHOW = 'N', _('No Show')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='reservations')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='reservations')
    customer_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    party_size = models.PositiveIntegerField(default=2, validators=[MinValueValidator(1)])
    date = models.DateField()
    start_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=90)
    occasion = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=1, choices=ReservationStatus.choices, default=ReservationStatus.PENDING)
    source = models.CharField(max_length=50, blank=True, help_text="e.g., web, phone, walk-in")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-start_time']
        indexes = [
            models.Index(fields=['restaurant', 'date']),
        ]

    def __str__(self):
        return f"Reservation {self.customer_name} ({self.party_size}) on {self.date} {self.start_time}"


class ReservationSetting(models.Model):
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='reservation_settings')
    open_time = models.TimeField(default='10:00')
    close_time = models.TimeField(default='22:00')
    slot_interval_minutes = models.PositiveIntegerField(default=15)
    max_party_size = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1)])
    max_seats_per_slot = models.PositiveIntegerField(null=True, blank=True, help_text="Total seats capacity per slot. Leave blank for unlimited.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Reservation Setting'
        verbose_name_plural = 'Reservation Settings'

    def __str__(self):
        return f"Settings for {self.restaurant.name}"
