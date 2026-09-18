from decimal import Decimal
from rest_framework import serializers
from events.models import ClubEvent, TicketType, EventBooking


class TicketTypeSerializer(serializers.ModelSerializer):
    available_quantity = serializers.ReadOnlyField()
    is_sold_out = serializers.ReadOnlyField()
    is_sales_open = serializers.ReadOnlyField()

    class Meta:
        model = TicketType
        fields = [
            'id', 'event', 'name', 'description', 'base_price',
            'total_allocated', 'quantity_sold', 'available_quantity',
            'is_sold_out', 'is_sales_open', 'max_per_order',
            'sales_start_datetime', 'sales_end_datetime',
            'is_active', 'display_order', 'created_at'
        ]
        read_only_fields = ['quantity_sold', 'created_at']


class PublicTicketTypeSerializer(serializers.ModelSerializer):
    available_quantity = serializers.ReadOnlyField()
    is_sold_out = serializers.ReadOnlyField()
    is_sales_open = serializers.ReadOnlyField()

    class Meta:
        model = TicketType
        fields = [
            'id', 'name', 'description', 'base_price',
            'available_quantity', 'is_sold_out', 'is_sales_open',
            'max_per_order', 'display_order'
        ]


class ClubEventSerializer(serializers.ModelSerializer):
    ticket_types = TicketTypeSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    branch_slug = serializers.CharField(source='branch.slug', read_only=True)
    is_upcoming = serializers.ReadOnlyField()
    total_tickets_allocated = serializers.ReadOnlyField()
    total_tickets_sold = serializers.ReadOnlyField()
    total_revenue = serializers.ReadOnlyField()
    total_attendees_checked_in = serializers.ReadOnlyField()

    class Meta:
        model = ClubEvent
        fields = [
            'id', 'branch', 'branch_name', 'branch_slug', 'title', 'slug',
            'description', 'cover_image', 'start_datetime', 'end_datetime',
            'venue_name', 'venue_address', 'age_restriction', 'dress_code',
            'is_published', 'is_featured', 'terms_and_conditions',
            'is_upcoming', 'total_tickets_allocated', 'total_tickets_sold',
            'total_revenue', 'total_attendees_checked_in',
            'ticket_types', 'created_at', 'updated_at'
        ]
        read_only_fields = ['slug', 'created_at', 'updated_at']


class PublicClubEventSerializer(serializers.ModelSerializer):
    ticket_types = PublicTicketTypeSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    branch_slug = serializers.CharField(source='branch.slug', read_only=True)
    is_upcoming = serializers.ReadOnlyField()
    min_price = serializers.SerializerMethodField()

    class Meta:
        model = ClubEvent
        fields = [
            'id', 'branch_name', 'branch_slug', 'title', 'slug',
            'description', 'cover_image', 'start_datetime', 'end_datetime',
            'venue_name', 'venue_address', 'age_restriction', 'dress_code',
            'is_featured', 'terms_and_conditions', 'is_upcoming',
            'min_price', 'ticket_types'
        ]

    def get_min_price(self, obj):
        active_tiers = obj.ticket_types.filter(is_active=True)
        if active_tiers.exists():
            return str(min(t.base_price for t in active_tiers))
        return "0.00"


class EventBookingSerializer(serializers.ModelSerializer):
    event_title = serializers.CharField(source='event.title', read_only=True)
    ticket_tier_name = serializers.CharField(source='ticket_type.name', read_only=True)
    event_start_datetime = serializers.DateTimeField(source='event.start_datetime', read_only=True)
    event_end_datetime = serializers.DateTimeField(source='event.end_datetime', read_only=True)
    venue_name = serializers.CharField(source='event.venue_name', read_only=True)
    venue_address = serializers.CharField(source='event.venue_address', read_only=True)
    branch_name = serializers.CharField(source='event.branch.name', read_only=True)

    class Meta:
        model = EventBooking
        fields = [
            'id', 'booking_reference', 'event', 'event_title', 'event_start_datetime',
            'event_end_datetime', 'venue_name', 'venue_address', 'branch_name',
            'ticket_type', 'ticket_tier_name', 'guest_name',
            'guest_email', 'guest_phone', 'quantity', 'unit_price', 'tax_amount',
            'total_amount', 'payment_status', 'payment_method',
            'razorpay_order_id', 'razorpay_payment_id', 'booking_qr_code',
            'checked_in', 'checked_in_at', 'notes', 'created_at'
        ]
        read_only_fields = [
            'booking_reference', 'tax_amount', 'total_amount',
            'razorpay_order_id', 'razorpay_payment_id', 'booking_qr_code',
            'checked_in', 'checked_in_at', 'created_at'
        ]


class CheckoutInitiateSerializer(serializers.Serializer):
    ticket_type_id = serializers.IntegerField(required=True)
    quantity = serializers.IntegerField(min_value=1, default=1)
    guest_name = serializers.CharField(max_length=150, required=True)
    guest_email = serializers.EmailField(required=True)
    guest_phone = serializers.CharField(max_length=20, required=True)

    def validate(self, attrs):
        tt_id = attrs.get('ticket_type_id')
        qty = attrs.get('quantity')
        try:
            ticket = TicketType.objects.select_related('event', 'event__branch').get(pk=tt_id)
        except TicketType.DoesNotExist:
            raise serializers.ValidationError({"ticket_type_id": "Invalid ticket type."})

        if not ticket.event.is_published:
            raise serializers.ValidationError({"ticket_type_id": "This event is not published."})

        if not ticket.is_sales_open:
            raise serializers.ValidationError({"ticket_type_id": "Ticket sales are currently closed for this tier."})

        if qty > ticket.max_per_order:
            raise serializers.ValidationError({"quantity": f"Maximum {ticket.max_per_order} tickets allowed per order."})

        if ticket.available_quantity < qty:
            raise serializers.ValidationError({"quantity": f"Only {ticket.available_quantity} tickets available."})

        attrs['ticket_obj'] = ticket
        return attrs


class PaymentVerifySerializer(serializers.Serializer):
    booking_reference = serializers.CharField(required=True)
    razorpay_order_id = serializers.CharField(required=True)
    razorpay_payment_id = serializers.CharField(required=True)
    razorpay_signature = serializers.CharField(required=True)


class ManualBookingCreateSerializer(serializers.Serializer):
    ticket_type_id = serializers.IntegerField(required=True)
    quantity = serializers.IntegerField(min_value=1, default=1)
    guest_name = serializers.CharField(max_length=150, required=True)
    guest_email = serializers.EmailField(required=False, allow_blank=True, default="walkin@clubvenue.local")
    guest_phone = serializers.CharField(max_length=20, required=True)
    payment_method = serializers.ChoiceField(
        choices=[EventBooking.PaymentMethod.DOOR_CASH, EventBooking.PaymentMethod.COMPLIMENTARY],
        default=EventBooking.PaymentMethod.DOOR_CASH
    )
    notes = serializers.CharField(max_length=255, required=False, allow_blank=True)


class GateScanSerializer(serializers.Serializer):
    qr_payload = serializers.CharField(required=True, help_text="Raw scanned QR string or booking reference")
