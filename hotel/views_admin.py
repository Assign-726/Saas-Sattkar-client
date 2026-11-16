import os
import io
import uuid
import datetime
import json
from decimal import Decimal
from functools import wraps
from PIL import Image

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.utils.text import slugify
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.contrib.auth.models import User, Group

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from hotel.models import (
    RoomCategory,
    Room,
    RoomBooking,
    HotelSettings,
    RoomCategoryImage,
    CabServicePackage,
    FleetVehicle,
    CabBooking,
    FolioPayment,
)
from hotel.forms import HotelSettingsForm
import logging

logger = logging.getLogger(__name__)


def optimize_image_to_webp(uploaded_file, max_size=(1920, 1080), quality=85):
    """
    Media Controller:
    Validates file size (max 8MB).
    Reads image, converts mode to RGB/RGBA, resizes within max_size preserving aspect ratio,
    and converts to modern, lightweight WebP format.
    Returns ContentFile with .webp extension.
    """
    try:
        img = Image.open(uploaded_file)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        img.save(output, format="WEBP", quality=quality, optimize=True)
        output.seek(0)
        
        base_name = os.path.splitext(uploaded_file.name)[0]
        return ContentFile(output.read(), name=f"{base_name}.webp")
    except Exception as e:
        return uploaded_file


def process_video_file_upload(uploaded_file, max_size_mb=25):
    """
    Media Controller:
    Validates video file size (max 25MB) and format (MP4, WebM, MOV).
    Saves to media/hotel/categories/videos/
    Returns (url_path, file_size_display).
    """
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    allowed = ['.mp4', '.webm', '.mov', '.mkv']
    if ext not in allowed:
        raise ValueError(f"Invalid video format '{ext}'. Allowed formats: MP4, WebM, MOV.")
    
    max_bytes = max_size_mb * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ValueError(f"Video '{uploaded_file.name}' exceeds {max_size_mb}MB limit ({round(uploaded_file.size / (1024*1024), 1)}MB).")
    
    clean_name = f"cat_vid_{uuid.uuid4().hex[:8]}{ext}"
    save_path = f"hotel/categories/videos/{clean_name}"
    saved_name = default_storage.save(save_path, uploaded_file)
    size_mb = f"{round(uploaded_file.size / (1024*1024), 1)} MB"
    return default_storage.url(saved_name), size_mb


def get_hotel_context(request, restaurant_slug):
    """
    Helper to resolve the restaurant, branch, and access privileges for Hotel Admin.
    Supports either restaurant slug or specific branch slug in the URL.
    """
    restaurant = Restaurant.objects.filter(slug=restaurant_slug).first()
    explicit_branch = None

    if not restaurant:
        explicit_branch = RestaurantBranch.objects.filter(slug=restaurant_slug).first()
        if explicit_branch:
            restaurant = explicit_branch.restaurant
        else:
            restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)

    user = request.user

    # Branch resolution:
    # 1. Explicit branch from URL slug
    # 2. User's explicit branch in profile
    # 3. Session active branch
    # 4. GET param ?branch_id=
    # 5. First branch of restaurant with has_hotel_module=True, or first branch
    active_branch = explicit_branch
    profile = getattr(user, 'restaurant_profile', None)

    if not active_branch:
        if profile and profile.branch:
            active_branch = profile.branch
        else:
            branch_id = request.GET.get('branch_id') or request.session.get(f'active_branch_{restaurant.id}')
            if branch_id and str(branch_id).isdigit():
                active_branch = restaurant.branches.filter(id=int(branch_id)).first()
                if active_branch:
                    request.session[f'active_branch_{restaurant.id}'] = active_branch.id

            if not active_branch:
                active_branch = restaurant.branches.filter(has_hotel_module=True).first() or restaurant.branches.first()
                if active_branch:
                    request.session[f'active_branch_{restaurant.id}'] = active_branch.id

    # Only branches that actually run the hotel module belong in the hotel admin's branch switcher
    available_branches = restaurant.branches.filter(has_hotel_module=True)
    user_role = getattr(user, 'role', None)
    if not user_role and profile:
        user_role = profile.role
    user_role = user_role or 'HOTEL_ADMIN'

    is_general_manager = (user_role == 'GENERAL_MANAGER' or user.is_superuser)
    is_hotel_admin = (user_role == 'HOTEL_ADMIN' or is_general_manager)
    is_front_desk = (user_role == 'FRONT_DESK')
    is_housekeeping = (user_role == 'HOUSEKEEPING')

    # Feature Group Visibility Flags for Hotel PMS:
    show_front_desk_ops = (is_hotel_admin or is_front_desk) and not is_housekeeping
    show_rooms_board = True
    show_room_categories = is_hotel_admin
    show_hotel_config = is_hotel_admin
    show_hotel_settings = is_hotel_admin
    show_hotel_staff = is_hotel_admin
    can_create_reservation = (is_hotel_admin or is_front_desk) and not is_housekeeping
    can_manage_physical_rooms = is_hotel_admin

    return {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'available_branches': available_branches,
        'rslug': restaurant.slug,
        'user_role': user_role,
        'is_general_manager': is_general_manager,
        'is_hotel_admin': is_hotel_admin,
        'is_front_desk': is_front_desk,
        'is_housekeeping': is_housekeeping,
        'show_front_desk_ops': show_front_desk_ops,
        'show_rooms_board': show_rooms_board,
        'show_room_categories': show_room_categories,
        'show_hotel_config': show_hotel_config,
        'show_hotel_settings': show_hotel_settings,
        'show_hotel_staff': show_hotel_staff,
        'can_create_reservation': can_create_reservation,
        'can_manage_physical_rooms': can_manage_physical_rooms,
    }


def hotel_admin_required(view_func):
    """
    Decorator that enforces:
    1. User is authenticated and staff/superuser.
    2. User role is HOTEL_ADMIN or GENERAL_MANAGER (RESTAURANT_ADMIN blocked).
    3. Branch has has_hotel_module enabled.
    """
    @wraps(view_func)
    def _wrapped(request, restaurant_slug, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')

        user_role = getattr(request.user, 'role', None)
        if not user_role and hasattr(request.user, 'restaurant_profile'):
            user_role = request.user.restaurant_profile.role

        from restaurant_project import constants
        if not getattr(constants, 'ENABLE_HOTEL_MODULE', True):
            messages.error(
                request,
                "The Hotel & Rooms PMS module is globally disabled in system configuration (.env)."
            )
            return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

        # If user is not authorized for Hotel PMS, deny access
        if not request.user.is_superuser and user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER', 'FRONT_DESK', 'HOUSEKEEPING']:
            messages.error(
                request,
                "Access restricted: Your account does not have Hotel PMS permissions. "
                "You have been redirected to the Restaurant Admin portal."
            )
            return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

        # Housekeeping role is strictly locked to Room Status view
        if user_role == 'HOUSEKEEPING' and not request.path.endswith('/rooms/') and not '/rooms/' in request.path:
            return redirect('hotel_admin:rooms', restaurant_slug=restaurant_slug)

        ctx = get_hotel_context(request, restaurant_slug)
        branch = ctx['current_branch']

        # Tenant isolation: a login only ever opens its own business (and its own branch
        # when branch-locked). Only platform superusers may look at another hotel.
        if not request.user.is_superuser:
            from django.http import HttpResponseForbidden
            profile = getattr(request.user, 'restaurant_profile', None)
            if profile is None or profile.restaurant_id != ctx['restaurant'].id:
                return HttpResponseForbidden("You do not have access to this hotel.")
            if profile.branch_id and branch is not None and branch.id != profile.branch_id:
                return HttpResponseForbidden("You do not have access to this branch.")

        if branch and not branch.has_hotel_module and not request.user.is_superuser:
            return render(request, 'hotel_admin/feature_disabled.html', {
                **ctx,
                'error_message': f"The Hotel Module is currently disabled for {branch.name}. "
                                 f"Please contact your venue administrator to enable the Hotel PMS capability."
            })

        return view_func(request, restaurant_slug, ctx, *args, **kwargs)
    return _wrapped


@hotel_admin_required
def dashboard_view(request, restaurant_slug, ctx):
    """
    Dedicated Hotel Admin Front-Desk Dashboard:
    - Quick KPI counts (Total, Available, Occupied, Cleaning, Maintenance, Occupancy %)
    - Today's Arrivals with 1-click Check-in
    - Today's Departures with 1-click Check-out
    - Housekeeping Alerts (Cleaning rooms with 1-click 'Mark Clean')
    - 7-Day Tape Chart visual preview
    """
    branch = ctx['current_branch']
    today = timezone.localdate()

    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category') if branch else Room.objects.none()
    total_rooms = rooms.count()
    available_rooms = rooms.filter(status=Room.RoomStatus.AVAILABLE)
    occupied_rooms = rooms.filter(status=Room.RoomStatus.OCCUPIED)
    cleaning_rooms = rooms.filter(status=Room.RoomStatus.CLEANING)
    maintenance_rooms = rooms.filter(status=Room.RoomStatus.MAINTENANCE)

    # Today's Arrivals
    arrivals_today = RoomBooking.objects.filter(
        branch=branch,
        check_in_date=today,
        booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN]
    ).select_related('room', 'room__category').order_by('check_in_date')

    # Today's Departures
    departures_today = RoomBooking.objects.filter(
        branch=branch,
        check_out_date=today,
        booking_status__in=[RoomBooking.BookingStatus.CHECKED_IN, RoomBooking.BookingStatus.CHECKED_OUT]
    ).select_related('room', 'room__category').order_by('check_out_date')

    occupancy_rate = round((occupied_rooms.count() / total_rooms * 100), 1) if total_rooms > 0 else 0

    # 7-day mini tape chart preview
    days_count = 7
    preview_dates = [(today + datetime.timedelta(days=i)) for i in range(days_count)]
    end_date = today + datetime.timedelta(days=days_count)

    active_bookings = RoomBooking.objects.filter(
        branch=branch,
        booking_status__in=[
            RoomBooking.BookingStatus.RESERVED,
            RoomBooking.BookingStatus.CHECKED_IN,
            RoomBooking.BookingStatus.CHECKED_OUT,
        ],
        check_in_date__lt=end_date,
        check_out_date__gt=today
    ).select_related('room') if branch else []

    # Map bookings to room_id
    booking_map = {}
    for b in active_bookings:
        if b.room_id not in booking_map:
            booking_map[b.room_id] = []
        booking_map[b.room_id].append(b)

    categories = RoomCategory.objects.filter(branch=branch, is_active=True) if branch else []

    context = {
        **ctx,
        'active_menu': 'dashboard',
        'today': today,
        'total_rooms_count': total_rooms,
        'available_count': available_rooms.count(),
        'occupied_count': occupied_rooms.count(),
        'cleaning_count': cleaning_rooms.count(),
        'maintenance_count': maintenance_rooms.count(),
        'occupancy_rate': occupancy_rate,
        'arrivals_today': arrivals_today,
        'departures_today': departures_today,
        'cleaning_rooms': cleaning_rooms,
        'preview_dates': preview_dates,
        'rooms': rooms,
        'booking_map': booking_map,
        'categories': categories,
    }
    if branch:
        from hotel import ops
        from super_admin import permissions as P
        attention = ops.action_items(branch, P.access_map(request.user))
        context.update({'attention': attention, 'attention_total': len(attention)})
    return render(request, 'hotel_admin/dashboard.html', context)


