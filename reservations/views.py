from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from core import tenancy
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from django.utils import timezone
from datetime import datetime, timedelta
from reservations.models import Reservation, ReservationSetting
from reservations.serializers import ReservationSerializer
from super_admin.models import Restaurant


class ReservationAnonRateThrottle(SimpleRateThrottle):
    scope = 'reservation_anon'
    rate = '10/min'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"res_throttle_{self.get_ident(request)}"


class ReservationDayRateThrottle(SimpleRateThrottle):
    scope = 'reservation_day'
    rate = '60/day'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated and request.user.is_staff:
            return None
        return f"res_day_throttle_{self.get_ident(request)}"


class ReservationViewSet(viewsets.ModelViewSet):
    queryset = Reservation.objects.all()
    serializer_class = ReservationSerializer
    throttle_classes = [ReservationAnonRateThrottle, ReservationDayRateThrottle]

    def get_permissions(self):
        if self.action in ['create', 'availability']:
            return [AllowAny()]
        return [tenancy.IsBusinessStaff()]

    def get_queryset(self):
        qs = tenancy.scope(super().get_queryset().select_related('restaurant', 'branch'), self.request.user)
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        branch_id = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        branch_slug = self.request.query_params.get('branch_slug')
        date_str = self.request.query_params.get('date')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        elif branch_slug:
            qs = qs.filter(branch__slug=branch_slug)
        if date_str:
            from django.utils.dateparse import parse_date
            try:
                parsed = parse_date(date_str)
            except ValueError:
                parsed = None
            if parsed:
                qs = qs.filter(date=parsed)
        return qs.order_by('-date', '-start_time')

    @action(detail=False, methods=['get'], url_path='availability', permission_classes=[AllowAny], authentication_classes=[])
    def availability(self, request):
        rslug = request.query_params.get('restaurant_slug')
        rid = request.query_params.get('restaurant_id')
        branch_id = request.query_params.get('branch') or request.query_params.get('branch_id')
        branch_slug = request.query_params.get('branch_slug')
        date_str = request.query_params.get('date')
        time_str = request.query_params.get('start_time')
        try:
            duration = min(max(int(request.query_params.get('duration', '90') or 90), 15), 600)
            party_size = min(max(int(request.query_params.get('party_size', '2') or 2), 1), 500)
        except (TypeError, ValueError):
            return Response({'available': False, 'message': 'duration and party_size must be numbers'}, status=400)

        restaurant = None
        if rid:
            restaurant = Restaurant.objects.filter(id=rid).first()
        elif rslug:
            restaurant = Restaurant.objects.filter(slug=rslug).first()
        if not restaurant:
            return Response({'available': False, 'message': 'Invalid restaurant'}, status=400)
        
        branch = None
        if branch_id:
            branch = restaurant.branches.filter(id=branch_id).first()
        elif branch_slug:
            branch = restaurant.branches.filter(slug=branch_slug).first()

        if not date_str or not time_str:
            return Response({'available': False, 'message': 'Date and time are required (YYYY-MM-DD, HH:MM)'}, status=400)
        try:
            start_dt = datetime.fromisoformat(f"{date_str} {time_str}")
        except Exception:
            return Response({'available': False, 'message': 'Invalid date or time format.'}, status=400)
        
        if start_dt.date() < timezone.localdate():
            return Response({'available': False, 'message': 'Cannot check availability for past dates.'}, status=400)

        end_dt = start_dt + timedelta(minutes=duration)

        settings = ReservationSetting.objects.filter(restaurant=restaurant).first()
        if settings:
            if not (settings.open_time <= start_dt.time() < settings.close_time):
                return Response({'available': False, 'message': f'Outside operating hours ({settings.open_time.strftime("%H:%M")} - {settings.close_time.strftime("%H:%M")})'}, status=200)
            if settings.slot_interval_minutes:
                minutes = (start_dt.hour * 60 + start_dt.minute)
                if minutes % settings.slot_interval_minutes != 0:
                    return Response({'available': False, 'message': f'Start time must align to {settings.slot_interval_minutes}-minute intervals'}, status=200)

        overlap_filter = {'restaurant': restaurant, 'date': start_dt.date()}
        if branch:
            overlap_filter['branch'] = branch
        overlaps = Reservation.objects.filter(**overlap_filter).exclude(status=Reservation.ReservationStatus.CANCELLED).filter(
            start_time__lt=end_dt.time(),
        )

        conflict = False
        seats_taken = 0
        for r in overlaps:
            r_start = datetime.combine(r.date, r.start_time)
            r_end = r_start + timedelta(minutes=r.duration_minutes or 90)
            if start_dt < r_end and end_dt > r_start:
                conflict = True
                if r.status != Reservation.ReservationStatus.CANCELLED:
                    seats_taken += (r.party_size or 0)

        if settings and settings.max_seats_per_slot:
            if seats_taken + party_size > settings.max_seats_per_slot:
                return Response({'available': False, 'message': 'Capacity full for this time slot'}, status=200)
        if conflict and not (settings and settings.max_seats_per_slot):
            return Response({'available': False, 'message': 'Selected slot is already reserved'}, status=200)
        return Response({'available': True}, status=200)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm_reservation(self, request, pk=None):
        reservation = self.get_object()
        reservation.status = Reservation.ReservationStatus.CONFIRMED
        reservation.save(update_fields=['status'])
        return Response({'status': 'confirmed', 'message': 'Reservation confirmed successfully.'})

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel_reservation(self, request, pk=None):
        reservation = self.get_object()
        reservation.status = Reservation.ReservationStatus.CANCELLED
        reservation.save(update_fields=['status'])
        return Response({'status': 'cancelled', 'message': 'Reservation cancelled successfully.'})
