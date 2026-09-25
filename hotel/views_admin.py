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
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.contrib.auth.models import User, Group

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from hotel.models import RoomCategory, Room, RoomBooking, HotelSettings, RoomCategoryImage
from hotel.forms import HotelSettingsForm


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

    available_branches = restaurant.branches.all()
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
    all_rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category') if branch else []
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
    categories = RoomCategory.objects.filter(branch=branch).prefetch_related('rooms', 'images').order_by('name') if branch else []

    context = {
        **ctx,
        'active_menu': 'categories',
        'categories': categories,
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

    context = {
        **ctx,
        'active_menu': 'bookings',
        'bookings': bookings,
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
    """
    Check-In guest: Flips booking to CHECKED_IN and associated room to OCCUPIED.
    """
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)

    if booking.booking_status == RoomBooking.BookingStatus.CANCELLED:
        messages.error(request, "Cannot check in a cancelled reservation.")
    else:
        booking.booking_status = RoomBooking.BookingStatus.CHECKED_IN
        booking.check_in_time = timezone.now()
        booking.save(update_fields=['booking_status', 'check_in_time', 'updated_at'])

        room = booking.room
        room.status = Room.RoomStatus.OCCUPIED
        room.save(update_fields=['status', 'updated_at'])

        messages.success(request, f"Guest {booking.guest_name} successfully checked into Room {room.room_number}.")

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def booking_check_out_action(request, restaurant_slug, ctx, booking_id):
    """
    Check-Out guest: Flips booking to CHECKED_OUT and associated room to CLEANING.
    """
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)

    booking.booking_status = RoomBooking.BookingStatus.CHECKED_OUT
    booking.check_out_time = timezone.now()
    booking.save(update_fields=['booking_status', 'check_out_time', 'updated_at'])

    room = booking.room
    room.status = Room.RoomStatus.CLEANING
    room.save(update_fields=['status', 'updated_at'])

    messages.success(request, f"Guest {booking.guest_name} checked out. Room {room.room_number} marked for Housekeeping/Cleaning.")

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


