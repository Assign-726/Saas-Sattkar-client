from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from django.utils import timezone
from datetime import datetime, timedelta
from .models import Reservation, ReservationSetting
from .serializers import ReservationSerializer
from super_admin.models import Restaurant


class ReservationViewSet(viewsets.ModelViewSet):
    queryset = Reservation.objects.all()
    serializer_class = ReservationSerializer

    def get_permissions(self):
        if self.action in ['create', 'list', 'availability']:
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset()
        restaurant_id = self.request.query_params.get('restaurant')
        restaurant_slug = self.request.query_params.get('restaurant_slug')
        date_str = self.request.query_params.get('date')
        if restaurant_id:
            qs = qs.filter(restaurant_id=restaurant_id)
        elif restaurant_slug:
            qs = qs.filter(restaurant__slug=restaurant_slug)
        if date_str:
            try:
                qs = qs.filter(date=date_str)
            except Exception:
                pass
        return qs.order_by('-date', '-start_time')

    @action(detail=False, methods=['get'], url_path='availability', permission_classes=[AllowAny], authentication_classes=[])
    def availability(self, request):
        rslug = request.query_params.get('restaurant_slug')
        rid = request.query_params.get('restaurant_id')
        date_str = request.query_params.get('date')
        time_str = request.query_params.get('start_time')
        duration = int(request.query_params.get('duration', '90') or 90)
        party_size = int(request.query_params.get('party_size', '2') or 2)

        restaurant = None
        if rid:
            restaurant = Restaurant.objects.filter(id=rid).first()
        elif rslug:
            restaurant = Restaurant.objects.filter(slug=rslug).first()
        if not restaurant:
            return Response({'available': False, 'message': 'Invalid restaurant'}, status=400)
        if not date_str or not time_str:
            return Response({'available': False, 'message': 'date and start_time are required (YYYY-MM-DD, HH:MM)'}, status=400)
        try:
            start_dt = datetime.fromisoformat(f"{date_str} {time_str}")
        except Exception:
            return Response({'available': False, 'message': 'Invalid date/time format'}, status=400)
        end_dt = start_dt + timedelta(minutes=duration)
        # Enforce settings (hours, interval, capacity)
        settings = ReservationSetting.objects.filter(restaurant=restaurant).first()
        if settings:
            # Hours check
            if not (settings.open_time <= start_dt.time() < settings.close_time):
                return Response({'available': False, 'message': 'Outside operating hours'}, status=200)
            # Interval check
            if settings.slot_interval_minutes:
                minutes = (start_dt.hour * 60 + start_dt.minute)
                if minutes % settings.slot_interval_minutes != 0:
                    return Response({'available': False, 'message': f'Start time must align to {settings.slot_interval_minutes}-minute intervals'}, status=200)
        # Overlap check (exclude cancelled)
        overlaps = Reservation.objects.filter(
            restaurant=restaurant,
            date=start_dt.date(),
        ).exclude(status=Reservation.ReservationStatus.CANCELLED).filter(
            # start < other_end AND end > other_start
            start_time__lt=end_dt.time(),
            # compute other_end as start_time + duration on same day approximated: use conservative window
        )
        # For accuracy, compute using python loop due to end time from duration
        conflict = False
        seats_taken = 0
        for r in overlaps:
            r_start = datetime.combine(r.date, r.start_time)
            r_end = r_start + timedelta(minutes=r.duration_minutes or 90)
            if start_dt < r_end and end_dt > r_start:
                conflict = True
                # Count seats for capacity enforcement (Pending/Confirmed/Completed count, exclude Cancelled)
                if r.status != Reservation.ReservationStatus.CANCELLED:
                    seats_taken += (r.party_size or 0)
        if settings and settings.max_seats_per_slot:
            if seats_taken + party_size > settings.max_seats_per_slot:
                return Response({'available': False, 'message': 'Capacity full for this time slot'}, status=200)
        if conflict and not (settings and settings.max_seats_per_slot):
            # If capacity is managed, allow multiple until capacity; else treat any overlap as conflict
            return Response({'available': False, 'message': 'Selected slot is not available'}, status=200)
        # Basic capacity not enforced here; return available
        return Response({'available': True}, status=200)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        reservation = self.get_object()
        reservation.status = Reservation.ReservationStatus.CONFIRMED
        reservation.save(update_fields=['status'])
        return Response({'success': True, 'status': reservation.status})

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        reservation = self.get_object()
        reservation.status = Reservation.ReservationStatus.CANCELLED
        reservation.save(update_fields=['status'])
        return Response({'success': True, 'status': reservation.status})