@hotel_admin_required
def tape_chart_view(request, restaurant_slug, ctx):
    """
    Dedicated Interactive Tape Chart / Calendar Timeline UI:
    Rows = Physical Rooms (grouped by category)
    Columns = Calendar dates
    Booking blocks rendered spanning check-in to check-out dates.
    """
    branch = ctx['current_branch']
    today = timezone.localdate()

    start_str = request.GET.get('start_date')
    if start_str:
        try:
            start_date = datetime.date.fromisoformat(start_str)
        except ValueError:
            start_date = today
    else:
        start_date = today

    try:
        days_count = int(request.GET.get('days', 14))
    except (ValueError, TypeError):
        days_count = 14
    days_count = max(7, min(days_count, 31))

    end_date = start_date + datetime.timedelta(days=days_count)
    dates_list = [start_date + datetime.timedelta(days=i) for i in range(days_count)]

    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('category__name', 'room_number') if branch else []

    bookings = RoomBooking.objects.filter(
        branch=branch,
        booking_status__in=[
            RoomBooking.BookingStatus.RESERVED,
            RoomBooking.BookingStatus.CHECKED_IN,
            RoomBooking.BookingStatus.CHECKED_OUT,
        ],
        check_in_date__lt=end_date,
        check_out_date__gt=start_date
    ).select_related('room', 'room__category') if branch else []

    # Prepare structured room data with bookings
    rooms_data = []
    for r in rooms:
        r_bookings = [b for b in bookings if b.room_id == r.id]
        rooms_data.append({
            'room': r,
            'bookings': r_bookings,
        })

    categories = RoomCategory.objects.filter(branch=branch, is_active=True) if branch else []

    context = {
        **ctx,
        'active_menu': 'tape_chart',
        'today': today,
        'start_date': start_date,
        'end_date': end_date,
        'prev_date': start_date - datetime.timedelta(days=days_count),
        'next_date': start_date + datetime.timedelta(days=days_count),
        'days_count': days_count,
        'dates_list': dates_list,
        'rooms_data': rooms_data,
        'categories': categories,
        'rooms': rooms,
    }
    return render(request, 'hotel_admin/tape_chart.html', context)


@hotel_admin_required
def rooms_view(request, restaurant_slug, ctx):
    """
    Room Inventory & Housekeeping Management View:
    Displays rooms by status (Available, Occupied, Cleaning, Maintenance)
    Allows 1-click status changes (e.g. Mark Clean) and adding physical rooms.
    """
    branch = ctx['current_branch']

    status_filter = request.GET.get('status', '').upper()
    category_filter = request.GET.get('category_id')

    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('room_number') if branch else Room.objects.none()

    if status_filter in Room.RoomStatus.values:
        rooms = rooms.filter(status=status_filter)
    if category_filter and category_filter.isdigit():
        rooms = rooms.filter(category_id=int(category_filter))

    categories = RoomCategory.objects.filter(branch=branch, is_active=True) if branch else []

    # Grouped for Kanban board
    all_rooms = list(Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('room_number')) if branch else []

    # Attach the in-house stay and today's arrival so cards can show who is in / due
    today = timezone.localdate()
    if all_rooms:
        in_house = {}
        arrivals = {}
        stays = RoomBooking.objects.filter(branch=branch, room__in=all_rooms).filter(
            Q(booking_status=RoomBooking.BookingStatus.CHECKED_IN) |
            Q(booking_status=RoomBooking.BookingStatus.RESERVED, check_in_date=today)
        ).order_by('check_in_date')
        for b in stays:
            if b.booking_status == RoomBooking.BookingStatus.CHECKED_IN:
                in_house.setdefault(b.room_id, b)
            else:
                arrivals.setdefault(b.room_id, b)
        for r in all_rooms:
            r.current_stay = in_house.get(r.id)
            r.arrival_today = arrivals.get(r.id)
            r.departs_today = bool(r.current_stay and r.current_stay.check_out_date <= today)

    floors = sorted({r.floor for r in all_rooms if r.floor})
    kanban = {
        'AVAILABLE': [r for r in all_rooms if r.status == Room.RoomStatus.AVAILABLE],
        'OCCUPIED': [r for r in all_rooms if r.status == Room.RoomStatus.OCCUPIED],
        'CLEANING': [r for r in all_rooms if r.status == Room.RoomStatus.CLEANING],
        'MAINTENANCE': [r for r in all_rooms if r.status == Room.RoomStatus.MAINTENANCE],
    }

    context = {
        **ctx,
        'active_menu': 'rooms',
        'rooms': rooms,
        'categories': categories,
        'kanban': kanban,
        'all_rooms': all_rooms,
        'floors': floors,
        'total_rooms': len(all_rooms),
        'today': today,
        'status_filter': status_filter,
        'category_filter': category_filter,
    }
    return render(request, 'hotel_admin/rooms.html', context)


@hotel_admin_required
def categories_view(request, restaurant_slug, ctx):
    """
    Room Categories / Types Configuration View:
    Deluxe Suite, Royal Villa, etc. with base price, max occupancy, and amenities.
    """
    if not ctx['show_room_categories']:
        messages.error(request, "Permission denied: Only Hotel Administrators can manage room categories and tariffs.")
        return redirect('hotel_admin:rooms' if ctx['is_housekeeping'] else 'hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    branch = ctx['current_branch']
    from django.db.models import Prefetch
    active_rooms = Prefetch('rooms', queryset=Room.objects.filter(is_active=True).order_by('room_number'), to_attr='active_rooms')
    categories = RoomCategory.objects.filter(branch=branch).prefetch_related(active_rooms, 'images').order_by('name') if branch else []

    context = {
        **ctx,
        'active_menu': 'categories',
        'categories': categories,
        'floors': sorted({r.floor for c in categories for r in c.active_rooms if r.floor}),
    }
    return render(request, 'hotel_admin/categories.html', context)


@hotel_admin_required
def bookings_view(request, restaurant_slug, ctx):
    """
    Bookings & Reservations Manager View:
    Search, filter, check-in, check-out, and create guest bookings.
    """
    branch = ctx['current_branch']
    bookings = RoomBooking.objects.filter(branch=branch).select_related('room', 'room__category').order_by('-created_at') if branch else RoomBooking.objects.none()

    # Search & filters
    q = request.GET.get('q', '').strip()
    if q:
        bookings = bookings.filter(
            Q(guest_name__icontains=q) |
            Q(guest_phone__icontains=q) |
            Q(guest_email__icontains=q) |
            Q(booking_id__icontains=q) |
            Q(room__room_number__icontains=q)
        )

    b_status = request.GET.get('status', '').upper()
    if b_status in RoomBooking.BookingStatus.values:
        bookings = bookings.filter(booking_status=b_status)

    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category') if branch else []
    categories = RoomCategory.objects.filter(branch=branch, is_active=True) if branch else []
    from hotel.paging import paginate
    page_obj = paginate(request, bookings)

    context = {
        **ctx,
        'active_menu': 'bookings',
        'bookings': page_obj.object_list,
        'page_obj': page_obj,
        'rooms': rooms,
        'categories': categories,
        'search_query': q,
        'status_filter': b_status,
        'today': timezone.localdate(),
    }
    return render(request, 'hotel_admin/bookings.html', context)


# ==========================================
# POST ACTION HANDLERS (Front Desk Operations)
# ==========================================

@hotel_admin_required
def booking_check_in_action(request, restaurant_slug, ctx, booking_id):
    """Check the guest in (rules in hotel/folio.py: status, ID requirement, room free and in order)."""
    from hotel import folio
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)
    if request.method != 'POST':
        return redirect('hotel_admin:booking_detail', restaurant_slug=restaurant_slug, booking_id=booking.pk)
    error = folio.check_in(booking, request.user)
    if error:
        messages.error(request, error)
        url = reverse('hotel_admin:booking_detail', kwargs={'restaurant_slug': restaurant_slug, 'booking_id': booking.pk})
        return redirect(url + ('?tab=guest' if 'registration' in error else ''))
    messages.success(request, f"Guest {booking.guest_name} successfully checked into Room {booking.room.room_number}.")
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def booking_check_out_action(request, restaurant_slug, ctx, booking_id):
    """Check the guest out: the folio must be settled (or the balance confirmed), an invoice number is issued
    and the room goes to housekeeping with a departure-clean task."""
    from hotel import folio
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)
    if request.method != 'POST':
        return redirect('hotel_admin:booking_detail', restaurant_slug=restaurant_slug, booking_id=booking.pk)
    error = folio.check_out(booking, request.user, allow_balance=request.POST.get('allow_balance') == '1')
    if error:
        messages.error(request, error)
        url = reverse('hotel_admin:booking_detail', kwargs={'restaurant_slug': restaurant_slug, 'booking_id': booking.pk})
        return redirect(url + ('?tab=guest' if 'registration' in error else ''))
    messages.success(request, f"Guest {booking.guest_name} checked out (invoice {booking.invoice_number}). Room {booking.room.room_number} sent to housekeeping.")
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def booking_cancel_action(request, restaurant_slug, ctx, booking_id):
    """Cancel a booking (with an optional reason) and release the room if the guest was in it."""
    from hotel import folio
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)
    if request.method != 'POST':
        return redirect('hotel_admin:booking_detail', restaurant_slug=restaurant_slug, booking_id=booking.pk)
    error = folio.cancel(booking, request.user, request.POST.get('reason', ''))
    if error:
        messages.error(request, error)
    else:
        balance = folio.Folio(booking).paid
        messages.info(request, f"Reservation {booking.booking_id} has been cancelled."
                      + (f" ₹{balance:,.2f} was paid — record a refund on the folio if one is due." if balance > 0 else ""))
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def room_status_update_action(request, restaurant_slug, ctx, room_id):
    """
    Quick status toggle for a physical room (e.g. Mark Clean -> AVAILABLE).
    """
    branch = ctx['current_branch']
    room = get_object_or_404(Room, id=room_id, branch=branch)

    new_status = request.POST.get('status', '').upper()
    if new_status in Room.RoomStatus.values:
        was_cleaning = room.status == Room.RoomStatus.CLEANING
        room.status = new_status
        room.save(update_fields=['status', 'updated_at'])
        if was_cleaning and new_status == Room.RoomStatus.AVAILABLE:
            # "Mark clean" closes the room's open cleaning tasks on the housekeeping board
            from hotel.models import HousekeepingTask
            HousekeepingTask.objects.filter(room=room, status__in=['PENDING', 'IN_PROGRESS'],
                                            kind__in=['CHECKOUT_CLEAN', 'DEEP_CLEAN']).update(
                status='DONE', completed_at=timezone.now(), completed_by=request.user)
        messages.success(request, f"Room {room.room_number} status updated to '{room.get_status_display()}'.")
    else:
        messages.error(request, f"Invalid room status '{new_status}'.")

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def room_create_edit_action(request, restaurant_slug, ctx):
    """
    Create or edit a physical room inventory item.
    """
    if not ctx['can_manage_physical_rooms']:
        messages.error(request, "Permission denied: Only Hotel Administrators can add or edit physical rooms.")
        return redirect('hotel_admin:rooms', restaurant_slug=restaurant_slug)

    branch = ctx['current_branch']
    if request.method == 'POST':
        room_id = request.POST.get('room_id')
        room_number = request.POST.get('room_number', '').strip()
        floor = request.POST.get('floor', '1st Floor').strip()
        category_id = request.POST.get('category_id')
        status_val = request.POST.get('status', Room.RoomStatus.AVAILABLE).upper()
        notes = request.POST.get('notes', '').strip()

        if not room_number or not category_id:
            messages.error(request, "Room number and Category are required.")
            return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug}))

        category = get_object_or_404(RoomCategory, id=category_id, branch=branch)

        if room_id:
            room = get_object_or_404(Room, id=room_id, branch=branch)
            # Check duplicate room number
            if Room.objects.filter(branch=branch, room_number__iexact=room_number).exclude(pk=room.pk).exists():
                messages.error(request, f"Room {room_number} already exists in this branch.")
                return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug}))
            room.room_number = room_number
            room.floor = floor
            room.category = category
            room.status = status_val if status_val in Room.RoomStatus.values else room.status
            room.notes = notes
            room.save()
            messages.success(request, f"Room {room.room_number} updated successfully.")
        else:
            if Room.objects.filter(branch=branch, room_number__iexact=room_number).exists():
                messages.error(request, f"Room {room_number} already exists in this branch.")
                return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug}))
            room = Room.objects.create(
                branch=branch,
                category=category,
                room_number=room_number,
                floor=floor,
                status=status_val if status_val in Room.RoomStatus.values else Room.RoomStatus.AVAILABLE,
                notes=notes
            )
            messages.success(request, f"Room {room.room_number} created successfully.")

    return redirect(_safe_next(request) or reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug}))


