import json
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from core.models import Zone, Table


def _slug_restaurant(request, restaurant_slug):
    """The business in the URL (company or branch slug), checked against the user — see resolve_restaurant."""
    from django.core.exceptions import PermissionDenied
    from django.http import HttpResponse
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        raise PermissionDenied
    return restaurant
from django.shortcuts import render
from django.core.paginator import Paginator

def ensure_default_zone(restaurant, active_branch=None):
    if not restaurant:
        return None
    if active_branch:
        zone = Zone.objects.filter(restaurant=restaurant, branch=active_branch, name__iexact='Main Area').first()
        if not zone:
            zone, _ = Zone.objects.get_or_create(
                restaurant=restaurant,
                branch=active_branch,
                name='Main Area'
            )
        return zone
    else:
        zone = Zone.objects.filter(restaurant=restaurant, branch__isnull=True, name__iexact='Main Area').first()
        if not zone:
            zone, _ = Zone.objects.get_or_create(
                restaurant=restaurant,
                branch=None,
                name='Main Area'
            )
        return zone

@login_required
def floor_plan_api(request, restaurant_slug=None):
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)  # tenant check; superusers may open any business
    if not hasattr(restaurant, 'branches'):
        return JsonResponse({'error': 'No restaurant found for this user.'}, status=403)

    profile = getattr(request.user, 'restaurant_profile', None)
    active_branch = profile.branch if profile else None
    if not active_branch and restaurant:
        branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
        if branch_id and str(branch_id).isdigit():
            active_branch = restaurant.branches.filter(id=int(branch_id)).first()
        if not active_branch:
            active_branch = restaurant.branches.first()

    ensure_default_zone(restaurant, active_branch)

    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        zones = zones.filter(branch=active_branch)
    zones = zones.prefetch_related('tables', 'tables__master_table', 'tables__linked_tables')
    
    from core.models import Order
    data = []
    for zone in zones:
        zone_data = {
            'id': zone.id,
            'name': zone.name,
            'tables': []
        }
        for table in zone.tables.filter(is_active=True).order_by('table_number'):
            active_orders_qs = table.orders.exclude(status__in=['C', 'COMPLETED', 'DRAFT']).exclude(payment_status='F')
            if active_branch:
                active_orders_qs = active_orders_qs.filter(branch=active_branch)

            latest_order = active_orders_qs.order_by('-order_time').first()
            if not latest_order and table.master_table_id and table.master_table:
                m_orders = table.master_table.orders.exclude(status__in=['C', 'COMPLETED', 'DRAFT']).exclude(payment_status='F')
                if active_branch:
                    m_orders = m_orders.filter(branch=active_branch)
                m_latest = m_orders.order_by('-order_time').first()
                if m_latest:
                    latest_order = m_latest

            active_order_data = None
            if latest_order:
                active_order_data = {
                    'id': latest_order.id,
                    'order_number': latest_order.order_number,
                    'customer_name': latest_order.customer_name,
                    'total_amount': str(latest_order.total_amount),
                    'status': latest_order.status,
                    'status_display': 'Ready' if (latest_order.is_ready and latest_order.status not in ['S', 'COMPLETED', 'C']) else latest_order.get_status_display(),
                    'payment_status': latest_order.payment_status,
                    'payment_status_display': latest_order.get_payment_status_display(),
                    'is_master_order': bool(table.master_table_id and latest_order.table_id != table.id),
                }

            is_master = bool(table.party_name and not table.master_table_id) or table.linked_tables.filter(is_active=True).exists()
            is_linked = bool(table.master_table_id)

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
                'party_name': table.effective_party_name,
                'party_host': table.effective_party_host,
                'party_phone': table.effective_party_phone,
                'party_notes': table.party_notes or (table.master_table.party_notes if table.master_table else ''),
                'master_table_id': table.master_table_id,
                'master_table_name': table.master_table.name if table.master_table else '',
                'is_master': is_master,
                'is_linked': is_linked,
                'linked_table_names': list(table.linked_tables.filter(is_active=True).values_list('name', flat=True)),
            })
        data.append(zone_data)
        
    return JsonResponse({'zones': data})

