from django.contrib import admin
from hotel.models import (
    RoomCategory,
    Room,
    RoomBooking,
    HotelSettings,
    RoomCategoryImage,
    CabServicePackage,
    FleetVehicle,
    CabBooking,
)


class RoomCategoryImageInline(admin.TabularInline):
    model = RoomCategoryImage
    extra = 1
    fields = ('image', 'image_url', 'caption', 'display_order')


@admin.register(RoomCategory)
class RoomCategoryAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'branch',
        'base_price_per_night',
        'price_is_tax_inclusive',
        'includes_free_transfer',
        'transfer_type',
        'show_on_banner',
        'max_occupancy',
        'is_active',
        'created_at'
    )
    list_filter = ('branch', 'includes_free_transfer', 'transfer_type', 'price_is_tax_inclusive', 'is_active')
    search_fields = ('name', 'branch__name', 'banner_badge_text')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [RoomCategoryImageInline]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('room_number', 'category', 'branch', 'floor', 'status', 'is_active')
    list_filter = ('branch', 'category', 'status', 'is_active')
    search_fields = ('room_number', 'category__name', 'branch__name')


class CabBookingInline(admin.TabularInline):
    model = CabBooking
    extra = 0
    fields = ('booking_id', 'package', 'pickup_datetime', 'pickup_location', 'drop_location', 'payment_status', 'trip_status', 'total_amount')
    readonly_fields = ('booking_id', 'total_amount')


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
    inlines = [CabBookingInline]


@admin.register(CabServicePackage)
class CabServicePackageAdmin(admin.ModelAdmin):
    list_display = ('title', 'branch', 'service_type', 'vehicle_category', 'base_price', 'included_km', 'included_hours', 'is_active')
    list_filter = ('branch', 'service_type', 'vehicle_category', 'is_active')
    search_fields = ('title', 'branch__name', 'itinerary_description')


@admin.register(FleetVehicle)
class FleetVehicleAdmin(admin.ModelAdmin):
    list_display = ('vehicle_number', 'branch', 'vehicle_category', 'driver_name', 'driver_phone', 'is_vendor_vehicle', 'status')
    list_filter = ('branch', 'vehicle_category', 'is_vendor_vehicle', 'status')
    search_fields = ('vehicle_number', 'driver_name', 'driver_phone', 'branch__name')


@admin.register(CabBooking)
class CabBookingAdmin(admin.ModelAdmin):
    list_display = ('booking_id', 'branch', 'room_booking', 'package', 'assigned_vehicle', 'is_complimentary', 'pickup_datetime', 'payment_status', 'trip_status', 'total_amount')
    list_filter = ('branch', 'is_complimentary', 'payment_status', 'trip_status', 'pickup_datetime')
    search_fields = ('booking_id', 'room_booking__guest_name', 'room_booking__guest_phone', 'flight_or_train_number', 'pickup_location', 'drop_location')
    readonly_fields = ('booking_id', 'created_at', 'updated_at')