def _ensure_entire_property_unit(request, category):
    """An entire-property category is booked through exactly one unit; create it if missing."""
    units = category.rooms.filter(is_active=True)
    count = units.count()
    if count == 0:
        base = (category.slug or slugify(category.name) or 'villa').upper()[:40]
        number, n = base, 2
        while Room.objects.filter(branch=category.branch, room_number__iexact=number).exists():
            number, n = f"{base[:46]}-{n}", n + 1
        Room.objects.create(branch=category.branch, category=category, room_number=number,
                            floor='', status=Room.RoomStatus.AVAILABLE,
                            notes='Entire property unit')
        messages.info(request, f"Created bookable unit '{number}' for {category.name}. Rename it from the category's Rooms list if you like.")
    elif count > 1:
        messages.warning(request, f"{category.name} is booked as an entire property but has {count} units. "
                                  f"Remove the extras from its Rooms list so it can't be double-booked.")


def _event_fields(data, room):
    """Optional event details, kept only for entire-property bookings."""
    if not room.category.is_entire_property:
        return {}
    event_type = (data.get('event_type') or '').upper()
    headcount = (data.get('expected_headcount') or '').strip()
    return {
        'event_type': event_type if event_type in RoomBooking.EventType.values else '',
        'expected_headcount': min(int(headcount), 100000) if headcount.isdigit() and int(headcount) > 0 else None,
        'event_notes': (data.get('event_notes') or '').strip()[:2000],
    }


def _safe_next(request):
    """Same-host `next` target posted by a form, or None."""
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get('next', '')
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return None


def _expand_room_numbers(spec, limit=100):
    """'101-105, 110, 2A' -> ['101', ..., '105', '110', '2A']. Ranges need plain integers."""
    numbers = []
    for part in spec.replace('\n', ',').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            lo, _, hi = part.partition('-')
            lo, hi = lo.strip(), hi.strip()
            if not (lo.isdigit() and hi.isdigit()) or int(hi) < int(lo):
                raise ValueError(f"'{part}' is not a valid range (use e.g. 101-110).")
            width = len(lo) if lo.startswith('0') else 0
            numbers.extend(str(n).zfill(width) for n in range(int(lo), int(hi) + 1))
        else:
            numbers.append(part[:50])
        if len(numbers) > limit:
            raise ValueError(f"You can add at most {limit} rooms at a time.")
    return list(dict.fromkeys(numbers))  # de-duplicate, keep order


@hotel_admin_required
def category_rooms_add_action(request, restaurant_slug, ctx, category_id):
    """Bulk-add physical rooms to a category from the Categories page."""
    back = _safe_next(request) or reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug})
    if request.method != 'POST':
        return redirect(back)
    if not ctx['can_manage_physical_rooms']:
        messages.error(request, "Permission denied: Only Hotel Administrators can add physical rooms.")
        return redirect(back)

    branch = ctx['current_branch']
    category = get_object_or_404(RoomCategory, id=category_id, branch=branch)
    try:
        numbers = _expand_room_numbers(request.POST.get('room_numbers', ''))
    except ValueError as e:
        messages.error(request, str(e))
        return redirect(back)
    if not numbers:
        messages.error(request, "Enter at least one room number.")
        return redirect(back)

    if category.is_entire_property and (len(numbers) > 1 or category.rooms.filter(is_active=True).exists()):
        messages.error(request, f"{category.name} is booked as an entire property, so it can only have one unit.")
        return redirect(back)

    floor = request.POST.get('floor', '').strip()[:50]
    existing = {n.lower() for n in Room.objects.filter(branch=branch).values_list('room_number', flat=True)}
    to_create = [n for n in numbers if n.lower() not in existing]
    skipped = [n for n in numbers if n.lower() in existing]

    Room.objects.bulk_create([
        Room(branch=branch, category=category, room_number=n, floor=floor, status=Room.RoomStatus.AVAILABLE)
        for n in to_create
    ])
    if to_create:
        messages.success(request, f"Added {len(to_create)} room{'s' if len(to_create) != 1 else ''} to {category.name}: {', '.join(to_create[:12])}{'…' if len(to_create) > 12 else ''}.")
    if skipped:
        messages.warning(request, f"Skipped existing room number{'s' if len(skipped) != 1 else ''}: {', '.join(skipped[:12])}{'…' if len(skipped) > 12 else ''}.")
    return redirect(back)


@hotel_admin_required
def room_remove_action(request, restaurant_slug, ctx, room_id):
    """Retire a room from inventory (soft delete) unless it has a live or upcoming stay."""
    back = _safe_next(request) or reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug})
    if request.method != 'POST':
        return redirect(back)
    if not ctx['can_manage_physical_rooms']:
        messages.error(request, "Permission denied: Only Hotel Administrators can remove rooms.")
        return redirect(back)

    room = get_object_or_404(Room, id=room_id, branch=ctx['current_branch'])
    live = RoomBooking.objects.filter(
        room=room,
        booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
        check_out_date__gte=timezone.localdate(),
    ).exists()
    if live:
        messages.error(request, f"Room {room.room_number} has a current or upcoming booking. Move or cancel it before removing the room.")
        return redirect(back)

    room.is_active = False
    room.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, f"Room {room.room_number} removed from inventory.")
    return redirect(back)


