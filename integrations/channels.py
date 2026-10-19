"""Hotel distribution: what each channel (OTA / channel manager) may sell, night by night.

The ARI feed (availability, rates, inventory) starts from the hotel's own rules in
``hotel.pricing`` — base price, seasonal overrides, stop-sell, minimum stay, booked and
out-of-order rooms — then applies the channel's room mapping: price adjustment, allotment
and a per-channel close. The same feed backs the ``/api/v1/rates`` endpoint and the preview
on the Channels screen, so what staff see is exactly what partners receive.
"""
import datetime
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q

from hotel import pricing

MAX_DAYS = 90


def mappings(connection):
    if connection is None:
        return {}
    return {m.category_id: m for m in connection.room_mappings.all()}


def channel_price(price, mapping):
    if mapping is None or not mapping.rate_adjust_pct:
        return Decimal(price)
    return (Decimal(price) * (1 + Decimal(mapping.rate_adjust_pct) / 100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _free_by_night(branch, categories, start, end):
    """{(category_id, night): rooms free}."""
    from hotel.models import MaintenanceIssue, Room, RoomBooking
    rooms = defaultdict(set)
    for rid, cid in Room.objects.filter(branch=branch, is_active=True, category__in=categories).values_list('id', 'category_id'):
        rooms[cid].add(rid)
    taken = defaultdict(set)
    for b in RoomBooking.objects.filter(branch=branch, booking_status__in=pricing.ACTIVE_STAY, check_in_date__lt=end, check_out_date__gt=start).only('room_id', 'check_in_date', 'check_out_date'):
        for n in pricing.nights_between(max(b.check_in_date, start), min(b.check_out_date, end)):
            taken[n].add(b.room_id)
    issues = MaintenanceIssue.objects.filter(branch=branch, out_of_order=True).exclude(status=MaintenanceIssue.Status.RESOLVED)
    issues = issues.filter(Q(block_from__isnull=True) | Q(block_from__lt=end), Q(block_until__isnull=True) | Q(block_until__gte=start))
    for i in issues:
        for n in pricing.nights_between(max(i.block_from or start, start), min((i.block_until or end) + datetime.timedelta(days=1), end)):
            taken[n].add(i.room_id)
    out = {}
    for n in pricing.nights_between(start, end):
        for cid, ids in rooms.items():
            out[(cid, n)] = len(ids - taken[n])
    return out


def ari(branch, start, end, connection=None, only_mapped=None):
    """[{room type, mapping ids, nights: [{date, price, available, min_stay, closed}]}]."""
    from hotel.models import HotelSettings, RateOverride, RoomCategory
    end = min(end, start + datetime.timedelta(days=MAX_DAYS))
    maps = mappings(connection)
    cats = RoomCategory.objects.filter(branch=branch, is_active=True).order_by('base_price_per_night', 'name')
    if only_mapped if only_mapped is not None else bool(maps):
        cats = cats.filter(id__in=list(maps))
    cats = list(cats)
    free = _free_by_night(branch, cats, start, end)
    settings = HotelSettings.get_settings(branch)
    out = []
    for cat in cats:
        m = maps.get(cat.id)
        overrides = list(RateOverride.objects.filter(category=cat, start_date__lt=end, end_date__gte=start).order_by('-id'))
        nights = []
        for night, price, used in pricing.nightly_rates(cat, start, end, overrides):
            stop = any(o.stop_sell and o.applies_to(night) for o in overrides)
            available = free.get((cat.id, night), 0)
            if m is not None and m.allotment is not None:
                available = min(available, m.allotment)
            closed = stop or bool(m and m.is_closed) or available <= 0
            nights.append({
                'date': night.isoformat(), 'price': str(channel_price(price, m)), 'base_price': str(price),
                'available': max(0, available), 'min_stay': max([o.min_stay for o in overrides if o.applies_to(night)] or [1]),
                'closed': closed, 'stop_sell': stop, 'special_rate': (used.label or 'Special rate') if used else '',
                'tax_percent': str((pricing.tax_fraction(cat, price, settings) * 100).quantize(Decimal('0.01'))),
            })
        out.append({'category_id': cat.id, 'name': cat.name, 'max_occupancy': cat.max_occupancy,
                    'tax_inclusive': cat.price_is_tax_inclusive,
                    'external_room_id': m.external_room_id if m else '', 'external_rate_plan_id': m.external_rate_plan_id if m else '',
                    'rate_adjust_pct': str(m.rate_adjust_pct) if m else '0', 'nights': nights})
    return out


def category_for(connection, branch, body):
    """Resolve the room type a channel booking is for: our id, the channel's room code, or the name."""
    from hotel.models import RoomCategory
    cats = RoomCategory.objects.filter(branch=branch, is_active=True)
    if body.get('category_id'):
        return cats.filter(pk=body['category_id']).first()
    code = str(body.get('external_room_id') or '').strip()
    if code and connection is not None:
        m = connection.room_mappings.filter(external_room_id=code, category__in=cats).select_related('category').first()
        if m:
            return m.category
    if body.get('room_type'):
        return cats.filter(name__iexact=str(body['room_type'])).first()
    return None


def booking_source(connection):
    from hotel.models import RoomBooking
    provider = getattr(connection, 'provider', '')
    if provider in ('ota', 'channel_manager'):
        return RoomBooking.Source.OTA
    if provider == 'website':
        return RoomBooking.Source.WEBSITE
    return RoomBooking.Source.DIRECT


def channel_stats(connection, since):
    from django.db.models import Count, Sum
    from integrations.models import ExternalBookingLink
    qs = ExternalBookingLink.objects.filter(connection=connection, created_at__gte=since)
    agg = qs.aggregate(n=Count('id'), revenue=Sum('booking__grand_total'), commission=Sum('commission_amount'),
                       cancelled=Count('id', filter=Q(booking__booking_status__in=['CANCELLED', 'NO_SHOW'])))
    last = qs.order_by('-created_at').select_related('booking').first()
    return {'bookings': agg['n'] or 0, 'revenue': agg['revenue'] or Decimal('0'), 'commission': agg['commission'] or Decimal('0'),
            'cancelled': agg['cancelled'] or 0, 'last': last}
