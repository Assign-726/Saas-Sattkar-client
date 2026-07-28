from django.contrib import admin
from .models import Reservation, ReservationSetting


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'restaurant', 'customer_name', 'phone', 'party_size',
        'date', 'start_time', 'duration_minutes', 'status', 'created_at'
    )
    list_filter = ('restaurant', 'status', 'date')
    search_fields = ('customer_name', 'phone', 'email', 'notes')
    date_hierarchy = 'date'
    ordering = ('-date', '-start_time')


@admin.register(ReservationSetting)
class ReservationSettingAdmin(admin.ModelAdmin):
    list_display = ('restaurant', 'open_time', 'close_time', 'slot_interval_minutes', 'max_party_size', 'max_seats_per_slot')
    search_fields = ('restaurant__name',)