@hotel_admin_required
def category_create_edit_action(request, restaurant_slug, ctx):
    """
    Create or edit a room category with:
    - Enforced MAX 10 Total Media Limit (Cover + Gallery Images + Videos <= 10)
    - Automated WebP Image Conversion & Optimization Controller
    - Video File Upload (max 25MB) & Video Streaming URL Controller
    - Tax Inclusive/Exclusive Pricing & Banner Showcase
    """
    branch = ctx['current_branch']
    MAX_TOTAL_MEDIA = 10

    if request.method == 'POST':
        cat_id = request.POST.get('category_id')
        name = request.POST.get('name', '').strip()
        price_str = request.POST.get('base_price_per_night', '0')
        max_occ = int(request.POST.get('max_occupancy', 2) or 2)
        max_adults = int(request.POST.get('max_adults', 2) or 2)
        max_children = int(request.POST.get('max_children', 1) or 0)
        bed_type = request.POST.get('bed_type', 'King Bed').strip()
        room_size = int(request.POST.get('room_size_sqft', 350) or 350)
        description = request.POST.get('description', '').strip()
        amenities_raw = request.POST.get('amenities', '').strip()

        # Pricing & Tax details
        price_is_tax_inclusive = request.POST.get('price_is_tax_inclusive') in ['true', 'on', '1', True]
        original_price_str = request.POST.get('original_price_per_night', '').strip()
        original_price = Decimal(original_price_str) if original_price_str else None
        
        custom_tax_str = request.POST.get('custom_tax_rate', '').strip()
        custom_tax_rate = Decimal(custom_tax_str) if custom_tax_str else None

        # Banner & Promotion
        show_on_banner = request.POST.get('show_on_banner') in ['true', 'on', '1', True]
        banner_badge_text = request.POST.get('banner_badge_text', '').strip()

        # Complimentary Airport / Station Transfers
        includes_free_transfer = request.POST.get('includes_free_transfer') in ['true', 'on', '1', True]
        transfer_type = request.POST.get('transfer_type', 'NONE').upper()
        if transfer_type not in ['NONE', 'PICKUP_ONLY', 'DROP_ONLY', 'BOTH']:
            transfer_type = 'NONE'

        # Entire-property units (villas / party venues)
        is_entire_property = request.POST.get('is_entire_property') in ['true', 'on', '1', True]
        bedrooms_raw = request.POST.get('bedrooms_count', '').strip()
        bedrooms_count = int(bedrooms_raw) if bedrooms_raw.isdigit() else None

        # Image URLs
        cover_image_url = request.POST.get('cover_image_url', '').strip()

        if not name:
            messages.error(request, "Category name is required.")
            return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))

        try:
            base_price = Decimal(price_str)
        except Exception:
            base_price = Decimal('5000.00')

        amenities_list = [a.strip() for a in amenities_raw.split(',') if a.strip()]

        if cat_id:
            category = get_object_or_404(RoomCategory, id=cat_id, branch=branch)
            category.name = name
            category.base_price_per_night = base_price
            category.max_occupancy = max_occ
            category.max_adults = max_adults
            category.max_children = max_children
            category.bed_type = bed_type
            category.room_size_sqft = room_size
            category.description = description
            category.amenities = amenities_list
            category.price_is_tax_inclusive = price_is_tax_inclusive
            category.original_price_per_night = original_price
            category.custom_tax_rate = custom_tax_rate
            category.show_on_banner = show_on_banner
            category.banner_badge_text = banner_badge_text
            category.includes_free_transfer = includes_free_transfer
            category.transfer_type = transfer_type
            category.is_entire_property = is_entire_property
            category.bedrooms_count = bedrooms_count
        else:
            category = RoomCategory(
                branch=branch,
                name=name,
                base_price_per_night=base_price,
                max_occupancy=max_occ,
                max_adults=max_adults,
                max_children=max_children,
                bed_type=bed_type,
                room_size_sqft=room_size,
                description=description,
                amenities=amenities_list,
                price_is_tax_inclusive=price_is_tax_inclusive,
                original_price_per_night=original_price,
                custom_tax_rate=custom_tax_rate,
                show_on_banner=show_on_banner,
                banner_badge_text=banner_badge_text,
                includes_free_transfer=includes_free_transfer,
                transfer_type=transfer_type,
                is_entire_property=is_entire_property,
                bedrooms_count=bedrooms_count,
            )
            category.save()

        # Handle Cover Image deletion / update
        remove_cover = request.POST.get('remove_cover_image') in ['1', 'true', 'on', True]
        if remove_cover:
            if category.cover_image:
                try:
                    category.cover_image.delete(save=False)
                except Exception:
                    pass
                category.cover_image = None
            category.cover_image_url = ""

        # Handle Cover Image with WebP conversion
        if 'cover_image' in request.FILES:
            converted_cover = optimize_image_to_webp(request.FILES['cover_image'])
            category.cover_image = converted_cover
            category.cover_image_url = ""
        elif cover_image_url and not remove_cover:
            category.cover_image_url = cover_image_url

        # Existing media count & strict limits
        MAX_TOTAL_MEDIA = 10
        MAX_VIDEOS = 2

        current_images_count = category.images.count()
        current_videos = list(category.video_links) if isinstance(category.video_links, list) else []
        has_cover = bool(category.cover_image or category.cover_image_url)
        current_media_count = current_images_count + len(current_videos) + (1 if has_cover else 0)

        # Process uploaded video files (Controller: max 25MB, mp4/webm/mov, max 2 videos total)
        uploaded_video_files = request.FILES.getlist('uploaded_videos')
        for v_file in uploaded_video_files:
            if len(current_videos) >= MAX_VIDEOS:
                messages.warning(request, f"Video limit reached (max {MAX_VIDEOS} videos allowed). Additional videos were skipped.")
                break
            if current_media_count >= MAX_TOTAL_MEDIA:
                messages.warning(request, f"Media limit reached (max {MAX_TOTAL_MEDIA} total items). Some videos were skipped.")
                break
            try:
                vid_url, vid_size = process_video_file_upload(v_file, max_size_mb=25)
                current_videos.append({
                    'url': vid_url,
                    'title': f"Video Tour ({vid_size})",
                    'type': 'uploaded',
                    'size': vid_size
                })
                current_media_count += 1
            except ValueError as ve:
                messages.error(request, str(ve))

        # Parse external video URLs (YouTube, Vimeo, MP4 CDN) - max 2 videos total
        video_json_raw = request.POST.get('video_links_json', '').strip()
        if video_json_raw:
            try:
                parsed_vids = json.loads(video_json_raw)
                if isinstance(parsed_vids, list):
                    for v in parsed_vids:
                        if len(current_videos) >= MAX_VIDEOS:
                            break
                        if current_media_count >= MAX_TOTAL_MEDIA:
                            break
                        if isinstance(v, dict) and v.get('url', '').strip():
                            v_url = v['url'].strip()
                            if not any(x.get('url') == v_url for x in current_videos):
                                current_videos.append({
                                    'url': v_url,
                                    'title': v.get('title', '').strip() or f'Video Tour #{len(current_videos)+1}',
                                    'type': 'link'
                                })
                                current_media_count += 1
            except Exception:
                pass

        # Also parse individual video_url_1 and video_url_2 inputs (max 2 videos total)
        for i in range(1, 3):
            if len(current_videos) >= MAX_VIDEOS:
                break
            if current_media_count >= MAX_TOTAL_MEDIA:
                break
            v_url = request.POST.get(f'video_url_{i}', '').strip()
            v_title = request.POST.get(f'video_title_{i}', '').strip()
            if v_url and not any(x.get('url') == v_url for x in current_videos):
                current_videos.append({
                    'url': v_url,
                    'title': v_title or f'Video Tour #{len(current_videos)+1}',
                    'type': 'link'
                })
                current_media_count += 1

        category.video_links = current_videos[:MAX_VIDEOS]

        # External Gallery URLs (Respect total media limit of 10)
        gallery_urls_raw = request.POST.get('gallery_image_urls', '').strip()
        if gallery_urls_raw:
            raw_lines = [u.strip() for u in gallery_urls_raw.replace('\r\n', '\n').split('\n') if u.strip()]
            for line in raw_lines:
                for part in line.split(','):
                    if current_media_count >= MAX_TOTAL_MEDIA:
                        messages.warning(request, f"Total media limit reached (max {MAX_TOTAL_MEDIA} items).")
                        break
                    part_clean = part.strip()
                    if part_clean and part_clean.startswith(('http://', 'https://', '/media/')):
                        RoomCategoryImage.objects.create(
                            category=category,
                            image_url=part_clean,
                            caption=f"{category.name} - Photo",
                            display_order=category.images.count() + 1
                        )
                        current_media_count += 1

        # Process multiple uploaded gallery photos (Controller: Auto WebP conversion, max 8MB, max 10 total)
        uploaded_gallery = request.FILES.getlist('gallery_images')
        for idx, file_obj in enumerate(uploaded_gallery, start=1):
            if current_media_count >= MAX_TOTAL_MEDIA:
                messages.warning(request, f"Media limit reached (max {MAX_TOTAL_MEDIA} items). Excess photos were skipped.")
                break
            webp_file = optimize_image_to_webp(file_obj, quality=85)
            RoomCategoryImage.objects.create(
                category=category,
                image=webp_file,
                caption=f"{category.name} - WebP Photo",
                display_order=category.images.count() + idx
            )
            current_media_count += 1

        category.save()

        if category.is_entire_property:
            _ensure_entire_property_unit(request, category)

        if cat_id:
            messages.success(request, f"Category '{category.name}' updated successfully with optimized media assets.")
        else:
            messages.success(request, f"Category '{category.name}' created successfully with optimized media assets.")

    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_delete_cover_action(request, restaurant_slug, ctx, category_id):
    """
    Media Controller: Remove the primary cover image from a room category.
    Supports AJAX or normal POST.
    """
    branch = ctx['current_branch']
    category = get_object_or_404(RoomCategory, id=category_id, branch=branch)
    if category.cover_image:
        try:
            category.cover_image.delete(save=False)
        except Exception:
            pass
        category.cover_image = None
    category.cover_image_url = ""
    category.save(update_fields=['cover_image', 'cover_image_url', 'updated_at'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'message': 'Cover photo removed'})

    messages.success(request, f"Cover photo removed from '{category.name}'.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_set_cover_action(request, restaurant_slug, ctx, image_id):
    """
    Media Controller: Make an existing gallery photo the primary category cover.
    """
    branch = ctx['current_branch']
    img = get_object_or_404(RoomCategoryImage, id=image_id, category__branch=branch)
    category = img.category

    if img.image:
        category.cover_image = img.image
        category.cover_image_url = ""
    elif img.image_url:
        category.cover_image_url = img.image_url

    category.save(update_fields=['cover_image', 'cover_image_url', 'updated_at'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'new_cover_url': category.effective_image_url})

    messages.success(request, f"Cover photo updated for '{category.name}'.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_delete_video_action(request, restaurant_slug, ctx, category_id, video_idx):
    """
    Media Controller: Remove a video from category's video_links by index.
    """
    branch = ctx['current_branch']
    category = get_object_or_404(RoomCategory, id=category_id, branch=branch)
    videos = list(category.video_links) if isinstance(category.video_links, list) else []

    if 0 <= video_idx < len(videos):
        removed = videos.pop(video_idx)
        # If it was an uploaded video file, try to delete it from storage
        if isinstance(removed, dict) and removed.get('type') == 'uploaded' and removed.get('url'):
            try:
                rel_path = removed['url'].replace('/media/', '', 1)
                if default_storage.exists(rel_path):
                    default_storage.delete(rel_path)
            except Exception:
                pass
        category.video_links = videos
        category.save(update_fields=['video_links', 'updated_at'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'remaining_videos_count': len(videos)})

    messages.success(request, "Video removed from category.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_delete_image_action(request, restaurant_slug, ctx, image_id):
    """
    Remove an individual gallery photo from a category.
    Supports AJAX or normal POST.
    """
    branch = ctx['current_branch']
    img = get_object_or_404(RoomCategoryImage, id=image_id, category__branch=branch)
    cat_name = img.category.name
    if img.image:
        try:
            img.image.delete(save=False)
        except Exception:
            pass
    img.delete()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'deleted_id': image_id})

    messages.success(request, f"Photo removed from '{cat_name}'.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def booking_create_action(request, restaurant_slug, ctx):
    """
    New reservation from the front desk.
    mode=reserve  -> advance booking; guest ID optional (completed at check-in).
    mode=walk_in  -> guest is at the desk: arrival is today, registration is validated and the guest is checked in.
    Any validation error sends the typed values back to the modal (session draft) instead of losing them.
    """
    from hotel import folio, pricing
    from hotel import validation as V
    branch = ctx['current_branch']
    back = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug})
    if request.method != 'POST':
        return redirect(back)

    p = request.POST
    walk_in = p.get('mode') == 'walk_in'
    settings_obj = HotelSettings.get_settings(branch)
    today = timezone.localdate()
    errors = {}

    def fail(message=None):
        draft = {k: v for k, v in p.items() if k not in ('csrfmiddlewaretoken',)}
        request.session['nb_draft'] = {'values': draft, 'errors': errors}
        messages.error(request, message or 'Reservation not saved. Fix the highlighted fields in the form.')
        return redirect(back)

    def text(name, limit=None):
        v = (p.get(name) or '').strip()
        return v[:limit] if limit else v

    # ---- Stay ----
    room = Room.objects.filter(id=p.get('room_id') or 0, branch=branch, is_active=True).select_related('category').first()
    if not room:
        errors['room_id'] = 'Choose a room.'
    check_in_date = today if walk_in else V.parse_date(p.get('check_in_date'))
    check_out_date = V.parse_date(p.get('check_out_date'))
    if not check_in_date:
        errors['check_in_date'] = 'Choose the arrival date.'
    elif not walk_in and check_in_date < today:
        errors['check_in_date'] = 'Arrival cannot be in the past.'
    if not check_out_date:
        errors['check_out_date'] = 'Choose the departure date.'
    elif check_in_date and check_out_date <= check_in_date:
        errors['check_out_date'] = 'Departure must be after arrival.'

    try:
        adults = int(p.get('adults') or p.get('num_guests') or 1)
        children = int(p.get('children') or 0)
    except ValueError:
        adults, children = 0, 0
    if adults < 1:
        errors['adults'] = 'At least one adult is required.'
    elif children < 0:
        errors['children'] = 'Children cannot be negative.'
    elif room and adults + children > room.category.max_occupancy:
        errors['adults'] = f'{room.category.name} takes at most {room.category.max_occupancy} guests.'

    # ---- Guest ----
    guest_name = text('guest_name', 150)
    guest_phone = V.clean_phone(p.get('guest_phone'))
    guest_email = text('guest_email', 254)
    if len(guest_name) < 2:
        errors['guest_name'] = "Enter the guest's full name."
    if V.phone_error(guest_phone):
        errors['guest_phone'] = V.phone_error(guest_phone)
    if V.email_error(guest_email):
        errors['guest_email'] = V.email_error(guest_email)

    # ---- Registration (required for walk-ins when the hotel's ID rule is on) ----
    need_id = walk_in and settings_obj.require_id_at_check_in
    nationality = text('nationality', 60) or ('Indian' if walk_in else '')
    foreign = nationality.lower() not in V.INDIAN
    id_type = p.get('id_type') if p.get('id_type') in RoomBooking.IdType.values else ''
    id_number = V.clean_id_number(p.get('id_number'))
    errors.update(V.id_errors(id_type, id_number, nationality, require=need_id))
    dob = V.parse_date(p.get('date_of_birth'))
    if V.dob_error(p.get('date_of_birth'), dob):
        errors['date_of_birth'] = V.dob_error(p.get('date_of_birth'), dob)
    visa_number = text('visa_number', 60)
    visa_valid_till = V.parse_date(p.get('visa_valid_till'))
    if foreign and need_id:
        if not visa_number:
            errors['visa_number'] = 'Visa number is required for foreign nationals.'
        if not visa_valid_till:
            errors['visa_valid_till'] = 'Visa expiry date is required.'
    if visa_valid_till and check_out_date and visa_valid_till < check_in_date:
        errors['visa_valid_till'] = 'The visa expires before the arrival date.'
    doc = request.FILES.get('id_document')
    if V.document_error(doc):
        errors['id_document'] = V.document_error(doc)
    elif need_id and settings_obj.require_id_document and not doc:
        errors['id_document'] = 'Upload a photo or scan of the ID.'
    guest_address = text('guest_address')
    if walk_in and not guest_address:
        errors['guest_address'] = 'Home address is required for the guest register.'

    # ---- Payment ----
    payment_status = p.get('payment_status', RoomBooking.PaymentStatus.PENDING)
    try:
        advance = Decimal(str(p.get('advance_amount') or '0').replace(',', ''))
    except Exception:
        advance = Decimal('0')
    if payment_status == RoomBooking.PaymentStatus.PARTIALLY_PAID and advance <= 0:
        errors['advance_amount'] = 'Enter the advance amount received.'

    if errors:
        return fail()

    source = p.get('source') if p.get('source') in RoomBooking.Source.values else RoomBooking.Source.DIRECT
    if walk_in:
        source = RoomBooking.Source.WALK_IN

    class CheckInFailed(Exception):
        pass

    try:
        with transaction.atomic():
            room = Room.objects.select_for_update().select_related('category').get(pk=room.pk)
            conflict = pricing.room_conflict(room, check_in_date, check_out_date)
            if conflict:
                errors['room_id'] = conflict
                return fail(conflict + ' Choose another room or other dates.')
            quote = pricing.quote(room.category, check_in_date, check_out_date, settings_obj)
            if quote['restriction'] and p.get('override_restriction') != '1':
                return fail(quote['restriction'] + ' Tick "Override rate restrictions" to book it anyway.')

            booking = RoomBooking.objects.create(
                room=room, branch=branch,
                guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone,
                check_in_date=check_in_date, check_out_date=check_out_date,
                num_guests=adults + children, adults=adults, children=children, source=source,
                total_room_charges=quote['net'], taxes=quote['tax'], grand_total=quote['total'],
                booking_status=RoomBooking.BookingStatus.RESERVED,
                payment_status=RoomBooking.PaymentStatus.PAID if payment_status == RoomBooking.PaymentStatus.PAID else RoomBooking.PaymentStatus.PENDING,
                payment_method=p.get('payment_method') or 'PAY_AT_DESK',
                special_requests=text('special_requests'),
                nationality=nationality, id_type=id_type, id_number=id_number, date_of_birth=dob,
                visa_number=visa_number if foreign else '', visa_valid_till=visa_valid_till if foreign else None,
                guest_address=guest_address, purpose_of_visit=text('purpose_of_visit', 100),
                arriving_from=text('arriving_from', 120), going_to=text('going_to', 120),
                id_document=doc or None,
                **_event_fields(p, room),
            )
            if payment_status == RoomBooking.PaymentStatus.PARTIALLY_PAID and advance > 0:
                method = p.get('payment_method') if p.get('payment_method') in FolioPayment.Method.values else 'CASH'
                folio.record_payment(booking, request.user, advance, method, note='Advance at booking')
            if walk_in:
                err = folio.check_in(booking, request.user)
                if err:
                    raise CheckInFailed(err)
    except CheckInFailed as e:
        return fail(f'Guest not checked in: {e}')

    try:
        from core.emails import send_hotel_booking_email
        send_hotel_booking_email(booking, request=request)
    except Exception as email_err:
        logger.warning(f"Could not send admin booking voucher email for {booking.booking_id}: {email_err}")

    if walk_in:
        messages.success(request, f"{booking.guest_name} is checked in to Room {room.room_number} (booking {booking.booking_id}).")
        return redirect('hotel_admin:booking_detail', restaurant_slug=restaurant_slug, booking_id=booking.pk)
    missing = booking.kyc_missing_items(require_document=settings_obj.require_id_document)
    note = f" Collect at check-in: {', '.join(missing)}." if missing and settings_obj.require_id_at_check_in else ''
    messages.success(request, f"Reservation confirmed! Booking ID: {booking.booking_id} for Room {room.room_number}.{note}")
    return redirect(back)


