from rest_framework import serializers
# pyrefly: ignore [missing-import]
from core.models import Category, SubCategory, MenuItem, Table, Zone, CustomCharge, Order, OrderItem, RestaurantLanding, GalleryImage, TeamMember, Review, RestaurantSettings, Event, BlogPost, BlogComment
from super_admin.models import Restaurant, RestaurantBranch
from django.utils import timezone


class RestaurantBranchSerializer(serializers.ModelSerializer):
    address = serializers.SerializerMethodField()
    gstin = serializers.SerializerMethodField()
    service_charge_percentage = serializers.SerializerMethodField()

    class Meta:
        model = RestaurantBranch
        fields = [
            'id', 'name', 'slug', 'phone', 'address_line_1', 'address_line_2',
            'city', 'state', 'pincode', 'address', 'serves_alcohol',
            'service_charge_percentage', 'fssai_license_no', 'gstin',
            'enable_customer_ordering', 'enable_online_payment', 'enable_pay_at_counter'
        ]

    def get_address(self, obj):
        addr_parts = [p for p in [obj.address_line_1, obj.address_line_2, obj.city, obj.state, obj.pincode] if p]
        if addr_parts:
            return ", ".join(addr_parts)
        return obj.restaurant.address if obj.restaurant else ""

    def get_gstin(self, obj):
        if getattr(obj, 'gst_registration', None) and getattr(obj.gst_registration, 'gstin', None):
            return obj.gst_registration.gstin
        return obj.gstin or ""

    def get_service_charge_percentage(self, obj):
        return str(obj.service_charge_percentage or 0)


class CategorySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    branch_id = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Category
        fields = ['id', 'name', 'description', 'image', 'image_url', 'branch_id']
        read_only_fields = ['image_url', 'branch_id']

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
    branch_id = serializers.ReadOnlyField(source='branch.id')
    
    class Meta:
        model = MenuItem
        fields = [
            'id', 'name', 'short_code', 'sku', 'description', 'price', 'tax_type', 'is_tax_inclusive',
            'image', 'image_url', 'availability', 'subcategory', 'category', 'branch_id',
            'food_type', 'is_jain', 'is_vegan', 'calories', 'portion_serves',
            'allergens', 'publishing_channels', 'variants_json',
            'markup_price', 'packaging_fee'
        ]
        read_only_fields = ['image_url', 'branch_id']
        
    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            request = self.context.get('request')
            if request is not None:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None
    
    def get_category(self, obj):
        try:
            if getattr(obj, 'category', None):
                return {
                    'id': obj.category.id,
                    'name': obj.category.name
                }
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
    restaurant_slug = serializers.CharField(required=False, write_only=True)
    branch_slug = serializers.CharField(required=False, write_only=True, allow_blank=True)
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    branch_name = serializers.ReadOnlyField(source='branch.name')
    branch_slug_display = serializers.ReadOnlyField(source='branch.slug')
    order_restaurant_slug = serializers.ReadOnlyField(source='restaurant.slug')
    remaining_seconds = serializers.SerializerMethodField()
    progress_pct = serializers.SerializerMethodField()
    coupon_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    applied_coupon_code = serializers.ReadOnlyField(source='coupon.code')
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    order_number = serializers.CharField(read_only=True)
    branch_order_number = serializers.IntegerField(read_only=True)
    table_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    zone = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)
    floor = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)
    table_token = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    round_off_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    bill_breakdown = serializers.SerializerMethodField()
    razorpay_order_id = serializers.CharField(write_only=True, required=False, allow_null=True, allow_blank=True)
    razorpay_payment_id = serializers.CharField(write_only=True, required=False, allow_null=True, allow_blank=True)
    razorpay_signature = serializers.CharField(write_only=True, required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'branch_order_number', 'customer_name', 'phone', 'table_number', 'table_id', 'zone', 'floor', 'table_token', 'order_time',
            'status', 'status_display', 'subtotal', 'tax_amount', 'round_off_amount', 'total_amount', 'discount_amount', 'bill_breakdown', 'notes', 'items',
            'coupon_code', 'applied_coupon_code', 'payment_method', 'payment_method_display', 'payment_status', 'payment_status_display',
            'razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature', 'restaurant_slug', 'branch_slug', 'branch_id', 'branch_name', 'branch_slug_display', 'order_restaurant_slug', 'security_token',
            'prep_minutes', 'prep_start', 'ready_at', 'remaining_seconds', 'progress_pct'
        ]
        read_only_fields = ['order_time', 'subtotal', 'tax_amount', 'round_off_amount', 'total_amount', 'discount_amount', 'security_token', 'order_number', 'branch_order_number', 'branch_name', 'branch_slug_display', 'order_restaurant_slug']

    def create(self, validated_data):
        from core.models import Coupon, RestaurantSettings
        items_data = validated_data.pop('items')
        coupon_code = validated_data.pop('coupon_code', '').strip().upper() if 'coupon_code' in validated_data else ''
        payment_method = validated_data.get('payment_method', 'C')
        restaurant_slug = validated_data.pop('restaurant_slug', None)
        branch_slug = validated_data.pop('branch_slug', None)
        branch_id = validated_data.pop('branch_id', None)

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

        if not restaurant and restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()

        branch = None
        if restaurant and branch_slug:
            from django.db.models import Q
            branch = restaurant.branches.filter(
                Q(slug__iexact=branch_slug) | Q(name__iexact=branch_slug)
            ).first()
            if not branch and str(branch_slug).isdigit():
                branch = restaurant.branches.filter(id=int(branch_slug)).first()
        elif restaurant and branch_id:
            branch = restaurant.branches.filter(id=branch_id).first()

        if not branch and resolved_items:
            for it in resolved_items:
                mi = it['menu_item']
                if getattr(mi, 'branch', None):
                    branch = mi.branch
                    break

        settings_obj = RestaurantSettings.get_settings(restaurant)
        if branch:
            if not getattr(branch, 'enable_customer_ordering', True):
                raise serializers.ValidationError(f"Digital ordering is currently disabled for {branch.name}.")
        else:
            if not settings_obj.enable_customer_ordering:
                raise serializers.ValidationError("Ordering is currently disabled for this restaurant.")

        if payment_method == 'O':
            if branch and not getattr(branch, 'enable_online_payment', False) and not settings_obj.enable_online_payment:
                raise serializers.ValidationError("Online payment is not enabled for this outlet.")
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

            validated_data['payment_status'] = 'paid'
        else:
            if branch and not getattr(branch, 'enable_pay_at_counter', True):
                raise serializers.ValidationError("Pay at counter is not enabled for this outlet. You must pay online.")
            elif not branch and not settings_obj.enable_pay_at_counter:
                raise serializers.ValidationError("Pay at counter is not enabled for this restaurant. You must pay online.")
            validated_data['payment_status'] = 'C'

        table_number = validated_data.get('table_number')
        table_id = validated_data.pop('table_id', None)
        zone_param = validated_data.pop('zone', None) or validated_data.pop('floor', None)
        table_token = validated_data.pop('table_token', None)
        table_obj = None

        if table_id and restaurant:
            t_qs = Table.objects.filter(id=table_id, restaurant=restaurant)
            if branch:
                t_qs = t_qs.filter(branch=branch)
            table_obj = t_qs.first()

        if not table_obj and table_number and restaurant:
            try:
                t_num = int(table_number)
                t_filter = {'restaurant': restaurant, 'table_number': t_num}
                if branch:
                    t_filter['branch'] = branch
                if zone_param:
                    t_filter['zone__name__iexact'] = str(zone_param).strip()

                table_obj = Table.objects.filter(**t_filter).first()

                # If no match with exact zone name, fallback without zone filter ONLY within the same branch
                if not table_obj and zone_param and branch:
                    t_filter.pop('zone__name__iexact', None)
                    table_obj = Table.objects.filter(**t_filter).first()
                elif not table_obj and not branch and zone_param:
                    table_obj = Table.objects.filter(
                        restaurant=restaurant,
                        table_number=t_num,
                        zone__name__iexact=str(zone_param).strip()
                    ).first()
            except Exception:
                pass

        # Security Verification for Table QR Orders (prevents table tampering & spoofing)
        request = self.context.get('request')
        is_staff_user = bool(request and request.user and request.user.is_staff)

        if not is_staff_user:
            if table_number and not table_obj:
                raise serializers.ValidationError({
                    "table_number": f"Table #{table_number} was not found or is inactive for this outlet."
                })
            if table_obj:
                expected_token = table_obj.get_security_token()
                provided_token = str(table_token or '').strip()
                if not provided_token or provided_token.lower() != expected_token.lower():
                    raise serializers.ValidationError({
                        "table_token": "Invalid or tampered table verification code. Please scan the QR code placed at your dining table."
                    })

        # If table_obj belongs to a branch and order branch was not yet resolved, sync it
        if table_obj and getattr(table_obj, 'branch', None) and not branch:
            branch = table_obj.branch

        # If branch is still not resolved and restaurant has a single branch, default to it
        if not branch and restaurant and restaurant.branches.count() == 1:
            branch = restaurant.branches.first()

        if table_obj:
            validated_data['table_number'] = table_obj.table_number

        if table_obj or table_number:
            validated_data['order_type'] = Order.OrderType.DINE_IN
        else:
            validated_data['order_type'] = Order.OrderType.TAKEAWAY

        order = Order.objects.create(restaurant=restaurant, branch=branch, table=table_obj, **validated_data)
        
        for item_data in resolved_items:
            menu_item = item_data['menu_item']
            OrderItem.objects.create(
                order=order,
                menu_item=menu_item,
                quantity=item_data['quantity'],
                unit_price=menu_item.price,
                notes=item_data.get('notes', '')
            )
        if coupon_code:
            coupon_qs = Coupon.objects.filter(code__iexact=coupon_code)
            if restaurant:
                coupon = coupon_qs.filter(restaurant=restaurant).first() or coupon_qs.filter(restaurant__isnull=True).first()
            else:
                coupon = coupon_qs.first()
            if coupon:
                from decimal import Decimal
                subtotal = sum((it.subtotal for it in order.items.all()), Decimal('0.00'))
                if coupon.is_valid_for(subtotal, restaurant=restaurant):
                    order.coupon = coupon
                    order.update_total()
                    if coupon.max_uses is None or coupon.used_count < (coupon.max_uses or 0) or coupon.max_uses == 0:
                        coupon.used_count = (coupon.used_count or 0) + 1
                        coupon.save(update_fields=['used_count'])
                else:
                    order.update_total()
            else:
                order.update_total()
        else:
            order.update_total()

        try:
            from core.sse import publish
            if order.restaurant and order.restaurant.slug:
                publish(order.restaurant.slug, {'type': 'new_order', 'order_id': order.id, 'branch_id': branch.id if branch else None})
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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['restaurant_slug'] = instance.restaurant.slug if instance.restaurant else None
        return data

    def get_bill_breakdown(self, obj):
        try:
            breakdown = obj.calculate_bill_breakdown()
            formatted_charges = []
            for cc in breakdown.get('custom_charges_list', []):
                formatted_charges.append({
                    'name': cc.get('name'),
                    'type': cc.get('type'),
                    'value': float(cc.get('value', 0)),
                    'amount': float(cc.get('amount', 0)),
                    'is_taxable': bool(cc.get('is_taxable', False)),
                })
            return {
                'bill_type': breakdown.get('bill_type', 'combined'),
                'gross_subtotal': float(breakdown.get('gross_subtotal', 0)),
                'food_subtotal': float(breakdown.get('food_subtotal', 0)),
                'liquor_subtotal': float(breakdown.get('liquor_subtotal', 0)),
                'discount_amount': float(breakdown.get('discount_amount', 0)),
                'custom_charges_total': float(breakdown.get('custom_charges_total', 0)),
                'custom_charges_list': formatted_charges,
                'service_charge': float(breakdown.get('service_charge', 0)),
                'service_charge_percentage': float(breakdown.get('service_charge_percentage', 0)),
                'gst_base': float(breakdown.get('gst_base', 0)),
                'cgst': float(breakdown.get('cgst', 0)),
                'cgst_percentage': float(breakdown.get('cgst_percentage', 0)),
                'sgst': float(breakdown.get('sgst', 0)),
                'sgst_percentage': float(breakdown.get('sgst_percentage', 0)),
                'beer_vat': float(breakdown.get('beer_vat', 0)),
                'imfl_vat': float(breakdown.get('imfl_vat', 0)),
                'wine_vat': float(breakdown.get('wine_vat', 0)),
                'gen_vat': float(breakdown.get('gen_vat', 0)),
                'vat': float(breakdown.get('vat', 0)),
                'round_off_amount': float(breakdown.get('round_off_amount', 0)),
                'final_total': float(breakdown.get('final_total', 0)),
            }
        except Exception:
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
    branches = RestaurantBranchSerializer(source='restaurant.branches', many=True, read_only=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')
    restaurant_slug = serializers.ReadOnlyField(source='restaurant.slug')

    class Meta:
        model = RestaurantLanding
        fields = [
            'id', 'restaurant', 'restaurant_name', 'restaurant_slug', 'logo', 'favicon', 'hero_title', 'hero_subtitle',
            'hero_image', 'hero_video', 'cta_text', 'cta_link',
            'about_heading', 'about_italic', 'about_bullet_1', 'about_bullet_2', 'about_bullet_3', 'about_body',
            'about_image',
            'about_video_url',
            'about_stat_num_1', 'about_stat_label_1', 'about_stat_desc_1',
            'about_stat_num_2', 'about_stat_label_2', 'about_stat_desc_2',
            'about_stat_num_3', 'about_stat_label_3', 'about_stat_desc_3',
            'about_stat_num_4', 'about_stat_label_4', 'about_stat_desc_4',
            'address', 'phone', 'email', 'opening_hours',
            'seo_title', 'seo_description', 'seo_keywords', 'seo_canonical_url',
            'og_title', 'og_description', 'og_url', 'og_site_name', 'og_locale', 'og_type',
            'twitter_card_type', 'twitter_title', 'twitter_description', 'pages_seo',
            'show_hero', 'show_about', 'show_why_us', 'show_specials', 'show_events', 'show_gallery', 'show_team', 'show_reviews',
            'why_us_title_1', 'why_us_description_1',
            'why_us_title_2', 'why_us_description_2',
            'why_us_title_3', 'why_us_description_3',
            'map_iframe_url',
            'gallery_images', 'team_members', 'reviews', 'events', 'branches'
        ]


class RestaurantSettingsSerializer(serializers.ModelSerializer):
    restaurant_id = serializers.ReadOnlyField(source='restaurant.id')

    class Meta:
        model = RestaurantSettings
        fields = ['restaurant_id', 'cgst_percentage', 'sgst_percentage', 'service_charge_percentage', 'enable_online_payment', 'enable_pay_at_counter', 'enable_customer_ordering', 'razorpay_key_id']


class BlogCommentSerializer(serializers.ModelSerializer):
    date_formatted = serializers.SerializerMethodField()

    class Meta:
        model = BlogComment
        fields = ['id', 'author_name', 'author_email', 'content', 'created_at', 'date_formatted']

    def get_date_formatted(self, obj):
        return obj.created_at.strftime('%I:%M %p %B %d, %Y')


class BlogPostSerializer(serializers.ModelSerializer):
    comments = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = [
            'id', 'restaurant', 'title', 'slug', 'category', 'author', 'cover_image',
            'shape_pattern', 'excerpt', 'intro_content', 'section_1_title', 'section_1_text',
            'quote', 'section_2_title', 'section_2_text', 'gallery_image_1', 'gallery_image_2',
            'tags', 'seo_title', 'seo_description', 'is_published', 'display_order',
            'created_at', 'updated_at', 'comments', 'comments_count'
        ]

    def get_comments(self, obj):
        approved_comments = obj.comments.filter(is_approved=True)
        return BlogCommentSerializer(approved_comments, many=True).data

    def get_comments_count(self, obj):
        return obj.comments.filter(is_approved=True).count()

from super_admin.models import CustomerInquiry


class CustomerInquirySerializer(serializers.ModelSerializer):
    restaurant_slug = serializers.CharField(write_only=True, required=False, allow_blank=True)
    hp_website = serializers.CharField(write_only=True, required=False, allow_blank=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')

    class Meta:
        model = CustomerInquiry
        fields = [
            'id', 'restaurant', 'restaurant_slug', 'restaurant_name',
            'name', 'email', 'phone', 'subject', 'message', 'is_read',
            'hp_website', 'created_at', 'updated_at'
        ]
        read_only_fields = ['is_read', 'created_at', 'updated_at', 'restaurant_name']

    def validate(self, attrs):
        import re
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError as DjangoValidationError

        # 1. Honeypot check
        hp = attrs.pop('hp_website', None)
        if hp and str(hp).strip():
            raise serializers.ValidationError({'detail': 'Invalid request detected.'})

        # 2. Name check
        name = (attrs.get('name') or '').strip()
        if not name:
            raise serializers.ValidationError({'name': 'Name is required.'})
        if len(name) < 2:
            raise serializers.ValidationError({'name': 'Name must be at least 2 characters.'})
        attrs['name'] = name

        # 3. Phone check
        phone = (attrs.get('phone') or '').strip()
        if phone:
            digits = re.sub(r'\D', '', phone)
            if len(digits) < 7 or len(digits) > 15:
                raise serializers.ValidationError({'phone': 'Please enter a valid phone number (7 to 15 digits).'})
            attrs['phone'] = phone

        # 4. Email check
        email = (attrs.get('email') or '').strip()
        if email:
            try:
                validate_email(email)
            except DjangoValidationError:
                raise serializers.ValidationError({'email': 'Please enter a valid email address.'})
            attrs['email'] = email

        # 5. Message check
        msg = (attrs.get('message') or '').strip()
        if not msg:
            raise serializers.ValidationError({'message': 'Message is required.'})
        attrs['message'] = msg

        # 6. Restaurant resolution
        rslug = attrs.pop('restaurant_slug', None)
        if rslug and not attrs.get('restaurant'):
            restaurant = Restaurant.objects.filter(slug=rslug).first()
            if restaurant:
                attrs['restaurant'] = restaurant

        return attrs

from super_admin.models import NewsletterSubscriber


class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    restaurant_slug = serializers.CharField(write_only=True, required=False, allow_blank=True)
    hp_website = serializers.CharField(write_only=True, required=False, allow_blank=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')

    class Meta:
        model = NewsletterSubscriber
        fields = ['id', 'restaurant', 'restaurant_slug', 'restaurant_name', 'email', 'source', 'is_active', 'hp_website', 'subscribed_at']
        read_only_fields = ['is_active', 'subscribed_at', 'restaurant_name']

    def validate(self, attrs):
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError as DjangoValidationError

        hp = attrs.pop('hp_website', None)
        if hp and str(hp).strip():
            raise serializers.ValidationError({'detail': 'Invalid request detected.'})

        email = (attrs.get('email') or '').strip().lower()
        if not email:
            raise serializers.ValidationError({'email': 'Email is required.'})
        try:
            validate_email(email)
        except DjangoValidationError:
            raise serializers.ValidationError({'email': 'Please enter a valid email address.'})
        attrs['email'] = email

        rslug = attrs.pop('restaurant_slug', None)
        if rslug and not attrs.get('restaurant'):
            restaurant = Restaurant.objects.filter(slug=rslug).first()
            if restaurant:
                attrs['restaurant'] = restaurant

        return attrs

    def create(self, validated_data):
        restaurant = validated_data.get('restaurant')
        email = validated_data.get('email')
        source = validated_data.get('source', 'web_footer')

        obj, created = NewsletterSubscriber.objects.get_or_create(
            restaurant=restaurant,
            email=email,
            defaults={'source': source, 'is_active': True}
        )
        if not created and not obj.is_active:
            obj.is_active = True
            obj.save(update_fields=['is_active'])

        self._was_created = created
        return obj


class TableSerializer(serializers.ModelSerializer):
    zone_name = serializers.ReadOnlyField(source='zone.name')
    branch_slug = serializers.ReadOnlyField(source='branch.slug')
    branch_id = serializers.ReadOnlyField(source='branch.id')
    status = serializers.ReadOnlyField(source='current_status')

    class Meta:
        model = Table
        fields = [
            'id', 'name', 'table_number', 'capacity', 'shape',
            'zone', 'zone_name', 'branch', 'branch_id', 'branch_slug',
            'is_active', 'is_placed', 'status'
        ]


class ZoneSerializer(serializers.ModelSerializer):
    branch_slug = serializers.ReadOnlyField(source='branch.slug')
    tables = serializers.SerializerMethodField()

    class Meta:
        model = Zone
        fields = ['id', 'name', 'branch', 'branch_slug', 'tables']

    def get_tables(self, obj):
        tables_qs = obj.tables.filter(is_active=True).order_by('table_number')
        branch = self.context.get('branch')
        if branch:
            from django.db.models import Q
            tables_qs = tables_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        return [
            {
                'id': t.id,
                'name': t.name,
                'table_number': t.table_number,
                'capacity': t.capacity,
                'zone': obj.name,
                'is_placed': t.is_placed,
                'status': t.current_status,
            }
            for t in tables_qs
        ]


class CustomChargeSerializer(serializers.ModelSerializer):
    branch_slug = serializers.ReadOnlyField(source='branch.slug')
    name = serializers.CharField(source='charge_name', read_only=True)
    type = serializers.CharField(source='charge_type', read_only=True)

    class Meta:
        model = CustomCharge
        fields = [
            'id', 'charge_name', 'name', 'charge_type', 'type',
            'value', 'is_taxable', 'applied_order_types', 'is_auto_applied',
            'is_active', 'branch', 'branch_slug'
        ]
