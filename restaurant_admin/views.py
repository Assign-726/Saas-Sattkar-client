import os
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import Count, Sum, Q, F, Max
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.http import JsonResponse, HttpResponseBadRequest, StreamingHttpResponse
from datetime import timedelta, datetime
from decimal import Decimal
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.conf import settings
from django.apps import apps
from core.models import Zone, Table, Category, SubCategory, MenuItem, Order, OrderItem, RestaurantSettings, RestaurantLanding, InventoryItem, Coupon, MenuItemIngredient, InventoryTransaction, Supplier, PurchaseOrder, PurchaseOrderItem, Party, CustomCharge, AdvanceReceipt, Branch, DeliveryChallan, DeliveryChallanItem
from django.db import transaction
from super_admin.models import Restaurant, RestaurantUser
from restaurant_admin.forms import MenuItemForm, CategoryForm, SubCategoryForm, OrderStatusForm, RestaurantSettingsForm, InventoryItemForm, CouponForm, RestaurantBranchComplianceForm

from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import qrcode
from io import BytesIO
from django.http import HttpResponse
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from core.sse import subscribe, unsubscribe, sse_event, sse_comment
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods
from django.http import StreamingHttpResponse

def is_admin(user):
    return user.is_authenticated and user.is_staff

def resolve_restaurant(request, restaurant_slug=None):
    """Resolve the Restaurant object based on slug or the user's linked restaurant.
    If restaurant_slug is provided, use it. Otherwise, fall back to the user's profile.
    Enforce that a logged-in staff user can only access their own restaurant when both are present.
    """
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
        # If user has a linked restaurant, ensure it matches
        if request.user.is_authenticated and hasattr(request.user, 'restaurant_profile') and request.user.is_staff:
            try:
                user_restaurant = request.user.restaurant_profile.restaurant
                if user_restaurant_id := getattr(user_restaurant, 'id', None):
                    if user_restaurant_id != restaurant.id and not request.user.is_superuser:
                        from django.http import HttpResponseForbidden
                        return HttpResponseForbidden("You do not have access to this restaurant.")
            except RestaurantUser.DoesNotExist:
                pass
        return restaurant
    # Fallback to user's linked restaurant
    try:
        return request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

def test_template(request):
    """Simple test view to check template rendering"""
    context = {
        'user': request.user,
        'is_authenticated': request.user.is_authenticated,
        'is_staff': request.user.is_staff if request.user.is_authenticated else False,
        'static_files_debug': {
            'STATIC_URL': settings.STATIC_URL,
            'STATIC_ROOT': settings.STATIC_ROOT,
            'STATICFILES_DIRS': settings.STATICFILES_DIRS,
            'DEBUG': settings.DEBUG,
        },
        'template_dirs': [os.path.join(settings.BASE_DIR, 'templates')] + [os.path.join(app_config.path, 'templates') for app_config in apps.get_app_configs()]
    }
    return render(request, 'restaurant_admin/test.html', context)

def test_static(request):
    """Test view to check static files loading"""
    return render(request, 'restaurant_admin/test_static.html')

def debug_menu_list(request):
    """Debug version of menu_list without authentication"""
    try:
        # Get search query
        search_query = request.GET.get('q', '')
        
        # Base queryset
        menu_items = MenuItem.objects.select_related('subcategory', 'subcategory__category').order_by('subcategory__category__name', 'subcategory__name', 'name')
        
        # Apply search filter if query exists
        if search_query:
            menu_items = menu_items.filter(
                Q(name__icontains=search_query) |
                Q(description__icontains=search_query) |
                Q(subcategory__name__icontains=search_query) |
                Q(subcategory__category__name__icontains=search_query)
            )
        
        # Paginate the menu items
        paginator = Paginator(menu_items, 20)  # Show 20 items per page
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)
        
        return render(request, 'restaurant_admin/menu/list_new.html', {
            'menu_items': page_obj,
            'active_menu': 'menu_list',
            'search_query': search_query,
            'today': timezone.now()
        })
    except Exception as e:
        from django.http import HttpResponse
        return HttpResponse(f"Error: {str(e)}", content_type="text/plain")

