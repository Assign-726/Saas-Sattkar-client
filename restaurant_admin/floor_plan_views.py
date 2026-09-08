import json
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from core.models import Zone, Table
from django.shortcuts import render
from django.core.paginator import Paginator

def ensure_default_zone(restaurant, active_branch=None):
    if not restaurant:
        return None
    from django.db.models import Q
    zone_qs = Zone.objects.filter(restaurant=restaurant, name__iexact='Main Area')
    if active_branch:
        zone_qs = zone_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    if not zone_qs.exists():
        return Zone.objects.create(
            restaurant=restaurant,
            branch=active_branch,
            name='Main Area'
        )
    return zone_qs.first()

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

    profile = getattr(request.user, 'restaurant_profile', None)
    active_branch = profile.branch if profile else None
    if not active_branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            active_branch = restaurant.branches.filter(id=int(branch_id)).first()
        if not active_branch:
            active_branch = restaurant.branches.first()

    ensure_default_zone(restaurant, active_branch)

    from django.db.models import Q
    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        zones = zones.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    zones = zones.prefetch_related('tables')
    
    from core.models import Order
    data = []
    for zone in zones:
        zone_data = {
            'id': zone.id,
            'name': zone.name,
            'tables': []
        }
        for table in zone.tables.filter(is_active=True).order_by('table_number'):
            active_orders = table.orders.exclude(status='C').exclude(payment_status__in=['F', 'paid', 'P'])
            if active_branch:
                active_orders = active_orders.filter(branch=active_branch)

            latest_order = active_orders.order_by('-order_time').first()
            if not latest_order:
                # If table has a recent order still associated before final clear
                recent = table.orders.exclude(status='C').exclude(payment_status='F').order_by('-order_time').first()
                if recent and table.current_status != 'BLANK':
                    latest_order = recent
            active_order_data = None
            if latest_order:
                active_order_data = {
                    'id': latest_order.id,
                    'order_number': latest_order.order_number,
                    'customer_name': latest_order.customer_name,
                    'total_amount': str(latest_order.total_amount),
                    'status': latest_order.status,
                    'status_display': 'Ready' if latest_order.is_ready else latest_order.get_status_display(),
                    'payment_status': latest_order.payment_status,
                    'payment_status_display': latest_order.get_payment_status_display(),
                }

            zone_data['tables'].append({
                'id': table.id,
                'name': table.name,
                'table_number': table.table_number,
                'capacity': table.capacity,
                'shape': table.shape or 'round',
                'is_placed': table.is_placed,
                'position_x': table.position_x or 0,
                'position_y': table.position_y or 0,
                'rotation': table.rotation or 0,
                'zone_id': zone.id,
                'zone_name': zone.name,
                'status': table.current_status,
                'active_order': active_order_data,
            })
        data.append(zone_data)
        
    return JsonResponse({'zones': data})

@login_required
def floor_plan_dashboard(request, restaurant_slug=None):
    from super_admin.models import Restaurant
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    ensure_default_zone(restaurant, active_branch)
    
    from django.db.models import Q
    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        zones = zones.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    zones = zones.order_by('name')

    tables_all_qs = Table.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_all_qs = tables_all_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    tables_qs = tables_all_qs.order_by('zone', 'table_number')

    all_tables_data = []
    for t in tables_qs:
        all_tables_data.append({
            'id': t.id,
            'name': t.name,
            'table_number': t.table_number,
            'capacity': t.capacity,
            'shape': t.shape or 'round',
            'is_placed': t.is_placed,
            'position_x': t.position_x or 0,
            'position_y': t.position_y or 0,
            'rotation': t.rotation or 0,
            'status': t.current_status,
            'zone_id': t.zone_id,
            'zone_name': t.zone.name if t.zone else '',
            'is_active': t.is_active,
        })
    all_tables_json = json.dumps(all_tables_data)
    zones_json = json.dumps([{'id': z.id, 'name': z.name} for z in zones])

    single_form = TableForm(restaurant=restaurant, branch=active_branch)
    bulk_form = BulkTableForm(restaurant=restaurant, branch=active_branch)
    zone_form = ZoneForm(restaurant=restaurant, branch=active_branch)

    return render(request, 'restaurant_admin/tables/live_floor_plan.html', {
        'restaurant_slug': restaurant_slug,
        'restaurant': restaurant,
        'active_menu': 'floor_plan',
        'zones': zones,
        'single_form': single_form,
        'bulk_form': bulk_form,
        'zone_form': zone_form,
        'all_tables_json': all_tables_json,
        'zones_json': zones_json,
    })

