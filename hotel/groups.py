"""Group bookings: several rooms for one party, handled together at the front desk."""
import datetime
from decimal import Decimal

from django.db import transaction

from hotel import folio as F
from hotel import pricing
from hotel.models import BookingGroup, FolioPayment, HotelSettings, Room, RoomBooking

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _price(category, ci, co, settings, negotiated):
    """(net, tax, total) for one room: hotel rates, or a negotiated price per night (before GST)."""
    if negotiated is None:
        q = pricing.quote(category, ci, co, settings)
        return q['net'], q['tax'], q['total'], q['restriction']
    net = tax = ZERO
    for _ in pricing.nights_between(ci, co):
        rate = pricing.tax_fraction(category, negotiated, settings)
        net += negotiated
        tax += (negotiated * rate).quantize(CENT)
    return net.quantize(CENT), tax, (net + tax).quantize(CENT), None


def create_group(branch, user, *, name, check_in, check_out, rooms_wanted, contact_name='', contact_phone='', contact_email='',
                 company_account=None, source=RoomBooking.Source.DIRECT, negotiated_rate=None, notes='', override_restrictions=False):
    """``rooms_wanted`` = [(RoomCategory, count)]. All rooms are blocked or none are. Returns (group, error)."""
    if not name.strip():
        return None, 'Give the group a name.'
    if check_out <= check_in:
        return None, 'Departure must be after arrival.'
    wanted = [(c, n) for c, n in rooms_wanted if n > 0]
    if not wanted:
        return None, 'Ask for at least one room.'
    if sum(n for _, n in wanted) > 60:
        return None, 'A group can hold at most 60 rooms.'
    settings = HotelSettings.get_settings(branch)
    with transaction.atomic():
        picks = []
        for cat, n in wanted:
            rooms = Room.objects.select_for_update().filter(branch=branch, category=cat, is_active=True).order_by('room_number')
            free = list(pricing.free_rooms(rooms, branch, check_in, check_out)[:n])
            if len(free) < n:
                return None, f'Only {len(free)} {cat.name} room{"s are" if len(free) != 1 else " is"} free for those dates; you asked for {n}.'
            net, tax, total, restriction = _price(cat, check_in, check_out, settings, negotiated_rate)
            if restriction and not override_restrictions:
                return None, restriction + ' Tick “override restrictions” to block the rooms anyway.'
            picks += [(room, net, tax, total) for room in free]
        group = BookingGroup.objects.create(branch=branch, name=name.strip()[:150], contact_name=contact_name.strip()[:120],
                                            contact_phone=contact_phone.strip()[:30], contact_email=contact_email.strip()[:254],
                                            company_account=company_account, notes=notes.strip(), created_by=user)
        for i, (room, net, tax, total) in enumerate(picks, 1):
            RoomBooking.objects.create(
                room=room, branch=branch, group=group, guest_name=f'{group.name} · guest {i}', guest_email=group.contact_email,
                guest_phone=group.contact_phone, check_in_date=check_in, check_out_date=check_out, num_guests=1, adults=1,
                source=source, total_room_charges=net, taxes=tax, grand_total=total, company_account=company_account,
                company_name=company_account.name if company_account else '', company_gstin=company_account.gstin if company_account else '',
                internal_notes=f'Group {group.code}' + (f' · negotiated ₹{negotiated_rate}/night' if negotiated_rate is not None else ''))
    return group, None


def members(group):
    return list(group.bookings.select_related('room', 'room__category').order_by('room__room_number'))


def summary(group):
    rows = members(group)
    live = [b for b in rows if b.booking_status not in ('CANCELLED', 'NO_SHOW')]
    folios = {b.pk: F.Folio(b) for b in rows}
    counts = {}
    for b in rows:
        counts[b.booking_status] = counts.get(b.booking_status, 0) + 1
    return {
        'rows': [(b, folios[b.pk]) for b in rows], 'rooms': len(live),
        'count_list': [(k, dict(RoomBooking.BookingStatus.choices).get(k, k), n) for k, n in counts.items()],
        'room_nights': sum(b.nights_count for b in live),
        'total': sum((folios[b.pk].total for b in live), ZERO), 'paid': sum((folios[b.pk].paid for b in rows), ZERO),
        'balance': sum((folios[b.pk].balance for b in rows), ZERO), 'counts': counts,
        'arrive': min((b.check_in_date for b in live), default=None), 'depart': max((b.check_out_date for b in live), default=None),
    }


def check_in_all(group, user):
    done, problems = 0, []
    for b in members(group):
        if b.booking_status != 'RESERVED':
            continue
        err = F.check_in(b, user)
        if err:
            problems.append(f'Room {b.room.room_number}: {err}')
        else:
            done += 1
    return done, problems


def check_out_all(group, user, bill_company=False):
    from hotel import ledger
    done, problems = 0, []
    for b in members(group):
        if b.booking_status != 'CHECKED_IN':
            continue
        if bill_company and group.company_account and F.Folio(b).balance > 0:
            _, err = ledger.bill_to_company(b, user, group.company_account)
            if err:
                problems.append(f'Room {b.room.room_number}: {err}')
                continue
        err = F.check_out(b, user)
        if err:
            problems.append(f'Room {b.room.room_number}: {err}')
        else:
            done += 1
    return done, problems


def cancel_all(group, user, reason=''):
    done = 0
    for b in members(group):
        if b.booking_status == 'RESERVED' and not F.cancel(b, user, reason or f'Group {group.code} cancelled'):
            done += 1
    return done


def pay(group, user, amount, method, reference=''):
    """Spread one payment across the group's folios, largest balance first. Returns (applied, error)."""
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:
        return ZERO, 'Enter the amount as a number.'
    if amount <= 0:
        return ZERO, 'Amount must be more than zero.'
    if method == FolioPayment.Method.COMPANY:
        return ZERO, 'Use “Bill the company” to move balances to the company account.'
    due = sorted(((F.Folio(b).balance, b) for b in members(group) if b.booking_status not in ('CANCELLED',)), key=lambda x: -x[0])
    total_due = sum((d for d, _ in due if d > 0), ZERO)
    if amount > total_due:
        return ZERO, f'Only ₹{total_due:,.2f} is due across the group.'
    left = amount
    with transaction.atomic():
        for d, b in due:
            if left <= 0 or d <= 0:
                continue
            part = min(d, left)
            _, err = F.record_payment(b, user, part, method, reference=reference, note=f'Group payment {group.code}')
            if err:
                return amount - left, err
            left -= part
    return amount, None


def bill_company_all(group, user):
    from hotel import ledger
    if not group.company_account:
        return 0, ['Link a company account to the group first.']
    done, problems = 0, []
    for b in members(group):
        if b.booking_status in ('CANCELLED', 'NO_SHOW') or F.Folio(b).balance <= 0:
            continue
        _, err = ledger.bill_to_company(b, user, group.company_account)
        if err:
            problems.append(f'Room {b.room.room_number}: {err}')
        else:
            done += 1
    return done, problems
