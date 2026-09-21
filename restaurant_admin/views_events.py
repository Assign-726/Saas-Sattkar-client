import json
import uuid
from decimal import Decimal
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.utils import timezone
from django.db import transaction, models
from django.urls import reverse

from super_admin.models import Restaurant, RestaurantBranch
from events.models import ClubEvent, TicketType, EventBooking
from core.models import Zone, Table
from restaurant_admin.views import resolve_restaurant, get_active_branch, is_admin


def club_module_required(view_func):
    """Decorator checking that the active branch has has_club_module enabled."""
    def wrapper(request, restaurant_slug=None, *args, **kwargs):
        restaurant = resolve_restaurant(request, restaurant_slug)
        if isinstance(restaurant, HttpResponseForbidden):
            return restaurant
        branch = get_active_branch(request, restaurant)
        if not branch:
            messages.error(request, "No active branch found.")
            return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant.slug)

        if not branch.has_club_module and not request.user.is_superuser:
            return render(request, 'restaurant_admin/events/upgrade_required.html', {
                'restaurant': restaurant,
                'current_branch': branch,
                'rslug': restaurant.slug,
                'active_menu': 'club_events'
            })
        return view_func(request, restaurant_slug, *args, **kwargs)
    return wrapper


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_events_list(request, restaurant_slug):
    """List all club events for the active branch."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    events_qs = ClubEvent.objects.filter(branch=branch).prefetch_related('ticket_types', 'bookings').order_by('-start_datetime')

    # Summary analytics
    total_events = events_qs.count()
    upcoming_events = sum(1 for e in events_qs if e.is_upcoming)
    total_tickets_sold = sum(e.total_tickets_sold for e in events_qs)
    total_revenue = sum(e.total_revenue for e in events_qs)

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'events': events_qs,
        'total_events': total_events,
        'upcoming_events': upcoming_events,
        'total_tickets_sold': total_tickets_sold,
        'total_revenue': total_revenue,
        'active_menu': 'club_events',
    }
    return render(request, 'restaurant_admin/events/list.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_create(request, restaurant_slug):
    """Create a new Club Event with dynamic ticket tiering."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        start_datetime_str = request.POST.get('start_datetime', '').strip()
        end_datetime_str = request.POST.get('end_datetime', '').strip()
        venue_name = request.POST.get('venue_name', '').strip() or branch.name
        venue_address = request.POST.get('venue_address', '').strip() or branch.address
        age_restriction = request.POST.get('age_restriction', '21+').strip()
        dress_code = request.POST.get('dress_code', 'Smart Casual').strip()
        is_published = request.POST.get('is_published') == 'on'
        is_featured = request.POST.get('is_featured') == 'on'
        terms = request.POST.get('terms_and_conditions', '').strip()
        cover_image = request.FILES.get('cover_image')

        if not title or not start_datetime_str or not end_datetime_str:
            messages.error(request, "Please provide Event Title, Start Time, and End Time.")
            return render(request, 'restaurant_admin/events/form.html', {
                'restaurant': restaurant,
                'current_branch': branch,
                'rslug': restaurant.slug,
                'is_edit': False,
                'active_menu': 'club_events',
            })

        try:
            start_dt = datetime.fromisoformat(start_datetime_str)
            end_dt = datetime.fromisoformat(end_datetime_str)
            if timezone.is_naive(start_dt):
                start_dt = timezone.make_aware(start_dt)
            if timezone.is_naive(end_dt):
                end_dt = timezone.make_aware(end_dt)
        except ValueError:
            messages.error(request, "Invalid datetime format. Please use the date picker.")
            return render(request, 'restaurant_admin/events/form.html', {
                'restaurant': restaurant,
                'current_branch': branch,
                'rslug': restaurant.slug,
                'is_edit': False,
                'active_menu': 'club_events',
            })

        with transaction.atomic():
            event = ClubEvent.objects.create(
                branch=branch,
                title=title,
                description=description,
                cover_image=cover_image,
                start_datetime=start_dt,
                end_datetime=end_dt,
                venue_name=venue_name,
                venue_address=venue_address,
                age_restriction=age_restriction,
                dress_code=dress_code,
                is_published=is_published,
                is_featured=is_featured,
                terms_and_conditions=terms if terms else "Entry strictly allowed with valid pass. Rights of admission reserved."
            )

            # Process dynamic ticket tiers from POST form
            tier_names = request.POST.getlist('tier_name[]')
            tier_descriptions = request.POST.getlist('tier_description[]')
            tier_prices = request.POST.getlist('tier_price[]')
            tier_capacities = request.POST.getlist('tier_capacity[]')
            tier_limits = request.POST.getlist('tier_limit[]')

            for idx, name in enumerate(tier_names):
                name = name.strip()
                if not name:
                    continue
                desc = tier_descriptions[idx].strip() if idx < len(tier_descriptions) else ''
                try:
                    price = Decimal(tier_prices[idx]) if idx < len(tier_prices) and tier_prices[idx] else Decimal('0.00')
                    capacity = int(tier_capacities[idx]) if idx < len(tier_capacities) and tier_capacities[idx] else 100
                    limit = int(tier_limits[idx]) if idx < len(tier_limits) and tier_limits[idx] else 10
                except (ValueError, IndexError):
                    price = Decimal('0.00')
                    capacity = 100
                    limit = 10

                TicketType.objects.create(
                    event=event,
                    name=name,
                    description=desc,
                    base_price=price,
                    total_allocated=capacity,
                    max_per_order=limit,
                    display_order=idx
                )

        messages.success(request, f'Event "{event.title}" created successfully with ticket tiers!')
        return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant.slug)

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'is_edit': False,
        'active_menu': 'club_events',
    }
    return render(request, 'restaurant_admin/events/form.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_edit(request, restaurant_slug, pk):
    """Edit existing Club Event and manage its ticket tiers."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=pk, branch__restaurant=restaurant)
    if event.branch:
        branch = event.branch
        request.session['active_branch_id'] = branch.id

    if request.method == 'POST':
        event.title = request.POST.get('title', '').strip() or event.title
        event.description = request.POST.get('description', '').strip()
        start_datetime_str = request.POST.get('start_datetime', '').strip()
        end_datetime_str = request.POST.get('end_datetime', '').strip()
        event.venue_name = request.POST.get('venue_name', '').strip() or branch.name
        event.venue_address = request.POST.get('venue_address', '').strip() or branch.address
        event.age_restriction = request.POST.get('age_restriction', '21+').strip()
        event.dress_code = request.POST.get('dress_code', 'Smart Casual').strip()
        event.is_published = request.POST.get('is_published') == 'on'
        event.is_featured = request.POST.get('is_featured') == 'on'
        event.terms_and_conditions = request.POST.get('terms_and_conditions', '').strip()

        if request.FILES.get('cover_image'):
            event.cover_image = request.FILES.get('cover_image')

        try:
            if start_datetime_str:
                start_dt = datetime.fromisoformat(start_datetime_str)
                event.start_datetime = timezone.make_aware(start_dt) if timezone.is_naive(start_dt) else start_dt
            if end_datetime_str:
                end_dt = datetime.fromisoformat(end_datetime_str)
                event.end_datetime = timezone.make_aware(end_dt) if timezone.is_naive(end_dt) else end_dt
        except ValueError:
            pass

        with transaction.atomic():
            event.save()

            # Update / recreate ticket tiers
            tier_ids = request.POST.getlist('tier_id[]')
            tier_names = request.POST.getlist('tier_name[]')
            tier_descriptions = request.POST.getlist('tier_description[]')
            tier_prices = request.POST.getlist('tier_price[]')
            tier_capacities = request.POST.getlist('tier_capacity[]')
            tier_limits = request.POST.getlist('tier_limit[]')

            kept_ids = []
            for idx, name in enumerate(tier_names):
                name = name.strip()
                if not name:
                    continue
                desc = tier_descriptions[idx].strip() if idx < len(tier_descriptions) else ''
                price = Decimal(tier_prices[idx]) if idx < len(tier_prices) and tier_prices[idx] else Decimal('0.00')
                capacity = int(tier_capacities[idx]) if idx < len(tier_capacities) and tier_capacities[idx] else 100
                limit = int(tier_limits[idx]) if idx < len(tier_limits) and tier_limits[idx] else 10

                t_id = tier_ids[idx] if idx < len(tier_ids) and tier_ids[idx] else None
                if t_id and t_id.isdigit():
                    t_obj = TicketType.objects.filter(pk=int(t_id), event=event).first()
                    if t_obj:
                        t_obj.name = name
                        t_obj.description = desc
                        t_obj.base_price = price
                        t_obj.total_allocated = max(t_obj.quantity_sold, capacity)
                        t_obj.max_per_order = limit
                        t_obj.display_order = idx
                        t_obj.save()
                        kept_ids.append(t_obj.id)
                        continue

                new_t = TicketType.objects.create(
                    event=event,
                    name=name,
                    description=desc,
                    base_price=price,
                    total_allocated=capacity,
                    max_per_order=limit,
                    display_order=idx
                )
                kept_ids.append(new_t.id)

            # Deactivate deleted tiers that have sales; delete tiers without sales
            for old_t in event.ticket_types.exclude(id__in=kept_ids):
                if old_t.quantity_sold > 0 or old_t.bookings.exists():
                    old_t.is_active = False
                    old_t.save(update_fields=['is_active'])
                else:
                    old_t.delete()

        messages.success(request, f'Event "{event.title}" updated successfully!')
        return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant.slug)

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'event': event,
        'ticket_types': event.ticket_types.filter(is_active=True).order_by('display_order'),
        'is_edit': True,
        'active_menu': 'club_events',
    }
    return render(request, 'restaurant_admin/events/form.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_delete(request, restaurant_slug, pk):
    """Deletes an event if no confirmed bookings exist."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=pk, branch=branch)

    if request.method == 'POST':
        confirmed_count = event.bookings.filter(payment_status=EventBooking.PaymentStatus.SUCCESS).count()
        if confirmed_count > 0:
            messages.error(request, f"Cannot delete '{event.title}' because {confirmed_count} confirmed tickets have already been sold. You can unpublish it instead.")
            return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant.slug)

        title = event.title
        event.delete()
        messages.success(request, f'Event "{title}" removed.')
    return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_event_toggle_publish(request, restaurant_slug, pk):
    """Toggle event publication status."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=pk, branch=branch)

    event.is_published = not event.is_published
    event.save(update_fields=['is_published'])
    status_text = "published and live" if event.is_published else "unpublished and hidden"
    messages.success(request, f'Event "{event.title}" is now {status_text}.')
    return redirect('restaurant_admin:club_events_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_guestlist(request, restaurant_slug, event_id=None):
    """
    Comprehensive guestlist desk for the active branch.
    View all attendees, filter by event/tier/status, mark check-ins, or issue door cash passes.
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    events_list = ClubEvent.objects.filter(branch=branch).order_by('-start_datetime')
    selected_event = None
    if event_id:
        selected_event = events_list.filter(id=event_id).first()
    elif events_list.exists():
        # Default to the next upcoming event or latest event
        selected_event = events_list.filter(end_datetime__gte=timezone.now()).order_by('start_datetime').first() or events_list.first()

    bookings_qs = EventBooking.objects.filter(event__branch=branch).select_related('event', 'ticket_type')

    if selected_event:
        bookings_qs = bookings_qs.filter(event=selected_event)

    search_query = request.GET.get('q', '').strip()
    if search_query:
        bookings_qs = bookings_qs.filter(
            models.Q(guest_name__icontains=search_query) |
            models.Q(guest_phone__icontains=search_query) |
            models.Q(guest_email__icontains=search_query) |
            models.Q(booking_reference__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        bookings_qs = bookings_qs.filter(payment_status=status_filter)

    checked_in_filter = request.GET.get('checked_in', '').strip()
    if checked_in_filter in ['0', '1']:
        bookings_qs = bookings_qs.filter(checked_in=(checked_in_filter == '1'))

    tier_filter = request.GET.get('tier', '').strip()
    if tier_filter and tier_filter.isdigit():
        bookings_qs = bookings_qs.filter(ticket_type_id=int(tier_filter))

    bookings_list = bookings_qs.order_by('-created_at')

    # Metrics
    confirmed_bookings = bookings_list.filter(payment_status=EventBooking.PaymentStatus.SUCCESS)
    total_confirmed_attendees = sum(b.quantity for b in confirmed_bookings)
    checked_in_count = confirmed_bookings.filter(checked_in=True).count()
    total_sales_amount = sum(b.total_amount for b in confirmed_bookings)

    ticket_tiers = selected_event.ticket_types.filter(is_active=True) if selected_event else []

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'events_list': events_list,
        'selected_event': selected_event,
        'bookings': bookings_list,
        'ticket_tiers': ticket_tiers,
        'total_confirmed_attendees': total_confirmed_attendees,
        'checked_in_count': checked_in_count,
        'total_sales_amount': total_sales_amount,
        'search_query': search_query,
        'status_filter': status_filter,
        'checked_in_filter': checked_in_filter,
        'tier_filter': tier_filter,
        'active_menu': 'club_guestlist',
    }
    return render(request, 'restaurant_admin/events/guestlist.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_manual_booking_create(request, restaurant_slug, event_id):
    """Handles manual walk-in cash or complimentary ticket generation from Guestlist or Ticket Grid."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = ClubEvent.objects.filter(pk=event_id, branch__restaurant=restaurant).select_related('branch').first()
    if not event:
        raise Http404("Event not found")
    if event.branch:
        branch = event.branch
        request.session['active_branch_id'] = branch.id

    if request.method == 'POST':
        ticket_type_id = request.POST.get('ticket_type_id')
        guest_name = request.POST.get('guest_name', '').strip()
        guest_phone = request.POST.get('guest_phone', '').strip()
        guest_email = request.POST.get('guest_email', '').strip() or "walkin@clubvenue.local"
        quantity = int(request.POST.get('quantity', 1) or 1)
        payment_method = request.POST.get('payment_method', EventBooking.PaymentMethod.DOOR_CASH)
        notes = request.POST.get('notes', '').strip()
        next_url = request.POST.get('next')

        ticket = get_object_or_404(TicketType, pk=ticket_type_id, event=event)

        with transaction.atomic():
            locked_ticket = TicketType.objects.select_for_update().get(pk=ticket.pk)
            if locked_ticket.available_quantity < quantity:
                messages.error(request, f"Cannot issue {quantity} tickets. Only {locked_ticket.available_quantity} available.")
                if next_url and next_url.startswith('/'):
                    return redirect(next_url)
                return redirect('restaurant_admin:club_guestlist_event', restaurant_slug=restaurant.slug, event_id=event.id)

            unit_price = Decimal('0.00') if payment_method == EventBooking.PaymentMethod.COMPLIMENTARY else locked_ticket.base_price
            total_amount = unit_price * quantity
            ref = EventBooking.generate_booking_reference()

            booking = EventBooking.objects.create(
                booking_reference=ref,
                event=event,
                ticket_type=locked_ticket,
                guest_name=guest_name,
                guest_phone=guest_phone,
                guest_email=guest_email,
                quantity=quantity,
                unit_price=unit_price,
                tax_amount=Decimal('0.00'),
                total_amount=total_amount,
                payment_status=EventBooking.PaymentStatus.SUCCESS,
                payment_method=payment_method,
                notes=notes
            )

            locked_ticket.quantity_sold += quantity
            locked_ticket.save(update_fields=['quantity_sold'])

            booking.security_token = booking.generate_security_token()
            booking.generate_qr_code(force=True)
            booking.save(update_fields=['security_token', 'booking_qr_code'])

        messages.success(request, f"Issued pass {booking.booking_reference} for {guest_name} ({quantity} guest(s)) successfully!")
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('restaurant_admin:club_guestlist_event', restaurant_slug=restaurant.slug, event_id=event.id)

    return redirect('restaurant_admin:club_guestlist_event', restaurant_slug=restaurant.slug, event_id=event.id)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_booking_toggle_checkin(request, restaurant_slug, booking_id):
    """Toggle attendee check-in state from the Guestlist table."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    booking = get_object_or_404(EventBooking, pk=booking_id, event__branch=branch)

    if booking.checked_in:
        booking.checked_in = False
        booking.checked_in_at = None
        booking.checked_in_by = None
        booking.save(update_fields=['checked_in', 'checked_in_at', 'checked_in_by'])
        messages.info(request, f"Check-in undone for {booking.guest_name} ({booking.booking_reference}).")
    else:
        booking.checked_in = True
        booking.checked_in_at = timezone.now()
        booking.checked_in_by = request.user
        booking.save(update_fields=['checked_in', 'checked_in_at', 'checked_in_by'])
        messages.success(request, f"Checked in: {booking.guest_name} (+{booking.quantity - 1} guests) confirmed!")

    event_id = booking.event_id
    return redirect('restaurant_admin:club_guestlist_event', restaurant_slug=restaurant.slug, event_id=event_id)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_gate_scanner(request, restaurant_slug, event_id=None):
    """Camera QR Scanner interface for venue door staff / security."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    events_list = ClubEvent.objects.filter(branch=branch, end_datetime__gte=timezone.now() - timezone.timedelta(days=1)).order_by('start_datetime')
    selected_event = None
    if event_id:
        selected_event = events_list.filter(id=event_id).first()
    elif events_list.exists():
        selected_event = events_list.first()

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'events_list': events_list,
        'selected_event': selected_event,
        'active_menu': 'club_scanner',
    }
    return render(request, 'restaurant_admin/events/scanner.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan(request, restaurant_slug, event_id=None):
    """
    Interactive Visual Floor Plan & Table Sales Management for Club Events.
    Visualizes zones, tables, VIP table passes, occupancy, and check-in status.
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    events_list = ClubEvent.objects.filter(branch=branch).order_by('-start_datetime')
    selected_event = None
    if event_id:
        selected_event = events_list.filter(id=event_id).first()
    elif events_list.exists():
        selected_event = events_list.filter(end_datetime__gte=timezone.now()).order_by('start_datetime').first() or events_list.first()

    # Zones for this branch
    zones = Zone.objects.filter(restaurant=restaurant)
    if branch:
        zones = zones.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))
    zones = zones.order_by('name')

    # Tables for this branch
    tables_qs = Table.objects.filter(restaurant=restaurant, is_active=True)
    if branch:
        tables_qs = tables_qs.filter(branch=branch)
    tables_qs = tables_qs.select_related('zone').order_by('zone__name', 'table_number')

    confirmed_bookings = []
    table_booking_map = {}
    unassigned_bookings = []

    if selected_event:
        confirmed_bookings = list(
            EventBooking.objects.filter(
                event=selected_event,
                payment_status=EventBooking.PaymentStatus.SUCCESS
            ).select_related('ticket_type', 'assigned_table', 'assigned_table__zone')
        )

        for b in confirmed_bookings:
            if b.assigned_table_id:
                table_booking_map[b.assigned_table_id] = b
            else:
                unassigned_bookings.append(b)

    # Metrics
    total_tables = tables_qs.count()
    assigned_tables_count = len(table_booking_map)
    available_tables_count = max(0, total_tables - assigned_tables_count)
    total_seated_capacity = sum(t.capacity for t in tables_qs)
    occupied_seated_capacity = sum(b.quantity for b in table_booking_map.values())
    total_event_attendees = sum(b.quantity for b in confirmed_bookings)
    checked_in_count = sum(1 for b in confirmed_bookings if b.checked_in)
    total_sales = sum(b.total_amount for b in confirmed_bookings)

    tables_data = []
    for t in tables_qs:
        b = table_booking_map.get(t.id)
        if b:
            status_val = 'CHECKED_IN' if b.checked_in else 'RESERVED'
            booking_info = {
                'id': b.id,
                'reference': b.booking_reference,
                'guest_name': b.guest_name,
                'guest_phone': b.guest_phone,
                'guest_email': b.guest_email,
                'ticket_tier': b.ticket_type.name,
                'quantity': b.quantity,
                'total_amount': str(b.total_amount),
                'checked_in': b.checked_in,
                'checked_in_at': b.checked_in_at.strftime('%I:%M %p') if b.checked_in_at else None,
            }
        else:
            status_val = 'AVAILABLE'
            booking_info = None

        tables_data.append({
            'id': t.id,
            'name': t.name,
            'table_number': t.table_number,
            'capacity': t.capacity,
            'shape': t.shape or 'round',
            'is_placed': t.is_placed,
            'position_x': t.position_x or 0,
            'position_y': t.position_y or 0,
            'rotation': t.rotation or 0,
            'zone_id': t.zone_id,
            'zone_name': t.zone.name if t.zone else 'Unassigned Zone',
            'status': status_val,
            'booking': booking_info,
        })

    unassigned_data = [
        {
            'id': b.id,
            'reference': b.booking_reference,
            'guest_name': b.guest_name,
            'guest_phone': b.guest_phone,
            'ticket_tier': b.ticket_type.name,
            'quantity': b.quantity,
            'total_amount': str(b.total_amount),
            'checked_in': b.checked_in,
        }
        for b in unassigned_bookings
    ]

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'events_list': events_list,
        'selected_event': selected_event,
        'zones': zones,
        'tables_data': tables_data,
        'tables_json': json.dumps(tables_data),
        'zones_json': json.dumps([{'id': z.id, 'name': z.name} for z in zones]),
        'unassigned_json': json.dumps(unassigned_data),
        'unassigned_bookings': unassigned_bookings,
        'total_tables': total_tables,
        'assigned_tables_count': assigned_tables_count,
        'available_tables_count': available_tables_count,
        'total_seated_capacity': total_seated_capacity,
        'occupied_seated_capacity': occupied_seated_capacity,
        'total_event_attendees': total_event_attendees,
        'checked_in_count': checked_in_count,
        'total_sales': total_sales,
        'active_menu': 'club_floor_plan',
    }
    return render(request, 'restaurant_admin/events/floor_plan.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan_api(request, restaurant_slug, event_id):
    """API returning live JSON of floor plan tables and booking assignments."""
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=event_id, branch=branch)

    tables_qs = Table.objects.filter(restaurant=restaurant, is_active=True)
    if branch:
        tables_qs = tables_qs.filter(branch=branch)
    tables_qs = tables_qs.select_related('zone').order_by('zone__name', 'table_number')

    confirmed_bookings = list(
        EventBooking.objects.filter(
            event=event,
            payment_status=EventBooking.PaymentStatus.SUCCESS
        ).select_related('ticket_type', 'assigned_table')
    )

    table_booking_map = {b.assigned_table_id: b for b in confirmed_bookings if b.assigned_table_id}
    unassigned_bookings = [b for b in confirmed_bookings if not b.assigned_table_id]

    tables_data = []
    for t in tables_qs:
        b = table_booking_map.get(t.id)
        if b:
            status_val = 'CHECKED_IN' if b.checked_in else 'RESERVED'
            booking_info = {
                'id': b.id,
                'reference': b.booking_reference,
                'guest_name': b.guest_name,
                'guest_phone': b.guest_phone,
                'guest_email': b.guest_email,
                'ticket_tier': b.ticket_type.name,
                'quantity': b.quantity,
                'total_amount': str(b.total_amount),
                'checked_in': b.checked_in,
                'checked_in_at': b.checked_in_at.strftime('%I:%M %p') if b.checked_in_at else None,
            }
        else:
            status_val = 'AVAILABLE'
            booking_info = None

        tables_data.append({
            'id': t.id,
            'name': t.name,
            'table_number': t.table_number,
            'capacity': t.capacity,
            'shape': t.shape or 'round',
            'is_placed': t.is_placed,
            'position_x': t.position_x or 0,
            'position_y': t.position_y or 0,
            'rotation': t.rotation or 0,
            'zone_id': t.zone_id,
            'zone_name': t.zone.name if t.zone else 'Unassigned Zone',
            'status': status_val,
            'booking': booking_info,
        })

    unassigned_data = [
        {
            'id': b.id,
            'reference': b.booking_reference,
            'guest_name': b.guest_name,
            'guest_phone': b.guest_phone,
            'ticket_tier': b.ticket_type.name,
            'quantity': b.quantity,
            'total_amount': str(b.total_amount),
            'checked_in': b.checked_in,
        }
        for b in unassigned_bookings
    ]

    return JsonResponse({
        'success': True,
        'tables': tables_data,
        'unassigned': unassigned_data,
        'metrics': {
            'total_tables': len(tables_data),
            'assigned_tables': len(table_booking_map),
            'available_tables': max(0, len(tables_data) - len(table_booking_map)),
            'total_attendees': sum(b.quantity for b in confirmed_bookings),
            'checked_in': sum(1 for b in confirmed_bookings if b.checked_in),
            'total_sales': str(sum(b.total_amount for b in confirmed_bookings)),
        }
    })


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_floor_plan_assign_table(request, restaurant_slug, event_id):
    """
    POST endpoint to assign or unassign a table to an event booking.
    Body (JSON): { "booking_id": 123, "table_id": 456 } or { "booking_id": 123, "table_id": null }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=event_id, branch=branch)

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON body.'}, status=400)

    booking_id = data.get('booking_id')
    table_id = data.get('table_id')

    if not booking_id:
        return JsonResponse({'success': False, 'error': 'booking_id is required.'}, status=400)

    booking = EventBooking.objects.filter(pk=booking_id, event=event).first()
    if not booking:
        return JsonResponse({'success': False, 'error': 'Booking not found for this event.'}, status=404)

    with transaction.atomic():
        if table_id:
            # Validate table
            table = Table.objects.filter(pk=table_id, restaurant=restaurant)
            if branch:
                table = table.filter(branch=branch)
            table = table.first()
            if not table:
                return JsonResponse({'success': False, 'error': 'Table not found for this branch.'}, status=404)

            # Check if another booking already holds this table for this event
            existing_holder = EventBooking.objects.filter(
                event=event,
                assigned_table=table,
                payment_status=EventBooking.PaymentStatus.SUCCESS
            ).exclude(pk=booking.pk).first()

            if existing_holder:
                if data.get('force_swap'):
                    existing_holder.assigned_table = None
                    existing_holder.assigned_at = None
                    existing_holder.save(update_fields=['assigned_table', 'assigned_at'])
                else:
                    return JsonResponse({
                        'success': False,
                        'conflict': True,
                        'error': f'Table "{table.name}" is already assigned to {existing_holder.guest_name} ({existing_holder.booking_reference}). Do you wish to reassign?',
                        'existing_holder': {
                            'id': existing_holder.id,
                            'name': existing_holder.guest_name,
                            'reference': existing_holder.booking_reference
                        }
                    }, status=409)

            booking.assigned_table = table
            booking.assigned_at = timezone.now()
            booking.save(update_fields=['assigned_table', 'assigned_at'])

            return JsonResponse({
                'success': True,
                'message': f'Table "{table.name}" assigned to {booking.guest_name} ({booking.booking_reference}).',
                'booking_id': booking.id,
                'table_id': table.id,
                'table_name': table.name,
                'status': 'CHECKED_IN' if booking.checked_in else 'RESERVED'
            })
        else:
            # Unassign table
            prev_table_name = booking.assigned_table.name if booking.assigned_table else 'Table'
            prev_table_id = booking.assigned_table_id
            booking.assigned_table = None
            booking.assigned_at = None
            booking.save(update_fields=['assigned_table', 'assigned_at'])

            return JsonResponse({
                'success': True,
                'message': f'Unassigned from "{prev_table_name}".',
                'booking_id': booking.id,
                'table_id': prev_table_id,
                'status': 'AVAILABLE'
            })


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_ticket_grid(request, restaurant_slug, event_id=None):
    """
    Visual Ticket Sales Matrix & Occupancy Visualizer.
    Renders ticket tiers with visual numbered slots (01, 02, 03...),
    color-coded by status (Available, Sold, Checked In, Pending Hold).
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)

    if event_id:
        selected_event = ClubEvent.objects.filter(pk=event_id, branch__restaurant=restaurant).select_related('branch').first()
        if selected_event and selected_event.branch:
            branch = selected_event.branch
            request.session['active_branch_id'] = branch.id
    else:
        events_qs = ClubEvent.objects.filter(branch=branch).order_by('-start_datetime')
        if not events_qs.exists():
            # Check other branches of this restaurant
            events_qs = ClubEvent.objects.filter(branch__restaurant=restaurant).order_by('-start_datetime')

        now = timezone.now()
        selected_event = events_qs.filter(end_datetime__gte=now).order_by('start_datetime').first() or events_qs.first()
        if selected_event and selected_event.branch:
            branch = selected_event.branch
            request.session['active_branch_id'] = branch.id

    if not selected_event:
        messages.info(request, "Please create a club event first to visualize ticket sales.")
        return redirect('restaurant_admin:club_event_create', restaurant_slug=restaurant.slug)

    events_qs = ClubEvent.objects.filter(branch=branch).order_by('-start_datetime')

    # Ticket tiers for this event
    ticket_types = selected_event.ticket_types.all().order_by('display_order', 'id')

    # Overall KPIs
    total_allocated = sum(t.total_allocated for t in ticket_types)
    total_sold = sum(t.quantity_sold for t in ticket_types)
    total_available = sum(t.available_quantity for t in ticket_types)
    total_holds = sum(t.active_holds_count for t in ticket_types)
    total_checked_in = selected_event.total_attendees_checked_in
    total_revenue = selected_event.total_revenue

    context = {
        'restaurant': restaurant,
        'current_branch': branch,
        'rslug': restaurant.slug,
        'events': events_qs,
        'selected_event': selected_event,
        'ticket_types': ticket_types,
        'total_allocated': total_allocated,
        'total_sold': total_sold,
        'total_available': total_available,
        'total_holds': total_holds,
        'total_checked_in': total_checked_in,
        'total_revenue': total_revenue,
        'active_menu': 'club_ticket_grid',
        'active_sub_tab': 'ticket_grid',
    }
    return render(request, 'restaurant_admin/events/ticket_grid.html', context)


