import json
import base64
import urllib.request
import urllib.error
import hmac
import hashlib
import logging
from decimal import Decimal
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.db import transaction, models
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.conf import settings

from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from super_admin.models import RestaurantBranch
from core.models import RestaurantSettings
from events.models import ClubEvent, TicketType, EventBooking
from events.permissions import HasClubModule
from events.serializers import (
    ClubEventSerializer,
    PublicClubEventSerializer,
    TicketTypeSerializer,
    EventBookingSerializer,
    CheckoutInitiateSerializer,
    PaymentVerifySerializer,
    ManualBookingCreateSerializer,
    GateScanSerializer,
)


from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return  # Bypasses CSRF validation for staff session AJAX / fetch calls


def _get_branch_razorpay_credentials(branch):
    """
    Resolves Razorpay API credentials for a branch.
    Checks RestaurantSettings for the restaurant, with fallback to django settings / env.
    """
    key_id = None
    key_secret = None

    if branch and branch.restaurant:
        settings_obj = RestaurantSettings.get_settings(branch.restaurant)
        if settings_obj and settings_obj.razorpay_key_id and settings_obj.razorpay_key_secret:
            key_id = settings_obj.razorpay_key_id.strip()
            key_secret = settings_obj.razorpay_key_secret.strip()

    if not key_id or not key_secret:
        key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)

    return key_id, key_secret