@login_required
def floor_plan_save_position_api(request, restaurant_slug=None):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required.'}, status=405)
    
    from super_admin.models import Restaurant
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)

    try:
        payload = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    items = payload.get('positions')
    if not items and 'table_id' in payload:
        items = [payload]

    if not items:
        return JsonResponse({'error': 'No position data provided.'}, status=400)

    from django.db.models import Q
    updated_count = 0
    for item in items:
        t_id = item.get('table_id')
        if not t_id:
            continue
        
        table_qs = Table.objects.filter(id=t_id, restaurant=restaurant)
        if active_branch:
            table_qs = table_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
        table = table_qs.first()
        
        if table:
            fields_to_update = []
            if 'is_placed' in item:
                table.is_placed = bool(item.get('is_placed'))
                fields_to_update.append('is_placed')
            if 'position_x' in item:
                table.position_x = max(0, int(item.get('position_x') or 0))
                fields_to_update.append('position_x')
            if 'position_y' in item:
                table.position_y = max(0, int(item.get('position_y') or 0))
                fields_to_update.append('position_y')
            if 'rotation' in item:
                table.rotation = int(item.get('rotation') or 0) % 360
                fields_to_update.append('rotation')
            if 'zone_id' in item and item.get('zone_id'):
                new_zone = Zone.objects.filter(id=item.get('zone_id'), restaurant=restaurant).first()
                if new_zone:
                    table.zone = new_zone
                    fields_to_update.append('zone')
            
            if fields_to_update:
                table.save(update_fields=fields_to_update)
            updated_count += 1

    return JsonResponse({'status': 'success', 'updated': updated_count})

from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from super_admin.models import Restaurant
from restaurant_admin.forms import ZoneForm, TableForm, BulkTableForm
from core.models import Zone, Table

def get_view_active_branch(request, restaurant):
    profile = getattr(request.user, 'restaurant_profile', None)
    active_branch = profile.branch if profile else None
    if not active_branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            active_branch = restaurant.branches.filter(id=int(branch_id)).first()
        if not active_branch:
            active_branch = restaurant.branches.first()
    return active_branch

@login_required
def zone_list(request, restaurant_slug=None):
    return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)

