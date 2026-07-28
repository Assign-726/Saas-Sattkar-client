from rest_framework import viewsets, status
from rest_framework.decorators import action
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, AllowAny
from django.views.generic import TemplateView
from django.shortcuts import get_object_or_404, redirect
from super_admin.models import Restaurant
from django.db.models import Prefetch

# pyrefly: ignore [missing-import]
from .models import Category, SubCategory, MenuItem, Order, RestaurantSettings, RestaurantLanding
# pyrefly: ignore [missing-import]
from .serializers import (
    CategorySerializer, 
    SubCategorySerializer,
    MenuItemSerializer, 
    OrderSerializer,
    RestaurantSerializer,
    RestaurantLandingSerializer,
    RestaurantSettingsSerializer
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
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)

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
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
        settings_obj = RestaurantSettings.get_settings()
        context.update({
            'cgst_percentage': settings_obj.cgst_percentage,
            'sgst_percentage': settings_obj.sgst_percentage,
            'service_percentage': settings_obj.service_charge_percentage,
            'restaurant': restaurant,
        })
        return context


class CartView(TemplateView):
    template_name = 'core/cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = RestaurantSettings.get_settings()
        context.update({
            'cgst_percentage': settings_obj.cgst_percentage,
            'sgst_percentage': settings_obj.sgst_percentage,
            'service_percentage': settings_obj.service_charge_percentage,
        })
        return context


class OrdersListView(TemplateView):
    template_name = 'core/orders_list.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        if restaurant_slug:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
            context['restaurant'] = restaurant
        return context


class OrderDetailView(TemplateView):
    template_name = 'core/order_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = RestaurantSettings.get_settings()
        context.update({
            'cgst_percentage': settings_obj.cgst_percentage,
            'sgst_percentage': settings_obj.sgst_percentage,
            'service_percentage': settings_obj.service_charge_percentage,
        })
        return context


class OrderConfirmView(TemplateView):
    template_name = 'core/order_confirm.html'

class LandingView(TemplateView):
    template_name = 'core/landing.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        restaurant = None
        if restaurant_slug:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
        else:
            restaurant = Restaurant.objects.first()
        landing = None
        if restaurant:
            # Try to fetch with prefetch of related content
            try:
                landing = RestaurantLanding.objects.select_related('restaurant').prefetch_related('gallery_images', 'team_members', 'reviews').get(restaurant=restaurant)
            except RestaurantLanding.DoesNotExist:
                landing = RestaurantLanding.objects.create(restaurant=restaurant)
        context['restaurant'] = restaurant
        context['landing'] = landing
        # Provide restaurant lists for navbar selection
        # All active restaurants (used for Menu dropdown)
        context['all_restaurants'] = Restaurant.objects.filter(is_active=True).order_by('name')
        # Backward compat: restaurants with same name (may be single due to unique constraint)
        if restaurant:
            context['same_named_restaurants'] = Restaurant.objects.filter(name=restaurant.name).order_by('id')
        else:
            context['same_named_restaurants'] = Restaurant.objects.none()
        return context


class AboutView(TemplateView):
    def get(self, request, *args, **kwargs):
        return redirect('/#about')


class ContactView(TemplateView):
    def get(self, request, *args, **kwargs):
        return redirect('/#visit')


class GalleryView(TemplateView):
    def get(self, request, *args, **kwargs):
        return redirect('/#gallery')


class ReserveEntryView(TemplateView):
    def get(self, request, *args, **kwargs):
        slug = request.GET.get('restaurant_slug') or request.GET.get('slug')
        if slug:
            return redirect('restaurant_reserve', restaurant_slug=slug)
        restaurant = Restaurant.objects.filter(is_active=True).order_by('id').first()
        if restaurant:
            return redirect('restaurant_reserve', restaurant_slug=restaurant.slug)
        return redirect('home')
    

class ReservationPageView(TemplateView):
    template_name = 'core/reserve.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        restaurant_slug = kwargs.get('restaurant_slug')
        if restaurant_slug:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
            context['restaurant'] = restaurant
        return context
class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        return qs


class SubCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SubCategory.objects.all()
    serializer_class = SubCategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = super().get_queryset()
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
        # First, get all menu items with their relationships
        queryset = MenuItem.objects.filter(availability=True)
        
        # # Debug: Print the raw query and count
        # print("\n=== DEBUG: MenuItem Query ===")
        # print(f"Total available items: {queryset.count()}")
        
        # Log some sample items for debugging
        for item in queryset[:5]:  # Show first 5 items for debugging
            print(f"- {item.name} (ID: {item.id})")
            print(f"  Subcategory: {getattr(item.subcategory, 'name', 'None')}")
            if item.subcategory and hasattr(item.subcategory, 'category'):
                print(f"  Category: {item.subcategory.category.name}")
        
        # Apply filtering if needed
        subcategory = self.request.query_params.get('subcategory')
        if subcategory:
            queryset = queryset.filter(subcategory_id=subcategory)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        if restaurant_id:
            queryset = queryset.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            queryset = queryset.filter(restaurant__slug=restaurant_slug)
        
        return queryset.order_by('name')
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

