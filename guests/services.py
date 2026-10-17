"""Guest matching, stats roll-up, loyalty and gift-card operations."""
import re
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from guests.models import GiftCard, GiftCardTransaction, Guest, LoyaltySettings, LoyaltyTransaction

WALK_IN_NAMES = {'', 'walk-in', 'walk in', 'walkin', 'guest', 'customer', 'cash', 'counter'}
ORDER_EXCLUDED = ('DRAFT', 'C')            # draft & cancelled orders don't count as visits
ROOM_EXCLUDED = ('CANCELLED',)
RESERVATION_EXCLUDED = ('X', 'N')          # cancelled, no-show
EVENT_COUNTED = ('SUCCESS',)


def normalize_phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) >= 6 else ''


def normalize_email(email):
    return (email or '').strip().lower()


def is_walk_in(name, phone, email):
    return not normalize_phone(phone) and not normalize_email(email)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def find_guest(restaurant, phone='', email=''):
    phone, email = normalize_phone(phone), normalize_email(email)
    qs = Guest.objects.filter(restaurant=restaurant)
    if phone:
        g = qs.filter(phone=phone).first()
        if g:
            return g
    if email:
        return qs.filter(email__iexact=email).first()
    return None


def upsert_guest(restaurant, name='', phone='', email=''):
    """Find the guest by phone/email or create one. Fills in missing contact details."""
    if not restaurant or is_walk_in(name, phone, email):
        return None
    phone_n, email_n = normalize_phone(phone), normalize_email(email)
    guest = find_guest(restaurant, phone_n, email_n)
    clean_name = (name or '').strip()
    if guest is None:
        return Guest.objects.create(restaurant=restaurant, name=clean_name or phone_n or email_n,
                                    phone=phone_n, email=email_n)
    changed = []
    if phone_n and not guest.phone:
        guest.phone = phone_n; changed.append('phone')
    if email_n and not guest.email:
        guest.email = email_n; changed.append('email')
    if clean_name and clean_name.lower() not in WALK_IN_NAMES and guest.name in (guest.phone, guest.email, ''):
        guest.name = clean_name; changed.append('name')
    if changed:
        guest.save(update_fields=changed + ['updated_at'])
    return guest


# --------------------------------------------------------------------------
# Activity across sources
# --------------------------------------------------------------------------

def _matches(guest, phone, email):
    p, e = normalize_phone(phone), normalize_email(email)
    return bool((guest.phone and p == guest.phone) or (guest.email and e and e == guest.email))


def guest_activity(guest, with_items=False):
    """Every touchpoint for one guest: orders, stays, reservations, tickets (newest first)."""
    from core.models import Order
    from events.models import EventBooking
    from hotel.models import RoomBooking
    from reservations.models import Reservation

    r = guest.restaurant
    tail = guest.phone[-4:] if guest.phone else ''

    def contact_q(phone_field, email_field=None):
        """Narrow candidates in SQL; exact matching happens in Python (phones are stored unformatted)."""
        q = Q(**{f'{phone_field}__contains': tail}) if tail else Q()
        if email_field and guest.email:
            q = (q | Q(**{f'{email_field}__iexact': guest.email})) if tail else Q(**{f'{email_field}__iexact': guest.email})
        return q if (tail or (email_field and guest.email)) else None

    def at(day, clock=None):
        return timezone.make_aware(datetime.combine(day, clock or time.min))

    items = []
    q = contact_q('phone')
    if q is not None:
        orders = Order.objects.filter(q, restaurant=r).exclude(status='DRAFT').select_related('branch')
        if with_items:
            orders = orders.prefetch_related('items__menu_item')
        for o in orders[:500]:
            if _matches(guest, o.phone, ''):
                items.append({'kind': 'order', 'when': o.order_time, 'obj': o, 'amount': o.total_amount,
                              'cancelled': o.status == 'C', 'title': f'Order {o.order_number}',
                              'subtitle': f'{o.get_order_type_display()} · {o.get_payment_status_display()}'})

    q = contact_q('guest_phone', 'guest_email')
    if q is not None:
        for b in RoomBooking.objects.filter(q, branch__restaurant=r).select_related('room', 'branch')[:200]:
            if _matches(guest, b.guest_phone, b.guest_email):
                nights = max((b.check_out_date - b.check_in_date).days, 1)
                items.append({'kind': 'stay', 'when': at(b.check_in_date), 'obj': b, 'amount': b.grand_total,
                              'cancelled': b.booking_status == 'CANCELLED', 'title': f'Stay {b.booking_id}',
                              'subtitle': f'{nights} night{"s" if nights != 1 else ""} · {b.get_booking_status_display()}'})

    q = contact_q('phone', 'email')
    if q is not None:
        for x in Reservation.objects.filter(q, restaurant=r)[:200]:
            if _matches(guest, x.phone, x.email):
                items.append({'kind': 'reservation', 'when': at(x.date, x.start_time), 'obj': x, 'amount': None,
                              'cancelled': x.status in RESERVATION_EXCLUDED, 'title': f'Table for {x.party_size}',
                              'subtitle': f'{x.get_status_display()}{" · " + x.occasion if x.occasion else ""}'})

    q = contact_q('guest_phone', 'guest_email')
    if q is not None:
        for t in EventBooking.objects.filter(q, event__branch__restaurant=r).select_related('event')[:200]:
            if _matches(guest, t.guest_phone, t.guest_email):
                items.append({'kind': 'event', 'when': t.created_at, 'obj': t, 'amount': t.total_amount,
                              'cancelled': t.payment_status not in EVENT_COUNTED, 'title': t.event.title,
                              'subtitle': f'{t.quantity} ticket{"s" if t.quantity != 1 else ""} · {t.get_payment_status_display()}'})

    items.sort(key=lambda i: i['when'] or timezone.now(), reverse=True)
    return items


