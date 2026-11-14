"""Guest folio and stay operations for the hotel front desk.

The folio of a booking is: the room charges stored on the booking, plus extra charges
(``FolioCharge``), plus cab trips billed to the room, minus payments and refunds
(``FolioPayment``). The booking's ``payment_status`` is kept in step with the balance.

Every stay change (check-in, check-out, cancel, no-show, extend, move) lives here so the
admin screens, the night audit and the tests all follow the same rules. Each function
returns an error message for the user, or ``None`` when the change was made.
"""
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from hotel import pricing
from hotel.models import (FolioCharge, FolioPayment, HotelSettings, HousekeepingTask, Room, RoomBooking)

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _d(v):
    return Decimal(v or 0)


@dataclass
class Line:
    date: datetime.date
    kind: str
    description: str
    net: Decimal
    tax: Decimal
    tax_percent: Decimal = ZERO
    quantity: Decimal = Decimal('1')
    charge: object = None   # FolioCharge, when the line can be voided
    sac: str = '996311'

    @property
    def total(self):
        return self.net + self.tax


@dataclass
class Folio:
    booking: RoomBooking
    lines: list = field(default_factory=list)
    voided: list = field(default_factory=list)
    payments: list = field(default_factory=list)

    def __post_init__(self):
        b = self.booking
        room_net, room_tax = _d(b.total_room_charges), _d(b.taxes)
        rate = (room_tax / room_net * 100).quantize(CENT) if room_net else ZERO
        nights = b.nights_count
        self.lines.append(Line(b.check_in_date, 'Room', f'Room {b.room.room_number} · {b.room.category.name} · {nights} night{"s" if nights != 1 else ""}',
                               room_net, room_tax, rate, Decimal(nights)))
        for c in b.folio_charges.all():
            if c.is_void:
                self.voided.append(c)
                continue
            self.lines.append(Line(c.service_date, c.get_kind_display(), c.description + (f' · {c.reference}' if c.reference else ''),
                                   c.amount, c.tax_amount, c.tax_percent, c.quantity, c, _sac(c.kind)))
        for cab in b.cab_bookings.filter(payment_status='ADDED_TO_FOLIO').exclude(trip_status='CANCELLED'):
            net = _d(cab.base_amount)
            tax = _d(cab.tax_amount)
            self.lines.append(Line(cab.pickup_datetime.date() if cab.pickup_datetime else b.check_in_date, 'Transport',
                                   f'Cab {cab.booking_id} · {cab.pickup_location} → {cab.drop_location}', net, tax,
                                   (tax / net * 100).quantize(CENT) if net else ZERO, sac='996601'))
        self.lines.sort(key=lambda l: (l.date, l.kind != 'Room'))
        self.payments = list(b.folio_payments.all())

    @property
    def net(self):
        return sum((l.net for l in self.lines), ZERO)

    @property
    def tax(self):
        return sum((l.tax for l in self.lines), ZERO)

    @property
    def total(self):
        return self.net + self.tax

    @property
    def extras_total(self):
        return sum((l.total for l in self.lines if l.kind != 'Room'), ZERO)

    @property
    def paid(self):
        return sum((p.signed_amount for p in self.payments if not p.is_void), ZERO)

    @property
    def balance(self):
        return (self.total - self.paid).quantize(CENT)

    @property
    def tax_breakdown(self):
        """[(rate %, taxable, cgst, sgst)] — intra-state GST is split equally."""
        rows = {}
        for l in self.lines:
            if not l.tax:
                continue
            r = rows.setdefault(l.tax_percent, [ZERO, ZERO])
            r[0] += l.net
            r[1] += l.tax
        out = []
        for rate, (taxable, tax) in sorted(rows.items()):
            half = (tax / 2).quantize(CENT)
            out.append({'rate': rate, 'taxable': taxable, 'cgst': half, 'sgst': tax - half, 'tax': tax})
        return out


def _sac(kind):
    return {'RESTAURANT': '996331', 'ROOM_SERVICE': '996332', 'LAUNDRY': '999712', 'SPA': '999722',
            'TRANSPORT': '996601'}.get(kind, '996311')


def settings_for(booking):
    return HotelSettings.get_settings(booking.branch)


