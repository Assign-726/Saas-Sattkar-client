"""Loyalty points, gift cards and "charge to room" as POS tenders.

The POS sends them as extra payment lines::

    {"method": "points", "points": 200, "amount": 200.0}
    {"method": "gift_card", "code": "ABCD-EFGH-JKLM", "amount": 500.0}
    {"method": "room", "booking_id": 41, "amount": 850.0}

A room tender posts the amount to an in-house hotel guest's folio (same business), so the
bill is settled at the restaurant and collected at hotel check-out.

``validate_tenders`` runs before the order is saved and recomputes every amount on the
server; ``apply_tenders`` runs after, once per order (re-settling never redeems twice).
``restore_tenders`` gives points and card balance back when the order is cancelled.
"""
from decimal import Decimal

from django.db import transaction

from guests import services
from guests.models import GiftCard, GiftCardTransaction, LoyaltySettings, LoyaltyTransaction

TENDER_METHODS = ('points', 'gift_card', 'room')
CENT = Decimal('0.01')


class TenderError(Exception):
    pass


def lookup(restaurant, phone='', code=''):
    """What the POS shows: the guest's points and/or a gift card's balance."""
    out = {}
    loyalty = LoyaltySettings.for_restaurant(restaurant)
    if phone:
        guest = services.find_guest(restaurant, phone=phone)
        if guest:
            out['guest'] = {
                'id': guest.id, 'name': guest.name, 'points': guest.loyalty_points, 'vip': guest.is_vip,
                'point_value': str(loyalty.point_value), 'min_redeem': loyalty.min_redeem_points,
                'points_value': str((Decimal(guest.loyalty_points) * loyalty.point_value).quantize(CENT)),
                'can_redeem': loyalty.enabled and guest.loyalty_points >= loyalty.min_redeem_points,
                'loyalty_enabled': loyalty.enabled,
            }
    if code:
        card = GiftCard.objects.filter(restaurant=restaurant, code__iexact=code.strip()).first()
        if not card:
            out['card_error'] = 'No gift card with that code.'
        else:
            card.refresh_status()
            usable = card.status == GiftCard.Status.ACTIVE and not card.is_expired and card.balance > 0
            out['card'] = {'code': card.code, 'balance': str(card.balance), 'status': card.get_status_display(), 'usable': usable,
                           'expires_on': card.expires_on.isoformat() if card.expires_on else None,
                           'holder': card.recipient_name or (card.guest.name if card.guest_id else '')}
            if not usable:
                out['card_error'] = 'This card is ' + ('expired' if card.is_expired else card.get_status_display().lower()) + ('' if card.balance > 0 else ' and has no balance') + '.'
    return out


def validate_tenders(restaurant, phone, payments, payable):
    """Return the cleaned tender lines (amounts recomputed). Raises ``TenderError``."""
    tenders = [p for p in payments if p.get('method') in TENDER_METHODS]
    if not tenders:
        return []
    loyalty = LoyaltySettings.for_restaurant(restaurant)
    clean, total = [], Decimal('0')
    for t in tenders:
        if t['method'] == 'points':
            if not loyalty.enabled:
                raise TenderError('Loyalty points are switched off for this business.')
            guest = services.find_guest(restaurant, phone=phone)
            if not guest:
                raise TenderError('Add the guest\'s phone number to redeem points.')
            try:
                points = int(t.get('points') or 0)
            except (TypeError, ValueError):
                raise TenderError('Enter a whole number of points.')
            if points <= 0:
                continue
            if points < loyalty.min_redeem_points:
                raise TenderError(f'Minimum redemption is {loyalty.min_redeem_points} points.')
            if points > guest.loyalty_points:
                raise TenderError(f'{guest.name} has only {guest.loyalty_points} points.')
            amount = (Decimal(points) * loyalty.point_value).quantize(CENT)
            clean.append({'method': 'points', 'points': points, 'amount': float(amount), 'guest_id': guest.id})
        elif t['method'] == 'room':
            clean.append(_validate_room(restaurant, t))
        else:
            code = str(t.get('code') or '').strip()
            card = GiftCard.objects.filter(restaurant=restaurant, code__iexact=code).first()
            if not card:
                raise TenderError(f'Gift card {code or "(blank)"} not found.')
            if card.status != GiftCard.Status.ACTIVE or card.is_expired:
                raise TenderError(f'Gift card {card.code} cannot be used ({card.get_status_display().lower()}).')
            try:
                amount = Decimal(str(t.get('amount') or 0)).quantize(CENT)
            except Exception:
                raise TenderError('Enter the amount to take from the gift card.')
            if amount <= 0:
                continue
            if amount > card.balance:
                raise TenderError(f'Only ₹{card.balance} left on gift card {card.code}.')
            clean.append({'method': 'gift_card', 'code': card.code, 'amount': float(amount), 'card_id': card.id})
        total += Decimal(str(clean[-1]['amount'])) if clean else 0
    if total > Decimal(payable) + CENT:
        raise TenderError(f'Points, gift cards and room charges (₹{total}) cannot exceed the bill (₹{Decimal(payable).quantize(CENT)}).')
    return clean