@login_required
def floor_plan_dashboard(request, restaurant_slug=None):
    from super_admin.models import Restaurant
    restaurant = _slug_restaurant(request, restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    ensure_default_zone(restaurant, active_branch)
    
    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        zones = zones.filter(branch=active_branch)
    zones = zones.order_by('name')

    tables_all_qs = Table.objects.filter(restaurant=restaurant).select_related('master_table', 'zone').prefetch_related('linked_tables')
    if active_branch:
        tables_all_qs = tables_all_qs.filter(branch=active_branch)
    tables_qs = tables_all_qs.order_by('zone', 'table_number')

    all_tables_data = []
    for t in tables_qs:
        is_master = bool(t.party_name and not t.master_table_id) or t.linked_tables.filter(is_active=True).exists()
        is_linked = bool(t.master_table_id)
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
            'party_name': t.effective_party_name,
            'party_host': t.effective_party_host,
            'party_phone': t.effective_party_phone,
            'party_notes': t.party_notes or (t.master_table.party_notes if t.master_table else ''),
            'master_table_id': t.master_table_id,
            'master_table_name': t.master_table.name if t.master_table else '',
            'is_master': is_master,
            'is_linked': is_linked,
            'linked_table_names': list(t.linked_tables.filter(is_active=True).values_list('name', flat=True)),
        })
    all_tables_json = all_tables_data
    zones_json = [{'id': z.id, 'name': z.name} for z in zones]

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
    
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)  # only your own business (or a platform admin)
    if not hasattr(restaurant, 'branches'):
        return JsonResponse({'error': 'You do not have access to this restaurant.'}, status=403)
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
            table_qs = table_qs.filter(branch=active_branch)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    ensure_default_zone(restaurant, active_branch)
    tables_all_qs = Table.objects.filter(restaurant=restaurant)
    zones = Zone.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_all_qs = tables_all_qs.filter(branch=active_branch)
        zones = zones.filter(branch=active_branch)
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
    all_tables_json = all_tables_data
    zones_json = [{'id': z.id, 'name': z.name} for z in zones]
    
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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
                    existing = existing.filter(branch=active_branch)

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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
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
    restaurant = _slug_restaurant(request, restaurant_slug)
    active_branch = get_view_active_branch(request, restaurant)
    
    tables_qs = Table.objects.filter(restaurant=restaurant)
    if active_branch:
        tables_qs = tables_qs.filter(branch=active_branch)

    count = 0
    for table in tables_qs:
        table.generate_qr_code(force=True)
        table.save(update_fields=['qr_code'])
        count += 1

    base_url = Table.get_frontend_base_url()
    messages.success(request, f"Successfully regenerated QR codes for {count} table(s) using frontend URL: {base_url}")
    return redirect('restaurant_admin:table_list', restaurant_slug=restaurant_slug)


