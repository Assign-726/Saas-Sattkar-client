import datetime
from decimal import Decimal
from django.conf import settings
from django.db.models import Q, Count
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth import authenticate
from rest_framework import status, viewsets, permissions, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from super_admin.models import RestaurantBranch, Restaurant
from hotel.models import (
    RoomCategory,
    Room,
    RoomBooking,
    HotelSettings,
    CabServicePackage,
    FleetVehicle,
    CabBooking,
)
from hotel.serializers import (
    RoomCategorySerializer,
    RoomSerializer,
    RoomBookingSerializer,
    PublicAvailabilityCategorySerializer,
    CabServicePackageSerializer,
    FleetVehicleSerializer,
    CabBookingSerializer,
    PublicHotelCheckoutSerializer,
)
from hotel.permissions import IsHotelAdminOrManager, IsActiveHotelAdmin



def resolve_branch_for_request(request):
    """
    Helper to resolve the active branch for staff or public requests.
    """
    branch = None
    branch_id = (
        request.query_params.get('branch_id') or
        request.query_params.get('branch') or
        (request.data.get('branch_id') if hasattr(request, 'data') and isinstance(request.data, dict) else None) or
        (request.data.get('branch') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
    )
    if branch_id:
        try:
            branch = RestaurantBranch.objects.filter(id=branch_id).first()
        except Exception:
            pass

    branch_slug = (
        request.query_params.get('branch_slug') or
        (request.data.get('branch_slug') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
    )
    if not branch and branch_slug:
        branch = RestaurantBranch.objects.filter(slug=branch_slug).first()

    if not branch and request.user and request.user.is_authenticated:
        branch = getattr(request.user, 'branch', None)
        if not branch and hasattr(request.user, 'restaurant_profile'):
            profile = request.user.restaurant_profile
            branch = profile.branch or (profile.restaurant.branches.filter(has_hotel_module=True).first() if profile.restaurant else None)

    if not branch:
        # Fallback to first branch with hotel module or first branch overall
        branch = RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()

    return branch


class RoomCategoryViewSet(viewsets.ModelViewSet):
    """
    API for managing Room Categories (Executive Suite, Deluxe Room, etc.)
    Gated by IsHotelAdminOrManager.
    """
    serializer_class = RoomCategorySerializer
    permission_classes = [IsHotelAdminOrManager]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            return RoomCategory.objects.all()
        if branch:
            return RoomCategory.objects.filter(branch=branch)
        return RoomCategory.objects.none()

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)


class RoomViewSet(viewsets.ModelViewSet):
    """
    API for managing Physical Rooms (Room 101, 102A) and their statuses.
    Gated by IsHotelAdminOrManager.
    """
    serializer_class = RoomSerializer
    permission_classes = [IsHotelAdminOrManager]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        qs = Room.objects.select_related('category', 'branch')
        if not (self.request.user.is_superuser and not self.request.query_params.get('branch_id')):
            if branch:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.none()

        # Optional category filter
        cat_id = self.request.query_params.get('category_id')
        if cat_id:
            qs = qs.filter(category_id=cat_id)

        # Optional status filter
        room_status = self.request.query_params.get('status')
        if room_status:
            qs = qs.filter(status=room_status.upper())

        return qs.order_by('room_number')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        category = serializer.validated_data.get('category')
        if not branch and category:
            branch = category.branch
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)


