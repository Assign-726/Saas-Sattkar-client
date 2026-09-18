from django.contrib import admin
from events.models import ClubEvent, TicketType, EventBooking


class TicketTypeInline(admin.TabularInline):
    model = TicketType
    extra = 1
    fields = ('name', 'base_price', 'total_allocated', 'quantity_sold', 'max_per_order', 'is_active', 'display_order')
    readonly_fields = ('quantity_sold',)


@admin.register(ClubEvent)
class ClubEventAdmin(admin.ModelAdmin):
    list_display = ('title', 'branch', 'start_datetime', 'end_datetime', 'is_published', 'is_featured', 'total_tickets_sold', 'total_revenue')
    list_filter = ('branch', 'is_published', 'is_featured', 'start_datetime')
    search_fields = ('title', 'venue_name', 'branch__name')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [TicketTypeInline]


@admin.register(TicketType)
class TicketTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'event', 'base_price', 'total_allocated', 'quantity_sold', 'available_quantity', 'is_active')
    list_filter = ('event__branch', 'is_active')
    search_fields = ('name', 'event__title')


@admin.register(EventBooking)
class EventBookingAdmin(admin.ModelAdmin):
    list_display = ('booking_reference', 'event', 'ticket_type', 'guest_name', 'guest_phone', 'quantity', 'total_amount', 'payment_status', 'checked_in', 'created_at')
    list_filter = ('event__branch', 'payment_status', 'checked_in', 'payment_method')
    search_fields = ('booking_reference', 'guest_name', 'guest_phone', 'guest_email', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('booking_reference', 'security_token', 'created_at', 'updated_at')