@login_required
def floor_plan_book_party_api(request, restaurant_slug=None):
    """
    POST JSON/form to book an entire zone or a group of tables for a party/event.
    Parameters:
    - scope: 'zone' or 'tables'
    - zone_id: int (when scope == 'zone')
    - table_ids: list of int
    - lead_table_id: int (master table for the party, optional)
    - party_name: str (e.g. 'Sharma Family Party')
    - party_host: str (e.g. 'Mr. Sharma')
    - party_phone: str (e.g. '9876543210')
    - party_notes: str (optional)
    """
    from django.db import transaction
    from restaurant_admin.views import resolve_restaurant, get_active_branch
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not hasattr(restaurant, 'branches'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    scope = data.get('scope', 'tables')
    zone_id = data.get('zone_id')
    raw_table_ids = data.get('table_ids') or []
    lead_table_id = data.get('lead_table_id')
    party_name = (data.get('party_name') or '').strip()
    party_host = (data.get('party_host') or '').strip()
    party_phone = (data.get('party_phone') or '').strip()
    party_notes = (data.get('party_notes') or '').strip()

    if not party_name and not party_host:
        return JsonResponse({'success': False, 'error': 'Please provide a Party Name or Host Name.'}, status=400)

    active_branch = get_active_branch(request, restaurant)
    tables_qs = Table.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        tables_qs = tables_qs.filter(branch=active_branch)

    if scope == 'zone' and zone_id:
        tables = list(tables_qs.filter(zone_id=zone_id).order_by('table_number'))
    elif raw_table_ids:
        clean_ids = [int(x) for x in raw_table_ids if str(x).isdigit()]
        tables = list(tables_qs.filter(id__in=clean_ids).order_by('table_number'))
    else:
        return JsonResponse({'success': False, 'error': 'No tables selected for booking.'}, status=400)

    if not tables:
        return JsonResponse({'success': False, 'error': 'No tables found in selection.'}, status=404)

    # Determine master table
    master_table = None
    if lead_table_id and str(lead_table_id).isdigit():
        master_table = next((t for t in tables if t.id == int(lead_table_id)), None)
    if not master_table:
        master_table = tables[0]

    with transaction.atomic():
        # Setup master table
        master_table.master_table = None
        master_table.party_name = party_name or f"{party_host}'s Party"
        master_table.party_host = party_host
        master_table.party_phone = party_phone
        master_table.party_notes = party_notes
        master_table.save(update_fields=['master_table', 'party_name', 'party_host', 'party_phone', 'party_notes'])

        # Link child tables
        for t in tables:
            if t.id != master_table.id:
                t.master_table = master_table
                t.party_name = master_table.party_name
                t.party_host = party_host
                t.party_phone = party_phone
                t.party_notes = party_notes
                t.save(update_fields=['master_table', 'party_name', 'party_host', 'party_phone', 'party_notes'])

    return JsonResponse({
        'success': True,
        'message': f'Party "{master_table.party_name}" successfully booked across {len(tables)} table(s). Lead Table: {master_table.name}.',
        'master_table_id': master_table.id,
        'master_table_name': master_table.name,
        'count': len(tables)
    })


@login_required
def floor_plan_release_party_api(request, restaurant_slug=None):
    """
    POST: Release party booking from a table, a group, or an entire zone.
    Parameters:
    - table_id: int
    - zone_id: int
    - release_group: bool (default True)
    """
    from django.db import transaction
    from restaurant_admin.views import resolve_restaurant, get_active_branch
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not hasattr(restaurant, 'branches'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    table_id = data.get('table_id')
    zone_id = data.get('zone_id')
    release_group = data.get('release_group', True)

    active_branch = get_active_branch(request, restaurant)
    tables_qs = Table.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        tables_qs = tables_qs.filter(branch=active_branch)

    tables_to_clear = []
    if zone_id:
        tables_to_clear = list(tables_qs.filter(zone_id=zone_id))
    elif table_id and str(table_id).isdigit():
        table = tables_qs.filter(id=int(table_id)).first()
        if not table:
            return JsonResponse({'success': False, 'error': 'Table not found.'}, status=404)
        
        if release_group:
            master = table.master_table if table.master_table_id else table
            tables_to_clear = [master] + list(master.linked_tables.all())
        else:
            tables_to_clear = [table]

    if not tables_to_clear:
        return JsonResponse({'success': False, 'error': 'No tables found to release.'}, status=400)

    with transaction.atomic():
        for t in tables_to_clear:
            t.master_table = None
            t.party_name = ''
            t.party_host = ''
            t.party_phone = ''
            t.party_notes = ''
            t.save(update_fields=['master_table', 'party_name', 'party_host', 'party_phone', 'party_notes'])

    return JsonResponse({
        'success': True,
        'message': f'Party booking cleared from {len(tables_to_clear)} table(s).'
    })


@login_required
def floor_plan_merge_tables_api(request, restaurant_slug=None):
    """
    POST: Merge specific tables into a master table.
    - master_table_id: int
    - child_table_ids: [int]
    - party_name: str (optional)
    """
    from django.db import transaction
    from restaurant_admin.views import resolve_restaurant, get_active_branch
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not hasattr(restaurant, 'branches'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    master_table_id = data.get('master_table_id')
    child_table_ids = data.get('child_table_ids') or []
    party_name = (data.get('party_name') or '').strip()

    if not master_table_id or not child_table_ids:
        return JsonResponse({'success': False, 'error': 'Please select a master table and at least one table to merge.'}, status=400)

    master = Table.objects.filter(pk=master_table_id, restaurant=restaurant).first()
    if not master:
        return JsonResponse({'success': False, 'error': 'Master table not found.'}, status=404)

    with transaction.atomic():
        if party_name:
            master.party_name = party_name
            master.save(update_fields=['party_name'])

        count = 0
        for cid in child_table_ids:
            if str(cid).isdigit() and int(cid) != master.id:
                child = Table.objects.filter(pk=int(cid), restaurant=restaurant).first()
                if child:
                    child.master_table = master
                    child.party_name = master.effective_party_name or f"Merged with {master.name}"
                    child.party_host = master.effective_party_host
                    child.party_phone = master.effective_party_phone
                    child.save(update_fields=['master_table', 'party_name', 'party_host', 'party_phone'])
                    count += 1

    return JsonResponse({
        'success': True,
        'message': f'Successfully merged {count} table(s) into Master Table {master.name}.'
    })