class RoomStatusUpdateView(APIView):
    """
    PATCH /api/hotel/rooms/{id}/status/
    Quickly update a room's physical status (AVAILABLE, OCCUPIED, CLEANING, MAINTENANCE)
    e.g. Front desk clicks 'Mark Clean' on dirty rooms.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        room = get_object_or_404(Room, pk=pk)
        new_status = request.data.get('status', '').upper()
        if new_status not in Room.RoomStatus.values:
            return Response(
                {"error": f"Invalid status '{new_status}'. Choices are: {Room.RoomStatus.values}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        room.status = new_status
        if 'notes' in request.data:
            room.notes = request.data.get('notes', '')
        room.save(update_fields=['status', 'notes', 'updated_at'])
        return Response({
            "success": True,
            "room_id": room.id,
            "room_number": room.room_number,
            "status": room.status,
            "status_display": room.get_status_display()
        })


class RoomBookingViewSet(viewsets.ModelViewSet):
    """
    API for managing Room Bookings and reservations.
    Gated by IsHotelAdminOrManager.
    """
    serializer_class = RoomBookingSerializer
    permission_classes = [IsHotelAdminOrManager]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        qs = RoomBooking.objects.select_related('room', 'room__category', 'branch')
        if not (self.request.user.is_superuser and not self.request.query_params.get('branch_id')):
            if branch:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.none()

        # Filters
        b_status = self.request.query_params.get('booking_status')
        if b_status:
            qs = qs.filter(booking_status=b_status.upper())

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(guest_name__icontains=search) |
                Q(guest_phone__icontains=search) |
                Q(guest_email__icontains=search) |
                Q(booking_id__icontains=search) |
                Q(room__room_number__icontains=search)
            )

        date_filter = self.request.query_params.get('date')
        if date_filter:
            qs = qs.filter(check_in_date__lte=date_filter, check_out_date__gte=date_filter)

        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        room = serializer.validated_data.get('room')
        branch = room.branch if room else resolve_branch_for_request(self.request)
        serializer.save(branch=branch)


class BookingCheckInView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/check-in/
    Updates booking status to 'CHECKED_IN' and alters the associated Room status to 'OCCUPIED'.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        booking = get_object_or_404(RoomBooking, pk=pk)
        if booking.booking_status == RoomBooking.BookingStatus.CANCELLED:
            return Response(
                {"error": "Cannot check in a cancelled booking."},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking.booking_status = RoomBooking.BookingStatus.CHECKED_IN
        booking.check_in_time = timezone.now()
        booking.save(update_fields=['booking_status', 'check_in_time', 'updated_at'])

        # Alter associated Room status to Occupied
        room = booking.room
        room.status = Room.RoomStatus.OCCUPIED
        room.save(update_fields=['status', 'updated_at'])

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": booking.booking_status,
            "room_number": room.room_number,
            "room_status": room.status,
            "message": f"Guest {booking.guest_name} successfully checked into Room {room.room_number}."
        })


class BookingCheckOutView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/check-out/
    Updates booking status to 'CHECKED_OUT' and alters the associated Room status to 'CLEANING'.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        booking = get_object_or_404(RoomBooking, pk=pk)
        if booking.booking_status != RoomBooking.BookingStatus.CHECKED_IN:
            # Allow checkout if reserved or already in-progress
            pass

        booking.booking_status = RoomBooking.BookingStatus.CHECKED_OUT
        booking.check_out_time = timezone.now()
        booking.save(update_fields=['booking_status', 'check_out_time', 'updated_at'])

        # Alter associated Room status to Cleaning
        room = booking.room
        room.status = Room.RoomStatus.CLEANING
        room.save(update_fields=['status', 'updated_at'])

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": booking.booking_status,
            "room_number": room.room_number,
            "room_status": room.status,
            "message": f"Guest {booking.guest_name} checked out. Room {room.room_number} marked for Cleaning."
        })


class BookingCancelView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/cancel/
    Cancels a booking and resets room status if occupied by this booking.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        booking = get_object_or_404(RoomBooking, pk=pk)
        booking.booking_status = RoomBooking.BookingStatus.CANCELLED
        booking.save(update_fields=['booking_status', 'updated_at'])

        # Check if room needs status reset
        room = booking.room
        active_other = RoomBooking.objects.filter(
            room=room,
            booking_status=RoomBooking.BookingStatus.CHECKED_IN
        ).exclude(pk=booking.pk).exists()

        if not active_other and room.status == Room.RoomStatus.OCCUPIED:
            room.status = Room.RoomStatus.AVAILABLE
            room.save(update_fields=['status', 'updated_at'])

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": booking.booking_status,
            "message": "Booking successfully cancelled."
        })


class HotelTapeChartView(APIView):
    """
    GET /api/hotel/tape-chart/?start_date=YYYY-MM-DD&days=14
    Provides data structured for the Front Desk Tape Chart timeline view:
    - List of physical rooms (grouped by category)
    - All reservations overlapping the timeframe
    """
    permission_classes = [IsHotelAdminOrManager]

    def get(self, request):
        branch = resolve_branch_for_request(request)
        if not branch:
            return Response({"error": "Branch required"}, status=400)

        start_str = request.query_params.get('start_date')
        if start_str:
            try:
                start_date = datetime.date.fromisoformat(start_str)
            except ValueError:
                start_date = timezone.localdate()
        else:
            start_date = timezone.localdate()

        try:
            days = int(request.query_params.get('days', 14))
        except (ValueError, TypeError):
            days = 14
        days = max(1, min(days, 31))

        end_date = start_date + datetime.timedelta(days=days)

        rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('room_number')
        bookings = RoomBooking.objects.filter(
            branch=branch,
            booking_status__in=[
                RoomBooking.BookingStatus.RESERVED,
                RoomBooking.BookingStatus.CHECKED_IN,
                RoomBooking.BookingStatus.CHECKED_OUT,
            ],
            check_in_date__lt=end_date,
            check_out_date__gt=start_date
        ).select_related('room', 'room__category')

        # Structure response
        dates_list = [(start_date + datetime.timedelta(days=i)).isoformat() for i in range(days)]

        rooms_data = []
        for r in rooms:
            room_bookings = [
                {
                    "id": b.id,
                    "booking_id": b.booking_id,
                    "guest_name": b.guest_name,
                    "guest_phone": b.guest_phone,
                    "check_in_date": b.check_in_date.isoformat(),
                    "check_out_date": b.check_out_date.isoformat(),
                    "booking_status": b.booking_status,
                    "payment_status": b.payment_status,
                    "grand_total": str(b.grand_total),
                }
                for b in bookings if b.room_id == r.id
            ]
            rooms_data.append({
                "id": r.id,
                "room_number": r.room_number,
                "floor": r.floor,
                "status": r.status,
                "status_display": r.get_status_display(),
                "category_id": r.category.id,
                "category_name": r.category.name,
                "base_price": str(r.category.base_price_per_night),
                "bookings": room_bookings,
            })

        return Response({
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days_count": days,
            "dates": dates_list,
            "rooms": rooms_data,
        })


class HotelStatsView(APIView):
    """
    GET /api/hotel/stats/
    Quick front-desk metrics for arrivals, departures, occupancy, and room statuses.
    """
    permission_classes = [IsHotelAdminOrManager]

    def get(self, request):
        branch = resolve_branch_for_request(request)
        if not branch:
            return Response({"error": "Branch not found"}, status=400)

        today = timezone.localdate()

        rooms = Room.objects.filter(branch=branch, is_active=True)
        total_rooms = rooms.count()
        available_rooms = rooms.filter(status=Room.RoomStatus.AVAILABLE).count()
        occupied_rooms = rooms.filter(status=Room.RoomStatus.OCCUPIED).count()
        cleaning_rooms = rooms.filter(status=Room.RoomStatus.CLEANING).count()
        maintenance_rooms = rooms.filter(status=Room.RoomStatus.MAINTENANCE).count()

        arrivals_today = RoomBooking.objects.filter(
            branch=branch,
            check_in_date=today,
            booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN]
        ).count()

        departures_today = RoomBooking.objects.filter(
            branch=branch,
            check_out_date=today,
            booking_status__in=[RoomBooking.BookingStatus.CHECKED_IN, RoomBooking.BookingStatus.CHECKED_OUT]
        ).count()

        occupancy_rate = round((occupied_rooms / total_rooms * 100), 1) if total_rooms > 0 else 0

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "date": today.isoformat(),
            "total_rooms": total_rooms,
            "available_rooms": available_rooms,
            "occupied_rooms": occupied_rooms,
            "cleaning_rooms": cleaning_rooms,
            "maintenance_rooms": maintenance_rooms,
            "arrivals_today": arrivals_today,
            "departures_today": departures_today,
            "occupancy_rate": occupancy_rate,
        })


class HotelAuthLoginView(APIView):
    """
    POST /api/hotel/auth/login/
    Authenticates staff, returns JWT tokens and user role:
    - GENERAL_MANAGER
    - HOTEL_ADMIN
    - RESTAURANT_ADMIN
    - CLUB_ADMIN
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')

        if not username or not password:
            return Response({"error": "Username and password are required."}, status=400)

        user = authenticate(request, username=username, password=password)
        if not user:
            # Fallback check for case-insensitive or email login
            from django.contrib.auth.models import User
            candidate = User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).first()
            if candidate and candidate.check_password(password):
                user = candidate

        if not user or not user.is_active:
            return Response({"error": "Invalid username or password."}, status=401)

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)

        # Resolve role
        role = getattr(user, 'role', None)
        if not role:
            if user.is_superuser:
                role = 'GENERAL_MANAGER'
            elif hasattr(user, 'restaurant_profile'):
                role = user.restaurant_profile.role
            else:
                role = 'RESTAURANT_ADMIN'

        # Resolve branch and capabilities
        branch_obj = getattr(user, 'branch', None)
        profile = getattr(user, 'restaurant_profile', None)
        restaurant_obj = profile.restaurant if profile else None

        if not branch_obj and restaurant_obj:
            branch_obj = restaurant_obj.branches.filter(has_hotel_module=True).first() or restaurant_obj.branches.first()

        branch_data = None
        if branch_obj:
            branch_data = {
                "id": branch_obj.id,
                "name": branch_obj.name,
                "slug": branch_obj.slug,
                "has_hotel_module": getattr(branch_obj, 'has_hotel_module', False),
                "has_club_module": getattr(branch_obj, 'has_club_module', False),
            }

        restaurant_data = None
        if restaurant_obj:
            restaurant_data = {
                "id": restaurant_obj.id,
                "name": restaurant_obj.name,
                "slug": restaurant_obj.slug,
            }

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": role,
                "is_superuser": user.is_superuser,
            },
            "branch": branch_data,
            "restaurant": restaurant_data,
        })