@login_required
def zone_add(request, restaurant_slug=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    if request.method == 'POST':
        form = ZoneForm(request.POST, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Zone added successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = ZoneForm(restaurant=restaurant, branch=active_branch)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Add Zone', 'restaurant_slug': restaurant_slug})

@login_required
def zone_edit(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    zone = get_object_or_404(Zone, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = ZoneForm(request.POST, instance=zone, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Zone updated successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = ZoneForm(instance=zone, restaurant=restaurant, branch=active_branch)
    return render(request, 'restaurant_admin/tables/form.html', {'form': form, 'title': 'Edit Zone', 'restaurant_slug': restaurant_slug})

@login_required
def zone_delete(request, restaurant_slug=None, pk=None):
    from django.db.models import ProtectedError
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    zone = get_object_or_404(Zone, pk=pk, restaurant=restaurant)

    default_names = ['main area', 'main floor', 'default', 'general']
    if zone.name.strip().lower() in default_names:
        messages.error(request, f"The default zone '{zone.name}' cannot be deleted.")
        return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)

    if request.method == 'POST':
        if zone.tables.exists():
            count = zone.tables.count()
            messages.error(request, f"Cannot delete zone '{zone.name}' because {count} table(s) are currently assigned to it. Please reassign or delete the tables first.")
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
        try:
            zone.delete()
            messages.success(request, 'Zone deleted successfully.')
        except ProtectedError:
            messages.error(request, f"Cannot delete zone '{zone.name}' because tables are currently assigned to it.")
        return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    return render(request, 'restaurant_admin/confirm_delete.html', {'object': zone, 'restaurant': restaurant, 'restaurant_slug': restaurant_slug})

@login_required
def table_list(request, restaurant_slug=None):
    from django.db.models import Q
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    ensure_default_zone(restaurant, active_branch)
    tables_all_qs = Table.objects.filter(restaurant=restaurant)
    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_all_qs = tables_all_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
        zones = zones.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    tables_qs = tables_all_qs.order_by('zone', 'table_number')
    zones = zones.order_by('name')

    all_tables_data = []
    for t in tables_qs:
        all_tables_data.append({
            'id': t.id,
            'name': t.name,
            'table_number': t.table_number,
            'capacity': t.capacity,
            'shape': t.shape or 'round',
            'zone_id': t.zone_id,
            'zone_name': t.zone.name if t.zone else '',
            'is_active': t.is_active,
        })
    all_tables_json = json.dumps(all_tables_data)
    zones_json = json.dumps([{'id': z.id, 'name': z.name} for z in zones])
    
    try:
        per_page = int(request.GET.get('per_page', 20))
    except ValueError:
        per_page = 20
    paginator = Paginator(tables_qs, per_page)
    page_number = request.GET.get('page')
    tables = paginator.get_page(page_number)
    
    single_form = TableForm(restaurant=restaurant, branch=active_branch)
    bulk_form = BulkTableForm(restaurant=restaurant, branch=active_branch)
    zone_form = ZoneForm(restaurant=restaurant, branch=active_branch)

    return render(request, 'restaurant_admin/tables/table_list.html', {
        'tables': tables,
        'zones': zones,
        'restaurant_slug': restaurant_slug,
        'active_menu': 'tables',
        'restaurant': restaurant,
        'single_form': single_form,
        'bulk_form': bulk_form,
        'zone_form': zone_form,
        'all_tables_json': all_tables_json,
        'zones_json': zones_json,
    })

@login_required
def table_add(request, restaurant_slug=None):
    from django.db.models import Q
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    initial_data = {}
    if 'zone' in request.GET:
        initial_data['zone'] = request.GET.get('zone')

    form_type = request.POST.get('form_type', 'bulk' if request.POST.get('count') else 'single')

    if request.method == 'POST':
        if form_type == 'bulk':
            bulk_form = BulkTableForm(request.POST, restaurant=restaurant, branch=active_branch)
            single_form = TableForm(restaurant=restaurant, branch=active_branch, initial=initial_data)
            if bulk_form.is_valid():
                zone = bulk_form.cleaned_data.get('zone')
                prefix = (bulk_form.cleaned_data.get('prefix') or '').strip()
                start_number = bulk_form.cleaned_data['start_number']
                count = bulk_form.cleaned_data['count']
                capacity = bulk_form.cleaned_data['capacity']
                shape = bulk_form.cleaned_data.get('shape') or 'round'

                # Pre-flight check: Scan in-memory candidates against existing tables
                existing = Table.objects.filter(restaurant=restaurant, zone=zone)
                if active_branch:
                    existing = existing.filter(Q(branch=active_branch) | Q(branch__isnull=True))

                colliding_tables = []
                candidates = []
                for i in range(start_number, start_number + count):
                    t_num = i
                    t_name = f"{prefix}{i}" if prefix else f"{i}"
                    if existing.filter(Q(table_number=t_num) | Q(name__iexact=t_name)).exists():
                        colliding_tables.append(t_name)
                    else:
                        candidates.append((t_num, t_name))

                if colliding_tables:
                    messages.error(request, f"Collision detected: Table(s) {', '.join(colliding_tables[:5])} already exist. Entire batch operation was canceled.")
                    return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)

                # Atomically create all candidate tables
                created_count = 0
                for t_num, t_name in candidates:
                    table_obj = Table(
                        name=t_name,
                        table_number=t_num,
                        capacity=capacity,
                        shape=shape,
                        zone=zone,
                        restaurant=restaurant,
                        branch=active_branch,
                        is_active=True
                    )
                    table_obj.save()
                    created_count += 1

                messages.success(request, f"Successfully created {created_count} table(s) in bulk.")
                return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
            
            # If invalid, render page with bulk form active
            return render(request, 'restaurant_admin/tables/add_table_page.html', {
                'single_form': single_form,
                'bulk_form': bulk_form,
                'active_tab': 'bulk',
                'title': 'Add New Table(s)',
                'restaurant_slug': restaurant_slug,
                'restaurant': restaurant
            })

        else:
            # Single table creation
            single_form = TableForm(request.POST, restaurant=restaurant, branch=active_branch)
            bulk_form = BulkTableForm(restaurant=restaurant, branch=active_branch, initial=initial_data)
            if single_form.is_valid():
                single_form.save()
                messages.success(request, 'Table added successfully.')
                return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
            
            return render(request, 'restaurant_admin/tables/add_table_page.html', {
                'single_form': single_form,
                'bulk_form': bulk_form,
                'active_tab': 'single',
                'title': 'Add New Table(s)',
                'restaurant_slug': restaurant_slug,
                'restaurant': restaurant
            })
    else:
        single_form = TableForm(restaurant=restaurant, branch=active_branch, initial=initial_data)
        bulk_form = BulkTableForm(restaurant=restaurant, branch=active_branch, initial=initial_data)
        active_tab = request.GET.get('tab', 'bulk' if 'bulk' in request.GET else 'single')

    return render(request, 'restaurant_admin/tables/add_table_page.html', {
        'single_form': single_form,
        'bulk_form': bulk_form,
        'active_tab': active_tab,
        'title': 'Add New Table(s)',
        'restaurant_slug': restaurant_slug,
        'restaurant': restaurant
    })

@login_required
def table_edit(request, restaurant_slug=None, pk=None):
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    table = get_object_or_404(Table, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = TableForm(request.POST, instance=table, restaurant=restaurant, branch=active_branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Table updated successfully.')
            return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
    else:
        form = TableForm(instance=table, restaurant=restaurant, branch=active_branch)
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


@login_required
def regenerate_table_qrs(request, restaurant_slug=None):
    """
    Regenerates QR code images for all tables of the active restaurant/branch
    using the current FRONTEND_URL from .env, branch slug, and floor zone name.
    """
    from django.db.models import Q
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    
    tables_qs = Table.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_qs = tables_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

    count = 0
    for table in tables_qs:
        table.generate_qr_code(force=True)
        table.save(update_fields=['qr_code'])
        count += 1

    base_url = Table.get_frontend_base_url()
    messages.success(request, f"Successfully regenerated QR codes for {count} table(s) using frontend URL: {base_url}")
    return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)