def sync_payment_status(booking):
    folio = Folio(booking)
    paid, balance = folio.paid, folio.balance
    if paid > 0 and balance <= 0:
        status = RoomBooking.PaymentStatus.PAID
    elif paid > 0:
        status = RoomBooking.PaymentStatus.PARTIALLY_PAID
    elif any(p.kind == FolioPayment.Kind.REFUND and not p.is_void for p in folio.payments):
        status = RoomBooking.PaymentStatus.REFUNDED
    else:
        status = RoomBooking.PaymentStatus.PENDING
    if status != booking.payment_status:
        RoomBooking.objects.filter(pk=booking.pk).update(payment_status=status)
        booking.payment_status = status
    return folio


# ------------------------------------------------------------------ charges & payments

def post_charge(booking, user, kind, description, unit_price, quantity=1, tax_percent=0, service_date=None, reference=''):
    if booking.booking_status in (RoomBooking.BookingStatus.CANCELLED, RoomBooking.BookingStatus.NO_SHOW):
        return None, 'This booking is closed; charges can no longer be posted.'
    if kind not in FolioCharge.Kind.values:
        return None, 'Choose what the charge is for.'
    try:
        unit_price, quantity, tax_percent = Decimal(str(unit_price)), Decimal(str(quantity or 1)), Decimal(str(tax_percent or 0))
    except Exception:
        return None, 'Enter the amount as a number.'
    if unit_price <= 0 or quantity <= 0:
        return None, 'Amount and quantity must be more than zero.'
    if not (0 <= tax_percent <= 28):
        return None, 'GST must be between 0% and 28%.'
    charge = FolioCharge.objects.create(
        booking=booking, kind=kind, description=(description or '').strip()[:200] or FolioCharge.Kind(kind).label,
        unit_price=unit_price, quantity=quantity, tax_percent=tax_percent,
        service_date=service_date or timezone.localdate(), reference=(reference or '')[:80], posted_by=user, amount=0)

    # Hotel inventory minibar hook
    if kind == FolioCharge.Kind.MINIBAR and booking.room:
        try:
            from hotel_inventory import services as store_services
            from hotel_inventory.models import StoreItem
            store_item = None
            ref_str = (reference or '').strip()
            if ref_str.isdigit():
                store_item = StoreItem.objects.filter(branch=booking.branch, pk=int(ref_str)).first()
            if not store_item and description:
                desc_clean = description.strip()
                store_item = StoreItem.objects.filter(branch=booking.branch, name__iexact=desc_clean).first()
            if not store_item and ref_str:
                store_item = StoreItem.objects.filter(branch=booking.branch, barcode=ref_str).first()
            if store_item:
                store_services.minibar_consume(booking, store_item, quantity, user=user, charge_id=charge.pk)
        except Exception:
            pass

    sync_payment_status(booking)
    return charge, None


def void_charge(charge, user, reason):
    if charge.is_void:
        return 'Already voided.'
    if not (reason or '').strip():
        return 'Give a reason for voiding the charge.'
    charge.is_void, charge.void_reason, charge.voided_by, charge.voided_at = True, reason.strip()[:200], user, timezone.now()
    charge.save(update_fields=['is_void', 'void_reason', 'voided_by', 'voided_at'])

    # Hotel inventory minibar reversal hook
    if charge.kind == FolioCharge.Kind.MINIBAR:
        try:
            from hotel_inventory import services as store_services
            store_services.minibar_reverse_consume(charge, user=user)
        except Exception:
            pass

    sync_payment_status(charge.booking)
    return None


def record_payment(booking, user, amount, method, kind=FolioPayment.Kind.PAYMENT, reference='', note='', company_account=None):
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:
        return None, 'Enter the amount as a number.'
    if amount <= 0:
        return None, 'Amount must be more than zero.'
    if method not in FolioPayment.Method.values:
        return None, 'Choose how it was paid.'
    if kind == FolioPayment.Kind.REFUND and amount > Folio(booking).paid:
        return None, 'You can’t refund more than the guest has paid.'
    if method == FolioPayment.Method.COMPANY:
        if kind == FolioPayment.Kind.REFUND:
            return None, 'Refunds go back to the guest, not to a company account.'
        if company_account is None:
            return None, 'Choose the company account to bill.'
    p = FolioPayment.objects.create(booking=booking, kind=kind, method=method, amount=amount,
                                    reference=(reference or '')[:100], note=(note or '')[:200], received_by=user,
                                    company_account=company_account if method == FolioPayment.Method.COMPANY else None)
    sync_payment_status(booking)
    return p, None