class HotelAuthMeView(APIView):
    """
    GET /api/hotel/auth/me/
    Returns current authenticated user details and active role.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        role = getattr(user, 'role', 'RESTAURANT_ADMIN')
        branch_obj = getattr(user, 'branch', None)

        branch_data = None
        if branch_obj:
            branch_data = {
                "id": branch_obj.id,
                "name": branch_obj.name,
                "slug": branch_obj.slug,
                "has_hotel_module": getattr(branch_obj, 'has_hotel_module', False),
                "has_club_module": getattr(branch_obj, 'has_club_module', False),
            }

        profile = getattr(user, 'restaurant_profile', None)
        restaurant_data = None
        if profile and profile.restaurant:
            restaurant_data = {
                "id": profile.restaurant.id,
                "name": profile.restaurant.name,
                "slug": profile.restaurant.slug,
            }

        return Response({
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": role,
                "is_superuser": user.is_superuser,
            },
            "branch": branch_data,
            "restaurant": restaurant_data,
        })


class PublicBranchAvailabilityView(APIView):
    """
    GET /api/public/branches/{id}/availability/?start=YYYY-MM-DD&end=YYYY-MM-DD
    Public Availability Engine:
    The backend queries RoomCategory for the branch and filters out categories
    where all associated rooms are locked in RoomBooking for those dates.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, branch_id=None):
        from restaurant_project import constants
        if not getattr(constants, 'ENABLE_HOTEL_MODULE', True):
            return Response({"error": "Hotel & Rooms PMS module is globally disabled in system configuration."}, status=403)

        # Resolve branch by ID or query param
        branch = None
        if branch_id:
            branch = RestaurantBranch.objects.filter(id=branch_id).first()
        if not branch:
            b_val = request.query_params.get('branch_slug') or request.query_params.get('branch') or request.query_params.get('branch_id')
            if b_val:
                if str(b_val).isdigit():
                    branch = RestaurantBranch.objects.filter(id=int(b_val)).first()
                else:
                    branch = RestaurantBranch.objects.filter(slug=b_val).first()
        if not branch:
            r_slug = request.query_params.get('restaurant_slug')
            if r_slug:
                rest = Restaurant.objects.filter(slug=r_slug).first()
                if rest:
                    branch = rest.branches.filter(has_hotel_module=True).first() or rest.branches.first()

        if not branch:
            # Fallback to the first branch that has the hotel module enabled
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()

        if not branch:
            return Response({"error": "Branch not found or not specified."}, status=404)

        if not getattr(branch, 'has_hotel_module', False):
            return Response({
                "branch_id": branch.id,
                "branch_name": branch.name,
                "has_hotel_module": False,
                "categories": [],
                "message": "Hotel module is not active for this branch."
            })

        start_str = request.query_params.get('start')
        end_str = request.query_params.get('end')

        if not start_str or not end_str:
            today = timezone.localdate()
            start_date = today
            end_date = today + datetime.timedelta(days=1)
        else:
            try:
                start_date = datetime.date.fromisoformat(start_str)
                end_date = datetime.date.fromisoformat(end_str)
            except ValueError:
                return Response({"error": "Dates must be in YYYY-MM-DD format."}, status=400)

        if end_date <= start_date:
            return Response({"error": "Check-out date must be strictly after check-in date."}, status=400)

        nights = max(1, (end_date - start_date).days)

        # Find all active room categories for this branch
        categories = RoomCategory.objects.filter(branch=branch, is_active=True).prefetch_related('rooms')

        # Read HotelSettings for dynamic GST or custom tax policy
        h_settings = getattr(branch, 'hotel_settings', None) or HotelSettings.get_settings(branch)

        available_categories = []
        for cat in categories:
            cat_rooms = cat.rooms.filter(is_active=True)
            if not cat_rooms.exists():
                continue

            # Query rooms locked in RoomBooking for these dates
            booked_room_ids = RoomBooking.objects.filter(
                room__in=cat_rooms,
                booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
                check_in_date__lt=end_date,
                check_out_date__gt=start_date,
            ).values_list('room_id', flat=True)

            available_rooms = cat_rooms.exclude(id__in=booked_room_ids)
            avail_count = available_rooms.count()

            # Filter out categories where all associated rooms are locked
            if avail_count > 0:
                base_rate = cat.base_price_per_night
                room_charges = base_rate * Decimal(nights)

                # Determine tax rate: category custom tax override or branch HotelSettings
                if cat.custom_tax_rate is not None:
                    tax_rate = (cat.custom_tax_rate / Decimal('100'))
                elif h_settings and not h_settings.enable_dynamic_gst:
                    tax_rate = (h_settings.tax_rate / Decimal('100'))
                else:
                    tax_rate = Decimal('0.12') if base_rate <= 7500 else Decimal('0.18')

                if cat.price_is_tax_inclusive:
                    # Base price already includes tax
                    tax_divisor = Decimal('1') + tax_rate
                    net_room_charges = round(room_charges / tax_divisor, 2)
                    tax_amount = round(room_charges - net_room_charges, 2)
                    service_charge = Decimal('0.00')
                    if h_settings and h_settings.service_charge_percent > 0:
                        service_charge = round(net_room_charges * (h_settings.service_charge_percent / Decimal('100')), 2)
                    grand_total = room_charges + service_charge
                    display_room_charges = net_room_charges
                else:
                    tax_amount = round(room_charges * tax_rate, 2)
                    service_charge = Decimal('0.00')
                    if h_settings and h_settings.service_charge_percent > 0:
                        service_charge = round(room_charges * (h_settings.service_charge_percent / Decimal('100')), 2)
                    grand_total = room_charges + tax_amount + service_charge
                    display_room_charges = room_charges

                name_lower = cat.name.lower()
                is_non_ac = 'non-ac' in name_lower or 'non ac' in name_lower
                cooling_type = 'Non-AC' if is_non_ac else 'AC'
                
                cat_type = 'deluxe'
                if 'villa' in name_lower:
                    cat_type = 'villa'
                elif 'suite' in name_lower or 'penthouse' in name_lower:
                    cat_type = 'suite'

                is_featured = bool(cat.show_on_banner or base_rate >= Decimal('8000') or 'presidential' in name_lower or 'royal' in name_lower)

                available_categories.append({
                    "id": cat.id,
                    "name": cat.name,
                    "slug": cat.slug,
                    "description": cat.description,
                    "base_price_per_night": str(cat.base_price_per_night),
                    "price_is_tax_inclusive": cat.price_is_tax_inclusive,
                    "original_price_per_night": str(cat.original_price_per_night) if cat.original_price_per_night else None,
                    "discount_percent": cat.discount_percent,
                    "show_on_banner": cat.show_on_banner,
                    "banner_badge_text": cat.banner_badge_text,
                    "max_occupancy": cat.max_occupancy,
                    "max_adults": cat.max_adults,
                    "max_children": cat.max_children,
                    "bed_type": cat.bed_type,
                    "room_size_sqft": cat.room_size_sqft,
                    "cooling_type": cooling_type,
                    "category_type": cat_type,
                    "is_featured": is_featured,
                    "tax_rate_percent": float(round(tax_rate * Decimal('100'), 1)),
                    "custom_tax_rate": str(cat.custom_tax_rate) if cat.custom_tax_rate is not None else None,
                    "amenities": cat.amenities,
                    "effective_image": cat.effective_image_url,
                    "all_images": cat.all_images_list,
                    "video_links": cat.video_links_list,
                    "available_rooms_count": avail_count,
                    "available_room_ids": list(available_rooms.values_list('id', flat=True)),
                    "nights": nights,
                    "estimated_room_charges": str(display_room_charges),
                    "estimated_taxes": str(tax_amount),
                    "estimated_service_charge": str(service_charge),
                    "estimated_grand_total": str(grand_total),
                })

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "has_hotel_module": True,
            "check_in_time": h_settings.check_in_time.strftime("%H:%M") if (h_settings and h_settings.check_in_time) else "14:00",
            "check_out_time": h_settings.check_out_time.strftime("%H:%M") if (h_settings and h_settings.check_out_time) else "11:00",
            "cancellation_policy": h_settings.cancellation_policy if h_settings else "",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "nights": nights,
            "available_categories_count": len(available_categories),
            "categories": available_categories,
            "payment_settings": {
                "enable_online_payment": h_settings.enable_online_payment if h_settings else True,
                "enable_pay_at_desk": h_settings.enable_pay_at_desk if h_settings else True,
                "payment_gateway_provider": h_settings.payment_gateway_provider if h_settings else "RAZORPAY",
                "razorpay_key_id": (h_settings.razorpay_key_id if h_settings and h_settings.razorpay_key_id else "") or getattr(settings, 'RAZORPAY_KEY_ID', ''),
                "upi_id": h_settings.upi_id if h_settings else "",
                "currency_symbol": h_settings.currency_symbol if h_settings else "₹",
                "cancellation_grace_hours": h_settings.cancellation_grace_hours if h_settings else 24,
                "enable_dynamic_gst": h_settings.enable_dynamic_gst if h_settings else True,
                "tax_rate": str(h_settings.tax_rate) if h_settings else "12.00",
                "service_charge_percent": str(h_settings.service_charge_percent) if h_settings else "0.00",
                "gstin": h_settings.gstin if h_settings else "",
            }
        })


