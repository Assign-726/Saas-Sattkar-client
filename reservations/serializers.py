from rest_framework import serializers
from django.utils import timezone
from reservations.models import Reservation
from super_admin.models import Restaurant
from reservations.models import ReservationSetting


class ReservationSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    restaurant_slug = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Reservation
        fields = [
            'id', 'restaurant', 'restaurant_slug', 'customer_name', 'phone', 'email',
            'party_size', 'date', 'start_time', 'duration_minutes', 'occasion', 'notes',
            'status', 'status_display', 'source', 'created_at', 'updated_at'
        ]
        read_only_fields = ['status', 'created_at', 'updated_at']
        extra_kwargs = {
            'restaurant': {'required': False},
        }

    def validate(self, attrs):
        # Allow setting restaurant via slug for public form convenience
        rslug = attrs.pop('restaurant_slug', None)
        if rslug and not attrs.get('restaurant'):
            restaurant = Restaurant.objects.filter(slug=rslug).first()
            if not restaurant:
                raise serializers.ValidationError({'restaurant_slug': 'Invalid restaurant slug'})
            attrs['restaurant'] = restaurant
        # Basic sanity checks
        if attrs.get('party_size', 0) <= 0:
            raise serializers.ValidationError({'party_size': 'Party size must be at least 1'})
        # Optionally prevent past dates
        if attrs.get('date'):
            try:
                if attrs['date'] < timezone.localdate():
                    raise serializers.ValidationError({'date': 'Date cannot be in the past'})
            except Exception:
                pass
        # Require date and start_time
        if not attrs.get('date'):
            raise serializers.ValidationError({'date': 'This field is required.'})
        if not attrs.get('start_time'):
            raise serializers.ValidationError({'start_time': 'This field is required.'})
        # Enforce settings if present
        restaurant = attrs.get('restaurant')
        if restaurant:
            settings = ReservationSetting.objects.filter(restaurant=restaurant).first()
            if settings:
                # Max party size
                if settings.max_party_size and attrs.get('party_size') and attrs['party_size'] > settings.max_party_size:
                    raise serializers.ValidationError({'party_size': f'Max party size is {settings.max_party_size}'})
                # Hours check
                st = attrs.get('start_time')
                if st and not (settings.open_time <= st < settings.close_time):
                    raise serializers.ValidationError({'start_time': 'Outside operating hours'})
                # Interval check
                if st and settings.slot_interval_minutes:
                    minutes = st.hour * 60 + st.minute
                    if minutes % settings.slot_interval_minutes != 0:
                        raise serializers.ValidationError({'start_time': f'Start time must align to {settings.slot_interval_minutes}-minute intervals'})
        return attrs

    def create(self, validated_data):
        # Default values for public bookings
        if not validated_data.get('source'):
            validated_data['source'] = 'web'
        # Status remains default (Pending) from model
        return super().create(validated_data)