def refresh_guest_stats(guest, activity=None):
    activity = activity if activity is not None else guest_activity(guest)
    counted = [a for a in activity if not a['cancelled']]
    guest.restaurant_visits = sum(1 for a in counted if a['kind'] == 'order')
    guest.hotel_stays = sum(1 for a in counted if a['kind'] == 'stay')
    guest.reservations = sum(1 for a in counted if a['kind'] == 'reservation')
    guest.event_bookings = sum(1 for a in counted if a['kind'] == 'event')
    guest.lifetime_spend = sum((Decimal(a['amount'] or 0) for a in counted), Decimal('0'))
    dates = [a['when'] for a in counted if a['when']]
    guest.first_seen = min(dates) if dates else guest.first_seen
    guest.last_seen = max(dates) if dates else guest.last_seen
    guest.save(update_fields=['restaurant_visits', 'hotel_stays', 'reservations', 'event_bookings',
                              'lifetime_spend', 'first_seen', 'last_seen', 'updated_at'])
    return guest


def favourite_items(guest, limit=5):
    counts = defaultdict(lambda: [0, ''])
    for a in guest_activity(guest, with_items=True):
        if a['kind'] == 'order' and not a['cancelled']:
            for item in a['obj'].items.all():
                c = counts[item.menu_item_id]
                c[0] += item.quantity
                c[1] = item.menu_item.name
    return sorted(({'name': n, 'quantity': q} for q, n in counts.values()), key=lambda x: -x['quantity'])[:limit]


def rebuild_guests(restaurant):
    """Create/refresh guest profiles from all historical activity. Returns (created, updated)."""
    from core.models import Order
    from events.models import EventBooking
    from hotel.models import RoomBooking
    from reservations.models import Reservation

    seen = {}
    sources = [
        ((o.customer_name, o.phone, '') for o in Order.objects.filter(restaurant=restaurant).exclude(status__in=ORDER_EXCLUDED).only('customer_name', 'phone')),
        ((b.guest_name, b.guest_phone, b.guest_email) for b in RoomBooking.objects.filter(branch__restaurant=restaurant).only('guest_name', 'guest_phone', 'guest_email')),
        ((x.customer_name, x.phone, x.email) for x in Reservation.objects.filter(restaurant=restaurant).only('customer_name', 'phone', 'email')),
        ((t.guest_name, t.guest_phone, t.guest_email) for t in EventBooking.objects.filter(event__branch__restaurant=restaurant).only('guest_name', 'guest_phone', 'guest_email')),
    ]
    before = Guest.objects.filter(restaurant=restaurant).count()
    for source in sources:
        for name, phone, email in source:
            key = normalize_phone(phone) or normalize_email(email)
            if not key or key in seen:
                continue
            seen[key] = upsert_guest(restaurant, name, phone, email)
    for guest in {g.pk: g for g in seen.values() if g}.values():
        refresh_guest_stats(guest)
    created = Guest.objects.filter(restaurant=restaurant).count() - before
    return created, len(seen) - created


# --------------------------------------------------------------------------
# Loyalty
# --------------------------------------------------------------------------

class LoyaltyError(Exception):
    pass


def _apply_points(guest, points, kind, reason='', source_type='', source_ref='', user=None):
    with transaction.atomic():
        locked = Guest.objects.select_for_update().get(pk=guest.pk)
        new_balance = locked.loyalty_points + points
        if new_balance < 0:
            raise LoyaltyError(f'Only {locked.loyalty_points} points available.')
        tx = LoyaltyTransaction.objects.create(
            guest=locked, kind=kind, points=points, balance_after=new_balance, reason=reason[:200],
            source_type=source_type, source_ref=str(source_ref or ''), created_by=user)
        Guest.objects.filter(pk=locked.pk).update(loyalty_points=F('loyalty_points') + points)
    guest.loyalty_points = new_balance
    return tx