class PublicBookingCreateView(APIView):
    """
    POST /api/hotel/public/bookings/ or /api/rooms/bookings/
    Allows guests on the public website to book a room directly.
    Auto-allocates an available room in the chosen category.
    Accepts both frontend-style and backend-style payload field names.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from restaurant_project import constants
        if not getattr(constants, 'ENABLE_HOTEL_MODULE', True):
            return Response({"error": "Hotel & Rooms PMS module is globally disabled in system configuration."}, status=403)

        data = request.data
        category_id = data.get('category_id') or data.get('room_id')
        room_id = data.get('actual_room_id') or (data.get('room_id') if not category_id else None)
        guest_name = (data.get('guest_name') or data.get('customer_name') or '').strip()
        guest_email = (data.get('guest_email') or data.get('email') or '').strip()
        guest_phone = (data.get('guest_phone') or data.get('phone') or '').strip()
        check_in_str = data.get('check_in_date') or data.get('check_in')
        check_out_str = data.get('check_out_date') or data.get('check_out')
        special_requests = data.get('special_requests') or data.get('notes') or ''
        num_guests = int(data.get('num_guests') or data.get('adults') or 1)

        if not guest_name or not guest_phone:
            return Response({"error": "Guest name and phone number are required."}, status=400)

        # Ensure email format is valid or fallback to auto-generated internal guest email
        if not guest_email or '@' not in guest_email:
            clean_digits = ''.join(filter(str.isdigit, guest_phone))[-6:] or 'stay'
            guest_email = f"guest.{clean_digits}@sharmahotel.internal"

        if not check_in_str or not check_out_str:
            return Response({"error": "Check-in and check-out dates are required."}, status=400)

        try:
            check_in_date = datetime.date.fromisoformat(check_in_str)
            check_out_date = datetime.date.fromisoformat(check_out_str)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        if check_out_date <= check_in_date:
            return Response({"error": "Check-out date must be strictly after check-in date."}, status=400)

        # Allocate room
        target_room = None
        target_cat = None

        if category_id:
            target_cat = RoomCategory.objects.filter(id=category_id, is_active=True).first()

        if target_cat:
            cat_rooms = target_cat.rooms.filter(is_active=True)
            booked_room_ids = RoomBooking.objects.filter(
                room__in=cat_rooms,
                booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
                check_in_date__lt=check_out_date,
                check_out_date__gt=check_in_date,
            ).values_list('room_id', flat=True)

            target_room = cat_rooms.exclude(id__in=booked_room_ids).first()

        # If not resolved via category, try directly as physical room ID
        if not target_room and room_id:
            candidate_room = Room.objects.filter(id=room_id, is_active=True).first()
            if candidate_room:
                is_booked = RoomBooking.objects.filter(
                    room=candidate_room,
                    booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
                    check_in_date__lt=check_out_date,
                    check_out_date__gt=check_in_date,
                ).exists()
                if not is_booked:
                    target_room = candidate_room

        # If still not found, check if ANY active category has available rooms
        if not target_room:
            all_cats = RoomCategory.objects.filter(is_active=True).prefetch_related('rooms')
            for fallback_cat in all_cats:
                cat_rooms = fallback_cat.rooms.filter(is_active=True)
                booked_ids = RoomBooking.objects.filter(
                    room__in=cat_rooms,
                    booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN],
                    check_in_date__lt=check_out_date,
                    check_out_date__gt=check_in_date,
                ).values_list('room_id', flat=True)
                avail = cat_rooms.exclude(id__in=booked_ids).first()
                if avail:
                    target_room = avail
                    break

        if not target_room:
            return Response(
                {"error": "No available rooms in the selected category for these dates. Please choose another date or category."},
                status=409
            )

        # Create booking
        booking = RoomBooking.objects.create(
            room=target_room,
            branch=target_room.branch,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_guests=num_guests,
            booking_status=RoomBooking.BookingStatus.RESERVED,
            payment_status=RoomBooking.PaymentStatus.PENDING,
            special_requests=special_requests,
        )

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_reference": booking.booking_id,
            "room_number": target_room.room_number,
            "category_name": target_room.category.name,
            "check_in_date": booking.check_in_date.isoformat(),
            "check_out_date": booking.check_out_date.isoformat(),
            "nights": booking.nights_count,
            "total_room_charges": str(booking.total_room_charges),
            "taxes": str(booking.taxes),
            "grand_total": str(booking.grand_total),
            "total_amount": float(booking.grand_total),
            "booking_status": booking.booking_status,
            "payment_status": booking.payment_status,
            "message": f"Reservation confirmed! Your booking code is {booking.booking_id}."
        }, status=201)


# ==============================================================================
# TRAVEL DESK & CAB MANAGEMENT CONTROLLERS
# ==============================================================================

def send_driver_assignment_notification(cab_booking, vehicle):
    """
    Sends dispatch notification to guest with driver & vehicle details.
    Uses hotel custom SMTP or platform default email.
    """
    import logging
    logger = logging.getLogger(__name__)

    guest_email = cab_booking.room_booking.guest_email if cab_booking.room_booking else ""
    guest_name = cab_booking.room_booking.guest_name if cab_booking.room_booking else "Valued Guest"
    guest_phone = cab_booking.room_booking.guest_phone if cab_booking.room_booking else ""

    pickup_time_str = cab_booking.pickup_datetime.strftime("%d %b %Y at %I:%M %p") if cab_booking.pickup_datetime else ""
    subject = f"Your Cab Details: Ride to {cab_booking.drop_location}"
    message = (
        f"Dear {guest_name},\n\n"
        f"Your driver has been assigned for your ride scheduled on {pickup_time_str}.\n\n"
        f"Driver Name: {vehicle.driver_name}\n"
        f"Driver Phone: {vehicle.driver_phone}\n"
        f"Vehicle Number: {vehicle.vehicle_number}\n"
        f"Vehicle Type: {vehicle.get_vehicle_category_display()}\n"
        f"Pickup Location: {cab_booking.pickup_location}\n"
        f"Drop Location: {cab_booking.drop_location}\n\n"
        f"Thank you for choosing our Hotel Travel Desk. Have a safe and pleasant journey!"
    )

    if guest_email and '@' in guest_email and not guest_email.endswith('.internal'):
        try:
            h_settings = getattr(cab_booking.branch, 'hotel_settings', None)
            from core.emails import get_mail_connection_and_sender
            from django.core.mail import EmailMultiAlternatives
            sender_name = (h_settings.email_sender_name if h_settings else None) or (h_settings.hotel_name if h_settings else None) or "Hotel Travel Desk"
            conn, from_email, _ = get_mail_connection_and_sender(
                branch=cab_booking.branch,
                hotel_settings=h_settings,
                sender_name_override=sender_name
            )
            msg = EmailMultiAlternatives(subject, message, from_email, [guest_email], connection=conn)
            msg.send(fail_silently=True)
        except Exception as e:
            logger.warning(f"Could not send cab dispatch email to {guest_email}: {e}")

    logger.info(f"Driver {vehicle.driver_name} ({vehicle.vehicle_number}) dispatched for ride {cab_booking.booking_id} to guest {guest_name} ({guest_phone})")


class CabServicePackageViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Cab Service Packages (Airport transfers, tourism sightseeing, etc.).
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = CabServicePackageSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = CabServicePackage.objects.all()
        elif branch:
            qs = CabServicePackage.objects.filter(branch=branch)
        else:
            qs = CabServicePackage.objects.none()

        st = self.request.query_params.get('service_type')
        if st:
            qs = qs.filter(service_type=st.upper())

        active_filter = self.request.query_params.get('is_active')
        if active_filter is not None:
            qs = qs.filter(is_active=active_filter.lower() in ['true', '1'])

        return qs.order_by('title')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)


class FleetVehicleViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Fleet Vehicles & Drivers (In-house and Outsourced vendor cabs).
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = FleetVehicleSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = FleetVehicle.objects.all()
        elif branch:
            qs = FleetVehicle.objects.filter(branch=branch)
        else:
            qs = FleetVehicle.objects.none()

        st = self.request.query_params.get('status')
        if st:
            qs = qs.filter(status=st.upper())

        cat = self.request.query_params.get('category')
        if cat:
            qs = qs.filter(vehicle_category=cat.upper())

        is_vendor = self.request.query_params.get('is_vendor')
        if is_vendor is not None:
            qs = qs.filter(is_vendor_vehicle=is_vendor.lower() in ['true', '1'])

        return qs.order_by('vehicle_number')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)


class CabBookingViewSet(viewsets.ModelViewSet):
    """
    CRUD and Listing API for Cab Bookings.
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = CabBookingSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = CabBooking.objects.all()
        elif branch:
            qs = CabBooking.objects.filter(branch=branch)
        else:
            qs = CabBooking.objects.none()

        date_str = self.request.query_params.get('date')
        if date_str:
            qs = qs.filter(pickup_datetime__date=date_str)

        st = self.request.query_params.get('status')
        if st:
            qs = qs.filter(trip_status=st.upper())

        room_booking_id = self.request.query_params.get('room_booking_id')
        if room_booking_id:
            qs = qs.filter(Q(room_booking__id=room_booking_id) | Q(room_booking__booking_id=room_booking_id))

        return qs.select_related('room_booking', 'room_booking__room', 'package', 'assigned_vehicle').order_by('pickup_datetime')


