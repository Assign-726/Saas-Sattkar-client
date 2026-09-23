from django.contrib import admin
from hotel.models import RoomCategory, Room, RoomBooking, HotelSettings


@admin.register(HotelSettings)
class HotelSettingsAdmin(admin.ModelAdmin):
    list_display = ('branch', 'hotel_name', 'payment_gateway_provider', 'payment_mode', 'enable_dynamic_gst', 'updated_at')
    list_filter = ('payment_gateway_provider', 'payment_mode', 'enable_dynamic_gst')
    search_fields = ('branch__name', 'hotel_name', 'gstin')


@admin.register(RoomCategory)
class RoomCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'branch', 'base_price_per_night', 'max_occupancy', 'is_active', 'created_at')
    list_filter = ('branch', 'is_active')
    search_fields = ('name', 'branch__name')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('room_number', 'category', 'branch', 'floor', 'status', 'is_active')
    list_filter = ('branch', 'category', 'status', 'is_active')
    search_fields = ('room_number', 'category__name', 'branch__name')


@admin.register(RoomBooking)
class RoomBookingAdmin(admin.ModelAdmin):
    list_display = (
        'booking_id',
        'guest_name',
        'room',
        'branch',
        'check_in_date',
        'check_out_date',
        'booking_status',
        'payment_status',
        'grand_total'
    )
    list_filter = ('booking_status', 'payment_status', 'branch', 'check_in_date')
    search_fields = ('booking_id', 'guest_name', 'guest_email', 'guest_phone', 'room__room_number')
    readonly_fields = ('booking_id', 'created_at', 'updated_at')
