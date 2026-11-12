import datetime
import logging
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Count
from django.utils import timezone

logger = logging.getLogger(__name__)
from django.shortcuts import get_object_or_404
from django.contrib.auth import authenticate
from rest_framework import status, viewsets, permissions, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from super_admin.models import RestaurantBranch, Restaurant
from hotel import pricing, ops
from hotel.models import (
    RoomCategory,
    Room,
    RoomBooking,
    HotelSettings,
    CabServicePackage,
    FleetVehicle,
    CabBooking,
    SpaTherapist,
    SpaTreatment,
    SpaAppointment,
    SpaPackage,
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
    SpaTherapistSerializer,
    SpaTreatmentSerializer,
    SpaPackageSerializer,
    SpaAppointmentSerializer,
)
from hotel.permissions import IsHotelAdminOrManager, IsActiveHotelAdmin
from core.throttling import (PaymentRateThrottle, PublicLookupThrottle, LoginRateThrottle,
                             login_locked, record_login_failure, clear_login_failures)



def resolve_branch_for_request(request):
    """
    The branch an authenticated hotel API call works on. A requested ``branch_id`` /
    ``branch_slug`` is honoured only if it belongs to the caller's business (platform
    admins may pick any); otherwise None, so the view returns nothing rather than another
    hotel's data.
    """
    from core import tenancy
    data = request.data if hasattr(request, 'data') and isinstance(request.data, dict) else {}
    branch_id = (request.query_params.get('branch_id') or request.query_params.get('branch')
                 or data.get('branch_id') or data.get('branch'))
    branch_slug = request.query_params.get('branch_slug') or data.get('branch_slug')
    allowed = tenancy.allowed_branches(request.user)

    if branch_id:
        try:
            return allowed.filter(id=int(branch_id)).first()
        except (TypeError, ValueError):
            return None
    if branch_slug:
        return allowed.filter(slug=branch_slug).first()

    p = tenancy.profile(request.user)
    if p:
        if p.branch_id:
            return p.branch
        return allowed.filter(has_hotel_module=True).first() or allowed.first()
    if tenancy.is_platform_admin(request.user):
        return RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()
    return None


def _scoped(model, request, pk):
    """``model`` row ``pk`` if it belongs to one of the caller's branches, else 404."""
    from core import tenancy
    return get_object_or_404(model, pk=pk, branch__in=tenancy.allowed_branches(request.user))


def _check_branch(request, branch):
    from core import tenancy
    if branch is not None and not tenancy.can_access_branch(request.user, branch):
        raise serializers.ValidationError({"branch": "You can only manage your own hotel."})


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
        if category is not None:
            _check_branch(self.request, category.branch)
            branch = category.branch
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)

    def perform_update(self, serializer):
        category = serializer.validated_data.get('category')
        if category is not None:
            _check_branch(self.request, category.branch)
        serializer.save()