class CabDispatchBoardView(APIView):
    """
    GET /api/hotel/cabs/dispatch-board/
    Daily Chronological Dispatch Board:
    - ?date=YYYY-MM-DD (defaults to today)
    - ?service_type=AIRPORT_TRANSFER
    - ?status=SCHEDULED
    - ?unassigned_only=true (highlights urgent rides needing a driver)
    Gated by IsActiveHotelAdmin.
    """
    permission_classes = [IsActiveHotelAdmin]

    def get(self, request):
        branch = resolve_branch_for_request(request)
        if not branch:
            return Response({"error": "Branch not found or specified."}, status=400)

        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                target_date = timezone.localdate()
        else:
            target_date = timezone.localdate()

        qs = CabBooking.objects.filter(
            branch=branch,
            pickup_datetime__date=target_date
        ).select_related('room_booking', 'room_booking__room', 'package', 'assigned_vehicle')

        # Compute summary metrics for target date before sub-filtering
        total_rides = qs.count()
        unassigned_rides = qs.filter(
            Q(assigned_vehicle__isnull=True) | Q(trip_status=CabBooking.TripStatus.SCHEDULED)
        ).exclude(trip_status=CabBooking.TripStatus.CANCELLED).count()
        assigned_rides = qs.filter(trip_status=CabBooking.TripStatus.DRIVER_ASSIGNED).count()
        in_transit_rides = qs.filter(trip_status=CabBooking.TripStatus.EN_ROUTE).count()
        completed_rides = qs.filter(trip_status=CabBooking.TripStatus.COMPLETED).count()
        cancelled_rides = qs.filter(trip_status=CabBooking.TripStatus.CANCELLED).count()

        # Apply optional filters
        st = request.query_params.get('service_type')
        if st:
            qs = qs.filter(package__service_type=st.upper())

        trip_status = request.query_params.get('status')
        if trip_status:
            qs = qs.filter(trip_status=trip_status.upper())

        unassigned_only = request.query_params.get('unassigned_only')
        if unassigned_only and unassigned_only.lower() in ['true', '1']:
            qs = qs.filter(
                Q(assigned_vehicle__isnull=True) | Q(trip_status=CabBooking.TripStatus.SCHEDULED)
            ).exclude(trip_status=CabBooking.TripStatus.CANCELLED)

        bookings_data = CabBookingSerializer(qs.order_by('pickup_datetime'), many=True).data

        # Available fleet vehicles for quick assignment
        available_vehicles = FleetVehicle.objects.filter(
            branch=branch,
            status=FleetVehicle.VehicleStatus.AVAILABLE
        ).order_by('vehicle_category', 'vehicle_number')
        fleet_data = FleetVehicleSerializer(available_vehicles, many=True).data

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "date": target_date.isoformat(),
            "summary": {
                "total_rides": total_rides,
                "unassigned_rides": unassigned_rides,
                "assigned_rides": assigned_rides,
                "in_transit_rides": in_transit_rides,
                "completed_rides": completed_rides,
                "cancelled_rides": cancelled_rides,
            },
            "rides_count": len(bookings_data),
            "rides": bookings_data,
            "available_fleet": fleet_data,
        })


class CabBookingManualCreateView(APIView):
    """
    POST /api/hotel/cabs/bookings/manual/
    Allows the Front Desk to book a cab or sightseeing tour for an already
    checked-in or reserved guest, with payment set to ADDED_TO_FOLIO or PAID.
    Gated by IsActiveHotelAdmin.
    """
    permission_classes = [IsActiveHotelAdmin]

    def post(self, request):
        branch = resolve_branch_for_request(request)
        if not branch:
            return Response({"error": "Branch required."}, status=400)

        data = request.data
        room_booking_id = data.get('room_booking_id')
        package_id = data.get('package_id')
        pickup_datetime_str = data.get('pickup_datetime')
        pickup_location = (data.get('pickup_location') or '').strip()
        drop_location = (data.get('drop_location') or '').strip()
        flight_or_train = (data.get('flight_or_train_number') or '').strip()
        guest_notes = data.get('guest_notes', '')
        payment_status_input = (data.get('payment_status') or 'ADDED_TO_FOLIO').upper()
        is_complimentary = bool(data.get('is_complimentary', False))
        assigned_vehicle_id = data.get('assigned_vehicle_id') or data.get('vehicle_id')

        if not room_booking_id or not package_id or not pickup_datetime_str or not pickup_location or not drop_location:
            return Response({
                "error": "room_booking_id, package_id, pickup_datetime, pickup_location, and drop_location are required."
            }, status=400)

        # Lookup RoomBooking
        room_booking = None
        if str(room_booking_id).isdigit():
            room_booking = RoomBooking.objects.filter(id=int(room_booking_id), branch=branch).first()
        if not room_booking:
            room_booking = RoomBooking.objects.filter(booking_id=room_booking_id, branch=branch).first()
        if not room_booking:
            return Response({"error": f"Room booking '{room_booking_id}' not found for this branch."}, status=404)

        # Lookup Package
        package = CabServicePackage.objects.filter(id=package_id, branch=branch, is_active=True).first()
        if not package:
            package = CabServicePackage.objects.filter(id=package_id, is_active=True).first()
        if not package:
            return Response({"error": f"Cab service package '{package_id}' not found or inactive."}, status=404)

        try:
            pickup_dt = datetime.datetime.fromisoformat(pickup_datetime_str)
            if timezone.is_naive(pickup_dt):
                pickup_dt = timezone.make_aware(pickup_dt)
        except Exception:
            return Response({"error": "Invalid pickup_datetime format. Use ISO format YYYY-MM-DDTHH:MM."}, status=400)

        # Complimentary check: only AIRPORT_TRANSFER can be complimentary
        if is_complimentary and package.service_type == CabServicePackage.ServiceType.AIRPORT_TRANSFER:
            base_amount = Decimal('0.00')
            tax_amount = Decimal('0.00')
            total_amount = Decimal('0.00')
            final_payment_status = CabBooking.PaymentStatus.COMPLIMENTARY
            final_is_comp = True
        else:
            base_amount = package.base_price
            tax_amount = round(base_amount * Decimal('0.05'), 2)
            total_amount = base_amount + tax_amount
            final_payment_status = payment_status_input if payment_status_input in CabBooking.PaymentStatus.values else CabBooking.PaymentStatus.ADDED_TO_FOLIO
            final_is_comp = False

        # Optional immediate vehicle assignment
        assigned_vehicle = None
        trip_status = CabBooking.TripStatus.SCHEDULED
        if assigned_vehicle_id:
            assigned_vehicle = FleetVehicle.objects.filter(id=assigned_vehicle_id, branch=branch).first()
            if assigned_vehicle:
                trip_status = CabBooking.TripStatus.DRIVER_ASSIGNED
                assigned_vehicle.status = FleetVehicle.VehicleStatus.ON_TRIP
                assigned_vehicle.save(update_fields=['status', 'updated_at'])

        cab_booking = CabBooking.objects.create(
            branch=branch,
            room_booking=room_booking,
            package=package,
            assigned_vehicle=assigned_vehicle,
            is_complimentary=final_is_comp,
            pickup_datetime=pickup_dt,
            pickup_location=pickup_location,
            drop_location=drop_location,
            flight_or_train_number=flight_or_train,
            base_amount=base_amount,
            tax_amount=tax_amount,
            total_amount=total_amount,
            payment_status=final_payment_status,
            trip_status=trip_status,
            guest_notes=guest_notes,
        )

        if assigned_vehicle:
            send_driver_assignment_notification(cab_booking, assigned_vehicle)

        return Response({
            "success": True,
            "message": "Cab booking successfully created by Front Desk.",
            "cab_booking": CabBookingSerializer(cab_booking).data,
        }, status=status.HTTP_201_CREATED)


