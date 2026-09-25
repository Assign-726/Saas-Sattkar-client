from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from hotel.models import RoomCategory, Room, RoomBooking


class RoomCategorySerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    total_rooms_count = serializers.ReadOnlyField()
    effective_image = serializers.ReadOnlyField(source='effective_image_url')
    discount_percent = serializers.ReadOnlyField()
    all_images = serializers.ReadOnlyField(source='all_images_list')
    video_links_list = serializers.ReadOnlyField()

    class Meta:
        model = RoomCategory
        fields = [
            'id',
            'branch',
            'branch_name',
            'name',
            'slug',
            'description',
            'base_price_per_night',
            'price_is_tax_inclusive',
            'original_price_per_night',
            'discount_percent',
            'custom_tax_rate',
            'show_on_banner',
            'banner_badge_text',
            'max_occupancy',
            'max_adults',
            'max_children',
            'bed_type',
            'room_size_sqft',
            'amenities',
            'cover_image',
            'cover_image_url',
            'effective_image',
            'all_images',
            'video_links',
            'video_links_list',
            'total_rooms_count',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'discount_percent', 'all_images', 'video_links_list', 'created_at', 'updated_at']


class RoomSerializer(serializers.ModelSerializer):
    category_name = serializers.ReadOnlyField(source='category.name')
    category_base_price = serializers.ReadOnlyField(source='category.base_price_per_night')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    status_display = serializers.ReadOnlyField(source='get_status_display')

    class Meta:
        model = Room
        fields = [
            'id',
            'category',
            'category_name',
            'category_base_price',
            'branch',
            'branch_name',
            'room_number',
            'floor',
            'status',
            'status_display',
            'notes',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class RoomBookingSerializer(serializers.ModelSerializer):
    room_number = serializers.ReadOnlyField(source='room.room_number')
    category_name = serializers.ReadOnlyField(source='room.category.name')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    status_display = serializers.ReadOnlyField(source='get_booking_status_display')
    payment_status_display = serializers.ReadOnlyField(source='get_payment_status_display')
    nights_count = serializers.ReadOnlyField()

    class Meta:
        model = RoomBooking
        fields = [
            'id',
            'booking_id',
            'room',
            'room_number',
            'category_name',
            'branch',
            'branch_name',
            'guest_name',
            'guest_email',
            'guest_phone',
            'check_in_date',
            'check_out_date',
            'check_in_time',
            'check_out_time',
            'num_guests',
            'nights_count',
            'total_room_charges',
            'taxes',
            'grand_total',
            'payment_status',
            'payment_status_display',
            'payment_method',
            'booking_status',
            'status_display',
            'special_requests',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'booking_id', 'branch', 'total_room_charges', 'taxes', 'grand_total', 'created_at', 'updated_at']

    def validate(self, data):
        check_in = data.get('check_in_date') or (self.instance.check_in_date if self.instance else None)
        check_out = data.get('check_out_date') or (self.instance.check_out_date if self.instance else None)
        room = data.get('room') or (self.instance.room if self.instance else None)

        if not check_in or not check_out:
            raise serializers.ValidationError("Both check-in and check-out dates are required.")

        if check_out <= check_in:
            raise serializers.ValidationError({"check_out_date": "Check-out date must be strictly after check-in date."})

        # Overlap validation on active bookings (RESERVED, CHECKED_IN)
        if room:
            overlap_qs = RoomBooking.objects.filter(
                room=room,
                booking_status__in=[
                    RoomBooking.BookingStatus.RESERVED,
                    RoomBooking.BookingStatus.CHECKED_IN,
                ],
                check_in_date__lt=check_out,
                check_out_date__gt=check_in,
            )
            if self.instance:
                overlap_qs = overlap_qs.exclude(pk=self.instance.pk)

            if overlap_qs.exists():
                conflict = overlap_qs.first()
                raise serializers.ValidationError({
                    "room": f"Room {room.room_number} is already booked from {conflict.check_in_date} to {conflict.check_out_date}."
                })

        return data


class PublicAvailabilityCategorySerializer(serializers.ModelSerializer):
    available_rooms_count = serializers.IntegerField(read_only=True)
    available_room_ids = serializers.ListField(read_only=True)
    nights = serializers.IntegerField(read_only=True, default=1)
    estimated_room_charges = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    estimated_taxes = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    estimated_grand_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    effective_image = serializers.ReadOnlyField(source='effective_image_url')
    discount_percent = serializers.ReadOnlyField()
    all_images = serializers.ReadOnlyField(source='all_images_list')
    video_links = serializers.ReadOnlyField(source='video_links_list')

    class Meta:
        model = RoomCategory
        fields = [
            'id',
            'name',
            'slug',
            'description',
            'base_price_per_night',
            'price_is_tax_inclusive',
            'original_price_per_night',
            'discount_percent',
            'show_on_banner',
            'banner_badge_text',
            'max_occupancy',
            'max_adults',
            'max_children',
            'bed_type',
            'room_size_sqft',
            'amenities',
            'effective_image',
            'all_images',
            'video_links',
            'available_rooms_count',
            'available_room_ids',
            'nights',
            'estimated_room_charges',
            'estimated_taxes',
            'estimated_grand_total',
        ]
