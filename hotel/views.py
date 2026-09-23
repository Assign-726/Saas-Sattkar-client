import datetime
from decimal import Decimal
from django.db.models import Q, Count
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth import authenticate
from rest_framework import status, viewsets, permissions, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from super_admin.models import RestaurantBranch, Restaurant
from hotel.models import RoomCategory, Room, RoomBooking, HotelSettings
from hotel.serializers import (
    RoomCategorySerializer,
    RoomSerializer,
    RoomBookingSerializer,
    PublicAvailabilityCategorySerializer,
)
from hotel.permissions import IsHotelAdminOrManager


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

                # Determine tax rate based on HotelSettings
                if h_settings and not h_settings.enable_dynamic_gst:
                    tax_rate = (h_settings.tax_rate / Decimal('100'))
                else:
                    tax_rate = Decimal('0.12') if base_rate <= 7500 else Decimal('0.18')

                tax_amount = round(room_charges * tax_rate, 2)

                service_charge = Decimal('0.00')
                if h_settings and h_settings.service_charge_percent > 0:
                    service_charge = round(room_charges * (h_settings.service_charge_percent / Decimal('100')), 2)

                grand_total = room_charges + tax_amount + service_charge

                name_lower = cat.name.lower()
                is_non_ac = 'non-ac' in name_lower or 'non ac' in name_lower
                cooling_type = 'Non-AC' if is_non_ac else 'AC'
                
                cat_type = 'deluxe'
                if 'villa' in name_lower:
                    cat_type = 'villa'
                elif 'suite' in name_lower or 'penthouse' in name_lower:
                    cat_type = 'suite'

                is_featured = bool(base_rate >= Decimal('8000') or 'presidential' in name_lower or 'royal' in name_lower)

                available_categories.append({
                    "id": cat.id,
                    "name": cat.name,
                    "slug": cat.slug,
                    "description": cat.description,
                    "base_price_per_night": str(cat.base_price_per_night),
                    "max_occupancy": cat.max_occupancy,
                    "max_adults": cat.max_adults,
                    "max_children": cat.max_children,
                    "bed_type": cat.bed_type,
                    "room_size_sqft": cat.room_size_sqft,
                    "cooling_type": cooling_type,
                    "category_type": cat_type,
                    "is_featured": is_featured,
                    "amenities": cat.amenities,
                    "effective_image": cat.effective_image_url,
                    "available_rooms_count": avail_count,
                    "available_room_ids": list(available_rooms.values_list('id', flat=True)),
                    "nights": nights,
                    "estimated_room_charges": str(room_charges),
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
