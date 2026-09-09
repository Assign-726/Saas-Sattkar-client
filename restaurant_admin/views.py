from django.core.exceptions import ValidationError
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
from core.models import Zone, Table, Category, SubCategory, MenuItem, Order, OrderItem, RestaurantSettings, RestaurantLanding, InventoryItem, InventoryCategory, Coupon, MenuItemIngredient, InventoryTransaction, Supplier, PurchaseOrder, PurchaseOrderItem, Party, CustomCharge, AdvanceReceipt, Branch, DeliveryChallan, DeliveryChallanItem, DocumentSequenceSetting
from restaurant_admin.sequence_helpers import get_document_sequence_info, get_and_increment_document_number, validate_unique_document_number
from django.db import transaction
from super_admin.models import Restaurant, RestaurantUser, RestaurantBranch
from restaurant_admin.forms import MenuItemForm, CategoryForm, SubCategoryForm, OrderStatusForm, RestaurantSettingsForm, InventoryItemForm, InventoryCategoryForm, CouponForm, RestaurantBranchComplianceForm

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


@login_required
@user_passes_test(is_admin)
def switch_branch(request):
    """
    POST/GET endpoint to switch the admin's active branch.
    Stores the selection in the session, then redirects back
    to the 'next' URL (or dashboard).
    """
    branch_id = request.POST.get('branch_id') or request.GET.get('branch_id')
    restaurant = getattr(request, 'restaurant', None)

    # Fallback: resolve restaurant from profile
    if not restaurant and request.user.is_authenticated and hasattr(request.user, 'restaurant_profile'):
        try:
            restaurant = request.user.restaurant_profile.restaurant
        except Exception:
            pass

    if branch_id and restaurant:
        try:
            branch = restaurant.branches.filter(id=int(branch_id)).first()
            if branch:
                request.session[f'active_branch_{restaurant.id}'] = branch.id
        except (ValueError, TypeError):
            pass

    next_url = request.POST.get('next') or request.GET.get('next') or '/'
    # Safety: only allow relative redirects
    if not next_url.startswith('/'):
        next_url = '/'
    return redirect(next_url)

def resolve_restaurant(request, restaurant_slug=None):
    """Resolve the Restaurant object based on branch slug, company slug, or user profile.
    If branch slug is passed, sets active_branch in session.
    """
    if restaurant_slug:
        branch_match = RestaurantBranch.objects.filter(slug__iexact=restaurant_slug).select_related('restaurant').first()
        if branch_match:
            restaurant = branch_match.restaurant
            request.session[f'active_branch_{restaurant.id}'] = branch_match.id
        else:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)

        # Permission enforcement
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

def get_active_branch(request, restaurant):
    """Returns the currently active branch for the logged in user / request session."""
    if not restaurant:
        return None
    profile = getattr(request.user, 'restaurant_profile', None)
    if profile and profile.branch:
        return profile.branch
    branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}') or request.session.get('active_branch_id')
    if branch_id and str(branch_id).isdigit():
        branch = restaurant.branches.filter(id=int(branch_id)).first()
        if branch:
            return branch
    return restaurant.branches.first()

def get_branch_gstin_str(branch=None, restaurant=None):
    """Returns normalized GSTIN string for a given branch or restaurant."""
    if branch:
        if getattr(branch, 'gst_registration', None) and getattr(branch.gst_registration, 'gstin', None) and branch.gst_registration.gstin.strip():
            return branch.gst_registration.gstin.strip()
        if getattr(branch, 'gstin', None) and branch.gstin.strip():
            return branch.gstin.strip()
    
    if restaurant:
        if hasattr(restaurant, 'branches'):
            for b in restaurant.branches.all():
                if getattr(b, 'gst_registration', None) and getattr(b.gst_registration, 'gstin', None) and b.gst_registration.gstin.strip():
                    return b.gst_registration.gstin.strip()
                if getattr(b, 'gstin', None) and b.gstin.strip():
                    return b.gstin.strip()
        if hasattr(restaurant, 'gst_registrations'):
            reg = restaurant.gst_registrations.filter(gstin__isnull=False).exclude(gstin='').first()
            if reg and reg.gstin and reg.gstin.strip():
                return reg.gstin.strip()
        if getattr(restaurant, 'gstin', None) and restaurant.gstin.strip():
            return restaurant.gstin.strip()
    return ''

@login_required
@user_passes_test(lambda u: u.is_superuser)
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

@login_required
@user_passes_test(lambda u: u.is_superuser)
def test_static(request):
    """Test view to check static files loading"""
    return render(request, 'restaurant_admin/test_static.html')

@login_required
@user_passes_test(lambda u: u.is_superuser)
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
        try:
            per_page = int(request.GET.get('per_page', 20))
        except ValueError:
            per_page = 20
        paginator = Paginator(menu_items, per_page)
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
    if order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING]:
        order.status = Order.OrderStatus.READY
    order.save(update_fields=['prep_start', 'ready_at', 'prep_minutes', 'status'])

    # Publish SSE table update for real-time floor plan update
    table = order.table
    if not table and order.table_number:
        table = Table.objects.filter(restaurant=restaurant, table_number=order.table_number).first()
    if table:
        try:
            from core.sse import publish
            publish(restaurant.slug, {
                'type': 'table_status_update',
                'order_id': order.id,
                'table_id': table.id,
                'status': table.current_status
            })
        except Exception:
            pass

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Order #{order.branch_order_number or order.id} marked ready now.',
            'prep_minutes': order.prep_minutes,
            'prep_start': order.prep_start,
            'ready_at': order.ready_at,
            'status': order.status,
            'status_display': order.get_status_display(),
        })

    messages.success(request, f'Order #{order.branch_order_number or order.id} marked ready now.')
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('restaurant_admin:order_detail', restaurant_slug=restaurant.slug, pk=order.id)