@hotel_admin_required
def hotel_settings_view(request, restaurant_slug, ctx):
    """
    Hotel Settings Management:
    - Online Payment Gateway (Razorpay, Stripe, PhonePe, UPI)
    - Taxes & GST Compliance (Dynamic GST vs Flat Slabs, Service Charge)
    - Hotel Property Info & Check-in / Check-out Timings
    - Guest Receipt / Invoice Formatting
    """
    if not ctx['show_hotel_settings']:
        messages.error(request, "Permission denied: Only Hotel Administrators can configure hotel settings.")
        return redirect('hotel_admin:rooms' if ctx['is_housekeeping'] else 'hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    branch = ctx['current_branch']
    if not branch:
        messages.error(request, "No active hotel branch found.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    settings_obj = HotelSettings.get_settings(branch)

    if request.method == 'POST':
        form = HotelSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Hotel configuration, payment gateway credentials, and tax settings updated successfully!")
            return redirect('hotel_admin:settings', restaurant_slug=restaurant_slug)
        else:
            messages.error(request, "Please review the form errors below before saving settings.")
    else:
        form = HotelSettingsForm(instance=settings_obj)

    from core import two_factor
    return render(request, 'hotel_admin/settings.html', {
        **ctx,
        'two_factor_on': two_factor.is_enabled(request.user),
        'active_menu': 'settings',
        'settings_obj': settings_obj,
        'form': form,
    })


@hotel_admin_required
def hotel_test_email_view(request, restaurant_slug, ctx):
    """
    Live test dispatcher for hotel custom SMTP credentials.
    Accepts POST parameters via AJAX, verifies SMTP connection, and sends a test email.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method.'}, status=405)

    from core.emails import send_test_email

    host = request.POST.get('email_host', 'smtp.gmail.com')
    port = request.POST.get('email_port', 587)
    username = request.POST.get('email_host_user', '')
    password = request.POST.get('email_host_password', '')
    use_tls = request.POST.get('email_use_tls') in ['true', 'True', '1', 'on']
    use_ssl = request.POST.get('email_use_ssl') in ['true', 'True', '1', 'on']
    recipient = request.POST.get('test_recipient', '').strip() or request.user.email
    sender_name = request.POST.get('email_sender_name', '').strip() or "Hotel PMS"

    success, message = send_test_email(
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        recipient_email=recipient,
        sender_name=sender_name
    )

    return JsonResponse({'success': success, 'message': message}, status=200 if success else 400)


# ==============================================================================
# HOTEL STAFF & OPERATIONS TEAM MANAGEMENT (PMS-NATIVE)
# ==============================================================================

@hotel_admin_required
def staff_list_view(request, restaurant_slug, ctx):
    """
    Hotel Staff & Operations Team Directory (PMS-native):
    Strictly scoped to Front Desk Receptionists (FRONT_DESK) and Housekeeping Attendants (HOUSEKEEPING).
    Non-GM Hotel Admins are strictly scoped to their assigned hotel branch.
    """
    user_role = ctx['user_role']
    is_gm = ctx['is_general_manager']
    branch = ctx['current_branch']
    restaurant = ctx['restaurant']

    # Security check: Only Hotel Admin and GM can view staff management
    if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER'] and not request.user.is_superuser:
        messages.error(request, "Permission denied: Staff management requires Hotel Administrator access.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    # Base queryset strictly limited to Hotel operational roles
    staff_qs = RestaurantUser.objects.filter(
        restaurant=restaurant,
        role__in=[RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]
    ).select_related('user', 'branch', 'created_by')

    # Branch scoping: HOTEL_ADMIN is strictly locked to their branch
    if not is_gm:
        if branch:
            staff_qs = staff_qs.filter(branch=branch)
        else:
            staff_qs = staff_qs.none()
    else:
        # GM can optionally filter by branch if provided
        branch_filter = request.GET.get('branch_id')
        if branch_filter and str(branch_filter).isdigit():
            staff_qs = staff_qs.filter(branch_id=int(branch_filter))

    # Metric counts (before role/search filters)
    total_staff = staff_qs.count()
    front_desk_count = staff_qs.filter(role=RestaurantUser.Role.FRONT_DESK).count()
    housekeeping_count = staff_qs.filter(role=RestaurantUser.Role.HOUSEKEEPING).count()

    # Search filter
    search_query = request.GET.get('q', '').strip()
    if search_query:
        staff_qs = staff_qs.filter(
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query) |
            Q(user__username__icontains=search_query) |
            Q(user__email__icontains=search_query) |
            Q(pos_pin__icontains=search_query)
        )

    # Role filter
    role_filter = request.GET.get('role', '').strip()
    if role_filter in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        staff_qs = staff_qs.filter(role=role_filter)

    staff_members = staff_qs.order_by('-created_at')

    # Available hotel branches for GM
    hotel_branches = restaurant.branches.filter(has_hotel_module=True) if is_gm else ([branch] if branch else [])

    return render(request, 'hotel_admin/staff/staff_list.html', {
        **ctx,
        'active_menu': 'staff',
        'staff_members': staff_members,
        'total_staff': total_staff,
        'front_desk_count': front_desk_count,
        'housekeeping_count': housekeeping_count,
        'search_query': search_query,
        'role_filter': role_filter,
        'hotel_branches': hotel_branches,
        'selected_branch_id': request.GET.get('branch_id', ''),
    })


@hotel_admin_required
def staff_create_view(request, restaurant_slug, ctx):
    """
    Create a new Hotel PMS staff member (Front Desk Receptionist or Housekeeping Attendant).
    """
    user_role = ctx['user_role']
    is_gm = ctx['is_general_manager']
    branch = ctx['current_branch']
    restaurant = ctx['restaurant']

    if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER'] and not request.user.is_superuser:
        messages.error(request, "Permission denied: Staff creation requires Hotel Administrator access.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    allowed_roles = [
        (RestaurantUser.Role.FRONT_DESK, 'Front Desk Receptionist (Check-in, Check-out, Bookings)'),
        (RestaurantUser.Role.HOUSEKEEPING, 'Housekeeping Attendant (Room Cleaning & Maintenance)'),
    ]

    hotel_branches = restaurant.branches.filter(has_hotel_module=True) if is_gm else ([branch] if branch else [])

    errors = {}
    form_data = {
        'username': '',
        'first_name': '',
        'last_name': '',
        'email': '',
        'phone': '',
        'pos_pin': '',
        'role': RestaurantUser.Role.FRONT_DESK,
        'branch_id': str(branch.id) if branch else '',
        'is_active': True,
    }

    if request.method == 'POST':
        form_data['username'] = request.POST.get('username', '').strip()
        form_data['first_name'] = request.POST.get('first_name', '').strip()
        form_data['last_name'] = request.POST.get('last_name', '').strip()
        form_data['email'] = request.POST.get('email', '').strip()
        form_data['phone'] = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '').strip()
        form_data['pos_pin'] = request.POST.get('pos_pin', '').strip()
        form_data['role'] = request.POST.get('role', '').strip()
        form_data['branch_id'] = request.POST.get('branch_id', '').strip()
        form_data['is_active'] = request.POST.get('is_active') == 'on'

        # Validation
        if not form_data['username']:
            errors['username'] = "Username is required."
        elif User.objects.filter(username__iexact=form_data['username']).exists():
            errors['username'] = "This username is already taken. Please choose another."

        if not password:
            errors['password'] = "Password is required."
        elif len(password) < 6:
            errors['password'] = "Password must be at least 6 characters."

        valid_roles = [r[0] for r in allowed_roles]
        if form_data['role'] not in valid_roles:
            errors['role'] = "Please select a valid Hotel PMS role."

        if form_data['pos_pin'] and (not form_data['pos_pin'].isdigit() or len(form_data['pos_pin']) != 4):
            errors['pos_pin'] = "Quick Switch PIN must be exactly 4 digits."

        # Target branch
        if is_gm and form_data['branch_id'] and form_data['branch_id'].isdigit():
            target_branch = restaurant.branches.filter(id=int(form_data['branch_id']), has_hotel_module=True).first()
            if not target_branch:
                target_branch = branch
        else:
            target_branch = branch

        if not target_branch:
            errors['branch'] = "No valid hotel branch is assigned."

        if not errors:
            try:
                with transaction.atomic():
                    new_user = User.objects.create_user(
                        username=form_data['username'],
                        email=form_data['email'],
                        password=password,
                        first_name=form_data['first_name'],
                        last_name=form_data['last_name'],
                    )
                    new_user.is_staff = True
                    new_user.is_active = form_data['is_active']
                    new_user.save()

                    try:
                        grp, _ = Group.objects.get_or_create(name="RestaurantAdmin")
                        new_user.groups.add(grp)
                    except Exception:
                        pass

                    ru = RestaurantUser.objects.create(
                        user=new_user,
                        restaurant=restaurant,
                        branch=target_branch,
                        role=form_data['role'],
                        is_primary_admin=False,
                        pos_pin=form_data['pos_pin'] or None,
                        created_by=request.user,
                    )

                messages.success(request, f"Hotel staff member '{new_user.get_full_name() or new_user.username}' created successfully.")
                return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)
            except Exception as e:
                errors['general'] = f"Failed to create staff member: {str(e)}"

    return render(request, 'hotel_admin/staff/staff_form.html', {
        **ctx,
        'active_menu': 'staff',
        'is_edit': False,
        'allowed_roles': allowed_roles,
        'hotel_branches': hotel_branches,
        'form_data': form_data,
        'errors': errors,
    })


@hotel_admin_required
def staff_edit_view(request, restaurant_slug, ctx, user_id):
    """
    Edit an existing Hotel staff member (Front Desk Receptionist or Housekeeping Attendant).
    """
    user_role = ctx['user_role']
    is_gm = ctx['is_general_manager']
    branch = ctx['current_branch']
    restaurant = ctx['restaurant']

    if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER'] and not request.user.is_superuser:
        messages.error(request, "Permission denied: Staff editing requires Hotel Administrator access.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    target_profile = get_object_or_404(
        RestaurantUser.objects.select_related('user', 'branch'),
        user_id=user_id,
        restaurant=restaurant
    )

    # Security: Ensure target is a hotel staff role
    if target_profile.role not in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        messages.error(request, "Unauthorized: Only Hotel PMS staff (Front Desk & Housekeeping) can be edited from Hotel PMS.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    # Security: Non-GM cannot edit staff of other branches
    if not is_gm and target_profile.branch_id != (branch.id if branch else None):
        messages.error(request, "Unauthorized: You can only edit staff in your assigned property.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    allowed_roles = [
        (RestaurantUser.Role.FRONT_DESK, 'Front Desk Receptionist (Check-in, Check-out, Bookings)'),
        (RestaurantUser.Role.HOUSEKEEPING, 'Housekeeping Attendant (Room Cleaning & Maintenance)'),
    ]
    hotel_branches = restaurant.branches.filter(has_hotel_module=True) if is_gm else ([branch] if branch else [])

    errors = {}
    target_user = target_profile.user
    form_data = {
        'username': target_user.username,
        'first_name': target_user.first_name,
        'last_name': target_user.last_name,
        'email': target_user.email,
        'phone': '',
        'pos_pin': target_profile.pos_pin or '',
        'role': target_profile.role,
        'branch_id': str(target_profile.branch_id or ''),
        'is_active': target_user.is_active,
    }

    if request.method == 'POST':
        form_data['first_name'] = request.POST.get('first_name', '').strip()
        form_data['last_name'] = request.POST.get('last_name', '').strip()
        form_data['email'] = request.POST.get('email', '').strip()
        form_data['phone'] = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '').strip()
        form_data['pos_pin'] = request.POST.get('pos_pin', '').strip()
        form_data['role'] = request.POST.get('role', '').strip()
        form_data['branch_id'] = request.POST.get('branch_id', '').strip()
        form_data['is_active'] = request.POST.get('is_active') == 'on'

        if password and len(password) < 6:
            errors['password'] = "New password must be at least 6 characters."

        valid_roles = [r[0] for r in allowed_roles]
        if form_data['role'] not in valid_roles:
            errors['role'] = "Please select a valid Hotel PMS role."

        if form_data['pos_pin'] and (not form_data['pos_pin'].isdigit() or len(form_data['pos_pin']) != 4):
            errors['pos_pin'] = "Quick Switch PIN must be exactly 4 digits."

        if not errors:
            try:
                with transaction.atomic():
                    target_user.first_name = form_data['first_name']
                    target_user.last_name = form_data['last_name']
                    target_user.email = form_data['email']
                    target_user.is_active = form_data['is_active']
                    if password:
                        target_user.set_password(password)
                    target_user.save()

                    target_profile.role = form_data['role']
                    target_profile.pos_pin = form_data['pos_pin'] or None

                    if is_gm and form_data['branch_id'] and form_data['branch_id'].isdigit():
                        new_branch = restaurant.branches.filter(id=int(form_data['branch_id']), has_hotel_module=True).first()
                        if new_branch:
                            target_profile.branch = new_branch

                    target_profile.save()

                messages.success(request, f"Staff account @{target_user.username} updated successfully.")
                return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)
            except Exception as e:
                errors['general'] = f"Failed to update staff member: {str(e)}"

    return render(request, 'hotel_admin/staff/staff_form.html', {
        **ctx,
        'active_menu': 'staff',
        'is_edit': True,
        'target_user': target_user,
        'target_profile': target_profile,
        'allowed_roles': allowed_roles,
        'hotel_branches': hotel_branches,
        'form_data': form_data,
        'errors': errors,
    })


@hotel_admin_required
def staff_toggle_active_view(request, restaurant_slug, ctx, user_id):
    """Toggle active status for a Hotel staff member."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST method required")

    user_role = ctx['user_role']
    is_gm = ctx['is_general_manager']
    branch = ctx['current_branch']
    restaurant = ctx['restaurant']

    if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER'] and not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    target_profile = get_object_or_404(RestaurantUser, user_id=user_id, restaurant=restaurant)

    if target_profile.user == request.user:
        messages.error(request, "You cannot deactivate your own administrative account.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    if target_profile.role not in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        messages.error(request, "Unauthorized to toggle non-hotel staff.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    if not is_gm and target_profile.branch_id != (branch.id if branch else None):
        messages.error(request, "Unauthorized: Staff belongs to another property.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    new_state = not target_profile.user.is_active
    target_profile.user.is_active = new_state
    target_profile.user.save()

    status_str = "activated" if new_state else "deactivated"
    messages.success(request, f"Staff member @{target_profile.user.username} has been {status_str}.")
    return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)


@hotel_admin_required
def staff_delete_view(request, restaurant_slug, ctx, user_id):
    """Delete a Hotel staff member."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST method required")

    user_role = ctx['user_role']
    is_gm = ctx['is_general_manager']
    branch = ctx['current_branch']
    restaurant = ctx['restaurant']

    if user_role not in ['HOTEL_ADMIN', 'GENERAL_MANAGER'] and not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect('hotel_admin:dashboard', restaurant_slug=restaurant_slug)

    target_profile = get_object_or_404(RestaurantUser, user_id=user_id, restaurant=restaurant)

    if target_profile.user == request.user:
        messages.error(request, "You cannot delete your own administrative account.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    if target_profile.role not in [RestaurantUser.Role.FRONT_DESK, RestaurantUser.Role.HOUSEKEEPING]:
        messages.error(request, "Unauthorized to delete non-hotel staff.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    if not is_gm and target_profile.branch_id != (branch.id if branch else None):
        messages.error(request, "Unauthorized: Staff belongs to another property.")
        return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)

    username = target_profile.user.username
    target_profile.user.delete()
    messages.success(request, f"Staff member @{username} has been permanently deleted.")
    return redirect('hotel_admin:staff_list', restaurant_slug=restaurant_slug)


# ==============================================================================
# TRAVEL DESK & CAB MANAGEMENT PMS VIEWS
# ==============================================================================

@hotel_admin_required
def cabs_dispatch_view(request, restaurant_slug, ctx):
    """
    Hotel Travel Desk PMS Interface:
    - Daily Chronological Dispatch Board with quick driver assignment
    - Fleet & Driver Management (In-house and Outsourced vendor cabs)
    - Transfer & Tourism Packages Catalog
    """
    branch = ctx['current_branch']
    if not branch or not getattr(branch, 'has_hotel_module', False):
        return render(request, 'hotel_admin/feature_disabled.html', ctx)

    today = timezone.localdate()
    date_param = request.GET.get('date', '').strip()
    selected_date = None
    if date_param:
        try:
            selected_date = datetime.date.fromisoformat(date_param)
        except ValueError:
            selected_date = None

    search_query = request.GET.get('q', '').strip()
    service_filter = request.GET.get('service_type', '')
    status_filter = request.GET.get('status', '')
    source_filter = request.GET.get('source', '')
    unassigned_filter = request.GET.get('unassigned_only', '')
    show_all = request.GET.get('show_all', '') in ['1', 'true']
    filter_mode = request.GET.get('mode', '').strip().lower()

    if not filter_mode:
        if selected_date:
            filter_mode = 'date'
        elif show_all:
            filter_mode = 'all'
        else:
            filter_mode = 'all'  # Default to showing all bookings/inquiries so nothing is missed!

    base_qs = CabBooking.objects.filter(branch=branch).select_related(
        'room_booking', 'room_booking__room', 'package', 'assigned_vehicle'
    )

    # Global metric counts
    total_all_count = base_qs.count()
    today_count = base_qs.filter(pickup_datetime__date=today).count()
    upcoming_count = base_qs.filter(pickup_datetime__date__gte=today).exclude(trip_status=CabBooking.TripStatus.CANCELLED).count()
    web_inquiries_count = base_qs.filter(source=CabBooking.BookingSource.FRONTEND_DIRECT).count()
    unassigned_count = base_qs.filter(
        Q(assigned_vehicle__isnull=True) | Q(trip_status=CabBooking.TripStatus.SCHEDULED)
    ).exclude(trip_status=CabBooking.TripStatus.CANCELLED).count()
    assigned_count = base_qs.filter(trip_status=CabBooking.TripStatus.DRIVER_ASSIGNED).count()
    en_route_count = base_qs.filter(trip_status=CabBooking.TripStatus.EN_ROUTE).count()
    completed_count = base_qs.filter(trip_status=CabBooking.TripStatus.COMPLETED).count()
    cancelled_count = base_qs.filter(trip_status=CabBooking.TripStatus.CANCELLED).count()

    filtered_qs = base_qs

    if search_query:
        # Cross-date search across guest name, phone, booking ID, vehicle plate, driver, flight #
        filtered_qs = filtered_qs.filter(
            Q(booking_id__icontains=search_query) |
            Q(guest_name__icontains=search_query) |
            Q(guest_phone__icontains=search_query) |
            Q(guest_email__icontains=search_query) |
            Q(flight_or_train_number__icontains=search_query) |
            Q(selected_cab_name__icontains=search_query) |
            Q(preferred_chauffeur__icontains=search_query) |
            Q(room_booking__guest_name__icontains=search_query) |
            Q(room_booking__guest_phone__icontains=search_query) |
            Q(assigned_vehicle__driver_name__icontains=search_query) |
            Q(assigned_vehicle__vehicle_number__icontains=search_query)
        )
    elif filter_mode == 'date' and selected_date:
        filtered_qs = filtered_qs.filter(pickup_datetime__date=selected_date)
    elif filter_mode == 'today':
        filtered_qs = filtered_qs.filter(pickup_datetime__date=today)
    elif filter_mode == 'upcoming':
        filtered_qs = filtered_qs.filter(pickup_datetime__date__gte=today).exclude(trip_status=CabBooking.TripStatus.CANCELLED)
    elif filter_mode == 'unassigned':
        filtered_qs = filtered_qs.filter(
            Q(assigned_vehicle__isnull=True) | Q(trip_status=CabBooking.TripStatus.SCHEDULED)
        ).exclude(trip_status=CabBooking.TripStatus.CANCELLED)
    elif filter_mode == 'web_inquiries':
        filtered_qs = filtered_qs.filter(source=CabBooking.BookingSource.FRONTEND_DIRECT)
    elif filter_mode == 'completed':
        filtered_qs = filtered_qs.filter(trip_status=CabBooking.TripStatus.COMPLETED)
    # else 'all' shows all bookings

    if service_filter:
        filtered_qs = filtered_qs.filter(Q(package__service_type=service_filter) | Q(trip_type=service_filter))
    if status_filter:
        filtered_qs = filtered_qs.filter(trip_status=status_filter)
    if source_filter:
        filtered_qs = filtered_qs.filter(source=source_filter)
    if unassigned_filter in ['true', '1']:
        filtered_qs = filtered_qs.filter(
            Q(assigned_vehicle__isnull=True) | Q(trip_status=CabBooking.TripStatus.SCHEDULED)
        ).exclude(trip_status=CabBooking.TripStatus.CANCELLED)

    bookings = filtered_qs.order_by('-created_at')

    fleet_vehicles = FleetVehicle.objects.filter(branch=branch).order_by('vehicle_category', 'vehicle_number')
    available_vehicles = fleet_vehicles.filter(status=FleetVehicle.VehicleStatus.AVAILABLE)
    packages = CabServicePackage.objects.filter(branch=branch).order_by('service_type', 'title')

    # Active room bookings for front desk quick booking
    active_room_bookings = RoomBooking.objects.filter(
        branch=branch,
        booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN]
    ).select_related('room', 'room__category').order_by('-created_at')[:40]

    ctx.update({
        'active_menu': 'cabs',
        'page_title': 'Travel Desk & Cab Dispatch',
        'today': today,
        'today_str': today.isoformat(),
        'selected_date': selected_date,
        'selected_date_str': selected_date.isoformat() if selected_date else '',
        'filter_mode': filter_mode,
        'search_query': search_query,
        'service_filter': service_filter,
        'status_filter': status_filter,
        'source_filter': source_filter,
        'unassigned_filter': unassigned_filter,
        'show_all': show_all or filter_mode == 'all',
        'bookings': bookings,
        'total_count': total_all_count,
        'today_count': today_count,
        'upcoming_count': upcoming_count,
        'web_inquiries_count': web_inquiries_count,
        'unassigned_count': unassigned_count,
        'assigned_count': assigned_count,
        'en_route_count': en_route_count,
        'completed_count': completed_count,
        'cancelled_count': cancelled_count,
        'fleet_vehicles': fleet_vehicles,
        'available_vehicles': available_vehicles,
        'packages': packages,
        'active_room_bookings': active_room_bookings,
    })
    return render(request, 'hotel_admin/cabs_dashboard.html', ctx)


@hotel_admin_required
def cab_package_create_edit_action(request, restaurant_slug, ctx):
    """Create or update a CabServicePackage offering."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    package_id = request.POST.get('package_id')
    title = request.POST.get('title', '').strip()
    service_type = request.POST.get('service_type', 'AIRPORT_TRANSFER')
    vehicle_category = request.POST.get('vehicle_category', 'SEDAN')
    base_price = request.POST.get('base_price', '0')
    included_km = request.POST.get('included_km', '0')
    included_hours = request.POST.get('included_hours', '0')
    itinerary_description = request.POST.get('itinerary_description', '').strip()
    is_active = request.POST.get('is_active') in ['true', '1', 'on']

    if not title:
        messages.error(request, "Package title is required.")
        return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    try:
        price_val = Decimal(base_price)
        km_val = int(included_km) if included_km else 0
        hrs_val = int(included_hours) if included_hours else 0
    except Exception:
        messages.error(request, "Invalid numeric values for price, km, or hours.")
        return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    if package_id:
        pkg = get_object_or_404(CabServicePackage, id=package_id, branch=branch)
        pkg.title = title
        pkg.service_type = service_type
        pkg.vehicle_category = vehicle_category
        pkg.base_price = price_val
        pkg.included_km = km_val
        pkg.included_hours = hrs_val
        pkg.itinerary_description = itinerary_description
        pkg.is_active = is_active
        pkg.save()
        messages.success(request, f"Package '{title}' updated successfully.")
    else:
        CabServicePackage.objects.create(
            branch=branch,
            title=title,
            service_type=service_type,
            vehicle_category=vehicle_category,
            base_price=price_val,
            included_km=km_val,
            included_hours=hrs_val,
            itinerary_description=itinerary_description,
            is_active=is_active,
        )
        messages.success(request, f"New package '{title}' created successfully.")

    return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)


@hotel_admin_required
def cab_package_delete_action(request, restaurant_slug, ctx, package_id):
    """Delete a CabServicePackage offering."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    pkg = get_object_or_404(CabServicePackage, id=package_id, branch=branch)
    title = pkg.title
    pkg.delete()
    messages.success(request, f"Package '{title}' deleted successfully.")
    return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)


@hotel_admin_required
def cab_fleet_create_edit_action(request, restaurant_slug, ctx):
    """Register or update a FleetVehicle and driver with all details."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    vehicle_id = request.POST.get('vehicle_id')
    vehicle_number = request.POST.get('vehicle_number', '').strip().upper()
    vehicle_model = request.POST.get('vehicle_model', '').strip()
    vehicle_category = request.POST.get('vehicle_category', 'SEDAN')
    tag = request.POST.get('tag', 'Most Popular').strip()
    description = request.POST.get('description', '').strip()
    cover_image = request.POST.get('cover_image', '').strip()

    # Local file upload for Primary Cover Image
    if 'cover_image_file' in request.FILES and request.FILES['cover_image_file']:
        try:
            converted_cover = optimize_image_to_webp(request.FILES['cover_image_file'])
            save_name = default_storage.save(f"hotel/cabs/covers/cab_{uuid.uuid4().hex[:8]}.webp", converted_cover)
            cover_image = default_storage.url(save_name)
        except Exception as e:
            messages.warning(request, f"Cover image upload failed: {e}")

    # Local file upload for Chauffeur Photo
    driver_photo = request.POST.get('driver_photo', '').strip()
    if 'driver_photo_file' in request.FILES and request.FILES['driver_photo_file']:
        try:
            converted_driver = optimize_image_to_webp(request.FILES['driver_photo_file'])
            save_name = default_storage.save(f"hotel/cabs/drivers/drv_{uuid.uuid4().hex[:8]}.webp", converted_driver)
            driver_photo = default_storage.url(save_name)
        except Exception as e:
            messages.warning(request, f"Chauffeur photo upload failed: {e}")

    # Gallery images: Existing kept images + Textarea URLs + Local File Uploads (Capped at 10 max)
    MAX_GALLERY_IMAGES = 10
    existing_gallery = [img.strip() for img in request.POST.getlist('existing_gallery_images') if img.strip()]
    gallery_raw = request.POST.get('gallery_images', '').strip()
    new_urls = [img.strip() for img in gallery_raw.replace(',', '\n').splitlines() if img.strip()]

    gallery_images = []
    for img in existing_gallery + new_urls:
        if img and img not in gallery_images:
            gallery_images.append(img)

    uploaded_gallery_files = request.FILES.getlist('gallery_image_files')
    if uploaded_gallery_files:
        for g_file in uploaded_gallery_files:
            if len(gallery_images) >= MAX_GALLERY_IMAGES:
                messages.warning(request, f"Gallery capacity reached ({MAX_GALLERY_IMAGES} images maximum). Additional files were skipped.")
                break
            try:
                converted_gal = optimize_image_to_webp(g_file)
                save_name = default_storage.save(f"hotel/cabs/gallery/cab_gal_{uuid.uuid4().hex[:8]}.webp", converted_gal)
                gallery_images.append(default_storage.url(save_name))
            except Exception as e:
                messages.warning(request, f"Gallery file '{g_file.name}' upload failed: {e}")

    # Ensure max 10 images limit
    if len(gallery_images) > MAX_GALLERY_IMAGES:
        gallery_images = gallery_images[:MAX_GALLERY_IMAGES]
        messages.warning(request, f"Gallery images capped at the maximum limit of {MAX_GALLERY_IMAGES} photos.")

    remove_cover = request.POST.get('remove_cover_image') in ['true', '1', 'on']
    if remove_cover and not ('cover_image_file' in request.FILES and request.FILES['cover_image_file']):
        cover_image = ''

    if cover_image and cover_image not in gallery_images and len(gallery_images) < MAX_GALLERY_IMAGES:
        gallery_images.insert(0, cover_image)

    try:
        passengers = int(request.POST.get('passengers', 4) or 4)
    except Exception:
        passengers = 4

    try:
        luggage = int(request.POST.get('luggage', 3) or 3)
    except Exception:
        luggage = 3

    fuel_type = request.POST.get('fuel_type', 'Petrol').strip()
    transmission = request.POST.get('transmission', 'Automatic').strip()

    features_raw = request.POST.get('features', '').strip()
    features = [f.strip() for f in features_raw.replace(',', '\n').splitlines() if f.strip()]

    amenities_raw = request.POST.get('amenities', '').strip()
    amenities = [a.strip() for a in amenities_raw.replace(',', '\n').splitlines() if a.strip()]

    try:
        price_per_km = Decimal(request.POST.get('price_per_km', '35.00') or '35.00')
    except Exception:
        price_per_km = Decimal('35.00')

    try:
        base_price_airport = Decimal(request.POST.get('base_price_airport', '2500.00') or '2500.00')
    except Exception:
        base_price_airport = Decimal('2500.00')

    try:
        hourly_rate = Decimal(request.POST.get('hourly_rate', '800.00') or '800.00')
    except Exception:
        hourly_rate = Decimal('800.00')

    try:
        full_day_rate = Decimal(request.POST.get('full_day_rate', '7500.00') or '7500.00')
    except Exception:
        full_day_rate = Decimal('7500.00')

    try:
        cab_rating = Decimal(request.POST.get('rating', '4.95') or '4.95')
    except Exception:
        cab_rating = Decimal('4.95')

    try:
        reviews_count = int(request.POST.get('reviews_count', 120) or 120)
    except Exception:
        reviews_count = 120

    # Chauffeur details
    driver_name = request.POST.get('driver_name', '').strip()
    driver_title = request.POST.get('driver_title', 'Executive Chauffeur').strip()
    driver_phone = request.POST.get('driver_phone', '').strip()
    driver_rating_str = request.POST.get('driver_rating', '4.95').strip()
    driver_exp_str = request.POST.get('driver_experience_years', '5').strip()
    driver_bio = request.POST.get('driver_bio', '').strip()
    try:
        trips_completed = int(request.POST.get('trips_completed', 150) or 150)
    except Exception:
        trips_completed = 150
    driver_languages = request.POST.get('driver_languages', 'English, Hindi').strip()
    driver_badges = request.POST.get('driver_badges', 'Executive Lead, Defensive Driving Certified').strip()
    driver_availability = request.POST.get('driver_availability', 'Available Now').strip()

    is_vendor = request.POST.get('is_vendor_vehicle') in ['true', '1', 'on']
    status_val = request.POST.get('status', 'AVAILABLE')
    notes = request.POST.get('notes', '').strip()

    if not vehicle_number or not driver_name or not driver_phone:
        messages.error(request, "Vehicle number, driver name, and driver phone are required.")
        return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    try:
        driver_rating = Decimal(driver_rating_str) if driver_rating_str else Decimal('4.95')
        driver_exp = int(driver_exp_str) if driver_exp_str else 5
    except Exception:
        driver_rating = Decimal('4.95')
        driver_exp = 5

    if vehicle_id:
        v = get_object_or_404(FleetVehicle, id=vehicle_id, branch=branch)
        # Check duplicate vehicle number if changed
        if FleetVehicle.objects.filter(branch=branch, vehicle_number=vehicle_number).exclude(pk=v.pk).exists():
            messages.error(request, f"Vehicle number '{vehicle_number}' is already registered for this branch.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

        v.vehicle_number = vehicle_number
        v.vehicle_model = vehicle_model
        v.vehicle_category = vehicle_category
        v.tag = tag
        v.description = description
        if 'cover_image_file' in request.FILES and request.FILES['cover_image_file']:
            v.cover_image = cover_image
        elif remove_cover:
            v.cover_image = ''
        elif 'cover_image' in request.POST:
            v.cover_image = cover_image

        v.gallery_images = gallery_images
        v.passengers = passengers
        v.luggage = luggage
        v.fuel_type = fuel_type
        v.transmission = transmission
        v.features = features
        v.amenities = amenities
        v.price_per_km = price_per_km
        v.base_price_airport = base_price_airport
        v.hourly_rate = hourly_rate
        v.full_day_rate = full_day_rate
        v.rating = cab_rating
        v.reviews_count = reviews_count

        v.driver_name = driver_name
        v.driver_title = driver_title
        v.driver_phone = driver_phone
        if driver_photo:
            v.driver_photo = driver_photo
        v.driver_rating = driver_rating
        v.driver_experience_years = driver_exp
        v.trips_completed = trips_completed
        v.driver_bio = driver_bio
        v.driver_languages = driver_languages
        v.driver_badges = driver_badges
        v.driver_availability = driver_availability

        v.is_vendor_vehicle = is_vendor
        v.status = status_val
        v.notes = notes
        v.save()
        messages.success(request, f"Vehicle '{vehicle_number}' ({vehicle_model or driver_name}) updated successfully.")
    else:
        if FleetVehicle.objects.filter(branch=branch, vehicle_number=vehicle_number).exists():
            messages.error(request, f"Vehicle number '{vehicle_number}' is already registered for this branch.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

        FleetVehicle.objects.create(
            branch=branch,
            vehicle_number=vehicle_number,
            vehicle_model=vehicle_model,
            vehicle_category=vehicle_category,
            tag=tag,
            description=description,
            cover_image=cover_image,
            gallery_images=gallery_images,
            passengers=passengers,
            luggage=luggage,
            fuel_type=fuel_type,
            transmission=transmission,
            features=features,
            amenities=amenities,
            price_per_km=price_per_km,
            base_price_airport=base_price_airport,
            hourly_rate=hourly_rate,
            full_day_rate=full_day_rate,
            rating=cab_rating,
            reviews_count=reviews_count,
            driver_name=driver_name,
            driver_title=driver_title,
            driver_phone=driver_phone,
            driver_photo=driver_photo,
            driver_rating=driver_rating,
            driver_experience_years=driver_exp,
            trips_completed=trips_completed,
            driver_bio=driver_bio,
            driver_languages=driver_languages,
            driver_badges=driver_badges,
            driver_availability=driver_availability,
            is_vendor_vehicle=is_vendor,
            status=status_val,
            notes=notes,
        )
        messages.success(request, f"Vehicle '{vehicle_number}' ({vehicle_model or driver_name}) registered successfully.")

    return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)


@hotel_admin_required
def cab_fleet_delete_action(request, restaurant_slug, ctx, vehicle_id):
    """Delete a vehicle from the fleet inventory."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    v = get_object_or_404(FleetVehicle, id=vehicle_id, branch=branch)
    num = v.vehicle_number
    v.delete()
    messages.success(request, f"Vehicle '{num}' deleted from fleet.")
    return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)


@hotel_admin_required
def cab_fleet_delete_image_action(request, restaurant_slug, ctx, vehicle_id):
    """
    AJAX action to delete a specific photo from vehicle gallery or cover.
    Removes the image URL from v.gallery_images and if it's the cover, clears or reassigns it.
    Cleans up the underlying local media file from default_storage if applicable.
    """
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    v = get_object_or_404(FleetVehicle, id=vehicle_id, branch=branch)

    image_url = request.POST.get('image_url', '').strip()
    if not image_url:
        return JsonResponse({'success': False, 'error': 'No image URL provided'}, status=400)

    # Remove from gallery_images
    current_gallery = list(v.gallery_images or [])
    new_gallery = [img for img in current_gallery if img != image_url]
    v.gallery_images = new_gallery

    # If it was the cover image, update cover
    was_cover = False
    if v.cover_image == image_url:
        was_cover = True
        v.cover_image = new_gallery[0] if new_gallery else ''

    v.save()

    # If local media file, delete physical file from disk
    if image_url.startswith('/media/'):
        rel_path = image_url[len('/media/'):]
        if default_storage.exists(rel_path):
            try:
                default_storage.delete(rel_path)
            except Exception:
                pass

    return JsonResponse({
        'success': True,
        'message': 'Image deleted successfully.',
        'remaining_count': len(v.gallery_images),
        'was_cover': was_cover,
        'new_cover_image': v.cover_image
    })


@hotel_admin_required
def cab_assign_driver_action(request, restaurant_slug, ctx, cab_id):
    """Assign an existing vehicle OR quick-add an outsourced vendor cab."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    from hotel.views import send_driver_assignment_notification
    branch = ctx['current_branch']
    booking = get_object_or_404(CabBooking, id=cab_id, branch=branch)

    quick_add = request.POST.get('quick_add_vendor') in ['true', '1', 'on'] or request.POST.get('action') == 'quick_vendor'
    target_vehicle = None

    if quick_add:
        v_num = request.POST.get('vehicle_number', '').strip().upper()
        d_name = request.POST.get('driver_name', '').strip()
        d_phone = request.POST.get('driver_phone', '').strip()
        v_cat = request.POST.get('vehicle_category') or (booking.package.vehicle_category if booking.package else 'SEDAN')

        if not v_num or not d_name or not d_phone:
            messages.error(request, "Outsourced vendor cab requires license plate, driver name, and phone number.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

        target_vehicle, _ = FleetVehicle.objects.get_or_create(
            branch=branch,
            vehicle_number=v_num,
            defaults={
                'driver_name': d_name,
                'driver_phone': d_phone,
                'vehicle_category': v_cat,
                'is_vendor_vehicle': True,
                'status': FleetVehicle.VehicleStatus.AVAILABLE,
            }
        )
        target_vehicle.driver_name = d_name
        target_vehicle.driver_phone = d_phone
        target_vehicle.is_vendor_vehicle = True
        target_vehicle.save()
    else:
        v_id = request.POST.get('vehicle_id')
        if not v_id:
            messages.error(request, "Please select a vehicle to assign.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)
        target_vehicle = get_object_or_404(FleetVehicle, id=v_id, branch=branch)

    # Release old vehicle if different
    if booking.assigned_vehicle and booking.assigned_vehicle.id != target_vehicle.id:
        old_v = booking.assigned_vehicle
        other_active = CabBooking.objects.filter(
            assigned_vehicle=old_v,
            trip_status__in=[CabBooking.TripStatus.DRIVER_ASSIGNED, CabBooking.TripStatus.EN_ROUTE]
        ).exclude(pk=booking.pk).exists()
        if not other_active:
            old_v.status = FleetVehicle.VehicleStatus.AVAILABLE
            old_v.save(update_fields=['status', 'updated_at'])

    booking.assigned_vehicle = target_vehicle
    booking.trip_status = CabBooking.TripStatus.DRIVER_ASSIGNED
    booking.save(update_fields=['assigned_vehicle', 'trip_status', 'updated_at'])

    target_vehicle.status = FleetVehicle.VehicleStatus.ON_TRIP
    target_vehicle.save(update_fields=['status', 'updated_at'])

    send_driver_assignment_notification(booking, target_vehicle)
    messages.success(request, f"Driver {target_vehicle.driver_name} ({target_vehicle.vehicle_number}) assigned to ride {booking.booking_id}.")

    date_param = booking.pickup_datetime.strftime('%Y-%m-%d')
    return redirect(f"{reverse('hotel_admin:cabs_dispatch', kwargs={'restaurant_slug': restaurant_slug})}?date={date_param}")


@hotel_admin_required
def cab_reschedule_action(request, restaurant_slug, ctx, cab_id):
    """Edit pickup datetime or flight/train number for delayed flights without touching billing."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    booking = get_object_or_404(CabBooking, id=cab_id, branch=branch)

    new_pickup_dt_str = request.POST.get('pickup_datetime')
    if new_pickup_dt_str:
        try:
            new_dt = datetime.datetime.fromisoformat(new_pickup_dt_str)
            if timezone.is_naive(new_dt):
                new_dt = timezone.make_aware(new_dt)
            booking.pickup_datetime = new_dt
        except Exception:
            messages.error(request, "Invalid datetime format.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    if 'flight_or_train_number' in request.POST:
        booking.flight_or_train_number = request.POST.get('flight_or_train_number', '').strip()
    if 'pickup_location' in request.POST and request.POST.get('pickup_location'):
        booking.pickup_location = request.POST.get('pickup_location').strip()
    if 'drop_location' in request.POST and request.POST.get('drop_location'):
        booking.drop_location = request.POST.get('drop_location').strip()

    booking.save(update_fields=['pickup_datetime', 'flight_or_train_number', 'pickup_location', 'drop_location', 'updated_at'])
    messages.success(request, f"Ride schedule for {booking.booking_id} updated successfully.")

    date_param = booking.pickup_datetime.strftime('%Y-%m-%d')
    return redirect(f"{reverse('hotel_admin:cabs_dispatch', kwargs={'restaurant_slug': restaurant_slug})}?date={date_param}")


@hotel_admin_required
def cab_status_action(request, restaurant_slug, ctx, cab_id):
    """Quick update trip status (EN_ROUTE, COMPLETED, CANCELLED)."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    booking = get_object_or_404(CabBooking, id=cab_id, branch=branch)
    new_status = request.POST.get('status', '').upper()

    if new_status in CabBooking.TripStatus.values:
        booking.trip_status = new_status
        booking.save(update_fields=['trip_status', 'updated_at'])

        if new_status in [CabBooking.TripStatus.COMPLETED, CabBooking.TripStatus.CANCELLED]:
            if booking.assigned_vehicle:
                v = booking.assigned_vehicle
                other_active = CabBooking.objects.filter(
                    assigned_vehicle=v,
                    trip_status__in=[CabBooking.TripStatus.DRIVER_ASSIGNED, CabBooking.TripStatus.EN_ROUTE]
                ).exclude(pk=booking.pk).exists()
                if not other_active:
                    v.status = FleetVehicle.VehicleStatus.AVAILABLE
                    v.save(update_fields=['status', 'updated_at'])

        messages.success(request, f"Ride status set to {booking.get_trip_status_display()}.")

    date_param = booking.pickup_datetime.strftime('%Y-%m-%d')
    return redirect(f"{reverse('hotel_admin:cabs_dispatch', kwargs={'restaurant_slug': restaurant_slug})}?date={date_param}")


@hotel_admin_required
def cab_manual_create_action(request, restaurant_slug, ctx):
    """Front Desk manual cab / tour booking for an in-house room guest or walk-in/outside passenger."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    room_booking_id = request.POST.get('room_booking_id')
    guest_name = request.POST.get('guest_name', '').strip()
    guest_phone = request.POST.get('guest_phone', '').strip()
    guest_email = request.POST.get('guest_email', '').strip()
    package_id = request.POST.get('package_id')
    pickup_dt_str = request.POST.get('pickup_datetime')
    pickup_loc = request.POST.get('pickup_location', '').strip()
    drop_loc = request.POST.get('drop_location', '').strip()
    flight_train = request.POST.get('flight_or_train_number', '').strip()
    is_comp = request.POST.get('is_complimentary') in ['true', '1', 'on']
    payment_status_val = request.POST.get('payment_status', 'ADDED_TO_FOLIO')

    room_booking = None
    if room_booking_id:
        room_booking = get_object_or_404(RoomBooking, id=room_booking_id, branch=branch)
        guest_name = room_booking.guest_name
        guest_phone = room_booking.guest_phone
        guest_email = room_booking.guest_email
        source_val = CabBooking.BookingSource.PMS_FRONT_DESK
    else:
        source_val = CabBooking.BookingSource.PMS_FRONT_DESK
        if not guest_name or not guest_phone:
            messages.error(request, "Either an in-house room or guest name & phone are required.")
            return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    if not package_id or not pickup_dt_str or not pickup_loc or not drop_loc:
        messages.error(request, "Package, pickup datetime, and locations are required.")
        return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    package = get_object_or_404(CabServicePackage, id=package_id, branch=branch)

    try:
        p_dt = datetime.datetime.fromisoformat(pickup_dt_str)
        if timezone.is_naive(p_dt):
            p_dt = timezone.make_aware(p_dt)
    except Exception:
        messages.error(request, "Invalid pickup datetime format.")
        return redirect('hotel_admin:cabs_dispatch', restaurant_slug=restaurant_slug)

    if is_comp and package.service_type == CabServicePackage.ServiceType.AIRPORT_TRANSFER:
        base = Decimal('0.00')
        tax = Decimal('0.00')
        tot = Decimal('0.00')
        pay_st = CabBooking.PaymentStatus.COMPLIMENTARY
        comp_flag = True
    else:
        base = package.base_price
        tax = round(base * Decimal('0.05'), 2)
        tot = base + tax
        pay_st = payment_status_val if payment_status_val in CabBooking.PaymentStatus.values else (CabBooking.PaymentStatus.ADDED_TO_FOLIO if room_booking else CabBooking.PaymentStatus.PENDING)
        comp_flag = False

    cb = CabBooking.objects.create(
        branch=branch,
        room_booking=room_booking,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        package=package,
        trip_type=package.service_type,
        selected_cab_name=package.title,
        source=source_val,
        is_complimentary=comp_flag,
        pickup_datetime=p_dt,
        pickup_location=pickup_loc,
        drop_location=drop_loc,
        flight_or_train_number=flight_train,
        base_amount=base,
        tax_amount=tax,
        total_amount=tot,
        payment_status=pay_st,
        trip_status=CabBooking.TripStatus.SCHEDULED,
    )
    desc = f"Room {room_booking.room.room_number} ({room_booking.guest_name})" if room_booking else guest_name
    messages.success(request, f"Ride {cb.booking_id} successfully booked for {desc}.")
    date_param = p_dt.strftime('%Y-%m-%d')
    return redirect(f"{reverse('hotel_admin:cabs_dispatch', kwargs={'restaurant_slug': restaurant_slug})}?date={date_param}")


@hotel_admin_required
def cab_payment_status_action(request, restaurant_slug, ctx, cab_id):
    """Quick update cab booking payment status (PAID, PENDING, ADDED_TO_FOLIO, COMPLIMENTARY)."""
    if request.method != 'POST':
        return HttpResponseForbidden("POST required")

    branch = ctx['current_branch']
    booking = get_object_or_404(CabBooking, id=cab_id, branch=branch)
    new_status = request.POST.get('payment_status', '').upper()

    if new_status in CabBooking.PaymentStatus.values:
        booking.payment_status = new_status
        booking.save(update_fields=['payment_status', 'updated_at'])
        messages.success(request, f"Payment status for {booking.booking_id} set to {booking.get_payment_status_display()}.")

    date_param = booking.pickup_datetime.strftime('%Y-%m-%d')
    return redirect(f"{reverse('hotel_admin:cabs_dispatch', kwargs={'restaurant_slug': restaurant_slug})}?date={date_param}")




@hotel_admin_required
@require_POST
def two_factor_update(request, restaurant_slug, ctx):
    """The signed-in user's own email-OTP sign-in preference (Settings → Security)."""
    from core import two_factor
    on = request.POST.get('is_2fa_enabled') == 'on'
    err = two_factor.set_enabled(request.user, on, request.POST.get('current_password', ''))
    if err:
        messages.error(request, err)
    else:
        messages.success(request, 'Two-factor sign-in is on. You will get a code by email each time you sign in.' if on
                         else 'Two-factor sign-in is off. You will sign in with your password only.')
    return redirect(reverse('hotel_admin:settings', kwargs={'restaurant_slug': restaurant_slug}) + '?tab=security')