def award_points(guest, amount, source_type, source_ref, reason):
    """Earn points for a paid order / completed stay — at most once per source."""
    settings_obj = LoyaltySettings.for_restaurant(guest.restaurant)
    if not settings_obj.enabled:
        return None
    points = settings_obj.points_for(amount)
    if points <= 0:
        return None
    if LoyaltyTransaction.objects.filter(guest=guest, kind='EARN', source_type=source_type, source_ref=str(source_ref)).exists():
        return None
    try:
        return _apply_points(guest, points, 'EARN', reason, source_type, source_ref)
    except IntegrityError:
        return None  # a concurrent save already awarded it


def reverse_points(guest, source_type, source_ref, reason):
    """Take back points earned from a source that was later cancelled or refunded."""
    earned = LoyaltyTransaction.objects.filter(guest=guest, kind='EARN', source_type=source_type, source_ref=str(source_ref)).first()
    if not earned or LoyaltyTransaction.objects.filter(guest=guest, kind='REVERSE', source_type=source_type, source_ref=str(source_ref)).exists():
        return None
    deduct = min(earned.points, guest.loyalty_points)
    if deduct <= 0:
        return None
    return _apply_points(guest, -deduct, 'REVERSE', reason, source_type, source_ref)


def redeem_points(guest, points, reason, user):
    settings_obj = LoyaltySettings.for_restaurant(guest.restaurant)
    points = int(points)
    if points <= 0:
        raise LoyaltyError('Enter the number of points to redeem.')
    if points < settings_obj.min_redeem_points:
        raise LoyaltyError(f'Minimum redemption is {settings_obj.min_redeem_points} points.')
    return _apply_points(guest, -points, 'REDEEM', reason or 'Redeemed at billing', 'manual', '', user)


def adjust_points(guest, points, reason, user):
    points = int(points)
    if points == 0:
        raise LoyaltyError('Enter a non-zero adjustment.')
    if not (reason or '').strip():
        raise LoyaltyError('Give a reason for the adjustment.')
    return _apply_points(guest, points, 'ADJUST', reason, 'manual', '', user)


# --------------------------------------------------------------------------
# Gift cards
# --------------------------------------------------------------------------

class GiftCardError(Exception):
    pass


def issue_gift_card(restaurant, value, user=None, guest=None, **details):
    value = Decimal(value)
    if value <= 0:
        raise GiftCardError('Value must be greater than zero.')
    with transaction.atomic():
        card = GiftCard.objects.create(restaurant=restaurant, initial_value=value, balance=value, guest=guest,
                                       issued_by=user, **details)
        GiftCardTransaction.objects.create(card=card, kind='ISSUE', amount=value, balance_after=value,
                                           note='Card issued', created_by=user)
    return card


def _card_change(card, kind, amount, note, user, reference='', allow_expired=False):
    with transaction.atomic():
        locked = GiftCard.objects.select_for_update().get(pk=card.pk)
        if locked.status == GiftCard.Status.VOID:
            raise GiftCardError('This card has been voided.')
        if kind == 'REDEEM':
            if locked.is_expired:
                raise GiftCardError('This card has expired.')
            if amount > locked.balance:
                raise GiftCardError(f'Only ₹{locked.balance} left on this card.')
            locked.balance -= amount
        elif kind == 'TOPUP':
            locked.balance += amount
            if locked.is_expired and not allow_expired:
                raise GiftCardError('Extend the expiry date before topping up an expired card.')
        locked.last_used_at = timezone.now()
        locked.refresh_status()
        locked.save()
        GiftCardTransaction.objects.create(card=locked, kind=kind, amount=amount, balance_after=locked.balance,
                                           note=note[:200], reference=reference, created_by=user)
    return locked


def redeem_gift_card(card, amount, note, user):
    amount = Decimal(amount)
    if amount <= 0:
        raise GiftCardError('Enter an amount to redeem.')
    return _card_change(card, 'REDEEM', amount, note or 'Redeemed at billing', user)


def topup_gift_card(card, amount, note, user):
    amount = Decimal(amount)
    if amount <= 0:
        raise GiftCardError('Enter an amount to add.')
    return _card_change(card, 'TOPUP', amount, note or 'Top-up', user)


def void_gift_card(card, note, user):
    with transaction.atomic():
        locked = GiftCard.objects.select_for_update().get(pk=card.pk)
        if locked.status == GiftCard.Status.VOID:
            return locked
        remaining = locked.balance
        locked.status = GiftCard.Status.VOID
        locked.balance = Decimal('0')
        locked.save()
        GiftCardTransaction.objects.create(card=locked, kind='VOID', amount=remaining, balance_after=Decimal('0'),
                                           note=note or 'Card voided', created_by=user)
    return locked