@login_required
@user_passes_test(is_admin)
def order_mark_ready(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    order = get_object_or_404(Order, pk=pk, restaurant=restaurant)

    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')

    now = timezone.now()
    start = order.prep_start or order.order_time or now
    elapsed_minutes = max(0, int((now - start).total_seconds() // 60))
    order.prep_start = start
    order.ready_at = now
    # If no prep_minutes or it's longer than elapsed, set to elapsed so countdown ends
    if not order.prep_minutes or order.prep_minutes < elapsed_minutes:
        order.prep_minutes = elapsed_minutes
    # Do not change status here; keep it Pending so customer sees "Ready" state
    order.save(update_fields=['prep_start', 'ready_at', 'prep_minutes'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Order #{order.branch_order_number or order.id} marked ready now.',
            'prep_minutes': order.prep_minutes,
            'prep_start': order.prep_start,
            'ready_at': order.ready_at,
            'status': order.status,
        })

    messages.success(request, f'Order #{order.branch_order_number or order.id} marked ready now.')
    return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)

# Inventory Management
@login_required
@user_passes_test(is_admin)
def inventory_list(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    q = request.GET.get('q', '').strip()
    items = InventoryItem.objects.filter(restaurant=restaurant)
    if q:
        items = items.filter(Q(name__icontains=q) | Q(sku__icontains=q))
    items = items.order_by('name')

    return render(request, 'restaurant_admin/inventory/list.html', {
        'items': items,
        'active_menu': 'inventory',
        'search_query': q,
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def inventory_quick_add_api(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid restaurant'}, status=400)

    if request.method == 'POST':
        form = InventoryItemForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            item = form.save()
            return JsonResponse({
                'success': True,
                'item': {
                    'id': item.id,
                    'name': item.name,
                    'sku': item.sku or '',
                    'category': item.get_category_display() or item.category,
                    'unit': item.unit or '',
                    'quantity': item.quantity,
                    'cost_price': float(item.unit_cost),
                    'hsn_sac_code': item.hsn_sac_code or '',
                    'gst_rate': float(item.gst_rate or 0),
                    'taxability_type': item.taxability_type or 'taxable'
                }
            })
        else:
            errors = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            return JsonResponse({'success': False, 'error': '; '.join(errors)}, status=400)

    return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)


@login_required
@user_passes_test(is_admin)
def inventory_add(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        form = InventoryItemForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            item = form.save()
            # Log starting stock if it's non-zero
            if item.quantity != 0:
                InventoryTransaction.objects.create(
                    inventory_item=item,
                    transaction_type=InventoryTransaction.TransactionType.RESTOCK,
                    delta=item.quantity,
                    performed_by=request.user
                )
            messages.success(request, 'Inventory item added successfully!')
            return redirect('restaurant_admin:inventory_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = InventoryItemForm(restaurant=restaurant)

    return render(request, 'restaurant_admin/inventory/form.html', {
        'form': form,
        'title': 'Add Inventory Item',
        'active_menu': 'inventory',
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def order_create_manual(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        import json
        is_json = (
            request.content_type == 'application/json' or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
            'json' in request.META.get('HTTP_ACCEPT', '')
        )

        data = {}
        if is_json and request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                data = request.POST
        else:
            data = request.POST

        customer_name = (data.get('customer_name') or 'Guest').strip()
        phone = (data.get('phone_number') or data.get('phone') or '').strip()
        notes = (data.get('notes') or '').strip()
        order_type = data.get('order_type') or Order.OrderType.DINE_IN
        payment_method = data.get('payment_method') or Order.PaymentMethod.CASH
        payment_status = data.get('payment_status') or Order.PaymentStatus.PAID
        action = data.get('action', 'save')
        order_id = data.get('order_id') or data.get('existing_order_id')

        def safe_decimal(val, default='0.00'):
            try:
                return Decimal(str(val or '0'))
            except Exception:
                return Decimal(default)

        subtotal_val = safe_decimal(data.get('subtotal'))
        tax_amount_val = safe_decimal(data.get('tax_amount'))
        round_off_amount_val = safe_decimal(data.get('round_off_amount'))
        grand_total_val = safe_decimal(data.get('grand_total'))

        receipt_no_input = (data.get('receipt_no') or '').strip()
        advance_amount_input = safe_decimal(data.get('advance_amount_paid') or data.get('advance_amount'))

        # Determine payments, paid_amount, due_amount & payment_status
        payments_input = data.get('payments')
        valid_payments = []
        if isinstance(payments_input, list) and len(payments_input) > 0:
            for p in payments_input:
              amt = safe_decimal(p.get('amount'))
              if amt > 0:
                  valid_payments.append({
                      'method': str(p.get('method') or payment_method),
                      'amount': float(amt)
                  })
            paid_amount_val = sum(safe_decimal(p['amount']) for p in valid_payments)
        elif data.get('paid_amount') is not None and str(data.get('paid_amount')).strip() != '':
            paid_amount_val = safe_decimal(data.get('paid_amount'))
            if paid_amount_val > 0:
                valid_payments = [{'method': payment_method, 'amount': float(paid_amount_val)}]
        elif action in ['settle_order', 'print_close'] or data.get('payment_status') == 'paid':
            paid_amount_val = max(Decimal('0.00'), grand_total_val - advance_amount_input)
            valid_payments = [{'method': payment_method, 'amount': float(paid_amount_val)}]
        else:
            paid_amount_val = Decimal('0.00')
            valid_payments = []

        due_amount_val = max(Decimal('0.00'), grand_total_val - advance_amount_input - paid_amount_val)

        if due_amount_val <= Decimal('0.00'):
            initial_payment_status = Order.PaymentStatus.PAID
            due_amount_val = Decimal('0.00')
            if not valid_payments and grand_total_val > advance_amount_input:
                paid_amount_val = max(Decimal('0.00'), grand_total_val - advance_amount_input)
                valid_payments = [{'method': payment_method, 'amount': float(paid_amount_val)}]
        elif (paid_amount_val + advance_amount_input) > Decimal('0.00'):
            initial_payment_status = Order.PaymentStatus.PARTIALLY_PAID
        else:
            initial_payment_status = Order.PaymentStatus.UNPAID
            paid_amount_val = Decimal('0.00')
            due_amount_val = grand_total_val

        # Determine order status based on action
        if action in ['fire_kot', 'save_kot']:
            initial_status = Order.OrderStatus.PENDING
        elif action in ['settle_order', 'print_close']:
            initial_status = Order.OrderStatus.SERVED
        else:
            initial_status = Order.OrderStatus.PENDING

        gstin_no = (data.get('gstin_no') or data.get('gstin') or '').strip()
        excise_bill_no = (data.get('excise_bill_no') or data.get('excise') or '').strip()

        if order_id:
            order = get_object_or_404(Order, pk=order_id, restaurant=restaurant)
            order.customer_name = customer_name or order.customer_name
            order.phone = phone or order.phone
            order.notes = notes if notes is not None else order.notes
            order.order_type = order_type or order.order_type
            order.payment_method = payment_method or order.payment_method
            order.payment_status = initial_payment_status
            order.payments = valid_payments
            order.paid_amount = paid_amount_val
            order.due_amount = due_amount_val
            if advance_amount_input > 0:
                order.advance_amount_paid = advance_amount_input
            if order.status == Order.OrderStatus.DRAFT:
                order.status = initial_status
            order.due_amount = due_amount_val
            order.status = initial_status
            order.subtotal = subtotal_val
            order.tax_amount = tax_amount_val
            order.round_off_amount = round_off_amount_val
            order.total_amount = grand_total_val
            if gstin_no:
                order.gstin_no = gstin_no
            elif not data.get('items') or not order.check_item_types()[0]:
                order.gstin_no = None

            if excise_bill_no:
                order.excise_bill_no = excise_bill_no
            else:
                order.excise_bill_no = None
            order.save()
            # Clear existing items if new items provided
            items_data = data.get('items')
            if isinstance(items_data, list) and len(items_data) > 0:
                order.items.all().delete()
        else:
            order = Order.objects.create(
                customer_name=customer_name or 'Walk-in Guest',
                phone=phone,
                notes=notes,
                order_type=order_type,
                payment_method=payment_method,
                payment_status=initial_payment_status,
                payments=valid_payments,
                paid_amount=paid_amount_val,
                due_amount=due_amount_val,
                status=initial_status,
                restaurant=restaurant,
                branch=restaurant.branches.first() if hasattr(restaurant, 'branches') else None,
                subtotal=subtotal_val,
                tax_amount=tax_amount_val,
                round_off_amount=round_off_amount_val,
                total_amount=grand_total_val,
                gstin_no=gstin_no or None,
                excise_bill_no=excise_bill_no or None,
            )

        if customer_name and customer_name.lower() not in ['guest', 'walk-in guest']:
            party, _ = Party.objects.get_or_create(
                restaurant=restaurant,
                name__iexact=customer_name,
                defaults={
                    'name': customer_name,
                    'party_type': Party.PartyType.CUSTOMER,
                    'phone': phone,
                    'gstin': gstin_no
                }
            )
            if phone and not party.phone:
                party.phone = phone
                party.save(update_fields=['phone'])
            order.party = party
            order.save(update_fields=['party'])

        if receipt_no_input:
            arc = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_input).first()
            if arc:
                order.advance_receipt = arc
                order.advance_amount_paid = advance_amount_input if advance_amount_input > 0 else arc.advance_amount
                order.save(update_fields=['advance_receipt', 'advance_amount_paid'])
                if action in ['settle_order', 'print_close'] or data.get('payment_status') == 'paid' or initial_payment_status == Order.PaymentStatus.PAID:
                    arc.status = AdvanceReceipt.ReceiptStatus.REDEEMED
                    arc.order = order
                    arc.save(update_fields=['status', 'order'])

        items_data = data.get('items')
        if not items_data and not is_json:
            item_ids = request.POST.getlist('item_id')
            quantities = request.POST.getlist('quantity')
            unit_prices = request.POST.getlist('unit_price')
            items_data = []
            for idx, item_id in enumerate(item_ids):
                qty = quantities[idx] if idx < len(quantities) else 1
                u_price = unit_prices[idx] if idx < len(unit_prices) else 0
                items_data.append({
                    'menu_item_id': item_id,
                    'quantity': qty,
                    'unit_price': u_price
                })

        created_items = []
        if isinstance(items_data, list):
            for item in items_data:
                item_id = item.get('menu_item_id') or item.get('id')
                qty = int(item.get('quantity') or 0)
                u_price = safe_decimal(item.get('unit_price'))

                if not item_id or qty <= 0:
                    continue
                try:
                    menu_item = MenuItem.objects.get(pk=item_id, restaurant=restaurant)
                except MenuItem.DoesNotExist:
                    continue
                if u_price <= 0:
                    u_price = menu_item.price

                disc_type = item.get('discount_type', 'flat') or 'flat'
                disc_val = safe_decimal(item.get('discount_value'))
                disc_amt = safe_decimal(item.get('discount_amount'))
                disc_reason = (item.get('discount_reason') or '').strip() or None

                created_item = OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=u_price,
                    discount_type=disc_type,
                    discount_value=disc_val,
                    discount_amount=disc_amt,
                    discount_reason=disc_reason,
                )
                created_items.append(created_item)

        table_id = data.get('table_id') or data.get('table')
        if table_id:
            try:
                order.table_id = int(table_id)
                order.save()
            except Exception:
                pass

        if grand_total_val > 0:
            order.subtotal = subtotal_val
            order.tax_amount = tax_amount_val
            order.round_off_amount = round_off_amount_val
            order.total_amount = grand_total_val
            order.save()
        else:
            order.update_total()

        order.assign_invoice_numbers()

        if order.payment_status == Order.PaymentStatus.PAID or order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.SERVED]:
            order.deduct_inventory()

        kot_url = reverse('restaurant_admin:order_kot', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})
        invoice_url = reverse('restaurant_admin:order_invoice', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})
        food_invoice_url = invoice_url + '?type=food'
        liquor_invoice_url = invoice_url + '?type=liquor'
        combined_invoice_url = invoice_url + '?type=combined'
        order_list_url = reverse('restaurant_admin:order_list', kwargs={'restaurant_slug': restaurant.slug})

        invoice_mode = data.get('invoice_mode', 'food')
        if invoice_mode == 'liquor':
            settlement_target_url = liquor_invoice_url
        else:
            settlement_target_url = food_invoice_url

        if is_json:
            if action in ['save_kot', 'fire_kot']:
                target_url = kot_url
            elif action == 'save_food_invoice':
                target_url = food_invoice_url
            elif action == 'save_liquor_invoice':
                target_url = liquor_invoice_url
            elif action in ['settle_order', 'print_close']:
                target_url = settlement_target_url
            elif action in ['save_invoice', 'save_combined_invoice']:
                target_url = food_invoice_url
            else:
                target_url = order_list_url

            # Query updated active orders list
            active_orders_qs = Order.objects.filter(
                restaurant=restaurant,
                status__in=[Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING]
            ).select_related('table').prefetch_related('items', 'items__menu_item').order_by('-order_time')[:30]

            active_orders_resp = []
            for ao in active_orders_qs:
                active_orders_resp.append({
                    'id': ao.id,
                    'order_number': ao.order_number,
                    'customer_name': ao.customer_name or 'Walk-in Guest',
                    'phone': ao.phone or '',
                    'notes': ao.notes or '',
                    'table_id': ao.table.id if ao.table else None,
                    'table_number': ao.table_number or (ao.table.table_number if ao.table else None),
                    'order_type': ao.order_type,
                    'subtotal': float(ao.subtotal),
                    'tax_amount': float(ao.tax_amount),
                    'total_amount': float(ao.total_amount),
                    'order_time': ao.order_time.strftime('%I:%M %p'),
                    'items': [
                        {
                            'id': it.menu_item.id if it.menu_item else 0,
                            'name': it.menu_item.name if it.menu_item else 'Item',
                            'quantity': it.quantity,
                            'unit_price': float(it.unit_price),
                            'line_total': float(it.subtotal),
                            'tax_type': it.menu_item.tax_type if it.menu_item else 'GST_FOOD'
                        }
                        for it in ao.items.all()
                    ]
                })

            return JsonResponse({
                'success': True,
                'order_id': order.id,
                'order_number': order.order_number,
                'invoice_url': invoice_url,
                'food_invoice_url': food_invoice_url,
                'liquor_invoice_url': liquor_invoice_url,
                'combined_invoice_url': combined_invoice_url,
                'kot_url': kot_url,
                'redirect_url': target_url,
                'active_orders': active_orders_resp
            })

        messages.success(request, f'Order #{order.branch_order_number or order.id} updated/created successfully.')
        if action in ['save_kot', 'fire_kot']:
            return redirect(kot_url)
        if action in ['settle_order', 'print_close']:
            return redirect(settlement_target_url)
        if action == 'save_food_invoice':
            return redirect(food_invoice_url)
        if action == 'save_liquor_invoice':
            return redirect(liquor_invoice_url)
        if action in ['save_invoice', 'save_combined_invoice']:
            return redirect(food_invoice_url)
        return redirect(order_list_url)

    # GET
    from core.models import Table
    tables_qs = Table.objects.filter(restaurant=restaurant).order_by('table_number')
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory').order_by('category__name', 'name')
    categories_qs = Category.objects.filter(restaurant=restaurant).order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch_obj = restaurant.branches.first() if hasattr(restaurant, 'branches') else None

    cgst_pct = float(settings_obj.cgst_percentage or 2.50)
    sgst_pct = float(settings_obj.sgst_percentage or 2.50)
    vat_beer_pct = float(branch_obj.vat_beer_rate if branch_obj and getattr(branch_obj, 'vat_beer_rate', None) is not None else 60.00)
    vat_imfl_pct = float(branch_obj.vat_imfl_rate if branch_obj and getattr(branch_obj, 'vat_imfl_rate', None) is not None else 65.00)
    vat_wine_pct = float(branch_obj.vat_wine_rate if branch_obj and getattr(branch_obj, 'vat_wine_rate', None) is not None else 60.00)
    vat_gen_pct = float(getattr(settings_obj, 'vat_percentage', 20.00) or 20.00)

    menu_items_list = []
    for mi in menu_items_qs:
        cat_id = mi.category.id if mi.category else 0
        cat_name = mi.category.name if mi.category else 'General'
        img_url = mi.image.url if mi.image else ''
        
        tax_type = mi.tax_type or 'GST_FOOD'
        if tax_type == 'GST_FOOD' and cat_name:
            c_name_lower = cat_name.lower()
            if 'beer' in c_name_lower:
                tax_type = 'VAT_BEER'
            elif 'wine' in c_name_lower:
                tax_type = 'VAT_WINE'
            elif any(k in c_name_lower for k in ['liquor', 'bar', 'spirit', 'whiskey', 'vodka', 'rum', 'cocktail']):
                tax_type = 'VAT_IMFL'

        menu_items_list.append({
            'id': mi.id,
            'name': mi.name,
            'price': float(mi.price),
            'category_id': cat_id,
            'category_name': cat_name,
            'image_url': img_url,
            'tax_type': tax_type
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    # Calculate top selling item IDs for this restaurant
    from django.db.models import Sum
    top_selling_qs = (
        OrderItem.objects.filter(order__restaurant=restaurant)
        .values('menu_item_id')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')[:12]
    )
    top_selling_ids = [item['menu_item_id'] for item in top_selling_qs if item['menu_item_id']]

    # Active / In-kitchen orders
    active_orders_qs = Order.objects.filter(
        restaurant=restaurant,
        status__in=[Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING]
    ).select_related('table').prefetch_related('items', 'items__menu_item').order_by('-order_time')[:30]

    active_orders_list = []
    for ao in active_orders_qs:
        active_orders_list.append({
            'id': ao.id,
            'order_number': ao.order_number,
            'customer_name': ao.customer_name or 'Walk-in Guest',
            'phone': ao.phone or '',
            'notes': ao.notes or '',
            'table_id': ao.table.id if ao.table else None,
            'table_number': ao.table_number or (ao.table.table_number if ao.table else None),
            'order_type': ao.order_type,
            'subtotal': float(ao.subtotal),
            'tax_amount': float(ao.tax_amount),
            'total_amount': float(ao.total_amount),
            'order_time': ao.order_time.strftime('%I:%M %p'),
            'items': [
                {
                    'id': it.menu_item.id if it.menu_item else 0,
                    'name': it.menu_item.name if it.menu_item else 'Item',
                    'quantity': it.quantity,
                    'unit_price': float(it.unit_price),
                    'line_total': float(it.subtotal),
                    'tax_type': it.menu_item.tax_type if it.menu_item else 'GST_FOOD'
                }
                for it in ao.items.all()
            ]
        })

    custom_charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True).order_by('charge_name')
    custom_charges_list = []
    for cc in custom_charges_qs:
        custom_charges_list.append({
            'id': cc.id,
            'name': cc.charge_name,
            'type': cc.charge_type,
            'value': float(cc.value),
            'is_taxable': cc.is_taxable,
            'applied_order_types': cc.applied_order_types or ['dine_in', 'takeaway', 'delivery'],
            'is_auto_applied': cc.is_auto_applied,
        })

    import json
    attached_receipt = None
    draft_order = None
    order_id_param = request.GET.get('order_id')
    receipt_no_param = request.GET.get('receipt_no')

    if order_id_param:
        draft_order = Order.objects.filter(pk=order_id_param, restaurant=restaurant).first()
        if draft_order and draft_order.advance_receipt:
            attached_receipt = draft_order.advance_receipt

    if not attached_receipt and receipt_no_param:
        attached_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_param).first()
        if attached_receipt and attached_receipt.order:
            draft_order = attached_receipt.order

    if not attached_receipt and draft_order and draft_order.advance_amount_paid > 0:
        attached_receipt_json = json.dumps({
            'order_id': draft_order.id,
            'receipt_no': draft_order.advance_receipt.receipt_no if draft_order.advance_receipt else f"ORD-{draft_order.id:04d}",
            'booking_ref': draft_order.order_number or f"ORD-{draft_order.id:04d}",
            'customer_name': draft_order.customer_name,
            'phone': draft_order.phone,
            'advance_amount': float(draft_order.advance_amount_paid),
            'payment_method': draft_order.payment_method,
        })
    elif attached_receipt:
        attached_receipt_json = json.dumps({
            'order_id': attached_receipt.order.id if attached_receipt.order else 0,
            'receipt_no': attached_receipt.receipt_no,
            'booking_ref': attached_receipt.order.order_number if attached_receipt.order else attached_receipt.booking_ref,
            'customer_name': attached_receipt.customer_name,
            'phone': attached_receipt.phone,
            'advance_amount': float(attached_receipt.advance_amount),
            'payment_method': attached_receipt.payment_method,
        })
    else:
        attached_receipt_json = 'null'

    return render(request, 'restaurant_admin/orders/manual_create.html', {
        'menu_items': menu_items_qs,
        'menu_items_json': json.dumps(menu_items_list),
        'top_selling_json': json.dumps(top_selling_ids),
        'categories_json': json.dumps(categories_list),
        'categories': categories_qs,
        'tables': tables_qs,
        'active_orders': active_orders_qs,
        'active_orders_json': json.dumps(active_orders_list),
        'tax_settings_json': json.dumps({
            'cgst_pct': cgst_pct,
            'sgst_pct': sgst_pct,
            'vat_beer_pct': vat_beer_pct,
            'vat_imfl_pct': vat_imfl_pct,
            'vat_wine_pct': vat_wine_pct,
            'vat_gen_pct': vat_gen_pct,
        }),
        'customer_parties': Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both']).order_by('name'),
        'custom_charges_json': json.dumps(custom_charges_list),
        'attached_receipt': attached_receipt,
        'attached_receipt_json': attached_receipt_json,
        'active_menu': 'order_list',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def order_edit_manual(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    order = get_object_or_404(Order.objects.prefetch_related('items', 'items__menu_item').filter(restaurant=restaurant), pk=pk)

    if request.method == 'POST':
        import json
        is_json = (
            request.content_type == 'application/json' or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
            'json' in request.META.get('HTTP_ACCEPT', '')
        )

        data = {}
        if is_json and request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                data = request.POST
        else:
            data = request.POST

        def safe_decimal(val, default='0.00'):
            try:
                return Decimal(str(val or '0'))
            except Exception:
                return Decimal(default)

        subtotal_val = safe_decimal(data.get('subtotal'))
        tax_amount_val = safe_decimal(data.get('tax_amount'))
        round_off_amount_val = safe_decimal(data.get('round_off_amount'))
        grand_total_val = safe_decimal(data.get('grand_total'))

        order.customer_name = (data.get('customer_name') or 'Guest').strip()
        order.phone = (data.get('phone_number') or data.get('phone') or '').strip()
        gstin_no = (data.get('gstin_no') or data.get('gstin') or '').strip()
        excise_bill_no = (data.get('excise_bill_no') or data.get('excise') or '').strip()
        if gstin_no: order.gstin_no = gstin_no
        if excise_bill_no: order.excise_bill_no = excise_bill_no

        table_id = data.get('table_id') or data.get('table')
        if table_id:
            try:
                order.table_id = int(table_id)
            except Exception:
                pass

        action = data.get('action', 'save')

        items_data = data.get('items')
        if not items_data and not is_json:
            item_ids = request.POST.getlist('item_id')
            quantities = request.POST.getlist('quantity')
            unit_prices = request.POST.getlist('unit_price')
            items_data = []
            for idx, item_id in enumerate(item_ids):
                qty = quantities[idx] if idx < len(quantities) else 1
                u_price = unit_prices[idx] if idx < len(unit_prices) else 0
                items_data.append({
                    'menu_item_id': item_id,
                    'quantity': qty,
                    'unit_price': u_price
                })

        if isinstance(items_data, list):
            order.items.all().delete()
            for item in items_data:
                item_id = item.get('menu_item_id') or item.get('id')
                qty = int(item.get('quantity') or 0)
                try:
                    u_price = Decimal(str(item.get('unit_price') or 0))
                except Exception:
                    u_price = Decimal('0.00')

                if not item_id or qty <= 0:
                    continue
                try:
                    menu_item = MenuItem.objects.get(pk=item_id, restaurant=restaurant)
                except MenuItem.DoesNotExist:
                    continue
                if u_price <= 0:
                    u_price = menu_item.price

                OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=u_price,
                )

        if grand_total_val > 0:
            order.subtotal = subtotal_val
            order.tax_amount = tax_amount_val
            order.round_off_amount = round_off_amount_val
            order.total_amount = grand_total_val
            order.save()
        else:
            order.update_total()

        if order.payment_status == Order.PaymentStatus.PAID or order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.SERVED]:
            order.deduct_inventory()

        kot_url = reverse('restaurant_admin:order_kot', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})
        invoice_url = reverse('restaurant_admin:order_invoice', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})
        food_invoice_url = invoice_url + '?type=food'
        liquor_invoice_url = invoice_url + '?type=liquor'
        combined_invoice_url = invoice_url + '?type=combined'
        order_detail_url = reverse('restaurant_admin:order_detail', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})

        if is_json:
            if action == 'save_kot':
                target_url = kot_url
            elif action == 'save_food_invoice':
                target_url = food_invoice_url
            elif action == 'save_liquor_invoice':
                target_url = liquor_invoice_url
            elif action in ['save_invoice', 'save_combined_invoice']:
                target_url = food_invoice_url  # Combined bill disabled; fallback to separate food bill
            else:
                target_url = order_detail_url

            return JsonResponse({
                'success': True,
                'order_id': order.id,
                'order_number': order.order_number,
                'invoice_url': invoice_url,
                'food_invoice_url': food_invoice_url,
                'liquor_invoice_url': liquor_invoice_url,
                'combined_invoice_url': combined_invoice_url,
                'kot_url': kot_url,
                'redirect_url': target_url
            })

        messages.success(request, f'Order #{order.branch_order_number or order.id} updated successfully.')
        if action == 'save_kot':
            return redirect(kot_url)
        if action == 'save_food_invoice':
            return redirect(food_invoice_url)
        if action == 'save_liquor_invoice':
            return redirect(liquor_invoice_url)
        if action in ['save_invoice', 'save_combined_invoice']:
            return redirect(food_invoice_url)  # Combined bill disabled; fallback to separate food bill
        return redirect(order_detail_url)

    # GET
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory').order_by('category__name', 'name')
    categories_qs = Category.objects.filter(restaurant=restaurant).order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch_obj = restaurant.branches.first() if hasattr(restaurant, 'branches') else None

    cgst_pct = float(settings_obj.cgst_percentage or 2.50)
    sgst_pct = float(settings_obj.sgst_percentage or 2.50)
    vat_beer_pct = float(branch_obj.vat_beer_rate if branch_obj and getattr(branch_obj, 'vat_beer_rate', None) is not None else 60.00)
    vat_imfl_pct = float(branch_obj.vat_imfl_rate if branch_obj and getattr(branch_obj, 'vat_imfl_rate', None) is not None else 65.00)
    vat_wine_pct = float(branch_obj.vat_wine_rate if branch_obj and getattr(branch_obj, 'vat_wine_rate', None) is not None else 60.00)
    vat_gen_pct = float(getattr(settings_obj, 'vat_percentage', 20.00) or 20.00)

    menu_items_list = []
    for mi in menu_items_qs:
        cat_id = mi.category.id if mi.category else 0
        cat_name = mi.category.name if mi.category else 'General'
        img_url = mi.image.url if mi.image else ''
        
        tax_type = mi.tax_type or 'GST_FOOD'
        if tax_type == 'GST_FOOD' and cat_name:
            c_name_lower = cat_name.lower()
            if 'beer' in c_name_lower:
                tax_type = 'VAT_BEER'
            elif 'wine' in c_name_lower:
                tax_type = 'VAT_WINE'
            elif any(k in c_name_lower for k in ['liquor', 'bar', 'spirit', 'whiskey', 'vodka', 'rum', 'cocktail']):
                tax_type = 'VAT_IMFL'

        menu_items_list.append({
            'id': mi.id,
            'name': mi.name,
            'price': float(mi.price),
            'category_id': cat_id,
            'category_name': cat_name,
            'image_url': img_url,
            'tax_type': tax_type
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    existing_items = []
    for item in order.items.select_related('menu_item', 'menu_item__category').all():
        mi = item.menu_item
        cat_name = mi.category.name if mi and mi.category else 'General'
        tax_type = (mi.tax_type if mi else 'GST_FOOD') or 'GST_FOOD'
        if tax_type == 'GST_FOOD' and cat_name:
            c_name_lower = cat_name.lower()
            if 'beer' in c_name_lower:
                tax_type = 'VAT_BEER'
            elif 'wine' in c_name_lower:
                tax_type = 'VAT_WINE'
            elif any(k in c_name_lower for k in ['liquor', 'bar', 'spirit', 'whiskey', 'vodka', 'rum', 'cocktail']):
                tax_type = 'VAT_IMFL'

        existing_items.append({
            'id': mi.id if mi else 0,
            'name': mi.name if mi else 'Item',
            'unit_price': float(item.unit_price or (mi.price if mi else 0)),
            'quantity': item.quantity,
            'tax_type': tax_type,
            'category_name': cat_name
        })

    existing_order_dict = {
        'id': order.id,
        'order_number': order.branch_order_number or f'#{order.id}',
        'customer_name': order.customer_name,
        'phone': order.phone,
        'notes': order.notes,
        'table_id': order.table.id if order.table else None,
        'order_type': order.order_type or 'dine_in',
        'payment_method': order.payment_method or 'cash',
        'payment_status': order.payment_status or 'paid',
        'items': existing_items
    }

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    # Calculate top selling item IDs for this restaurant
    from django.db.models import Sum
    top_selling_qs = (
        OrderItem.objects.filter(order__restaurant=restaurant)
        .values('menu_item_id')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')[:12]
    )
    top_selling_ids = [item['menu_item_id'] for item in top_selling_qs if item['menu_item_id']]

    from core.models import Table
    tables_qs = Table.objects.filter(restaurant=restaurant).order_by('table_number')

    import json
    return render(request, 'restaurant_admin/orders/manual_create.html', {
        'menu_items': menu_items_qs,
        'menu_items_json': json.dumps(menu_items_list),
        'top_selling_json': json.dumps(top_selling_ids),
        'categories_json': json.dumps(categories_list),
        'categories': categories_qs,
        'tables': tables_qs,
        'tax_settings_json': json.dumps({
            'cgst_pct': cgst_pct,
            'sgst_pct': sgst_pct,
            'vat_beer_pct': vat_beer_pct,
            'vat_imfl_pct': vat_imfl_pct,
            'vat_wine_pct': vat_wine_pct,
            'vat_gen_pct': vat_gen_pct,
        }),
        'existing_order_json': json.dumps(existing_order_dict),
        'is_edit_mode': True,
        'order': order,
        'active_menu': 'order_list',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def menu_quick_add_api(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            menu_item = form.save()
            return JsonResponse({
                'success': True,
                'item': {
                    'id': menu_item.id,
                    'name': menu_item.name,
                    'price': float(menu_item.price),
                    'tax_type': menu_item.tax_type or 'GST_FOOD',
                    'category_id': menu_item.category.id if menu_item.category else None,
                    'category_name': menu_item.category.name if menu_item.category else 'General'
                }
            })
        else:
            errors = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            return JsonResponse({'success': False, 'error': '; '.join(errors)}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@login_required
@user_passes_test(is_admin)
def inventory_edit(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    item = get_object_or_404(InventoryItem, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        old_quantity = item.quantity or 0
        form = InventoryItemForm(request.POST, instance=item, restaurant=restaurant)
        if form.is_valid():
            item = form.save()
            new_quantity = item.quantity or 0
            if old_quantity != new_quantity:
                delta = new_quantity - old_quantity
                InventoryTransaction.objects.create(
                    inventory_item=item,
                    transaction_type=InventoryTransaction.TransactionType.MANUAL,
                    delta=delta,
                    performed_by=request.user
                )
            messages.success(request, 'Inventory item updated successfully!')
            return redirect('restaurant_admin:inventory_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = InventoryItemForm(instance=item, restaurant=restaurant)

    return render(request, 'restaurant_admin/inventory/form.html', {
        'form': form,
        'title': 'Edit Inventory Item',
        'active_menu': 'inventory',
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def inventory_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    item = get_object_or_404(InventoryItem, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        item.delete()
        messages.success(request, 'Inventory item deleted successfully!')
        return redirect('restaurant_admin:inventory_list', restaurant_slug=restaurant.slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': item, 'restaurant': restaurant})

@login_required
@user_passes_test(is_admin)
def inventory_adjust(request, pk, restaurant_slug=None):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    item = get_object_or_404(InventoryItem, pk=pk, restaurant=restaurant)
    try:
        delta = int(request.POST.get('delta', '0'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid adjustment value'}, status=400)
    item.adjust(delta)
    InventoryTransaction.objects.create(
        inventory_item=item,
        transaction_type=InventoryTransaction.TransactionType.MANUAL if delta < 0 else InventoryTransaction.TransactionType.RESTOCK,
        delta=delta,
        performed_by=request.user
    )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'quantity': item.quantity})
    messages.success(request, 'Inventory adjusted successfully!')
    return redirect('restaurant_admin:inventory_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def menu_recipe(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    ingredients = MenuItemIngredient.objects.filter(menu_item=menu_item).select_related('inventory_item')

    if request.method == 'POST':
        inv_item_id = request.POST.get('inventory_item')
        qty = request.POST.get('quantity_needed')
        try:
            inv_item = get_object_or_404(InventoryItem, pk=inv_item_id, restaurant=restaurant)
            qty_val = Decimal(qty)
            if qty_val <= 0:
                raise ValueError("Quantity must be greater than zero.")

            # Create or update recipe requirement
            obj, created = MenuItemIngredient.objects.get_or_create(
                menu_item=menu_item,
                inventory_item=inv_item,
                defaults={'quantity_needed': qty_val}
            )
            if not created:
                obj.quantity_needed = qty_val
                obj.save()

            messages.success(request, f"Recipe requirement for {inv_item.name} saved successfully!")
            return redirect('restaurant_admin:menu_recipe', restaurant_slug=restaurant.slug, pk=menu_item.id)
        except Exception as e:
            messages.error(request, f"Error saving recipe requirement: {str(e)}")

    # GET: fetch all inventory items for this restaurant
    all_inventory = InventoryItem.objects.filter(restaurant=restaurant, is_active=True).order_by('name')

    return render(request, 'restaurant_admin/menu/recipe.html', {
        'menu_item': menu_item,
        'ingredients': ingredients,
        'all_inventory': all_inventory,
        'active_menu': 'menu_list',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def menu_recipe_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    recipe_item = get_object_or_404(MenuItemIngredient, pk=pk, menu_item__restaurant=restaurant)
    menu_item_id = recipe_item.menu_item.id
    recipe_item.delete()
    messages.success(request, "Ingredient removed from recipe.")
    return redirect('restaurant_admin:menu_recipe', restaurant_slug=restaurant.slug, pk=menu_item_id)


@login_required
@user_passes_test(is_admin)
def inventory_logs(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    logs = InventoryTransaction.objects.filter(inventory_item__restaurant=restaurant).select_related('inventory_item', 'order', 'performed_by').order_by('-created_at')

    return render(request, 'restaurant_admin/inventory/logs.html', {
        'logs': logs,
        'active_menu': 'inventory_logs',
        'restaurant': restaurant,
    })


# Party Management Views
@login_required
@user_passes_test(is_admin)
def party_list(request, restaurant_slug=None):
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        try:
            restaurant = request.user.restaurant_profile.restaurant
        except RestaurantUser.DoesNotExist:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        party_id = request.POST.get('party_id')
        name = request.POST.get('name', '').strip()
        party_type = request.POST.get('party_type', 'vendor')
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        gstin = request.POST.get('gstin', '').strip()
        gst_registration_type = request.POST.get('gst_registration_type', 'unregistered').strip()
        place_of_supply = request.POST.get('place_of_supply', '').strip()
        address_line1 = request.POST.get('address_line1', '').strip()
        city = request.POST.get('city', '').strip()
        state = request.POST.get('state', '').strip()
        pincode = request.POST.get('pincode', '').strip()
        address = request.POST.get('address', '').strip()

        combined_parts = [p for p in [address_line1, city, state, pincode] if p]
        full_addr_str = ", ".join(combined_parts) if combined_parts else address

        if not name:
            messages.error(request, "Party Name is required.")
            return redirect(request.path)

        if party_id:
            party = get_object_or_404(Party, id=party_id, restaurant=restaurant)
            party.name = name
            party.party_type = party_type
            party.phone = phone
            party.email = email
            party.gstin = gstin
            party.gst_registration_type = gst_registration_type
            party.place_of_supply = place_of_supply
            party.address_line1 = address_line1
            party.city = city
            party.state = state
            party.pincode = pincode
            party.address = full_addr_str
            party.save()
            messages.success(request, f"Party '{party.name}' updated successfully.")
        else:
            party = Party.objects.create(
                restaurant=restaurant,
                name=name,
                party_type=party_type,
                phone=phone,
                email=email,
                gstin=gstin,
                gst_registration_type=gst_registration_type,
                place_of_supply=place_of_supply,
                address_line1=address_line1,
                city=city,
                state=state,
                pincode=pincode,
                address=full_addr_str,
                opening_balance=Decimal('0.00'),
                balance_type=Party.BalanceType.PAYABLE
            )
            messages.success(request, f"Party '{party.name}' created successfully.")

        return redirect('restaurant_admin:party_list', restaurant_slug=restaurant.slug)

    tab = request.GET.get('tab', 'all')
    q = request.GET.get('q', '').strip()

    parties = Party.objects.filter(restaurant=restaurant)
    if tab == 'vendor':
        parties = parties.filter(party_type__in=['vendor', 'both'])
    elif tab == 'customer':
        parties = parties.filter(party_type__in=['customer', 'both'])

    if q:
        parties = parties.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(gstin__icontains=q))

    parties = parties.order_by('name')

    return render(request, 'restaurant_admin/parties/list.html', {
        'restaurant': restaurant,
        'parties': parties,
        'current_tab': tab,
        'search_query': q,
        'active_menu': 'parties',
    })


@login_required
@user_passes_test(is_admin)
def party_edit_api(request, restaurant_slug, pk):
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        restaurant = request.user.restaurant_profile.restaurant

    party = get_object_or_404(Party, pk=pk, restaurant=restaurant)

    if request.method == 'GET':
        return JsonResponse({
            'id': party.id,
            'name': party.name,
            'party_type': party.party_type,
            'phone': party.phone or '',
            'email': party.email or '',
            'gstin': party.gstin or '',
            'gst_registration_type': party.gst_registration_type or 'unregistered',
            'place_of_supply': party.place_of_supply or '',
            'address_line1': party.address_line1 or '',
            'city': party.city or '',
            'state': party.state or '',
            'pincode': party.pincode or '',
            'address': party.full_address,
            'opening_balance': str(party.opening_balance),
            'balance_type': party.balance_type
        })


@login_required
@user_passes_test(is_admin)
def party_ledger_detail(request, restaurant_slug, pk):
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        restaurant = request.user.restaurant_profile.restaurant

    party = get_object_or_404(Party, pk=pk, restaurant=restaurant)

    purchases = PurchaseOrder.objects.filter(restaurant=restaurant).filter(
        Q(party=party) | Q(supplier_name__iexact=party.name)
    ).order_by('-purchase_date', '-id')

    sales = Order.objects.filter(restaurant=restaurant).filter(
        Q(party=party) | Q(customer_name__iexact=party.name) | (Q(phone=party.phone) if party.phone else Q(pk=None))
    ).exclude(status='C').order_by('-order_time', '-id')

    ledger = []
    for po in purchases:
        ledger.append({
            'date': po.purchase_date,
            'created_at': po.created_at,
            'ref_no': po.invoice_number,
            'type_label': 'Inward Purchase',
            'type_code': 'PURCHASE',
            'amount': po.grand_total,
            'payment_status': po.payment_status,
            'url': reverse('restaurant_admin:purchase_list', kwargs={'restaurant_slug': restaurant.slug})
        })

    for ord_obj in sales:
        if not ord_obj.party:
            ord_obj.party = party
            ord_obj.save(update_fields=['party'])
        ref_no = ord_obj.branch_order_number or f"ORD-{ord_obj.id}"
        p_status = 'paid' if ord_obj.payment_status in ['paid', 'P'] else ord_obj.payment_status
        ledger.append({
            'date': ord_obj.order_time.date(),
            'created_at': ord_obj.order_time,
            'ref_no': ref_no,
            'type_label': 'POS Sale',
            'type_code': 'SALE',
            'amount': ord_obj.total_amount,
            'payment_status': p_status,
            'url': reverse('restaurant_admin:order_invoice', kwargs={'restaurant_slug': restaurant.slug, 'pk': ord_obj.id})
        })

    ledger.sort(key=lambda x: (x['date'], x['created_at']), reverse=True)

    return render(request, 'restaurant_admin/parties/ledger.html', {
        'restaurant': restaurant,
        'party': party,
        'ledger': ledger,
        'active_menu': 'parties',
    })


@login_required
@user_passes_test(is_admin)
def purchase_create(request, restaurant_slug=None):
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        try:
            restaurant = request.user.restaurant_profile.restaurant
        except RestaurantUser.DoesNotExist:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        from django.db import transaction

        is_json = request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.content_type
        if is_json:
            try:
                data = json.loads(request.body)
            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Invalid JSON payload: {str(e)}'}, status=400)
        else:
            raw_items = request.POST.get('items', '[]')
            try:
                items_parsed = json.loads(raw_items)
            except Exception:
                items_parsed = []
            data = {
                'supplier_name': request.POST.get('supplier_name', '').strip(),
                'supplier_gstin': request.POST.get('supplier_gstin', '').strip(),
                'invoice_number': request.POST.get('invoice_number', '').strip(),
                'purchase_date': request.POST.get('purchase_date', timezone.now().strftime('%Y-%m-%d')),
                'payment_method': request.POST.get('payment_method', 'bank_transfer'),
                'payment_status': request.POST.get('payment_status', 'paid'),
                'tax_amount': request.POST.get('tax_amount', '0'),
                'items': items_parsed
            }

        supplier_name = (data.get('supplier_name') or '').strip()
        invoice_number = (data.get('invoice_number') or '').strip()
        purchase_date_str = (data.get('purchase_date') or '').strip() or timezone.now().strftime('%Y-%m-%d')
        items = data.get('items') or []

        if not supplier_name:
            err = 'Supplier Name is required.'
            return JsonResponse({'success': False, 'error': err}, status=400) if is_json else (messages.error(request, err) or redirect(request.path))

        if not invoice_number:
            err = 'Supplier Bill/Invoice No. is required.'
            return JsonResponse({'success': False, 'error': err}, status=400) if is_json else (messages.error(request, err) or redirect(request.path))

        if not items:
            err = 'Please add at least one inventory item to the purchase list.'
            return JsonResponse({'success': False, 'error': err}, status=400) if is_json else (messages.error(request, err) or redirect(request.path))

        try:
            with transaction.atomic():
                supplier, _ = Supplier.objects.get_or_create(
                    restaurant=restaurant,
                    name__iexact=supplier_name,
                    defaults={
                        'name': supplier_name,
                        'gstin': (data.get('supplier_gstin') or '').strip()
                    }
                )
                if data.get('supplier_gstin'):
                    new_gstin = data['supplier_gstin'].strip()
                    if supplier.gstin != new_gstin:
                        supplier.gstin = new_gstin
                        supplier.save(update_fields=['gstin'])

                # Find or create Party
                party, _ = Party.objects.get_or_create(
                    restaurant=restaurant,
                    name__iexact=supplier_name,
                    defaults={
                        'name': supplier_name,
                        'party_type': Party.PartyType.VENDOR,
                        'gstin': (data.get('supplier_gstin') or '').strip()
                    }
                )
                if data.get('supplier_gstin') and party.gstin != data['supplier_gstin'].strip():
                    party.gstin = data['supplier_gstin'].strip()
                    party.save(update_fields=['gstin'])

                subtotal = Decimal('0.00')
                parsed_items = []
                for item_data in items:
                    item_id = item_data.get('item_id')
                    inv_item = get_object_or_404(InventoryItem, id=item_id, restaurant=restaurant)
                    qty = Decimal(str(item_data.get('quantity') or 1))
                    cost = Decimal(str(item_data.get('unit_cost') if item_data.get('unit_cost') is not None else (inv_item.price_per_unit or 0)))
                    line_total = (qty * cost).quantize(Decimal('0.01'))
                    subtotal += line_total
                    parsed_items.append({
                        'inventory_item': inv_item,
                        'quantity': qty,
                        'unit_cost': cost,
                        'line_total': line_total
                    })

                tax_amount = Decimal(str(data.get('tax_amount') or 0)).quantize(Decimal('0.01'))
                grand_total = subtotal + tax_amount

                po = PurchaseOrder.objects.create(
                    restaurant=restaurant,
                    party=party,
                    supplier=supplier,
                    supplier_name=supplier.name,
                    supplier_gstin=supplier.gstin or '',
                    invoice_number=invoice_number,
                    purchase_date=purchase_date_str,
                    subtotal=subtotal,
                    tax_amount=tax_amount,
                    grand_total=grand_total,
                    payment_method=data.get('payment_method', 'bank_transfer'),
                    payment_status=data.get('payment_status', 'paid'),
                    created_by=request.user
                )

                for pi in parsed_items:
                    PurchaseOrderItem.objects.create(
                        purchase_order=po,
                        inventory_item=pi['inventory_item'],
                        quantity=pi['quantity'],
                        unit_cost=pi['unit_cost'],
                        line_total=pi['line_total']
                    )

                    inv_item = pi['inventory_item']
                    delta_qty = int(pi['quantity'])
                    inv_item.adjust(delta_qty)

                    if pi['unit_cost'] > 0:
                        inv_item.cost_price = pi['unit_cost']
                        inv_item.price_per_unit = pi['unit_cost']
                        inv_item.save(update_fields=['cost_price', 'price_per_unit'])

                    InventoryTransaction.objects.create(
                        inventory_item=inv_item,
                        transaction_type=InventoryTransaction.TransactionType.INWARD_PURCHASE,
                        delta=delta_qty,
                        purchase_order=po,
                        performed_by=request.user
                    )

                if is_json:
                    return JsonResponse({
                        'success': True,
                        'message': f'Purchase order #{po.id} saved and inventory updated successfully!',
                        'redirect_url': reverse('restaurant_admin:purchase_list', kwargs={'restaurant_slug': restaurant.slug})
                    })

                messages.success(request, f'Purchase Order #{po.id} saved and inventory stock updated!')
                return redirect('restaurant_admin:purchase_list', restaurant_slug=restaurant.slug)

        except Exception as e:
            err = f'Error processing purchase entry: {str(e)}'
            if is_json:
                return JsonResponse({'success': False, 'error': err}, status=500)
            messages.error(request, err)
            return redirect(request.path)

    # GET request
    suppliers = Supplier.objects.filter(restaurant=restaurant).order_by('name')
    vendor_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['vendor', 'both']).order_by('name')
    inventory_items = InventoryItem.objects.filter(restaurant=restaurant, is_active=True).order_by('name')
    
    inventory_items_list = []
    for item in inventory_items:
        inventory_items_list.append({
            'id': item.id,
            'name': item.name,
            'sku': item.sku or '',
            'category': item.get_category_display() if hasattr(item, 'get_category_display') else item.category,
            'unit': item.unit or 'unit',
            'quantity': item.quantity,
            'cost_price': float(item.unit_cost),
            'hsn_sac_code': item.hsn_sac_code or '',
            'gst_rate': float(item.gst_rate or 0),
            'taxability_type': item.taxability_type or 'taxable'
        })
    categories = [choice[0] for choice in InventoryItem.CategoryChoices.choices]
    today_date = timezone.now().strftime('%Y-%m-%d')

    last_po = PurchaseOrder.objects.filter(restaurant=restaurant).order_by('-id').first()
    next_po_id = (last_po.id + 1001) if last_po else 1001
    entry_no = f"PO-{next_po_id}"

    return render(request, 'restaurant_admin/purchases/create.html', {
        'restaurant': restaurant,
        'suppliers': suppliers,
        'vendor_parties': vendor_parties,
        'inventory_items': inventory_items,
        'inventory_items_json': json.dumps(inventory_items_list),
        'categories': categories,
        'today_date': today_date,
        'entry_no': entry_no,
        'active_menu': 'purchases',
    })


@login_required
@user_passes_test(is_admin)
def purchase_list(request, restaurant_slug=None):
    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        try:
            restaurant = request.user.restaurant_profile.restaurant
        except RestaurantUser.DoesNotExist:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("No restaurant associated with this account.")

    purchases = PurchaseOrder.objects.filter(restaurant=restaurant).select_related('supplier', 'created_by').prefetch_related('items__inventory_item').order_by('-purchase_date', '-id')

    paginator = Paginator(purchases, 20)
    page = request.GET.get('page')
    try:
        purchases_page = paginator.page(page)
    except PageNotAnInteger:
        purchases_page = paginator.page(1)
    except EmptyPage:
        purchases_page = paginator.page(paginator.num_pages)

    return render(request, 'restaurant_admin/purchases/list.html', {
        'restaurant': restaurant,
        'purchases': purchases_page,
        'active_menu': 'purchases',
    })


class MenuListView(ListView):
    model = MenuItem
    template_name = 'restaurant_admin/menu/list_new.html'
    context_object_name = 'menu_items'
    paginate_by = 20

    def get_queryset(self):
        queryset = super().get_queryset()
        queryset = queryset.select_related('subcategory', 'subcategory__category').order_by('subcategory__category__name', 'subcategory__name', 'name')
        return queryset
def dashboard(request, restaurant_slug=None):
    # Check authentication
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied. Staff permissions required.")
    
    # Resolve restaurant for this staff user or slug
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant  # HttpResponse

    # Get today's date
    today = timezone.now()
    
    # Get statistics (scoped to restaurant)
    total_orders = Order.objects.filter(restaurant=restaurant).exclude(status=Order.OrderStatus.DRAFT).count()
    today_orders = Order.objects.filter(restaurant=restaurant, order_time__date=today.date()).exclude(status=Order.OrderStatus.DRAFT).count()
    total_menu_items = MenuItem.objects.filter(restaurant=restaurant).count()
    
    # Calculate total revenue from completed/served orders only (excluding drafts)
    total_revenue = Order.objects.filter(
        restaurant=restaurant,
        status__in=['C', 'S']  # Completed or Served
    ).exclude(status=Order.OrderStatus.DRAFT).aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    # Today's advance collections (Cashflow for end-of-day drawer balance)
    today_advance_collected = Order.objects.filter(
        restaurant=restaurant,
        order_time__date=today.date()
    ).aggregate(s=Sum('advance_amount_paid'))['s'] or Decimal('0.00')
    
    # Get recent orders
    recent_orders = Order.objects.filter(restaurant=restaurant).order_by('-order_time')[:5]
    
    # Get top selling items
    top_items = OrderItem.objects.filter(order__restaurant=restaurant).values(
        'menu_item__name'
    ).annotate(
        total_ordered=Sum('quantity')
    ).order_by('-total_ordered')[:5]
    
    agg_dash = Order.objects.filter(restaurant=restaurant).aggregate(
        latest_order_time=Max('order_time'),
        total_orders=Count('id'),
    )

    # Notification sound URL
    try:
        notif_url = RestaurantSettings.get_settings(restaurant).notification_sound.url
    except Exception:
        notif_url = ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''

    context = {
        'active_menu': 'dashboard',
        'user': request.user,
        'total_orders': total_orders,
        'today_orders': today_orders,
        'total_menu_items': total_menu_items,
        'total_revenue': total_revenue,
        'recent_orders': recent_orders,
        'top_items': top_items,
        'today': today,
        'restaurant': restaurant,
        'baseline_total_orders': agg_dash.get('total_orders') or 0,
        'baseline_latest_order_iso': agg_dash.get('latest_order_time').isoformat() if agg_dash.get('latest_order_time') else '',
        'notification_sound_url': notif_url,
        'notification_sound_abs_url': notif_abs_url,
    }
    
    return render(request, 'restaurant_admin/dashboard.html', context)

# Menu Management
@login_required
@user_passes_test(is_admin)
def menu_list(request, restaurant_slug=None):
    # Resolve restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    
    # Get search query and category filter
    search_query = request.GET.get('q', '')
    category_id = request.GET.get('category')
    
    # Get all categories for the filter dropdown (scoped)
    categories = Category.objects.filter(restaurant=restaurant).order_by('name')
    
    # Base queryset scoped to restaurant
    menu_items = MenuItem.objects.select_related('subcategory__category').filter(restaurant=restaurant)
    
    # Apply category filter if selected
    if category_id:
        menu_items = menu_items.filter(subcategory__category_id=category_id)
    
    # Apply search filter if query exists
    if search_query:
        menu_items = menu_items.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(subcategory__name__icontains=search_query) |
            Q(subcategory__category__name__icontains=search_query)
        )
    
    # Order by category and name
    menu_items = menu_items.order_by('subcategory__category__name', 'name')
    
    # Paginate the menu items
    paginator = Paginator(menu_items, 12)  # Show 12 items per page (3x4 grid)
    page_number = request.GET.get('page')
    
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    
    # Get the current URL parameters for pagination
    url_params = request.GET.copy()
    if 'page' in url_params:
        del url_params['page']
    
    context = {
        'menu_items': page_obj,
        'categories': categories,
        'search_query': search_query,
        'active_menu': 'menu_list',  # This should match the menu item's active class check
        'title': 'Menu Management',
        'url_params': url_params.urlencode() if url_params else '',
        'restaurant': restaurant,
    }
    
    return render(request, 'restaurant_admin/menu/dashboard.html', context)

@login_required
@user_passes_test(is_admin)
def menu_add(request, restaurant_slug=None):
    # Resolve restaurant
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            # Form enforces category/subcategory; save directly
            menu_item = form.save()
            messages.success(request, 'Menu item added successfully!')
            return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = MenuItemForm(restaurant=restaurant)
    
    return render(request, 'restaurant_admin/menu/form.html', {
        'form': form, 
        'title': 'Add Menu Item',
        'categories': Category.objects.all(),
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def menu_bulk_add(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        import json
        raw_json = ''
        if 'json_file' in request.FILES:
            raw_json = request.FILES['json_file'].read().decode('utf-8')
        elif 'json_text' in request.POST:
            raw_json = request.POST.get('json_text', '').strip()

        if not raw_json:
            messages.error(request, 'Please provide a valid JSON file or paste JSON content.')
            return redirect('restaurant_admin:menu_bulk_add', restaurant_slug=restaurant.slug)

        try:
            data = json.loads(raw_json)
        except Exception as e:
            messages.error(request, f'Invalid JSON format: {str(e)}')
            return redirect('restaurant_admin:menu_bulk_add', restaurant_slug=restaurant.slug)

        if not isinstance(data, list):
            if isinstance(data, dict):
                data = [data]
            else:
                messages.error(request, 'JSON content must be an array of menu item objects.')
                return redirect('restaurant_admin:menu_bulk_add', restaurant_slug=restaurant.slug)

        created_count = 0
        updated_count = 0
        error_list = []

        for idx, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                error_list.append(f"Item #{idx}: Must be a JSON object.")
                continue

            name = str(item.get('name', '')).strip()
            if not name:
                error_list.append(f"Item #{idx}: Missing required 'name' field.")
                continue

            raw_price = item.get('price')
            try:
                price = float(raw_price)
                if price < 0:
                    raise ValueError("Price cannot be negative")
            except (ValueError, TypeError):
                error_list.append(f"Item #{idx} ({name}): Invalid price '{raw_price}'.")
                continue

            cat_name = str(item.get('category', 'General')).strip() or 'General'
            category, _ = Category.objects.get_or_create(
                name=cat_name,
                restaurant=restaurant
            )

            subcat_name = str(item.get('subcategory', '')).strip()
            subcategory = None
            if subcat_name:
                subcategory, _ = SubCategory.objects.get_or_create(
                    name=subcat_name,
                    category=category
                )

            description = str(item.get('description', '')).strip()
            tax_type = str(item.get('tax_type', 'GST_FOOD')).strip().upper()
            valid_tax_types = [choice[0] for choice in MenuItem.TaxType.choices]
            if tax_type not in valid_tax_types:
                tax_type = MenuItem.TaxType.GST_FOOD

            availability = bool(item.get('availability', True))

            menu_item, created = MenuItem.objects.update_or_create(
                name=name,
                restaurant=restaurant,
                defaults={
                    'description': description,
                    'price': price,
                    'tax_type': tax_type,
                    'category': category,
                    'subcategory': subcategory,
                    'availability': availability,
                }
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        if created_count > 0 or updated_count > 0:
            messages.success(request, f"Successfully imported menu items ({created_count} created, {updated_count} updated).")

        if error_list:
            for err in error_list[:10]:
                messages.warning(request, err)
            if len(error_list) > 10:
                messages.warning(request, f"...and {len(error_list) - 10} more errors.")

        return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)

    import json
    sample_json = json.dumps([
        {
            "name": "Paneer Butter Masala",
            "description": "Rich cottage cheese in tomato butter gravy",
            "price": 280.00,
            "tax_type": "GST_FOOD",
            "category": "Main Course",
            "subcategory": "North Indian",
            "availability": True
        },
        {
            "name": "Kingfisher Premium Beer 650ml",
            "description": "Chilled lager beer",
            "price": 220.00,
            "tax_type": "VAT_BEER",
            "category": "Bar",
            "subcategory": "Beer",
            "availability": True
        },
        {
            "name": "Single Malt Whiskey 30ml",
            "description": "Premium aged scotch whiskey",
            "price": 450.00,
            "tax_type": "VAT_IMFL",
            "category": "Bar",
            "subcategory": "Spirits",
            "availability": True
        }
    ], indent=2)

    return render(request, 'restaurant_admin/menu/bulk_add.html', {
        'title': 'Bulk Import Menu Items (JSON)',
        'sample_json': sample_json,
        'restaurant': restaurant,
        'active_menu': 'menu_list',
    })

@login_required
@user_passes_test(is_admin)
def menu_edit(request, pk, restaurant_slug=None):
    # Resolve restaurant and ensure object belongs to it
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, instance=menu_item, restaurant=restaurant)
        if form.is_valid():
            menu_item = form.save()
            messages.success(request, 'Menu item updated successfully!')
            return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = MenuItemForm(instance=menu_item, restaurant=restaurant)
        
    return render(request, 'restaurant_admin/menu/form.html', {
        'form': form, 
        'title': 'Edit Menu Item',
        'categories': Category.objects.all(),
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def menu_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        menu_item.delete()
        messages.success(request, 'Menu item deleted successfully!')
        return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': menu_item, 'restaurant': restaurant})
@login_required
@user_passes_test(is_admin)
def order_list(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    query_params = request.GET.urlencode()
    redirect_url = reverse('restaurant_admin:order_records', kwargs={'restaurant_slug': restaurant.slug})
    if query_params:
        redirect_url += f"?{query_params}"
    return redirect(redirect_url)

    # Filter by status if provided
    status = request.GET.get('status')
    if status:
        orders_qs = orders_qs.filter(status=status)

    # Filter by date period (empty = all time, no hidden date restriction)
    period = request.GET.get('period', '')
    now = timezone.now()
    if period == 'today':
        orders_qs = orders_qs.filter(order_time__date=now.date())
    elif period == 'week':
        orders_qs = orders_qs.filter(order_time__gte=now - timedelta(days=7))
    elif period == 'month':
        orders_qs = orders_qs.filter(order_time__gte=now - timedelta(days=30))


    # Search functionality
    search_query = request.GET.get('q', '')
    if search_query:
        orders_qs = orders_qs.filter(
            Q(customer_name__icontains=search_query)
            | Q(phone__icontains=search_query)
            | Q(id__icontains=search_query)
        )

    # Paginate
    paginator = Paginator(orders_qs, 10)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Determine selected order (from query param or first item on page)
    selected_order_id = request.GET.get('order_id')
    selected_order = None
    if selected_order_id:
        try:
            selected_order = next(
                (o for o in page_obj.object_list if str(o.id) == str(selected_order_id)),
                None,
            )
        except Exception:
            selected_order = None

    # Compute totals for the selected order using dynamic settings
    subtotal = Decimal('0.00')
    cgst_percentage = Decimal('0.00')
    sgst_percentage = Decimal('0.00')
    service_percentage = Decimal('0.00')
    cgst_amount = Decimal('0.00')
    sgst_amount = Decimal('0.00')
    service_amount = Decimal('0.00')
    total = Decimal('0.00')
    discount_amount = Decimal('0.00')
    if selected_order:
        settings_obj = RestaurantSettings.get_settings(restaurant)
        cgst_percentage = Decimal(str(settings_obj.cgst_percentage))
        sgst_percentage = Decimal(str(settings_obj.sgst_percentage))
        service_percentage = Decimal(str(settings_obj.service_charge_percentage))
        subtotal = sum((item.subtotal for item in selected_order.items.all()), Decimal('0.00'))
        cgst_amount = (subtotal * cgst_percentage) / Decimal('100')
        sgst_amount = (subtotal * sgst_percentage) / Decimal('100')
        service_amount = (subtotal * service_percentage) / Decimal('100')
        try:
            discount_amount = Decimal(str(getattr(selected_order, 'discount_amount', 0) or 0))
        except Exception:
            discount_amount = Decimal('0.00')
        total = subtotal + cgst_amount + sgst_amount + service_amount - discount_amount

    # Compute a display total for each order in the list (subtotal + taxes + service - discount)
    try:
        settings_obj_all = RestaurantSettings.get_settings(restaurant)
        list_cgst_p = Decimal(str(settings_obj_all.cgst_percentage))
        list_sgst_p = Decimal(str(settings_obj_all.sgst_percentage))
        list_service_p = Decimal(str(settings_obj_all.service_charge_percentage))
    except Exception:
        list_cgst_p = list_sgst_p = list_service_p = Decimal('0.00')
    try:
        for o in page_obj.object_list:
            o_sub = sum((it.subtotal for it in o.items.all()), Decimal('0.00'))
            o_cgst = (o_sub * list_cgst_p) / Decimal('100')
            o_sgst = (o_sub * list_sgst_p) / Decimal('100')
            o_svc  = (o_sub * list_service_p) / Decimal('100')
            o_disc = Decimal(str(getattr(o, 'discount_amount', 0) or 0))
            o.calc_total = o_sub + o_cgst + o_sgst + o_svc - o_disc
    except Exception:
        pass

    # ── Lightweight poll endpoint (replaces SSE thread-hold) ──
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' and request.GET.get('poll'):
        total_orders = Order.objects.filter(restaurant=restaurant).count()
        return JsonResponse({'total_orders': total_orders})

    # Status choices for the filter dropdown (use model choices)
    status_choices = list(Order.OrderStatus.choices)

    # Preserve current filters/search in URL params (exclude order_id and page)
    url_params_qs = request.GET.copy()
    url_params_qs.pop('order_id', None)
    url_params_qs.pop('page', None)
    url_params = url_params_qs.urlencode()

    # Notification sound URL
    try:
        notif_url = RestaurantSettings.get_settings(restaurant).notification_sound.url
    except Exception:
        notif_url = ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''

    return render(
        request,
        'restaurant_admin/orders/list.html',
        {
            'orders': page_obj,
            'active_menu': 'order_list',
            'search_query': search_query,
            'status_filter': status,
            'selected_status': status,
            'status_choices': status_choices,
            'selected_order': selected_order,
            'period': period,
            'url_params': url_params,
            'restaurant': restaurant,
            'minutes_choices': [5, 10, 15, 20, 25, 30, 45, 60, 90, 120],
            'rslug': getattr(restaurant, 'slug', None),
            'notification_sound_abs_url': notif_abs_url,
        },
    )


def _filter_order_queryset(request, restaurant):
    profile = getattr(request.user, 'restaurant_profile', None)
    branch = profile.branch if profile else None

    orders_qs = Order.objects.filter(restaurant=restaurant)
    if branch:
        orders_qs = orders_qs.filter(branch=branch)

    # Search filter
    q = request.GET.get('q', '').strip()
    if q:
        orders_qs = orders_qs.filter(
            Q(id__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(phone__icontains=q) |
            Q(table__name__icontains=q)
        )

    # Status filter
    status = request.GET.get('status', '').strip()
    if status:
        orders_qs = orders_qs.filter(status=status)

    # Payment Status filter
    payment_status = request.GET.get('payment_status', '').strip()
    if payment_status:
        orders_qs = orders_qs.filter(payment_status=payment_status)

    # Payment Method filter
    payment_method = request.GET.get('payment_method', '').strip()
    if payment_method:
        orders_qs = orders_qs.filter(payment_method=payment_method)

    # Date / Period filtering
    period = request.GET.get('period', '').strip()
    start_date_str = request.GET.get('start_date', '').strip()
    end_date_str = request.GET.get('end_date', '').strip()

    now = timezone.now()

    if period == 'today':
        orders_qs = orders_qs.filter(order_time__date=now.date())
    elif period == 'week':
        orders_qs = orders_qs.filter(order_time__gte=now - timedelta(days=7))
    elif period == 'month':
        orders_qs = orders_qs.filter(order_time__gte=now - timedelta(days=30))
    elif period == 'year':
        orders_qs = orders_qs.filter(order_time__year=now.year)
    elif period == 'custom' or (start_date_str and end_date_str):
        if start_date_str:
            try:
                start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
                orders_qs = orders_qs.filter(order_time__gte=start_dt)
            except ValueError:
                pass
        if end_date_str:
            try:
                end_dt = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
                orders_qs = orders_qs.filter(order_time__lt=end_dt)
            except ValueError:
                pass

    return orders_qs.select_related('table', 'table__zone', 'restaurant', 'branch').prefetch_related('items', 'items__menu_item').order_by('-order_time')


@login_required
@user_passes_test(is_admin)
def order_records(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    orders_qs = _filter_order_queryset(request, restaurant)

    # Compute Summary Stats
    total_orders = orders_qs.count()
    total_revenue = orders_qs.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    paid_orders = orders_qs.filter(payment_status=Order.PaymentStatus.PAID)
    paid_revenue = paid_orders.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    avg_order_value = (total_revenue / Decimal(total_orders)) if total_orders > 0 else Decimal('0.00')

    # Paginate
    paginator = Paginator(orders_qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Build URL query params preserving filters for pagination
    url_params_qs = request.GET.copy()
    url_params_qs.pop('page', None)
    url_params = url_params_qs.urlencode()

    return render(request, 'restaurant_admin/orders/records.html', {
        'orders': page_obj,
        'active_menu': 'order_records',
        'restaurant': restaurant,
        'rslug': getattr(restaurant, 'slug', None),
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'paid_revenue': paid_revenue,
        'avg_order_value': avg_order_value,
        'period': request.GET.get('period', ''),
        'start_date': request.GET.get('start_date', ''),
        'end_date': request.GET.get('end_date', ''),
        'status_filter': request.GET.get('status', ''),
        'payment_status_filter': request.GET.get('payment_status', ''),
        'payment_method_filter': request.GET.get('payment_method', ''),
        'search_query': request.GET.get('q', ''),
        'order_statuses': Order.OrderStatus.choices,
        'payment_statuses': Order.PaymentStatus.choices,
        'payment_methods': Order.PaymentMethod.choices,
        'url_params': url_params,
    })


@login_required
@user_passes_test(is_admin)
def order_records_export(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    orders_qs = _filter_order_queryset(request, restaurant)

    headers = [
        "Order #", "Date & Time", "Customer Name", "Phone", "Table #",
        "Order Type", "Items Summary", "Food Subtotal", "Liquor Subtotal", "Gross Subtotal",
        "Discount", "CGST Amount", "SGST Amount", "State VAT Amount", "Total Amount",
        "Payment Method", "Payment Status", "Order Status"
    ]

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Order Audit Report"

        title_font = Font(name="Calibri", size=15, bold=True, color="1F2937")
        sub_font = Font(name="Calibri", size=10, italic=True, color="6B7280")
        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        data_font = Font(name="Calibri", size=10)
        total_font = Font(name="Calibri", size=10, bold=True, color="1E3A8A")
        total_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin', color='E5E7EB'),
            right=Side(style='thin', color='E5E7EB'),
            top=Side(style='thin', color='E5E7EB'),
            bottom=Side(style='thin', color='E5E7EB')
        )

        ws.append([f"Order Audit Report - {restaurant.name}"])
        ws.cell(row=1, column=1).font = title_font

        period_label = request.GET.get('period') or 'All Time'
        ws.append([f"Export Date: {timezone.now().strftime('%Y-%m-%d %H:%M')} | Filter Period: {period_label.upper()}"])
        ws.cell(row=2, column=1).font = sub_font
        ws.append([])

        ws.append(headers)
        header_row = 4
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=header_row, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        current_row = 5

        sum_food = Decimal('0.00')
        sum_liquor = Decimal('0.00')
        sum_gross = Decimal('0.00')
        sum_discount = Decimal('0.00')
        sum_cgst = Decimal('0.00')
        sum_sgst = Decimal('0.00')
        sum_vat = Decimal('0.00')
        sum_total = Decimal('0.00')

        for o in orders_qs.prefetch_related('items', 'items__menu_item'):
            b = o.calculate_bill_breakdown()
            
            if o.table:
                table_str = f"T-{o.table.table_number:02d}" if isinstance(o.table.table_number, int) else f"Table {o.table.table_number}"
            elif o.table_number:
                table_str = f"T-{o.table_number:02d}"
            else:
                table_str = "N/A"

            order_type = "Dine-In" if (o.table or o.table_number) else "Takeaway"
            items_summary = " | ".join([f"{item.quantity}x {item.menu_item.name if item.menu_item else 'Item'}" for item in o.items.all()])

            if o.payment_status == 'P':
                p_status = "Paid"
            elif o.payment_status == 'F':
                p_status = "Failed"
            elif o.payment_status == 'R':
                p_status = "Refunded"
            elif o.status == 'S':
                p_status = "Paid"
            elif o.status == 'C':
                p_status = "Cancelled"
            else:
                p_status = "Pending"

            p_method = o.get_payment_method_display() or "Pay at Counter"

            ws.append([
                o.order_number,
                o.order_time.strftime('%Y-%m-%d %H:%M'),
                o.customer_name or 'N/A',
                o.phone or 'N/A',
                table_str,
                order_type,
                items_summary,
                float(b['food_subtotal']),
                float(b['liquor_subtotal']),
                float(b['gross_subtotal']),
                float(b['discount_amount']),
                float(b['cgst']),
                float(b['sgst']),
                float(b['vat']),
                float(b['final_total']),
                p_method,
                p_status,
                o.get_status_display()
            ])

            sum_food += b['food_subtotal']
            sum_liquor += b['liquor_subtotal']
            sum_gross += b['gross_subtotal']
            sum_discount += b['discount_amount']
            sum_cgst += b['cgst']
            sum_sgst += b['sgst']
            sum_vat += b['vat']
            sum_total += b['final_total']

            for col_num in range(1, len(headers) + 1):
                cell = ws.cell(row=current_row, column=col_num)
                cell.font = data_font
                cell.border = thin_border
                if 8 <= col_num <= 15:
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")
                elif col_num in [1, 2, 5, 6, 16, 17, 18]:
                    cell.alignment = Alignment(horizontal="center")

            current_row += 1

        # Summary Row
        ws.append([
            "TOTAL", "", "", "", "", "", f"{orders_qs.count()} Orders",
            float(sum_food), float(sum_liquor), float(sum_gross), float(sum_discount),
            float(sum_cgst), float(sum_sgst), float(sum_vat), float(sum_total),
            "", "", ""
        ])
        summary_row = current_row
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=summary_row, column=col_num)
            cell.font = total_font
            cell.fill = total_fill
            cell.border = thin_border
            if 8 <= col_num <= 15:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right")

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        filename = f"Order_Report_{restaurant.slug}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response

    except Exception as e:
        # CSV Fallback
        import csv
        response = HttpResponse(content_type='text/csv')
        filename = f"Order_Report_{restaurant.slug}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(headers)

        for o in orders_qs.prefetch_related('items', 'items__menu_item'):
            b = o.calculate_bill_breakdown()
            if o.table:
                table_str = f"T-{o.table.table_number:02d}" if isinstance(o.table.table_number, int) else f"Table {o.table.table_number}"
            elif o.table_number:
                table_str = f"T-{o.table_number:02d}"
            else:
                table_str = "N/A"

            order_type = "Dine-In" if (o.table or o.table_number) else "Takeaway"
            items_summary = " | ".join([f"{item.quantity}x {item.menu_item.name if item.menu_item else 'Item'}" for item in o.items.all()])

            if o.payment_status == 'P':
                p_status = "Paid"
            elif o.payment_status == 'F':
                p_status = "Failed"
            elif o.payment_status == 'R':
                p_status = "Refunded"
            elif o.status == 'S':
                p_status = "Paid"
            elif o.status == 'C':
                p_status = "Cancelled"
            else:
                p_status = "Pending"

            p_method = o.get_payment_method_display() or "Pay at Counter"

            writer.writerow([
                o.order_number,
                o.order_time.strftime('%Y-%m-%d %H:%M'),
                o.customer_name or 'N/A',
                o.phone or 'N/A',
                table_str,
                order_type,
                items_summary,
                f"{b['food_subtotal']:.2f}",
                f"{b['liquor_subtotal']:.2f}",
                f"{b['gross_subtotal']:.2f}",
                f"{b['discount_amount']:.2f}",
                f"{b['cgst']:.2f}",
                f"{b['sgst']:.2f}",
                f"{b['vat']:.2f}",
                f"{b['final_total']:.2f}",
                p_method,
                p_status,
                o.get_status_display()
            ])
        return response


@login_required
@user_passes_test(is_admin)
def orders_stream(request, restaurant_slug=None):
    """Server-Sent Events stream for new orders for the admin's restaurant."""
    # Resolve restaurant
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    slug = getattr(restaurant, 'slug', None)
    if not slug:
        return HttpResponseBadRequest('Missing restaurant slug')

    q, closed = subscribe(slug)

    def event_stream():
        import time
        last_ping = 0
        try:
            # Initial comment to open stream
            yield sse_comment(f"stream opened for {slug}")
            while not closed.is_set():
                # Periodic keepalive every 15s
                now = time.time()
                if now - last_ping > 15:
                    last_ping = now
                    yield sse_comment('keepalive')
                # Try to get next payload without blocking long
                from queue import Empty
                try:
                    data = q.get(timeout=1.0)
                except Empty:
                    data = None
                if data is None:
                    continue
                yield sse_event(data, event='new_order')
        finally:
            unsubscribe(slug, q, closed)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # helpful for some proxies
    return response

# Order Detail View
@login_required
@user_passes_test(is_admin)
def order_detail(request, pk, restaurant_slug=None):
    # Scope to restaurant
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    order = get_object_or_404(Order.objects.prefetch_related('items', 'items__menu_item').filter(restaurant=restaurant), pk=pk)

    # Calculate subtotal, taxes, round off, and totals using Order breakdown
    breakdown = order.calculate_bill_breakdown(bill_type='combined')
    subtotal = breakdown['gross_subtotal']
    cgst_percentage = breakdown['cgst_percentage']
    sgst_percentage = breakdown['sgst_percentage']
    service_percentage = breakdown['service_charge_percentage']
    cgst_amount = breakdown['cgst']
    sgst_amount = breakdown['sgst']
    service_amount = breakdown['service_charge']
    discount_amount = breakdown['discount_amount']
    round_off_amount = breakdown['round_off_amount']
    total = breakdown['final_total']

    # Notification sound URL
    try:
        # pyrefly: ignore [unknown-name]
        notif_url = settings_obj.notification_sound.url
    except Exception:
        notif_url = ''
    notif_abs_url = request.build_absolute_uri(notif_url) if notif_url else ''

    return render(request, 'restaurant_admin/orders/detail.html', {
        'order': order,
        'active_menu': 'order_list',
        'subtotal': subtotal,
        'cgst_percentage': cgst_percentage,
        'sgst_percentage': sgst_percentage,
        'service_percentage': service_percentage,
        'cgst_amount': cgst_amount,
        'sgst_amount': sgst_amount,
        'service_amount': service_amount,
        'discount_amount': discount_amount,
        'round_off_amount': round_off_amount,
        'total': total,
        'minutes_choices': [5, 10, 15, 20, 25, 30, 45, 60, 90, 120],
        'restaurant': restaurant,
        'rslug': getattr(restaurant, 'slug', None),
        'notification_sound_abs_url': notif_abs_url,
    })


@login_required
@user_passes_test(is_admin)
def order_set_prep_time(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    order = get_object_or_404(Order, pk=pk, restaurant=restaurant)

    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')

    try:
        minutes = int(request.POST.get('minutes', ''))
        if minutes <= 0 or minutes > 240:
            return JsonResponse({'success': False, 'error': 'Minutes must be between 1 and 240'}, status=400)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid minutes'}, status=400)

    now = timezone.now()
    order.prep_minutes = minutes
    order.prep_start = now
    order.ready_at = now + timezone.timedelta(minutes=minutes)
    order.save(update_fields=['prep_minutes', 'prep_start', 'ready_at'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Preparation time set to {minutes} minutes.',
            'prep_minutes': minutes,
            'prep_start': order.prep_start,
            'ready_at': order.ready_at,
        })

    messages.success(request, f'Preparation time set to {minutes} minutes for order #{order.branch_order_number or order.id}.')
    return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)


@csrf_exempt
@login_required
@user_passes_test(is_admin)
def order_settle_api(request, pk, restaurant_slug=None):
    if request.method not in ['POST', 'PATCH']:
        return JsonResponse({'success': False, 'error': 'POST or PATCH method required'}, status=405)

    if restaurant_slug:
        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    else:
        try:
            restaurant = request.user.restaurant_profile.restaurant
        except Exception:
            return JsonResponse({'success': False, 'error': 'No restaurant profile found'}, status=403)

    order = get_object_or_404(Order, pk=pk, restaurant=restaurant)

    data = {}
    if request.body:
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST.dict()
    else:
        data = request.POST.dict()

    def safe_dec(val, default='0.00'):
        try:
            return Decimal(str(val or '0'))
        except Exception:
            return Decimal(default)

    payment_method = data.get('payment_method') or 'cash'
    payments_input = data.get('payments')
    valid_payments = list(order.payments or [])

    advance_paid = order.advance_amount_paid or Decimal('0.00')
    net_payable_target = max(Decimal('0.00'), order.total_amount - advance_paid)

    if isinstance(payments_input, list) and len(payments_input) > 0:
        valid_payments = []
        for p in payments_input:
            amt = safe_dec(p.get('amount'))
            if amt > 0:
                valid_payments.append({
                    'method': str(p.get('method') or payment_method),
                    'amount': float(amt)
                })
        paid_val = sum(safe_dec(p['amount']) for p in valid_payments)
    elif data.get('paid_amount') is not None and str(data.get('paid_amount')).strip() != '':
        paid_val = safe_dec(data.get('paid_amount'))
        valid_payments = [{'method': payment_method, 'amount': float(paid_val)}]
    elif data.get('amount') is not None and str(data.get('amount')).strip() != '':
        added_amt = safe_dec(data.get('amount'))
        paid_val = (order.paid_amount or Decimal('0.00')) + added_amt
        valid_payments.append({'method': payment_method, 'amount': float(added_amt)})
    else:
        paid_val = net_payable_target
        valid_payments = [{'method': payment_method, 'amount': float(net_payable_target)}]

    due_val = max(Decimal('0.00'), net_payable_target - paid_val)

    if due_val <= Decimal('0.00'):
        order.payment_status = Order.PaymentStatus.PAID
        order.due_amount = Decimal('0.00')
        order.paid_amount = paid_val
    elif (paid_val + advance_paid) > Decimal('0.00'):
        order.payment_status = Order.PaymentStatus.PARTIALLY_PAID
        order.paid_amount = paid_val
        order.due_amount = due_val
    else:
        order.payment_status = Order.PaymentStatus.UNPAID
        order.paid_amount = Decimal('0.00')
        order.due_amount = net_payable_target

    if order.advance_receipt and order.advance_receipt.status == 'active':
        order.advance_receipt.status = 'redeemed'
        order.advance_receipt.order = order
        order.advance_receipt.save(update_fields=['status', 'order'])

    order.payments = valid_payments
    order.payment_method = payment_method
    if order.status == Order.OrderStatus.PENDING:
        order.status = Order.OrderStatus.SERVED

    gstin_no = (data.get('gstin_no') or data.get('gstin') or '').strip()
    excise_bill_no = (data.get('excise_bill_no') or data.get('excise') or '').strip()
    if gstin_no:
        order.gstin_no = gstin_no
    if excise_bill_no:
        order.excise_bill_no = excise_bill_no

    if not order.party and order.customer_name and order.customer_name.lower() not in ['guest', 'walk-in guest']:
        party, _ = Party.objects.get_or_create(
            restaurant=restaurant,
            name__iexact=order.customer_name,
            defaults={
                'name': order.customer_name,
                'party_type': Party.PartyType.CUSTOMER,
                'phone': order.phone,
            }
        )
        order.party = party

    order.save()

    return JsonResponse({
        'success': True,
        'message': f'Order #{order.branch_order_number or order.id} payment updated ({order.get_payment_status_display()}).',
        'order_id': order.id,
        'payment_status': order.payment_status,
        'payment_method': order.payment_method,
        'paid_amount': str(order.paid_amount),
        'due_amount': str(order.due_amount)
    })


# Coupon Management
@login_required
@user_passes_test(is_admin)
def coupon_list(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    q = (request.GET.get('q') or '').strip()
    coupons = Coupon.objects.filter(restaurant=restaurant) | Coupon.objects.filter(restaurant__isnull=True)
    if q:
        coupons = coupons.filter(Q(code__icontains=q) | Q(description__icontains=q))
    coupons = coupons.order_by('-updated_at')

    return render(request, 'restaurant_admin/coupons/list.html', {
        'coupons': coupons,
        'active_menu': 'coupons',
        'search_query': q,
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def coupon_add(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'POST':
        form = CouponForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Coupon created successfully!')
            return redirect('restaurant_admin:coupon_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CouponForm(restaurant=restaurant)

    return render(request, 'restaurant_admin/coupons/form.html', {
        'form': form,
        'title': 'Add Coupon',
        'active_menu': 'coupons',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def coupon_edit(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    coupon = get_object_or_404(Coupon, pk=pk)
    # Only allow editing if global (restaurant null) or matches restaurant
    if coupon.restaurant_id and coupon.restaurant_id != restaurant.id and not request.user.is_superuser:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('Not allowed')

    if request.method == 'POST':
        form = CouponForm(request.POST, instance=coupon, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Coupon updated successfully!')
            return redirect('restaurant_admin:coupon_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CouponForm(instance=coupon, restaurant=restaurant)

    return render(request, 'restaurant_admin/coupons/form.html', {
        'form': form,
        'title': f'Edit Coupon: {coupon.code}',
        'active_menu': 'coupons',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def coupon_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    coupon = get_object_or_404(Coupon, pk=pk)
    if request.method == 'POST':
        # Scope enforcement
        if coupon.restaurant_id and coupon.restaurant_id != restaurant.id and not request.user.is_superuser:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden('Not allowed')
        coupon.delete()
        messages.success(request, 'Coupon deleted successfully!')
        return redirect('restaurant_admin:coupon_list', restaurant_slug=restaurant.slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {
        'object': coupon,
        'restaurant': restaurant,
        'title': f'Delete Coupon {coupon.code}'
    })


@login_required
@user_passes_test(is_admin)
def order_invoice(request, pk, restaurant_slug=None):
    # Ensure the order belongs to current admin's restaurant
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    order = get_object_or_404(
        Order.objects.prefetch_related('items', 'items__menu_item').filter(restaurant=restaurant),
        pk=pk,
    )

    bill_type = request.GET.get('type', 'food').lower()
    if bill_type not in ['food', 'liquor']:
        bill_type = 'food'  # Combined bill disabled for separate billing compliance

    # Totals using order bill breakdown engine
    breakdown = order.calculate_bill_breakdown(bill_type=bill_type)

    # Filter items according to bill_type
    order_items = list(order.items.all())
    if bill_type == 'food':
        order_items = [it for it in order_items if not it.menu_item or it.menu_item.tax_type == MenuItem.TaxType.GST_FOOD]
    elif bill_type == 'liquor':
        order_items = [it for it in order_items if it.menu_item and it.menu_item.tax_type != MenuItem.TaxType.GST_FOOD]

    # Landing info (logo, address, phone)
    landing = None
    try:
        landing = RestaurantLanding.objects.select_related('restaurant').filter(restaurant=restaurant).first()
    except Exception:
        landing = None

    branch = order.branch or restaurant.branches.first()

    context = {
        'restaurant': restaurant,
        'branch': branch,
        'landing': landing,
        'order': order,
        'order_items': order_items,
        'bill_type': bill_type,
        'breakdown': breakdown,
        'subtotal': breakdown['gross_subtotal'],
        'food_subtotal': breakdown['food_subtotal'],
        'beer_subtotal': breakdown['beer_subtotal'],
        'imfl_subtotal': breakdown['imfl_subtotal'],
        'wine_subtotal': breakdown['wine_subtotal'],
        'liquor_subtotal': breakdown['liquor_subtotal'],
        'discount_amount': breakdown['discount_amount'],
        'cgst_percentage': breakdown['cgst_percentage'],
        'sgst_percentage': breakdown['sgst_percentage'],
        'gst_percentage': breakdown['cgst_percentage'] + breakdown['sgst_percentage'],
        'service_percentage': breakdown['service_charge_percentage'],
        'vat_beer_percentage': breakdown['vat_beer_percentage'],
        'vat_imfl_percentage': breakdown['vat_imfl_percentage'],
        'vat_wine_percentage': breakdown['vat_wine_percentage'],
        'vat_percentage': breakdown['vat_percentage'],
        'cgst_amount': breakdown['cgst'],
        'sgst_amount': breakdown['sgst'],
        'service_amount': breakdown['service_charge'],
        'beer_vat': breakdown['beer_vat'],
        'imfl_vat': breakdown['imfl_vat'],
        'wine_vat': breakdown['wine_vat'],
        'gen_vat': breakdown['gen_vat'],
        'vat_amount': breakdown['vat'],
        'gst_base': breakdown['gst_base'],
        'round_off_amount': breakdown['round_off_amount'],
        'total': breakdown['final_total'],
    }

    return render(request, 'restaurant_admin/orders/invoice.html', context)

@login_required
def order_kot(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except Exception:
        restaurant = getattr(request.user, 'restaurant', None)

    if not restaurant:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    order = get_object_or_404(
        Order.objects.prefetch_related('items', 'items__menu_item').filter(restaurant=restaurant),
        pk=pk,
    )

    subtotal = sum((item.subtotal for item in order.items.all()), Decimal('0.00'))
    settings_obj = RestaurantSettings.get_settings(restaurant)
    cgst_percentage = Decimal(str(settings_obj.cgst_percentage))
    sgst_percentage = Decimal(str(settings_obj.sgst_percentage))
    service_percentage = Decimal(str(settings_obj.service_charge_percentage))
    cgst_amount = (subtotal * cgst_percentage) / Decimal('100')
    sgst_amount = (subtotal * sgst_percentage) / Decimal('100')
    service_amount = (subtotal * service_percentage) / Decimal('100')
    discount_amount = Decimal(str(order.discount_amount or 0))
    total = max(Decimal('0.00'), subtotal + cgst_amount + sgst_amount + service_amount - discount_amount)

    food_items, bar_items = order.get_food_and_bar_items()
    total_food_qty = sum(item.quantity for item in food_items)
    total_bar_qty = sum(item.quantity for item in bar_items)

    table_obj = order.table
    zone_obj = table_obj.zone if (table_obj and hasattr(table_obj, 'zone')) else None
    total_kitchen_items = sum(item.quantity for item in order.items.all())

    context = {
        'restaurant': restaurant,
        'order': order,
        'table_obj': table_obj,
        'zone_obj': zone_obj,
        'food_items': food_items,
        'bar_items': bar_items,
        'total_food_qty': total_food_qty,
        'total_bar_qty': total_bar_qty,
        'has_food': len(food_items) > 0,
        'has_bar': len(bar_items) > 0,
        'total_kitchen_items': total_kitchen_items,
        'subtotal': subtotal,
        'cgst_amount': cgst_amount,
        'sgst_amount': sgst_amount,
        'service_amount': service_amount,
        'discount_amount': discount_amount,
        'total': total,
    }

    return render(request, 'restaurant_admin/orders/kot.html', context)

@login_required
@user_passes_test(is_admin)
def order_update_status(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    try:
        order = get_object_or_404(Order, pk=pk, restaurant=restaurant)
        
        if request.method == 'POST':
            new_status = request.POST.get('status')
            
            # Get the valid statuses from the Order model's choices
            status_choices = dict(Order.OrderStatus.choices)
            
            if new_status in status_choices:
                order.status = new_status
                order.save()
                
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': f'Order #{order.branch_order_number or order.id} status updated to {status_choices[new_status]}',
                        'status': order.get_status_display(),
                        'status_class': {
                            'P': 'warning',
                            'S': 'success',
                            'C': 'danger'
                        }.get(order.status, 'secondary')
                    })
                else:
                    messages.success(request, f'Order #{order.branch_order_number or order.id} status updated to {status_choices[new_status]}')
                    return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)
            else:
                error_msg = f'Invalid status: {new_status}. Valid statuses are: {list(status_choices.values())}'
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)
                
    except Exception as e:
        import traceback
        error_msg = f'Error updating order status: {str(e)}\n{traceback.format_exc()}'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=500)
        messages.error(request, 'An error occurred while updating the order status.')
        return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)
    
    return redirect('restaurant_admin:order_list', restaurant_slug=restaurant.slug)

# Category Management
@login_required
@user_passes_test(is_admin)
def category_list(request, restaurant_slug=None):
    # Resolve restaurant
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    # First get the count of subcategories for each category (scoped)
    categories = Category.objects.filter(restaurant=restaurant).annotate(
        subcategory_count=Count('subcategories', distinct=True)
    )
    
    # Then get the count of menu items for each category
    categories = categories.annotate(
        menu_item_count=Count('subcategories__menu_items', distinct=True)
    )
    
    return render(request, 'restaurant_admin/categories/list.html', {
        'categories': categories,
        'active_menu': 'category_list',  # For highlighting active menu item
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def category_detail(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    category = get_object_or_404(Category.objects.prefetch_related('subcategories').filter(restaurant=restaurant), pk=pk)
    subcategories = category.subcategories.all()
    
    return render(request, 'restaurant_admin/categories/detail.html', {
        'category': category,
        'subcategories': subcategories,
        'active_menu': 'category_list',
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def category_add(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category added successfully!')
            return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    else:
        form = CategoryForm(restaurant=restaurant)
    return render(request, 'restaurant_admin/categories/form.html', {'form': form, 'title': 'Add Category'})

@login_required
@user_passes_test(is_admin)
def category_edit(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    category = get_object_or_404(Category, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES, instance=category, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category updated successfully!')
            return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    else:
        form = CategoryForm(instance=category, restaurant=restaurant)
    return render(request, 'restaurant_admin/categories/form.html', {'form': form, 'title': 'Edit Category'})

@login_required
@user_passes_test(is_admin)
def category_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    category = get_object_or_404(Category, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        category.delete()
        messages.success(request, 'Category deleted successfully!')
        return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': category, 'restaurant': restaurant})

# Subcategory Management
@login_required
@user_passes_test(is_admin)
def subcategory_list(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    subcategories = SubCategory.objects.select_related('category').filter(category__restaurant=restaurant)
    return render(request, 'restaurant_admin/subcategories/list.html', {
        'subcategories': subcategories,
        'active_menu': 'subcategory_list'
    })

@login_required
@user_passes_test(is_admin)
def subcategory_add(request, category_id=None, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    category = get_object_or_404(Category, pk=category_id, restaurant=restaurant) if category_id else None
    
    if request.method == 'POST':
        form = SubCategoryForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            subcategory = form.save(commit=False)
            if category:
                subcategory.category = category
            subcategory.save()
            messages.success(request, 'Subcategory added successfully!')
            if category:
                return redirect('restaurant_admin:category_detail', restaurant_slug=restaurant.slug, pk=category.pk)
            return redirect('restaurant_admin:subcategory_list', restaurant_slug=restaurant.slug)
    else:
        initial = {'category': category} if category else {}
        form = SubCategoryForm(initial=initial, restaurant=restaurant)
    
    return render(request, 'restaurant_admin/subcategories/form.html', {
        'form': form,
        'title': 'Add Subcategory',
        'category': category,
        'active_menu': 'subcategory_list'
    })

@login_required
@user_passes_test(is_admin)
def subcategory_edit(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    subcategory = get_object_or_404(SubCategory.objects.select_related('category'), pk=pk, category__restaurant=restaurant)
    
    if request.method == 'POST':
        form = SubCategoryForm(request.POST, request.FILES, instance=subcategory, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Subcategory updated successfully!')
            return redirect('restaurant_admin:category_detail', restaurant_slug=restaurant.slug, pk=subcategory.category.pk)
    else:
        form = SubCategoryForm(instance=subcategory, restaurant=restaurant)
    
    return render(request, 'restaurant_admin/subcategories/form.html', {
        'form': form,
        'title': 'Edit Subcategory',
        'category': subcategory.category,
        'active_menu': 'category_list',
        'restaurant': restaurant,
    })

@login_required
@user_passes_test(is_admin)
def subcategory_delete(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    subcategory = get_object_or_404(SubCategory, pk=pk, category__restaurant=restaurant)
    category_pk = subcategory.category.pk if subcategory.category else None
    
    if request.method == 'POST':
        subcategory.delete()
        messages.success(request, 'Subcategory deleted successfully!')
        if category_pk:
            return redirect('restaurant_admin:category_detail', restaurant_slug=restaurant.slug, pk=category_pk)
        return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    
    return render(request, 'restaurant_admin/confirm_delete.html', {
        'object': subcategory,
        'cancel_url': reverse_lazy('restaurant_admin:category_detail', kwargs={'restaurant_slug': restaurant.slug, 'pk': category_pk}) if category_pk else reverse_lazy('restaurant_admin:category_list', kwargs={'restaurant_slug': restaurant.slug})
    })

@require_http_methods(["GET"])
def get_subcategories(request, restaurant_slug=None):

    # 
    #     AJAX view to get subcategories for a given category.
    #     
    category_id = request.GET.get('category_id')
    if not category_id:
        return JsonResponse({'error': 'Category ID is required'}, status=400)
    
    try:
        # Optionally verify category belongs to current admin's restaurant
        if request.user.is_authenticated and hasattr(request.user, 'restaurant_profile'):
            try:
                restaurant = request.user.restaurant_profile.restaurant
                subcategories = SubCategory.objects.filter(category_id=category_id, category__restaurant=restaurant).values('id', 'name')
            except RestaurantUser.DoesNotExist:
                subcategories = SubCategory.objects.none().values('id', 'name')
        else:
            subcategories = SubCategory.objects.filter(category_id=category_id).values('id', 'name')
        return JsonResponse(list(subcategories), safe=False)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def qr_code_generator(request, restaurant_slug=None):
    """View for the QR code generator page"""
    # Get categories and menu items for the form
    if request.user.is_authenticated and hasattr(request.user, 'restaurant_profile'):
        try:
            restaurant = request.user.restaurant_profile.restaurant
            categories = Category.objects.filter(restaurant=restaurant)
            menu_items = MenuItem.objects.filter(restaurant=restaurant)
        except RestaurantUser.DoesNotExist:
            categories = Category.objects.none()
            menu_items = MenuItem.objects.none()
    else:
        categories = Category.objects.all()
        menu_items = MenuItem.objects.all()
    
    return render(request, 'restaurant_admin/qr_code_generator.html', {
        'categories': categories,
        'menu_items': menu_items,
        'active_menu': 'qr_generator',
    })


def generate_qr_code(request, menu_id=None, restaurant_slug=None):
    """
    Generate and return a QR code PNG that points to the frontend menu.
    Supports optional query params:
    - item: specific menu item id
    - category: category id (future use by frontend)
    - table: table/room identifier to prefill checkout
    The function also supports a path param menu_id (legacy) to set item.
    """
    # Compute base URL to the frontend root (strip any /admin suffix)
    base_url = request.build_absolute_uri('/').rstrip('/')
    if '/admin' in base_url:
        base_url = base_url.split('/admin')[0]
    
    # Resolve restaurant to inject slug into customer URL
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    # Gather params
    item_param = request.GET.get('item')
    category_param = request.GET.get('category')
    table_param = request.GET.get('table')

    # Backward-compat: allow path menu_id to behave like item
    if menu_id and not item_param:
        item_param = str(menu_id)

    # Build target URL (support direct-to-cart) with restaurant slug
    dest = request.GET.get('dest')
    path = f"/r/{restaurant.slug}/cart/" if dest == 'cart' else f"/r/{restaurant.slug}/"
    url = f"{base_url}{path}"
    params = {}
    if item_param:
        params['item'] = item_param
    if category_param:
        params['category'] = category_param
    if table_param:
        params['table'] = table_param

    if params:
        query = '&'.join([f"{k}={v}" for k, v in params.items()])
        url = f"{url}?{query}"

    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    # pyrefly: ignore [unexpected-keyword]
    img.save(buffer, format='PNG')
    buffer.seek(0)

    # Filename
    if item_param:
        filename = f"menu_item_{item_param}_qr.png"
    elif category_param:
        filename = f"category_{category_param}_qr.png"
    elif table_param:
        filename = f"table_{table_param}_qr.png"
    else:
        filename = "menu_qr.png"

    response = HttpResponse(buffer, content_type='image/png')
    if 'download' in request.GET:
        response['Content-Disposition'] = f'attachment; filename={filename}'
    else:
        response['Content-Disposition'] = f'inline; filename={filename}'
    return response


# Settings (Taxes and Service Charge)
@login_required
@user_passes_test(is_admin)
def settings_view(request, restaurant_slug=None):
    # Resolve restaurant for context / access consistency
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch = restaurant.branches.first()

    custom_charges = CustomCharge.objects.filter(restaurant=restaurant, is_active=True).order_by('charge_name')

    if request.method == 'POST':
        form = RestaurantSettingsForm(request.POST, request.FILES, instance=settings_obj)
        branch_form = RestaurantBranchComplianceForm(request.POST, instance=branch) if branch else None
        
        form_valid = form.is_valid()
        branch_valid = branch_form.is_valid() if branch_form else True

        if form_valid and branch_valid:
            settings_instance = form.save(commit=False)
            settings_instance.restaurant = restaurant
            settings_instance.save()
            if branch_form:
                branch_form.save()
            messages.success(request, 'Settings updated successfully.')
            return redirect('restaurant_admin:settings', restaurant_slug=restaurant_slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = RestaurantSettingsForm(instance=settings_obj)
        branch_form = RestaurantBranchComplianceForm(instance=branch) if branch else None

    return render(request, 'restaurant_admin/settings.html', {
        'form': form,
        'branch_form': branch_form,
        'branch': branch,
        'custom_charges': custom_charges,
        'active_menu': 'settings',
        'title': 'Restaurant Settings',
        'restaurant': restaurant,
        'rslug': getattr(restaurant, 'slug', None),
    })


@login_required
@user_passes_test(is_admin)
def custom_charge_save(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method == 'POST':
        charge_id = request.POST.get('charge_id')
        charge_name = request.POST.get('charge_name', '').strip()
        charge_type = request.POST.get('charge_type', 'flat')
        value = Decimal(request.POST.get('value', '0') or '0')
        is_taxable = request.POST.get('is_taxable') == 'on' or request.POST.get('is_taxable') == 'true'
        is_auto_applied = request.POST.get('is_auto_applied') == 'on' or request.POST.get('is_auto_applied') == 'true'
        
        applied_types = request.POST.getlist('applied_order_types')
        if not applied_types and request.POST.get('applied_order_types'):
            import json
            try:
                applied_types = json.loads(request.POST.get('applied_order_types'))
            except Exception:
                applied_types = ['dine_in', 'takeaway', 'delivery']
        if not applied_types:
            applied_types = ['dine_in', 'takeaway', 'delivery']

        if not charge_name:
            messages.error(request, 'Charge Name is required.')
            return redirect('restaurant_admin:settings', restaurant_slug=restaurant.slug)

        if charge_id:
            charge = get_object_or_404(CustomCharge, id=charge_id, restaurant=restaurant)
            charge.charge_name = charge_name
            charge.charge_type = charge_type
            charge.value = value
            charge.is_taxable = is_taxable
            charge.applied_order_types = applied_types
            charge.is_auto_applied = is_auto_applied
            charge.save()
            messages.success(request, f"Custom Charge '{charge.charge_name}' updated successfully.")
        else:
            charge = CustomCharge.objects.create(
                restaurant=restaurant,
                charge_name=charge_name,
                charge_type=charge_type,
                value=value,
                is_taxable=is_taxable,
                applied_order_types=applied_types,
                is_auto_applied=is_auto_applied
            )
            messages.success(request, f"Custom Charge '{charge.charge_name}' added successfully.")

    return redirect('restaurant_admin:settings', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def custom_charge_delete(request, restaurant_slug=None, pk=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    charge = get_object_or_404(CustomCharge, id=pk, restaurant=restaurant)
    charge_name = charge.charge_name
    charge.delete()
    messages.success(request, f"Custom Charge '{charge_name}' removed.")
    return redirect('restaurant_admin:settings', restaurant_slug=restaurant.slug)

@login_required
@user_passes_test(is_admin)
def sales_analytics(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    import json
    from datetime import datetime
    from decimal import Decimal
    from django.db.models import Sum, F

    range_preset = request.GET.get('range', 'this_month')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')

    now = timezone.now()
    today = now.date()

    if range_preset == 'today':
        start_date = today
        end_date = today
    elif range_preset == 'yesterday':
        start_date = today - timezone.timedelta(days=1)
        end_date = start_date
    elif range_preset == 'last_7_days':
        start_date = today - timezone.timedelta(days=6)
        end_date = today
    elif range_preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
    elif range_preset == 'custom' and start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_date = today.replace(day=1)
            end_date = today
    else:
        range_preset = 'all_time'
        start_date = None
        end_date = None

    orders_qs = Order.objects.filter(restaurant=restaurant).exclude(status__in=['C', Order.OrderStatus.DRAFT])
    if start_date and end_date:
        orders_qs = orders_qs.filter(order_time__date__range=[start_date, end_date])

    total_orders = orders_qs.count()

    total_gross_subtotal = Decimal('0.00')
    total_food_subtotal = Decimal('0.00')
    total_beer_subtotal = Decimal('0.00')
    total_imfl_subtotal = Decimal('0.00')
    total_wine_subtotal = Decimal('0.00')
    total_liquor_subtotal = Decimal('0.00')
    total_discounts = Decimal('0.00')
    total_service_charges = Decimal('0.00')
    total_cgst = Decimal('0.00')
    total_sgst = Decimal('0.00')
    total_beer_vat = Decimal('0.00')
    total_imfl_vat = Decimal('0.00')
    total_wine_vat = Decimal('0.00')
    total_gen_vat = Decimal('0.00')
    total_vat = Decimal('0.00')
    total_revenue = Decimal('0.00')

    payment_method_counts = {}
    payment_method_totals = {}

    daily_data = {}

    for o in orders_qs.prefetch_related('items', 'items__menu_item'):
        b = o.calculate_bill_breakdown()
        total_gross_subtotal += b['gross_subtotal']
        total_food_subtotal += b['food_subtotal']
        total_beer_subtotal += b['beer_subtotal']
        total_imfl_subtotal += b['imfl_subtotal']
        total_wine_subtotal += b['wine_subtotal']
        total_liquor_subtotal += b['liquor_subtotal']
        total_discounts += b['discount_amount']
        total_service_charges += b['service_charge']
        total_cgst += b['cgst']
        total_sgst += b['sgst']
        total_beer_vat += b['beer_vat']
        total_imfl_vat += b['imfl_vat']
        total_wine_vat += b['wine_vat']
        total_gen_vat += b['gen_vat']
        total_vat += b['vat']
        total_revenue += b['final_total']

        pm = o.get_payment_method_display() or 'Counter / Cash'
        payment_method_counts[pm] = payment_method_counts.get(pm, 0) + 1
        payment_method_totals[pm] = payment_method_totals.get(pm, Decimal('0.00')) + b['final_total']

        date_key = o.order_time.strftime('%Y-%m-%d')
        if date_key not in daily_data:
            daily_data[date_key] = {'revenue': Decimal('0.00'), 'gst': Decimal('0.00'), 'vat': Decimal('0.00'), 'orders': 0}
        daily_data[date_key]['revenue'] += b['final_total']
        daily_data[date_key]['gst'] += (b['cgst'] + b['sgst'])
        daily_data[date_key]['vat'] += b['vat']
        daily_data[date_key]['orders'] += 1

    total_gst = total_cgst + total_sgst
    aov = (total_revenue / Decimal(str(total_orders))) if total_orders > 0 else Decimal('0.00')

    top_items_qs = OrderItem.objects.filter(order__in=orders_qs).values('menu_item__name', 'menu_item__tax_type').annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('unit_price'))
    ).order_by('-total_sales')[:10]

    sorted_dates = sorted(daily_data.keys())
    chart_labels = sorted_dates
    chart_revenue = [float(daily_data[d]['revenue']) for d in sorted_dates]
    chart_gst = [float(daily_data[d]['gst']) for d in sorted_dates]
    chart_vat = [float(daily_data[d]['vat']) for d in sorted_dates]

    context = {
        'restaurant': restaurant,
        'active_menu': 'analytics',
        'title': 'Sales & Tax Analytics',
        'range_preset': range_preset,
        'start_date': start_date,
        'end_date': end_date,
        'total_orders': total_orders,
        'aov': aov,
        'total_gross_subtotal': total_gross_subtotal,
        'total_food_subtotal': total_food_subtotal,
        'total_beer_subtotal': total_beer_subtotal,
        'total_imfl_subtotal': total_imfl_subtotal,
        'total_wine_subtotal': total_wine_subtotal,
        'total_liquor_subtotal': total_liquor_subtotal,
        'total_discounts': total_discounts,
        'total_service_charges': total_service_charges,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_gst': total_gst,
        'total_beer_vat': total_beer_vat,
        'total_imfl_vat': total_imfl_vat,
        'total_wine_vat': total_wine_vat,
        'total_gen_vat': total_gen_vat,
        'total_vat': total_vat,
        'total_revenue': total_revenue,
        'payment_methods': [
            {'name': k, 'count': payment_method_counts[k], 'total': payment_method_totals[k]}
            for k in payment_method_counts
        ],
        'top_items': top_items_qs,
        'chart_labels_json': json.dumps(chart_labels),
        'chart_revenue_json': json.dumps(chart_revenue),
        'chart_gst_json': json.dumps(chart_gst),
        'chart_vat_json': json.dumps(chart_vat),
    }

    return render(request, 'restaurant_admin/analytics.html', context)


# ==========================================
# RECEIPT SYSTEM VIEWS (Advance Reservations & Deposits)
# ==========================================

@login_required
@user_passes_test(is_admin)
def receipt_list(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    qs = AdvanceReceipt.objects.filter(restaurant=restaurant).select_related('party', 'order')

    search_query = request.GET.get('q', '').strip()
    if search_query:
        qs = qs.filter(
            Q(receipt_no__icontains=search_query) |
            Q(booking_ref__icontains=search_query) |
            Q(customer_name__icontains=search_query) |
            Q(phone__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter in ['active', 'redeemed', 'cancelled']:
        qs = qs.filter(status=status_filter)

    # Compute Statistics
    stats_qs = AdvanceReceipt.objects.filter(restaurant=restaurant)
    total_advance_collected = stats_qs.exclude(status='cancelled').aggregate(s=Sum('advance_amount'))['s'] or Decimal('0.00')
    active_receipts_count = stats_qs.filter(status='active').count()
    redeemed_receipts_count = stats_qs.filter(status='redeemed').count()

    paginator = Paginator(qs, 20)
    page = request.GET.get('page', 1)
    try:
        receipts = paginator.page(page)
    except PageNotAnInteger:
        receipts = paginator.page(1)
    except EmptyPage:
        receipts = paginator.page(paginator.num_pages)

    customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both']).order_by('name')

    return render(request, 'restaurant_admin/receipts/list.html', {
        'receipts': receipts,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_advance_collected': total_advance_collected,
        'active_receipts_count': active_receipts_count,
        'redeemed_receipts_count': redeemed_receipts_count,
        'customer_parties': customer_parties,
        'active_menu': 'receipts',
        'restaurant': restaurant,
        'rslug': restaurant.slug if restaurant else '',
    })


@login_required
@user_passes_test(is_admin)
def receipt_create_page(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    if request.method == 'GET':
        last_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant).order_by('-id').first()
        next_seq = (last_receipt.id + 1001) if last_receipt else 1001
        next_receipt_no = f"REC-{next_seq}"

        customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both']).order_by('name')
        parties_list = [
            {'id': p.id, 'name': p.name, 'phone': p.phone or '', 'email': p.email or ''}
            for p in customer_parties
        ]

        today_date = timezone.now().strftime('%Y-%m-%d')

        return render(request, 'restaurant_admin/receipts/create.html', {
            'next_receipt_no': next_receipt_no,
            'today_date': today_date,
            'customer_parties': customer_parties,
            'customer_parties_json': json.dumps(parties_list),
            'active_menu': 'receipts',
            'restaurant': restaurant,
            'rslug': restaurant.slug if restaurant else '',
        })

    elif request.method == 'POST':
        is_json = (request.content_type == 'application/json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest')
        data = json.loads(request.body.decode('utf-8')) if (is_json and request.body) else request.POST

        receipt_no = (data.get('receipt_no') or '').strip()
        receipt_date_str = (data.get('receipt_date') or '').strip()
        customer_name = (data.get('customer_name') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()
        advance_amount_val = data.get('advance_amount') or 0
        payment_method = data.get('payment_method') or 'upi'
        notes = (data.get('notes') or '').strip()
        party_id = data.get('party_id')

        if not receipt_no:
            last_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant).order_by('-id').first()
            next_seq = (last_receipt.id + 1001) if last_receipt else 1001
            receipt_no = f"REC-{next_seq}"

        # Uniqueness check for custom receipt_no
        if AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no__iexact=receipt_no).exists():
            err_msg = f"Receipt No. '{receipt_no}' already exists for this restaurant. Please use a unique receipt number."
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_create_page', restaurant_slug=restaurant.slug)

        if not customer_name:
            err_msg = 'Customer name is required.'
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_create_page', restaurant_slug=restaurant.slug)

        try:
            advance_amount = Decimal(str(advance_amount_val))
            if advance_amount <= Decimal('0.00'):
                err_msg = 'Advance amount must be greater than 0.'
                if is_json:
                    return JsonResponse({'success': False, 'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('restaurant_admin:receipt_create_page', restaurant_slug=restaurant.slug)
        except Exception:
            err_msg = 'Invalid advance amount.'
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_create_page', restaurant_slug=restaurant.slug)

        issue_date = timezone.now().date()
        if receipt_date_str:
            try:
                issue_date = datetime.strptime(receipt_date_str, '%Y-%m-%d').date()
            except Exception:
                pass

        party = None
        if party_id:
            party = Party.objects.filter(pk=party_id, restaurant=restaurant).first()

        if not party and customer_name.lower() not in ['guest', 'walk-in guest']:
            party, _ = Party.objects.get_or_create(
                restaurant=restaurant,
                name__iexact=customer_name,
                defaults={
                    'name': customer_name,
                    'party_type': Party.PartyType.CUSTOMER,
                    'phone': phone,
                    'email': email,
                }
            )
            if phone and not party.phone:
                party.phone = phone
                party.save(update_fields=['phone'])

        # Create main Draft Order
        draft_order = Order.objects.create(
            restaurant=restaurant,
            party=party,
            customer_name=customer_name,
            phone=phone,
            status=Order.OrderStatus.DRAFT,
            payment_status=Order.PaymentStatus.ADVANCE_PAID,
            payment_method=payment_method,
            advance_amount_paid=advance_amount,
            paid_amount=advance_amount,
            due_amount=Decimal('0.00'),
            total_amount=Decimal('0.00'),
            subtotal=Decimal('0.00'),
            event_date=issue_date,
            notes=notes or '',
            payments=[{'method': payment_method, 'amount': float(advance_amount)}]
        )

        receipt = AdvanceReceipt.objects.create(
            restaurant=restaurant,
            party=party,
            receipt_no=receipt_no,
            booking_ref=draft_order.order_number if hasattr(draft_order, 'order_number') else f"ORD-{draft_order.id:04d}",
            customer_name=customer_name,
            phone=phone,
            email=email,
            event_date=issue_date,
            advance_amount=advance_amount,
            payment_method=payment_method,
            notes=notes,
            status=AdvanceReceipt.ReceiptStatus.ACTIVE,
            order=draft_order,
            created_by=request.user,
        )
        draft_order.advance_receipt = receipt
        draft_order.save(update_fields=['advance_receipt'])

        redirect_target = reverse('restaurant_admin:receipt_list', kwargs={'restaurant_slug': restaurant.slug})

        if is_json:
            return JsonResponse({'success': True, 'redirect_url': redirect_target})

        messages.success(request, f"Advance Receipt '{receipt.receipt_no}' generated successfully.")
        return redirect(redirect_target)


@login_required
@user_passes_test(is_admin)
def receipt_create_api(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=405)

    data = {}
    if request.content_type == 'application/json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST
    else:
        data = request.POST

    customer_name = (data.get('customer_name') or '').strip()
    phone = (data.get('phone') or '').strip()
    email = (data.get('email') or '').strip()
    event_date_str = (data.get('event_date') or '').strip()
    event_time_str = (data.get('event_time') or '').strip()
    guest_count_val = data.get('guest_count') or 1
    advance_amount_val = data.get('advance_amount') or 0
    payment_method = data.get('payment_method') or 'upi'
    notes = (data.get('notes') or '').strip()
    party_id = data.get('party_id')

    if not customer_name:
        return JsonResponse({'success': False, 'error': 'Customer name is required.'}, status=400)
    if not event_date_str:
        return JsonResponse({'success': False, 'error': 'Event date is required.'}, status=400)

    try:
        advance_amount = Decimal(str(advance_amount_val))
        if advance_amount <= Decimal('0.00'):
            return JsonResponse({'success': False, 'error': 'Advance amount must be greater than 0.'}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid advance amount.'}, status=400)

    try:
        guest_count = int(guest_count_val)
    except Exception:
        guest_count = 1

    event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
    event_time = None
    if event_time_str:
        try:
            event_time = datetime.strptime(event_time_str, '%H:%M').time()
        except Exception:
            pass

    party = None
    if party_id:
        party = Party.objects.filter(pk=party_id, restaurant=restaurant).first()

    if not party and customer_name.lower() not in ['guest', 'walk-in guest']:
        party, _ = Party.objects.get_or_create(
            restaurant=restaurant,
            name__iexact=customer_name,
            defaults={
                'name': customer_name,
                'party_type': Party.PartyType.CUSTOMER,
                'phone': phone,
                'email': email,
            }
        )
        if phone and not party.phone:
            party.phone = phone
            party.save(update_fields=['phone'])

    # Create the main Draft Order immediately
    draft_order = Order.objects.create(
        restaurant=restaurant,
        party=party,
        customer_name=customer_name,
        phone=phone,
        status=Order.OrderStatus.DRAFT,
        payment_status=Order.PaymentStatus.ADVANCE_PAID,
        payment_method=payment_method,
        advance_amount_paid=advance_amount,
        paid_amount=advance_amount,
        due_amount=Decimal('0.00'),
        total_amount=Decimal('0.00'),
        subtotal=Decimal('0.00'),
        event_date=event_date,
        event_time=event_time,
        guest_count=guest_count,
        notes=notes or '',
        payments=[{'method': payment_method, 'amount': float(advance_amount)}]
    )

    receipt = AdvanceReceipt.objects.create(
        restaurant=restaurant,
        party=party,
        customer_name=customer_name,
        phone=phone,
        email=email,
        event_date=event_date,
        event_time=event_time,
        guest_count=guest_count,
        advance_amount=advance_amount,
        payment_method=payment_method,
        notes=notes,
        status=AdvanceReceipt.ReceiptStatus.ACTIVE,
        order=draft_order,
        booking_ref=draft_order.order_number if hasattr(draft_order, 'order_number') else f"ORD-{draft_order.id:04d}",
        created_by=request.user,
    )
    draft_order.advance_receipt = receipt
    draft_order.save(update_fields=['advance_receipt'])

    print_url = reverse('restaurant_admin:receipt_print', kwargs={'restaurant_slug': restaurant.slug, 'pk': receipt.id})
    start_order_url = reverse('restaurant_admin:order_create_manual', kwargs={'restaurant_slug': restaurant.slug}) + f"?order_id={draft_order.id}"

    return JsonResponse({
        'success': True,
        'receipt': {
            'id': receipt.id,
            'receipt_no': receipt.receipt_no,
            'booking_ref': receipt.booking_ref,
            'customer_name': receipt.customer_name,
            'phone': receipt.phone,
            'event_date': receipt.event_date.strftime('%Y-%m-%d'),
            'event_time': receipt.event_time.strftime('%I:%M %p') if receipt.event_time else '',
            'guest_count': receipt.guest_count,
            'advance_amount': float(receipt.advance_amount),
            'payment_method': receipt.get_payment_method_display(),
            'status': receipt.status,
        },
        'print_url': print_url,
        'start_order_url': start_order_url,
    })


@login_required
@user_passes_test(is_admin)
def receipt_print(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    receipt = get_object_or_404(AdvanceReceipt, pk=pk, restaurant=restaurant)
    branch = restaurant.branches.first() if hasattr(restaurant, 'branches') else None

    return render(request, 'restaurant_admin/receipts/print_receipt.html', {
        'receipt': receipt,
        'restaurant': restaurant,
        'branch': branch,
    })


@login_required
@user_passes_test(is_admin)
def receipt_cancel_api(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required.'}, status=405)

    receipt = get_object_or_404(AdvanceReceipt, pk=pk, restaurant=restaurant)
    if receipt.status == AdvanceReceipt.ReceiptStatus.REDEEMED:
        return JsonResponse({'success': False, 'error': 'Cannot cancel an advance receipt that has already been redeemed.'}, status=400)

    receipt.status = AdvanceReceipt.ReceiptStatus.CANCELLED
    receipt.save(update_fields=['status'])

    return JsonResponse({'success': True})


@login_required
@user_passes_test(is_admin)
def receipt_detail_api(request, receipt_no, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    receipt = get_object_or_404(AdvanceReceipt, restaurant=restaurant, receipt_no=receipt_no)

    return JsonResponse({
        'success': True,
        'receipt': {
            'id': receipt.id,
            'receipt_no': receipt.receipt_no,
            'booking_ref': receipt.booking_ref,
            'customer_name': receipt.customer_name,
            'phone': receipt.phone,
            'email': receipt.email,
            'event_date': receipt.event_date.strftime('%Y-%m-%d'),
            'event_time': receipt.event_time.strftime('%H:%M') if receipt.event_time else '',
            'guest_count': receipt.guest_count,
            'advance_amount': float(receipt.advance_amount),
            'payment_method': receipt.payment_method,
            'payment_method_display': receipt.get_payment_method_display(),
            'status': receipt.status,
            'notes': receipt.notes,
        }
    })


# Internal Branch Management Views
@login_required
@user_passes_test(is_admin)
def branch_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    branches = Branch.objects.filter(restaurant=restaurant).order_by('branch_name')

    return render(request, 'restaurant_admin/branches/list.html', {
        'branches': branches,
        'restaurant': restaurant,
        'active_menu': 'settings',
        'active_tab': 'branches',
    })


@login_required
@user_passes_test(is_admin)
def branch_save(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method == 'POST':
        pk = request.POST.get('pk')
        branch_name = request.POST.get('branch_name', '').strip()
        branch_code = request.POST.get('branch_code', '').strip()
        address_line_1 = request.POST.get('address_line_1', '').strip()
        address_line_2 = request.POST.get('address_line_2', '').strip()
        city = request.POST.get('city', '').strip()
        state = request.POST.get('state', '').strip()
        pincode = request.POST.get('pincode', '').strip()
        address = request.POST.get('address', '').strip()
        gstin = request.POST.get('gstin', '').strip()
        is_active = request.POST.get('is_active') == 'on' or request.POST.get('is_active') == 'true' or request.POST.get('is_active') == '1'

        addr_parts = [address_line_1, address_line_2, city, state, pincode]
        constructed_addr = ", ".join([p for p in addr_parts if p])
        final_address = constructed_addr if constructed_addr else address

        if not branch_name:
            messages.error(request, "Branch name is required.")
            return redirect('restaurant_admin:branch_list', restaurant_slug=restaurant.slug)

        if pk:
            branch = get_object_or_404(Branch, pk=pk, restaurant=restaurant)
            branch.branch_name = branch_name
            branch.branch_code = branch_code
            branch.address_line_1 = address_line_1
            branch.address_line_2 = address_line_2
            branch.city = city
            branch.state = state
            branch.pincode = pincode
            branch.address = final_address
            branch.gstin = gstin
            branch.is_active = is_active
            branch.save()
            messages.success(request, f"Branch '{branch.branch_name}' updated successfully.")
        else:
            Branch.objects.create(
                restaurant=restaurant,
                branch_name=branch_name,
                branch_code=branch_code,
                address_line_1=address_line_1,
                address_line_2=address_line_2,
                city=city,
                state=state,
                pincode=pincode,
                address=final_address,
                gstin=gstin,
                is_active=is_active
            )
            messages.success(request, f"Internal branch '{branch_name}' created successfully.")

    return redirect('restaurant_admin:branch_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def branch_delete(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method == 'POST':
        branch = get_object_or_404(Branch, pk=pk, restaurant=restaurant)
        name = branch.branch_name
        branch.delete()
        messages.success(request, f"Branch '{name}' deleted successfully.")

    return redirect('restaurant_admin:branch_list', restaurant_slug=restaurant.slug)


# Delivery Challan Management Views
@login_required
@user_passes_test(is_admin)
def challan_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    challans = DeliveryChallan.objects.filter(restaurant=restaurant).select_related(
        'from_branch', 'to_branch', 'created_by'
    ).prefetch_related('items__inventory_item').order_by('-dispatch_date', '-created_at')

    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        challans = challans.filter(status=status_filter)

    return render(request, 'restaurant_admin/challans/list.html', {
        'challans': challans,
        'restaurant': restaurant,
        'active_menu': 'challans',
        'status_filter': status_filter,
    })


@login_required
@user_passes_test(is_admin)
def challan_create(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method == 'POST':
        challan_no = request.POST.get('challan_no', '').strip()
        dispatch_date_str = request.POST.get('dispatch_date', '').strip()
        from_branch_id = request.POST.get('from_branch_id')
        to_branch_id = request.POST.get('to_branch_id')
        notes = request.POST.get('notes', '').strip()

        if not challan_no:
            messages.error(request, "Challan Number is required.")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        if not to_branch_id:
            messages.error(request, "Destination Branch is required.")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        to_branch = get_object_or_404(Branch, pk=to_branch_id, restaurant=restaurant)
        from_branch = None
        if from_branch_id:
            from_branch = Branch.objects.filter(pk=from_branch_id, restaurant=restaurant).first()
            if from_branch and from_branch.id == to_branch.id:
                messages.error(request, "Destination branch cannot be the same as source branch.")
                return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        try:
            dispatch_date = datetime.strptime(dispatch_date_str, '%Y-%m-%d').date() if dispatch_date_str else timezone.now().date()
        except ValueError:
            dispatch_date = timezone.now().date()

        item_ids = request.POST.getlist('item_id[]')
        quantities = request.POST.getlist('quantity[]')
        unit_values = request.POST.getlist('unit_value[]')

        if not item_ids or len(item_ids) == 0:
            messages.error(request, "Please add at least one inventory item to the delivery challan.")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        try:
            with transaction.atomic():
                challan = DeliveryChallan.objects.create(
                    restaurant=restaurant,
                    challan_no=challan_no,
                    from_branch=from_branch,
                    to_branch=to_branch,
                    dispatch_date=dispatch_date,
                    status=DeliveryChallan.StatusChoices.IN_TRANSIT,
                    notes=notes,
                    created_by=request.user
                )

                total_val = Decimal('0.00')
                for item_id, qty_str, unit_val_str in zip(item_ids, quantities, unit_values):
                    if not item_id or not qty_str:
                        continue
                    qty = Decimal(str(qty_str or '0'))
                    unit_val = Decimal(str(unit_val_str or '0'))
                    if qty <= 0:
                        continue

                    line_total = qty * unit_val
                    total_val += line_total

                    inv_item = get_object_or_404(InventoryItem, pk=item_id, restaurant=restaurant)

                    DeliveryChallanItem.objects.create(
                        challan=challan,
                        inventory_item=inv_item,
                        quantity=qty,
                        unit_value=unit_val,
                        line_total=line_total
                    )

                    # Handshake Dispatch Action: Deduct quantity from source branch inventory item
                    inv_item.adjust(-int(qty))

                    # Record transaction log
                    InventoryTransaction.objects.create(
                        inventory_item=inv_item,
                        transaction_type=InventoryTransaction.TransactionType.MANUAL,
                        delta=-int(qty),
                        performed_by=request.user
                    )

                challan.total_value = total_val
                challan.save(update_fields=['total_value'])

                messages.success(request, f"Delivery Challan #{challan_no} created and dispatched (In Transit)!")
                return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

        except Exception as e:
            messages.error(request, f"Failed to save delivery challan: {str(e)}")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

    # GET Request: Prepare form data
    last_challan = DeliveryChallan.objects.filter(restaurant=restaurant).order_by('-id').first()
    next_num = (last_challan.id + 1001) if last_challan else 1001
    auto_challan_no = f"DC-{next_num}"
    while DeliveryChallan.objects.filter(restaurant=restaurant, challan_no=auto_challan_no).exists():
        next_num += 1
        auto_challan_no = f"DC-{next_num}"

    branches = Branch.objects.filter(restaurant=restaurant, is_active=True).order_by('branch_name')
    inventory_items = InventoryItem.objects.filter(restaurant=restaurant, is_active=True).order_by('name')

    return render(request, 'restaurant_admin/challans/create.html', {
        'auto_challan_no': auto_challan_no,
        'today_date': timezone.now().strftime('%Y-%m-%d'),
        'branches': branches,
        'inventory_items': inventory_items,
        'restaurant': restaurant,
        'active_menu': 'challans',
    })


@login_required
@user_passes_test(is_admin)
def challan_mark_received(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method != 'POST':
        messages.error(request, "Invalid request method.")
        return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

    challan = get_object_or_404(DeliveryChallan, pk=pk, restaurant=restaurant)

    if challan.status != DeliveryChallan.StatusChoices.IN_TRANSIT:
        messages.warning(request, f"Challan #{challan.challan_no} is already {challan.get_status_display()}.")
        return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

    try:
        with transaction.atomic():
            challan.status = DeliveryChallan.StatusChoices.RECEIVED
            challan.save(update_fields=['status', 'updated_at'])

            # Handshake Receive Action: Credit stock to target branch
            for item_record in challan.items.all():
                src_item = item_record.inventory_item
                qty = int(item_record.quantity)

                # Check if there is a target-branch-specific InventoryItem or update source item
                target_item = InventoryItem.objects.filter(
                    restaurant=restaurant,
                    internal_branch=challan.to_branch,
                    name__iexact=src_item.name
                ).first()

                if not target_item:
                    if src_item.internal_branch and src_item.internal_branch != challan.to_branch:
                        target_item = InventoryItem.objects.create(
                            restaurant=restaurant,
                            internal_branch=challan.to_branch,
                            name=src_item.name,
                            sku=f"{src_item.sku}-{challan.to_branch.branch_code}" if src_item.sku and challan.to_branch.branch_code else (src_item.sku or ''),
                            category=src_item.category,
                            unit=src_item.unit,
                            quantity=qty,
                            cost_price=src_item.cost_price,
                            unit_cost=src_item.unit_cost,
                            taxability_type=src_item.taxability_type,
                            gst_rate=src_item.gst_rate
                        )
                    else:
                        target_item = src_item
                        target_item.adjust(qty)
                else:
                    target_item.adjust(qty)

                InventoryTransaction.objects.create(
                    inventory_item=target_item,
                    transaction_type=InventoryTransaction.TransactionType.RESTOCK,
                    delta=qty,
                    performed_by=request.user
                )

            messages.success(request, f"Challan #{challan.challan_no} marked as RECEIVED. Inventory stock credited to '{challan.to_branch.branch_name}'.")

    except Exception as e:
        messages.error(request, f"Failed to mark challan as received: {str(e)}")

    return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def challan_detail(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    challan = get_object_or_404(
        DeliveryChallan.objects.select_related('from_branch', 'to_branch', 'created_by').prefetch_related('items__inventory_item'),
        pk=pk,
        restaurant=restaurant
    )

    return render(request, 'restaurant_admin/challans/detail.html', {
        'challan': challan,
        'restaurant': restaurant,
        'active_menu': 'challans',
    })


@login_required
@user_passes_test(is_admin)
def challan_cancel(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method != 'POST':
        return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

    challan = get_object_or_404(DeliveryChallan, pk=pk, restaurant=restaurant)
    if challan.status != DeliveryChallan.StatusChoices.IN_TRANSIT:
        messages.warning(request, "Only in-transit challans can be cancelled.")
        return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

    try:
        with transaction.atomic():
            challan.status = DeliveryChallan.StatusChoices.CANCELLED
            challan.save(update_fields=['status', 'updated_at'])

            # Restore deducted inventory stock to source
            for item_record in challan.items.all():
                item_record.inventory_item.adjust(int(item_record.quantity))
                InventoryTransaction.objects.create(
                    inventory_item=item_record.inventory_item,
                    transaction_type=InventoryTransaction.TransactionType.CANCEL,
                    delta=int(item_record.quantity),
                    performed_by=request.user
                )

            messages.success(request, f"Delivery Challan #{challan.challan_no} has been cancelled and stock restored.")
    except Exception as e:
        messages.error(request, f"Failed to cancel challan: {str(e)}")

    return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def transaction_desk_create(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    if request.method == 'POST':
        txn_type = request.POST.get('txn_type') or request.GET.get('type', 'pos')
        if txn_type == 'pos':
            return order_create_manual(request, restaurant_slug=restaurant.slug)
        elif txn_type == 'receipt':
            return receipt_create_page(request, restaurant_slug=restaurant.slug)
        elif txn_type == 'purchase':
            return purchase_create(request, restaurant_slug=restaurant.slug)
        elif txn_type == 'challan':
            return challan_create(request, restaurant_slug=restaurant.slug)

    active_type = (request.GET.get('type') or 'pos').lower()
    if active_type not in ['pos', 'receipt', 'purchase', 'challan']:
        active_type = 'pos'

    today_date = timezone.now().strftime('%Y-%m-%d')

    # POS Context Data Generation
    tables_qs = Table.objects.filter(restaurant=restaurant).order_by('table_number')
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory').order_by('category__name', 'name')
    categories_qs = Category.objects.filter(restaurant=restaurant).order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch_obj = restaurant.branches.first() if hasattr(restaurant, 'branches') else None

    cgst_pct = float(settings_obj.cgst_percentage or 2.50)
    sgst_pct = float(settings_obj.sgst_percentage or 2.50)
    vat_beer_pct = float(branch_obj.vat_beer_rate if branch_obj and getattr(branch_obj, 'vat_beer_rate', None) is not None else 60.00)
    vat_imfl_pct = float(branch_obj.vat_imfl_rate if branch_obj and getattr(branch_obj, 'vat_imfl_rate', None) is not None else 65.00)
    vat_wine_pct = float(branch_obj.vat_wine_rate if branch_obj and getattr(branch_obj, 'vat_wine_rate', None) is not None else 60.00)
    vat_gen_pct = float(getattr(settings_obj, 'vat_percentage', 20.00) or 20.00)

    menu_items_list = []
    for mi in menu_items_qs:
        cat_id = mi.category.id if mi.category else 0
        cat_name = mi.category.name if mi.category else 'General'
        img_url = mi.image.url if mi.image else ''
        
        tax_type = mi.tax_type or 'GST_FOOD'
        if tax_type == 'GST_FOOD' and cat_name:
            c_name_lower = cat_name.lower()
            if 'beer' in c_name_lower:
                tax_type = 'VAT_BEER'
            elif 'wine' in c_name_lower:
                tax_type = 'VAT_WINE'
            elif any(k in c_name_lower for k in ['liquor', 'bar', 'spirit', 'whiskey', 'vodka', 'rum', 'cocktail']):
                tax_type = 'VAT_IMFL'

        menu_items_list.append({
            'id': mi.id,
            'name': mi.name,
            'price': float(mi.price),
            'category_id': cat_id,
            'category_name': cat_name,
            'image_url': img_url,
            'tax_type': tax_type
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    top_selling_qs = (
        OrderItem.objects.filter(order__restaurant=restaurant)
        .values('menu_item_id')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')[:12]
    )
    top_selling_ids = [item['menu_item_id'] for item in top_selling_qs if item['menu_item_id']]

    active_orders_qs = Order.objects.filter(
        restaurant=restaurant,
        status__in=[Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING]
    ).select_related('table').prefetch_related('items', 'items__menu_item').order_by('-order_time')[:30]

    active_orders_list = []
    for ao in active_orders_qs:
        active_orders_list.append({
            'id': ao.id,
            'order_number': ao.order_number,
            'customer_name': ao.customer_name or 'Walk-in Guest',
            'phone': ao.phone or '',
            'notes': ao.notes or '',
            'table_id': ao.table.id if ao.table else None,
            'table_number': ao.table_number or (ao.table.table_number if ao.table else None),
            'order_type': ao.order_type,
            'subtotal': float(ao.subtotal),
            'tax_amount': float(ao.tax_amount),
            'total_amount': float(ao.total_amount),
            'order_time': ao.order_time.strftime('%I:%M %p'),
            'items': [
                {
                    'id': it.menu_item.id if it.menu_item else 0,
                    'name': it.menu_item.name if it.menu_item else 'Item',
                    'quantity': it.quantity,
                    'unit_price': float(it.unit_price),
                    'line_total': float(it.subtotal),
                    'tax_type': it.menu_item.tax_type if it.menu_item else 'GST_FOOD'
                }
                for it in ao.items.all()
            ]
        })

    custom_charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True).order_by('charge_name')
    custom_charges_list = []
    for cc in custom_charges_qs:
        custom_charges_list.append({
            'id': cc.id,
            'name': cc.charge_name,
            'type': cc.charge_type,
            'value': float(cc.value),
            'is_taxable': cc.is_taxable,
            'applied_order_types': cc.applied_order_types or ['dine_in', 'takeaway', 'delivery'],
            'is_auto_applied': cc.is_auto_applied,
        })

    attached_receipt = None
    draft_order = None
    order_id_param = request.GET.get('order_id')
    receipt_no_param = request.GET.get('receipt_no')

    if order_id_param:
        draft_order = Order.objects.filter(pk=order_id_param, restaurant=restaurant).first()
        if draft_order and draft_order.advance_receipt:
            attached_receipt = draft_order.advance_receipt

    if not attached_receipt and receipt_no_param:
        attached_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_param).first()
        if attached_receipt and attached_receipt.order:
            draft_order = attached_receipt.order

    if not attached_receipt and draft_order and draft_order.advance_amount_paid > 0:
        attached_receipt_json = json.dumps({
            'order_id': draft_order.id,
            'receipt_no': draft_order.advance_receipt.receipt_no if draft_order.advance_receipt else f"ORD-{draft_order.id:04d}",
            'booking_ref': draft_order.order_number or f"ORD-{draft_order.id:04d}",
            'customer_name': draft_order.customer_name,
            'phone': draft_order.phone,
            'advance_amount': float(draft_order.advance_amount_paid),
            'payment_method': draft_order.payment_method,
        })
    elif attached_receipt:
        attached_receipt_json = json.dumps({
            'order_id': attached_receipt.order.id if attached_receipt.order else 0,
            'receipt_no': attached_receipt.receipt_no,
            'booking_ref': attached_receipt.order.order_number if attached_receipt.order else attached_receipt.booking_ref,
            'customer_name': attached_receipt.customer_name,
            'phone': attached_receipt.phone,
            'advance_amount': float(attached_receipt.advance_amount),
            'payment_method': attached_receipt.payment_method,
        })
    else:
        attached_receipt_json = 'null'

    existing_order_json = 'null'
    if draft_order:
        existing_order_json = json.dumps({
            'id': draft_order.id,
            'order_number': draft_order.order_number or f"ORD-{draft_order.id:04d}",
            'customer_name': draft_order.customer_name,
            'phone': draft_order.phone or '',
            'notes': draft_order.notes or '',
            'table_id': draft_order.table.id if draft_order.table else None,
            'order_type': draft_order.order_type,
            'items': [
                {
                    'id': it.menu_item.id if it.menu_item else 0,
                    'name': it.menu_item.name if it.menu_item else 'Item',
                    'quantity': it.quantity,
                    'unit_price': float(it.unit_price),
                    'line_total': float(it.subtotal),
                    'tax_type': it.menu_item.tax_type if it.menu_item else 'GST_FOOD'
                }
                for it in draft_order.items.all()
            ]
        })

    customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both']).order_by('name')

    # Advance Receipt Context Data
    last_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant).order_by('-id').first()
    next_receipt_seq = (last_receipt.id + 1001) if last_receipt else 1001
    next_receipt_no = f"REC-{next_receipt_seq}"
    parties_list = [
        {'id': p.id, 'name': p.name, 'phone': p.phone or '', 'email': p.email or ''}
        for p in customer_parties
    ]

    # Purchase Context Data
    inventory_items = InventoryItem.objects.filter(restaurant=restaurant).order_by('name')
    inventory_items_list = [
        {'id': i.id, 'name': i.name, 'cost_price': float(i.cost_price or 0), 'unit': i.unit or 'Units'}
        for i in inventory_items
    ]
    suppliers = Supplier.objects.filter(restaurant=restaurant).order_by('name')
    last_purchase = PurchaseOrder.objects.filter(restaurant=restaurant).order_by('-id').first()
    next_purchase_seq = (last_purchase.id + 1001) if last_purchase else 1001
    next_purchase_no = f"PUR-{next_purchase_seq}"

    # Delivery Challan Context Data
    last_challan = DeliveryChallan.objects.filter(restaurant=restaurant).order_by('-id').first()
    next_challan_seq = (last_challan.id + 1001) if last_challan else 1001
    auto_challan_no = f"DC-{next_challan_seq}"
    branches = Branch.objects.filter(restaurant=restaurant).order_by('branch_name')

    context = {
        'active_type': active_type,
        'today_date': today_date,
        'restaurant': restaurant,
        'rslug': restaurant.slug,
        'active_menu': 'transactions_create',

        # POS Full Context
        'menu_items': menu_items_qs,
        'menu_items_json': json.dumps(menu_items_list),
        'top_selling_json': json.dumps(top_selling_ids),
        'categories_json': json.dumps(categories_list),
        'categories': categories_qs,
        'tables': tables_qs,
        'active_orders': active_orders_qs,
        'active_orders_json': json.dumps(active_orders_list),
        'tax_settings_json': json.dumps({
            'cgst_pct': cgst_pct,
            'sgst_pct': sgst_pct,
            'vat_beer_pct': vat_beer_pct,
            'vat_imfl_pct': vat_imfl_pct,
            'vat_wine_pct': vat_wine_pct,
            'vat_gen_pct': vat_gen_pct,
        }),
        'custom_charges_json': json.dumps(custom_charges_list),
        'customer_parties': customer_parties,
        'customer_parties_json': json.dumps(parties_list),
        'attached_receipt': attached_receipt,
        'attached_receipt_json': attached_receipt_json,
        'existing_order_json': existing_order_json,
        'draft_order': draft_order,
        'order': draft_order,
        'is_edit_mode': bool(draft_order),

        # Advance Receipt Context
        'next_receipt_no': next_receipt_no,

        # Purchase Context
        'inventory_items': inventory_items,
        'inventory_items_json': json.dumps(inventory_items_list),
        'suppliers': suppliers,
        'next_purchase_no': next_purchase_no,

        # Delivery Challan Context
        'auto_challan_no': auto_challan_no,
        'branches': branches,
    }

    return render(request, 'restaurant_admin/transactions/create.html', context)