def void_payment(payment, user, reason):
    if payment.is_void:
        return 'Already voided.'
    if not (reason or '').strip():
        return 'Give a reason for voiding the payment.'
    payment.is_void, payment.void_reason, payment.voided_at = True, reason.strip()[:200], timezone.now()
    payment.save(update_fields=['is_void', 'void_reason', 'voided_at'])
    sync_payment_status(payment.booking)
    return None


def next_invoice_number(branch):
    with transaction.atomic():
        s = HotelSettings.objects.select_for_update().get(pk=HotelSettings.get_settings(branch).pk)
        n = s.invoice_next_number or 1
        number = f"{s.invoice_prefix or 'INV-HTL'}-{n:05d}"
        while RoomBooking.objects.filter(branch=branch, invoice_number=number).exists():
            n += 1
            number = f"{s.invoice_prefix or 'INV-HTL'}-{n:05d}"
        s.invoice_next_number = n + 1
        s.save(update_fields=['invoice_next_number'])
    return number


# ------------------------------------------------------------------ stay operations

def _clean_task(room, booking, user, kind=HousekeepingTask.Kind.CHECKOUT_CLEAN):
    today = timezone.localdate()
    if HousekeepingTask.objects.filter(room=room, kind=kind, task_date=today,
                                       status__in=[HousekeepingTask.Status.PENDING, HousekeepingTask.Status.IN_PROGRESS]).exists():
        return
    rush = RoomBooking.objects.filter(room=room, check_in_date=today, booking_status=RoomBooking.BookingStatus.RESERVED).exists()
    HousekeepingTask.objects.create(branch=room.branch, room=room, booking=booking, kind=kind, is_rush=rush,
                                    task_date=today, created_by=user)


def kyc_blockers(booking, s):
    """What the hotel's ID rules still need for this booking (empty when the rules are off or met)."""
    if not (s and s.require_id_at_check_in):
        return []
    return booking.kyc_missing_items(require_document=s.require_id_document)


def _join(items):
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]


def check_in(booking, user):
    if booking.booking_status != RoomBooking.BookingStatus.RESERVED:
        return f'Only reserved bookings can be checked in (this one is {booking.get_booking_status_display().lower()}).'
    s = settings_for(booking)
    missing = kyc_blockers(booking, s)
    if missing:
        return f'Guest registration is incomplete: add the {_join(missing)} before checking in.'

    today = timezone.localdate()
    if booking.check_in_date > today:
        return f'This booking arrives on {booking.check_in_date:%d %b}. Change the dates to check the guest in early.'
    other = RoomBooking.objects.filter(room=booking.room, booking_status=RoomBooking.BookingStatus.CHECKED_IN).exclude(pk=booking.pk).first()
    if other:
        return f'Room {booking.room.room_number} is still occupied by {other.guest_name}. Check them out or move this guest.'
    if booking.room_id in pricing.blocked_room_ids(booking.branch, today, today + datetime.timedelta(days=1)):
        return f'Room {booking.room.room_number} is out of order. Move the guest to another room.'
    booking.booking_status = RoomBooking.BookingStatus.CHECKED_IN
    booking.check_in_time = timezone.now()
    booking.save(update_fields=['booking_status', 'check_in_time', 'updated_at'])
    Room.objects.filter(pk=booking.room_id).update(status=Room.RoomStatus.OCCUPIED, updated_at=timezone.now())
    return None