class CabBookingAssignDriverView(APIView):
    """
    PATCH /api/hotel/cabs/bookings/{id}/assign-driver/
    Assigns an in-house vehicle OR quick-adds an external vendor cab on the fly.
    Validates availability, updates vehicle status to 'ON_TRIP', sets trip_status = 'DRIVER_ASSIGNED',
    and triggers a guest notification.
    Gated by IsActiveHotelAdmin.
    """
    permission_classes = [IsActiveHotelAdmin]

    def patch(self, request, pk):
        booking = get_object_or_404(CabBooking, pk=pk)

        if booking.trip_status == CabBooking.TripStatus.CANCELLED:
            return Response({"error": "Cannot assign a driver to a cancelled cab booking."}, status=400)

        data = request.data
        vehicle_id = data.get('vehicle_id')
        quick_add = data.get('quick_add_vendor') or (data.get('vehicle_number') and not vehicle_id)

        target_vehicle = None

        if quick_add:
            # Quick Add Vendor Cab directly inside modal
            v_num = (data.get('vehicle_number') or '').strip().upper()
            d_name = (data.get('driver_name') or '').strip()
            d_phone = (data.get('driver_phone') or '').strip()
            v_cat = data.get('vehicle_category') or (booking.package.vehicle_category if booking.package else 'SEDAN')

            if not v_num or not d_name or not d_phone:
                return Response({
                    "error": "vehicle_number, driver_name, and driver_phone are required to quick-add an outsourced cab."
                }, status=400)

            target_vehicle, _ = FleetVehicle.objects.get_or_create(
                branch=booking.branch,
                vehicle_number=v_num,
                defaults={
                    'driver_name': d_name,
                    'driver_phone': d_phone,
                    'vehicle_category': v_cat,
                    'is_vendor_vehicle': True,
                    'status': FleetVehicle.VehicleStatus.AVAILABLE,
                }
            )
            # Update driver details if provided
            target_vehicle.driver_name = d_name
            target_vehicle.driver_phone = d_phone
            target_vehicle.is_vendor_vehicle = True
            target_vehicle.save()
        elif vehicle_id:
            target_vehicle = FleetVehicle.objects.filter(id=vehicle_id, branch=booking.branch).first()
            if not target_vehicle:
                return Response({"error": f"Vehicle with ID {vehicle_id} not found for this branch."}, status=404)

            # Check availability if not already assigned to this booking
            if booking.assigned_vehicle_id != target_vehicle.id and target_vehicle.status == FleetVehicle.VehicleStatus.MAINTENANCE:
                return Response({"error": f"Vehicle {target_vehicle.vehicle_number} is currently under Maintenance."}, status=400)
        else:
            return Response({"error": "Either vehicle_id or vendor cab details (vehicle_number, driver_name, driver_phone) must be provided."}, status=400)

        # Release previous vehicle if different
        if booking.assigned_vehicle and booking.assigned_vehicle.id != target_vehicle.id:
            old_v = booking.assigned_vehicle
            other_active = CabBooking.objects.filter(
                assigned_vehicle=old_v,
                trip_status__in=[CabBooking.TripStatus.DRIVER_ASSIGNED, CabBooking.TripStatus.EN_ROUTE]
            ).exclude(pk=booking.pk).exists()
            if not other_active and old_v.status == FleetVehicle.VehicleStatus.ON_TRIP:
                old_v.status = FleetVehicle.VehicleStatus.AVAILABLE
                old_v.save(update_fields=['status', 'updated_at'])

        # Assign new vehicle and update statuses
        booking.assigned_vehicle = target_vehicle
        booking.trip_status = CabBooking.TripStatus.DRIVER_ASSIGNED
        booking.save(update_fields=['assigned_vehicle', 'trip_status', 'updated_at'])

        target_vehicle.status = FleetVehicle.VehicleStatus.ON_TRIP
        target_vehicle.save(update_fields=['status', 'updated_at'])

        # Trigger notification
        send_driver_assignment_notification(booking, target_vehicle)

        return Response({
            "success": True,
            "message": f"Driver {target_vehicle.driver_name} successfully assigned to booking {booking.booking_id}.",
            "booking_id": booking.booking_id,
            "trip_status": booking.trip_status,
            "trip_status_display": booking.get_trip_status_display(),
            "assigned_vehicle": {
                "id": target_vehicle.id,
                "vehicle_number": target_vehicle.vehicle_number,
                "driver_name": target_vehicle.driver_name,
                "driver_phone": target_vehicle.driver_phone,
                "vehicle_category": target_vehicle.get_vehicle_category_display(),
                "is_vendor_vehicle": target_vehicle.is_vendor_vehicle,
                "status": target_vehicle.status,
            }
        })


class CabBookingRescheduleView(APIView):
    """
    PATCH /api/hotel/cabs/bookings/{id}/reschedule/
    Allows the Hotel Admin to edit pickup_datetime, flight_or_train_number,
    pickup_location, or drop_location on the Dispatch Board (e.g. for Delayed Flights)
    without altering the booking price or payment status.
    Gated by IsActiveHotelAdmin.
    """
    permission_classes = [IsActiveHotelAdmin]

    def patch(self, request, pk):
        booking = get_object_or_404(CabBooking, pk=pk)
        data = request.data

        if 'pickup_datetime' in data and data['pickup_datetime']:
            try:
                p_dt = datetime.datetime.fromisoformat(data['pickup_datetime'])
                if timezone.is_naive(p_dt):
                    p_dt = timezone.make_aware(p_dt)
                booking.pickup_datetime = p_dt
            except Exception:
                return Response({"error": "Invalid pickup_datetime format. Use ISO format YYYY-MM-DDTHH:MM."}, status=400)

        if 'flight_or_train_number' in data:
            booking.flight_or_train_number = (data.get('flight_or_train_number') or '').strip()

        if 'pickup_location' in data and data['pickup_location']:
            booking.pickup_location = data['pickup_location'].strip()

        if 'drop_location' in data and data['drop_location']:
            booking.drop_location = data['drop_location'].strip()

        if 'guest_notes' in data:
            booking.guest_notes = data['guest_notes'].strip()

        # Save ONLY the operational travel fields, keeping price and payment untouched
        booking.save(update_fields=[
            'pickup_datetime',
            'flight_or_train_number',
            'pickup_location',
            'drop_location',
            'guest_notes',
            'updated_at'
        ])

        return Response({
            "success": True,
            "message": f"Cab booking {booking.booking_id} ride schedule updated successfully.",
            "cab_booking": CabBookingSerializer(booking).data
        })


class CabBookingStatusUpdateView(APIView):
    """
    PATCH /api/hotel/cabs/bookings/{id}/status/
    Updates trip status (SCHEDULED, DRIVER_ASSIGNED, EN_ROUTE, COMPLETED, CANCELLED).
    Releases assigned FleetVehicle back to 'AVAILABLE' upon COMPLETED or CANCELLED.
    Gated by IsActiveHotelAdmin.
    """
    permission_classes = [IsActiveHotelAdmin]

    def patch(self, request, pk):
        booking = get_object_or_404(CabBooking, pk=pk)
        new_status = (request.data.get('status') or request.data.get('trip_status') or '').upper()

        if new_status not in CabBooking.TripStatus.values:
            return Response({
                "error": f"Invalid trip status '{new_status}'. Choices are: {CabBooking.TripStatus.values}"
            }, status=400)

        booking.trip_status = new_status
        booking.save(update_fields=['trip_status', 'updated_at'])

        # If completed or cancelled, release assigned vehicle back to AVAILABLE
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

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "trip_status": booking.trip_status,
            "trip_status_display": booking.get_trip_status_display(),
            "message": f"Ride status updated to {booking.get_trip_status_display()}."
        })