@hotel_admin_required
def booking_cancel_action(request, restaurant_slug, ctx, booking_id):
    """
    Cancel booking and release room back to AVAILABLE if occupied by this booking.
    """
    branch = ctx['current_branch']
    booking = get_object_or_404(RoomBooking, id=booking_id, branch=branch)

    booking.booking_status = RoomBooking.BookingStatus.CANCELLED
    booking.save(update_fields=['booking_status', 'updated_at'])

    room = booking.room
    other_active = RoomBooking.objects.filter(
        room=room,
        booking_status=RoomBooking.BookingStatus.CHECKED_IN
    ).exclude(pk=booking.pk).exists()

    if not other_active and room.status == Room.RoomStatus.OCCUPIED:
        room.status = Room.RoomStatus.AVAILABLE
        room.save(update_fields=['status', 'updated_at'])

    messages.info(request, f"Reservation {booking.booking_id} has been cancelled.")

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
        room.status = new_status
        room.save(update_fields=['status', 'updated_at'])
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

    return redirect(reverse('hotel_admin:rooms', kwargs={'restaurant_slug': restaurant_slug}))


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
            )
            category.save()

        # Handle Cover Image with WebP conversion
        if 'cover_image' in request.FILES:
            converted_cover = optimize_image_to_webp(request.FILES['cover_image'])
            category.cover_image = converted_cover
        if cover_image_url:
            category.cover_image_url = cover_image_url

        # Existing media count
        current_images_count = category.images.count()
        current_videos = list(category.video_links) if isinstance(category.video_links, list) else []
        current_media_count = current_images_count + len(current_videos) + (1 if category.cover_image or category.cover_image_url else 0)

        # Process uploaded video files (Controller: max 25MB, mp4/webm/mov)
        uploaded_video_files = request.FILES.getlist('uploaded_videos')
        for v_file in uploaded_video_files:
            if current_media_count >= MAX_TOTAL_MEDIA:
                messages.warning(request, f"Media limit reached (max {MAX_TOTAL_MEDIA} items). Some videos were skipped.")
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

        # Parse external video URLs (YouTube, Vimeo, MP4 CDN)
        video_json_raw = request.POST.get('video_links_json', '').strip()
        if video_json_raw:
            try:
                parsed_vids = json.loads(video_json_raw)
                if isinstance(parsed_vids, list):
                    for v in parsed_vids:
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

        # Also parse individual video_url_1 .. 5 inputs
        for i in range(1, 6):
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

        category.video_links = current_videos[:MAX_TOTAL_MEDIA]

        # External Gallery URLs
        gallery_urls_raw = request.POST.get('gallery_image_urls', '').strip()
        if gallery_urls_raw:
            raw_lines = [u.strip() for u in gallery_urls_raw.replace('\r\n', '\n').split('\n') if u.strip()]
            for line in raw_lines:
                for part in line.split(','):
                    if current_media_count >= MAX_TOTAL_MEDIA:
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

        # Process multiple uploaded gallery photos (Controller: Auto WebP conversion, max 8MB)
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

        if cat_id:
            messages.success(request, f"Category '{category.name}' updated successfully with optimized media assets.")
        else:
            messages.success(request, f"Category '{category.name}' created successfully with optimized media assets.")

    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_set_cover_action(request, restaurant_slug, image_id, ctx):
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
def category_delete_video_action(request, restaurant_slug, category_id, video_idx, ctx):
    """
    Media Controller: Remove a video from category's video_links by index.
    """
    branch = ctx['current_branch']
    category = get_object_or_404(RoomCategory, id=category_id, branch=branch)
    videos = list(category.video_links) if isinstance(category.video_links, list) else []

    if 0 <= video_idx < len(videos):
        removed = videos.pop(video_idx)
        category.video_links = videos
        category.save(update_fields=['video_links', 'updated_at'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'remaining_videos_count': len(videos)})

    messages.success(request, "Video removed from category.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def category_delete_image_action(request, restaurant_slug, image_id, ctx):
    """
    Remove an individual gallery photo from a category.
    Supports AJAX or normal POST.
    """
    branch = ctx['current_branch']
    img = get_object_or_404(RoomCategoryImage, id=image_id, category__branch=branch)
    cat_name = img.category.name
    img.delete()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax'):
        return JsonResponse({'status': 'ok', 'deleted_id': image_id})

    messages.success(request, f"Photo removed from '{cat_name}'.")
    return redirect(reverse('hotel_admin:categories', kwargs={'restaurant_slug': restaurant_slug}))


@hotel_admin_required
def booking_create_action(request, restaurant_slug, ctx):
    """
    Create a new reservation from the front desk with date overlap validation.
    """
    branch = ctx['current_branch']
    if request.method == 'POST':
        room_id = request.POST.get('room_id')
        guest_name = request.POST.get('guest_name', '').strip()
        guest_email = request.POST.get('guest_email', '').strip()
        guest_phone = request.POST.get('guest_phone', '').strip()
        check_in_str = request.POST.get('check_in_date')
        check_out_str = request.POST.get('check_out_date')
        num_guests = int(request.POST.get('num_guests', 1))
        payment_status = request.POST.get('payment_status', RoomBooking.PaymentStatus.PENDING)
        special_requests = request.POST.get('special_requests', '').strip()

        if not room_id or not guest_name or not check_in_str or not check_out_str:
            messages.error(request, "Room, guest name, and check-in/out dates are required.")
            return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug}))

        room = get_object_or_404(Room, id=room_id, branch=branch)

        try:
            check_in_date = datetime.date.fromisoformat(check_in_str)
            check_out_date = datetime.date.fromisoformat(check_out_str)
        except ValueError:
            messages.error(request, "Invalid dates provided. Format must be YYYY-MM-DD.")
            return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug}))

        if check_out_date <= check_in_date:
            messages.error(request, "Check-out date must be strictly after check-in date.")
            return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug}))

        # Overlap Validation
        conflict = RoomBooking.objects.filter(
            room=room,
            booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
            check_in_date__lt=check_out_date,
            check_out_date__gt=check_in_date
        ).first()

        if conflict:
            messages.error(
                request,
                f"Room {room.room_number} is already booked from {conflict.check_in_date} to {conflict.check_out_date} "
                f"by {conflict.guest_name}. Please choose another room or date."
            )
            return redirect(request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug}))

        booking = RoomBooking.objects.create(
            room=room,
            branch=branch,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_guests=num_guests,
            booking_status=RoomBooking.BookingStatus.RESERVED,
            payment_status=payment_status if payment_status in RoomBooking.PaymentStatus.values else RoomBooking.PaymentStatus.PENDING,
            special_requests=special_requests
        )

        messages.success(request, f"Reservation confirmed! Booking ID: {booking.booking_id} for Room {room.room_number}.")

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('hotel_admin:bookings', kwargs={'restaurant_slug': restaurant_slug})
    return redirect(next_url)


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

    return render(request, 'hotel_admin/settings.html', {
        **ctx,
        'active_menu': 'settings',
        'settings_obj': settings_obj,
        'form': form,
    })


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