def check_out(booking, user, allow_balance=False):
    if booking.booking_status != RoomBooking.BookingStatus.CHECKED_IN:
        return 'Only guests who are checked in can be checked out.'
    s = settings_for(booking)
    missing = kyc_blockers(booking, s)
    if missing:
        return f'Guest registration is incomplete: add the {_join(missing)} before checking out.'
    folio = sync_payment_status(booking)
    if folio.balance > 0 and not allow_balance and (s is None or s.block_checkout_with_balance):
        return f'₹{folio.balance:,.2f} is still due. Collect it, or confirm checking out with a balance (bill to company / city ledger).'
    booking.booking_status = RoomBooking.BookingStatus.CHECKED_OUT
    booking.check_out_time = timezone.now()
    fields = ['booking_status', 'check_out_time', 'updated_at']
    if not booking.invoice_number:
        booking.invoice_number = next_invoice_number(booking.branch)
        booking.invoiced_at = timezone.now()
        fields += ['invoice_number', 'invoiced_at']
    booking.save(update_fields=fields)
    room = booking.room
    if not RoomBooking.objects.filter(room=room, booking_status=RoomBooking.BookingStatus.CHECKED_IN).exclude(pk=booking.pk).exists():
        Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.CLEANING, updated_at=timezone.now())
        _clean_task(room, booking, user)
    return None


def _release_room(booking):
    room = booking.room
    if room.status == Room.RoomStatus.OCCUPIED and not RoomBooking.objects.filter(
            room=room, booking_status=RoomBooking.BookingStatus.CHECKED_IN).exclude(pk=booking.pk).exists():
        Room.objects.filter(pk=room.pk).update(status=Room.RoomStatus.AVAILABLE, updated_at=timezone.now())


def cancel(booking, user, reason=''):
    if booking.booking_status in (RoomBooking.BookingStatus.CHECKED_OUT, RoomBooking.BookingStatus.CANCELLED):
        return f'This booking is already {booking.get_booking_status_display().lower()}.'
    was_in_house = booking.booking_status == RoomBooking.BookingStatus.CHECKED_IN
    booking.booking_status = RoomBooking.BookingStatus.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.cancellation_reason = (reason or '').strip()[:255]
    booking.save(update_fields=['booking_status', 'cancelled_at', 'cancellation_reason', 'updated_at'])
    if was_in_house:
        _release_room(booking)
    return None


def mark_no_show(booking, user, charge_first_night=False):
    if booking.booking_status != RoomBooking.BookingStatus.RESERVED:
        return 'Only reserved bookings can be marked as a no-show.'
    if booking.check_in_date > timezone.localdate():
        return 'The guest isn’t due yet.'
    if charge_first_night:
        q = pricing.quote(booking.room.category, booking.check_in_date, booking.check_in_date + datetime.timedelta(days=1), settings_for(booking))
        n = q['nights'][0] if q['nights'] else None
        if n:
            post_charge(booking, user, FolioCharge.Kind.ROOM_ADJUSTMENT, f'No-show charge ({booking.check_in_date:%d %b})',
                        n['net'], 1, n['tax_percent'], booking.check_in_date)
    booking.booking_status = RoomBooking.BookingStatus.NO_SHOW
    booking.cancelled_at = timezone.now()
    booking.save(update_fields=['booking_status', 'cancelled_at', 'updated_at'])
    if charge_first_night:
        # The stay itself is not billed; only the no-show line is.
        RoomBooking.objects.filter(pk=booking.pk).update(total_room_charges=ZERO, taxes=ZERO, grand_total=ZERO)
        booking.total_room_charges = booking.taxes = booking.grand_total = ZERO
        sync_payment_status(booking)
    return None


def change_dates(booking, user, new_check_in, new_check_out):
    """Extend, shorten or shift a stay. Nights added are priced at today's rates; removed nights are credited."""
    if booking.booking_status not in (RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN):
        return 'Only upcoming or in-house stays can change dates.'
    if booking.booking_status == RoomBooking.BookingStatus.CHECKED_IN and new_check_in != booking.check_in_date:
        return 'The guest is already in; only the departure date can change.'
    if new_check_out <= new_check_in:
        return 'Departure must be after arrival.'
    if booking.booking_status == RoomBooking.BookingStatus.CHECKED_IN and new_check_out < timezone.localdate():
        return 'Departure can’t be in the past.'
    from django.db import transaction
    with transaction.atomic():
        locked_room = Room.objects.select_for_update().filter(pk=booking.room_id).first() or booking.room
        clash = pricing.room_conflict(locked_room, new_check_in, new_check_out, exclude_booking=booking)
        if clash:
            return clash
        s = settings_for(booking)
        old = set(pricing.nights_between(booking.check_in_date, booking.check_out_date))
        new = set(pricing.nights_between(new_check_in, new_check_out))
        cat = booking.room.category
        old_count = len(old) or 1
        avg_net = _d(booking.total_room_charges) / old_count
        avg_tax = _d(booking.taxes) / old_count
        add_net = add_tax = ZERO
        for night in sorted(new - old):
            q = pricing.quote(cat, night, night + datetime.timedelta(days=1), s)
            add_net += q['net']
            add_tax += q['tax']
        removed = len(old - new)
        add_net -= avg_net * removed
        add_tax -= avg_tax * removed
        booking.total_room_charges = max(ZERO, (_d(booking.total_room_charges) + add_net).quantize(CENT))
        booking.taxes = max(ZERO, (_d(booking.taxes) + add_tax).quantize(CENT))
        booking.grand_total = booking.total_room_charges + booking.taxes
        booking.check_in_date, booking.check_out_date = new_check_in, new_check_out
        booking.save(update_fields=['check_in_date', 'check_out_date', 'total_room_charges', 'taxes', 'grand_total', 'updated_at'])
        sync_payment_status(booking)
        return None