def _validate_room(restaurant, t):
    from hotel.models import RoomBooking
    try:
        booking_id = int(t.get('booking_id') or 0)
    except (TypeError, ValueError):
        booking_id = 0
    booking = RoomBooking.objects.filter(pk=booking_id, branch__restaurant=restaurant).select_related('room').first()
    if not booking:
        raise TenderError('Choose the room to charge.')
    if booking.booking_status != RoomBooking.BookingStatus.CHECKED_IN:
        raise TenderError(f'{booking.guest_name} is not checked in, so nothing can be charged to room {booking.room.room_number}.')
    try:
        amount = Decimal(str(t.get('amount') or 0)).quantize(CENT)
    except Exception:
        raise TenderError('Enter the amount to charge to the room.')
    if amount <= 0:
        raise TenderError('Enter the amount to charge to the room.')
    return {'method': 'room', 'booking_id': booking.pk, 'room': booking.room.room_number, 'guest': booking.guest_name,
            'amount': float(amount)}


def in_house_guests(restaurant, branch=None):
    """Checked-in hotel guests a restaurant bill can be charged to."""
    from hotel.models import RoomBooking
    qs = RoomBooking.objects.filter(branch__restaurant=restaurant, booking_status=RoomBooking.BookingStatus.CHECKED_IN)
    if branch is not None:
        qs = qs.filter(branch=branch)
    return [{'booking_id': b.pk, 'room': b.room.room_number, 'guest': b.guest_name, 'phone': b.guest_phone,
             'departs': b.check_out_date.isoformat()} for b in qs.select_related('room').order_by('room__room_number')]


def _charge_room(order, t, user):
    from hotel.models import FolioCharge, RoomBooking
    from hotel.folio import sync_payment_status
    if FolioCharge.objects.filter(source_order=order, booking_id=t['booking_id'], is_void=False).exists():
        return
    booking = RoomBooking.objects.select_related('room').get(pk=t['booking_id'])
    if booking.booking_status != RoomBooking.BookingStatus.CHECKED_IN:
        raise services.LoyaltyError(f'{booking.guest_name} checked out before the bill was charged.')
    amount = Decimal(str(t['amount']))
    total = Decimal(order.total_amount or 0)
    tax = Decimal(order.tax_amount or 0)
    rate = (tax / (total - tax)) if total > tax > 0 else Decimal('0')
    FolioCharge.objects.create(
        booking=booking, kind=FolioCharge.Kind.RESTAURANT, description=f'Restaurant bill {order.order_number}',
        unit_price=(amount / (1 + rate)).quantize(CENT), quantity=1, tax_percent=(rate * 100).quantize(CENT),
        reference=str(order.order_number)[:80], source_order=order, posted_by=user, amount=0)
    sync_payment_status(booking)


