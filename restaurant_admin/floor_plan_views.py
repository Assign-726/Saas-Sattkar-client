import json
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from core.models import Zone, Table
from django.shortcuts import render

@login_required
def floor_plan_api(request, restaurant_slug=None):
    try:
        # Check standard admin relationships
        restaurant = request.user.restaurant
    except Exception:
        # Fallback to looking for related restaurant profile
        if hasattr(request.user, 'restaurant_profile'):
            restaurant = request.user.restaurant_profile.restaurant
        else:
            restaurant = getattr(request.user, 'restaurant', None)

    if not restaurant:
        return JsonResponse({'error': 'No restaurant found for this user.'}, status=400)

    zones = Zone.objects.filter(restaurant=restaurant).prefetch_related('tables')
    
    from core.models import Order
    data = []
    for zone in zones:
        zone_data = {
            'id': zone.id,
            'name': zone.name,
            'tables': []
        }
        for table in zone.tables.filter(is_active=True).order_by('table_number'):
            active_orders = table.orders.exclude(status='C').exclude(payment_status='F')
            if not active_orders.exists() and table.restaurant and table.table_number:
                active_orders = Order.objects.filter(
                    restaurant=table.restaurant,
                    table_number=table.table_number
                ).exclude(status='C').exclude(payment_status='F')

            latest_order = active_orders.first()
            active_order_data = None
            if latest_order:
                active_order_data = {
                    'id': latest_order.id,
                    'order_number': latest_order.order_number,
                    'customer_name': latest_order.customer_name,
                    'total_amount': str(latest_order.total_amount),
                    'status': latest_order.status,
                    'status_display': latest_order.get_status_display(),
                    'payment_status': latest_order.payment_status,
                    'payment_status_display': latest_order.get_payment_status_display(),
                }

            zone_data['tables'].append({
                'id': table.id,
                'name': table.name,
                'table_number': table.table_number,
                'capacity': table.capacity,
                'status': table.current_status,
                'active_order': active_order_data,
            })
        data.append(zone_data)
        
    return JsonResponse({'zones': data})

@login_required
def floor_plan_dashboard(request, restaurant_slug=None):
    try:
        restaurant = request.user.restaurant
    except Exception:
        restaurant = getattr(request.user, 'restaurant', None)
        
    restaurant_slug = restaurant.slug if restaurant else 'default'
    return render(request, 'restaurant_admin/tables/live_floor_plan.html', {
        'restaurant_slug': restaurant_slug
    })

from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from super_admin.models import Restaurant
from restaurant_admin.forms import ZoneForm, TableForm
from core.models import Zone, Table

@login_required
def zone_list(request, restaurant_slug=None):
    return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)

@login_required
def zone_add(request, restaurant_slug=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    if request.method == 'POST':
        form = ZoneForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Zone added successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = ZoneForm(restaurant=restaurant)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Add Zone', 'restaurant_slug': restaurant_slug})

@login_required
def zone_edit(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    zone = get_object_or_404(Zone, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = ZoneForm(request.POST, instance=zone, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Zone updated successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = ZoneForm(instance=zone, restaurant=restaurant)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Edit Zone', 'restaurant_slug': restaurant_slug})

@login_required
def zone_delete(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    zone = get_object_or_404(Zone, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        zone.delete()
        messages.success(request, 'Zone deleted successfully.')
        return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': zone, 'restaurant': restaurant, 'restaurant_slug': restaurant_slug})

@login_required
def table_list(request, restaurant_slug=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    tables = Table.objects.filter(restaurant=restaurant).order_by('zone', 'table_number')
    zones = Zone.objects.filter(restaurant=restaurant).order_by('name')
    return render(request, 'restaurant_admin/tables/table_list.html', {
        'tables': tables,
        'zones': zones,
        'restaurant_slug': restaurant_slug,
        'active_menu': 'tables',
        'restaurant': restaurant
    })

@login_required
def table_add(request, restaurant_slug=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    initial_data = {}
    if 'zone' in request.GET:
        initial_data['zone'] = request.GET.get('zone')

    if request.method == 'POST':
        form = TableForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Table added successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = TableForm(restaurant=restaurant, initial=initial_data)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Add Table', 'restaurant_slug': restaurant_slug})

@login_required
def table_edit(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    table = get_object_or_404(Table, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = TableForm(request.POST, instance=table, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Table updated successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = TableForm(instance=table, restaurant=restaurant)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Edit Table', 'restaurant_slug': restaurant_slug})

@login_required
def table_delete(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    table = get_object_or_404(Table, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        table.delete()
        messages.success(request, 'Table deleted successfully.')
        return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': table, 'restaurant': restaurant, 'restaurant_slug': restaurant_slug})