class RoomStatusUpdateView(APIView):
    """
    PATCH /api/hotel/rooms/{id}/status/
    Quickly update a room's physical status (AVAILABLE, OCCUPIED, CLEANING, MAINTENANCE)
    e.g. Front desk clicks 'Mark Clean' on dirty rooms.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        room = _scoped(Room, request, pk)
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
        with transaction.atomic():
            room = serializer.validated_data.get('room')
            if room:
                Room.objects.select_for_update().filter(id=room.id).first()
                check_in = serializer.validated_data.get('check_in_date')
                check_out = serializer.validated_data.get('check_out_date')
                if check_in and check_out:
                    overlap_qs = RoomBooking.objects.filter(
                        room=room,
                        booking_status__in=[
                            RoomBooking.BookingStatus.RESERVED,
                            RoomBooking.BookingStatus.CHECKED_IN,
                        ],
                        check_in_date__lt=check_out,
                        check_out_date__gt=check_in,
                    )
                    if overlap_qs.exists():
                        conflict = overlap_qs.first()
                        raise serializers.ValidationError({
                            "room": f"Room {room.room_number} is already booked from {conflict.check_in_date} to {conflict.check_out_date}."
                        })
            branch = room.branch if room else resolve_branch_for_request(self.request)
            _check_branch(self.request, branch)
            if branch is None:
                raise serializers.ValidationError({"branch": "A valid hotel branch is required."})
            serializer.save(branch=branch)

    def perform_update(self, serializer):
        room = serializer.validated_data.get('room')
        if room is not None:
            _check_branch(self.request, room.branch)
            serializer.save(branch=room.branch)
        else:
            serializer.save()


class BookingCheckInView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/check-in/
    Checks a reserved guest in (same rules as the front desk: ID on file, arrival date,
    room free and not out of order) and marks the room occupied.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        from hotel import folio
        booking = _scoped(RoomBooking, request, pk)
        err = folio.check_in(booking, request.user)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        booking.refresh_from_db()
        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": booking.booking_status,
            "room_number": booking.room.room_number,
            "room_status": Room.RoomStatus.OCCUPIED,
            "message": f"Guest {booking.guest_name} successfully checked into Room {booking.room.room_number}."
        })


class BookingCheckOutView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/check-out/
    Checks the guest out, issues the invoice number and queues the room for cleaning.
    Refused while a balance is due unless ``{"allow_balance": true}`` is sent.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        from hotel import folio
        booking = _scoped(RoomBooking, request, pk)
        allow = str(request.data.get('allow_balance', '')).lower() in ('1', 'true', 'yes')
        err = folio.check_out(booking, request.user, allow_balance=allow)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        booking.refresh_from_db()
        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": booking.booking_status,
            "invoice_number": booking.invoice_number,
            "room_number": booking.room.room_number,
            "room_status": Room.RoomStatus.CLEANING,
            "message": f"Guest {booking.guest_name} checked out. Room {booking.room.room_number} marked for Cleaning."
        })


class BookingCancelView(APIView):
    """
    PATCH /api/hotel/bookings/{id}/cancel/   body: {"reason": "..."}
    Cancels a booking and frees the room if the guest was in house.
    """
    permission_classes = [IsHotelAdminOrManager]

    def patch(self, request, pk):
        from hotel import folio
        booking = _scoped(RoomBooking, request, pk)
        err = folio.cancel(booking, request.user, request.data.get('reason', ''))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "success": True,
            "booking_id": booking.booking_id,
            "booking_status": RoomBooking.BookingStatus.CANCELLED,
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
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')

        if not username or not password:
            return Response({"error": "Username and password are required."}, status=400)
        if login_locked(username):
            return Response({"error": "Too many failed attempts. Try again in 15 minutes."}, status=429)

        user = authenticate(request, username=username, password=password)
        if not user:
            # Fallback check for case-insensitive or email login
            from django.contrib.auth.models import User
            candidate = User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).first()
            if candidate and candidate.check_password(password):
                user = candidate

        if not user or not user.is_active:
            record_login_failure(username)
            return Response({"error": "Invalid username or password."}, status=401)
        clear_login_failures(username)

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

            # Free rooms: not booked and not out of order for these dates (hotel/pricing.py)
            available_rooms = pricing.free_rooms(cat_rooms, branch, start_date, end_date)
            avail_count = available_rooms.count()

            q = pricing.quote(cat, start_date, end_date, h_settings)
            if q['restriction']:
                avail_count = 0

            # Filter out categories where all associated rooms are locked
            if avail_count > 0:
                base_rate = cat.base_price_per_night
                room_charges = q['room_charges']
                tax_amount = q['tax']
                service_charge = q['service_charge']
                grand_total = q['total'] + service_charge
                display_room_charges = q['net'] if cat.price_is_tax_inclusive else room_charges

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
                    "tax_rate_percent": float(q['nights'][0]['tax_percent']) if q['nights'] else 0.0,
                    "average_price_per_night": str(q['average_rate']),
                    "has_special_rate": q['has_special_rate'],
                    "nightly_rates": [{"date": n['date'].isoformat(), "price": str(n['price']), "label": n['label']} for n in q['nights']],
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
    throttle_classes = [PaymentRateThrottle]

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

        with transaction.atomic():
            # Allocate room with database locking
            target_room = None
            target_cat = None

            if category_id:
                target_cat = RoomCategory.objects.filter(id=category_id, is_active=True).first()

            if target_cat:
                candidate_rooms = list(target_cat.rooms.select_for_update().filter(is_active=True).order_by('room_number'))
                from hotel import pricing
                target_room = pricing.free_rooms(
                    Room.objects.filter(id__in=[r.id for r in candidate_rooms]),
                    target_cat.branch,
                    check_in_date,
                    check_out_date
                ).first()

            # If not resolved via category, try directly as physical room ID
            if not target_room and room_id:
                candidate_room = Room.objects.select_for_update().filter(id=room_id, is_active=True).first()
                if candidate_room:
                    from hotel import pricing
                    if not pricing.room_conflict(candidate_room, check_in_date, check_out_date):
                        target_room = candidate_room

            # If still not found, check if ANY active category has available rooms
            if not target_room:
                all_cats = RoomCategory.objects.filter(is_active=True).prefetch_related('rooms')
                for fallback_cat in all_cats:
                    candidate_rooms = list(fallback_cat.rooms.select_for_update().filter(is_active=True).order_by('room_number'))
                    from hotel import pricing
                    avail = pricing.free_rooms(
                        Room.objects.filter(id__in=[r.id for r in candidate_rooms]),
                        fallback_cat.branch,
                        check_in_date,
                        check_out_date
                    ).first()
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
                source=RoomBooking.Source.WEBSITE,
            )

        # Dispatch hotel reservation voucher email
        try:
            from core.emails import send_hotel_booking_email
            send_hotel_booking_email(booking, request=request)
        except Exception as email_err:
            logger.warning(f"Could not send hotel booking voucher email for {booking.booking_id}: {email_err}")

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
            package = None
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
        booking = _scoped(CabBooking, request, pk)

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
        booking = _scoped(CabBooking, request, pk)
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
        booking = _scoped(CabBooking, request, pk)
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
    throttle_classes = [PaymentRateThrottle]

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

        # For non-gateway flows (Pay at Desk, Direct UPI submission), dispatch voucher immediately
        if room_booking.payment_method != 'ONLINE_RAZORPAY':
            try:
                from core.emails import send_hotel_booking_email
                send_hotel_booking_email(room_booking, request=request)
            except Exception as email_err:
                logger.warning(f"Could not send checkout voucher email for {room_booking.booking_id}: {email_err}")

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
    throttle_classes = [PaymentRateThrottle]

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

        # Signature verification — always required, and always against the Razorpay order
        # we created for this booking (never one the client names).
        from core import payments
        from core.models import GatewayPayment
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

        stored_order_id = room_booking.razorpay_order_id or ''
        if stored_order_id and razorpay_order_id and razorpay_order_id != stored_order_id:
            return Response({"error": "This payment belongs to a different checkout."}, status=400)
        order_to_verify = stored_order_id or (razorpay_order_id if payments.is_mock_secret(key_secret) else '')
        if not payments.signature_ok(key_secret, order_to_verify, razorpay_payment_id, razorpay_signature):
            return Response({"error": "Razorpay payment signature verification failed."}, status=400)
        if not payments.is_mock_secret(key_secret) and not GatewayPayment.claim(
                razorpay_payment_id, 'room', room_booking.pk, stored_order_id, room_booking.grand_total):
            return Response({"error": "This payment has already been used for another booking."}, status=409)
        razorpay_order_id = stored_order_id or razorpay_order_id

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

        # Dispatch verified paid hotel reservation voucher email
        try:
            from core.emails import send_hotel_booking_email
            send_hotel_booking_email(room_booking, request=request)
        except Exception as email_err:
            logger.warning(f"Could not send verified hotel booking email for {room_booking.booking_id}: {email_err}")

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
    throttle_classes = [PaymentRateThrottle]

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
        # Only the guest's own abandoned online checkout can be released here — never a
        # pay-at-desk, front-desk or channel booking, and only while it is fresh.
        fresh = room_booking.created_at and room_booking.created_at >= timezone.now() - datetime.timedelta(hours=2)
        if (room_booking.payment_method != 'ONLINE_RAZORPAY' or not fresh
                or room_booking.booking_status != RoomBooking.BookingStatus.RESERVED):
            return Response({"error": "This booking can't be cancelled online. Please contact the hotel."}, status=400)
        given_order = request.data.get('razorpay_order_id')
        if room_booking.razorpay_order_id and given_order and given_order != room_booking.razorpay_order_id:
            return Response({"error": "Booking not found."}, status=404)

        room_booking.booking_status = RoomBooking.BookingStatus.CANCELLED
        room_booking.cancelled_at = timezone.now()
        room_booking.cancellation_reason = 'Online payment not completed'
        room_booking.save(update_fields=['booking_status', 'cancelled_at', 'cancellation_reason', 'updated_at'])

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
    throttle_classes = [PaymentRateThrottle]

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
    throttle_classes = [PublicLookupThrottle]

    def get(self, request, booking_id):
        # Look-ups by the unguessable booking code only; numeric ids would let anyone page
        # through every guest's trip and phone number.
        booking = CabBooking.objects.select_related('package', 'assigned_vehicle').filter(booking_id=booking_id).first()
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


# ==============================================================================
# SPA & WELLNESS VIEWS
# ==============================================================================

class SpaTherapistViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Spa Therapists.
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = SpaTherapistSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = SpaTherapist.objects.all()
        elif branch:
            qs = SpaTherapist.objects.filter(branch=branch)
        else:
            qs = SpaTherapist.objects.none()

        active = self.request.query_params.get('is_active')
        if active is not None:
            qs = qs.filter(is_active=active.lower() in ['true', '1'])
        return qs.order_by('name')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)

    def perform_destroy(self, instance):
        appts = SpaAppointment.objects.filter(therapist=instance)
        if appts.filter(charge__isnull=False).exists():
            raise serializers.ValidationError({"detail": "Cannot delete specialist with billed appointments on guest folios. Set them to Inactive instead."})
        other = SpaTherapist.objects.filter(branch=instance.branch, is_active=True).exclude(pk=instance.pk).first()
        if other:
            appts.update(therapist=other)
        else:
            appts.delete()
        instance.delete()


class SpaTreatmentViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Spa Treatments & Rituals.
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = SpaTreatmentSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = SpaTreatment.objects.all()
        elif branch:
            qs = SpaTreatment.objects.filter(branch=branch)
        else:
            qs = SpaTreatment.objects.none()

        cat = self.request.query_params.get('category')
        if cat:
            qs = qs.filter(category__iexact=cat)

        active = self.request.query_params.get('is_active')
        if active is not None:
            qs = qs.filter(is_active=active.lower() in ['true', '1'])
        return qs.order_by('category', 'name')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)

    def perform_destroy(self, instance):
        appts = SpaAppointment.objects.filter(treatment=instance)
        if appts.filter(charge__isnull=False).exists():
            raise serializers.ValidationError({"detail": "Cannot delete treatment with billed appointments on guest folios. Set it to Inactive instead."})
        other = SpaTreatment.objects.filter(branch=instance.branch, is_active=True).exclude(pk=instance.pk).first()
        if other:
            appts.update(treatment=other)
        else:
            appts.delete()
        instance.delete()


class SpaAppointmentViewSet(viewsets.ModelViewSet):
    """
    CRUD and Listing API for Spa Appointments.
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = SpaAppointmentSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = SpaAppointment.objects.all()
        elif branch:
            qs = SpaAppointment.objects.filter(branch=branch)
        else:
            qs = SpaAppointment.objects.none()

        date_str = self.request.query_params.get('date')
        if date_str:
            qs = qs.filter(date=date_str)

        st = self.request.query_params.get('status')
        if st:
            qs = qs.filter(status=st.upper())

        therapist_id = self.request.query_params.get('therapist_id')
        if therapist_id:
            qs = qs.filter(therapist_id=therapist_id)

        booking_id = self.request.query_params.get('booking_id')
        if booking_id:
            qs = qs.filter(Q(booking__id=booking_id) | Q(booking__booking_id=booking_id))

        return qs.select_related('treatment', 'therapist', 'booking', 'booking__room').order_by('date', 'start_time')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch, created_by=self.request.user)


class SpaPackageViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Spa Signature Retreat Packages.
    Gated by IsActiveHotelAdmin.
    """
    serializer_class = SpaPackageSerializer
    permission_classes = [IsActiveHotelAdmin]

    def get_queryset(self):
        branch = resolve_branch_for_request(self.request)
        if self.request.user.is_superuser and not self.request.query_params.get('branch_id') and not self.request.query_params.get('branch'):
            qs = SpaPackage.objects.all()
        elif branch:
            qs = SpaPackage.objects.filter(branch=branch)
        else:
            qs = SpaPackage.objects.none()

        active = self.request.query_params.get('is_active')
        if active is not None:
            qs = qs.filter(is_active=active.lower() in ['true', '1'])
        return qs.order_by('title')

    def perform_create(self, serializer):
        branch = resolve_branch_for_request(self.request)
        if not branch:
            raise serializers.ValidationError({"branch": "A valid branch with hotel module enabled is required."})
        serializer.save(branch=branch)


def ensure_rich_spa_catalog(branch, force=False):
    """
    Ensures that the given branch has rich database-backed Spa data (treatments, therapists, packages).
    Only runs initial seeding if the branch has NO catalog items at all (or force=True).
    Never resurrects items that an administrator has deleted.
    """
    if not branch:
        return

    # Check if branch already has any catalog items or appointments
    has_therapists = SpaTherapist.objects.filter(branch=branch).exists()
    has_treatments = SpaTreatment.objects.filter(branch=branch).exists()
    has_packages = SpaPackage.objects.filter(branch=branch).exists()
    has_appts = SpaAppointment.objects.filter(branch=branch).exists()

    if not force and (has_therapists or has_treatments or has_packages or has_appts):
        return
    # 1. Certified Specialists / Therapists
    therapists_data = [
        {
            'name': 'Dr. Ananya Sharma, BAMS',
            'title': 'Lead Ayurvedic Vaidya & Wellness Director',
            'experience_years': 14,
            'photo': 'https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?q=80&w=800&auto=format&fit=crop',
            'rating': Decimal('4.99'),
            'sessions_completed': 1850,
            'skills': 'Ayurvedic Pulse Diagnosis, Panchakarma, Marma Therapy',
            'languages': 'English, Hindi, Sanskrit',
            'categories': ['ayurveda'],
            'certifications': ['Doctor of Ayurvedic Medicine (BAMS)', 'Panchakarma Specialist', 'Shirodhara Master'],
            'specialties': ['Ayurvedic Shirodhara', 'Dosha Balancing', 'Holistic Marma Therapy'],
            'bio': 'With over 14 years of royal resort wellness leadership, Dr. Ananya specializes in classical Vedic diagnosis, customized healing oils, and stress-reversing Shirodhara rituals.',
            'availability': 'Available Today',
            'is_active': True,
        },
        {
            'name': 'Marcus Chen',
            'title': 'Senior Neuromuscular & Deep Tissue Specialist',
            'experience_years': 11,
            'photo': 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?q=80&w=800&auto=format&fit=crop',
            'rating': Decimal('4.97'),
            'sessions_completed': 2100,
            'skills': 'Neuromuscular Release, Trigger Point Therapy, Sports Massage',
            'languages': 'English, Mandarin',
            'categories': ['massages'],
            'certifications': ['Licensed Sports Massage Therapist', 'Myofascial Release Certified', 'Acupressure Master'],
            'specialties': ['Deep Tissue Muscle Recovery', 'Himalayan Hot Stones', 'Posture Correction'],
            'bio': 'Marcus has trained elite athletes and high-performance executives globally. His targeted neuromuscular strokes uncoil the deepest muscular knots with precision and ease.',
            'availability': 'Available Today',
            'is_active': True,
        },
        {
            'name': 'Elena Rostova',
            'title': 'Master Aesthetician & Holistic Facialist',
            'experience_years': 9,
            'photo': 'https://images.unsplash.com/photo-1580489944761-15a19d654956?q=80&w=800&auto=format&fit=crop',
            'rating': Decimal('4.98'),
            'sessions_completed': 1420,
            'skills': 'Cellular 24K Gold Facial, Cryo-Sculpting, Botanical Peels',
            'languages': 'English, French, Russian',
            'categories': ['facials'],
            'certifications': ['CIDESCO International Diploma', 'Advanced Cryo-Facialist', 'Aromatherapist IFA'],
            'specialties': ['24K Gold Cellular Facial', 'Lymphatic Facial Sculpting', 'Botanical Peels'],
            'bio': 'Trained in Paris and Switzerland, Elena blends medical-grade botanical actives with ancestral facial massage techniques to sculpt, de-puff, and illuminate your complexion.',
            'availability': 'Limited Slots',
            'is_active': True,
        },
        {
            'name': 'Priya Nambiar',
            'title': 'Senior Aromatherapist & Couples Ritual Curator',
            'experience_years': 10,
            'photo': 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?q=80&w=800&auto=format&fit=crop',
            'rating': Decimal('4.96'),
            'sessions_completed': 1680,
            'skills': 'Swedish Aromatherapy, Chakra Balancing, Couples Synchronized Rituals',
            'languages': 'English, Hindi, Malayalam',
            'categories': ['massages', 'couples'],
            'certifications': ['Certified Clinical Aromatherapist', 'Swedish Massage Master', 'Sound Healing Practitioner'],
            'specialties': ['Swedish Aromatherapy', 'Couples Retreats', 'Chakra Sound Harmonization'],
            'bio': 'Priya curates sensory sanctuaries where botanical aromas, warm carrier oils, and delicate flowing touch ease anxiety and open the heart center.',
            'availability': 'Available Today',
            'is_active': True,
        },
    ]

    # Clean any invalid placeholder test therapists
    SpaTherapist.objects.filter(branch=branch, name__iexact='Soft Drink').delete()

    for item in therapists_data:
        obj = SpaTherapist.objects.filter(branch=branch, name=item['name']).first()
        if not obj:
            first_n = item['name'].split()[0]
            obj = SpaTherapist.objects.filter(branch=branch, name__istartswith=first_n).first()
        if not obj:
            SpaTherapist.objects.create(branch=branch, **item)

    # 2. Treatments & Rituals
    treatments_data = [
        {
            'name': 'Royal Swedish Aromatherapy Massage',
            'slug': 'royal-swedish-aromatherapy',
            'category': 'massages',
            'tag': 'Signature Ritual',
            'duration_minutes': 60,
            'duration_options': [
                {'duration': 60, 'price': 3200, 'label': '60 Mins — ₹3,200'},
                {'duration': 90, 'price': 4400, 'label': '90 Mins — ₹4,400'},
                {'duration': 120, 'price': 5800, 'label': '120 Mins — ₹5,800'},
            ],
            'price': Decimal('3200.00'),
            'original_price': Decimal('3800.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.97'),
            'reviews_count': 248,
            'suite_type': 'VIP Private Suite',
            'short_description': 'A harmonious full-body therapy using organic botanical essential oils, gentle flowing effleurage, and targeted acupressure.',
            'full_description': 'Our most sought-after restorative ritual. Drawing upon classical European techniques harmonized with custom-blended cold-pressed essential oils (Lavender, Sweet Orange, and Himalayan Cedarwood), this massage melts away physical tension, improves lymphatic drainage, and induces deep meditative serenity.',
            'ritual_steps': [
                'Welcome foot cleansing with warm herbal compress and floral salts',
                'Olfactory sensory consultation to select your bespoke essential oil',
                'Full body rhythmic effleurage, gentle kneading, and joint mobilization',
                'Scalp and temple tension release with warm infused sandalwood drops',
                'Warm towel compress and herbal green tea elixir closing'
            ],
            'benefits': [
                'Relieves muscular tension and chronic fatigue',
                'Stimulates healthy blood circulation and lymph flow',
                'Calms central nervous system and promotes restorative sleep',
                'Deep skin nourishment with cold-pressed virgin oils'
            ],
            'suitable_for': ['Stress relief', 'First-time spa guests', 'General fatigue', 'Insomnia'],
            'cover_image': 'https://images.unsplash.com/photo-1544161515-4ab6ce6db874?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1544161515-4ab6ce6db874?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1600334089648-b0d9d3028eb2?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Access to Herbal Steam Sauna',
                'Complimentary Detox Infused Water',
                'Private Suite Shower with Organic Amenities',
                'Organic Herbal Tea Selection in Post-Ritual Lounge'
            ],
            'is_featured': True,
            'is_active': True,
        },
        {
            'name': 'Deep Tissue Muscle Recovery Therapy',
            'slug': 'deep-tissue-muscle-recovery',
            'category': 'massages',
            'tag': 'Bestseller',
            'duration_minutes': 60,
            'duration_options': [
                {'duration': 60, 'price': 3600, 'label': '60 Mins — ₹3,600'},
                {'duration': 90, 'price': 4900, 'label': '90 Mins — ₹4,900'},
            ],
            'price': Decimal('3600.00'),
            'original_price': Decimal('4200.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.95'),
            'reviews_count': 192,
            'suite_type': 'VIP Private Suite',
            'short_description': 'Intense therapeutic pressure targeting chronic knots, posture fatigue, and deep connective fascia using warm arnica extracts.',
            'full_description': 'Engineered for athletes, travelers, and individuals carrying chronic posture strain. Our senior neuromuscular therapists use slow, deliberate strokes and deep finger pressure along tendon lines to release chronic muscle adhesion, break down lactic acid, and restore full range of motion.',
            'ritual_steps': [
                'Targeted posture and muscle tension assessment',
                'Warm herbal heat pack placement to soften deep fascia',
                'Intensive trigger point therapy with wild arnica and eucalyptus balm',
                'Assisted passive stretching for shoulders, lower back, and hamstrings',
                'Cooling botanical mist and muscle recovery hydration elixir'
            ],
            'benefits': [
                'Breaks down deep-seated scar tissue and muscle adhesions',
                'Accelerates athletic recovery and eliminates stiffness',
                'Corrects postural strain caused by desk work or long flights',
                'Enhances flexibility and joint mobility'
            ],
            'suitable_for': ['Athletes & fitness enthusiasts', 'Chronic neck & shoulder pain', 'Frequent flyers'],
            'cover_image': 'https://images.unsplash.com/photo-1519823551278-64ac92734fb1?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1519823551278-64ac92734fb1?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1544161515-4ab6ce6db874?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Hydrotherapy Jacuzzi Pre-Soak (15 mins)',
                'Steam Room Access',
                'Electrolyte & Vitamin Herbal Infusion'
            ],
            'is_featured': True,
            'is_active': True,
        },
        {
            'name': 'Vedic Shirodhara & Abhyanga Healing',
            'slug': 'ayurvedic-shirodhara-abhyanga',
            'category': 'ayurveda',
            'tag': 'Ancient Healing',
            'duration_minutes': 90,
            'duration_options': [
                {'duration': 90, 'price': 5200, 'label': '90 Mins — ₹5,200'},
                {'duration': 120, 'price': 6800, 'label': '120 Mins — ₹6,800'},
            ],
            'price': Decimal('5200.00'),
            'original_price': Decimal('6000.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.99'),
            'reviews_count': 180,
            'suite_type': 'Ayurvedic Sanctum',
            'short_description': 'Sacred Ayurvedic ritual featuring synchronized warm medicated herb oil massage followed by continuous rhythmic oil flow over the third-eye chakra.',
            'full_description': 'An authentic five-thousand-year-old Vedic healing tradition practiced in its purest royal form. Commences with an Abhyanga whole-body warm herbal oil massage to balance the Vata, Pitta, and Kapha doshas, culminating in Shirodhara—a meditative, rhythmic pour of warm medicated herbal decoction continuously over the forehead (Ajna chakra) to dissolve mental chatter and induce profound theta-wave stillness.',
            'ritual_steps': [
                'Traditional Vedic prayer and dosha balance consultation',
                'Abhyanga full body rhythmic massage with warm Brahmi and Ashwagandha oil',
                'Shirodhara stream: continuous pour of warm medicated herbal oil on the forehead',
                'Gentle marma point stimulation around temples and crown',
                'Warm herbal steam wrap (Swedana) to draw out deep toxins',
                'Organic Ayurvedic turmeric saffron tea and candied amla'
            ],
            'benefits': [
                'Transcendent mental clarity, relief from anxiety and overthinking',
                'Alleviates chronic headaches, migraines, and insomnia',
                'Balances the neuroendocrine system and nervous vitality',
                'Deeply conditions the scalp, hair roots, and sensory organs'
            ],
            'suitable_for': ['Mental burnout', 'Severe insomnia', 'Spiritual grounding', 'Stress exhaustion'],
            'cover_image': 'https://images.unsplash.com/photo-1515377905703-c4788e51af15?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1515377905703-c4788e51af15?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1600334089648-b0d9d3028eb2?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Ayurvedic Vaidya (Doctor) Pulse Assessment',
                'Herbal Steam Swedana Chamber',
                'Pure Silk Spa Robe & Footwear',
                'Organic Ayurvedic Herbal Brew'
            ],
            'is_featured': True,
            'is_active': True,
        },
        {
            'name': 'Himalayan Hot Stone Harmony Ritual',
            'slug': 'himalayan-hot-stone-harmony',
            'category': 'massages',
            'tag': 'Ultra Relaxing',
            'duration_minutes': 90,
            'duration_options': [
                {'duration': 90, 'price': 4800, 'label': '90 Mins — ₹4,800'},
                {'duration': 120, 'price': 6200, 'label': '120 Mins — ₹6,200'},
            ],
            'price': Decimal('4800.00'),
            'original_price': Decimal('5500.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.96'),
            'reviews_count': 142,
            'suite_type': 'Thermal Basalt Suite',
            'short_description': 'Heated volcanic basalt river stones and organic amber essential oils placed along chakra energy vortexes to dissolve tension.',
            'full_description': 'Heated naturally tumbled volcanic basalt stones, rich in grounding mineral energy, are anointed with warm almond and amber essential oils. Therapists gently glide the smooth warm stones across your body as extensions of their hands, easing deep muscular tension without harsh pressure while stone placement on energy centers balances internal resonance.',
            'ritual_steps': [
                'Purifying floral water foot wash with natural Himalayan pink salt',
                'Energy chakra placement of heated volcanic basalt stones',
                'Continuous gliding stone massage with warm sandalwood amber oil',
                'Cold marble stone application on facial marma points for vascular tone',
                'Gentle temple and neck alignment with warm herbal compress'
            ],
            'benefits': [
                'Thermal penetration eases deep muscle tension 3x faster than traditional hands',
                'Balances energetic meridian lines and grounding centers',
                'Promotes cellular detoxification and metabolic relaxation',
                'Relieves symptoms of arthritis, fibromyalgia, and chronic stiffness'
            ],
            'suitable_for': ['Cold weather rejuvenation', 'Chronic stiffness', 'Deep relaxation seekers'],
            'cover_image': 'https://images.unsplash.com/photo-1507652313519-d4e9174996dd?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1507652313519-d4e9174996dd?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Access to Heated Vitality Jacuzzi Pool',
                'Infrared Sauna Session (15 Mins)',
                'Warm Spiced Herbal Infusion'
            ],
            'is_featured': False,
            'is_active': True,
        },
        {
            'name': '24K Gold & Caviar Cellular Radiance Facial',
            'slug': '24k-gold-radiance-cellular-facial',
            'category': 'facials',
            'tag': 'Royal Experience',
            'duration_minutes': 75,
            'duration_options': [
                {'duration': 75, 'price': 5500, 'label': '75 Mins — ₹5,500'},
                {'duration': 90, 'price': 6800, 'label': '90 Mins with Neck & Décolleté — ₹6,800'},
            ],
            'price': Decimal('5500.00'),
            'original_price': Decimal('6500.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.98'),
            'reviews_count': 165,
            'suite_type': 'Parisian Aesthetic Suite',
            'short_description': 'High-performance aesthetic facial utilizing pure 24-karat gold leaf, marine caviar extract, and cryo-sculpting rollers for luminous skin.',
            'full_description': 'An opulent anti-aging and illuminating facial designed to instantly restore skin elasticity and youthful glow. Incorporates ultrasonic pore purification, enzymatic papaya exfoliation, lymphatic facial drainage, and sheets of pure 24-karat gold leaf massaged into the dermal layer with peptide-rich marine caviar nectar.',
            'ritual_steps': [
                'Double sonic botanical cleansing with damask rose milk',
                'Gentle enzymatic papaya peeling and warm aromatic steam',
                'Ultrasonic pore deep extraction and oxygen mist infusion',
                'Application of genuine 24-karat cosmetic gold leaves and caviar elixir',
                'Cryo-globes lymphatic contour massage to firm and tighten',
                'Gold foil bio-collagen peel-off mask and antioxidant day shield'
            ],
            'benefits': [
                'Immediate radiant luminosity and glass-skin glow',
                'Stimulates collagen production and cell turnover',
                'Sculpts jawline and drains under-eye puffiness',
                'Deeply hydrates and plumps fine lines'
            ],
            'suitable_for': ['Pre-wedding preparation', 'Red carpet events', 'Anti-aging & skin firming'],
            'cover_image': 'https://images.unsplash.com/photo-1570172619644-dfd03ed5d881?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1570172619644-dfd03ed5d881?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1596178065887-1198b6148b2b?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Complimentary Skin Health Digital Analysis',
                'Decollete & Shoulder Tension Massage',
                'Collagen Elixir & Champagne Tasting'
            ],
            'is_featured': True,
            'is_active': True,
        },
        {
            'name': 'Thermal Hydrotherapy & Herbal Steam Detox',
            'slug': 'thermal-hydrotherapy-eucalyptus-steam',
            'category': 'steam-hydro',
            'tag': 'Bestseller',
            'duration_minutes': 45,
            'duration_options': [
                {'duration': 45, 'price': 2200, 'label': '45 Mins — ₹2,200'},
                {'duration': 60, 'price': 2900, 'label': '60 Mins with Sea Salt Scrub — ₹2,900'},
            ],
            'price': Decimal('2200.00'),
            'original_price': Decimal('2800.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('4.93'),
            'reviews_count': 118,
            'suite_type': 'Hydrothermal Pavilion',
            'short_description': 'Private thermal hydro-massage jacuzzi followed by cedarwood eucalyptus steam sauna to detoxify pores and soothe tired limbs.',
            'full_description': 'Immerse your body in high-pressure thermal mineral waters calibrated to 38°C with micro-bubble hydrojets targeting key pressure points. Conclude in our Nordic cedarwood steam chamber infused with crushed eucalyptus, spearmint, and medicinal forest pine vapors.',
            'ritual_steps': [
                'Warm rainfall shower rinse with organic olive Castile soap',
                'Mineral-enriched private hydrotherapy jacuzzi bath with targeted hydrojets',
                'Eucalyptus inhalation steam sauna to open airways and sweat out impurities',
                'Contrast ice-mist shower to boost circulation and tone skin',
                'Cooling cucumber eye compress and herbal electrolyte refreshment'
            ],
            'benefits': [
                'Purges environmental toxins through deep thermal perspiration',
                'Relieves respiratory congestion with eucalyptus aromatic vapors',
                'Decompresses spinal tension and joint pressure with buoyant hydrojets',
                'Boosts cellular immunity and micro-vascular circulation'
            ],
            'suitable_for': ['Detoxification', 'Post-workout recovery', 'Respiratory clarity'],
            'cover_image': 'https://images.unsplash.com/photo-1512290900672-1f5be5bf509f?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1512290900672-1f5be5bf509f?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Private Suite Changing Lounge',
                'Plush Terry Velour Robes and Slippers',
                'Chilled Eucalyptus Towels'
            ],
            'is_featured': False,
            'is_active': True,
        },
        {
            'name': 'Sacred Couples Rose & Champagne Retreat',
            'slug': 'sacred-couples-rose-champagne-retreat',
            'category': 'couples',
            'tag': 'Signature Ritual',
            'duration_minutes': 120,
            'duration_options': [
                {'duration': 120, 'price': 9500, 'label': '120 Mins (For 2 Guests) — ₹9,500'},
                {'duration': 150, 'price': 12500, 'label': '150 Mins with Private Jacuzzi & Canapés — ₹12,500'},
            ],
            'price': Decimal('9500.00'),
            'original_price': Decimal('12000.00'),
            'gst_percent': Decimal('18.00'),
            'rating': Decimal('5.00'),
            'reviews_count': 174,
            'suite_type': 'Presidential Couples Sanctuary',
            'short_description': 'An intimate VIP couples suite experience featuring side-by-side bespoke massages, rose petal hydro-soak, and champagne pairing.',
            'full_description': 'Created for anniversaries, honeymoons, and celebrations of shared love. Indulge in our private presidential couples sanctuary illuminated by soft candlelight and fragrant with fresh damask rose petals. Two master therapists deliver synchronized full-body massages, followed by a candlelit soak in a double marble sunken jacuzzi with chilled sparkling champagne and chef-crafted artisan truffles.',
            'ritual_steps': [
                'Welcome toast with chilled vintage Brut Champagne or sparkling botanicals',
                'Rose petal and sea salt foot ritual with pure jasmine essence',
                'Synchronized 75-minute full-body aromatherapy massages side-by-side',
                'Private sunken jacuzzi soak infused with rose essential oil and botanical milk',
                'Relaxation lounge time with chocolate-dipped strawberries and artisan macaroons'
            ],
            'benefits': [
                'Unforgettable shared intimacy and romantic connection',
                'Simultaneous dual relaxation under guidance of two master therapists',
                'Exclusive private suite with dedicated butler and private en-suite shower',
                'Complete sensory celebration for couples'
            ],
            'suitable_for': ['Honeymooners', 'Anniversaries', 'Romantic weekends', 'Gift experiences'],
            'cover_image': 'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop',
            'gallery_images': [
                'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop',
                'https://images.unsplash.com/photo-1544161515-4ab6ce6db874?q=80&w=1200&auto=format&fit=crop'
            ],
            'included_amenities': [
                'Exclusive Presidential Couples Suite with Private Marble Jacuzzi',
                'Chilled Bottle of Moët & Chandon or Premium Sparkling Wine',
                'Chef Gourmet Chocolate Truffles & Strawberries',
                'Monogrammed Luxury Keepsake Bathrobes'
            ],
            'is_featured': True,
            'is_active': True,
        },
    ]

    for item in treatments_data:
        obj = SpaTreatment.objects.filter(branch=branch, name=item['name']).first()
        if not obj:
            # Check by slug or similar name
            obj = SpaTreatment.objects.filter(branch=branch, slug=item['slug']).first()
        if not obj:
            first_n = item['name'].split()[0]
            obj = SpaTreatment.objects.filter(branch=branch, name__istartswith=first_n, category=item['category']).first()
        if not obj:
            SpaTreatment.objects.create(branch=branch, **item)

    # 3. Signature Multi-Therapy Packages
    packages_data = [
        {
            'title': 'Half-Day Royal Detox Sanctuary',
            'slug': 'half-day-royal-detox-rejuvenation',
            'tag': 'Signature Package',
            'duration': '3.5 Hours',
            'duration_minutes': 210,
            'price': Decimal('8800.00'),
            'original_price': Decimal('11000.00'),
            'image': 'https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1200&auto=format&fit=crop',
            'description': 'The ultimate whole-body reset. Combines thermal hydrotherapy, full-body exfoliation, Swedish deep aromatherapy massage, and mini gold radiance facial.',
            'inclusions': [
                '30 Mins Eucalyptus Steam & Jacuzzi Pre-Soak',
                '30 Mins Himalayan Sea Salt & Herb Body Polish',
                '60 Mins Royal Swedish Aromatherapy Massage',
                '45 Mins Express Botanical Radiance Facial',
                'Healthy Spa Lunch with Cold-Pressed Juices at Poolside Pavilion'
            ],
            'popular': True,
            'is_active': True,
        },
        {
            'title': 'Bridal & Celebration Radiance Ritual',
            'slug': 'bridal-glow-celebration-ritual',
            'tag': 'Wedding & Gala Special',
            'duration': '4 Hours',
            'duration_minutes': 240,
            'price': Decimal('10500.00'),
            'original_price': Decimal('13500.00'),
            'image': 'https://images.unsplash.com/photo-1570172619644-dfd03ed5d881?q=80&w=1200&auto=format&fit=crop',
            'description': 'Head-to-toe beautification designed for brides, bridal parties, or special gala events. Leaves skin illuminated with pure diamond and 24K gold nectars.',
            'inclusions': [
                '75 Mins 24K Gold Cellular Radiance Facial',
                '60 Mins Silken Jasmine Body Wrap & Hydration',
                '45 Mins Scalp Brahmi Hair Spa & Head Massage',
                '30 Mins Manicure & Pedicure Luxury Polish',
                'Glass of Chilled French Champagne & Artisan Macarons'
            ],
            'popular': False,
            'is_active': True,
        },
        {
            'title': 'Trans-Globe Jet-Lag Recovery Protocol',
            'slug': 'trans-globe-jet-lag-reset',
            'tag': 'Traveler Choice',
            'duration': '2.5 Hours',
            'duration_minutes': 150,
            'price': Decimal('6800.00'),
            'original_price': Decimal('8500.00'),
            'image': 'https://images.unsplash.com/photo-1519823551278-64ac92734fb1?q=80&w=1200&auto=format&fit=crop',
            'description': 'Specifically engineered for long-haul flight passengers to reset circadian rhythms, eliminate lower leg edema, and ease lower back compression.',
            'inclusions': [
                '60 Mins Deep Tissue & Lymphatic Drainage Massage',
                '30 Mins Thermal Jacuzzi Jet Soak with Epsom Salts',
                '30 Mins Acupressure Foot Reflexology Therapy',
                'Melatonin & Chamomile Sleep Induction Elixir'
            ],
            'popular': True,
            'is_active': True,
        },
    ]

    for item in packages_data:
        p_obj = SpaPackage.objects.filter(branch=branch, title=item['title']).first()
        if not p_obj:
            p_obj = SpaPackage.objects.filter(branch=branch, slug=item['slug']).first()
        if not p_obj:
            SpaPackage.objects.create(branch=branch, **item)


class PublicSpaCatalogView(APIView):
    """
    GET /api/public/hotel/spa/catalog/ or /api/spa/catalog/
    Returns active spa treatments, certified master healers/therapists, and signature retreat packages.
    Backed completely by dynamic database records.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, branch_id=None):
        branch_val = branch_id or request.query_params.get('branch_id') or request.query_params.get('branch_slug') or request.query_params.get('restaurant_slug')
        branch = None

        if branch_val:
            if str(branch_val).isdigit():
                branch = RestaurantBranch.objects.filter(id=int(branch_val)).first()
            else:
                branch = RestaurantBranch.objects.filter(slug=branch_val).first() or RestaurantBranch.objects.filter(restaurant__slug=branch_val, has_hotel_module=True).first()

        if not branch:
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()

        if not branch:
            return Response({"branch_id": None, "treatments": [], "therapists": [], "packages": [], "time_slots": []})

        # Ensure database is rich and up-to-date with full service & specialist data
        ensure_rich_spa_catalog(branch)

        treatments = list(SpaTreatment.objects.filter(branch=branch, is_active=True).order_by('category', 'name'))
        therapists = list(SpaTherapist.objects.filter(branch=branch, is_active=True).order_by('name'))
        packages = list(SpaPackage.objects.filter(branch=branch, is_active=True).order_by('-popular', 'price'))

        time_slots = [
            "09:00 AM", "09:30 AM", "10:00 AM", "10:30 AM", "11:00 AM", "11:30 AM",
            "12:00 PM", "12:30 PM", "01:00 PM", "01:30 PM", "02:00 PM", "02:30 PM",
            "03:00 PM", "03:30 PM", "04:00 PM", "04:30 PM", "05:00 PM", "05:30 PM",
            "06:00 PM", "06:30 PM", "07:00 PM", "07:30 PM", "08:00 PM", "08:30 PM",
        ]

        return Response({
            "branch_id": branch.id,
            "branch_name": branch.name,
            "treatments": SpaTreatmentSerializer(treatments, many=True).data,
            "therapists": SpaTherapistSerializer(therapists, many=True).data,
            "packages": SpaPackageSerializer(packages, many=True).data,
            "time_slots": time_slots,
            "operating_hours": {
                "open": "09:00",
                "close": "21:00",
                "open_display": "09:00 AM",
                "close_display": "09:00 PM"
            }
        })