class PublicCabPackagesView(APIView):
    """
    GET /api/public/hotel/{branch_id}/cab-packages/
    Returns active CabServicePackage items ONLY if branch.has_hotel_module == True.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, branch_id=None):
        branch = None
        if branch_id:
            branch = RestaurantBranch.objects.filter(id=branch_id).first()

        if not branch:
            b_val = request.query_params.get('branch_id') or request.query_params.get('branch') or request.query_params.get('branch_slug')
            if b_val:
                if str(b_val).isdigit():
                    branch = RestaurantBranch.objects.filter(id=int(b_val)).first()
                else:
                    branch = RestaurantBranch.objects.filter(slug=b_val).first()

        if not branch:
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first()

        if not branch:
            return Response({"error": "Branch not found or specified."}, status=404)

        if not getattr(branch, 'has_hotel_module', False):
            return Response({
                "branch_id": branch.id,
                "has_hotel_module": False,
                "packages": [],
                "message": "Hotel module is not active for this branch."
            }, status=403)

        packages = CabServicePackage.objects.filter(branch=branch, is_active=True).order_by('service_type', 'base_price')
        serializer = CabServicePackageSerializer(packages, many=True)

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "has_hotel_module": True,
            "count": len(serializer.data),
            "packages": serializer.data,
        })


class PublicHotelCheckoutView(APIView):
    """
    POST /api/public/hotel/checkout/
    Unified atomic checkout endpoint that:
    1. Locks room inventory (select_for_update()).
    2. Creates the RoomBooking record (PENDING).
    3. Validates and creates linked CabBooking records (applying complimentary override if eligible).
    4. Generates a single Razorpay Order ID for the combined Room + Paid Cabs grand total.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from restaurant_project import constants
        if not getattr(constants, 'ENABLE_HOTEL_MODULE', True):
            return Response({"error": "Hotel PMS module is globally disabled."}, status=403)

        serializer = PublicHotelCheckoutSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "Validation failed", "details": serializer.errors}, status=400)

        result = serializer.save()

        room_booking = result['room_booking']
        cabs_data = CabBookingSerializer(result['cab_bookings'], many=True).data

        return Response({
            "success": True,
            "booking_id": room_booking.booking_id,
            "room_booking_id": room_booking.id,
            "room_number": room_booking.room.room_number,
            "category_name": room_booking.room.category.name,
            "check_in_date": room_booking.check_in_date.isoformat(),
            "check_out_date": room_booking.check_out_date.isoformat(),
            "guest_name": room_booking.guest_name,
            "guest_phone": room_booking.guest_phone,
            "guest_email": room_booking.guest_email,
            "room_charges": str(result['room_charges']),
            "room_taxes": str(result['room_taxes']),
            "room_total": str(result['room_total']),
            "cab_total": str(result['cab_total']),
            "grand_total": str(result['grand_total']),
            "total_amount": float(result['grand_total']),
            "currency": result['currency'],
            "razorpay_order_id": result['razorpay_order_id'],
            "razorpay_key_id": result['razorpay_key_id'],
            "booking_status": room_booking.booking_status,
            "payment_status": room_booking.payment_status,
            "cab_bookings_count": len(cabs_data),
            "cab_bookings": cabs_data,
            "message": f"Reservation created! Booking code: {room_booking.booking_id}. Please proceed to payment."
        }, status=status.HTTP_201_CREATED)



class PublicHotelVerifyPaymentView(APIView):
    """
    POST /api/rooms/verify-payment/ or /api/hotel/public/verify-payment/
    Verifies Razorpay payment signature for room reservation.
    Updates room_booking.payment_status = 'PAID' and linked cab bookings.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        booking_id = request.data.get('booking_id') or request.data.get('booking_reference')
        razorpay_order_id = request.data.get('razorpay_order_id')
        razorpay_payment_id = request.data.get('razorpay_payment_id')
        razorpay_signature = request.data.get('razorpay_signature')

        if not booking_id:
            return Response({"error": "booking_id is required."}, status=400)

        # Lookup RoomBooking
        room_booking = None
        if str(booking_id).isdigit():
            room_booking = RoomBooking.objects.filter(id=int(booking_id)).first()
        if not room_booking:
            room_booking = RoomBooking.objects.filter(booking_id=booking_id).first()
        if not room_booking:
            return Response({"error": f"Booking '{booking_id}' not found."}, status=404)

        # If already PAID, return idempotent success
        if room_booking.payment_status == RoomBooking.PaymentStatus.PAID:
            return Response({
                "success": True,
                "message": "Payment already verified.",
                "booking_id": room_booking.booking_id,
                "payment_status": room_booking.payment_status,
                "booking_status": room_booking.booking_status,
                "grand_total": str(room_booking.grand_total),
            })

        # Signature verification
        branch = room_booking.branch
        h_settings = getattr(branch, 'hotel_settings', None) or HotelSettings.get_settings(branch)
        key_secret = None
        if h_settings and h_settings.razorpay_key_secret:
            key_secret = h_settings.razorpay_key_secret.strip()
        elif branch.restaurant:
            from core.models import RestaurantSettings
            r_settings = RestaurantSettings.get_settings(branch.restaurant)
            if r_settings and r_settings.razorpay_key_secret:
                key_secret = r_settings.razorpay_key_secret.strip()
        if not key_secret:
            from django.conf import settings as dj_settings
            key_secret = getattr(dj_settings, 'RAZORPAY_KEY_SECRET', '')

        # Check signature if credentials present
        if key_secret and not key_secret.startswith('mock') and razorpay_signature:
            import hmac
            import hashlib
            order_to_verify = razorpay_order_id or room_booking.razorpay_order_id or ''
            msg = f"{order_to_verify}|{razorpay_payment_id}".encode('utf-8')
            generated_signature = hmac.new(key_secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(generated_signature, razorpay_signature):
                return Response({"error": "Razorpay payment signature verification failed."}, status=400)

        # Mark paid
        room_booking.payment_status = RoomBooking.PaymentStatus.PAID
        room_booking.payment_method = 'ONLINE_RAZORPAY'
        if razorpay_order_id:
            room_booking.razorpay_order_id = razorpay_order_id
        if razorpay_payment_id:
            room_booking.razorpay_payment_id = razorpay_payment_id
        if razorpay_signature:
            room_booking.razorpay_signature = razorpay_signature
        room_booking.save(update_fields=[
            'payment_status', 'payment_method',
            'razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature',
            'updated_at'
        ])

        # Also update any linked cab bookings to PAID
        room_booking.cab_bookings.filter(
            payment_status=CabBooking.PaymentStatus.PENDING
        ).update(payment_status=CabBooking.PaymentStatus.PAID)

        return Response({
            "success": True,
            "message": "Payment verified and booking confirmed!",
            "booking_id": room_booking.booking_id,
            "room_booking_id": room_booking.id,
            "room_number": room_booking.room.room_number,
            "category_name": room_booking.room.category.name,
            "guest_name": room_booking.guest_name,
            "payment_status": room_booking.payment_status,
            "payment_method": room_booking.payment_method,
            "razorpay_payment_id": razorpay_payment_id,
            "grand_total": str(room_booking.grand_total),
        })


class PublicHotelCancelUnpaidView(APIView):
    """
    POST /api/rooms/cancel-unpaid/ or /api/hotel/public/cancel-unpaid/
    If a guest aborts online checkout or closes the payment modal,
    marks the pending reservation as CANCELLED to release the inventory lock immediately.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        booking_id = request.data.get('booking_id') or request.data.get('booking_reference')
        if not booking_id:
            return Response({"error": "booking_id is required."}, status=400)

        room_booking = RoomBooking.objects.filter(booking_id=booking_id).first()
        if not room_booking and str(booking_id).isdigit():
            room_booking = RoomBooking.objects.filter(id=int(booking_id)).first()

        if not room_booking:
            return Response({"error": "Booking not found."}, status=404)

        if room_booking.payment_status == RoomBooking.PaymentStatus.PAID:
            return Response({"error": "Cannot cancel an already paid booking via public endpoint."}, status=400)

        room_booking.booking_status = RoomBooking.BookingStatus.CANCELLED
        room_booking.save(update_fields=['booking_status', 'updated_at'])

        # Cancel linked cabs as well
        room_booking.cab_bookings.all().update(trip_status=CabBooking.TripStatus.CANCELLED)

        return Response({
            "success": True,
            "message": f"Unpaid reservation {room_booking.booking_id} has been released.",
            "booking_id": room_booking.booking_id,
            "booking_status": room_booking.booking_status
        })