@login_required
@user_passes_test(is_admin)
@club_module_required
def club_ticket_grid_api(request, restaurant_slug, event_id):
    """
    Returns live JSON of ticket tiers and sequential slots for the visual ticket matrix.
    Each slot (1..N) represents a unit capacity of that tier, with status and attendee details.
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    branch = get_active_branch(request, restaurant)
    event = get_object_or_404(ClubEvent, pk=event_id, branch__restaurant=restaurant)

    now = timezone.now()
    # Expire old holds first
    EventBooking.objects.filter(
        payment_status=EventBooking.PaymentStatus.PENDING,
        expires_at__lt=now
    ).update(payment_status=EventBooking.PaymentStatus.EXPIRED)

    ticket_types_data = []
    total_allocated = 0
    total_sold = 0
    total_checked_in = 0
    total_holds = 0

    for tt in event.ticket_types.all().order_by('display_order', 'id'):
        allocated = tt.total_allocated
        total_allocated += allocated

        # Confirmed bookings for this tier
        confirmed_bookings = list(
            tt.bookings.filter(payment_status=EventBooking.PaymentStatus.SUCCESS)
            .order_by('created_at')
        )

        # Active pending holds
        pending_bookings = list(
            tt.bookings.filter(
                payment_status=EventBooking.PaymentStatus.PENDING,
                expires_at__gt=now
            ).order_by('created_at')
        )

        slots = []
        slot_index = 1

        # Map confirmed bookings to slots
        for booking in confirmed_bookings:
            for _ in range(booking.quantity):
                if slot_index > allocated:
                    break
                is_checked_in = booking.checked_in
                if is_checked_in:
                    total_checked_in += 1
                total_sold += 1

                slots.append({
                    'slot_number': f"{slot_index:02d}",
                    'raw_number': slot_index,
                    'status': 'CHECKED_IN' if is_checked_in else 'SOLD',
                    'booking_id': booking.id,
                    'booking_reference': booking.booking_reference,
                    'guest_name': booking.guest_name,
                    'guest_phone': booking.guest_phone,
                    'guest_email': booking.guest_email,
                    'unit_price': str(booking.unit_price),
                    'total_amount': str(booking.total_amount),
                    'quantity': booking.quantity,
                    'checked_in': is_checked_in,
                    'checked_in_at': booking.checked_in_at.strftime('%b %d, %I:%M %p') if booking.checked_in_at else None,
                    'booked_at': booking.created_at.strftime('%b %d, %I:%M %p'),
                    'assigned_table': booking.assigned_table.name if booking.assigned_table else None,
                    'payment_status': booking.payment_status,
                })
                slot_index += 1

        # Map pending holds to slots
        for booking in pending_bookings:
            for _ in range(booking.quantity):
                if slot_index > allocated:
                    break
                total_holds += 1
                remaining_secs = int((booking.expires_at - now).total_seconds()) if booking.expires_at else 0
                slots.append({
                    'slot_number': f"{slot_index:02d}",
                    'raw_number': slot_index,
                    'status': 'PENDING_HOLD',
                    'booking_id': booking.id,
                    'booking_reference': booking.booking_reference,
                    'guest_name': booking.guest_name,
                    'guest_phone': booking.guest_phone,
                    'guest_email': booking.guest_email,
                    'unit_price': str(booking.unit_price),
                    'total_amount': str(booking.total_amount),
                    'quantity': booking.quantity,
                    'checked_in': False,
                    'checked_in_at': None,
                    'booked_at': booking.created_at.strftime('%b %d, %I:%M %p'),
                    'hold_expires_in_seconds': max(0, remaining_secs),
                    'payment_status': booking.payment_status,
                })
                slot_index += 1

        # Remaining slots are AVAILABLE
        while slot_index <= allocated:
            slots.append({
                'slot_number': f"{slot_index:02d}",
                'raw_number': slot_index,
                'status': 'AVAILABLE',
                'booking_id': None,
                'booking_reference': None,
                'guest_name': None,
                'unit_price': str(tt.base_price),
            })
            slot_index += 1

        tier_sold_count = len([s for s in slots if s['status'] in ['SOLD', 'CHECKED_IN']])
        tier_checked_in_count = len([s for s in slots if s['status'] == 'CHECKED_IN'])
        tier_available_count = len([s for s in slots if s['status'] == 'AVAILABLE'])
        tier_hold_count = len([s for s in slots if s['status'] == 'PENDING_HOLD'])

        ticket_types_data.append({
            'id': tt.id,
            'name': tt.name,
            'description': tt.description,
            'base_price': str(tt.base_price),
            'total_allocated': allocated,
            'sold_count': tier_sold_count,
            'checked_in_count': tier_checked_in_count,
            'available_count': tier_available_count,
            'hold_count': tier_hold_count,
            'occupancy_rate': round((tier_sold_count / allocated * 100), 1) if allocated > 0 else 0,
            'slots': slots,
        })

    return JsonResponse({
        'event': {
            'id': event.id,
            'title': event.title,
            'start_datetime': event.start_datetime.isoformat(),
            'end_datetime': event.end_datetime.isoformat(),
            'venue_name': event.venue_name,
        },
        'metrics': {
            'total_allocated': total_allocated,
            'total_sold': total_sold,
            'total_available': max(0, total_allocated - total_sold - total_holds),
            'total_checked_in': total_checked_in,
            'total_holds': total_holds,
            'total_revenue': str(event.total_revenue),
            'occupancy_rate': round((total_sold / total_allocated * 100), 1) if total_allocated > 0 else 0,
        },
        'ticket_types': ticket_types_data,
    })