class PublicSpaBookingCreateView(APIView):
    """
    POST /api/public/hotel/spa/book/ or /api/spa/bookings/
    Accepts SpaBookingPayload from frontend, validates clash, resolves resident room or walk-in,
    creates SpaAppointment, and returns reference code.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PaymentRateThrottle]

    def post(self, request):
        from hotel import ops
        data = request.data or {}

        customer_name = (data.get('customer_name') or data.get('guest_name') or '').strip()
        phone = (data.get('phone') or data.get('guest_phone') or '').strip()
        email = (data.get('email') or '').strip()
        service_id = data.get('service_id') or data.get('treatment_id')
        service_name = (data.get('service_name') or '').strip()
        date_str = data.get('date')
        time_slot = (data.get('time_slot') or data.get('start_time') or '10:00 AM').strip()
        duration_minutes = data.get('duration_minutes')
        therapist_id = data.get('therapist_id')
        therapist_name = (data.get('therapist_name') or '').strip()
        is_hotel_guest = bool(data.get('is_hotel_guest', False))
        room_number = (data.get('room_number') or '').strip()
        aroma = data.get('aroma_oil_preference') or ''
        pressure = data.get('pressure_preference') or ''
        special_requests = data.get('special_requests') or ''

        if not customer_name:
            return Response({"error": "Guest name is required."}, status=400)
        if not phone:
            return Response({"error": "Guest phone number is required."}, status=400)

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
            branch = RestaurantBranch.objects.filter(has_hotel_module=True).first() or RestaurantBranch.objects.first()

        if not branch:
            return Response({"error": "No hotel branch available for spa booking."}, status=400)

        # 2. Parse Date
        today = timezone.localdate()
        target_date = today
        if date_str:
            try:
                target_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                target_date = today
        if target_date < today:
            target_date = today

        # 3. Parse Start Time (support "10:30 AM", "10:30", "14:00", etc.)
        parsed_time = datetime.time(10, 0)
        try:
            clean_ts = time_slot.upper()
            if 'AM' in clean_ts or 'PM' in clean_ts:
                parsed_time = datetime.datetime.strptime(clean_ts, "%I:%M %p").time()
            elif ':' in clean_ts:
                parts = clean_ts.split(':')
                parsed_time = datetime.time(int(parts[0]), int(parts[1][:2]))
        except Exception:
            parsed_time = datetime.time(10, 0)

        # 4. Resolve Treatment
        treatment = None
        if service_id and str(service_id).isdigit():
            treatment = SpaTreatment.objects.filter(branch=branch, id=int(service_id), is_active=True).first()
        if not treatment and service_name:
            treatment = SpaTreatment.objects.filter(branch=branch, name__icontains=service_name, is_active=True).first()
        if not treatment:
            treatment = SpaTreatment.objects.filter(branch=branch, is_active=True).first()
        if not treatment:
            treatment = SpaTreatment.objects.create(
                branch=branch,
                name=service_name or "Swedish Aromatherapy Balance",
                category="massages",
                duration_minutes=int(duration_minutes or 60),
                price=Decimal(str(data.get('total_amount') or '3200.00')),
                gst_percent=Decimal('18.00'),
                is_active=True
            )

        # 5. Resolve Therapist
        therapist = None
        if therapist_id and str(therapist_id).isdigit():
            therapist = SpaTherapist.objects.filter(branch=branch, id=int(therapist_id), is_active=True).first()
        elif therapist_name and therapist_name.lower() not in ['any', 'auto', 'master specialist on duty']:
            first_name = therapist_name.split()[0]
            therapist = SpaTherapist.objects.filter(branch=branch, name__icontains=first_name, is_active=True).first()

        dur_mins = int(duration_minutes or treatment.duration_minutes or 60)
        active_therapists = list(SpaTherapist.objects.filter(branch=branch, is_active=True))
        if not active_therapists:
            therapist = SpaTherapist.objects.create(
                branch=branch,
                name="Master Specialist on Duty",
                skills="Holistic Wellness & Bodywork",
                categories=[treatment.category or 'massages'],
                is_active=True
            )
            active_therapists = [therapist]

        # Prioritize specialists qualified for this treatment's category
        cat_key = (treatment.category or '').strip()
        qualified_therapists = [th for th in active_therapists if th.can_perform_category(cat_key)]
        pool = qualified_therapists if qualified_therapists else active_therapists

        # If therapist was specified manually, check if valid and free
        if therapist and not ops.spa_clash(branch, therapist, target_date, parsed_time, dur_mins):
            pass  # keep requested therapist
        else:
            # Auto-assign from qualified pool without time clashes
            assigned = None
            for th in pool:
                if not ops.spa_clash(branch, th, target_date, parsed_time, dur_mins):
                    assigned = th
                    break
            therapist = assigned or (pool[0] if pool else active_therapists[0])

        # 6. Resolve Linked Room Booking (if guest is staying at hotel)
        room_booking = None
        if is_hotel_guest:
            if room_number:
                room_booking = RoomBooking.objects.filter(
                    branch=branch,
                    room__room_number=room_number,
                    booking_status__in=['CHECKED_IN', 'CONFIRMED']
                ).order_by('-id').first()
            if not room_booking:
                room_booking = RoomBooking.objects.filter(
                    branch=branch,
                    guest_phone=phone,
                    booking_status__in=['CHECKED_IN', 'CONFIRMED']
                ).order_by('-id').first()

        # 7. Construct Notes
        notes_parts = []
        if email:
            notes_parts.append(f"Email: {email}")
        if is_hotel_guest and room_number:
            notes_parts.append(f"Room: {room_number}")
        if aroma:
            notes_parts.append(f"Aroma: {aroma}")
        if pressure:
            notes_parts.append(f"Pressure: {pressure}")
        if special_requests:
            notes_parts.append(f"Requests: {special_requests}")
        combined_notes = " · ".join(notes_parts)[:255]

        # 8. Create Appointment
        user = request.user if request.user.is_authenticated else None
        appt, err = ops.book_spa(
            branch=branch,
            user=user,
            treatment=treatment,
            therapist=therapist,
            date=target_date,
            start=parsed_time,
            guest_name=customer_name,
            guest_phone=phone,
            booking=room_booking,
            notes=combined_notes
        )

        if err or not appt:
            appt = SpaAppointment.objects.create(
                branch=branch,
                treatment=treatment,
                therapist=therapist,
                date=target_date,
                start_time=parsed_time,
                duration_minutes=dur_mins,
                booking=room_booking,
                guest_name=customer_name,
                guest_phone=phone,
                price=treatment.price,
                gst_percent=treatment.gst_percent,
                notes=combined_notes,
                created_by=user
            )

        ref_code = appt.reference_id

        return Response({
            "success": True,
            "booking_id": ref_code,
            "reference": ref_code,
            "appointment_id": appt.id,
            "treatment_name": appt.treatment.name,
            "treatment_category": appt.treatment.category,
            "therapist_name": appt.therapist.name,
            "date": appt.date.isoformat(),
            "time_slot": appt.start_time.strftime("%I:%M %p"),
            "duration_minutes": appt.duration_minutes,
            "customer_name": appt.guest_name,
            "phone": appt.guest_phone,
            "email": email,
            "room_number": appt.booking.room.room_number if appt.booking and appt.booking.room else (room_number or None),
            "price": float(appt.price),
            "gst_percent": float(appt.gst_percent),
            "total_amount": float(appt.total),
            "status": appt.status,
            "status_display": appt.get_status_display(),
            "message": f"Spa treatment booked successfully! Reference code: {ref_code}."
        }, status=status.HTTP_201_CREATED)


class PublicSpaBookingDetailView(APIView):
    """
    GET /api/public/hotel/spa/booking/<ref_id>/ or /api/spa/bookings/<ref_id>/
    Allows public status checking and voucher verification for a spa appointment.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PublicLookupThrottle]

    def get(self, request, ref_id):
        appt = None
        if str(ref_id).isdigit():
            appt = SpaAppointment.objects.filter(id=int(ref_id)).first()
        if not appt:
            parts = str(ref_id).split('-')
            if len(parts) >= 3 and parts[-1].isdigit():
                appt = SpaAppointment.objects.filter(id=int(parts[-1])).first()

        if not appt:
            return Response({"error": "Spa appointment not found."}, status=404)

        data = SpaAppointmentSerializer(appt).data
        return Response({
            "success": True,
            "appointment": data
        })