# Inventory Category Management Views
@login_required
@user_passes_test(is_admin)
def inventory_category_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    q = request.GET.get('q', '').strip()

    categories_qs = InventoryCategory.objects.filter(restaurant=restaurant)
    if active_branch:
        categories_qs = categories_qs.filter(branch=active_branch)

    if q:
        categories_qs = categories_qs.filter(Q(name__icontains=q) | Q(hsn_sac_code__icontains=q))

    categories_qs = categories_qs.select_related('parent').prefetch_related('subcategories', 'items').order_by('name')

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(categories_qs, per_page)
    page_number = request.GET.get('page')
    categories = paginator.get_page(page_number)

    return render(request, 'restaurant_admin/inventory/category_list.html', {
        'categories': categories,
        'active_menu': 'inventory_categories',
        'search_query': q,
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def inventory_category_add(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = InventoryCategoryForm(request.POST, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            category = form.save()
            messages.success(request, f'Inventory Category "{category.name}" added successfully!')
            return redirect('restaurant_admin:inventory_category_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = InventoryCategoryForm(restaurant=restaurant, branch=active_branch)

    return render(request, 'restaurant_admin/inventory/category_form.html', {
        'form': form,
        'title': 'Add Inventory Category',
        'active_menu': 'inventory_categories',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def inventory_category_edit(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    category = get_object_or_404(InventoryCategory, pk=pk, restaurant=restaurant)

    if request.method == 'POST':
        form = InventoryCategoryForm(request.POST, instance=category, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            category = form.save()
            messages.success(request, f'Inventory Category "{category.name}" updated successfully!')
            return redirect('restaurant_admin:inventory_category_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = InventoryCategoryForm(instance=category, restaurant=restaurant, branch=active_branch)

    return render(request, 'restaurant_admin/inventory/category_form.html', {
        'form': form,
        'title': 'Edit Inventory Category',
        'active_menu': 'inventory_categories',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def inventory_category_delete(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    category = get_object_or_404(InventoryCategory, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        cat_name = category.name
        category.delete()
        messages.success(request, f'Inventory Category "{cat_name}" deleted successfully.')

    return redirect('restaurant_admin:inventory_category_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def inventory_category_api(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid restaurant'}, status=400)

    active_branch = get_active_branch(request, restaurant)
    cats_qs = InventoryCategory.objects.filter(restaurant=restaurant).select_related('parent', 'restaurant')
    if active_branch:
        cats_qs = cats_qs.filter(branch=active_branch)

    category_data = []
    for cat in cats_qs:
        resolved_gst = cat.resolve_category_value('default_gst_rate')
        resolved_hsn = cat.resolve_category_value('default_hsn_code')
        resolved_taxability = cat.resolve_category_value('taxability')

        category_data.append({
            'id': cat.id,
            'name': str(cat),
            'parent_id': cat.parent_id,
            'tax_configuration': cat.tax_configuration,
            'taxability_type': cat.taxability_type or 'taxable',
            'hsn_sac_code': cat.hsn_sac_code or '',
            'gst_rate': float(cat.gst_rate or 0) if cat.gst_rate is not None else None,
            'resolved_taxability_type': resolved_taxability or 'taxable',
            'resolved_hsn_sac_code': resolved_hsn or '',
            'resolved_gst_rate': float(resolved_gst or 0)
        })

    return JsonResponse({'success': True, 'categories': category_data})


# Inventory Management
@login_required
@user_passes_test(is_admin)
def inventory_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    q = request.GET.get('q', '').strip()
    items = InventoryItem.objects.filter(restaurant=restaurant).select_related('category', 'category__parent')
    if active_branch:
        items = items.filter(branch=active_branch)
    if q:
        items = items.filter(
            Q(name__icontains=q) |
            Q(sku__icontains=q) |
            Q(category__name__icontains=q) |
            Q(hsn_sac_code__icontains=q) |
            Q(category__default_hsn_code__icontains=q) |
            Q(category__hsn_sac_code__icontains=q) |
            Q(category__parent__default_hsn_code__icontains=q) |
            Q(category__parent__hsn_sac_code__icontains=q)
        ).distinct()
    items_qs = items.order_by('name')
    
    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(items_qs, per_page)
    page_number = request.GET.get('page')
    items = paginator.get_page(page_number)

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

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = InventoryItemForm(request.POST, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            item = form.save()
            if item.quantity != 0:
                InventoryTransaction.objects.create(
                    inventory_item=item,
                    transaction_type=InventoryTransaction.TransactionType.RESTOCK,
                    delta=item.quantity,
                    performed_by=request.user
                )
            return JsonResponse({
                'success': True,
                'item': {
                    'id': item.id,
                    'name': item.name,
                    'sku': item.sku or '',
                    'category': str(item.category) if item.category else '',
                    'unit': item.unit or '',
                    'quantity': item.quantity,
                    'cost_price': float(item.unit_cost),
                    'hsn_sac_code': item.effective_hsn_code or '',
                    'gst_rate': float(item.effective_gst_rate or 0),
                    'taxability_type': item.effective_taxability or 'taxable'
                }
            })
        else:
            errors = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            return JsonResponse({'success': False, 'error': '; '.join(errors)}, status=400)

    return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)


@login_required
@user_passes_test(is_admin)
def inventory_add(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = InventoryItemForm(request.POST, restaurant=restaurant, branch=active_branch)
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
        form = InventoryItemForm(restaurant=restaurant, branch=active_branch)

    return render(request, 'restaurant_admin/inventory/form.html', {
        'form': form,
        'title': 'Add Inventory Item',
        'active_menu': 'inventory',
        'restaurant': restaurant,
    })


def serialize_pos_order(order):
    if not order:
        return None
    items = []
    for item in order.items.select_related('menu_item', 'menu_item__category').all():
        mi = item.menu_item
        cat_name = mi.category.name if mi and mi.category else 'General'
        tax_type = (mi.tax_type if mi else 'GST_FOOD') or 'GST_FOOD'
        branch_serves_alcohol = bool(order.branch and order.branch.serves_alcohol)
        if branch_serves_alcohol and tax_type == 'GST_FOOD' and cat_name:
            c_name_lower = cat_name.lower()
            if 'beer' in c_name_lower:
                tax_type = 'VAT_BEER'
            elif 'wine' in c_name_lower:
                tax_type = 'VAT_WINE'
            elif any(k in c_name_lower for k in ['liquor', 'bar', 'spirit', 'whiskey', 'vodka', 'rum', 'cocktail']):
                tax_type = 'VAT_IMFL'

        disc_amt = float(item.discount_amount or item.calc_discount_amount or 0)
        items.append({
            'id': mi.id if mi else 0,
            'name': mi.name if mi else 'Item',
            'quantity': item.quantity,
            'unit_price': float(item.unit_price or (mi.price if mi else 0)),
            'tax_type': tax_type,
            'category_name': cat_name,
            'is_tax_inclusive': mi.is_tax_inclusive if mi else False,
            'discount_type': item.discount_type or 'flat',
            'discount_value': float(item.discount_value or 0),
            'discount_amount': disc_amt,
            'discount_reason': item.discount_reason or '',
            'notes': item.notes or '',
            'note': item.notes or '',
            'sent': True if order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.READY, Order.OrderStatus.SERVED, Order.OrderStatus.COMPLETED] else False
        })

    table_name = ''
    if order.table:
        table_name = order.table.name
    elif order.table_number:
        table_name = f"Table {order.table_number}"

    disc_amt = float(order.discount_amount or 0)
    disc_val = float(order.discount_value or 0)
    order_discount = None
    if disc_amt > 0 or disc_val > 0:
        order_discount = {
            'amount': disc_amt,
            'type': order.discount_type or 'flat',
            'value': disc_val if disc_val > 0 else disc_amt,
            'reason': order.discount_reason or (order.coupon.code if order.coupon else '') or 'Bill discount'
        }

    return {
        'id': order.id,
        'order_number': order.order_number or f"ORD-{order.id:04d}",
        'branch_order_number': order.branch_order_number or order.id,
        'customer_name': order.customer_name or 'Walk-in Guest',
        'phone': order.phone or '',
        'notes': order.notes or '',
        'table_id': order.table.id if order.table else None,
        'table_name': table_name,
        'table_number': order.table_number or (order.table.table_number if order.table else None),
        'order_type': order.order_type or 'dine_in',
        'payment_method': order.payment_method or 'cash',
        'payment_status': order.payment_status or 'unpaid',
        'status': order.status,
        'subtotal': float(order.subtotal or 0),
        'tax_amount': float(order.tax_amount or 0),
        'discount_amount': disc_amt,
        'discount_type': order.discount_type or 'flat',
        'discount_value': disc_val,
        'discount_reason': order.discount_reason or '',
        'order_discount': order_discount,
        'round_off_amount': float(order.round_off_amount or 0),
        'total_amount': float(order.total_amount or 0),
        'guest_count': order.guest_count or 2,
        'items': items
    }


def get_pos_active_orders(restaurant, active_branch=None):
    from django.utils import timezone
    from django.db.models import Q
    # Unsettled orders (Drafts, Pending, Preparing, and Unpaid)
    orders_qs = Order.objects.filter(
        restaurant=restaurant
    ).exclude(
        status=Order.OrderStatus.CANCELLED
    ).filter(
        Q(status__in=[Order.OrderStatus.DRAFT, Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.READY]) |
        Q(payment_status__in=['unpaid', 'partially_paid', 'advance_paid', 'failed'])
    )
    if active_branch:
        orders_qs = orders_qs.filter(branch=active_branch)

    orders_qs = orders_qs.select_related('table').prefetch_related('items', 'items__menu_item', 'items__menu_item__category').order_by('-order_time')[:50]

    results = []
    for ord_obj in orders_qs:
        elapsed_str = ''
        if ord_obj.order_time:
            diff = timezone.now() - ord_obj.order_time
            tot_sec = int(diff.total_seconds())
            if tot_sec < 60:
                elapsed_str = 'just now'
            elif tot_sec < 3600:
                elapsed_str = f"{tot_sec // 60}m"
            elif tot_sec < 86400:
                h = tot_sec // 3600
                m = (tot_sec % 3600) // 60
                elapsed_str = f"{h}h {m}m" if m else f"{h}h"
            else:
                elapsed_str = f"{tot_sec // 86400}d"

        num = ord_obj.branch_order_number or ord_obj.id
        ord_type = ord_obj.order_type or 'dine_in'
        if ord_type == 'dine_in':
            title = f"Dine-in #{num}"
            if ord_obj.table:
                title = f"Dine-in {ord_obj.table.name}"
            elif ord_obj.table_number:
                title = f"Dine-in T-{ord_obj.table_number}"
        elif ord_type == 'takeaway':
            title = f"Takeaway #{num}"
        else:
            title = f"Delivery #{num}"

        items_count = ord_obj.items.count()

        results.append({
            'id': ord_obj.id,
            'order_number': ord_obj.order_number or f"ORD-{ord_obj.id:04d}",
            'branch_order_number': num,
            'title': title,
            'customer_name': ord_obj.customer_name or 'Walk-in Guest',
            'phone': ord_obj.phone or '',
            'notes': ord_obj.notes or '',
            'table_id': ord_obj.table.id if ord_obj.table else None,
            'table_number': ord_obj.table_number or (ord_obj.table.table_number if ord_obj.table else None),
            'order_type': ord_type,
            'order_type_display': ord_obj.get_order_type_display(),
            'status': ord_obj.status,
            'subtotal': float(ord_obj.subtotal or 0),
            'tax_amount': float(ord_obj.tax_amount or 0),
            'discount_amount': float(ord_obj.discount_amount or 0),
            'discount_type': ord_obj.discount_type or 'flat',
            'discount_value': float(ord_obj.discount_value or 0),
            'discount_reason': ord_obj.discount_reason or '',
            'order_discount': {
                'amount': float(ord_obj.discount_amount or 0),
                'type': ord_obj.discount_type or 'flat',
                'value': float(ord_obj.discount_value or ord_obj.discount_amount or 0),
                'reason': ord_obj.discount_reason or 'Bill discount'
            } if (ord_obj.discount_amount or ord_obj.discount_value) else None,
            'total_amount': float(ord_obj.total_amount or 0),
            'order_time': ord_obj.order_time.strftime('%I:%M %p') if ord_obj.order_time else '',
            'elapsed': elapsed_str,
            'lines_count': items_count,
            'items': [
                {
                    'id': it.menu_item.id if it.menu_item else 0,
                    'name': it.menu_item.name if it.menu_item else 'Item',
                    'quantity': it.quantity,
                    'unit_price': float(it.unit_price or 0),
                    'line_total': float(it.subtotal or 0),
                    'tax_type': it.menu_item.tax_type if it.menu_item else 'GST_FOOD',
                    'discount_type': it.discount_type or 'flat',
                    'discount_value': float(it.discount_value or 0),
                    'discount_amount': float(it.discount_amount or it.calc_discount_amount or 0),
                    'discount_reason': it.discount_reason or '',
                    'notes': it.notes or '',
                    'note': it.notes or '',
                }
                for it in ord_obj.items.all()
            ]
        })
    return results


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
        bypass_same_gstin = data.get('bypass_same_gstin') in [1, '1', True, 'true'] or request.POST.get('bypass_same_gstin') == '1'

        if customer_name and not bypass_same_gstin and customer_name.lower() not in ['guest', 'walk-in guest', '']:
            active_b = get_active_branch(request, restaurant)
            party_qs = Party.objects.filter(restaurant=restaurant, name__iexact=customer_name)
            if active_b:
                party_qs = party_qs.filter(branch=active_b)
            matched_party = party_qs.first()
            if matched_party and matched_party.gstin:
                active_b = get_active_branch(request, restaurant)
                active_b_gstin = get_branch_gstin_str(active_b, restaurant)
                import re
                clean_p_gstin = re.sub(r'[^A-Za-z0-9]', '', matched_party.gstin).upper()
                clean_b_gstin = re.sub(r'[^A-Za-z0-9]', '', active_b_gstin).upper()
                if clean_p_gstin and clean_b_gstin and clean_p_gstin == clean_b_gstin:
                    if is_json:
                        return JsonResponse({
                            'success': False,
                            'same_gstin_warning': True,
                            'party_name': matched_party.name,
                            'party_gstin': matched_party.gstin,
                            'party_phone': matched_party.phone or '',
                            'party_address': matched_party.full_address or '',
                            'active_branch_gstin': active_b_gstin,
                            'error': f"Internal Transfer Warning: Buyer party '{matched_party.name}' shares the same GSTIN ({matched_party.gstin}) as your active branch. Delivery Challan recommended."
                        }, status=400)

        phone = (data.get('phone_number') or data.get('phone') or '').strip()
        notes = (data.get('notes') or '').strip()
        table_id_val = data.get('table_id') or data.get('table')

        table_obj = None
        if table_id_val:
            table_val_str = str(table_id_val).strip()
            if table_val_str.isdigit():
                table_obj = Table.objects.filter(pk=int(table_val_str), restaurant=restaurant).first()
                if not table_obj:
                    table_obj = Table.objects.filter(table_number=int(table_val_str), restaurant=restaurant).first()
            if not table_obj:
                table_obj = Table.objects.filter(name__iexact=table_val_str, restaurant=restaurant).first()
            if not table_obj:
                clean_name = table_val_str.replace('Table', '').strip()
                table_obj = Table.objects.filter(name__iexact=clean_name, restaurant=restaurant).first()

        order_type = data.get('order_type') or (Order.OrderType.DINE_IN if table_obj else Order.OrderType.DINE_IN)
        if table_obj:
            order_type = Order.OrderType.DINE_IN

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
        discount_amount_val = safe_decimal(data.get('discount_amount'))
        discount_type_val = (data.get('discount_type') or 'flat').strip()
        discount_value_val = safe_decimal(data.get('discount_value'))
        discount_reason_val = (data.get('discount_reason') or '').strip() or None
        round_off_amount_val = safe_decimal(data.get('round_off_amount'))
        grand_total_val = safe_decimal(data.get('grand_total'))

        receipt_no_input = (data.get('receipt_no') or '').strip()
        advance_amount_input = safe_decimal(data.get('advance_amount_paid') or data.get('advance_amount'))

        if receipt_no_input and advance_amount_input <= Decimal('0.00'):
            arc = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_input).first()
            if arc:
                advance_amount_input = arc.advance_amount

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
        # Determine order status based on action
        if action in ['fire_kot', 'save_kot']:
            initial_status = Order.OrderStatus.PENDING
        elif action in ['settle_order', 'print_close']:
            initial_status = Order.OrderStatus.SERVED
        else:
            initial_status = Order.OrderStatus.PENDING

        # Document Sequence & Invoice Number handling
        gstin_no = (data.get('gstin_no') or data.get('gstin') or data.get('invoice_number') or '').strip()
        excise_bill_no = (data.get('excise_bill_no') or data.get('excise') or data.get('vat_bill_no') or '').strip()

        should_assign_invoice_number = (
            action in ['settle_order', 'print_close', 'save_invoice', 'save_food_invoice', 'save_liquor_invoice', 'save_combined_invoice'] or
            data.get('payment_status') == 'paid' or
            initial_payment_status == Order.PaymentStatus.PAID
        )

        active_branch = get_active_branch(request, restaurant)
        sales_seq_info = get_document_sequence_info(restaurant, 'sales', branch=active_branch)
        vat_seq_info = get_document_sequence_info(restaurant, 'vat_bill', branch=active_branch) if (active_branch and active_branch.serves_alcohol) else None

        if should_assign_invoice_number and sales_seq_info['mode'] in ['automatic', 'manual_override'] and not gstin_no:
            try:
                gstin_no = get_and_increment_document_number(restaurant, 'sales', branch=active_branch, user_input_no=gstin_no, exclude_pk=order_id)
            except ValidationError as ve:
                if is_json:
                    return JsonResponse({'success': False, 'error': str(ve)}, status=400)
                messages.error(request, str(ve))
                return redirect(request.path)
        elif gstin_no:
            try:
                validate_unique_document_number(restaurant, 'sales', gstin_no, branch=active_branch, exclude_pk=order_id)
            except ValidationError as ve:
                if is_json:
                    return JsonResponse({'success': False, 'error': str(ve)}, status=400)
                messages.error(request, str(ve))
                return redirect(request.path)

        if should_assign_invoice_number and vat_seq_info and vat_seq_info['mode'] in ['automatic', 'manual_override'] and not excise_bill_no:
            # Check if items contain liquor
            items_data = data.get('items')
            has_alcohol = False
            if isinstance(items_data, list):
                for it in items_data:
                    m_id = it.get('menu_item_id') or it.get('id')
                    if m_id:
                        m_obj = MenuItem.objects.filter(id=m_id).first()
                        if m_obj and m_obj.tax_type in [MenuItem.TaxType.VAT_BEER, MenuItem.TaxType.VAT_IMFL, MenuItem.TaxType.VAT_WINE, MenuItem.TaxType.VAT_LIQUOR]:
                            has_alcohol = True
                            break
            if has_alcohol:
                try:
                    excise_bill_no = get_and_increment_document_number(restaurant, 'vat_bill', branch=active_branch, user_input_no=excise_bill_no, exclude_pk=order_id)
                except ValidationError as ve:
                    if is_json:
                        return JsonResponse({'success': False, 'error': str(ve)}, status=400)
                    messages.error(request, str(ve))
                    return redirect(request.path)
        elif excise_bill_no:
            try:
                validate_unique_document_number(restaurant, 'vat_bill', excise_bill_no, branch=active_branch, exclude_pk=order_id)
            except ValidationError as ve:
                if is_json:
                    return JsonResponse({'success': False, 'error': str(ve)}, status=400)
                messages.error(request, str(ve))
                return redirect(request.path)

        if order_id:
            order = get_object_or_404(Order, pk=order_id, restaurant=restaurant)
            if not order.branch and active_branch:
                order.branch = active_branch
            order.customer_name = customer_name or order.customer_name
            order.phone = phone or order.phone
            order.notes = notes if notes is not None else order.notes
            if table_obj:
                order.table = table_obj
                order.table_number = table_obj.table_number
                order.order_type = Order.OrderType.DINE_IN
            else:
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
            order.discount_amount = discount_amount_val
            order.discount_type = discount_type_val
            order.discount_value = discount_value_val
            order.discount_reason = discount_reason_val
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
            # Clear existing items if items data is provided
            items_data = data.get('items')
            if isinstance(items_data, list):
                order.items.all().delete()

            voided_item = data.get('voided_item_name')
            void_reason = data.get('void_reason')
            if voided_item and void_reason:
                void_entry = f"[Voided {voided_item}: {void_reason}]"
                if void_entry not in (order.notes or ''):
                    order.notes = f"{order.notes}\n{void_entry}".strip() if order.notes else void_entry
                    order.save(update_fields=['notes'])
        else:
            order = Order.objects.create(
                customer_name=customer_name or 'Walk-in Guest',
                phone=phone,
                notes=notes,
                order_type=order_type,
                table=table_obj,
                table_number=table_obj.table_number if table_obj else None,
                payment_method=payment_method,
                payment_status=initial_payment_status,
                payments=valid_payments,
                paid_amount=paid_amount_val,
                due_amount=due_amount_val,
                status=initial_status,
                restaurant=restaurant,
                branch=active_branch,
                subtotal=subtotal_val,
                tax_amount=tax_amount_val,
                discount_amount=discount_amount_val,
                discount_type=discount_type_val,
                discount_value=discount_value_val,
                discount_reason=discount_reason_val,
                round_off_amount=round_off_amount_val,
                total_amount=grand_total_val,
                gstin_no=gstin_no or None,
                excise_bill_no=excise_bill_no or None,
            )

        if customer_name and customer_name.strip().lower() not in ['guest', 'walk-in', 'walk-in guest']:
            party_lookup = {'restaurant': restaurant, 'name__iexact': customer_name.strip()}
            if active_branch:
                party_lookup['branch'] = active_branch
            existing_party = Party.objects.filter(**party_lookup).first()
            if not existing_party and phone and len(phone) >= 4:
                # Also try looking up party by phone (exact or substring)
                existing_party = Party.objects.filter(restaurant=restaurant, phone__icontains=phone).first()

            party_gstin_input = (data.get('party_gstin') or data.get('customer_gstin') or '').strip()

            if existing_party:
                party = existing_party
                # If order has incomplete or missing phone but party has full phone, auto-populate order.phone
                if party.phone and (not order.phone or len(str(order.phone)) < len(str(party.phone))):
                    order.phone = party.phone
                    order.save(update_fields=['phone'])
                if party_gstin_input and not party.gstin:
                    party.gstin = party_gstin_input
                    party.save(update_fields=['gstin'])
            else:
                party = Party.objects.create(
                    restaurant=restaurant,
                    branch=active_branch,
                    name=customer_name.strip(),
                    party_type=Party.PartyType.CUSTOMER,
                    phone=phone or '',
                    gstin=party_gstin_input or ''
                )
            order.party = party
            order.save(update_fields=['party'])
        else:
            if order.party and (not customer_name or customer_name.strip().lower() in ['guest', 'walk-in', 'walk-in guest']):
                order.party = None
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
                    mi_filter = {'pk': item_id, 'restaurant': restaurant}
                    if active_branch:
                        mi_filter['branch'] = active_branch
                    menu_item = MenuItem.objects.get(**mi_filter)
                except MenuItem.DoesNotExist:
                    # Fallback: allow untagged/legacy items that lack a branch
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
                item_notes = (item.get('notes') or item.get('note') or '').strip()

                created_item = OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=u_price,
                    notes=item_notes,
                    discount_type=disc_type,
                    discount_value=disc_val,
                    discount_amount=disc_amt,
                    discount_reason=disc_reason,
                )
                created_items.append(created_item)

        table_id = data.get('table_id') or data.get('table')
        if table_id:
            try:
                t_obj = Table.objects.filter(pk=int(table_id), restaurant=restaurant).first()
                if t_obj:
                    order.table = t_obj
                    order.table_number = t_obj.table_number
                    order.order_type = Order.OrderType.DINE_IN
                    order.save(update_fields=['table', 'table_number', 'order_type'])
            except Exception:
                pass

        if grand_total_val > 0:
            order.subtotal = subtotal_val
            order.tax_amount = tax_amount_val
            order.discount_amount = discount_amount_val
            order.discount_type = discount_type_val
            order.discount_value = discount_value_val
            order.discount_reason = discount_reason_val
            order.round_off_amount = round_off_amount_val
            order.total_amount = grand_total_val
            order.save()
        else:
            order.update_total()

        order.assign_invoice_numbers()

        # Check if only new items are being fired in KOT
        items_data = data.get('items')
        new_item_ids = []
        if isinstance(items_data, list) and len(items_data) > 0 and len(created_items) > 0:
            for ci, item_data in zip(created_items, items_data):
                if not item_data.get('sent'):
                    new_item_ids.append(str(ci.id))

        kot_url = reverse('restaurant_admin:order_kot', kwargs={'restaurant_slug': restaurant.slug, 'pk': order.id})
        if new_item_ids and len(new_item_ids) < len(created_items):
            kot_url = kot_url + '?items=' + ','.join(new_item_ids)

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
                status__in=[Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.READY]
            )
            if active_branch:
                active_orders_qs = active_orders_qs.filter(branch=active_branch)
            active_orders_qs = active_orders_qs.select_related('table').prefetch_related('items', 'items__menu_item').order_by('-order_time')[:30]

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
    active_branch = get_active_branch(request, restaurant)
    tables_qs = Table.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_qs = tables_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    tables_qs = tables_qs.order_by('table_number')

    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory')
    categories_qs = Category.objects.filter(restaurant=restaurant)
    if active_branch:
        menu_items_qs = menu_items_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
        categories_qs = categories_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    menu_items_qs = menu_items_qs.order_by('category__name', 'name')
    categories_qs = categories_qs.order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch_obj = active_branch or (restaurant.branches.first() if hasattr(restaurant, 'branches') else None)

    if settings_obj and settings_obj.cgst_percentage is not None:
        cgst_pct = float(settings_obj.cgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            cgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            cgst_pct = 0.00
        else:
            cgst_pct = 2.50
    else:
        cgst_pct = 2.50

    if settings_obj and settings_obj.sgst_percentage is not None:
        sgst_pct = float(settings_obj.sgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            sgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            sgst_pct = 0.00
        else:
            sgst_pct = 2.50
    else:
        sgst_pct = 2.50

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
        branch_serves_alcohol = bool(branch_obj and branch_obj.serves_alcohol)
        if branch_serves_alcohol and tax_type == 'GST_FOOD' and cat_name:
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
            'short_code': mi.short_code or str(mi.id),
            'price': float(mi.price),
            'category_id': cat_id,
            'category_name': cat_name,
            'image_url': img_url,
            'tax_type': tax_type,
            'is_tax_inclusive': mi.is_tax_inclusive
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    # Calculate top selling item IDs scoped to active branch (with top items fallback)
    from django.db.models import Sum
    top_selling_filter = {'order__restaurant': restaurant}
    if active_branch:
        top_selling_filter['order__branch'] = active_branch
    top_selling_qs = (
        OrderItem.objects.filter(**top_selling_filter)
        .values('menu_item_id')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')[:20]
    )
    top_selling_ids = [item['menu_item_id'] for item in top_selling_qs if item['menu_item_id']]
    if len(top_selling_ids) < 6:
        for mi_obj in menu_items_qs[:12]:
            if mi_obj.id not in top_selling_ids:
                top_selling_ids.append(mi_obj.id)

    # Active / In-kitchen / Draft orders (branch-scoped)
    active_orders_list = get_pos_active_orders(restaurant, active_branch)

    custom_charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        custom_charges_qs = custom_charges_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    custom_charges_qs = custom_charges_qs.order_by('charge_name')
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
    new_param = request.GET.get('new')
    receipt_no_param = request.GET.get('receipt_no')

    if order_id_param:
        draft_order = Order.objects.filter(pk=order_id_param, restaurant=restaurant).first()
        if draft_order and draft_order.advance_receipt:
            attached_receipt = draft_order.advance_receipt
    elif not new_param:
        # Check if there is an existing DRAFT order for this user/branch
        draft_qs = Order.objects.filter(restaurant=restaurant, status=Order.OrderStatus.DRAFT)
        if active_branch:
            draft_qs = draft_qs.filter(branch=active_branch)
        draft_order = draft_qs.order_by('-order_time').first()

    if not attached_receipt and receipt_no_param:
        attached_receipt = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_param).first()

    if attached_receipt:
        if attached_receipt.order:
            draft_order = attached_receipt.order
        elif attached_receipt.receipt_type == 'advance':
            active_branch_for_receipt = get_active_branch(request, restaurant)
            draft_order = Order.objects.create(
                restaurant=restaurant,
                branch=active_branch_for_receipt,
                party=attached_receipt.party,
                customer_name=attached_receipt.customer_name or (attached_receipt.party.name if attached_receipt.party else 'Walk-in Guest'),
                phone=attached_receipt.phone or (attached_receipt.party.phone if attached_receipt.party else ''),
                status=Order.OrderStatus.DRAFT,
                order_type=Order.OrderType.DINE_IN,
                payment_status=Order.PaymentStatus.ADVANCE_PAID if attached_receipt.advance_amount > 0 else Order.PaymentStatus.UNPAID,
                advance_amount_paid=attached_receipt.advance_amount,
                advance_receipt=attached_receipt,
                notes=f"Draft order linked to Advance Receipt #{attached_receipt.receipt_no}",
            )
            attached_receipt.order = draft_order
            attached_receipt.reference_number = draft_order.order_number
            attached_receipt.booking_ref = draft_order.order_number
            attached_receipt.save(update_fields=['order', 'reference_number', 'booking_ref'])

    existing_order_json = json.dumps(serialize_pos_order(draft_order)) if draft_order else 'null'

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
            'order_id': attached_receipt.order.id if attached_receipt.order else (draft_order.id if draft_order else 0),
            'receipt_no': attached_receipt.receipt_no,
            'booking_ref': attached_receipt.order.order_number if attached_receipt.order else (draft_order.order_number if draft_order else attached_receipt.booking_ref),
            'customer_name': attached_receipt.customer_name,
            'phone': attached_receipt.phone,
            'advance_amount': float(attached_receipt.advance_amount),
            'payment_method': attached_receipt.payment_method,
        })
    else:
        attached_receipt_json = 'null'

    # Sequence info for invoice numbers
    gst_seq_info = get_document_sequence_info(restaurant, 'sales', branch=branch_obj)
    excise_seq_info = get_document_sequence_info(restaurant, 'vat_bill', branch=branch_obj) if (branch_obj and branch_obj.serves_alcohol) else gst_seq_info

    cust_parties_qs = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both'])
    if branch_obj:
        cust_parties_qs = cust_parties_qs.filter(branch=branch_obj)
    cust_parties_qs = cust_parties_qs.order_by('name')
    cust_parties_list = [
        {
            'id': p.id,
            'name': p.name,
            'phone': p.phone or '',
            'email': p.email or '',
            'gstin': p.gstin or '',
            'full_address': p.full_address or ''
        }
        for p in cust_parties_qs
    ]
    active_branch_gstin_manual = get_branch_gstin_str(branch_obj, restaurant)

    service_charge_pct_val = float(branch_obj.service_charge_percentage if (branch_obj and branch_obj.service_charge_percentage is not None) else (settings_obj.service_charge_percentage or 0))

    return render(request, 'restaurant_admin/orders/manual_create.html', {
        'menu_items': menu_items_qs,
        'menu_items_json': json.dumps(menu_items_list),
        'top_selling_json': json.dumps(top_selling_ids),
        'categories_json': json.dumps(categories_list),
        'categories': categories_qs,
        'tables': tables_qs,
        'active_orders': active_orders_list,
        'active_orders_json': json.dumps(active_orders_list),
        'tax_settings_json': json.dumps({
            'cgst_pct': cgst_pct,
            'sgst_pct': sgst_pct,
            'vat_beer_pct': vat_beer_pct,
            'vat_imfl_pct': vat_imfl_pct,
            'vat_wine_pct': vat_wine_pct,
            'vat_gen_pct': vat_gen_pct,
            'service_charge_pct': service_charge_pct_val,
            'service_charge_taxable': getattr(settings_obj, 'service_charge_taxable', True),
        }),
        'customer_parties': cust_parties_qs,
        'customer_parties_json': json.dumps(cust_parties_list),
        'active_branch_gstin': active_branch_gstin_manual,
        'active_branch_gstin_json': json.dumps(active_branch_gstin_manual),
        'custom_charges_json': json.dumps(custom_charges_list),
        'attached_receipt': attached_receipt,
        'attached_receipt_json': attached_receipt_json,
        'existing_order_json': existing_order_json,
        'is_edit_mode': True if draft_order else False,
        'order': draft_order,
        'gst_seq_info': gst_seq_info,
        'excise_seq_info': excise_seq_info,
        'gst_seq_info_json': json.dumps(gst_seq_info),
        'excise_seq_info_json': json.dumps(excise_seq_info),
        'active_menu': 'order_create_manual',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def pos_draft_order_api(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant profile'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'GET':
        order_id = request.GET.get('order_id')
        if order_id:
            order = Order.objects.filter(pk=order_id, restaurant=restaurant).first()
        else:
            draft_qs = Order.objects.filter(restaurant=restaurant, status=Order.OrderStatus.DRAFT)
            if active_branch:
                draft_qs = draft_qs.filter(branch=active_branch)
            order = draft_qs.order_by('-order_time').first()

        return JsonResponse({
            'success': True,
            'order': serialize_pos_order(order),
            'active_orders': get_pos_active_orders(restaurant, active_branch)
        })

    elif request.method == 'POST':
        import json
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST

        order_id = data.get('order_id')
        customer_name = (data.get('customer_name') or 'Walk-in Guest').strip()
        phone = (data.get('phone') or '').strip()
        notes = (data.get('notes') or '').strip()
        order_type = data.get('order_type') or 'dine_in'
        table_id = data.get('table_id') or data.get('table')
        guest_count = data.get('guest_count') or data.get('covers')

        table_obj = None
        if table_id:
            table_obj = Table.objects.filter(pk=table_id, restaurant=restaurant).first()
            if table_obj:
                order_type = 'dine_in'

        if order_id:
            order = Order.objects.filter(pk=order_id, restaurant=restaurant).first()
            if order:
                order.customer_name = customer_name
                order.phone = phone
                order.notes = notes
                order.order_type = order_type
                order.table = table_obj
                order.table_number = table_obj.table_number if table_obj else None
                if guest_count:
                    order.guest_count = int(guest_count)
                order.save()
        else:
            order = Order.objects.create(
                restaurant=restaurant,
                branch=active_branch,
                status=Order.OrderStatus.DRAFT,
                customer_name=customer_name,
                phone=phone,
                notes=notes,
                order_type=order_type,
                table=table_obj,
                table_number=table_obj.table_number if table_obj else None,
                guest_count=int(guest_count) if guest_count else 2
            )

        return JsonResponse({
            'success': True,
            'order_id': order.id,
            'order_number': order.order_number or f"ORD-{order.id:04d}",
            'order': serialize_pos_order(order),
            'active_orders': get_pos_active_orders(restaurant, active_branch)
        })


@login_required
@user_passes_test(is_admin)
def pos_sync_draft_items_api(request, restaurant_slug=None):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant profile'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    import json
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    def safe_dec(val, default='0.00'):
        try:
            return Decimal(str(val or '0'))
        except Exception:
            return Decimal(default)

    order_id = data.get('order_id')
    customer_name = (data.get('customer_name') or 'Walk-in Guest').strip()
    phone = (data.get('phone') or '').strip()
    notes = (data.get('notes') or '').strip()
    order_type = data.get('order_type') or 'dine_in'
    table_id = data.get('table_id') or data.get('table')
    guest_count = data.get('guest_count') or data.get('covers')

    table_obj = None
    if table_id:
        table_obj = Table.objects.filter(pk=table_id, restaurant=restaurant).first()
        if table_obj:
            order_type = 'dine_in'

    subtotal_val = safe_dec(data.get('subtotal'))
    tax_amount_val = safe_dec(data.get('tax_amount'))
    discount_amount_val = safe_dec(data.get('discount_amount'))
    discount_type_val = (data.get('discount_type') or 'flat').strip()
    discount_value_val = safe_dec(data.get('discount_value'))
    discount_reason_val = (data.get('discount_reason') or '').strip() or None
    round_off_amount_val = safe_dec(data.get('round_off_amount'))
    grand_total_val = safe_dec(data.get('grand_total'))

    from django.db import transaction
    with transaction.atomic():
        if order_id:
            order = Order.objects.filter(pk=order_id, restaurant=restaurant).first()
            if not order:
                order = Order.objects.create(
                    restaurant=restaurant,
                    branch=active_branch,
                    status=Order.OrderStatus.DRAFT,
                    customer_name=customer_name,
                    phone=phone,
                    notes=notes,
                    order_type=order_type,
                    table=table_obj,
                    table_number=table_obj.table_number if table_obj else None,
                    guest_count=int(guest_count) if guest_count else 2
                )
            else:
                order.customer_name = customer_name
                order.phone = phone
                order.notes = notes
                order.order_type = order_type
                order.table = table_obj
                order.table_number = table_obj.table_number if table_obj else None
                if guest_count:
                    order.guest_count = int(guest_count)
        else:
            order = Order.objects.create(
                restaurant=restaurant,
                branch=active_branch,
                status=Order.OrderStatus.DRAFT,
                customer_name=customer_name,
                phone=phone,
                notes=notes,
                order_type=order_type,
                table=table_obj,
                table_number=table_obj.table_number if table_obj else None,
                guest_count=int(guest_count) if guest_count else 2
            )

        # 1. Sync items first using bulk_create to avoid intermediate individual item save triggers
        items_data = data.get('items', [])
        if isinstance(items_data, list):
            OrderItem.objects.filter(order=order).delete()
            new_items = []
            for it in items_data:
                item_id = it.get('menu_item_id') or it.get('id')
                qty = int(it.get('quantity') or 0)
                u_price = safe_dec(it.get('unit_price') or it.get('price'))
                if not item_id or qty <= 0:
                    continue

                mi = MenuItem.objects.filter(pk=item_id, restaurant=restaurant).first()
                if not mi:
                    continue
                if u_price <= 0:
                    u_price = mi.price

                disc_type = it.get('discount_type', 'flat') or 'flat'
                disc_val = safe_dec(it.get('discount_value'))
                disc_amt = safe_dec(it.get('discount_amount'))
                disc_reason = (it.get('discount_reason') or '').strip() or None
                it_notes = (it.get('notes') or it.get('note') or '').strip()

                new_items.append(OrderItem(
                    order=order,
                    menu_item=mi,
                    quantity=qty,
                    unit_price=u_price,
                    discount_type=disc_type,
                    discount_value=disc_val,
                    discount_amount=disc_amt,
                    discount_reason=disc_reason,
                    notes=it_notes
                ))
            if new_items:
                OrderItem.objects.bulk_create(new_items)

        # 2. Persist order discount and computed totals
        order.subtotal = subtotal_val
        order.tax_amount = tax_amount_val
        order.discount_amount = discount_amount_val
        order.discount_type = discount_type_val
        order.discount_value = discount_value_val
        order.discount_reason = discount_reason_val
        order.round_off_amount = round_off_amount_val
        order.total_amount = grand_total_val
        order.save()

    return JsonResponse({
        'success': True,
        'order_id': order.id,
        'order_number': order.order_number or f"ORD-{order.id:04d}",
        'order': serialize_pos_order(order),
        'active_orders': get_pos_active_orders(restaurant, active_branch)
    })


@login_required
@user_passes_test(is_admin)
def pos_finalize_order_api(request, restaurant_slug=None):
    return order_create_manual(request, restaurant_slug=restaurant_slug)


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

        initial_payment_status = order.payment_status
        receipt_no_input = (data.get('receipt_no') or data.get('advance_receipt_no') or '').strip()
        advance_amount_input = safe_decimal(data.get('advance_amount') or data.get('advance_amount_paid'))

        arc = None
        if receipt_no_input:
            arc = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no=receipt_no_input).first()
        elif order and order.advance_receipt:
            arc = order.advance_receipt

        if arc:
            order.advance_receipt = arc
            if advance_amount_input > 0:
                order.advance_amount_paid = advance_amount_input
            elif arc.advance_amount > 0:
                order.advance_amount_paid = arc.advance_amount
            order.save(update_fields=['advance_receipt', 'advance_amount_paid'])
            if action in ['settle_order', 'print_close'] or data.get('payment_status') == 'paid' or initial_payment_status == Order.PaymentStatus.PAID:
                arc.status = AdvanceReceipt.ReceiptStatus.REDEEMED
                arc.order = order
                arc.save(update_fields=['status', 'order'])

        discount_amount_val = safe_decimal(data.get('discount_amount'))
        discount_type_val = (data.get('discount_type') or 'flat').strip()
        discount_value_val = safe_decimal(data.get('discount_value'))
        discount_reason_val = (data.get('discount_reason') or '').strip() or None

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
                    mi_filter = {'pk': item_id, 'restaurant': restaurant}
                    edit_branch = get_active_branch(request, restaurant)
                    if edit_branch:
                        mi_filter['branch'] = edit_branch
                    menu_item = MenuItem.objects.get(**mi_filter)
                except MenuItem.DoesNotExist:
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
                item_notes = (item.get('notes') or item.get('note') or '').strip()

                OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=u_price,
                    notes=item_notes,
                    discount_type=disc_type,
                    discount_value=disc_val,
                    discount_amount=disc_amt,
                    discount_reason=disc_reason,
                )

        order.discount_amount = discount_amount_val
        order.discount_type = discount_type_val
        order.discount_value = discount_value_val
        order.discount_reason = discount_reason_val

        if grand_total_val > 0:
            order.subtotal = subtotal_val
            order.tax_amount = tax_amount_val
            order.round_off_amount = round_off_amount_val
            order.total_amount = grand_total_val
            order.save()
        else:
            order.update_total()

        if order.payment_status == Order.PaymentStatus.PAID or order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.READY, Order.OrderStatus.SERVED]:
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
                target_url = food_invoice_url
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
            return redirect(food_invoice_url)
        return redirect(order_detail_url)

    # GET
    active_branch = get_active_branch(request, restaurant)
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory')
    categories_qs = Category.objects.filter(restaurant=restaurant)
    if active_branch:
        menu_items_qs = menu_items_qs.filter(branch=active_branch)
        categories_qs = categories_qs.filter(branch=active_branch)
    menu_items_qs = menu_items_qs.order_by('category__name', 'name')
    categories_qs = categories_qs.order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)
    branch_obj = active_branch

    if settings_obj and settings_obj.cgst_percentage is not None:
        cgst_pct = float(settings_obj.cgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            cgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            cgst_pct = 0.00
        else:
            cgst_pct = 2.50
    else:
        cgst_pct = 2.50

    if settings_obj and settings_obj.sgst_percentage is not None:
        sgst_pct = float(settings_obj.sgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            sgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            sgst_pct = 0.00
        else:
            sgst_pct = 2.50
    else:
        sgst_pct = 2.50

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
        branch_serves_alcohol = bool(branch_obj and branch_obj.serves_alcohol)
        if branch_serves_alcohol and tax_type == 'GST_FOOD' and cat_name:
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
            'short_code': mi.short_code or str(mi.id),
            'price': float(mi.price),
            'category_id': cat_id,
            'category_name': cat_name,
            'image_url': img_url,
            'tax_type': tax_type,
            'is_tax_inclusive': mi.is_tax_inclusive
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    existing_items = []
    for item in order.items.select_related('menu_item', 'menu_item__category').all():
        mi = item.menu_item
        cat_name = mi.category.name if mi and mi.category else 'General'
        tax_type = (mi.tax_type if mi else 'GST_FOOD') or 'GST_FOOD'
        order_branch_serves_alcohol = bool(order.branch and order.branch.serves_alcohol) if order.branch else branch_serves_alcohol
        if order_branch_serves_alcohol and tax_type == 'GST_FOOD' and cat_name:
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
            'category_name': cat_name,
            'is_tax_inclusive': mi.is_tax_inclusive if mi else False
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
    tables_qs = Table.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_qs = tables_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    custom_charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        custom_charges_qs = custom_charges_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    custom_charges_qs = custom_charges_qs.order_by('charge_name')
    custom_charges_list = []
    for cc in custom_charges_qs:
        custom_charges_list.append({
            'id': cc.id,
            'name': cc.charge_name,
            'charge_name': cc.charge_name,
            'type': cc.charge_type,
            'value': float(cc.value),
            'is_taxable': cc.is_taxable,
            'applied_order_types': cc.applied_order_types or ['dine_in', 'takeaway', 'delivery'],
            'is_auto_applied': cc.is_auto_applied,
        })

    cust_parties_qs = Party.objects.filter(restaurant=restaurant).order_by('name')
    if active_branch:
        cust_parties_qs = cust_parties_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    cust_parties_list = [{'id': p.id, 'name': p.name, 'phone': p.phone, 'email': p.email, 'gstin': p.gstin or ''} for p in cust_parties_qs]

    active_branch_gstin_manual = get_branch_gstin_str(active_branch, restaurant)

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
            'service_charge_pct': float(settings_obj.service_charge_percentage or 0),
            'service_charge_taxable': getattr(settings_obj, 'service_charge_taxable', True),
        }),
        'customer_parties': cust_parties_qs,
        'customer_parties_json': json.dumps(cust_parties_list),
        'active_branch_gstin': active_branch_gstin_manual,
        'active_branch_gstin_json': json.dumps(active_branch_gstin_manual),
        'custom_charges_json': json.dumps(custom_charges_list),
        'existing_order_json': json.dumps(serialize_pos_order(order)),
        'is_edit_mode': True,
        'order': order,
        'active_menu': 'order_create_manual',
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def menu_quick_add_api(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            menu_item = form.save()
            return JsonResponse({
                'success': True,
                'item': {
                    'id': menu_item.id,
                    'name': menu_item.name,
                    'short_code': menu_item.short_code or '',
                    'price': float(menu_item.price),
                    'tax_type': menu_item.tax_type or 'GST_FOOD',
                    'category_id': menu_item.category.id if menu_item.category else None,
                    'category_name': menu_item.category.name if menu_item.category else 'General',
                    'is_tax_inclusive': menu_item.is_tax_inclusive,
                    'availability': menu_item.availability
                }
            })
        else:
            errors = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            return JsonResponse({'success': False, 'error': '; '.join(errors)}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@login_required
@user_passes_test(is_admin)
def inventory_edit(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    item = get_object_or_404(InventoryItem, pk=pk, restaurant=restaurant)

    if request.method == 'POST':
        old_quantity = item.quantity or 0
        form = InventoryItemForm(request.POST, instance=item, restaurant=restaurant, branch=active_branch)
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
        form = InventoryItemForm(instance=item, restaurant=restaurant, branch=active_branch)

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
    active_branch = get_active_branch(request, restaurant)
    all_inventory = InventoryItem.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        all_inventory = all_inventory.filter(branch=active_branch)
    all_inventory = all_inventory.order_by('name')

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

    active_branch = get_active_branch(request, restaurant)
    logs_qs = InventoryTransaction.objects.filter(inventory_item__restaurant=restaurant)
    if active_branch:
        logs_qs = logs_qs.filter(inventory_item__branch=active_branch)
    logs_qs = logs_qs.select_related('inventory_item', 'order', 'performed_by').order_by('-created_at')

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20

    from django.core.paginator import Paginator
    paginator = Paginator(logs_qs, per_page)
    page_number = request.GET.get('page')
    logs = paginator.get_page(page_number)

    return render(request, 'restaurant_admin/inventory/logs.html', {
        'logs': logs,
        'active_menu': 'inventory_logs',
        'restaurant': restaurant,
    })


# Party Management Views
@login_required
@user_passes_test(is_admin)
def party_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

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

        if gst_registration_type == 'unregistered':
            gstin = ''
            place_of_supply = ''

        combined_parts = [p for p in [address_line1, city, state, pincode] if p]
        full_addr_str = ", ".join(combined_parts) if combined_parts else address

        if not name:
            messages.error(request, "Party Name is required.")
            return redirect(request.path)

        if party_id:
            party = get_object_or_404(Party, id=party_id, restaurant=restaurant)
            if not party.branch and active_branch:
                party.branch = active_branch
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
                branch=active_branch,
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
    if active_branch:
        parties = parties.filter(branch=active_branch)
    if tab == 'vendor':
        parties = parties.filter(party_type__in=['vendor', 'both'])
    elif tab == 'customer':
        parties = parties.filter(party_type__in=['customer', 'both'])

    if q:
        parties = parties.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(gstin__icontains=q))

    parties_qs = parties.order_by('name')

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(parties_qs, per_page)
    page_number = request.GET.get('page')
    parties = paginator.get_page(page_number)

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

    active_branch = get_active_branch(request, restaurant)

    purchases = PurchaseOrder.objects.filter(restaurant=restaurant).filter(
        Q(party=party) | Q(supplier_name__iexact=party.name)
    )
    if active_branch:
        purchases = purchases.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    purchases = purchases.order_by('-purchase_date', '-id')

    sales = Order.objects.filter(restaurant=restaurant).filter(
        Q(party=party) | Q(customer_name__iexact=party.name) | (Q(phone=party.phone) if party.phone else Q(pk=None))
    ).exclude(status='C')
    if active_branch:
        sales = sales.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    sales = sales.order_by('-order_time', '-id')

    advance_receipts = AdvanceReceipt.objects.filter(restaurant=restaurant).filter(
        Q(party=party) | Q(customer_name__iexact=party.name) | (Q(phone=party.phone) if party.phone else Q(pk=None))
    )
    if active_branch:
        advance_receipts = advance_receipts.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    advance_receipts = advance_receipts.order_by('-created_at', '-id')

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
            'paid_amount': ord_obj.paid_amount,
            'advance_paid': ord_obj.advance_amount_paid or Decimal('0.00'),
            'url': reverse('restaurant_admin:order_invoice', kwargs={'restaurant_slug': restaurant.slug, 'pk': ord_obj.id})
        })

    for arc in advance_receipts:
        if not arc.party:
            arc.party = party
            arc.save(update_fields=['party'])
        ref_no = arc.receipt_no
        receipt_date = arc.event_date or arc.created_at.date()
        ref_info = arc.reference_number or arc.booking_ref
        if arc.order:
            order_num = arc.order.branch_order_number or f"ORD-{arc.order.id:04d}"
            ref_info = f"Redeemed for Order #{order_num}"
        ledger.append({
            'date': receipt_date,
            'created_at': arc.created_at,
            'ref_no': ref_no,
            'type_label': 'Advance / Payment Receipt',
            'type_code': 'RECEIPT',
            'amount': arc.advance_amount,
            'payment_status': arc.status,
            'ref_info': ref_info,
            'url': reverse('restaurant_admin:receipt_list', kwargs={'restaurant_slug': restaurant.slug})
        })

    ledger.sort(key=lambda x: (x['date'], x['created_at']), reverse=True)

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20

    from django.core.paginator import Paginator
    paginator = Paginator(ledger, per_page)
    page_number = request.GET.get('page')
    ledger_page = paginator.get_page(page_number)

    return render(request, 'restaurant_admin/parties/ledger.html', {
        'restaurant': restaurant,
        'party': party,
        'ledger': ledger_page,
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

    active_branch = get_active_branch(request, restaurant)

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

        active_branch = get_active_branch(request, restaurant)
        try:
            invoice_number = get_and_increment_document_number(
                restaurant=restaurant,
                document_type='purchase',
                branch=active_branch,
                user_input_no=(data.get('entry_no') or data.get('invoice_number') or '').strip()
            )
        except ValidationError as ve:
            return JsonResponse({'success': False, 'error': str(ve)}, status=400) if is_json else (messages.error(request, str(ve)) or redirect(request.path))

        if not items:
            err = 'Please add at least one inventory item to the purchase list.'
            return JsonResponse({'success': False, 'error': err}, status=400) if is_json else (messages.error(request, err) or redirect(request.path))

        try:
            with transaction.atomic():
                sup_lookup = {'restaurant': restaurant, 'name__iexact': supplier_name}
                if active_branch:
                    sup_lookup['branch'] = active_branch
                supplier, _ = Supplier.objects.get_or_create(
                    **sup_lookup,
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
                party_lookup = {'restaurant': restaurant, 'name__iexact': supplier_name}
                if active_branch:
                    party_lookup['branch'] = active_branch
                party, _ = Party.objects.get_or_create(
                    **party_lookup,
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
                calc_total_tax = Decimal('0.00')
                parsed_items = []
                for item_data in items:
                    item_id = item_data.get('item_id')
                    # Scope inventory item lookup to branch if possible
                    inv_filter = {'id': item_id, 'restaurant': restaurant}
                    if active_branch:
                        inv_filter['branch'] = active_branch
                    try:
                        inv_item = InventoryItem.objects.get(**inv_filter)
                    except InventoryItem.DoesNotExist:
                        inv_item = get_object_or_404(InventoryItem, id=item_id, restaurant=restaurant)
                    qty = Decimal(str(item_data.get('quantity') or 1))
                    cost = Decimal(str(item_data.get('unit_cost') if item_data.get('unit_cost') is not None else (inv_item.price_per_unit or 0)))
                    
                    user_gst = item_data.get('gst_rate')
                    if user_gst is not None and str(user_gst) != '':
                        gst_rate_val = Decimal(str(user_gst))
                    else:
                        eff_gst_rate = inv_item.get_effective_value('default_gst_rate')
                        gst_rate_val = Decimal(str(eff_gst_rate if eff_gst_rate is not None else 0))
                    eff_hsn_code = inv_item.get_effective_value('default_hsn_code') or ''

                    line_total = (qty * cost).quantize(Decimal('0.01'))
                    line_gst = (line_total * gst_rate_val / Decimal('100.00')).quantize(Decimal('0.01'))
                    
                    subtotal += line_total
                    calc_total_tax += line_gst

                    parsed_items.append({
                        'inventory_item': inv_item,
                        'quantity': qty,
                        'unit_cost': cost,
                        'gst_rate': gst_rate_val,
                        'hsn_code': eff_hsn_code,
                        'line_total': line_total
                    })

                user_tax = data.get('tax_amount')
                tax_amount = Decimal(str(user_tax if user_tax is not None else calc_total_tax)).quantize(Decimal('0.01'))
                grand_total = subtotal + tax_amount

                po = PurchaseOrder.objects.create(
                    restaurant=restaurant,
                    branch=active_branch,
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
                        gst_rate=pi['gst_rate'],
                        hsn_code=pi['hsn_code'],
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
    suppliers = Supplier.objects.filter(restaurant=restaurant)
    if active_branch:
        suppliers = suppliers.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    suppliers = suppliers.order_by('name')

    vendor_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['vendor', 'both'])
    if active_branch:
        vendor_parties = vendor_parties.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    vendor_parties = vendor_parties.order_by('name')

    vendor_parties_list = [
        {
            'id': str(p.id),
            'name': p.name,
            'gstin': p.gstin or '',
            'phone': p.phone or '',
            'email': p.email or ''
        }
        for p in vendor_parties
    ]
    for supp in suppliers:
        if not any(v['name'].lower() == supp.name.lower() for v in vendor_parties_list):
            vendor_parties_list.append({
                'id': f'supp_{supp.id}',
                'name': supp.name,
                'gstin': supp.gstin or '',
                'phone': supp.phone or '',
                'email': ''
            })

    inventory_items = InventoryItem.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        inventory_items = inventory_items.filter(branch=active_branch)
    inventory_items = inventory_items.select_related('category', 'category__parent', 'restaurant').order_by('name')
    
    inventory_items_list = []
    for item in inventory_items:
        eff_gst = item.get_effective_value('default_gst_rate')
        eff_hsn = item.get_effective_value('default_hsn_code')
        eff_taxability = item.get_effective_value('taxability')
        inventory_items_list.append({
            'id': item.id,
            'name': item.name,
            'sku': item.sku or '',
            'category': str(item.category) if item.category else '',
            'unit': item.unit or 'unit',
            'quantity': item.quantity,
            'cost_price': float(item.unit_cost),
            'hsn_sac_code': str(eff_hsn or ''),
            'gst_rate': float(eff_gst if eff_gst is not None else 0),
            'taxability_type': str(eff_taxability or 'taxable')
        })
    categories_qs = InventoryCategory.objects.filter(restaurant=restaurant)
    if active_branch:
        categories_qs = categories_qs.filter(branch=active_branch)
    categories = list(categories_qs.values_list('name', flat=True))
    today_date = timezone.now().strftime('%Y-%m-%d')

    seq_info = get_document_sequence_info(restaurant, 'purchase', branch=active_branch)
    entry_no = seq_info['next_number']

    return render(request, 'restaurant_admin/purchases/create.html', {
        'restaurant': restaurant,
        'suppliers': suppliers,
        'vendor_parties': vendor_parties,
        'vendor_parties_json': json.dumps(vendor_parties_list),
        'inventory_items': inventory_items,
        'inventory_items_json': json.dumps(inventory_items_list),
        'categories': categories,
        'category_objects': categories_qs,
        'today_date': today_date,
        'entry_no': entry_no,
        'seq_info': seq_info,
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

    active_branch = get_active_branch(request, restaurant)
    purchases = PurchaseOrder.objects.filter(restaurant=restaurant)
    if active_branch:
        purchases = purchases.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    purchases = purchases.select_related('supplier', 'created_by').prefetch_related('items__inventory_item').order_by('-purchase_date', '-id')

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(purchases, per_page)
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


@login_required
@user_passes_test(is_admin)
def purchase_edit(request, restaurant_slug=None, pk=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    po = get_object_or_404(PurchaseOrder, pk=pk, restaurant=restaurant)

    if request.method == 'POST':
        invoice_number = request.POST.get('invoice_number', '').strip()
        purchase_date = request.POST.get('purchase_date', '').strip()
        supplier_name = request.POST.get('supplier_name', '').strip()
        supplier_gstin = request.POST.get('supplier_gstin', '').strip()
        payment_method = request.POST.get('payment_method', po.payment_method)
        payment_status = request.POST.get('payment_status', po.payment_status)

        if not invoice_number:
            messages.error(request, "Invoice / Ref Number is required.")
            return redirect('restaurant_admin:purchase_list', restaurant_slug=restaurant.slug)

        # Validate unique invoice number for this restaurant excluding current PO
        try:
            validate_unique_document_number(restaurant, 'purchase', invoice_number, exclude_pk=po.pk)
        except ValidationError as ve:
            messages.error(request, str(ve))
            return redirect('restaurant_admin:purchase_list', restaurant_slug=restaurant.slug)

        po.invoice_number = invoice_number
        if purchase_date:
            try:
                po.purchase_date = datetime.strptime(purchase_date, '%Y-%m-%d').date()
            except ValueError:
                pass
        if supplier_name:
            po.supplier_name = supplier_name
            if po.supplier and po.supplier.name != supplier_name:
                po.supplier.name = supplier_name
                po.supplier.save(update_fields=['name'])
        po.supplier_gstin = supplier_gstin
        if payment_method in [c[0] for c in PurchaseOrder.PaymentMethod.choices]:
            po.payment_method = payment_method
        if payment_status in [c[0] for c in PurchaseOrder.PaymentStatus.choices]:
            po.payment_status = payment_status

        po.save()
        messages.success(request, f"Purchase entry #{po.id} updated successfully!")
        return redirect('restaurant_admin:purchase_list', restaurant_slug=restaurant.slug)

    return redirect('restaurant_admin:purchase_list', restaurant_slug=restaurant.slug)


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

    # Resolve active branch
    profile = getattr(request.user, 'restaurant_profile', None)
    active_branch = profile.branch if profile else None
    if not active_branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            active_branch = restaurant.branches.filter(id=int(branch_id)).first()
            if active_branch:
                request.session[f'active_branch_{restaurant.id}'] = active_branch.id
        if not active_branch:
            active_branch = restaurant.branches.first()
            if active_branch:
                request.session[f'active_branch_{restaurant.id}'] = active_branch.id

    # Get today's date
    today = timezone.now()
    
    # Base order queryset scoped to active_branch
    base_orders = Order.objects.filter(restaurant=restaurant)
    if active_branch:
        base_orders = base_orders.filter(branch=active_branch)

    # Get statistics (scoped to branch)
    total_orders = base_orders.exclude(status=Order.OrderStatus.DRAFT).count()
    today_orders = base_orders.filter(order_time__date=today.date()).exclude(status=Order.OrderStatus.DRAFT).count()
    menu_items_qs_dash = MenuItem.objects.filter(restaurant=restaurant)
    if active_branch:
        menu_items_qs_dash = menu_items_qs_dash.filter(branch=active_branch)
    total_menu_items = menu_items_qs_dash.count()
    
    # Calculate total revenue from completed/served orders only (excluding drafts)
    total_revenue = base_orders.filter(
        status__in=['C', 'S']  # Completed or Served
    ).exclude(status=Order.OrderStatus.DRAFT).aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    # Today's advance collections (Cashflow for end-of-day drawer balance)
    today_advance_collected = base_orders.filter(
        order_time__date=today.date()
    ).aggregate(s=Sum('advance_amount_paid'))['s'] or Decimal('0.00')
    
    # Get recent orders
    recent_orders = base_orders.order_by('-order_time')[:5]
    
    # Get top selling items
    top_items = OrderItem.objects.filter(order__in=base_orders).values(
        'menu_item__name'
    ).annotate(
        total_ordered=Sum('quantity')
    ).order_by('-total_ordered')[:5]
    
    agg_dash = base_orders.aggregate(
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
    
    active_branch = get_active_branch(request, restaurant)
    search_query = request.GET.get('q', '').strip()
    category_id = request.GET.get('category', '').strip()
    status_filter = request.GET.get('status', '').strip()
    sort_by = request.GET.get('sort', '').strip()

    # Get all categories for the filter dropdown (scoped to branch)
    categories = Category.objects.filter(restaurant=restaurant)
    if active_branch:
        categories = categories.filter(branch=active_branch)
    categories = categories.order_by('name')
    
    # Base queryset scoped to restaurant & branch
    base_menu_items = MenuItem.objects.select_related('category', 'subcategory__category').filter(restaurant=restaurant)
    if active_branch:
        base_menu_items = base_menu_items.filter(branch=active_branch)
    
    # Summary KPI counts
    total_items_count = base_menu_items.count()
    available_count = base_menu_items.filter(availability=True).count()
    unavailable_count = base_menu_items.filter(availability=False).count()
    total_categories_count = categories.count()

    menu_items = base_menu_items

    # Apply category filter if selected
    if category_id:
        menu_items = menu_items.filter(
            Q(category_id=category_id) | Q(subcategory__category_id=category_id)
        )
    
    # Apply status filter if selected
    if status_filter == 'available':
        menu_items = menu_items.filter(availability=True)
    elif status_filter == 'unavailable':
        menu_items = menu_items.filter(availability=False)

    # Apply search filter if query exists
    if search_query:
        menu_items = menu_items.filter(
            Q(name__icontains=search_query) |
            Q(short_code__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(subcategory__name__icontains=search_query) |
            Q(subcategory__category__name__icontains=search_query) |
            Q(category__name__icontains=search_query)
        )
    
    # Apply sorting
    if sort_by == 'price_asc':
        menu_items = menu_items.order_by('price')
    elif sort_by == 'price_desc':
        menu_items = menu_items.order_by('-price')
    elif sort_by == 'name_asc':
        menu_items = menu_items.order_by('name')
    elif sort_by == 'name_desc':
        menu_items = menu_items.order_by('-name')
    else:
        # Default: category and name
        menu_items = menu_items.order_by('category__name', 'subcategory__category__name', 'name')
    
    # Paginate the menu items
    try:
        per_page = int(request.GET.get('per_page', 16))
    except ValueError:
        per_page = 16
    paginator = Paginator(menu_items, per_page)
    page_number = request.GET.get('page')
    
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    
    # Get current URL parameters for pagination
    url_params = request.GET.copy()
    if 'page' in url_params:
        del url_params['page']
    
    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    context = {
        'menu_items': page_obj,
        'categories': categories,
        'search_query': search_query,
        'selected_category': category_id,
        'status_filter': status_filter,
        'sort_by': sort_by,
        'total_items_count': total_items_count,
        'available_count': available_count,
        'unavailable_count': unavailable_count,
        'total_categories_count': total_categories_count,
        'active_menu': 'menu_list',
        'title': 'Menu Management',
        'url_params': url_params.urlencode() if url_params else '',
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
    }
    
    return render(request, 'restaurant_admin/menu/dashboard.html', context)

@login_required
@user_passes_test(is_admin)
def menu_toggle_availability(request, pk, restaurant_slug=None):
    """AJAX endpoint to quickly toggle a menu item between available and out of stock."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=405)
    
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    
    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    menu_item.availability = not menu_item.availability
    menu_item.save(update_fields=['availability'])
    
    return JsonResponse({
        'success': True,
        'item_id': menu_item.id,
        'name': menu_item.name,
        'availability': menu_item.availability,
        'message': f"'{menu_item.name}' is now {'In Stock' if menu_item.availability else 'Out of Stock'}."
    })

@login_required
@user_passes_test(is_admin)
def menu_add(request, restaurant_slug=None):
    # Resolve restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            menu_item = form.save()
            messages.success(request, 'Menu item added successfully!')
            return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = MenuItemForm(restaurant=restaurant, branch=active_branch)
    
    categories_qs = Category.objects.filter(restaurant=restaurant)
    if active_branch:
        categories_qs = categories_qs.filter(branch=active_branch)

    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    return render(request, 'restaurant_admin/menu/form.html', {
        'form': form, 
        'title': 'Add Menu Item',
        'categories': categories_qs,
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
        'active_menu': 'menu_list',
    })

@login_required
@user_passes_test(is_admin)
def menu_bulk_add(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

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
                restaurant=restaurant,
                branch=active_branch
            )

            subcat_name = str(item.get('subcategory', '')).strip()
            subcategory = None
            if subcat_name:
                subcategory, _ = SubCategory.objects.get_or_create(
                    name=subcat_name,
                    category=category
                )

            raw_short_code = item.get('short_code')
            short_code = str(raw_short_code).strip() if raw_short_code is not None else ''

            description = str(item.get('description', '')).strip()
            tax_type = str(item.get('tax_type', 'GST_FOOD')).strip().upper()
            valid_tax_types = [choice[0] for choice in MenuItem.TaxType.choices]
            if active_branch and not active_branch.serves_alcohol:
                tax_type = MenuItem.TaxType.GST_FOOD
            elif tax_type not in valid_tax_types:
                tax_type = MenuItem.TaxType.GST_FOOD

            availability = bool(item.get('availability', True))
            is_tax_inclusive = bool(item.get('is_tax_inclusive', False))
            image_url = str(item.get('image_url', '')).strip()

            menu_item, created = MenuItem.objects.update_or_create(
                name=name,
                restaurant=restaurant,
                branch=active_branch,
                defaults={
                    'short_code': short_code,
                    'description': description,
                    'price': price,
                    'tax_type': tax_type,
                    'is_tax_inclusive': is_tax_inclusive,
                    'category': category,
                    'subcategory': subcategory,
                    'availability': availability,
                }
            )

            if image_url and (image_url.startswith('http://') or image_url.startswith('https://')):
                try:
                    import urllib.request
                    from urllib.parse import urlparse
                    from django.core.files.base import ContentFile
                    import os
                    req = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        if response.status == 200:
                            parsed_path = urlparse(image_url).path
                            filename = os.path.basename(parsed_path) or f"bulk_item_{menu_item.id}.jpg"
                            if not any(filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                                filename += '.jpg'
                            menu_item.image.save(filename, ContentFile(response.read()), save=True)
                except Exception:
                    pass

            if created:
                created_count += 1
            else:
                updated_count += 1

        if created_count > 0 or updated_count > 0:
            branch_label = f" for branch '{active_branch.name}'" if active_branch else ""
            messages.success(request, f"Successfully imported menu items ({created_count} created, {updated_count} updated){branch_label}.")

        if error_list:
            for err in error_list[:10]:
                messages.warning(request, err)
            if len(error_list) > 10:
                messages.warning(request, f"...and {len(error_list) - 10} more errors.")

        return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)

    import json
    if active_branch is not None:
        serves_alcohol = bool(getattr(active_branch, 'serves_alcohol', False))
    elif restaurant:
        serves_alcohol = restaurant.branches.filter(serves_alcohol=True).exists()
    else:
        serves_alcohol = False

    if serves_alcohol:
        sample_items = [
            {
                "name": "Paneer Butter Masala",
                "short_code": "PBM01",
                "price": 280.00,
                "is_tax_inclusive": False,
                "tax_type": "GST_FOOD",
                "category": "Main Course",
                "subcategory": "North Indian",
                "description": "Rich cottage cheese in creamy tomato butter gravy with aromatic spices",
                "availability": True,
                "image_url": ""
            },
            {
                "name": "Kingfisher Premium Beer 650ml",
                "short_code": "KF650",
                "price": 220.00,
                "is_tax_inclusive": True,
                "tax_type": "VAT_BEER",
                "category": "Bar",
                "subcategory": "Beer",
                "description": "Chilled lager beer",
                "availability": True,
                "image_url": ""
            },
            {
                "name": "Single Malt Whiskey 30ml",
                "short_code": "SMW30",
                "price": 450.00,
                "is_tax_inclusive": False,
                "tax_type": "VAT_IMFL",
                "category": "Bar",
                "subcategory": "Spirits",
                "description": "Premium aged scotch whiskey",
                "availability": True,
                "image_url": ""
            }
        ]
    else:
        sample_items = [
            {
                "name": "Paneer Butter Masala",
                "short_code": "PBM01",
                "price": 280.00,
                "is_tax_inclusive": False,
                "tax_type": "GST_FOOD",
                "category": "Main Course",
                "subcategory": "North Indian",
                "description": "Rich cottage cheese in creamy tomato butter gravy with aromatic spices",
                "availability": True,
                "image_url": ""
            },
            {
                "name": "Butter Naan",
                "short_code": "NAAN",
                "price": 45.00,
                "is_tax_inclusive": True,
                "tax_type": "GST_FOOD",
                "category": "Breads",
                "subcategory": "Tandoori Breads",
                "description": "Crisp and fluffy traditional clay-oven flatbread brushed with butter",
                "availability": True,
                "image_url": ""
            },
            {
                "name": "Fresh Lime Soda",
                "short_code": "FLS",
                "price": 80.00,
                "is_tax_inclusive": False,
                "tax_type": "GST_FOOD",
                "category": "Beverages",
                "subcategory": "Mocktails",
                "description": "Refreshing chilled lime soda with option of sweet or salted",
                "availability": True,
                "image_url": ""
            }
        ]
    sample_json = json.dumps(sample_items, indent=2)

    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    return render(request, 'restaurant_admin/menu/bulk_add.html', {
        'title': 'Bulk Import Menu Items (JSON)',
        'sample_json': sample_json,
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
        'active_menu': 'menu_list',
    })

@login_required
@user_passes_test(is_admin)
def menu_edit(request, pk, restaurant_slug=None):
    # Resolve restaurant and ensure object belongs to it
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    item_branch = menu_item.branch or active_branch

    if request.method == 'POST':
        form = MenuItemForm(request.POST, request.FILES, instance=menu_item, restaurant=restaurant, branch=item_branch)
        if form.is_valid():
            menu_item = form.save()
            messages.success(request, 'Menu item updated successfully!')
            return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = MenuItemForm(instance=menu_item, restaurant=restaurant, branch=item_branch)
        
    categories_qs = Category.objects.filter(restaurant=restaurant)
    if item_branch:
        categories_qs = categories_qs.filter(branch=item_branch)

    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    return render(request, 'restaurant_admin/menu/form.html', {
        'form': form, 
        'title': 'Edit Menu Item',
        'categories': categories_qs,
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
        'active_menu': 'menu_list',
    })

@login_required
@user_passes_test(is_admin)
def menu_delete(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    menu_item = get_object_or_404(MenuItem, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        menu_item.delete()
        messages.success(request, 'Menu item deleted successfully!')
        return redirect('restaurant_admin:menu_list', restaurant_slug=restaurant.slug)

    active_branch = get_active_branch(request, restaurant)
    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {
        'object': menu_item, 
        'restaurant': restaurant, 
        'rslug': rslug,
        'current_branch': active_branch,
    })
@login_required
@user_passes_test(is_admin)
def order_notifications_poll(request, restaurant_slug=None):
    """
    Lightweight, high-performance polling endpoint for global order notifications.
    Returns:
      - total_orders: total count of orders for branch/restaurant
      - latest_id: the most recent order PK
      - new_orders: list of any orders created with ID > since_id
      - recent_orders: latest 5 orders for the dropdown preview
      - sound_url: custom alert sound URL if configured in RestaurantSettings
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid restaurant'}, status=403)

    active_branch = get_active_branch(request, restaurant)
    orders_qs = Order.objects.filter(restaurant=restaurant)
    if active_branch:
        orders_qs = orders_qs.filter(branch=active_branch)

    latest_order = orders_qs.select_related('table').order_by('-id').first()
    latest_id = latest_order.id if latest_order else 0
    total_orders = orders_qs.count()

    since_id_raw = request.GET.get('since_id')
    since_id = int(since_id_raw) if (since_id_raw and str(since_id_raw).isdigit()) else 0

    new_orders = []
    if since_id > 0 and latest_id > since_id:
        new_items = orders_qs.filter(id__gt=since_id).select_related('table').order_by('-id')[:10]
        for o in new_items:
            table_name = o.table.name if o.table else (f"Table {o.table_number}" if o.table_number else (o.get_order_type_display() or "Takeaway"))
            new_orders.append({
                'id': o.id,
                'order_number': o.order_number or f"#{o.id}",
                'order_type': o.get_order_type_display(),
                'order_type_raw': o.order_type,
                'table_name': table_name,
                'customer_name': o.customer_name or 'Customer',
                'total_amount': float(o.total_amount or 0),
                'status': o.get_status_display(),
                'status_raw': o.status,
                'time_str': o.order_time.strftime('%I:%M %p') if o.order_time else '',
                'detail_url': reverse('restaurant_admin:order_detail', kwargs={'restaurant_slug': restaurant.slug, 'pk': o.id}),
            })

    # Recent 5 orders for dropdown preview
    recent_items = orders_qs.select_related('table').order_by('-id')[:5]
    recent_orders = []
    for o in recent_items:
        table_name = o.table.name if o.table else (f"Table {o.table_number}" if o.table_number else (o.get_order_type_display() or "Takeaway"))
        recent_orders.append({
            'id': o.id,
            'order_number': o.order_number or f"#{o.id}",
            'order_type': o.get_order_type_display(),
            'order_type_raw': o.order_type,
            'table_name': table_name,
            'customer_name': o.customer_name or 'Customer',
            'total_amount': float(o.total_amount or 0),
            'status': o.get_status_display(),
            'status_raw': o.status,
            'time_str': o.order_time.strftime('%I:%M %p') if o.order_time else '',
            'detail_url': reverse('restaurant_admin:order_detail', kwargs={'restaurant_slug': restaurant.slug, 'pk': o.id}),
        })

    # Sound URL (with cache-busting timestamp so track updates apply instantly)
    sound_url = ''
    try:
        settings_obj = RestaurantSettings.get_settings(restaurant)
        if settings_obj and settings_obj.notification_sound and settings_obj.notification_sound.name:
            base_url = request.build_absolute_uri(settings_obj.notification_sound.url)
            ts = int(settings_obj.updated_at.timestamp()) if getattr(settings_obj, 'updated_at', None) else int(datetime.now().timestamp())
            sound_url = f"{base_url}?v={ts}"
    except Exception:
        sound_url = ''

    return JsonResponse({
        'success': True,
        'total_orders': total_orders,
        'latest_id': latest_id,
        'new_orders': new_orders,
        'recent_orders': recent_orders,
        'sound_url': sound_url,
    })


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
    try:
        per_page = int(request.GET.get('per_page', 10))
    except ValueError:
        per_page = 10
    paginator = Paginator(orders_qs, per_page)
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


def _filter_order_queryset(request, restaurant, ignore_order_type=False):
    profile = getattr(request.user, 'restaurant_profile', None)
    branch = profile.branch if profile else None
    if not branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            branch = restaurant.branches.filter(id=int(branch_id)).first()
            if branch:
                request.session[f'active_branch_{restaurant.id}'] = branch.id
        if not branch:
            branch = restaurant.branches.first()

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

    # Order Type filter
    if not ignore_order_type:
        order_type = request.GET.get('order_type', '').strip()
        if order_type:
            orders_qs = orders_qs.filter(order_type=order_type)

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
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    base_orders_qs = _filter_order_queryset(request, restaurant, ignore_order_type=True)

    # Compute live counts for each Order Type tab
    order_type_counts = {
        'all': base_orders_qs.count(),
        'dine_in': base_orders_qs.filter(order_type='dine_in').count(),
        'takeaway': base_orders_qs.filter(order_type='takeaway').count(),
        'delivery': base_orders_qs.filter(order_type='delivery').count(),
    }

    order_type_filter = request.GET.get('order_type', '').strip()
    if order_type_filter:
        orders_qs = base_orders_qs.filter(order_type=order_type_filter)
    else:
        orders_qs = base_orders_qs

    # Compute Summary Stats
    total_orders = orders_qs.count()
    total_revenue = orders_qs.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    paid_revenue = orders_qs.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')

    avg_order_value = (total_revenue / Decimal(total_orders)) if total_orders > 0 else Decimal('0.00')

    # Paginate
    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(orders_qs, per_page)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Build URL query params preserving filters for pagination
    url_params_qs = request.GET.copy()
    url_params_qs.pop('page', None)
    url_params = url_params_qs.urlencode()

    active_branch = get_active_branch(request, restaurant)
    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)

    return render(request, 'restaurant_admin/orders/records.html', {
        'orders': page_obj,
        'active_menu': 'order_records',
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'paid_revenue': paid_revenue,
        'avg_order_value': avg_order_value,
        'order_type_counts': order_type_counts,
        'period': request.GET.get('period', ''),
        'start_date': request.GET.get('start_date', ''),
        'end_date': request.GET.get('end_date', ''),
        'status_filter': request.GET.get('status', ''),
        'payment_status_filter': request.GET.get('payment_status', ''),
        'payment_method_filter': request.GET.get('payment_method', ''),
        'order_type_filter': order_type_filter,
        'search_query': request.GET.get('q', ''),
        'order_statuses': Order.OrderStatus.choices,
        'payment_statuses': Order.PaymentStatus.choices,
        'payment_methods': Order.PaymentMethod.choices,
        'order_types': Order.OrderType.choices,
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
    if restaurant_slug and slug != restaurant_slug:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("You do not have permission for this restaurant.")
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
    total = breakdown['final_total']
    if order.total_amount != total or order.discount_amount != breakdown['discount_amount']:
        order.discount_amount = breakdown['discount_amount']
        order.subtotal = breakdown['gross_subtotal']
        order.tax_amount = breakdown['cgst'] + breakdown['sgst'] + breakdown['vat']
        order.round_off_amount = breakdown['round_off_amount']
        order.total_amount = total
        order.save(update_fields=['discount_amount', 'subtotal', 'tax_amount', 'round_off_amount', 'total_amount'])

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
        'breakdown': breakdown,
        'subtotal': subtotal,
        'food_subtotal': breakdown['food_subtotal'],
        'liquor_subtotal': breakdown['liquor_subtotal'],
        'beer_subtotal': breakdown['beer_subtotal'],
        'imfl_subtotal': breakdown['imfl_subtotal'],
        'wine_subtotal': breakdown['wine_subtotal'],
        'gen_liquor_subtotal': breakdown['gen_liquor_subtotal'],
        'cgst_percentage': breakdown['cgst_percentage'],
        'sgst_percentage': breakdown['sgst_percentage'],
        'service_percentage': breakdown['service_charge_percentage'],
        'cgst_amount': breakdown['cgst'],
        'sgst_amount': breakdown['sgst'],
        'beer_vat': breakdown['beer_vat'],
        'imfl_vat': breakdown['imfl_vat'],
        'wine_vat': breakdown['wine_vat'],
        'gen_vat': breakdown['gen_vat'],
        'vat_amount': breakdown['vat'],
        'vat_beer_percentage': breakdown['vat_beer_percentage'],
        'vat_imfl_percentage': breakdown['vat_imfl_percentage'],
        'vat_wine_percentage': breakdown['vat_wine_percentage'],
        'vat_percentage': breakdown['vat_percentage'],
        'custom_charges_total': breakdown.get('custom_charges_total', Decimal('0.00')),
        'custom_charges_list': breakdown.get('custom_charges_list', []),
        'service_amount': breakdown['service_charge'],
        'discount_amount': breakdown['discount_amount'],
        'round_off_amount': breakdown['round_off_amount'],
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
    if order.status == Order.OrderStatus.READY:
        order.status = Order.OrderStatus.PENDING
    order.save(update_fields=['prep_minutes', 'prep_start', 'ready_at', 'status'])

    # Publish SSE table update
    table = order.table
    if not table and order.table_number:
        table = Table.objects.filter(restaurant=restaurant, table_number=order.table_number).first()
    if table:
        try:
            from core.sse import publish
            publish(restaurant.slug, {
                'type': 'table_status_update',
                'order_id': order.id,
                'table_id': table.id,
                'status': table.current_status
            })
        except Exception:
            pass

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
    existing_paid = order.paid_amount or Decimal('0.00')
    order_grand_total = order.total_amount if (order.total_amount is not None and order.total_amount > Decimal('0.00')) else order.calculate_bill_breakdown()['final_total']
    remaining_due_target = max(Decimal('0.00'), order_grand_total - advance_paid - existing_paid)

    new_payments = []
    if isinstance(payments_input, list) and len(payments_input) > 0:
        for p in payments_input:
            amt = safe_dec(p.get('amount'))
            if amt > 0:
                new_payments.append({
                    'method': str(p.get('method') or payment_method),
                    'amount': float(amt)
                })
        new_paid_sum = sum(safe_dec(p['amount']) for p in new_payments)
    elif data.get('paid_amount') is not None and str(data.get('paid_amount')).strip() != '':
        new_paid_sum = safe_dec(data.get('paid_amount'))
        new_payments = [{'method': payment_method, 'amount': float(new_paid_sum)}]
    elif data.get('amount') is not None and str(data.get('amount')).strip() != '':
        new_paid_sum = safe_dec(data.get('amount'))
        new_payments = [{'method': payment_method, 'amount': float(new_paid_sum)}]
    else:
        new_paid_sum = remaining_due_target
        new_payments = [{'method': payment_method, 'amount': float(remaining_due_target)}]

    # Check if user submitted full total or incremental remaining settlement
    if new_paid_sum >= (order_grand_total - advance_paid):
        total_paid_val = new_paid_sum
        valid_payments = new_payments
    else:
        total_paid_val = existing_paid + new_paid_sum
        valid_payments = list(order.payments or []) + new_payments

    due_val = max(Decimal('0.00'), order_grand_total - advance_paid - total_paid_val)

    if due_val <= Decimal('0.00'):
        order.payment_status = Order.PaymentStatus.PAID
        order.due_amount = Decimal('0.00')
        order.paid_amount = total_paid_val
    elif (total_paid_val + advance_paid) > Decimal('0.00'):
        order.payment_status = Order.PaymentStatus.PARTIALLY_PAID
        order.paid_amount = total_paid_val
        order.due_amount = due_val
    else:
        order.payment_status = Order.PaymentStatus.UNPAID
        order.paid_amount = Decimal('0.00')
        order.due_amount = max(Decimal('0.00'), order_grand_total - advance_paid)

    if order.advance_receipt and order.advance_receipt.status == 'active':
        order.advance_receipt.status = 'redeemed'
        order.advance_receipt.order = order
        order.advance_receipt.save(update_fields=['status', 'order'])

    order.payments = valid_payments
    order.payment_method = payment_method
    if order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.READY, Order.OrderStatus.PREPARING]:
        order.status = Order.OrderStatus.SERVED

    gstin_no = (data.get('gstin_no') or '').strip()
    excise_bill_no = (data.get('excise_bill_no') or data.get('excise') or '').strip()
    if gstin_no:
        order.gstin_no = gstin_no
    if excise_bill_no:
        order.excise_bill_no = excise_bill_no

    customer_gstin = (data.get('customer_gstin') or data.get('party_gstin') or '').strip().upper()
    registered_name = (data.get('registered_name') or data.get('customer_name') or '').strip()
    if registered_name and registered_name.lower() not in ['guest', 'walk-in', 'walk-in guest']:
        order.customer_name = registered_name

    if order.customer_name and order.customer_name.lower() not in ['guest', 'walk-in', 'walk-in guest']:
        party = order.party
        if not party:
            party, _ = Party.objects.get_or_create(
                restaurant=restaurant,
                name__iexact=order.customer_name,
                defaults={
                    'name': order.customer_name,
                    'party_type': Party.PartyType.CUSTOMER,
                    'phone': order.phone,
                    'gstin': customer_gstin if len(customer_gstin) == 15 else '',
                    'gst_registration_type': 'regular' if len(customer_gstin) == 15 else 'unregistered'
                }
            )
            order.party = party
        if customer_gstin and len(customer_gstin) == 15:
            party.gstin = customer_gstin
            party.gst_registration_type = 'regular'
            party.save(update_fields=['gstin', 'gst_registration_type'])

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
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    q = (request.GET.get('q') or '').strip()
    coupons = Coupon.objects.filter(restaurant=restaurant)
    if active_branch:
        coupons = coupons.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    if q:
        coupons = coupons.filter(Q(code__icontains=q) | Q(description__icontains=q))
    coupons_qs = coupons.order_by('-updated_at')

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(coupons_qs, per_page)
    page_number = request.GET.get('page')
    coupons = paginator.get_page(page_number)

    return render(request, 'restaurant_admin/coupons/list.html', {
        'coupons': coupons,
        'active_menu': 'coupons',
        'search_query': q,
        'restaurant': restaurant,
    })


@login_required
@user_passes_test(is_admin)
def coupon_add(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        form = CouponForm(request.POST, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Coupon created successfully!')
            return redirect('restaurant_admin:coupon_list', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CouponForm(restaurant=restaurant, branch=active_branch)

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
        'custom_charges_total': breakdown.get('custom_charges_total', Decimal('0.00')),
        'custom_charges_list': breakdown.get('custom_charges_list', []),
        'gst_base': breakdown['gst_base'],
        'round_off_amount': breakdown['round_off_amount'],
        'total': breakdown['final_total'],
    }

    settings_obj = RestaurantSettings.get_settings(restaurant)
    template_choice = (request.GET.get('template') or getattr(settings_obj, 'bill_template', 'thermal') or 'thermal').lower()
    if template_choice == 'standard':
        template_name = 'restaurant_admin/orders/invoice.html'
    else:
        template_name = 'restaurant_admin/orders/invoice_thermal.html'

    return render(request, template_name, context)

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

    item_ids_param = request.GET.get('items')
    if item_ids_param:
        filter_ids = [int(x) for x in item_ids_param.split(',') if x.isdigit()]
        if filter_ids:
            food_items = [it for it in food_items if it.id in filter_ids]
            bar_items = [it for it in bar_items if it.id in filter_ids]

    total_food_qty = sum(item.quantity for item in food_items)
    total_bar_qty = sum(item.quantity for item in bar_items)

    table_obj = order.table
    zone_obj = table_obj.zone if (table_obj and hasattr(table_obj, 'zone')) else None
    total_kitchen_items = sum(item.quantity for item in (food_items + bar_items))

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

    template_choice = (request.GET.get('template') or getattr(settings_obj, 'kot_template', 'thermal') or 'thermal').lower()
    if template_choice == 'standard':
        template_name = 'restaurant_admin/orders/kot.html'
    else:
        template_name = 'restaurant_admin/orders/kot_thermal.html'

    return render(request, template_name, context)

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
                if new_status == Order.OrderStatus.READY and not order.ready_at:
                    order.ready_at = timezone.now()
                order.save()
                
                # Publish SSE table update
                table = order.table
                if not table and order.table_number:
                    table = Table.objects.filter(restaurant=restaurant, table_number=order.table_number).first()
                if table:
                    try:
                        from core.sse import publish
                        publish(restaurant.slug, {
                            'type': 'table_status_update',
                            'order_id': order.id,
                            'table_id': table.id,
                            'status': table.current_status
                        })
                    except Exception:
                        pass

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': f'Order #{order.branch_order_number or order.id} status updated to {status_choices[new_status]}',
                        'status': order.get_status_display(),
                        'status_class': {
                            'P': 'warning',
                            'READY': 'primary',
                            'PR': 'info',
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
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    search_query = request.GET.get('q', '').strip()

    # Base categories queryset scoped to branch
    base_categories = Category.objects.filter(restaurant=restaurant)
    if active_branch:
        base_categories = base_categories.filter(branch=active_branch)

    # Summary KPI counts
    total_categories_count = base_categories.count()
    
    subcategories_qs = SubCategory.objects.filter(category__restaurant=restaurant)
    if active_branch:
        subcategories_qs = subcategories_qs.filter(category__branch=active_branch)
    total_subcategories_count = subcategories_qs.count()

    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant)
    if active_branch:
        menu_items_qs = menu_items_qs.filter(branch=active_branch)
    total_menu_items_count = menu_items_qs.count()

    categories_qs = base_categories.annotate(
        subcategory_count=Count('subcategories', distinct=True),
        menu_items_count=Count('menu_items', distinct=True)
    ).prefetch_related('subcategories').order_by('name')

    # Apply search filter
    if search_query:
        categories_qs = categories_qs.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(subcategories__name__icontains=search_query)
        ).distinct()

    try:
        per_page = int(request.GET.get('per_page', 12))
    except ValueError:
        per_page = 12
    paginator = Paginator(categories_qs, per_page)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    rslug = (active_branch.slug if (active_branch and active_branch.slug) else restaurant.slug)
    return render(request, 'restaurant_admin/categories/list.html', {
        'categories': page_obj,
        'search_query': search_query,
        'total_categories_count': total_categories_count,
        'total_subcategories_count': total_subcategories_count,
        'total_menu_items_count': total_menu_items_count,
        'active_menu': 'category_list',
        'restaurant': restaurant,
        'rslug': rslug,
        'current_branch': active_branch,
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
    active_branch = get_active_branch(request, restaurant)
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category added successfully!')
            return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    else:
        form = CategoryForm(restaurant=restaurant, branch=active_branch)
    return render(request, 'restaurant_admin/categories/form.html', {
        'form': form,
        'title': 'Add Category',
        'active_menu': 'category_list',
        'restaurant': restaurant,
        'rslug': restaurant.slug,
    })

@login_required
@user_passes_test(is_admin)
def category_edit(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    active_branch = get_active_branch(request, restaurant)
    category = get_object_or_404(Category, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES, instance=category, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category updated successfully!')
            return redirect('restaurant_admin:category_list', restaurant_slug=restaurant.slug)
    else:
        form = CategoryForm(instance=category, restaurant=restaurant, branch=active_branch)
    return render(request, 'restaurant_admin/categories/form.html', {
        'form': form,
        'title': 'Edit Category',
        'active_menu': 'category_list',
        'restaurant': restaurant,
        'rslug': restaurant.slug,
        'category': category,
    })

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
    subcategories_qs = SubCategory.objects.select_related('category').filter(category__restaurant=restaurant)

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(subcategories_qs, per_page)
    page_number = request.GET.get('page')
    subcategories = paginator.get_page(page_number)
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
    # Compute base URL to the frontend root from .env / settings
    from core.models import Table
    base_url = Table.get_frontend_base_url()
    
    # Resolve restaurant to inject slug into customer URL
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    # Gather params
    item_param = request.GET.get('item')
    category_param = request.GET.get('category')
    table_param = request.GET.get('table')
    branch_param = request.GET.get('branch') or request.GET.get('branch_slug')
    zone_param = request.GET.get('zone') or request.GET.get('floor')

    # Backward-compat: allow path menu_id to behave like item
    if menu_id and not item_param:
        item_param = str(menu_id)

    # Build target URL (support direct-to-cart or direct-to-menu) with restaurant slug
    dest = request.GET.get('dest')
    if dest == 'cart':
        path = f"/r/{restaurant.slug}/cart/"
    elif dest == 'menu' or table_param:
        path = f"/r/{restaurant.slug}/menu"
    else:
        path = f"/r/{restaurant.slug}/"

    url = f"{base_url}{path}"
    params = {}
    if item_param:
        params['item'] = item_param
    if category_param:
        params['category'] = category_param
    if table_param:
        params['table'] = table_param
    if branch_param:
        params['branch'] = branch_param
    if zone_param:
        params['zone'] = zone_param

    if params:
        from urllib.parse import urlencode
        query = urlencode(params)
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

    # Resolve active branch dynamically
    profile = getattr(request.user, 'restaurant_profile', None)
    branch = profile.branch if profile else None
    if not branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            branch = restaurant.branches.filter(id=int(branch_id)).first()
        if not branch:
            branch = restaurant.branches.first()

    if branch and not branch.slug:
        branch.save()

    active_branch = get_active_branch(request, restaurant)
    if not branch and active_branch:
        branch = active_branch

    custom_charges = CustomCharge.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        custom_charges = custom_charges.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    custom_charges = custom_charges.order_by('charge_name')

    doc_types = ['sales', 'purchase', 'receipt', 'challan']
    if branch and branch.serves_alcohol:
        doc_types.append('vat_bill')

    doc_sequences = {}
    for dt in doc_types:
        doc_sequences[dt] = DocumentSequenceSetting.get_sequence_setting(restaurant, dt, branch=branch)

    # Resolve branch-specific GST rates for settings display
    branch_cgst = Decimal('2.50')
    branch_sgst = Decimal('2.50')
    if branch:
        if branch.gst_registration_type == 'R18' or branch.is_hotel_tariff_7500_plus:
            branch_cgst = Decimal('9.00')
            branch_sgst = Decimal('9.00')
        elif branch.gst_registration_type in ['C', 'U']:
            branch_cgst = Decimal('0.00')
            branch_sgst = Decimal('0.00')
        else:
            branch_cgst = Decimal('2.50')
            branch_sgst = Decimal('2.50')

    if request.method == 'POST':
        form = RestaurantSettingsForm(request.POST, request.FILES, instance=settings_obj)
        branch_form = RestaurantBranchComplianceForm(request.POST, instance=branch) if branch else None
        
        form_valid = form.is_valid()
        branch_valid = branch_form.is_valid() if branch_form else True

        if form_valid and branch_valid:
            settings_instance = form.save(commit=False)
            settings_instance.restaurant = restaurant
            # Keep global settings enabled so it does not accidentally disable other branches
            settings_instance.enable_customer_ordering = True
            settings_instance.save()
            
            # Save branch-specific digital ordering & payment settings
            if branch:
                branch.enable_customer_ordering = form.cleaned_data.get('enable_customer_ordering', True)
                branch.enable_online_payment = form.cleaned_data.get('enable_online_payment', False)
                branch.enable_pay_at_counter = form.cleaned_data.get('enable_pay_at_counter', True)
                if branch_form:
                    sc = branch_form.cleaned_data.get('service_charge_percentage')
                    if sc is not None:
                        branch.service_charge_percentage = sc
                branch.save()
            
            if branch_form:
                branch_form.save()

            # Process Document Sequence Settings
            for dt in doc_types:
                seq_obj = doc_sequences[dt]
                mode = request.POST.get(f'seq_{dt}_mode')
                if mode in [DocumentSequenceSetting.GenerationMode.AUTOMATIC,
                            DocumentSequenceSetting.GenerationMode.MANUAL_OVERRIDE,
                            DocumentSequenceSetting.GenerationMode.MANUAL]:
                    seq_obj.generation_mode = mode

                prefix = request.POST.get(f'seq_{dt}_prefix')
                if prefix is not None:
                    seq_obj.prefix = prefix.strip()

                suffix = request.POST.get(f'seq_{dt}_suffix')
                if suffix is not None:
                    seq_obj.suffix = suffix.strip()

                padding = request.POST.get(f'seq_{dt}_padding')
                if padding and padding.isdigit():
                    seq_obj.padding_digits = max(1, int(padding))

                counter = request.POST.get(f'seq_{dt}_counter')
                if counter and counter.isdigit():
                    seq_obj.current_counter = max(1, int(counter))

                v_from = request.POST.get(f'seq_{dt}_valid_from')
                if v_from:
                    try:
                        seq_obj.valid_from = datetime.strptime(v_from, '%Y-%m-%d').date()
                    except ValueError:
                        seq_obj.valid_from = None
                else:
                    seq_obj.valid_from = None

                v_to = request.POST.get(f'seq_{dt}_valid_to')
                if v_to:
                    try:
                        seq_obj.valid_to = datetime.strptime(v_to, '%Y-%m-%d').date()
                    except ValueError:
                        seq_obj.valid_to = None
                else:
                    seq_obj.valid_to = None

                seq_obj.save()

            messages.success(request, 'Settings and Document Numbering Configurations updated successfully.')
            redirect_slug = branch.slug if (branch and branch.slug) else getattr(restaurant, 'slug', restaurant_slug)
            return redirect('restaurant_admin:settings', restaurant_slug=redirect_slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = RestaurantSettingsForm(instance=settings_obj)
        if branch:
            if settings_obj.cgst_percentage is None:
                form.initial['cgst_percentage'] = branch_cgst
            if settings_obj.sgst_percentage is None:
                form.initial['sgst_percentage'] = branch_sgst
            if branch.service_charge_percentage is not None:
                form.initial['service_charge_percentage'] = branch.service_charge_percentage
            form.initial['enable_customer_ordering'] = getattr(branch, 'enable_customer_ordering', True)
            form.initial['enable_online_payment'] = getattr(branch, 'enable_online_payment', False)
            form.initial['enable_pay_at_counter'] = getattr(branch, 'enable_pay_at_counter', True)
        branch_form = RestaurantBranchComplianceForm(instance=branch) if branch else None

    rslug = branch.slug if (branch and branch.slug) else getattr(restaurant, 'slug', None)

    return render(request, 'restaurant_admin/settings.html', {
        'form': form,
        'branch_form': branch_form,
        'branch': branch,
        'custom_charges': custom_charges,
        'doc_sequences': doc_sequences,
        'active_menu': 'settings',
        'title': 'Restaurant Settings',
        'restaurant': restaurant,
        'rslug': rslug,
    })


@login_required
@user_passes_test(is_admin)
def custom_charge_save(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

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
            if not charge.branch and active_branch:
                charge.branch = active_branch
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
                branch=active_branch,
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

    active_branch = get_active_branch(request, restaurant)

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
    if active_branch:
        orders_qs = orders_qs.filter(branch=active_branch)
    if start_date and end_date:
        orders_qs = orders_qs.filter(order_time__date__range=[start_date, end_date])

    total_orders = orders_qs.count()

    # Order Type breakdown & Guest count
    total_guests = orders_qs.aggregate(total=Sum('guest_count'))['total'] or 0
    dine_in_count = orders_qs.filter(order_type='dine_in').count()
    takeaway_count = orders_qs.filter(order_type='takeaway').count()
    delivery_count = orders_qs.filter(order_type='delivery').count()

    dine_in_revenue = Decimal('0.00')
    takeaway_revenue = Decimal('0.00')
    delivery_revenue = Decimal('0.00')

    # Hourly distribution (0 to 23 hours)
    hourly_orders = [0] * 24
    hourly_revenue = [0.0] * 24

    # Table sales map
    table_sales = {}

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

    for o in orders_qs.prefetch_related('items', 'items__menu_item').select_related('table', 'table__zone'):
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

        # Track channel revenue
        otype = o.order_type or 'dine_in'
        if otype == 'dine_in':
            dine_in_revenue += b['final_total']
        elif otype == 'takeaway':
            takeaway_revenue += b['final_total']
        elif otype == 'delivery':
            delivery_revenue += b['final_total']

        # Track hourly data
        if o.order_time:
            loc_time = timezone.localtime(o.order_time) if timezone.is_aware(o.order_time) else o.order_time
            h = loc_time.hour
            if 0 <= h < 24:
                hourly_orders[h] += 1
                hourly_revenue[h] += float(b['final_total'])

        # Track table sales
        if o.table:
            z_name = o.table.zone.name if (hasattr(o.table, 'zone') and o.table.zone) else 'Main Floor'
            t_key = f"{o.table.name} ({z_name})"
            if t_key not in table_sales:
                table_sales[t_key] = {'name': o.table.name, 'zone': z_name, 'orders': 0, 'revenue': Decimal('0.00')}
            table_sales[t_key]['orders'] += 1
            table_sales[t_key]['revenue'] += b['final_total']

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
    avg_spend_per_guest = (total_revenue / Decimal(str(total_guests))) if total_guests > 0 else Decimal('0.00')
    discount_rate_pct = ((total_discounts / (total_gross_subtotal or Decimal('1.00'))) * 100) if total_gross_subtotal > 0 else Decimal('0.00')

    # Peak Rush Hour
    max_hour_orders = max(hourly_orders) if total_orders > 0 else 0
    if max_hour_orders > 0:
        peak_hour_idx = hourly_orders.index(max_hour_orders)
        peak_hour_str = f"{peak_hour_idx % 12 or 12}:00 {'AM' if peak_hour_idx < 12 else 'PM'} - {(peak_hour_idx + 1) % 12 or 12}:00 {'AM' if (peak_hour_idx + 1) < 12 or (peak_hour_idx + 1) == 24 else 'PM'}"
    else:
        peak_hour_str = "No Data"

    # Category-wise sales
    category_sales_qs = OrderItem.objects.filter(order__in=orders_qs).values(
        'menu_item__category__name'
    ).annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('unit_price'))
    ).order_by('-total_sales')[:8]

    category_labels = [c['menu_item__category__name'] or 'General / Direct' for c in category_sales_qs]
    category_sales_data = [float(c['total_sales'] or 0) for c in category_sales_qs]
    category_qtys = [int(c['total_qty'] or 0) for c in category_sales_qs]

    # Top Items & Slow Items
    top_items_qs = OrderItem.objects.filter(order__in=orders_qs).values('menu_item__name', 'menu_item__tax_type').annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('unit_price'))
    ).order_by('-total_sales')[:8]

    slow_items_qs = OrderItem.objects.filter(order__in=orders_qs).values('menu_item__name', 'menu_item__tax_type').annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('unit_price'))
    ).order_by('total_sales')[:5]

    top_tables = sorted(table_sales.values(), key=lambda x: x['revenue'], reverse=True)[:5]

    sorted_dates = sorted(daily_data.keys())
    chart_labels = sorted_dates
    chart_revenue = [float(daily_data[d]['revenue']) for d in sorted_dates]
    chart_gst = [float(daily_data[d]['gst']) for d in sorted_dates]
    chart_vat = [float(daily_data[d]['vat']) for d in sorted_dates]

    hourly_labels = ["12 AM", "1 AM", "2 AM", "3 AM", "4 AM", "5 AM", "6 AM", "7 AM", "8 AM", "9 AM", "10 AM", "11 AM",
                     "12 PM", "1 PM", "2 PM", "3 PM", "4 PM", "5 PM", "6 PM", "7 PM", "8 PM", "9 PM", "10 PM", "11 PM"]

    # Channel chart data
    channel_labels = ['Dine-In', 'Takeaway', 'Delivery']
    channel_counts = [dine_in_count, takeaway_count, delivery_count]
    channel_revenues = [float(dine_in_revenue), float(takeaway_revenue), float(delivery_revenue)]

    # Payment chart data
    payment_labels = list(payment_method_totals.keys())
    payment_amounts = [float(payment_method_totals[k]) for k in payment_labels]

    context = {
        'restaurant': restaurant,
        'active_menu': 'analytics',
        'title': 'Sales & Tax Analytics',
        'range_preset': range_preset,
        'start_date': start_date,
        'end_date': end_date,
        'total_orders': total_orders,
        'aov': aov,
        'avg_spend_per_guest': avg_spend_per_guest,
        'discount_rate_pct': discount_rate_pct,
        'peak_hour_str': peak_hour_str,
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
        'slow_items': slow_items_qs,
        'top_tables': top_tables,
        'chart_labels_json': json.dumps(chart_labels),
        'chart_revenue_json': json.dumps(chart_revenue),
        'chart_gst_json': json.dumps(chart_gst),
        'chart_vat_json': json.dumps(chart_vat),
        'hourly_labels_json': json.dumps(hourly_labels),
        'hourly_orders_json': json.dumps(hourly_orders),
        'hourly_revenue_json': json.dumps(hourly_revenue),
        'channel_labels_json': json.dumps(channel_labels),
        'channel_counts_json': json.dumps(channel_counts),
        'channel_revenues_json': json.dumps(channel_revenues),
        'category_labels_json': json.dumps(category_labels),
        'category_sales_json': json.dumps(category_sales_data),
        'payment_labels_json': json.dumps(payment_labels),
        'payment_amounts_json': json.dumps(payment_amounts),
        'total_guests': total_guests,
        'dine_in_count': dine_in_count,
        'takeaway_count': takeaway_count,
        'delivery_count': delivery_count,
        'dine_in_revenue': dine_in_revenue,
        'takeaway_revenue': takeaway_revenue,
        'delivery_revenue': delivery_revenue,
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

    active_branch = get_active_branch(request, restaurant)
    qs = AdvanceReceipt.objects.filter(restaurant=restaurant)
    if active_branch:
        qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    qs = qs.select_related('party', 'order')

    search_query = request.GET.get('q', '').strip()
    if search_query:
        qs = qs.filter(
            Q(receipt_no__icontains=search_query) |
            Q(reference_number__icontains=search_query) |
            Q(booking_ref__icontains=search_query) |
            Q(customer_name__icontains=search_query) |
            Q(phone__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter in ['active', 'redeemed', 'cancelled']:
        qs = qs.filter(status=status_filter)

    # Compute Statistics (branch-scoped)
    stats_qs = AdvanceReceipt.objects.filter(restaurant=restaurant)
    if active_branch:
        stats_qs = stats_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    total_advance_collected = stats_qs.exclude(status='cancelled').aggregate(s=Sum('advance_amount'))['s'] or Decimal('0.00')
    active_receipts_count = stats_qs.filter(status='active').count()
    redeemed_receipts_count = stats_qs.filter(status='redeemed').count()

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(qs, per_page)
    page = request.GET.get('page', 1)
    try:
        receipts = paginator.page(page)
    except PageNotAnInteger:
        receipts = paginator.page(1)
    except EmptyPage:
        receipts = paginator.page(paginator.num_pages)

    customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both'])
    if active_branch:
        customer_parties = customer_parties.filter(branch=active_branch)
    customer_parties = customer_parties.order_by('name')

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

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'GET':
        seq_info = get_document_sequence_info(restaurant, 'receipt', branch=active_branch)
        next_receipt_no = seq_info['next_number']
        
        max_num = Order.objects.filter(restaurant=restaurant).aggregate(Max('branch_order_number'))['branch_order_number__max'] or 0
        if not max_num:
            last_order = Order.objects.filter(restaurant=restaurant).order_by('-id').first()
            max_num = last_order.id if last_order else 0
        next_reference_no = f"ORD-{(max_num + 1):04d}"

        customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both'])
        if active_branch:
            customer_parties = customer_parties.filter(branch=active_branch)
        customer_parties = customer_parties.order_by('name')
        parties_list = [
            {'id': p.id, 'name': p.name, 'phone': p.phone or '', 'email': p.email or ''}
            for p in customer_parties
        ]

        today_date = timezone.now().strftime('%Y-%m-%d')

        return render(request, 'restaurant_admin/receipts/create.html', {
            'is_edit': False,
            'next_receipt_no': next_receipt_no,
            'next_reference_no': next_reference_no,
            'seq_info': seq_info,
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

        receipt_type = (data.get('receipt_type') or 'on_account').strip()
        receipt_no_input = (data.get('receipt_no') or '').strip()
        reference_number = (data.get('reference_number') or '').strip()
        receipt_date_str = (data.get('receipt_date') or '').strip()
        customer_name = (data.get('customer_name') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()
        advance_amount_val = data.get('advance_amount') or 0
        payment_method = data.get('payment_method') or 'upi'
        notes = (data.get('notes') or '').strip()
        party_id = data.get('party_id')

        try:
            receipt_no = get_and_increment_document_number(
                restaurant=restaurant,
                document_type='receipt',
                branch=active_branch,
                user_input_no=receipt_no_input
            )
        except ValidationError as ve:
            if is_json:
                return JsonResponse({'success': False, 'error': str(ve)}, status=400)
            messages.error(request, str(ve))
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
                err_msg = 'Receipt amount must be greater than 0.'
                if is_json:
                    return JsonResponse({'success': False, 'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('restaurant_admin:receipt_create_page', restaurant_slug=restaurant.slug)
        except Exception:
            err_msg = 'Invalid receipt amount.'
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

        active_branch_for_receipt = get_active_branch(request, restaurant)
        party = None
        if party_id:
            party = Party.objects.filter(pk=party_id, restaurant=restaurant).first()

        if not party and customer_name.lower() not in ['guest', 'walk-in guest']:
            party_lookup = {'restaurant': restaurant, 'name__iexact': customer_name}
            if active_branch_for_receipt:
                party_lookup['branch'] = active_branch_for_receipt
            party, _ = Party.objects.get_or_create(
                **party_lookup,
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

        active_branch_for_receipt = get_active_branch(request, restaurant)
        receipt = AdvanceReceipt.objects.create(
            restaurant=restaurant,
            branch=active_branch_for_receipt,
            party=party,
            receipt_no=receipt_no,
            receipt_type=receipt_type,
            reference_number=reference_number if receipt_type == 'on_account' else '',
            booking_ref=reference_number if receipt_type == 'on_account' else '',
            customer_name=customer_name,
            phone=phone,
            email=email,
            event_date=issue_date,
            advance_amount=advance_amount,
            payment_method=payment_method,
            notes=notes,
            status=AdvanceReceipt.ReceiptStatus.ACTIVE,
            created_by=request.user,
        )

        if receipt_type == 'advance':
            user_ref_no = (data.get('reference_number') or '').strip()
            custom_branch_num = None
            if user_ref_no:
                import re
                match = re.search(r'\d+', user_ref_no)
                if match:
                    num_val = int(match.group(0))
                    if num_val > 0 and not Order.objects.filter(restaurant=restaurant, branch_order_number=num_val).exists():
                        custom_branch_num = num_val

            order_kwargs = {
                'restaurant': restaurant,
                'branch': active_branch_for_receipt,
                'party': party,
                'customer_name': customer_name or (party.name if party else 'Walk-in Guest'),
                'phone': phone or (party.phone if party else ''),
                'status': Order.OrderStatus.DRAFT,
                'order_type': Order.OrderType.DINE_IN,
                'payment_status': Order.PaymentStatus.ADVANCE_PAID if advance_amount > 0 else Order.PaymentStatus.UNPAID,
                'advance_amount_paid': advance_amount,
                'advance_receipt': receipt,
                'notes': f"Draft order linked to Advance Receipt #{receipt.receipt_no}",
            }
            if custom_branch_num:
                order_kwargs['branch_order_number'] = custom_branch_num

            draft_order = Order.objects.create(**order_kwargs)
            final_ref_no = user_ref_no or draft_order.order_number
            receipt.order = draft_order
            receipt.reference_number = final_ref_no
            receipt.booking_ref = final_ref_no
            receipt.save(update_fields=['order', 'reference_number', 'booking_ref'])

        redirect_target = reverse('restaurant_admin:receipt_list', kwargs={'restaurant_slug': restaurant.slug})

        if is_json:
            return JsonResponse({'success': True, 'redirect_url': redirect_target})

        messages.success(request, f"Payment Receipt '{receipt.receipt_no}' created successfully.")
        return redirect(redirect_target)


@login_required
@user_passes_test(is_admin)
def receipt_edit_page(request, pk, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")

    receipt = get_object_or_404(AdvanceReceipt, pk=pk, restaurant=restaurant)

    if request.method == 'GET':
        customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both']).order_by('name')
        parties_list = [
            {'id': p.id, 'name': p.name, 'phone': p.phone or '', 'email': p.email or ''}
            for p in customer_parties
        ]

        formatted_date = receipt.event_date.strftime('%Y-%m-%d') if receipt.event_date else timezone.now().strftime('%Y-%m-%d')

        return render(request, 'restaurant_admin/receipts/create.html', {
            'is_edit': True,
            'receipt': receipt,
            'today_date': formatted_date,
            'customer_parties': customer_parties,
            'customer_parties_json': json.dumps(parties_list),
            'active_menu': 'receipts',
            'restaurant': restaurant,
            'rslug': restaurant.slug if restaurant else '',
        })

    elif request.method == 'POST':
        is_json = (request.content_type == 'application/json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest')
        data = json.loads(request.body.decode('utf-8')) if (is_json and request.body) else request.POST

        receipt_type = (data.get('receipt_type') or receipt.receipt_type or 'on_account').strip()
        receipt_no = (data.get('receipt_no') or '').strip()
        reference_number = (data.get('reference_number') or '').strip()
        receipt_date_str = (data.get('receipt_date') or '').strip()
        customer_name = (data.get('customer_name') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()
        advance_amount_val = data.get('advance_amount') or 0
        payment_method = data.get('payment_method') or 'upi'
        notes = (data.get('notes') or '').strip()
        party_id = data.get('party_id')

        if not receipt_no:
            receipt_no = receipt.receipt_no

        if AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no__iexact=receipt_no).exclude(pk=receipt.pk).exists():
            err_msg = f"Receipt No. '{receipt_no}' is already used by another receipt."
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_edit_page', restaurant_slug=restaurant.slug, pk=receipt.pk)

        if not customer_name:
            err_msg = 'Customer name is required.'
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_edit_page', restaurant_slug=restaurant.slug, pk=receipt.pk)

        try:
            advance_amount = Decimal(str(advance_amount_val))
            if advance_amount <= Decimal('0.00'):
                err_msg = 'Receipt amount must be greater than 0.'
                if is_json:
                    return JsonResponse({'success': False, 'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('restaurant_admin:receipt_edit_page', restaurant_slug=restaurant.slug, pk=receipt.pk)
        except Exception:
            err_msg = 'Invalid receipt amount.'
            if is_json:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('restaurant_admin:receipt_edit_page', restaurant_slug=restaurant.slug, pk=receipt.pk)

        issue_date = receipt.event_date or timezone.now().date()
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

        # Update fields cleanly via Django ORM
        receipt.receipt_no = receipt_no
        receipt.receipt_type = receipt_type
        receipt.customer_name = customer_name
        receipt.phone = phone
        receipt.email = email
        receipt.party = party
        receipt.event_date = issue_date
        receipt.advance_amount = advance_amount
        receipt.payment_method = payment_method
        receipt.notes = notes

        if receipt_type == 'on_account':
            receipt.reference_number = reference_number
            receipt.booking_ref = reference_number
        elif receipt_type == 'advance':
            user_ref_no = reference_number.strip()
            if not receipt.order:
                active_branch_for_receipt = get_active_branch(request, restaurant)
                custom_branch_num = None
                if user_ref_no:
                    import re
                    match = re.search(r'\d+', user_ref_no)
                    if match:
                        num_val = int(match.group(0))
                        if num_val > 0 and not Order.objects.filter(restaurant=restaurant, branch_order_number=num_val).exists():
                            custom_branch_num = num_val

                order_kwargs = {
                    'restaurant': restaurant,
                    'branch': active_branch_for_receipt,
                    'party': party,
                    'customer_name': customer_name or (party.name if party else 'Walk-in Guest'),
                    'phone': phone or (party.phone if party else ''),
                    'status': Order.OrderStatus.DRAFT,
                    'order_type': Order.OrderType.DINE_IN,
                    'payment_status': Order.PaymentStatus.ADVANCE_PAID if advance_amount > 0 else Order.PaymentStatus.UNPAID,
                    'advance_amount_paid': advance_amount,
                    'advance_receipt': receipt,
                    'notes': f"Draft order linked to Advance Receipt #{receipt.receipt_no}",
                }
                if custom_branch_num:
                    order_kwargs['branch_order_number'] = custom_branch_num

                draft_order = Order.objects.create(**order_kwargs)
                final_ref_no = user_ref_no or draft_order.order_number
                receipt.order = draft_order
                receipt.reference_number = final_ref_no
                receipt.booking_ref = final_ref_no
            else:
                receipt.order.customer_name = customer_name
                receipt.order.phone = phone
                receipt.order.party = party
                receipt.order.advance_amount_paid = advance_amount
                if user_ref_no:
                    import re
                    match = re.search(r'\d+', user_ref_no)
                    if match:
                        num_val = int(match.group(0))
                        if num_val > 0 and not Order.objects.filter(restaurant=restaurant, branch_order_number=num_val).exclude(pk=receipt.order.pk).exists():
                            receipt.order.branch_order_number = num_val
                receipt.order.save()

                final_ref_no = user_ref_no or receipt.order.order_number
                receipt.reference_number = final_ref_no
                receipt.booking_ref = final_ref_no

        receipt.save()

        redirect_target = reverse('restaurant_admin:receipt_list', kwargs={'restaurant_slug': restaurant.slug})

        if is_json:
            return JsonResponse({'success': True, 'redirect_url': redirect_target})

        messages.success(request, f"Payment Receipt '{receipt.receipt_no}' updated successfully.")
        return redirect(redirect_target)


@login_required
@user_passes_test(is_admin)
def receipt_create_api(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No restaurant associated with account.'}, status=403)

    active_branch = get_active_branch(request, restaurant)

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

    receipt = AdvanceReceipt.objects.create(
        restaurant=restaurant,
        branch=active_branch,
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
        created_by=request.user,
    )

    print_url = reverse('restaurant_admin:receipt_print', kwargs={'restaurant_slug': restaurant.slug, 'pk': receipt.id})

    return JsonResponse({
        'success': True,
        'receipt': {
            'id': receipt.id,
            'receipt_no': receipt.receipt_no,
            'reference_number': receipt.reference_number,
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


# Delivery Challan Management Views
@login_required
@user_passes_test(is_admin)
def challan_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    challans = DeliveryChallan.objects.filter(restaurant=restaurant).select_related(
        'from_branch', 'to_branch', 'created_by'
    ).prefetch_related('items__inventory_item', 'items__menu_item').order_by('-dispatch_date', '-created_at')

    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        challans = challans.filter(status=status_filter)

    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(challans, per_page)
    page_number = request.GET.get('page')
    challans_page = paginator.get_page(page_number)

    return render(request, 'restaurant_admin/challans/list.html', {
        'challans': challans_page,
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

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        challan_no_input = request.POST.get('challan_no', '').strip()
        dispatch_date_str = request.POST.get('dispatch_date', '').strip()
        reason_for_transport = request.POST.get('reason_for_transport', 'Branch Transfer').strip() or 'Branch Transfer'
        transport_mode = request.POST.get('transport_mode', 'Road').strip() or 'Road'
        vehicle_no = request.POST.get('vehicle_no', '').strip()

        receiver_party_id = request.POST.get('receiver_party_id', '').strip()
        receiver_name = request.POST.get('receiver_name', '').strip()
        receiver_address = request.POST.get('receiver_address', '').strip()
        receiver_state = request.POST.get('receiver_state', '').strip()
        receiver_gstin = request.POST.get('receiver_gstin', '').strip()

        notes = request.POST.get('notes', '').strip()

        receiver_party_obj = None
        if receiver_party_id and receiver_party_id.isdigit():
            receiver_party_obj = Party.objects.filter(pk=int(receiver_party_id), restaurant=restaurant).first()
            if receiver_party_obj:
                if not receiver_name:
                    receiver_name = receiver_party_obj.name
                if not receiver_address:
                    receiver_address = receiver_party_obj.full_address or receiver_party_obj.address
                if not receiver_gstin:
                    receiver_gstin = receiver_party_obj.gstin

        try:
            challan_no = get_and_increment_document_number(
                restaurant=restaurant,
                document_type='challan',
                branch=active_branch,
                user_input_no=challan_no_input
            )
        except ValidationError as ve:
            messages.error(request, str(ve))
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        try:
            dispatch_date = datetime.strptime(dispatch_date_str, '%Y-%m-%d').date() if dispatch_date_str else timezone.now().date()
        except ValueError:
            dispatch_date = timezone.now().date()

        item_ids = request.POST.getlist('item_id[]')
        hsn_codes = request.POST.getlist('hsn_code[]')
        quantities = request.POST.getlist('quantity[]')
        units = request.POST.getlist('unit[]')
        unit_values = request.POST.getlist('unit_value[]')

        if not item_ids or len(item_ids) == 0:
            messages.error(request, "Please add at least one item to the delivery challan.")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

        try:
            with transaction.atomic():
                challan = DeliveryChallan.objects.create(
                    restaurant=restaurant,
                    challan_no=challan_no,
                    from_branch=active_branch,
                    reason_for_transport=reason_for_transport,
                    transport_mode=transport_mode,
                    vehicle_no=vehicle_no,
                    receiver_party=receiver_party_obj,
                    receiver_name=receiver_name,
                    receiver_address=receiver_address,
                    receiver_state=receiver_state or 'Rajasthan (08)',
                    receiver_gstin=receiver_gstin,
                    dispatch_date=dispatch_date,
                    status=DeliveryChallan.StatusChoices.IN_TRANSIT,
                    notes=notes,
                    created_by=request.user
                )

                total_val = Decimal('0.00')
                for idx, item_id_val in enumerate(item_ids):
                    if not item_id_val:
                        continue
                    qty_str = quantities[idx] if idx < len(quantities) else '0'
                    unit_val_str = unit_values[idx] if idx < len(unit_values) else '0'
                    hsn = hsn_codes[idx] if idx < len(hsn_codes) else ''
                    u_unit = units[idx] if idx < len(units) else ''

                    qty = Decimal(str(qty_str or '0'))
                    unit_val = Decimal(str(unit_val_str or '0'))
                    if qty <= 0:
                        continue

                    line_total = qty * unit_val
                    total_val += line_total

                    item_str = str(item_id_val)
                    if item_str.startswith('menu_'):
                        menu_id = int(item_str.replace('menu_', ''))
                        menu_item = get_object_or_404(MenuItem, pk=menu_id, restaurant=restaurant)
                        DeliveryChallanItem.objects.create(
                            challan=challan,
                            menu_item=menu_item,
                            inventory_item=None,
                            hsn_code=hsn or '2106',
                            quantity=qty,
                            unit=u_unit or 'Pcs',
                            unit_value=unit_val,
                            line_total=line_total
                        )
                    else:
                        raw_id = item_str.replace('inv_', '')
                        inv_item = get_object_or_404(InventoryItem, pk=int(raw_id), restaurant=restaurant)

                        DeliveryChallanItem.objects.create(
                            challan=challan,
                            inventory_item=inv_item,
                            menu_item=None,
                            hsn_code=hsn or getattr(inv_item, 'hsn_code', '') or '2106',
                            quantity=qty,
                            unit=u_unit or inv_item.unit or 'Units',
                            unit_value=unit_val,
                            line_total=line_total
                        )

                        # Deduct quantity from inventory item stock
                        inv_item.adjust(-int(qty))

                        # Record transaction log
                        InventoryTransaction.objects.create(
                            inventory_item=inv_item,
                            transaction_type=InventoryTransaction.TransactionType.MANUAL,
                            delta=-int(qty),
                            performed_by=request.user
                        )

                challan.total_value = total_val
                challan.cgst_amount = Decimal('0.00')
                challan.sgst_amount = Decimal('0.00')
                challan.igst_amount = Decimal('0.00')
                challan.total_tax = Decimal('0.00')
                challan.save(update_fields=['total_value', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_tax'])

                messages.success(request, f"Delivery Challan #{challan_no} created successfully!")
                return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)

        except Exception as e:
            messages.error(request, f"Failed to save delivery challan: {str(e)}")
            return redirect('restaurant_admin:challan_create', restaurant_slug=restaurant.slug)

    # GET Request: Prepare form data
    seq_info = get_document_sequence_info(restaurant, 'challan', branch=active_branch)
    auto_challan_no = seq_info['next_number']

    inventory_items = InventoryItem.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        inventory_items = inventory_items.filter(branch=active_branch)
    inventory_items = inventory_items.order_by('name')

    menu_items = MenuItem.objects.filter(restaurant=restaurant, availability=True)
    if active_branch:
        menu_items = menu_items.filter(branch=active_branch)
    menu_items = menu_items.order_by('name')

    customer_parties = Party.objects.filter(restaurant=restaurant)
    if active_branch:
        customer_parties = customer_parties.filter(branch=active_branch)
    customer_parties = customer_parties.order_by('name')

    sender_name = active_branch.name if active_branch and hasattr(active_branch, 'name') and active_branch.name else (restaurant.name if restaurant else '')
    sender_address = (active_branch.address_line_1 + (' ' + active_branch.address_line_2 if active_branch.address_line_2 else '') + (', ' + active_branch.city if active_branch.city else '')) if active_branch and active_branch.address_line_1 else getattr(restaurant, 'address', '')
    sender_state = (active_branch.state + (f" ({active_branch.state_code})" if hasattr(active_branch, 'state_code') and active_branch.state_code else '')) if active_branch and active_branch.state else 'Rajasthan (08)'
    sender_gstin = get_branch_gstin_str(active_branch, restaurant)

    party_name = request.GET.get('party_name', '').strip()
    party_gstin = request.GET.get('party_gstin', '').strip()
    party_phone = request.GET.get('party_phone', '').strip()
    party_address = request.GET.get('party_address', '').strip()

    prefill_notes = request.GET.get('notes', '').strip()
    if not prefill_notes and (party_name or party_gstin):
        gstin_str = f" (GSTIN: {party_gstin})" if party_gstin else ''
        prefill_notes = f"Inter-branch/godown transfer for buyer party '{party_name}'{gstin_str}."

    customer_parties_json = json.dumps([
        {
            'id': p.id,
            'name': p.name,
            'phone': p.phone or '',
            'gstin': p.gstin or '',
            'address': p.full_address or p.address or ''
        }
        for p in customer_parties
    ])

    return render(request, 'restaurant_admin/challans/create.html', {
        'auto_challan_no': auto_challan_no,
        'seq_info': seq_info,
        'today_date': timezone.now().strftime('%Y-%m-%d'),
        'inventory_items': inventory_items,
        'menu_items': menu_items,
        'customer_parties': customer_parties,
        'customer_parties_json': customer_parties_json,
        'restaurant': restaurant,
        'active_menu': 'challans',
        'sender_name': sender_name,
        'sender_address': sender_address,
        'sender_state': sender_state,
        'sender_gstin': sender_gstin,
        'party_name': party_name,
        'party_gstin': party_gstin,
        'party_phone': party_phone,
        'party_address': party_address,
        'prefill_notes': prefill_notes,
    })


@login_required
@user_passes_test(is_admin)
def challan_detail(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    challan = get_object_or_404(
        DeliveryChallan.objects.select_related('from_branch', 'to_branch', 'receiver_party', 'created_by').prefetch_related('items__inventory_item', 'items__menu_item'),
        pk=pk,
        restaurant=restaurant
    )

    active_branch = challan.from_branch or get_active_branch(request, restaurant)
    sender_name = active_branch.name if active_branch and hasattr(active_branch, 'name') and active_branch.name else (restaurant.name if restaurant else '')
    sender_address = (active_branch.address_line_1 + (' ' + active_branch.address_line_2 if active_branch.address_line_2 else '') + (', ' + active_branch.city if active_branch.city else '')) if active_branch and active_branch.address_line_1 else getattr(restaurant, 'address', '')
    sender_state = (active_branch.state + (f" ({active_branch.state_code})" if hasattr(active_branch, 'state_code') and active_branch.state_code else '')) if active_branch and active_branch.state else 'Rajasthan (08)'
    sender_gstin = get_branch_gstin_str(active_branch, restaurant)

    receiver_name = challan.receiver_name or (challan.receiver_party.name if challan.receiver_party else '')
    receiver_address = challan.receiver_address or (challan.receiver_party.full_address if challan.receiver_party else '')
    receiver_state = challan.receiver_state or 'Rajasthan (08)'
    receiver_gstin = challan.receiver_gstin or (challan.receiver_party.gstin if challan.receiver_party else '')

    return render(request, 'restaurant_admin/challans/detail.html', {
        'challan': challan,
        'restaurant': restaurant,
        'active_menu': 'challans',
        'sender_name': sender_name,
        'sender_address': sender_address,
        'sender_state': sender_state,
        'sender_gstin': sender_gstin,
        'receiver_name': receiver_name,
        'receiver_address': receiver_address,
        'receiver_state': receiver_state,
        'receiver_gstin': receiver_gstin,
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
            messages.success(request, f"Challan #{challan.challan_no} marked as RECEIVED.")

    except Exception as e:
        messages.error(request, f"Failed to mark challan as received: {str(e)}")

    return redirect('restaurant_admin:challan_list', restaurant_slug=restaurant.slug)


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
                if item_record.inventory_item:
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
    branch_obj = get_active_branch(request, restaurant)
    tables_qs = Table.objects.filter(restaurant=restaurant)
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True).select_related('category', 'subcategory')
    categories_qs = Category.objects.filter(restaurant=restaurant)
    if branch_obj:
        tables_qs = tables_qs.filter(Q(branch=branch_obj) | Q(branch__isnull=True))
        menu_items_qs = menu_items_qs.filter(branch=branch_obj)
        categories_qs = categories_qs.filter(branch=branch_obj)
    tables_qs = tables_qs.order_by('table_number')
    menu_items_qs = menu_items_qs.order_by('category__name', 'name')
    categories_qs = categories_qs.order_by('name')

    settings_obj = RestaurantSettings.get_settings(restaurant)

    if settings_obj and settings_obj.cgst_percentage is not None:
        cgst_pct = float(settings_obj.cgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            cgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            cgst_pct = 0.00
        else:
            cgst_pct = 2.50
    else:
        cgst_pct = 2.50

    if settings_obj and settings_obj.sgst_percentage is not None:
        sgst_pct = float(settings_obj.sgst_percentage)
    elif branch_obj:
        if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
            sgst_pct = 9.00
        elif branch_obj.gst_registration_type in ['C', 'U']:
            sgst_pct = 0.00
        else:
            sgst_pct = 2.50
    else:
        sgst_pct = 2.50
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
        branch_serves_alcohol = bool(branch_obj and branch_obj.serves_alcohol)
        if branch_serves_alcohol and tax_type == 'GST_FOOD' and cat_name:
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
            'tax_type': tax_type,
            'is_tax_inclusive': mi.is_tax_inclusive
        })

    categories_list = [{'id': c.id, 'name': c.name} for c in categories_qs]

    top_selling_filter = {'order__restaurant': restaurant}
    if branch_obj:
        top_selling_filter['order__branch'] = branch_obj
    top_selling_qs = (
        OrderItem.objects.filter(**top_selling_filter)
        .values('menu_item_id')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')[:12]
    )
    top_selling_ids = [item['menu_item_id'] for item in top_selling_qs if item['menu_item_id']]

    active_orders_qs = Order.objects.filter(
        restaurant=restaurant,
        status__in=[Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, Order.OrderStatus.READY]
    )
    if branch_obj:
        active_orders_qs = active_orders_qs.filter(branch=branch_obj)
    active_orders_qs = active_orders_qs.select_related('table').prefetch_related('items', 'items__menu_item').order_by('-order_time')[:30]

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

    custom_charges_qs = CustomCharge.objects.filter(restaurant=restaurant, is_active=True)
    if branch_obj:
        custom_charges_qs = custom_charges_qs.filter(Q(branch=branch_obj) | Q(branch__isnull=True))
    custom_charges_qs = custom_charges_qs.order_by('charge_name')
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

    existing_order_json = json.dumps(serialize_pos_order(draft_order)) if draft_order else 'null'

    customer_parties = Party.objects.filter(restaurant=restaurant, party_type__in=['customer', 'both'])
    if branch_obj:
        customer_parties = customer_parties.filter(Q(branch=branch_obj) | Q(branch__isnull=True))
    customer_parties = customer_parties.order_by('name')

    # Document Sequences Info
    seq_info_sales = get_document_sequence_info(restaurant, 'sales', branch=branch_obj)
    seq_info_purchase = get_document_sequence_info(restaurant, 'purchase', branch=branch_obj)
    seq_info_receipt = get_document_sequence_info(restaurant, 'receipt', branch=branch_obj)
    seq_info_challan = get_document_sequence_info(restaurant, 'challan', branch=branch_obj)
    seq_info_vat_bill = get_document_sequence_info(restaurant, 'vat_bill', branch=branch_obj) if (branch_obj and branch_obj.serves_alcohol) else None

    active_branch_gstin = get_branch_gstin_str(branch_obj, restaurant)

    # Advance Receipt Context Data
    next_receipt_no = seq_info_receipt['next_number']
    parties_list = [
        {
            'id': p.id,
            'name': p.name,
            'phone': p.phone or '',
            'email': p.email or '',
            'gstin': p.gstin or '',
            'full_address': p.full_address or ''
        }
        for p in customer_parties
    ]

    # Purchase Context Data
    inventory_items = InventoryItem.objects.filter(restaurant=restaurant)
    if branch_obj:
        inventory_items = inventory_items.filter(branch=branch_obj)
    inventory_items = inventory_items.order_by('name')
    inventory_items_list = [
        {'id': i.id, 'name': i.name, 'cost_price': float(i.cost_price or 0), 'unit': i.unit or 'Units'}
        for i in inventory_items
    ]
    suppliers = Supplier.objects.filter(restaurant=restaurant)
    if branch_obj:
        suppliers = suppliers.filter(branch=branch_obj)
    suppliers = suppliers.order_by('name')
    next_purchase_no = seq_info_purchase['next_number']

    # Delivery Challan Context Data
    auto_challan_no = seq_info_challan['next_number']

    context = {
        'active_type': active_type,
        'today_date': today_date,
        'restaurant': restaurant,
        'rslug': restaurant.slug,
        'active_menu': 'transactions_create',
        'active_branch_gstin': active_branch_gstin,
        'active_branch_gstin_json': json.dumps(active_branch_gstin),

        'seq_info_sales': seq_info_sales,
        'seq_info_purchase': seq_info_purchase,
        'seq_info_receipt': seq_info_receipt,
        'seq_info_challan': seq_info_challan,
        'seq_info_vat_bill': seq_info_vat_bill,

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
            'service_charge_pct': float(branch_obj.service_charge_percentage if (branch_obj and branch_obj.service_charge_percentage is not None) else (settings_obj.service_charge_percentage or 0)),
            'service_charge_taxable': getattr(settings_obj, 'service_charge_taxable', True),
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
    }

    return render(request, 'restaurant_admin/transactions/create.html', context)