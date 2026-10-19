"""Room pricing and availability, shared by the front desk, the website booking API and reports.

Prices are set per night: the room type's base price, replaced by the newest matching
``RateOverride`` (season, event, weekend). Overrides can also close a room type (stop-sell)
or require a minimum stay from the arrival night. Rooms with an open out-of-order
maintenance issue are never offered.
"""
import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q

CENT = Decimal('0.01')
ACTIVE_STAY = ('RESERVED', 'CHECKED_IN')


def _q(v):
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def nights_between(check_in, check_out):
    return [check_in + datetime.timedelta(days=i) for i in range(max(0, (check_out - check_in).days))]


def tax_fraction(category, nightly_rate, settings=None):
    """GST for one night: the room type's own rate, a flat branch rate, or the tariff slab."""
    if category.custom_tax_rate is not None:
        return Decimal(category.custom_tax_rate) / 100
    if settings is not None and not settings.enable_dynamic_gst:
        return Decimal(settings.tax_rate) / 100
    return Decimal('0.12') if Decimal(nightly_rate) <= 7500 else Decimal('0.18')


def _overrides(category, check_in, check_out):
    from hotel.models import RateOverride
    return list(RateOverride.objects.filter(category=category, start_date__lt=check_out, end_date__gte=check_in).order_by('-id'))


def nightly_rates(category, check_in, check_out, overrides=None):
    """[(night, price, override_or_None)] for every night of the stay."""
    overrides = _overrides(category, check_in, check_out) if overrides is None else overrides
    out = []
    for night in nights_between(check_in, check_out):
        price, used = Decimal(category.base_price_per_night), None
        for o in overrides:
            if o.price_per_night is not None and o.applies_to(night):
                price, used = Decimal(o.price_per_night), o
                break
        out.append((night, price, used))
    return out


def restriction(category, check_in, check_out, overrides=None):
    """A guest-readable reason the stay cannot be sold, or None."""
    overrides = _overrides(category, check_in, check_out) if overrides is None else overrides
    nights = nights_between(check_in, check_out)
    for night in nights:
        for o in overrides:
            if o.stop_sell and o.applies_to(night):
                return f"{category.name} is closed for sale on {night:%d %b %Y}."
    need = max([o.min_stay for o in overrides if o.applies_to(check_in)] or [1])
    if len(nights) < need:
        return f"{category.name} needs a minimum stay of {need} nights from {check_in:%d %b %Y}."
    return None


def quote(category, check_in, check_out, settings=None):
    """Price a stay. Amounts follow the room type's tax-inclusive setting, like the website always has."""
    overrides = _overrides(category, check_in, check_out)
    rows = nightly_rates(category, check_in, check_out, overrides)
    net = tax = gross = Decimal('0')
    nights = []
    for night, price, used in rows:
        rate = tax_fraction(category, price, settings)
        if category.price_is_tax_inclusive:
            n = _q(price / (1 + rate))
            t = _q(price - n)
        else:
            n = _q(price)
            t = _q(price * rate)
        net, tax, gross = net + n, tax + t, gross + price
        nights.append({'date': night, 'price': price, 'net': n, 'tax': t, 'tax_percent': _q(rate * 100),
                       'label': (used.label or 'Special rate') if used else ''})
    service_charge = Decimal('0')
    if settings is not None and settings.service_charge_percent and settings.service_charge_percent > 0:
        service_charge = _q(net * Decimal(settings.service_charge_percent) / 100)
    count = len(rows) or 1
    return {
        'nights': nights,
        'night_count': len(rows),
        'room_charges': gross,          # sum of the prices as displayed
        'net': net,                     # before tax
        'tax': tax,
        'total': net + tax,             # what the booking stores as grand_total
        'service_charge': service_charge,
        'average_rate': _q(gross / count),
        'has_special_rate': any(n['label'] for n in nights),
        'restriction': restriction(category, check_in, check_out, overrides),
    }


def blocked_room_ids(branch, check_in, check_out):
    """Rooms off sale for any night of the stay because of an open out-of-order issue."""
    from hotel.models import MaintenanceIssue
    issues = MaintenanceIssue.objects.filter(branch=branch, out_of_order=True).exclude(status=MaintenanceIssue.Status.RESOLVED)
    issues = issues.filter(Q(block_from__isnull=True) | Q(block_from__lt=check_out),
                           Q(block_until__isnull=True) | Q(block_until__gte=check_in))
    return set(issues.values_list('room_id', flat=True))


def booked_room_ids(rooms, check_in, check_out, exclude_booking=None):
    from hotel.models import RoomBooking
    qs = RoomBooking.objects.filter(room__in=rooms, booking_status__in=ACTIVE_STAY,
                                    check_in_date__lt=check_out, check_out_date__gt=check_in)
    if exclude_booking is not None:
        qs = qs.exclude(pk=exclude_booking.pk)
    return set(qs.values_list('room_id', flat=True))


def free_rooms(rooms, branch, check_in, check_out, exclude_booking=None):
    """Narrow a Room queryset to rooms that can be sold for the stay."""
    taken = booked_room_ids(rooms, check_in, check_out, exclude_booking) | blocked_room_ids(branch, check_in, check_out)
    return rooms.exclude(id__in=taken)


def room_conflict(room, check_in, check_out, exclude_booking=None):
    """Why this room can't take the stay (booking clash or out of order), or None."""
    from hotel.models import RoomBooking
    qs = RoomBooking.objects.filter(room=room, booking_status__in=ACTIVE_STAY,
                                    check_in_date__lt=check_out, check_out_date__gt=check_in)
    if exclude_booking is not None:
        qs = qs.exclude(pk=exclude_booking.pk)
    clash = qs.first()
    if clash:
        return (f"Room {room.room_number} is already booked from {clash.check_in_date:%d %b} to "
                f"{clash.check_out_date:%d %b} by {clash.guest_name}.")
    if room.id in blocked_room_ids(room.branch, check_in, check_out):
        return f"Room {room.room_number} is out of order for some of these nights."
    return None