@method_decorator(csrf_exempt, name='dispatch')
class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'create', 'validate_coupon', 'verify_payment', 'cancel_unpaid', 'initiate_razorpay']:
            return [AllowAny()]
        return [IsAdminUser()]

    @method_decorator(csrf_exempt)
    @action(detail=False, methods=['post'], url_path='initiate-razorpay', permission_classes=[AllowAny], authentication_classes=[])
    def initiate_razorpay(self, request):
        from decimal import Decimal
        import json, urllib.request, base64
        # pyrefly: ignore [missing-import]
        from .models import Coupon, MenuItem, RestaurantSettings
        from super_admin.models import Restaurant

        data = request.data if isinstance(request.data, dict) else {}
        items_data = data.get('items') or []
        coupon_code = (data.get('coupon_code') or '').strip().upper()
        restaurant_id = data.get('restaurant_id')
        restaurant_slug = data.get('restaurant_slug')

        if not items_data:
            return Response({'error': 'No menu items provided'}, status=status.HTTP_400_BAD_REQUEST)

        restaurant = None
        subtotal = Decimal('0')
        for item in items_data:
            item_id = item.get('menu_item')
            qty = int(item.get('quantity') or 1)
            menu_item = MenuItem.objects.filter(id=item_id).first()
            if not menu_item:
                return Response({'error': f'Item ID {item_id} not found'}, status=status.HTTP_400_BAD_REQUEST)
            if restaurant is None:
                restaurant = menu_item.restaurant
            elif restaurant.id != menu_item.restaurant_id:
                return Response({'error': 'All items must belong to the same restaurant'}, status=status.HTTP_400_BAD_REQUEST)
            subtotal += Decimal(str(menu_item.price)) * qty

        if not restaurant and restaurant_id:
            restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        elif not restaurant and restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()

        settings_obj = RestaurantSettings.get_settings(restaurant)
        if not settings_obj.enable_online_payment or not settings_obj.razorpay_key_id or not settings_obj.razorpay_key_secret:
            return Response({'error': 'Online payment is not enabled for this restaurant.'}, status=status.HTTP_400_BAD_REQUEST)

        # Compute taxes & service charges
        cgst_pct = Decimal(str(settings_obj.cgst_percentage or 0))
        sgst_pct = Decimal(str(settings_obj.sgst_percentage or 0))
        service_pct = Decimal(str(settings_obj.service_charge_percentage or 0))

        cgst_amt = (subtotal * cgst_pct) / Decimal('100')
        sgst_amt = (subtotal * sgst_pct) / Decimal('100')
        service_amt = (subtotal * service_pct) / Decimal('100')
        taxes_and_charges = cgst_amt + sgst_amt + service_amt

        # Calculate coupon discount if applicable
        discount = Decimal('0')
        if coupon_code:
            coupon_qs = Coupon.objects.filter(code__iexact=coupon_code)
            coupon = coupon_qs.filter(restaurant=restaurant).first() or coupon_qs.filter(restaurant__isnull=True).first()
            if coupon and coupon.is_valid_for(subtotal, restaurant=restaurant):
                discount = coupon.calculate_discount(subtotal)
                discount = max(Decimal('0'), min(discount, subtotal))

        total_amount = max(Decimal('0'), subtotal + taxes_and_charges - discount)
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
        data = request.data if isinstance(request.data, dict) else {}
        payment_method = data.get('payment_method') or request.POST.get('payment_method') or 'cash'

        order.payment_status = 'paid'
        order.payment_method = payment_method
        if order.status == Order.OrderStatus.PENDING:
            order.status = Order.OrderStatus.SERVED

        if not order.party and order.customer_name and order.customer_name.lower() not in ['guest', 'walk-in guest'] and order.restaurant:
            from core.models import Party
            party, _ = Party.objects.get_or_create(
                restaurant=order.restaurant,
                name__iexact=order.customer_name,
                defaults={
                    'name': order.customer_name,
                    'party_type': Party.PartyType.CUSTOMER,
                    'phone': order.phone,
                }
            )
            order.party = party

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


class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Restaurant.objects.filter(is_active=True)
    serializer_class = RestaurantSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'


class RestaurantLandingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RestaurantLanding.objects.all()
    serializer_class = RestaurantLandingSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
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
        restaurant = None
        if restaurant_id:
            restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        elif restaurant_slug:
            restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
        
        obj = RestaurantSettings.get_settings(restaurant)
        serializer = self.get_serializer(obj)
        return Response(serializer.data)