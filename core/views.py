from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, AllowAny
from django.views.generic import TemplateView
from django.shortcuts import get_object_or_404, redirect
from django.http import Http404
from super_admin.models import Restaurant, RestaurantBranch
from django.db.models import Prefetch
from restaurant_project import constants

from core.models import Category, SubCategory, MenuItem, Order, RestaurantSettings, RestaurantLanding, BlogPost, BlogComment, Review

from core.serializers import (
    RestaurantBranchSerializer,
    CategorySerializer, 
    SubCategorySerializer,
    MenuItemSerializer, 
    OrderSerializer,
    RestaurantSerializer,
    RestaurantLandingSerializer,
    RestaurantSettingsSerializer, ZoneSerializer, TableSerializer, CustomChargeSerializer, ZoneSerializer, TableSerializer, CustomChargeSerializer,
    BlogPostSerializer,
    BlogCommentSerializer,
    ReviewSerializer,
    CustomerReviewSubmitSerializer
)


class HomeView(TemplateView):
    template_name = 'core/Customer_base.html'
    #template_name = 'core/index.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get all categories with their subcategories and menu items
        categories = Category.objects.all().prefetch_related(
            Prefetch(
                'subcategories',
                queryset=SubCategory.objects.all().prefetch_related('menu_items')
            )
        )
        
        # Process categories to include only available menu items
        processed_categories = []
        for category in categories:
            # Create a copy of the category with processed subcategories
            category_data = {
                'id': category.id,
                'name': category.name,
                'image': category.image,
                'subcategories': []
            }
            
            for subcategory in category.subcategories.all():
                # Get available menu items for this subcategory
                available_items = subcategory.menu_items.filter(availability=True).order_by('name')
                if available_items.exists():
                    category_data['subcategories'].append({
                        'id': subcategory.id,
                        'name': subcategory.name,
                        'menu_items': available_items,
                        #'image': subcategory.image
                    })
            
            # Only include categories that have subcategories with available items
            if category_data['subcategories']:
                processed_categories.append(category_data)
        
        context['categories'] = processed_categories
        
        # # Debug information
        # print("\n=== DEBUG: Categories Data ===")
        # for cat in processed_categories:
        #     print(f"\nCategory: {cat['name']} (ID: {cat['id']})")
        #     for sub in cat['subcategories']:
        #         print(f"  - Subcategory: {sub['name']} (ID: {sub['id']}): {len(sub['menu_items'])} items")
        #         for item in sub['menu_items']:
        #             print(f"    * {item.name} - ${item.price} (Available: {item.availability})")
        
        return context


class RestaurantMenuView(TemplateView):
    template_name = 'core/Customer_base.html'

    def get_context_data(self, **kwargs):
        if not constants.SHOW_WEBSITE_PAGES:
            raise Http404("Website is currently disabled.")
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug, is_active=True, feature_website_pages=True)

        categories = Category.objects.filter(restaurant=restaurant).prefetch_related(
            Prefetch(
                'subcategories',
                queryset=SubCategory.objects.filter(category__restaurant=restaurant).prefetch_related('menu_items')
            )
        )

        processed_categories = []
        for category in categories:
            category_data = {
                'id': category.id,
                'name': category.name,
                'image': category.image,
                'subcategories': []
            }
            for subcategory in category.subcategories.all():
                available_items = subcategory.menu_items.filter(availability=True, restaurant__isnull=False, restaurant__id=restaurant.id).order_by('name')
                if available_items.exists():
                    category_data['subcategories'].append({
                        'id': subcategory.id,
                        'name': subcategory.name,
                        'menu_items': available_items,
                    })
            if category_data['subcategories']:
                processed_categories.append(category_data)

        context['categories'] = processed_categories
        context['restaurant'] = restaurant
        return context


