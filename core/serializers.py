from rest_framework import serializers
# pyrefly: ignore [missing-import]
from .models import Category, SubCategory, MenuItem, Order, OrderItem, RestaurantLanding, GalleryImage, TeamMember, Review, RestaurantSettings, Event
from super_admin.models import Restaurant
from django.utils import timezone


class CategorySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'name', 'description', 'image', 'image_url']
        read_only_fields = ['image_url']

    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            request = self.context.get('request')
            if request is not None:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None


class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SubCategory
        fields = ['id', 'name', 'category']


class MenuItemSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = MenuItem
        fields = ['id', 'name', 'description', 'price', 'image', 'image_url', 'availability', 'subcategory', 'category']
        read_only_fields = ['image_url']
        
    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            # This ensures we get the full URL including the domain
            request = self.context.get('request')
            if request is not None:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None
    
    def get_category(self, obj):
        try:
            # Prefer direct category field if set
            if getattr(obj, 'category', None):
                return {
                    'id': obj.category.id,
                    'name': obj.category.name
                }
            # Fallback to subcategory -> category
            if hasattr(obj, 'subcategory') and obj.subcategory and getattr(obj.subcategory, 'category', None):
                return {
                    'id': obj.subcategory.category.id,
                    'name': obj.subcategory.category.name
                }
            return None
        except Exception as e:
            print(f"Error getting category for item {obj.id}: {str(e)}")
            return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Ensure we have all required fields
        data['category'] = self.get_category(instance)
        return data


class OrderItemSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.ReadOnlyField(source='menu_item.name')
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'menu_item', 'menu_item_name', 'quantity', 'unit_price', 'subtotal', 'notes']
        read_only_fields = ['unit_price', 'subtotal']


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    payment_method_display = serializers.CharField(source='get_payment_method_display', read_only=True)
    payment_status_display = serializers.CharField(source='get_payment_status_display', read_only=True)
    restaurant_slug = serializers.ReadOnlyField(source='restaurant.slug')
    remaining_seconds = serializers.SerializerMethodField()
    progress_pct = serializers.SerializerMethodField()
    coupon_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    applied_coupon_code = serializers.ReadOnlyField(source='coupon.code')
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    order_number = serializers.CharField(read_only=True)
    branch_order_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'branch_order_number', 'customer_name', 'phone', 'table_number', 'order_time',
            'status', 'status_display', 'total_amount', 'discount_amount', 'notes', 'items',
            'coupon_code', 'applied_coupon_code', 'payment_method', 'payment_method_display', 'payment_status', 'payment_status_display',
            'razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature', 'restaurant_slug', 'security_token',
            'prep_minutes', 'prep_start', 'ready_at', 'remaining_seconds', 'progress_pct'
        ]
        read_only_fields = ['order_time', 'total_amount', 'discount_amount', 'restaurant_slug', 'security_token', 'order_number', 'branch_order_number']

    def create(self, validated_data):
        # pyrefly: ignore [missing-import]
        from .models import Coupon, RestaurantSettings
        items_data = validated_data.pop('items')
        coupon_code = validated_data.pop('coupon_code', '').strip().upper() if 'coupon_code' in validated_data else ''
        payment_method = validated_data.get('payment_method', 'C')

        # Determine restaurant from items and validate consistency
        restaurant = None
        resolved_items = []
        for item_data in items_data:
            menu_item = item_data['menu_item']
            if restaurant is None:
                restaurant = getattr(menu_item, 'restaurant', None)
            elif getattr(menu_item, 'restaurant_id', None) != getattr(restaurant, 'id', None):
                raise serializers.ValidationError("All items in an order must belong to the same restaurant")
            if not menu_item.availability:
                raise serializers.ValidationError(f"Menu item {menu_item.name} is not available")
            resolved_items.append(item_data)

        # Validate settings for the restaurant
        settings_obj = RestaurantSettings.get_settings(restaurant)
        if not settings_obj.enable_customer_ordering:
            raise serializers.ValidationError("Ordering is currently disabled for this restaurant.")

        if payment_method == 'O':
            if not settings_obj.enable_online_payment or not settings_obj.razorpay_key_id or not settings_obj.razorpay_key_secret:
                raise serializers.ValidationError("Online payment is not enabled for this restaurant.")

            rzp_order_id = validated_data.get('razorpay_order_id')
            rzp_payment_id = validated_data.get('razorpay_payment_id')
            rzp_signature = validated_data.get('razorpay_signature')

            if not rzp_order_id or not rzp_payment_id or not rzp_signature:
                raise serializers.ValidationError("Online payment details (order ID, payment ID, and signature) are required.")

            import hmac, hashlib
            key_secret = settings_obj.razorpay_key_secret or ''
            msg = f"{rzp_order_id}|{rzp_payment_id}"
            generated_signature = hmac.new(
                key_secret.encode('utf-8'),
                msg.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(generated_signature, rzp_signature):
                raise serializers.ValidationError("Razorpay signature verification failed.")

            validated_data['payment_status'] = 'paid' # Paid
        else:
            if not settings_obj.enable_pay_at_counter:
                raise serializers.ValidationError("Pay at counter is not enabled for this restaurant. You must pay online.")
            validated_data['payment_status'] = 'C' # Pay at Counter

        # Attach restaurant to order
        order = Order.objects.create(restaurant=restaurant, **validated_data)
        
        for item_data in resolved_items:
            menu_item = item_data['menu_item']
            OrderItem.objects.create(
                order=order,
                menu_item=menu_item,
                quantity=item_data['quantity'],
                unit_price=menu_item.price,
                notes=item_data.get('notes', '')
            )
        # Apply coupon if provided
        if coupon_code:
            coupon_qs = Coupon.objects.filter(code__iexact=coupon_code)
            if restaurant:
                coupon = coupon_qs.filter(restaurant=restaurant).first() or coupon_qs.filter(restaurant__isnull=True).first()
            else:
                coupon = coupon_qs.first()
            if coupon:
                # compute subtotal using Decimal baseline
                from decimal import Decimal
                subtotal = sum((it.subtotal for it in order.items.all()), Decimal('0.00'))
                if coupon.is_valid_for(subtotal, restaurant=restaurant):
                    order.coupon = coupon
                    order.update_total()
                    # increment usage counter optimistically
                    if coupon.max_uses is None or coupon.used_count < (coupon.max_uses or 0) or coupon.max_uses == 0:
                        coupon.used_count = (coupon.used_count or 0) + 1
                        coupon.save(update_fields=['used_count'])
        else:
            # Force total amount calculation
            order.update_total()

        # Publish SSE notification for new order
        try:
            from core.sse import publish
            if order.restaurant and order.restaurant.slug:
                publish(order.restaurant.slug, {'type': 'new_order', 'order_id': order.id})
        except Exception:
            pass

        return order

    def get_remaining_seconds(self, obj):
        try:
            if obj.ready_at:
                now = timezone.now()
                delta = (obj.ready_at - now).total_seconds()
                return int(delta) if delta > 0 else 0
            if obj.prep_minutes and obj.order_time:
                end = obj.order_time + timezone.timedelta(minutes=obj.prep_minutes)
                delta = (end - timezone.now()).total_seconds()
                return int(delta) if delta > 0 else 0
        except Exception:
            return None
        return None

    def get_progress_pct(self, obj):
        try:
            if obj.prep_minutes:
                start = obj.prep_start or obj.order_time
                end = obj.ready_at or (start + timezone.timedelta(minutes=obj.prep_minutes))
                total = (end - start).total_seconds()
                if total <= 0:
                    return 100
                elapsed = (timezone.now() - start).total_seconds()
                pct = max(0, min(100, int((elapsed/total)*100)))
                return pct
        except Exception:
            return None
        return None


class RestaurantSerializer(serializers.ModelSerializer):
    address = serializers.ReadOnlyField()
    invoice_heading = serializers.ReadOnlyField()

    class Meta:
        model = Restaurant
        fields = [
            'id', 'name', 'slug', 'address', 'address_line_1', 'address_line_2',
            'city', 'state', 'pincode', 'email', 'mailing_address', 'phone',
            'gst_type', 'invoice_heading', 'is_active'
        ]


class GalleryImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GalleryImage
        fields = ['id', 'image', 'caption', 'display_order']


class TeamMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamMember
        fields = ['id', 'name', 'position', 'photo', 'display_order', 'active']


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ['id', 'name', 'stars', 'description', 'display_order', 'active']


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = ['id', 'title', 'price', 'image', 'description', 'bullet_1', 'bullet_2', 'bullet_3', 'display_order', 'active']


class RestaurantLandingSerializer(serializers.ModelSerializer):
    gallery_images = GalleryImageSerializer(many=True, read_only=True)
    team_members = TeamMemberSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)
    events = EventSerializer(many=True, read_only=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')
    restaurant_slug = serializers.ReadOnlyField(source='restaurant.slug')

    class Meta:
        model = RestaurantLanding
        fields = [
            'id', 'restaurant', 'restaurant_name', 'restaurant_slug', 'logo', 'hero_title', 'hero_subtitle',
            'hero_image', 'hero_video', 'cta_text', 'cta_link',
            'about_heading', 'about_italic', 'about_bullet_1', 'about_bullet_2', 'about_bullet_3', 'about_body',
            'about_image', 'address', 'phone', 'email', 'opening_hours', 'seo_title', 'seo_description',
            'show_hero', 'show_about', 'show_why_us', 'show_specials', 'show_events', 'show_gallery', 'show_team', 'show_reviews',
            'why_us_title_1', 'why_us_description_1',
            'why_us_title_2', 'why_us_description_2',
            'why_us_title_3', 'why_us_description_3',
            'map_iframe_url',
            'gallery_images', 'team_members', 'reviews', 'events'
        ]


class RestaurantSettingsSerializer(serializers.ModelSerializer):
    restaurant_id = serializers.ReadOnlyField(source='restaurant.id')

    class Meta:
        model = RestaurantSettings
        fields = ['restaurant_id', 'cgst_percentage', 'sgst_percentage', 'service_charge_percentage', 'enable_online_payment', 'enable_pay_at_counter', 'enable_customer_ordering', 'razorpay_key_id']