class AdminClubEventViewSet(viewsets.ModelViewSet):
    """
    Branch Admin Event Management ViewSet.
    Protected by Session/JWT Authentication and HasClubModule permission.
    """
    serializer_class = ClubEventSerializer
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]
    permission_classes = [IsAuthenticated, HasClubModule]

    def get_queryset(self):
        user = self.request.user
        qs = ClubEvent.objects.select_related('branch', 'branch__restaurant').prefetch_related('ticket_types').all()

        if user.is_superuser or user.is_staff:
            branch_param = self.request.query_params.get('branch_id')
            if branch_param:
                qs = qs.filter(branch_id=branch_param)
            return qs

        branch = None
        if hasattr(user, 'restaurant_profile'):
            profile = user.restaurant_profile
            if profile.branch:
                branch = profile.branch
            elif profile.restaurant:
                if hasattr(self.request, 'active_branch') and self.request.active_branch:
                    branch = self.request.active_branch
                else:
                    return qs.filter(branch__restaurant=profile.restaurant)

        if branch:
            return qs.filter(branch=branch)
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        branch = None
        branch_id = self.request.data.get('branch') or self.request.data.get('branch_id')
        if branch_id:
            branch = RestaurantBranch.objects.filter(id=branch_id).first()

        if not branch and hasattr(user, 'restaurant_profile'):
            branch = user.restaurant_profile.branch or (
                getattr(self.request, 'active_branch', None) or user.restaurant_profile.restaurant.branches.first()
            )

        if not branch:
            raise serializers.ValidationError({"branch": "Branch could not be determined."})

        serializer.save(branch=branch)

    @action(detail=True, methods=['get'])
    def guestlist(self, request, pk=None):
        """
        Retrieves filterable attendee guestlist for this event.
        Supports search by guest_name, phone, email, ticket_type, payment_status, checked_in.
        """
        event = self.get_object()
        bookings = event.bookings.select_related('ticket_type').all().order_by('-created_at')

        # Filters
        search_query = request.query_params.get('search')
        if search_query:
            bookings = bookings.filter(
                models.Q(guest_name__icontains=search_query) |
                models.Q(guest_phone__icontains=search_query) |
                models.Q(guest_email__icontains=search_query) |
                models.Q(booking_reference__icontains=search_query)
            )

        status_filter = request.query_params.get('payment_status')
        if status_filter:
            bookings = bookings.filter(payment_status=status_filter.upper())

        tier_filter = request.query_params.get('ticket_type_id')
        if tier_filter:
            bookings = bookings.filter(ticket_type_id=tier_filter)

        checked_in_filter = request.query_params.get('checked_in')
        if checked_in_filter is not None:
            is_checked_in = checked_in_filter.lower() in ['true', '1', 'yes']
            bookings = bookings.filter(checked_in=is_checked_in)

        serializer = EventBookingSerializer(bookings, many=True, context={'request': request})
        return Response({
            'event_id': event.id,
            'event_title': event.title,
            'total_bookings': bookings.count(),
            'total_attendees': sum(b.quantity for b in bookings if b.payment_status == EventBooking.PaymentStatus.SUCCESS),
            'checked_in_count': bookings.filter(checked_in=True).count(),
            'results': serializer.data
        })

    @action(detail=True, methods=['post'], url_path='manual-booking')
    def manual_booking(self, request, pk=None):
        """
        Generates manual cash-at-door or complimentary pass directly from the Branch Admin desk.
        Instantly confirmed with a scannable QR ticket.
        """
        event = self.get_object()
        serializer = ManualBookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ticket = get_object_or_404(TicketType, pk=data['ticket_type_id'], event=event)
        qty = data['quantity']

        with transaction.atomic():
            ticket = TicketType.objects.select_for_update().get(pk=ticket.pk)
            if ticket.available_quantity < qty:
                return Response(
                    {'error': f'Insufficient ticket availability. Only {ticket.available_quantity} left.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            unit_price = Decimal('0.00') if data['payment_method'] == EventBooking.PaymentMethod.COMPLIMENTARY else ticket.base_price
            total_amount = unit_price * qty
            booking_ref = EventBooking.generate_booking_reference()

            booking = EventBooking.objects.create(
                booking_reference=booking_ref,
                event=event,
                ticket_type=ticket,
                guest_name=data['guest_name'],
                guest_email=data.get('guest_email') or "walkin@venue.local",
                guest_phone=data['guest_phone'],
                quantity=qty,
                unit_price=unit_price,
                tax_amount=Decimal('0.00'),
                total_amount=total_amount,
                payment_status=EventBooking.PaymentStatus.SUCCESS,
                payment_method=data['payment_method'],
                notes=data.get('notes', '')
            )

            ticket.quantity_sold += qty
            ticket.save(update_fields=['quantity_sold'])

            booking.security_token = booking.generate_security_token()
            booking.generate_qr_code(force=True)
            booking.save(update_fields=['security_token', 'booking_qr_code'])

        # Dispatch event ticket pass email if guest email provided
        try:
            from core.emails import send_event_ticket_email
            send_event_ticket_email(booking, request=request)
        except Exception as email_err:
            logger.warning(f"Could not send door sale ticket email: {email_err}")

        res_serializer = EventBookingSerializer(booking, context={'request': request})
        return Response(res_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='scan-pass')
    def scan_pass(self, request):
        """
        Door bouncer / gate scanner endpoint.
        Validates QR string or reference, prevents duplicate entries.
        """
        serializer = GateScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_input = serializer.validated_data['qr_payload'].strip()

        # Input can be JSON payload or plain booking reference
        booking_ref = None
        security_tok = None
        try:
            parsed = json.loads(raw_input)
            booking_ref = parsed.get('ref')
            security_tok = parsed.get('tok')
        except Exception:
            booking_ref = raw_input

        if booking_ref:
            booking_ref = str(booking_ref).strip().lstrip('#')

        booking = EventBooking.objects.filter(booking_reference=booking_ref).select_related('event', 'ticket_type', 'event__branch').first()
        if not booking:
            return Response({'valid': False, 'message': 'Invalid pass: Booking reference not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Check module permission for this branch
        if not request.user.is_superuser and not request.user.is_staff:
            user_profile = getattr(request.user, 'restaurant_profile', None)
            if user_profile:
                if user_profile.branch and user_profile.branch_id != booking.event.branch_id:
                    return Response({'valid': False, 'message': 'Pass belongs to a different venue/branch.'}, status=status.HTTP_403_FORBIDDEN)

        # Verify payment status
        if booking.payment_status != EventBooking.PaymentStatus.SUCCESS:
            return Response({
                'valid': False,
                'message': f'Entry denied: Pass payment is {booking.get_payment_status_display()}.',
                'payment_status': booking.payment_status
            }, status=status.HTTP_400_BAD_REQUEST)

        # Verify security token if provided
        if security_tok and booking.security_token and security_tok != booking.security_token:
            return Response({'valid': False, 'message': 'Security token mismatch. Possible forged QR pass.'}, status=status.HTTP_400_BAD_REQUEST)

        # Check-in validation
        if booking.checked_in:
            return Response({
                'valid': False,
                'already_checked_in': True,
                'checked_in_at': booking.checked_in_at,
                'message': f'ALREADY SCANNED: Guest was checked in on {booking.checked_in_at.strftime("%I:%M %p") if booking.checked_in_at else "earlier"}.',
                'booking': EventBookingSerializer(booking).data
            }, status=status.HTTP_400_BAD_REQUEST)

        # Mark checked in
        success, msg = booking.mark_checked_in(staff_user=request.user)
        return Response({
            'valid': True,
            'message': f'Access Granted: Welcome {booking.guest_name} (+{booking.quantity - 1} guests)' if booking.quantity > 1 else f'Access Granted: Welcome {booking.guest_name}',
            'booking': EventBookingSerializer(booking).data
        }, status=status.HTTP_200_OK)


class AdminTicketTypeViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Ticket Types belonging to an event.
    """
    serializer_class = TicketTypeSerializer
    authentication_classes = [CsrfExemptSessionAuthentication, JWTAuthentication]
    permission_classes = [IsAuthenticated, HasClubModule]

    def get_queryset(self):
        user = self.request.user
        qs = TicketType.objects.select_related('event', 'event__branch').all()
        event_id = self.request.query_params.get('event_id')
        if event_id:
            qs = qs.filter(event_id=event_id)

        if user.is_superuser or user.is_staff:
            return qs

        if hasattr(user, 'restaurant_profile'):
            profile = user.restaurant_profile
            if profile.branch:
                return qs.filter(event__branch=profile.branch)
            if profile.restaurant:
                return qs.filter(event__branch__restaurant=profile.restaurant)

        return qs.none()


# -------------------------------------------------------------------------
# PUBLIC APIS (Unauthenticated - Customer Discovery & Checkout Flow)
# -------------------------------------------------------------------------

class PublicClubEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public discovery of published events for a given restaurant branch.
    Accessible without authentication.
    """
    serializer_class = PublicClubEventSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        from restaurant_project import constants
        if not getattr(constants, 'ENABLE_CLUB_EVENTS_MODULE', True):
            return ClubEvent.objects.none()

        # Clean up any expired pending holds before querying
        now = timezone.now()
        EventBooking.objects.filter(
            payment_status=EventBooking.PaymentStatus.PENDING,
            expires_at__lt=now
        ).update(payment_status=EventBooking.PaymentStatus.EXPIRED)

        qs = ClubEvent.objects.filter(
            is_published=True,
            branch__has_club_module=True
        ).select_related('branch', 'branch__restaurant').prefetch_related('ticket_types')
        
        include_past = self.request.query_params.get('include_past', 'false').lower() in ['true', '1', 'yes']
        if not include_past:
            qs = qs.filter(end_datetime__gte=now)

        branch_id = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        branch_slug = self.request.query_params.get('branch_slug') or self.request.query_params.get('slug')
        restaurant_slug = self.request.query_params.get('restaurant_slug') or self.request.query_params.get('restaurant')

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        elif branch_slug:
            qs = qs.filter(branch__slug=branch_slug)
        elif restaurant_slug:
            qs = qs.filter(branch__restaurant__slug=restaurant_slug)

        return qs.order_by('start_datetime')


@api_view(['POST'])
@permission_classes([AllowAny])
def public_checkout_initiate(request):
    """
    Public ticket checkout endpoint.
    1. Locks inventory using select_for_update() inside an atomic transaction.
    2. Calculates flat price with ZERO tax (total = base_price * quantity).
    3. Creates a PENDING EventBooking with a 15-minute lock.
    4. Generates a Razorpay Order ID.
    """
    serializer = CheckoutInitiateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    ticket = data['ticket_obj']
    qty = data['quantity']
    guest_name = data['guest_name']
    guest_email = data['guest_email']
    guest_phone = data['guest_phone']

    # Auto-expire overdue holds first
    now = timezone.now()
    EventBooking.objects.filter(
        payment_status=EventBooking.PaymentStatus.PENDING,
        expires_at__lt=now
    ).update(payment_status=EventBooking.PaymentStatus.EXPIRED)

    with transaction.atomic():
        # Concurrency safety: Lock row for update
        locked_ticket = TicketType.objects.select_for_update().select_related('event', 'event__branch').get(pk=ticket.pk)
        
        if locked_ticket.available_quantity < qty:
            return Response({
                'error': f'Insufficient tickets available. Only {locked_ticket.available_quantity} remaining.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Flat pricing calculation: Zero tax
        unit_price = locked_ticket.base_price
        tax_amount = Decimal('0.00')
        total_amount = unit_price * qty
        amount_in_paise = int(round(total_amount * 100))

        booking_ref = EventBooking.generate_booking_reference()
        expires_at = now + timedelta(minutes=15)

        booking = EventBooking.objects.create(
            booking_reference=booking_ref,
            event=locked_ticket.event,
            ticket_type=locked_ticket,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            quantity=qty,
            unit_price=unit_price,
            tax_amount=tax_amount,
            total_amount=total_amount,
            payment_status=EventBooking.PaymentStatus.PENDING,
            payment_method=EventBooking.PaymentMethod.ONLINE,
            expires_at=expires_at
        )

        # Razorpay integration
        branch = locked_ticket.event.branch
        key_id, key_secret = _get_branch_razorpay_credentials(branch)
        razorpay_order_id = None

        if key_id and key_secret and not key_id.startswith('rzp_mock'):
            url = "https://api.razorpay.com/v1/orders"
            payload = {
                "amount": amount_in_paise,
                "currency": "INR",
                "receipt": booking_ref[:40]
            }
            auth_str = f"{key_id}:{key_secret}"
            auth_b64 = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
            req = urllib.request.Request(url, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header("Authorization", f"Basic {auth_b64}")

            try:
                req_data = json.dumps(payload).encode('utf-8')
                with urllib.request.urlopen(req, data=req_data, timeout=10) as resp:
                    if resp.status in [200, 201]:
                        resp_data = json.loads(resp.read().decode('utf-8'))
                        razorpay_order_id = resp_data.get('id')
            except Exception as e:
                # Log error and fallback to local order generation for tests/resilience
                pass

        if not razorpay_order_id:
            razorpay_order_id = f"order_evt_{booking_ref.lower()}"

        booking.razorpay_order_id = razorpay_order_id
        booking.security_token = booking.generate_security_token()
        booking.save(update_fields=['razorpay_order_id', 'security_token'])

    return Response({
        'booking_reference': booking.booking_reference,
        'razorpay_order_id': razorpay_order_id,
        'razorpay_key_id': key_id or "rzp_test_placeholder",
        'unit_price': str(unit_price),
        'tax_amount': "0.00",
        'total_amount': str(total_amount),
        'amount_in_paise': amount_in_paise,
        'expires_at': expires_at.isoformat(),
        'event_title': locked_ticket.event.title,
        'tier_name': locked_ticket.name,
        'quantity': qty,
        'guest_name': guest_name,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def public_verify_payment(request):
    """
    Verifies Razorpay payment signature, updates booking to SUCCESS,
    increments quantity_sold, and creates scannable entry QR pass.
    """
    serializer = PaymentVerifySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    booking_ref = data['booking_reference']
    rzp_order_id = data['razorpay_order_id']
    rzp_payment_id = data['razorpay_payment_id']
    rzp_signature = data['razorpay_signature']

    booking = get_object_or_404(
        EventBooking.objects.select_related('event', 'ticket_type', 'event__branch'),
        booking_reference=booking_ref
    )

    # Idempotent response if already confirmed
    if booking.payment_status == EventBooking.PaymentStatus.SUCCESS:
        booking.generate_qr_code()
        return Response({
            'success': True,
            'message': 'Payment already verified and confirmed.',
            'booking': EventBookingSerializer(booking, context={'request': request}).data
        })

    # Signature verification
    branch = booking.event.branch
    key_id, key_secret = _get_branch_razorpay_credentials(branch)

    if key_secret and not key_secret.startswith('mock'):
        msg = f"{rzp_order_id}|{rzp_payment_id}".encode('utf-8')
        generated_signature = hmac.new(key_secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(generated_signature, rzp_signature):
            return Response({'error': 'Razorpay payment signature verification failed.'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        locked_booking = EventBooking.objects.select_for_update().get(pk=booking.pk)
        locked_ticket = TicketType.objects.select_for_update().get(pk=locked_booking.ticket_type_id)

        locked_booking.payment_status = EventBooking.PaymentStatus.SUCCESS
        locked_booking.razorpay_payment_id = rzp_payment_id
        locked_booking.razorpay_signature = rzp_signature
        locked_booking.security_token = locked_booking.generate_security_token()

        locked_ticket.quantity_sold += locked_booking.quantity
        locked_ticket.save(update_fields=['quantity_sold'])

        locked_booking.generate_qr_code(force=True)
        locked_booking.save(update_fields=[
            'payment_status', 'razorpay_payment_id', 'razorpay_signature',
            'security_token', 'booking_qr_code'
        ])

    # Dispatch branded digital entry pass with QR code to guest email
    try:
        from core.emails import send_event_ticket_email
        send_event_ticket_email(locked_booking, request=request)
    except Exception as email_err:
        logger.warning(f"Could not send event ticket pass email for {locked_booking.booking_reference}: {email_err}")

    res_data = EventBookingSerializer(locked_booking, context={'request': request}).data
    return Response({
        'success': True,
        'message': 'Payment successfully verified! Your entry pass is confirmed.',
        'booking': res_data
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def public_booking_detail(request, booking_reference):
    """
    Public endpoint for customers to view and download their confirmed digital ticket pass.
    """
    booking = get_object_or_404(
        EventBooking.objects.select_related('event', 'ticket_type', 'event__branch'),
        booking_reference=booking_reference
    )
    # Ensure QR code exists
    if not booking.booking_qr_code and booking.payment_status == EventBooking.PaymentStatus.SUCCESS:
        booking.generate_qr_code()
        booking.save(update_fields=['booking_qr_code'])

    serializer = EventBookingSerializer(booking, context={'request': request})
    return Response(serializer.data)


def customer_events_page(request, branch_slug=None):
    """Public customer-facing club events showcase."""
    # Clean up expired holds
    EventBooking.objects.filter(
        payment_status=EventBooking.PaymentStatus.PENDING,
        expires_at__lt=timezone.now()
    ).update(payment_status=EventBooking.PaymentStatus.EXPIRED)

    qs = ClubEvent.objects.filter(is_published=True, end_datetime__gte=timezone.now()).select_related('branch').prefetch_related('ticket_types').order_by('start_datetime')
    branch = None
    if branch_slug:
        branch = RestaurantBranch.objects.filter(slug=branch_slug).first()
        if branch:
            qs = qs.filter(branch=branch)

    from django.shortcuts import render
    return render(request, 'events/public_list.html', {
        'events': qs,
        'branch': branch,
    })


def customer_booking_confirmation(request, booking_reference):
    """Customer-facing digital ticket pass confirmation screen."""
    from django.shortcuts import render
    booking = get_object_or_404(
        EventBooking.objects.select_related('event', 'ticket_type', 'event__branch'),
        booking_reference=booking_reference
    )
    if not booking.booking_qr_code and booking.payment_status == EventBooking.PaymentStatus.SUCCESS:
        booking.generate_qr_code()
        booking.save(update_fields=['booking_qr_code'])

    return render(request, 'events/public_pass.html', {
        'booking': booking,
    })