class RestaurantCartView(TemplateView):
    template_name = 'core/cart.html'

    def get_context_data(self, **kwargs):
        if not constants.SHOW_WEBSITE_PAGES:
            raise Http404("Website is currently disabled.")
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = Restaurant.objects.filter(slug=restaurant_slug, is_active=True, feature_website_pages=True).first()
        branch = None
        if not restaurant:
            branch = RestaurantBranch.objects.filter(slug=restaurant_slug).first()
            if branch:
                restaurant = branch.restaurant
        if not restaurant:
            restaurant = Restaurant.objects.first()

        settings_obj = RestaurantSettings.get_settings(restaurant)
        active_b = branch or (restaurant.branches.first() if restaurant else None)
        if settings_obj and settings_obj.cgst_percentage is not None:
            cgst_p = Decimal(str(settings_obj.cgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                cgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                cgst_p = Decimal('0.00')
            else:
                cgst_p = Decimal('2.50')
        else:
            cgst_p = Decimal('2.50')

        if settings_obj and settings_obj.sgst_percentage is not None:
            sgst_p = Decimal(str(settings_obj.sgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                sgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                sgst_p = Decimal('0.00')
            else:
                sgst_p = Decimal('2.50')
        else:
            sgst_p = Decimal('2.50')

        context.update({
            'cgst_percentage': cgst_p,
            'sgst_percentage': sgst_p,
            'service_percentage': settings_obj.service_charge_percentage if settings_obj else Decimal('0.00'),
            'restaurant': restaurant,
            'branch': active_b,
        })
        return context


class CartView(TemplateView):
    template_name = 'core/cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
        branch = None
        if not restaurant:
            branch = RestaurantBranch.objects.filter(slug=restaurant_slug).first()
            if branch:
                restaurant = branch.restaurant
        if not restaurant:
            restaurant = Restaurant.objects.first()

        settings_obj = RestaurantSettings.get_settings(restaurant)
        active_b = branch or (restaurant.branches.first() if restaurant else None)
        if settings_obj and settings_obj.cgst_percentage is not None:
            cgst_p = Decimal(str(settings_obj.cgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                cgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                cgst_p = Decimal('0.00')
            else:
                cgst_p = Decimal('2.50')
        else:
            cgst_p = Decimal('2.50')

        if settings_obj and settings_obj.sgst_percentage is not None:
            sgst_p = Decimal(str(settings_obj.sgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                sgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                sgst_p = Decimal('0.00')
            else:
                sgst_p = Decimal('2.50')
        else:
            sgst_p = Decimal('2.50')

        context.update({
            'cgst_percentage': cgst_p,
            'sgst_percentage': sgst_p,
            'service_percentage': settings_obj.service_charge_percentage if settings_obj else Decimal('0.00'),
            'restaurant': restaurant,
            'branch': active_b,
        })
        return context


class OrdersListView(TemplateView):
    template_name = 'core/orders_list.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        if restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
            if not restaurant:
                b = RestaurantBranch.objects.filter(slug=restaurant_slug).first()
                restaurant = b.restaurant if b else None
            context['restaurant'] = restaurant
        return context


class OrderDetailView(TemplateView):
    template_name = 'core/order_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
        branch = None
        if not restaurant:
            branch = RestaurantBranch.objects.filter(slug=restaurant_slug).first()
            if branch:
                restaurant = branch.restaurant
        if not restaurant:
            restaurant = Restaurant.objects.first()

        settings_obj = RestaurantSettings.get_settings(restaurant)
        active_b = branch or (restaurant.branches.first() if restaurant else None)
        if settings_obj and settings_obj.cgst_percentage is not None:
            cgst_p = Decimal(str(settings_obj.cgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                cgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                cgst_p = Decimal('0.00')
            else:
                cgst_p = Decimal('2.50')
        else:
            cgst_p = Decimal('2.50')

        if settings_obj and settings_obj.sgst_percentage is not None:
            sgst_p = Decimal(str(settings_obj.sgst_percentage))
        elif active_b:
            if active_b.gst_registration_type == 'R18' or active_b.is_hotel_tariff_7500_plus:
                sgst_p = Decimal('9.00')
            elif active_b.gst_registration_type in ['C', 'U']:
                sgst_p = Decimal('0.00')
            else:
                sgst_p = Decimal('2.50')
        else:
            sgst_p = Decimal('2.50')

        context.update({
            'cgst_percentage': cgst_p,
            'sgst_percentage': sgst_p,
            'service_percentage': settings_obj.service_charge_percentage if settings_obj else Decimal('0.00'),
            'restaurant': restaurant,
            'branch': active_b,
        })
        return context


class OrderConfirmView(TemplateView):
    template_name = 'core/order_confirm.html'




class ReserveEntryView(TemplateView):
    def get(self, request, *args, **kwargs):
        if not constants.SHOW_WEBSITE_PAGES:
            raise Http404("Website is currently disabled.")
        slug = request.GET.get('restaurant_slug') or request.GET.get('slug')
        if slug:
            return redirect('restaurant_reserve', restaurant_slug=slug)
        restaurant = Restaurant.objects.filter(is_active=True, feature_website_pages=True).order_by('id').first()
        if restaurant:
            return redirect('restaurant_reserve', restaurant_slug=restaurant.slug)
        return redirect('home')
    

class ReservationPageView(TemplateView):
    template_name = 'core/reserve.html'

    def get_context_data(self, **kwargs):
        if not constants.SHOW_WEBSITE_PAGES:
            raise Http404("Website is currently disabled.")
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        if restaurant_slug:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug, is_active=True, feature_website_pages=True)
            context['restaurant'] = restaurant
        return context
class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return Category.objects.none()
        qs = super().get_queryset().filter(restaurant__is_active=True, restaurant__feature_website_pages=True)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        branch_id = self.request.query_params.get('branch_id')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if branch_slug:
            from django.db.models import Q
            qs = qs.filter(Q(branch__slug=branch_slug) | Q(branch__isnull=True))
        elif branch_id:
            from django.db.models import Q
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        return qs


class SubCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SubCategory.objects.all()
    serializer_class = SubCategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return SubCategory.objects.none()
        queryset = super().get_queryset().filter(category__restaurant__is_active=True, category__restaurant__feature_website_pages=True)
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category_id=category)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_id:
            queryset = queryset.filter(category__restaurant_id=restaurant_id)
        elif restaurant_slug:
            queryset = queryset.filter(category__restaurant__slug=restaurant_slug)
        return queryset


class MenuItemViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MenuItemSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return MenuItem.objects.none()
        from django.db.models import Q
        queryset = MenuItem.objects.filter(
            availability=True,
            restaurant__is_active=True,
            restaurant__feature_website_pages=True,
            publishing_channels__contains=['qr']
        ).select_related('category', 'subcategory', 'subcategory__category')

        subcategory = self.request.query_params.get('subcategory')
        if subcategory:
            queryset = queryset.filter(subcategory_id=subcategory)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        branch_id = self.request.query_params.get('branch_id')
        if restaurant_id:
            queryset = queryset.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            queryset = queryset.filter(restaurant__slug=restaurant_slug)
        if branch_slug:
            queryset = queryset.filter(Q(branch__slug=branch_slug) | Q(branch__isnull=True))
        elif branch_id:
            queryset = queryset.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        return queryset.order_by('name')
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'create', 'validate_coupon', 'verify_payment', 'cancel_unpaid', 'initiate_razorpay', 'toggle_service_charge']:
            return [AllowAny()]
        return [IsAdminUser()]

    @method_decorator(csrf_exempt)
    @action(detail=False, methods=['post'], url_path='initiate-razorpay', permission_classes=[AllowAny], authentication_classes=[])
    def initiate_razorpay(self, request):
        from decimal import Decimal
        import json, urllib.request, base64
        # pyrefly: ignore [missing-import]
        from .models import Coupon, MenuItem, RestaurantSettings, Order, OrderItem
        from super_admin.models import Restaurant, RestaurantBranch

        data = request.data if isinstance(request.data, dict) else {}
        items_data = data.get('items') or []
        coupon_code = (data.get('coupon_code') or '').strip().upper()
        restaurant_id = data.get('restaurant_id')
        restaurant_slug = data.get('restaurant_slug')
        order_type = data.get('order_type') or 'takeaway'

        if not items_data:
            return Response({'error': 'No menu items provided'}, status=status.HTTP_400_BAD_REQUEST)

        restaurant = None
        mock_items = []
        for item in items_data:
            item_id = item.get('menu_item')
            qty = int(item.get('quantity') or 1)
            menu_item = MenuItem.objects.filter(id=item_id).first()
            if not menu_item:
                return Response({'error': f'Item ID {item_id} not found'}, status=status.HTTP_400_BAD_REQUEST)
            channels = getattr(menu_item, 'publishing_channels', []) or []
            if channels and 'qr' not in channels and 'online' not in channels:
                return Response({'error': f'{menu_item.name} is available for in-store POS billing only.'}, status=status.HTTP_400_BAD_REQUEST)
            if getattr(menu_item, 'is_alcoholic', False):
                has_valid_table = bool(data.get('table_token') and (data.get('table_id') or data.get('table_number')))
                if order_type != 'dine_in' or not has_valid_table:
                    return Response({
                        'error': f"'{menu_item.name}' contains alcohol and can only be ordered for Dine-In via a verified table QR scan."
                    }, status=status.HTTP_400_BAD_REQUEST)
            if restaurant is None:
                restaurant = menu_item.restaurant
            elif restaurant.id != menu_item.restaurant_id:
                return Response({'error': 'All items must belong to the same restaurant'}, status=status.HTTP_400_BAD_REQUEST)

            raw_unit_price = item.get('unit_price')
            variant_name = str(item.get('variant_name') or '').strip().lower()
            resolved_unit_price = Decimal(str(menu_item.price))

            v_data = getattr(menu_item, 'variants_json', []) or []
            if isinstance(v_data, str):
                try:
                    v_data = json.loads(v_data)
                except Exception:
                    v_data = []

            if isinstance(v_data, list) and v_data:
                matched_v = None
                if variant_name:
                    for v in v_data:
                        if str(v.get('name', '')).strip().lower() == variant_name:
                            matched_v = v
                            break
                if matched_v and matched_v.get('price') is not None:
                    resolved_unit_price = Decimal(str(matched_v['price']))
                elif raw_unit_price is not None:
                    try:
                        rup = Decimal(str(raw_unit_price))
                        variant_prices = [Decimal(str(v.get('price'))) for v in v_data if v.get('price') is not None]
                        if rup in variant_prices:
                            resolved_unit_price = rup
                    except Exception:
                        pass
            
            mock_items.append(
                OrderItem(
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=resolved_unit_price
                )
            )

        if not restaurant and restaurant_id:
            restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        elif not restaurant and restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()

        settings_obj = RestaurantSettings.get_settings(restaurant)
        if not settings_obj.enable_online_payment or not settings_obj.razorpay_key_id or not settings_obj.razorpay_key_secret:
            return Response({'error': 'Online payment is not enabled for this restaurant.'}, status=status.HTTP_400_BAD_REQUEST)

        from django.db.models import Q
        branch_id = data.get('branch_id') or data.get('branch')
        branch_slug = data.get('branch_slug')
        branch = None
        if branch_id and restaurant:
            branch = RestaurantBranch.objects.filter(id=branch_id, restaurant=restaurant).first()
        elif branch_slug and restaurant:
            branch = RestaurantBranch.objects.filter(Q(slug__iexact=branch_slug) | Q(name__iexact=branch_slug), restaurant=restaurant).first()
        if not branch and restaurant and restaurant.branches.count() == 1:
            branch = restaurant.branches.first()

        if branch and getattr(branch, 'enforce_operating_hours', False):
            op_status = branch.get_operating_status()
            if not op_status.get('is_open'):
                next_info = f" {op_status.get('next_open_label')}." if op_status.get('next_open_label') else ""
                return Response(
                    {'error': f"Ordering is currently closed for {branch.name}.{next_info}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Coupon lookup
        coupon = None
        if coupon_code:
            coupon_qs = Coupon.objects.filter(code__iexact=coupon_code)
            coupon = coupon_qs.filter(restaurant=restaurant).first() or coupon_qs.filter(restaurant__isnull=True).first()

        temp_order = Order(
            restaurant=restaurant,
            branch=branch,
            order_type=order_type,
            coupon=coupon
        )
        if 'waive_service_charge' in data:
            temp_order.waive_service_charge = bool(data.get('waive_service_charge'))

        breakdown = temp_order.calculate_bill_breakdown(items_override=mock_items)
        total_amount = breakdown['final_total']
        discount = breakdown['discount_amount']
        subtotal = breakdown['gross_subtotal']
        amount_in_paise = int(round(total_amount * 100))

        # Call Razorpay API to generate razorpay_order_id without inserting an Order in DB
        url = "https://api.razorpay.com/v1/orders"
        payload = {
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": f"rcpt_{int(subtotal)}"
        }
        req = urllib.request.Request(url, method='POST')
        req.add_header('Content-Type', 'application/json')
        auth_str = f"{settings_obj.razorpay_key_id}:{settings_obj.razorpay_key_secret}"
        auth_b64 = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
        req.add_header("Authorization", f"Basic {auth_b64}")

        try:
            req_data = json.dumps(payload).encode('utf-8')
            with urllib.request.urlopen(req, data=req_data, timeout=10) as response:
                if response.status in [200, 201]:
                    resp_data = json.loads(response.read().decode('utf-8'))
                    return Response({
                        'razorpay_order_id': resp_data.get('id'),
                        'key_id': settings_obj.razorpay_key_id,
                        'amount_paise': amount_in_paise,
                        'total_amount': str(total_amount),
                        'discount_amount': str(discount),
                        'bill_breakdown': breakdown,
                    })
                else:
                    return Response({'error': 'Failed to create Razorpay payment order'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Razorpay gateway error: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_staff:
            if self.action == 'list':
                qs = qs.exclude(payment_method='O', payment_status__in=['U', 'F'])
                ids_param = self.request.query_params.get('ids')
                if ids_param:
                    try:
                        ids = [int(x) for x in ids_param.split(',')]
                        qs = qs.filter(id__in=ids)
                    except ValueError:
                        return qs.none()
                else:
                    return qs.none()
            
            restaurant_slug = self.request.query_params.get('restaurant_slug')
            if restaurant_slug:
                qs = qs.filter(restaurant__slug=restaurant_slug)
        else:
            restaurant_id = self.request.query_params.get('restaurant')
            restaurant_slug = self.request.query_params.get('restaurant_slug')
            if restaurant_id:
                qs = qs.filter(restaurant_id=restaurant_id)
            elif restaurant_slug:
                qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs.order_by('-order_time')

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs.get('pk')
        try:
            int(lookup_value)
            filter_kwargs = {'pk': lookup_value}
        except ValueError:
            filter_kwargs = {'security_token': lookup_value}
        from django.shortcuts import get_object_or_404
        obj = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if not request.user.is_staff:
            lookup_value = self.kwargs.get('pk')
            if lookup_value != instance.security_token:
                token = request.query_params.get('token')
                if not token or instance.security_token != token:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have permission to view this order.")
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def receipt(self, request, pk=None):
        order = self.get_object()
        if not request.user.is_staff:
            lookup_value = self.kwargs.get('pk')
            if lookup_value != order.security_token:
                token = request.query_params.get('token')
                if not token or order.security_token != token:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have permission to view this order.")
        serializer = self.get_serializer(order)
        return Response(serializer.data)

    @method_decorator(csrf_exempt)
    @action(detail=True, methods=['post'], url_path='verify-payment', permission_classes=[AllowAny], authentication_classes=[])
    def verify_payment(self, request, pk=None):
        import hmac
        import hashlib
        
        order = self.get_object()
        razorpay_payment_id = request.data.get('razorpay_payment_id')
        razorpay_signature = request.data.get('razorpay_signature')
        
        if not razorpay_payment_id or not razorpay_signature:
            return Response({'error': 'Missing razorpay_payment_id or razorpay_signature'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Retrieve settings for signature verification
        # pyrefly: ignore [missing-import]
        from .models import RestaurantSettings
        settings_obj = RestaurantSettings.get_settings(order.restaurant)
        key_secret = settings_obj.razorpay_key_secret or ''
        razorpay_order_id = order.razorpay_order_id or ''
        
        # Generate signature
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        generated_signature = hmac.new(
            key_secret.encode('utf-8'),
            msg.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        if hmac.compare_digest(generated_signature, razorpay_signature):
            order.payment_status = 'paid' # Paid
            order.payment_method = 'O' # Online
            order.razorpay_payment_id = razorpay_payment_id
            order.razorpay_signature = razorpay_signature
            order.save(update_fields=['payment_status', 'payment_method', 'razorpay_payment_id', 'razorpay_signature'])
            
            # Publish to SSE notifications
            try:
                # pyrefly: ignore [missing-import]
                from .sse import publish
                if order.restaurant and order.restaurant.slug:
                    publish(order.restaurant.slug, {'type': 'new_order', 'order_id': order.id})
            except Exception:
                pass
            
            return Response({'success': True, 'message': 'Payment verified successfully.'})
        else:
            order.payment_status = 'F' # Failed
            order.save(update_fields=['payment_status'])
    @method_decorator(csrf_exempt)
    @action(detail=True, methods=['post', 'patch'], url_path='settle', permission_classes=[AllowAny], authentication_classes=[])
    def settle(self, request, pk=None):
        order = self.get_object()

        if not request.user.is_staff:
            lookup_value = self.kwargs.get('pk')
            if lookup_value != order.security_token:
                token = request.query_params.get('token') or (request.data.get('token') if isinstance(request.data, dict) else None)
                if not token or order.security_token != token:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have permission to modify this order.")

        data = request.data if isinstance(request.data, dict) else {}
        payment_method = data.get('payment_method') or request.POST.get('payment_method') or 'cash'

        order.payment_status = 'paid'
        order.payment_method = payment_method
        if order.status == Order.OrderStatus.PENDING:
            order.status = Order.OrderStatus.SERVED

        if not order.party and order.restaurant:
            order.resolve_and_sync_party()

        order.save()
        return Response({
            'success': True,
            'message': 'Order payment settled successfully.',
            'order_id': order.id,
            'payment_status': order.payment_status,
            'payment_method': order.payment_method
        })

    @method_decorator(csrf_exempt)
    @action(detail=True, methods=['post'], url_path='cancel-unpaid', permission_classes=[AllowAny], authentication_classes=[])
    def cancel_unpaid(self, request, pk=None):
        order = self.get_object()
        if not request.user.is_staff:
            lookup_value = self.kwargs.get('pk')
            if lookup_value != order.security_token:
                token = request.query_params.get('token') or (request.data.get('token') if isinstance(request.data, dict) else None)
                if not token or order.security_token != token:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have permission to modify this order.")

        if order.payment_method == 'O' and order.payment_status in ['U', 'F']:
            if order.coupon:
                if (order.coupon.used_count or 0) > 0:
                    order.coupon.used_count = max(0, (order.coupon.used_count or 1) - 1)
                    order.coupon.save(update_fields=['used_count'])
                order.coupon = None
            order.delete()
            return Response({'success': True, 'message': 'Unpaid online order attempt deleted and coupon usage restored.'})
        return Response({'success': False, 'message': 'Order is not an unpaid online order.'})

    @method_decorator(csrf_exempt)
    @action(detail=False, methods=['get', 'post'], url_path='validate-coupon', permission_classes=[AllowAny], authentication_classes=[])
    def validate_coupon(self, request):
        from decimal import Decimal
        # pyrefly: ignore [missing-import]
        from .models import Coupon
        from super_admin.models import Restaurant
        data = request.data if isinstance(request.data, dict) else {}
        code = (data.get('code') or request.query_params.get('code') or '').strip().upper()
        subtotal_val = data.get('subtotal') or request.query_params.get('subtotal')
        try:
            subtotal = Decimal(str(subtotal_val or '0'))
        except Exception:
            subtotal = Decimal('0')
        restaurant_id = data.get('restaurant_id') or request.query_params.get('restaurant_id')
        restaurant_slug = data.get('restaurant_slug') or request.query_params.get('restaurant_slug')
        restaurant = None
        if restaurant_id:
            restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        elif restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()

        if not code:
            return Response({'valid': False, 'message': 'Coupon code required'})

        coupon_qs = Coupon.objects.filter(code__iexact=code)
        if restaurant:
            coupon = coupon_qs.filter(restaurant=restaurant).first() or coupon_qs.filter(restaurant__isnull=True).first()
        else:
            coupon = coupon_qs.first()

        if not coupon:
            return Response({'valid': False, 'message': 'Invalid coupon code'})

        # Specific validations for clearer messaging
        if not coupon.is_active_now():
            return Response({'valid': False, 'message': 'Coupon inactive or expired'})
        if restaurant and coupon.restaurant_id and coupon.restaurant_id != getattr(restaurant, 'id', None):
            return Response({'valid': False, 'message': 'Coupon not valid for this restaurant'})
        if subtotal < (coupon.min_spend or Decimal('0')):
            return Response({'valid': False, 'message': f'Minimum spend is ₹{coupon.min_spend}'})

        discount = coupon.calculate_discount(subtotal)
        return Response({'valid': True, 'discount': str(discount), 'code': coupon.code})

    @method_decorator(csrf_exempt)
    @action(detail=True, methods=['post'], url_path='toggle-service-charge', permission_classes=[AllowAny], authentication_classes=[])
    def toggle_service_charge(self, request, pk=None):
        order = self.get_object()
        data = request.data if isinstance(request.data, dict) else {}
        if 'waive' in data:
            order.waive_service_charge = bool(data.get('waive'))
        else:
            order.waive_service_charge = not order.waive_service_charge
        order.save(update_fields=['waive_service_charge'])
        order.update_total()

        serializer = self.get_serializer(order)
        return Response({
            'success': True,
            'waive_service_charge': order.waive_service_charge,
            'total_amount': str(order.total_amount),
            'order': serializer.data
        })


class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RestaurantSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return Restaurant.objects.none()
        return Restaurant.objects.filter(is_active=True, feature_website_pages=True)


class RestaurantLandingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RestaurantLanding.objects.all()
    serializer_class = RestaurantLandingSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return RestaurantLanding.objects.none()
        qs = super().get_queryset().filter(
            restaurant__is_active=True,
            restaurant__feature_website_pages=True
        ).select_related('restaurant').prefetch_related(
            'gallery_images',
            'team_members',
            'reviews',
            'events',
            'why_choose_us_items',
            'restaurant__branches',
            'restaurant__branches__gst_registration'
        )
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs


class RestaurantSettingsViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RestaurantSettings.objects.all()
    serializer_class = RestaurantSettingsSerializer
    permission_classes = [AllowAny]

    def list(self, request, *args, **kwargs):
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch') or self.request.COOKIES.get('active_branch_slug')
        restaurant = None
        if restaurant_id:
            restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        elif restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
        if not restaurant:
            restaurant = Restaurant.objects.first()

        if not constants.SHOW_WEBSITE_PAGES or (restaurant and (not restaurant.is_active or not getattr(restaurant, 'feature_website_pages', True))):
            return Response({
                'is_active': False,
                'operating_status': {
                    'is_open': False,
                    'status': 'closed',
                    'status_label': 'Website Inactive',
                    'message': 'Website is currently disabled.'
                },
                'enable_customer_ordering': False,
                'enable_online_payment': False,
                'enable_pay_at_counter': False,
                'custom_charges': [],
            })
        
        obj = RestaurantSettings.get_settings(restaurant)
        data = self.get_serializer(obj).data
        
        branch = None
        if restaurant and branch_slug:
            from django.db.models import Q
            branch = restaurant.branches.filter(Q(slug__iexact=branch_slug) | Q(name__iexact=branch_slug)).first()
            if not branch and str(branch_slug).isdigit():
                branch = restaurant.branches.filter(id=int(branch_slug)).first()
        if not branch and restaurant and restaurant.branches.count() == 1:
            branch = restaurant.branches.first()

        if branch:
            data['enable_customer_ordering'] = getattr(branch, 'enable_customer_ordering', True)
            data['enable_online_payment'] = getattr(branch, 'enable_online_payment', False)
            data['enable_pay_at_counter'] = getattr(branch, 'enable_pay_at_counter', True)
            data['enforce_operating_hours'] = getattr(branch, 'enforce_operating_hours', False)
            data['opening_time'] = branch.opening_time.strftime("%H:%M") if branch.opening_time else None
            data['closing_time'] = branch.closing_time.strftime("%H:%M") if branch.closing_time else None
            data['closed_message'] = branch.closed_message
            data['operating_status'] = branch.get_operating_status()
            if branch.service_charge_percentage is not None:
                data['service_charge_percentage'] = str(branch.service_charge_percentage)

        branch_target = branch or (restaurant.branches.first() if restaurant else None)
        data['serves_alcohol'] = bool(branch_target and branch_target.serves_alcohol)
        data['service_charge_taxable'] = getattr(obj, 'service_charge_taxable', True)
        data['coupon_applies_before_tax'] = getattr(obj, 'coupon_applies_before_tax', True)

        if obj and obj.cgst_percentage is not None:
            data['cgst_percentage'] = str(obj.cgst_percentage)
        elif branch_target:
            if branch_target.gst_registration_type == 'R18' or branch_target.is_hotel_tariff_7500_plus:
                data['cgst_percentage'] = '9.00'
            elif branch_target.gst_registration_type in ['C', 'U']:
                data['cgst_percentage'] = '0.00'
            else:
                data['cgst_percentage'] = '2.50'
        else:
            data['cgst_percentage'] = '2.50'

        if obj and obj.sgst_percentage is not None:
            data['sgst_percentage'] = str(obj.sgst_percentage)
        elif branch_target:
            if branch_target.gst_registration_type == 'R18' or branch_target.is_hotel_tariff_7500_plus:
                data['sgst_percentage'] = '9.00'
            elif branch_target.gst_registration_type in ['C', 'U']:
                data['sgst_percentage'] = '0.00'
            else:
                data['sgst_percentage'] = '2.50'
        else:
            data['sgst_percentage'] = '2.50'

        data['vat_beer_percentage'] = str(branch_target.vat_beer_rate if branch_target and getattr(branch_target, 'vat_beer_rate', None) is not None else 60.00)
        data['vat_imfl_percentage'] = str(branch_target.vat_imfl_rate if branch_target and getattr(branch_target, 'vat_imfl_rate', None) is not None else 65.00)
        data['vat_wine_percentage'] = str(branch_target.vat_wine_rate if branch_target and getattr(branch_target, 'vat_wine_rate', None) is not None else 60.00)

        b_gen = getattr(branch_target, 'vat_gen_pct', None) if branch_target else None
        s_gen = getattr(obj, 'vat_gen_pct', None) or getattr(obj, 'vat_percentage', None)
        data['vat_gen_percentage'] = str(b_gen if b_gen is not None else (s_gen if s_gen is not None else 20.00))

        b_sur = getattr(branch_target, 'vat_surcharge_pct', None) if branch_target else None
        s_sur = getattr(obj, 'vat_surcharge_pct', None)
        data['vat_surcharge_percentage'] = str(b_sur if b_sur is not None else (s_sur if s_sur is not None else 0.00))

        b_sur_name = getattr(branch_target, 'vat_surcharge_name', None) if branch_target else None
        s_sur_name = getattr(obj, 'vat_surcharge_name', None)
        data['vat_surcharge_name'] = (b_sur_name or s_sur_name or 'VAT Surcharge').strip() or 'VAT Surcharge'

        # Dynamic Custom Charges & Dining Zones
        if restaurant:
            from django.db.models import Q
            from core.models import CustomCharge, Zone, Table
            charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True)
            if branch:
                charges_qs = charges_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                charges_qs = charges_qs.filter(branch__isnull=True)
            data['custom_charges'] = [
                {
                    'id': cc.id,
                    'name': cc.charge_name,
                    'charge_name': cc.charge_name,
                    'type': cc.charge_type,
                    'charge_type': cc.charge_type,
                    'value': float(cc.value),
                    'is_taxable': cc.is_taxable,
                    'applied_order_types': cc.applied_order_types or ['dine_in', 'takeaway', 'delivery'],
                    'is_auto_applied': cc.is_auto_applied,
                    'branch_id': cc.branch_id,
                    'branch_slug': cc.branch.slug if cc.branch else None,
                }
                for cc in charges_qs
            ]

            zones_qs = Zone.objects.filter(restaurant=restaurant)
            if branch:
                zones_qs = zones_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                zones_qs = zones_qs.filter(branch__isnull=True)
            
            zones_data = []
            for z in zones_qs.order_by('name'):
                tables_qs = z.tables.filter(is_active=True)
                if branch:
                    tables_qs = tables_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                tbls = [
                    {
                        'id': t.id,
                        'name': t.name,
                        'table_number': t.table_number,
                        'capacity': t.capacity,
                        'zone': z.name,
                        'is_placed': t.is_placed,
                        'status': t.current_status,
                    }
                    for t in tables_qs.order_by('table_number')
                ]
                if tbls:
                    zones_data.append({
                        'id': z.id,
                        'name': z.name,
                        'tables': tbls,
                    })
            
            unassigned_tables = Table.objects.filter(restaurant=restaurant, zone__isnull=True, is_active=True)
            if branch:
                unassigned_tables = unassigned_tables.filter(Q(branch=branch) | Q(branch__isnull=True))
            if unassigned_tables.exists():
                zones_data.append({
                    'id': 0,
                    'name': 'Main Area',
                    'tables': [
                        {
                            'id': t.id,
                            'name': t.name,
                            'table_number': t.table_number,
                            'capacity': t.capacity,
                            'zone': 'Main Area',
                            'is_placed': t.is_placed,
                            'status': t.current_status,
                        }
                        for t in unassigned_tables.order_by('table_number')
                    ]
                })

            data['zones'] = zones_data

        return Response(data)


class BlogPostViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BlogPost.objects.filter(is_published=True)
    serializer_class = BlogPostSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return BlogPost.objects.none()
        qs = super().get_queryset().filter(restaurant__is_active=True, restaurant__feature_website_pages=True)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs

    def get_throttles(self):
        if self.action == 'add_comment':
            from core.throttling import BlogCommentAnonRateThrottle, BlogCommentDayRateThrottle
            return [BlogCommentAnonRateThrottle(), BlogCommentDayRateThrottle()]
        return super().get_throttles()

    @action(detail=True, methods=['post'], permission_classes=[AllowAny])
    def add_comment(self, request, slug=None):
        post = self.get_object()
        author_name = request.data.get('author_name')
        author_email = request.data.get('author_email', '')
        content = request.data.get('content')

        if not author_name or not content:
            return Response({'error': 'Name and comment text are required.'}, status=status.HTTP_400_BAD_REQUEST)

        comment = BlogComment.objects.create(
            post=post,
            author_name=author_name,
            author_email=author_email,
            content=content,
            is_approved=False
        )
        serializer = BlogCommentSerializer(comment)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class RestaurantBranchViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RestaurantBranch.objects.all()
    serializer_class = RestaurantBranchSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return RestaurantBranch.objects.none()
        qs = super().get_queryset().filter(restaurant__is_active=True, restaurant__feature_website_pages=True)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug') or self.request.query_params.get('slug')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs.order_by('id')


from super_admin.models import CustomerInquiry
from core.serializers import CustomerInquirySerializer
from core.throttling import (
    ContactAnonRateThrottle, ContactDayRateThrottle,
    NewsletterAnonRateThrottle, NewsletterDayRateThrottle,
    JobApplicationAnonRateThrottle, JobApplicationDayRateThrottle,
    BlogCommentAnonRateThrottle, BlogCommentDayRateThrottle,
    ReviewAnonRateThrottle, ReviewDayRateThrottle,
)


class CustomerInquiryViewSet(viewsets.ModelViewSet):
    queryset = CustomerInquiry.objects.all()
    serializer_class = CustomerInquirySerializer
    throttle_classes = [ContactAnonRateThrottle, ContactDayRateThrottle]

    def get_permissions(self):
        if self.action == 'create':
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset()
        rslug = self.request.query_params.get('restaurant_slug')
        if rslug:
            qs = qs.filter(restaurant__slug=rslug)
        return qs.order_by('-created_at')


from super_admin.models import NewsletterSubscriber
from core.serializers import NewsletterSubscriberSerializer


class NewsletterSubscriberViewSet(viewsets.ModelViewSet):
    queryset = NewsletterSubscriber.objects.all()
    serializer_class = NewsletterSubscriberSerializer
    throttle_classes = [NewsletterAnonRateThrottle, NewsletterDayRateThrottle]

    def get_permissions(self):
        if self.action in ['create', 'subscribe']:
            return [AllowAny()]
        return [IsAdminUser()]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        was_created = getattr(serializer, '_was_created', True)
        if was_created:
            return Response({
                'status': 'success',
                'message': 'Thank you for subscribing! Check your inbox for your 25% discount code.',
                'email': instance.email
            }, status=status.HTTP_201_CREATED)
        else:
            return Response({
                'status': 'already_subscribed',
                'message': 'You are already subscribed to our newsletter!',
                'email': instance.email
            }, status=status.HTTP_200_OK)


class ZoneViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ZoneSerializer
    permission_classes = [AllowAny]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        if branch_slug:
            from super_admin.models import RestaurantBranch
            ctx['branch'] = RestaurantBranch.objects.filter(slug=branch_slug).first()
        return ctx

    def get_queryset(self):
        from core.models import Zone
        qs = Zone.objects.all()
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if branch_slug:
            from django.db.models import Q
            qs = qs.filter(Q(branch__slug=branch_slug) | Q(branch__isnull=True))
        return qs.order_by('name')


class TableViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        from core.models import Table
        qs = Table.objects.filter(is_active=True)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        zone_param = self.request.query_params.get('zone')
        zone_id = self.request.query_params.get('zone_id')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if branch_slug:
            from django.db.models import Q
            qs = qs.filter(Q(branch__slug=branch_slug) | Q(branch__isnull=True))
        if zone_id:
            qs = qs.filter(zone_id=zone_id)
        elif zone_param:
            qs = qs.filter(zone__name__iexact=zone_param)
        return qs.order_by('table_number')


class CustomChargeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CustomChargeSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        from core.models import CustomCharge
        qs = CustomCharge.objects.filter(is_active=True)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('branch')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if branch_slug:
            from django.db.models import Q
            qs = qs.filter(Q(branch__slug__iexact=branch_slug) | Q(branch__name__iexact=branch_slug) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
        return qs.order_by('charge_name')


from super_admin.models import JobPosting, JobApplication
from core.serializers import JobPostingSerializer, JobApplicationSerializer


class JobPostingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = JobPosting.objects.filter(is_active=True)
    serializer_class = JobPostingSerializer
    permission_classes = [AllowAny]
    lookup_field = 'pk'

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return JobPosting.objects.none()
        from django.db.models import Q
        qs = JobPosting.objects.filter(is_active=True, restaurant__is_active=True, restaurant__feature_website_pages=True).select_related('restaurant', 'branch')
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_id = self.request.query_params.get('branch')
        branch_slug = self.request.query_params.get('branch_slug')
        department = self.request.query_params.get('department')
        job_type = self.request.query_params.get('job_type')

        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)

        if branch_id:
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        elif branch_slug:
            qs = qs.filter(Q(branch__slug=branch_slug) | Q(branch__isnull=True))

        if department:
            qs = qs.filter(department__iexact=department)

        if job_type:
            qs = qs.filter(job_type__iexact=job_type)

        return qs.order_by('display_order', '-created_at')


class JobApplicationViewSet(viewsets.ModelViewSet):
    queryset = JobApplication.objects.all()
    serializer_class = JobApplicationSerializer
    throttle_classes = [JobApplicationAnonRateThrottle, JobApplicationDayRateThrottle]

    def get_permissions(self):
        if self.action == 'create':
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset().select_related('job', 'restaurant', 'branch')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        return Response({
            'status': 'success',
            'message': f'Thank you {instance.full_name}! Your application for {instance.job.title} has been submitted successfully.',
            'application_id': instance.id
        }, status=status.HTTP_201_CREATED)


class ReviewViewSet(viewsets.ModelViewSet):
    queryset = Review.objects.all().select_related('landing__restaurant', 'branch')
    serializer_class = ReviewSerializer

    def get_throttles(self):
        if self.action in ['create', 'submit']:
            return [ReviewAnonRateThrottle(), ReviewDayRateThrottle()]
        return super().get_throttles()

    def get_permissions(self):
        if self.action in ['create', 'submit']:
            return [AllowAny()]
        return [IsAdminUser()]

    def get_serializer_class(self):
        if self.action in ['create', 'submit']:
            return CustomerReviewSubmitSerializer
        return ReviewSerializer

    def get_queryset(self):
        if not constants.SHOW_WEBSITE_PAGES:
            return Review.objects.none()
        qs = super().get_queryset().filter(landing__restaurant__is_active=True, landing__restaurant__feature_website_pages=True)
        rslug = self.request.query_params.get('restaurant_slug')
        rid = self.request.query_params.get('restaurant')
        branch_id = self.request.query_params.get('branch_id')
        branch_slug = self.request.query_params.get('branch_slug')
        is_active = self.request.query_params.get('active')

        if rslug:
            qs = qs.filter(landing__restaurant__slug=rslug)
        elif rid:
            qs = qs.filter(landing__restaurant_id=rid)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        elif branch_slug:
            qs = qs.filter(branch__slug=branch_slug)

        if is_active is not None:
            if is_active.lower() in ('true', '1'):
                qs = qs.filter(active=True)
            elif is_active.lower() in ('false', '0'):
                qs = qs.filter(active=False)

        return qs.order_by('display_order', '-created_at', 'id')

    def create(self, request, *args, **kwargs):
        return self.submit(request)

    @action(detail=False, methods=['post'], url_path='submit', permission_classes=[AllowAny])
    def submit(self, request):
        serializer = CustomerReviewSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Resolve restaurant
        restaurant = None
        if data.get('restaurant_slug'):
            restaurant = Restaurant.objects.filter(slug=data['restaurant_slug']).first()
        elif data.get('restaurant_id'):
            restaurant = Restaurant.objects.filter(id=data['restaurant_id']).first()

        # Resolve branch
        branch = None
        if data.get('branch_id'):
            branch = RestaurantBranch.objects.filter(id=data['branch_id']).first()
            if branch and not restaurant:
                restaurant = branch.restaurant
        elif data.get('branch_slug'):
            if restaurant:
                branch = restaurant.branches.filter(slug=data['branch_slug']).first()
            else:
                branch = RestaurantBranch.objects.filter(slug=data['branch_slug']).first()
                if branch:
                    restaurant = branch.restaurant

        if not restaurant:
            return Response({'error': 'Valid restaurant or branch could not be resolved.'}, status=status.HTTP_400_BAD_REQUEST)

        landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)

        review = Review.objects.create(
            landing=landing,
            branch=branch,
            name=data['name'].strip(),
            stars=data.get('stars', 5),
            description=data['description'].strip(),
            email=data.get('email'),
            phone=data.get('phone'),
            table_no=data.get('table_no'),
            is_customer_submitted=True,
            active=False  # Must be approved by admin before displaying publicly
        )

        return Response({
            'status': 'success',
            'message': f'Thank you {review.name}! Your review has been received and will be published shortly.',
            'review_id': review.id
        }, status=status.HTTP_201_CREATED)