class PublicCabBookingCreateView(APIView):
    """
    POST /api/cabs/bookings/ or /api/public/hotel/cabs/book/
    Allows public guests to book a luxury cab, airport transfer, hourly rental, or outstation ride
    directly from the frontend cab service UI.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        customer_name = (data.get('customer_name') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()
        pickup_loc = (data.get('pickup_location') or '').strip()
        drop_loc = (data.get('drop_location') or '').strip()
        pickup_date = data.get('pickup_date') or ''
        pickup_time = data.get('pickup_time') or '10:00'
        cab_name = data.get('cab_name') or 'VIP Luxury Cab'
        cab_category_raw = data.get('cab_category') or ''
        raw_trip_type = (data.get('trip_type') or 'airport').lower()
        passengers = data.get('passengers') or 1
        luggage = data.get('luggage') or 1
        duration_hours = data.get('duration_hours')
        flight_number = (data.get('flight_number') or '').strip()
        chauffeur_name = (data.get('chauffeur_name') or '').strip()
        special_requests = (data.get('special_requests') or '').strip()

        if not customer_name:
            return Response({"error": "Passenger name is required."}, status=400)
        if not phone:
            return Response({"error": "Contact phone number is required."}, status=400)
        if not pickup_loc:
            return Response({"error": "Pickup location is required."}, status=400)

        # 1. Resolve Branch
        branch = None
        branch_id = data.get('branch_id')
        branch_slug = data.get('branch_slug')
        restaurant_slug = data.get('restaurant_slug')

        if branch_id:
            branch = RestaurantBranch.objects.filter(id=branch_id).first()
        elif branch_slug:
            branch = RestaurantBranch.objects.filter(slug=branch_slug).first()
        elif restaurant_slug:
            branch = RestaurantBranch.objects.filter(
                restaurant__slug=restaurant_slug,
                has_hotel_module=True
            ).first() or RestaurantBranch.objects.filter(restaurant__slug=restaurant_slug).first()

        if not branch:
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first()

        if not branch:
            branch = RestaurantBranch.objects.first()

        if not branch:
            return Response({"error": "No hotel branch available for cab booking."}, status=400)

        # 2. Parse pickup datetime
        try:
            p_dt = datetime.datetime.strptime(f"{pickup_date} {pickup_time}", "%Y-%m-%d %H:%M")
            if timezone.is_naive(p_dt):
                p_dt = timezone.make_aware(p_dt)
        except Exception:
            try:
                p_dt = datetime.datetime.fromisoformat(pickup_date)
                if timezone.is_naive(p_dt):
                    p_dt = timezone.make_aware(p_dt)
            except Exception:
                p_dt = timezone.now() + datetime.timedelta(hours=2)

        # 3. Map trip type
        trip_type_map = {
            'airport': CabServicePackage.ServiceType.AIRPORT_TRANSFER,
            'city': CabServicePackage.ServiceType.POINT_TO_POINT,
            'hourly': CabServicePackage.ServiceType.HOURLY_RENTAL,
            'outstation': CabServicePackage.ServiceType.OUTSTATION,
        }
        service_type = trip_type_map.get(raw_trip_type, CabServicePackage.ServiceType.AIRPORT_TRANSFER)

        # 4. Resolve Fleet vehicle or driver assignment
        assigned_vehicle = None
        if chauffeur_name and chauffeur_name.lower() not in ['auto', 'auto-assign best', 'nearest available']:
            first_name = chauffeur_name.split()[0]
            assigned_vehicle = FleetVehicle.objects.filter(
                branch=branch,
                driver_name__icontains=first_name,
                status=FleetVehicle.VehicleStatus.AVAILABLE
            ).first()

        if not assigned_vehicle:
            # Check if any matching vehicle category is available for auto assignment
            cat_code = CabServicePackage.VehicleCategory.SEDAN
            upper_cat = cab_category_raw.upper()
            if 'SUV' in upper_cat:
                cat_code = CabServicePackage.VehicleCategory.SUV
            elif 'MPV' in upper_cat or 'ROYAL' in upper_cat:
                cat_code = CabServicePackage.VehicleCategory.ROYAL_MPV
            elif 'ELECTRIC' in upper_cat or 'EV' in upper_cat:
                cat_code = CabServicePackage.VehicleCategory.ELECTRIC
            elif 'LIMOUSINE' in upper_cat:
                cat_code = CabServicePackage.VehicleCategory.LIMOUSINE

            assigned_vehicle = FleetVehicle.objects.filter(
                branch=branch,
                vehicle_category=cat_code,
                status=FleetVehicle.VehicleStatus.AVAILABLE
            ).first()

        trip_status = CabBooking.TripStatus.DRIVER_ASSIGNED if assigned_vehicle else CabBooking.TripStatus.SCHEDULED
        if assigned_vehicle:
            assigned_vehicle.status = FleetVehicle.VehicleStatus.ON_TRIP
            assigned_vehicle.save(update_fields=['status', 'updated_at'])

        # 5. Financials
        try:
            total_amt = Decimal(str(data.get('total_estimated_amount') or 0))
        except Exception:
            total_amt = Decimal('0.00')

        if total_amt <= 0:
            total_amt = Decimal('1500.00')

        tax_amt = round(total_amt * Decimal('0.05'), 2)
        base_amt = total_amt - tax_amt

        # 6. Create booking
        if not drop_loc and raw_trip_type == 'hourly':
            drop_loc = 'Flexible City Hourly Destination'
        elif not drop_loc:
            drop_loc = 'Resort Main Entrance & Reception'

        booking = CabBooking.objects.create(
            branch=branch,
            room_booking=None,
            guest_name=customer_name,
            guest_phone=phone,
            guest_email=email,
            trip_type=service_type,
            selected_cab_name=cab_name,
            preferred_chauffeur=chauffeur_name or '',
            passengers=int(passengers or 1),
            luggage=int(luggage or 1),
            duration_hours=int(duration_hours or 0) if duration_hours else None,
            special_requests=special_requests,
            source=CabBooking.BookingSource.FRONTEND_DIRECT,
            pickup_datetime=p_dt,
            pickup_location=pickup_loc,
            drop_location=drop_loc,
            flight_or_train_number=flight_number,
            base_amount=base_amt,
            tax_amount=tax_amt,
            total_amount=total_amt,
            payment_status=CabBooking.PaymentStatus.PENDING,
            trip_status=trip_status,
            assigned_vehicle=assigned_vehicle,
        )

        driver_info = {
            "name": assigned_vehicle.driver_name if assigned_vehicle else (chauffeur_name if chauffeur_name and chauffeur_name.lower() not in ['auto', 'auto-assign best'] else 'VIP Fleet Chauffeur (Dispatched prior to pickup)'),
            "phone": assigned_vehicle.driver_phone if assigned_vehicle else '+91 98765 43210',
            "vehicle_number": assigned_vehicle.vehicle_number if assigned_vehicle else 'Assigned upon dispatch',
            "vehicle_model": assigned_vehicle.vehicle_model if assigned_vehicle and assigned_vehicle.vehicle_model else booking.selected_cab_name,
        }

        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "reference": booking.booking_id,
            "trip_status": booking.trip_status,
            "trip_status_display": booking.get_trip_status_display(),
            "pickup_datetime": booking.pickup_datetime.isoformat(),
            "pickup_date": pickup_date or booking.pickup_datetime.strftime('%Y-%m-%d'),
            "pickup_time": pickup_time or booking.pickup_datetime.strftime('%H:%M'),
            "pickup_location": booking.pickup_location,
            "drop_location": booking.drop_location,
            "customer_name": booking.guest_name,
            "phone": booking.guest_phone,
            "email": booking.guest_email,
            "cab_name": booking.selected_cab_name,
            "trip_type": raw_trip_type,
            "trip_type_label": booking.get_trip_type_display(),
            "passengers": booking.passengers,
            "luggage": booking.luggage,
            "total_amount": float(booking.total_amount),
            "assigned_driver": driver_info,
            "message": f"Cab ride booked successfully! Reference: {booking.booking_id}."
        }, status=status.HTTP_201_CREATED)


class PublicCabBookingDetailView(APIView):
    """
    GET /api/cabs/bookings/<booking_id>/ or /api/public/hotel/cabs/booking/<booking_id>/
    Allows public status checking and voucher verification for a cab booking.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, booking_id):
        booking = CabBooking.objects.filter(booking_id=booking_id).first()
        if not booking and str(booking_id).isdigit():
            booking = CabBooking.objects.filter(id=int(booking_id)).first()

        if not booking:
            return Response({"error": "Cab booking not found."}, status=404)

        data = CabBookingSerializer(booking).data
        return Response({
            "success": True,
            "booking": data
        })


class PublicCabFleetView(APIView):
    """
    GET /api/cabs/fleet/
    Returns active fleet vehicles and chauffeurs for display on the frontend.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        branch_val = request.query_params.get('branch_id') or request.query_params.get('branch_slug') or request.query_params.get('restaurant_slug')
        branch = None
        if branch_val:
            if str(branch_val).isdigit():
                branch = RestaurantBranch.objects.filter(id=int(branch_val)).first()
            else:
                branch = RestaurantBranch.objects.filter(slug=branch_val).first() or RestaurantBranch.objects.filter(restaurant__slug=branch_val, has_hotel_module=True).first()

        if not branch:
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()

        if not branch:
            return Response({"fleet": [], "packages": []})

        vehicles = FleetVehicle.objects.filter(branch=branch)
        packages = CabServicePackage.objects.filter(branch=branch, is_active=True)

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "fleet": FleetVehicleSerializer(vehicles, many=True).data,
            "packages": CabServicePackageSerializer(packages, many=True).data,
        })