def move_room(booking, user, new_room, reprice=False):
    if booking.booking_status not in (RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN):
        return 'Only upcoming or in-house stays can be moved.'
    if new_room.pk == booking.room_id:
        return 'The guest is already in that room.'
    if new_room.branch_id != booking.branch_id or not new_room.is_active:
        return 'Choose an active room in this hotel.'
    start = max(booking.check_in_date, timezone.localdate()) if booking.booking_status == RoomBooking.BookingStatus.CHECKED_IN else booking.check_in_date
    from django.db import transaction
    with transaction.atomic():
        locked_new_room = Room.objects.select_for_update().filter(pk=new_room.pk).first() or new_room
        clash = pricing.room_conflict(locked_new_room, start, booking.check_out_date, exclude_booking=booking)
        if clash:
            return clash
        old_room = booking.room
        in_house = booking.booking_status == RoomBooking.BookingStatus.CHECKED_IN
        if in_house and RoomBooking.objects.filter(room=locked_new_room, booking_status=RoomBooking.BookingStatus.CHECKED_IN).exists():
            return f'Room {locked_new_room.room_number} is occupied.'
        booking.room = locked_new_room
        fields = ['room', 'updated_at']
        if reprice and not in_house:
            q = pricing.quote(locked_new_room.category, booking.check_in_date, booking.check_out_date, settings_for(booking))
            booking.total_room_charges, booking.taxes, booking.grand_total = q['net'], q['tax'], q['total']
            fields += ['total_room_charges', 'taxes', 'grand_total']
        note = f'Moved from room {old_room.room_number} to {locked_new_room.room_number} on {timezone.localdate():%d %b}.'
        booking.internal_notes = (booking.internal_notes + '\n' if booking.internal_notes else '') + note
        fields.append('internal_notes')
        booking.save(update_fields=fields)
        if in_house:
            Room.objects.filter(pk=locked_new_room.pk).update(status=Room.RoomStatus.OCCUPIED, updated_at=timezone.now())
            Room.objects.filter(pk=old_room.pk).update(status=Room.RoomStatus.CLEANING, updated_at=timezone.now())
            _clean_task(old_room, booking, user)
        sync_payment_status(booking)
        return None


METHOD_FROM_BOOKING = {'ONLINE_RAZORPAY': 'ONLINE', 'RAZORPAY': 'ONLINE', 'STRIPE': 'ONLINE', 'UPI': 'UPI',
                       'UPI_DIRECT': 'UPI', 'CARD': 'CARD', 'CASH': 'CASH', 'BANK': 'BANK', 'OTA': 'OTA'}


def opening_payment(booking):
    """Bookings paid before the folio existed (website, OTA, older records) get one matching payment line."""
    if booking.payment_status != RoomBooking.PaymentStatus.PAID or not booking.grand_total:
        return None
    if FolioPayment.objects.filter(booking=booking).exists():
        return None
    method = METHOD_FROM_BOOKING.get((booking.payment_method or '').upper(), 'CASH')
    return FolioPayment.objects.create(
        booking=booking, method=method, amount=booking.grand_total,
        reference=booking.razorpay_payment_id or booking.upi_transaction_ref or '',
        note='Paid at booking', received_at=booking.updated_at or timezone.now())
