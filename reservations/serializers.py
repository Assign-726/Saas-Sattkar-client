import re
from rest_framework import serializers
from django.utils import timezone
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from reservations.models import Reservation, ReservationSetting
from super_admin.models import Restaurant, RestaurantBranch


class ReservationSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    restaurant_slug = serializers.CharField(write_only=True, required=False)
    branch_slug = serializers.CharField(write_only=True, required=False, allow_blank=True)
    branch_id = serializers.ReadOnlyField(source='branch.id')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    hp_website = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Reservation
        fields = [
            'id', 'restaurant', 'restaurant_slug', 'branch', 'branch_slug', 'branch_id', 'branch_name',
            'customer_name', 'phone', 'email', 'party_size', 'date', 'start_time',
            'duration_minutes', 'occasion', 'notes', 'status', 'status_display', 'source',
            'hp_website', 'created_at', 'updated_at'
        ]
        read_only_fields = ['status', 'created_at', 'updated_at', 'branch_id', 'branch_name']
        extra_kwargs = {
            'restaurant': {'required': False},
            'branch': {'required': False, 'allow_null': True},
        }

    def validate(self, attrs):
        # 1. Bot Honeypot detection
        hp = attrs.pop('hp_website', None)
        if hp and str(hp).strip():
            raise serializers.ValidationError({'detail': 'Invalid request detected.'})

        # 2. Customer Name validation
        name = (attrs.get('customer_name') or '').strip()
        if not name:
            raise serializers.ValidationError({'customer_name': 'Name is required.'})
        if len(name) < 2:
            raise serializers.ValidationError({'customer_name': 'Name must be at least 2 characters long.'})
        if len(name) > 100:
            raise serializers.ValidationError({'customer_name': 'Name cannot exceed 100 characters.'})
        if '<' in name or '>' in name or '{' in name:
            raise serializers.ValidationError({'customer_name': 'Name contains invalid characters.'})
        attrs['customer_name'] = name

        # 3. Phone number validation
        phone = (attrs.get('phone') or '').strip()
        if not phone:
            raise serializers.ValidationError({'phone': 'Phone number is required.'})
        digits = re.sub(r'\D', '', phone)
        if len(digits) < 7 or len(digits) > 15:
            raise serializers.ValidationError({'phone': 'Please enter a valid phone number (7 to 15 digits).'})
        attrs['phone'] = phone

        # 4. Email format validation (if provided)
        email = (attrs.get('email') or '').strip()
        if email:
            try:
                validate_email(email)
            except DjangoValidationError:
                raise serializers.ValidationError({'email': 'Please enter a valid email address.'})
            attrs['email'] = email
        else:
            attrs['email'] = ''

        # 5. Restaurant resolution
        rslug = attrs.pop('restaurant_slug', None)
        if rslug and not attrs.get('restaurant'):
            restaurant = Restaurant.objects.filter(slug=rslug).first()
            if not restaurant:
                raise serializers.ValidationError({'restaurant_slug': 'Invalid restaurant slug'})
            attrs['restaurant'] = restaurant
        
        restaurant = attrs.get('restaurant')

        # 6. Branch resolution & validation
        bslug = attrs.pop('branch_slug', None)
        if bslug and restaurant and not attrs.get('branch'):
            branch = restaurant.branches.filter(slug=bslug).first()
            if not branch and str(bslug).isdigit():
                branch = restaurant.branches.filter(id=int(bslug)).first()
            if branch:
                attrs['branch'] = branch

        branch = attrs.get('branch')
        if branch and restaurant and branch.restaurant_id != restaurant.id:
            raise serializers.ValidationError({'branch': 'Branch does not belong to this restaurant.'})

        # 7. Party size bounds
        party_size = attrs.get('party_size', 0)
        if party_size <= 0:
            raise serializers.ValidationError({'party_size': 'Party size must be at least 1 person.'})
        if party_size > 50:
            raise serializers.ValidationError({'party_size': 'Online reservations are limited to 50 guests. Please call the restaurant directly.'})

        # 8. Date validation
        date = attrs.get('date')
        if not date:
            raise serializers.ValidationError({'date': 'Reservation date is required.'})
        today = timezone.localdate()
        if date < today:
            raise serializers.ValidationError({'date': 'Date cannot be in the past.'})
        if date > today + timezone.timedelta(days=90):
            raise serializers.ValidationError({'date': 'Reservations can only be made up to 90 days in advance.'})

        # 9. Time validation
        if not attrs.get('start_time'):
            raise serializers.ValidationError({'start_time': 'Reservation time is required.'})

        # 10. Enforce restaurant reservation settings
        if restaurant:
            settings = ReservationSetting.objects.filter(restaurant=restaurant).first()
            if settings:
                if settings.max_party_size and party_size > settings.max_party_size:
                    raise serializers.ValidationError({'party_size': f'Max party size is {settings.max_party_size}'})
                st = attrs.get('start_time')
                if st and not (settings.open_time <= st < settings.close_time):
                    raise serializers.ValidationError({'start_time': f'Operating hours are from {settings.open_time.strftime("%H:%M")} to {settings.close_time.strftime("%H:%M")}'})
                if st and settings.slot_interval_minutes:
                    minutes = st.hour * 60 + st.minute
                    if minutes % settings.slot_interval_minutes != 0:
                        raise serializers.ValidationError({'start_time': f'Start time must align to {settings.slot_interval_minutes}-minute intervals'})

        return attrs

    def create(self, validated_data):
        if not validated_data.get('source'):
            validated_data['source'] = 'web'
        return super().create(validated_data)