def _order_ref(order):
    return f'order:{order.pk}'


def apply_tenders(order, tenders, user=None):
    """Redeem the validated tenders against ``order``. Returns the lines that could not be taken."""
    failed = []
    label = f'Order {order.order_number}'
    for t in tenders:
        try:
            if t['method'] == 'room':
                _charge_room(order, t, user)
            elif t['method'] == 'points':
                if LoyaltyTransaction.objects.filter(guest_id=t['guest_id'], kind='REDEEM', source_type='order', source_ref=str(order.pk)).exists():
                    continue
                guest = services.Guest.objects.get(pk=t['guest_id'])
                services._apply_points(guest, -int(t['points']), 'REDEEM', f'Paid part of {label}', 'order', str(order.pk), user)
            else:
                if GiftCardTransaction.objects.filter(card_id=t['card_id'], kind='REDEEM', reference=_order_ref(order)).exists():
                    continue
                card = GiftCard.objects.get(pk=t['card_id'])
                services._card_change(card, 'REDEEM', Decimal(str(t['amount'])), f'Paid part of {label}', user, reference=_order_ref(order))
        except Exception as exc:  # a tender that can no longer be taken moves to the amount due
            failed.append({**t, 'error': str(exc)})
    if failed:
        _drop_failed(order, failed)
    return failed


def _drop_failed(order, failed):
    """A tender failed after validation (balance spent elsewhere meanwhile): move it to the amount due."""
    from core.models import Order
    with transaction.atomic():
        o = Order.objects.select_for_update().get(pk=order.pk)
        lost = sum(Decimal(str(f['amount'])) for f in failed)
        keys = {(f['method'], f.get('code'), f.get('points')) for f in failed}
        o.payments = [p for p in (o.payments or []) if (p.get('method'), p.get('code'), p.get('points')) not in keys]
        o.paid_amount = max(Decimal('0'), Decimal(o.paid_amount or 0) - lost)
        o.due_amount = Decimal(o.due_amount or 0) + lost
        o.payment_status = Order.PaymentStatus.PARTIALLY_PAID if o.paid_amount > 0 else Order.PaymentStatus.UNPAID
        if o.status == Order.OrderStatus.COMPLETED:
            o.status = Order.OrderStatus.SERVED
        o.save()
    order.refresh_from_db()


def tender_total(order, method=None):
    return sum((Decimal(str(p.get('amount') or 0)) for p in (order.payments or [])
                if p.get('method') in TENDER_METHODS and (method is None or p.get('method') == method)), Decimal('0'))


def restore_tenders(order, user=None):
    """Give back points and card balance used on an order that was cancelled or refunded, and take
    the bill off any hotel folio it was charged to."""
    ref = str(order.pk)
    from hotel.models import FolioCharge
    from hotel.folio import void_charge
    for charge in FolioCharge.objects.filter(source_order=order, is_void=False).select_related('booking'):
        void_charge(charge, user, f'Restaurant bill {order.order_number} cancelled')
    for tx in LoyaltyTransaction.objects.filter(kind='REDEEM', source_type='order', source_ref=ref).select_related('guest'):
        if not LoyaltyTransaction.objects.filter(guest=tx.guest, kind='ADJUST', source_type='order_refund', source_ref=ref).exists():
            services._apply_points(tx.guest, -tx.points, 'ADJUST', f'Points returned · order {order.order_number} cancelled', 'order_refund', ref, user)
    for tx in GiftCardTransaction.objects.filter(kind='REDEEM', reference=_order_ref(order)).select_related('card'):
        if not GiftCardTransaction.objects.filter(card=tx.card, kind='TOPUP', reference='refund:' + _order_ref(order)).exists():
            card = tx.card
            if card.status == GiftCard.Status.VOID:
                continue
            services._card_change(card, 'TOPUP', tx.amount, f'Refund · order {order.order_number} cancelled', user,
                                  reference='refund:' + _order_ref(order), allow_expired=True)
