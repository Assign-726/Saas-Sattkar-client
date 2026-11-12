from rest_framework import serializers
from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from hotel.models import (
    RoomCategory, Room, RoomBooking, CabServicePackage, FleetVehicle, CabBooking, HotelSettings,
    SpaTherapist, SpaTreatment, SpaAppointment, SpaPackage
)


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
            'includes_free_transfer',
            'transfer_type',
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
            'includes_free_transfer',
            'transfer_type',
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


# ==============================================================================
# TRAVEL DESK & CAB MANAGEMENT SERIALIZERS
# ==============================================================================

class CabServicePackageSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    service_type_display = serializers.ReadOnlyField(source='get_service_type_display')
    vehicle_category_display = serializers.ReadOnlyField(source='get_vehicle_category_display')

    class Meta:
        model = CabServicePackage
        fields = [
            'id',
            'branch',
            'branch_name',
            'title',
            'service_type',
            'service_type_display',
            'vehicle_category',
            'vehicle_category_display',
            'base_price',
            'included_km',
            'included_hours',
            'itinerary_description',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class FleetVehicleSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    vehicle_category_display = serializers.ReadOnlyField(source='get_vehicle_category_display')
    status_display = serializers.ReadOnlyField(source='get_status_display')
    languages_list = serializers.ReadOnlyField()
    badges_list = serializers.ReadOnlyField()
    features_text = serializers.ReadOnlyField()
    amenities_text = serializers.ReadOnlyField()
    gallery_images_text = serializers.ReadOnlyField()

    class Meta:
        model = FleetVehicle
        fields = [
            'id',
            'branch',
            'branch_name',
            'vehicle_number',
            'vehicle_model',
            'vehicle_category',
            'vehicle_category_display',
            'tag',
            'description',
            'cover_image',
            'gallery_images',
            'gallery_images_text',
            'passengers',
            'luggage',
            'fuel_type',
            'transmission',
            'features',
            'features_text',
            'amenities',
            'amenities_text',
            'price_per_km',
            'base_price_airport',
            'hourly_rate',
            'full_day_rate',
            'rating',
            'reviews_count',
            'driver_name',
            'driver_title',
            'driver_phone',
            'driver_photo',
            'driver_rating',
            'driver_experience_years',
            'trips_completed',
            'driver_bio',
            'driver_languages',
            'languages_list',
            'driver_badges',
            'badges_list',
            'driver_availability',
            'is_vendor_vehicle',
            'status',
            'status_display',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CabBookingSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    room_booking_code = serializers.ReadOnlyField(source='room_booking.booking_id')
    guest_name = serializers.SerializerMethodField()
    guest_phone = serializers.SerializerMethodField()
    guest_email = serializers.SerializerMethodField()
    room_number = serializers.ReadOnlyField(source='room_booking.room.room_number')
    package_title = serializers.ReadOnlyField(source='package.title')
    package_service_type = serializers.ReadOnlyField(source='package.service_type')
    package_vehicle_category = serializers.ReadOnlyField(source='package.get_vehicle_category_display')
    trip_type_display = serializers.ReadOnlyField(source='get_trip_type_display')
    source_display = serializers.ReadOnlyField(source='get_source_display')
    payment_status_display = serializers.ReadOnlyField(source='get_payment_status_display')
    trip_status_display = serializers.ReadOnlyField(source='get_trip_status_display')
    assigned_vehicle_details = serializers.SerializerMethodField()

    class Meta:
        model = CabBooking
        fields = [
            'id',
            'booking_id',
            'branch',
            'branch_name',
            'room_booking',
            'room_booking_code',
            'guest_name',
            'guest_phone',
            'guest_email',
            'room_number',
            'trip_type',
            'trip_type_display',
            'selected_cab_name',
            'preferred_chauffeur',
            'passengers',
            'luggage',
            'duration_hours',
            'special_requests',
            'source',
            'source_display',
            'package',
            'package_title',
            'package_service_type',
            'package_vehicle_category',
            'assigned_vehicle',
            'assigned_vehicle_details',
            'is_complimentary',
            'pickup_datetime',
            'pickup_location',
            'drop_location',
            'flight_or_train_number',
            'base_amount',
            'tax_amount',
            'total_amount',
            'payment_status',
            'payment_status_display',
            'trip_status',
            'trip_status_display',
            'guest_notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'booking_id', 'branch', 'created_at', 'updated_at']

    def get_guest_name(self, obj):
        return obj.display_guest_name

    def get_guest_phone(self, obj):
        return obj.display_guest_phone

    def get_guest_email(self, obj):
        return obj.display_guest_email

    def get_assigned_vehicle_details(self, obj):
        if not obj.assigned_vehicle:
            return None
        v = obj.assigned_vehicle
        return {
            'id': v.id,
            'vehicle_number': v.vehicle_number,
            'vehicle_model': v.vehicle_model,
            'vehicle_category': v.vehicle_category,
            'vehicle_category_display': v.get_vehicle_category_display(),
            'driver_name': v.driver_name,
            'driver_phone': v.driver_phone,
            'driver_photo': v.driver_photo,
            'driver_rating': str(v.driver_rating),
            'driver_experience_years': v.driver_experience_years,
            'is_vendor_vehicle': v.is_vendor_vehicle,
            'status': v.status,
            'status_display': v.get_status_display(),
        }


def generate_razorpay_order_id(branch, grand_total, receipt_id):
    """
    Creates Razorpay order for online payment.
    Falls back to deterministic order ID if credentials not configured or offline.
    """
    import requests
    from django.conf import settings
    from core.models import RestaurantSettings

    key_id = None
    key_secret = None

    h_settings = getattr(branch, 'hotel_settings', None) or HotelSettings.get_settings(branch)
    if h_settings and h_settings.razorpay_key_id and h_settings.razorpay_key_secret:
        key_id = h_settings.razorpay_key_id.strip()
        key_secret = h_settings.razorpay_key_secret.strip()

    if (not key_id or not key_secret) and branch.restaurant:
        r_settings = RestaurantSettings.get_settings(branch.restaurant)
        if r_settings and r_settings.razorpay_key_id and r_settings.razorpay_key_secret:
            key_id = r_settings.razorpay_key_id.strip()
            key_secret = r_settings.razorpay_key_secret.strip()

    if not key_id or not key_secret:
        key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)

    amount_paise = int(round(grand_total * Decimal('100')))
    if key_id and key_secret and amount_paise > 0:
        try:
            url = "https://api.razorpay.com/v1/orders"
            payload = {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": str(receipt_id)[:40],
                "payment_capture": 1,
            }
            resp = requests.post(url, auth=(key_id, key_secret), json=payload, timeout=8)
            if resp.status_code in [200, 201]:
                return resp.json().get('id'), key_id
        except Exception:
            pass

    return f"order_htl_{receipt_id.lower()}", (key_id or "rzp_test_placeholder")


class PublicCabBookingItemSerializer(serializers.Serializer):
    package_id = serializers.IntegerField(required=True)
    pickup_datetime = serializers.DateTimeField(required=True)
    pickup_location = serializers.CharField(max_length=255, required=True)
    drop_location = serializers.CharField(max_length=255, required=True)
    flight_or_train_number = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    guest_notes = serializers.CharField(required=False, allow_blank=True, default='')
    is_complimentary = serializers.BooleanField(required=False, default=False)


class PublicHotelCheckoutSerializer(serializers.Serializer):
    """
    Unified atomic checkout serializer:
    - Locks room inventory with select_for_update()
    - Creates RoomBooking record (PENDING)
    - Validates and creates linked CabBooking records (with complimentary override if eligible)
    - Generates Razorpay Order ID for combined grand total
    """
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    category_id = serializers.IntegerField(required=True)
    check_in_date = serializers.DateField(required=True)
    check_out_date = serializers.DateField(required=True)
    guest_name = serializers.CharField(max_length=150, required=True)
    guest_email = serializers.EmailField(required=False, allow_blank=True, default='')
    guest_phone = serializers.CharField(max_length=30, required=True)
    num_guests = serializers.IntegerField(required=False, default=1, min_value=1)
    special_requests = serializers.CharField(required=False, allow_blank=True, default='')
    payment_method = serializers.CharField(required=False, default='ONLINE_RAZORPAY')
    upi_transaction_ref = serializers.CharField(required=False, allow_blank=True, default='')
    cab_bookings = PublicCabBookingItemSerializer(many=True, required=False, default=[])

    def to_internal_value(self, data):
        mutable_data = data.copy() if hasattr(data, 'copy') else dict(data)
        # Normalize alternative keys and casing
        if 'cab_Bookings' in mutable_data and 'cab_bookings' not in mutable_data:
            mutable_data['cab_bookings'] = mutable_data['cab_Bookings']
        if 'room_id' in mutable_data and 'category_id' not in mutable_data:
            mutable_data['category_id'] = mutable_data['room_id']
        if 'check_in' in mutable_data and 'check_in_date' not in mutable_data:
            mutable_data['check_in_date'] = mutable_data['check_in']
        if 'check_out' in mutable_data and 'check_out_date' not in mutable_data:
            mutable_data['check_out_date'] = mutable_data['check_out']
        if 'customer_name' in mutable_data and 'guest_name' not in mutable_data:
            mutable_data['guest_name'] = mutable_data['customer_name']
        if 'phone' in mutable_data and 'guest_phone' not in mutable_data:
            mutable_data['guest_phone'] = mutable_data['phone']
        if 'email' in mutable_data and 'guest_email' not in mutable_data:
            mutable_data['guest_email'] = mutable_data['email']
        if 'adults' in mutable_data and 'num_guests' not in mutable_data:
            mutable_data['num_guests'] = mutable_data['adults']
        if 'notes' in mutable_data and 'special_requests' not in mutable_data:
            mutable_data['special_requests'] = mutable_data['notes']
        return super().to_internal_value(mutable_data)

    def validate(self, data):
        check_in = data.get('check_in_date')
        check_out = data.get('check_out_date')
        if check_out <= check_in:
            raise serializers.ValidationError({"check_out_date": "Check-out date must be strictly after check-in date."})

        category_id = data.get('category_id')
        try:
            category = RoomCategory.objects.get(id=category_id, is_active=True)
        except RoomCategory.DoesNotExist:
            raise serializers.ValidationError({"category_id": f"Active Room Category with ID {category_id} does not exist."})

        branch = category.branch
        if not getattr(branch, 'has_hotel_module', False):
            raise serializers.ValidationError("Hotel module is not active for this branch.")

        data['category_obj'] = category
        data['branch_obj'] = branch
        return data

    def create(self, validated_data):
        category = validated_data['category_obj']
        branch = validated_data['branch_obj']
        check_in_date = validated_data['check_in_date']
        check_out_date = validated_data['check_out_date']
        guest_name = validated_data['guest_name']
        guest_email = validated_data.get('guest_email')
        if not guest_email or '@' not in guest_email:
            guest_email = f"guest.{guest_name.lower().replace(' ', '')[:6]}@sharmahotel.internal"
        guest_phone = validated_data['guest_phone']
        num_guests = validated_data.get('num_guests', 1)
        special_requests = validated_data.get('special_requests', '')
        payment_method = validated_data.get('payment_method', 'ONLINE_RAZORPAY')
        upi_ref = validated_data.get('upi_transaction_ref', '') or ''
        cab_bookings_data = validated_data.get('cab_bookings', [])

        with transaction.atomic():
            # 1. Lock room inventory using select_for_update()
            # Must force immediate evaluation via list() so PostgreSQL acquires row-level write locks immediately.
            candidate_rooms = list(Room.objects.select_for_update().filter(
                category=category,
                branch=branch,
                is_active=True
            ).order_by('room_number'))

            if not candidate_rooms:
                raise serializers.ValidationError({
                    "room": "No active rooms found in this category."
                })

            from hotel import pricing
            h_settings = getattr(branch, 'hotel_settings', None) or HotelSettings.get_settings(branch)
            q = pricing.quote(category, check_in_date, check_out_date, h_settings)
            if q['restriction']:
                raise serializers.ValidationError({"room": q['restriction']})

            # Check free rooms among the locked candidate rooms
            candidate_room_ids = [r.id for r in candidate_rooms]
            target_room = pricing.free_rooms(
                Room.objects.filter(id__in=candidate_room_ids),
                branch,
                check_in_date,
                check_out_date
            ).first()

            if not target_room:
                raise serializers.ValidationError({
                    "room": "No available rooms in this category for the selected dates. Please choose different dates or category."
                })

            # Room pricing & taxes: per-night rates incl. seasonal overrides (hotel/pricing.py)
            room_total_charges, room_tax_amount, room_grand_total = q['net'], q['tax'], q['total']

            # 2. Create the RoomBooking record (PENDING)
            try:
                room_booking = RoomBooking.objects.create(
                room=target_room,
                branch=branch,
                guest_name=guest_name,
                guest_email=guest_email,
                guest_phone=guest_phone,
                check_in_date=check_in_date,
                check_out_date=check_out_date,
                num_guests=num_guests,
                total_room_charges=room_total_charges,
                taxes=room_tax_amount,
                grand_total=room_grand_total,
                booking_status=RoomBooking.BookingStatus.RESERVED,
                payment_status=RoomBooking.PaymentStatus.PENDING,
                payment_method=payment_method,
                special_requests=special_requests,
                upi_transaction_ref=upi_ref,
                source=RoomBooking.Source.WEBSITE,
                )
            except DjangoValidationError as err:
                msg = err.message if hasattr(err, 'message') else (err.messages[0] if hasattr(err, 'messages') and err.messages else str(err))
                raise serializers.ValidationError({"room": msg})

            # 3. Validate and create linked CabBooking records
            created_cabs = []
            paid_cabs_total = Decimal('0.00')

            for item in cab_bookings_data:
                package_id = item.get('package_id')
                package = CabServicePackage.objects.filter(id=package_id, branch=branch, is_active=True).first()
                if not package:
                    package = CabServicePackage.objects.filter(id=package_id, is_active=True).first()
                if not package:
                    raise serializers.ValidationError({
                        "cab_bookings": f"Cab service package with ID {package_id} not found or inactive."
                    })

                # Check complimentary eligibility:
                # Only AIRPORT_TRANSFER qualifies for complimentary; TOURISM_PACKAGE is ALWAYS a paid upsell
                qualifies_complimentary = False
                if (
                    package.service_type == CabServicePackage.ServiceType.AIRPORT_TRANSFER
                    and category.includes_free_transfer
                    and category.transfer_type != 'NONE'
                ):
                    qualifies_complimentary = True

                if qualifies_complimentary:
                    cab_base = Decimal('0.00')
                    cab_tax = Decimal('0.00')
                    cab_total = Decimal('0.00')
                    cab_payment_status = CabBooking.PaymentStatus.COMPLIMENTARY
                    is_comp = True
                else:
                    cab_base = package.base_price
                    # Transport dynamic tax (5% GST)
                    transport_tax_rate = Decimal('0.05')
                    cab_tax = round(cab_base * transport_tax_rate, 2)
                    cab_total = cab_base + cab_tax
                    cab_payment_status = CabBooking.PaymentStatus.PENDING
                    is_comp = False
                    paid_cabs_total += cab_total

                cab_booking = CabBooking.objects.create(
                    branch=branch,
                    room_booking=room_booking,
                    package=package,
                    is_complimentary=is_comp,
                    pickup_datetime=item['pickup_datetime'],
                    pickup_location=item['pickup_location'],
                    drop_location=item['drop_location'],
                    flight_or_train_number=item.get('flight_or_train_number', ''),
                    base_amount=cab_base,
                    tax_amount=cab_tax,
                    total_amount=cab_total,
                    payment_status=cab_payment_status,
                    trip_status=CabBooking.TripStatus.SCHEDULED,
                    guest_notes=item.get('guest_notes', ''),
                )
                created_cabs.append(cab_booking)

            # 4. Generate single combined Razorpay Order ID
            combined_grand_total = room_booking.grand_total + paid_cabs_total
            razorpay_order_id, razorpay_key_id = generate_razorpay_order_id(
                branch=branch,
                grand_total=combined_grand_total,
                receipt_id=room_booking.booking_id
            )

            return {
                'room_booking': room_booking,
                'cab_bookings': created_cabs,
                'room_charges': room_booking.total_room_charges,
                'room_taxes': room_booking.taxes,
                'room_total': room_booking.grand_total,
                'cab_total': paid_cabs_total,
                'grand_total': combined_grand_total,
                'razorpay_order_id': razorpay_order_id,
                'razorpay_key_id': razorpay_key_id,
                'currency': 'INR',
            }


# ==============================================================================
# SPA & WELLNESS SERIALIZERS
# ==============================================================================

class SpaTherapistSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    languages_list = serializers.ReadOnlyField()
    languages = serializers.SerializerMethodField()

    class Meta:
        model = SpaTherapist
        fields = [
            'id',
            'branch',
            'branch_name',
            'name',
            'title',
            'experience_years',
            'photo',
            'rating',
            'sessions_completed',
            'skills',
            'languages',
            'languages_list',
            'categories',
            'category_names',
            'certifications',
            'specialties',
            'bio',
            'availability',
            'is_active',
        ]
        read_only_fields = ['id']

    category_names = serializers.SerializerMethodField()

    def get_category_names(self, obj):
        names = {
            'massages': 'Massages & Bodywork',
            'ayurveda': 'Ancient Ayurveda',
            'facials': 'Aesthetic Facials',
            'steam-hydro': 'Steam & Hydrotherapy',
            'couples': 'Couples Sanctuary',
        }
        cats = obj.categories or []
        return [names.get(c, c.replace('-', ' ').title()) for c in cats]

    def get_languages(self, obj):
        if not obj.languages:
            return ["English", "Hindi"]
        if isinstance(obj.languages, list):
            return obj.languages
        return [s.strip() for s in str(obj.languages).split(',') if s.strip()]


class SpaTreatmentSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    total_price = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()

    class Meta:
        model = SpaTreatment
        fields = [
            'id',
            'branch',
            'branch_name',
            'name',
            'slug',
            'category',
            'category_name',
            'tag',
            'duration_minutes',
            'duration_options',
            'price',
            'original_price',
            'gst_percent',
            'total_price',
            'rating',
            'reviews_count',
            'suite_type',
            'short_description',
            'full_description',
            'ritual_steps',
            'benefits',
            'suitable_for',
            'contraindications',
            'included_amenities',
            'cover_image',
            'gallery_images',
            'is_featured',
            'is_active',
        ]
        read_only_fields = ['id']

    def get_total_price(self, obj):
        price = obj.price or Decimal('0.00')
        gst = obj.gst_percent or Decimal('18.00')
        return (price * (1 + gst / Decimal('100.00'))).quantize(Decimal('0.01'))

    def get_category_name(self, obj):
        names = {
            'massages': 'Holistic Bodywork & Massages',
            'ayurveda': 'Ancient Ayurvedic Healing',
            'facials': 'Aesthetic Skin Radiance',
            'steam-hydro': 'Hydrotherapy & Herbal Steam',
            'couples': 'Couples & Romance Sanctuaries',
        }
        return names.get(obj.category, obj.category.title() if obj.category else 'Holistic Wellness')


class SpaPackageSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')

    class Meta:
        model = SpaPackage
        fields = [
            'id',
            'branch',
            'branch_name',
            'title',
            'slug',
            'tag',
            'duration',
            'duration_minutes',
            'price',
            'original_price',
            'image',
            'description',
            'inclusions',
            'popular',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class SpaAppointmentSerializer(serializers.ModelSerializer):
    branch_name = serializers.ReadOnlyField(source='branch.name')
    reference_id = serializers.ReadOnlyField()
    treatment_name = serializers.ReadOnlyField(source='treatment.name')
    treatment_category = serializers.ReadOnlyField(source='treatment.category')
    therapist_name = serializers.ReadOnlyField(source='therapist.name')
    room_number = serializers.ReadOnlyField(source='booking.room.room_number')
    room_booking_code = serializers.ReadOnlyField(source='booking.booking_id')
    status_display = serializers.ReadOnlyField(source='get_status_display')
    settled_by_display = serializers.ReadOnlyField(source='get_settled_by_display')
    total_amount = serializers.ReadOnlyField(source='total')
    end_time_formatted = serializers.SerializerMethodField()

    class Meta:
        model = SpaAppointment
        fields = [
            'id',
            'reference_id',
            'branch',
            'branch_name',
            'treatment',
            'treatment_name',
            'treatment_category',
            'therapist',
            'therapist_name',
            'date',
            'start_time',
            'end_time_formatted',
            'duration_minutes',
            'booking',
            'room_booking_code',
            'room_number',
            'guest_name',
            'guest_phone',
            'price',
            'gst_percent',
            'total_amount',
            'status',
            'status_display',
            'settled_by',
            'settled_by_display',
            'settled_at',
            'notes',
            'created_at',
        ]
        read_only_fields = ['id', 'reference_id', 'created_at']

    def get_end_time_formatted(self, obj):
        return obj.end_time.strftime('%H:%M') if getattr(obj, 'end_time', None) else ''
