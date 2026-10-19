"""City ledger: companies that settle their guests' bills later.

A folio balance billed to a company is a ``FolioPayment`` with method COMPANY and the
account set — the guest's folio is settled, and the amount becomes the company's debt.
``LedgerPayment`` rows are what the company later pays. Ageing applies payments to the
oldest bills first.
"""
import datetime
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from hotel.models import CompanyAccount, FolioPayment, LedgerPayment

ZERO = Decimal('0')
CENT = Decimal('0.01')
BUCKETS = [(30, '0–30 days'), (60, '31–60 days'), (90, '61–90 days'), (None, 'Over 90 days')]


def charges(account):
    return (FolioPayment.objects.filter(company_account=account, method=FolioPayment.Method.COMPANY, is_void=False)
            .select_related('booking', 'booking__room').order_by('received_at', 'id'))


def settlements(account):
    return LedgerPayment.objects.filter(account=account, is_void=False).order_by('received_on', 'id')


def billed(account):
    return sum((p.signed_amount for p in charges(account)), ZERO)


def received(account):
    return settlements(account).aggregate(s=Sum('amount'))['s'] or ZERO


def balance(account):
    return (billed(account) - received(account)).quantize(CENT)


def open_bills(account, today=None):
    """Bills not yet covered by payments (oldest paid first), each with its age and due date."""
    today = today or timezone.localdate()
    pool = received(account)
    out = []
    for c in charges(account):
        amt = c.signed_amount
        if pool >= amt:
            pool -= amt
            continue
        left = amt - pool
        pool = ZERO
        billed_on = timezone.localtime(c.received_at).date()
        due = billed_on + datetime.timedelta(days=account.payment_terms_days)
        out.append({'charge': c, 'booking': c.booking, 'billed_on': billed_on, 'due_on': due, 'open': left.quantize(CENT),
                    'age': (today - billed_on).days, 'overdue_days': max(0, (today - due).days)})
    return out


def aging(account, today=None):
    rows = open_bills(account, today)
    buckets = [[label, ZERO] for _, label in BUCKETS]
    for r in rows:
        for i, (limit, _) in enumerate(BUCKETS):
            if limit is None or r['age'] <= limit:
                buckets[i][1] += r['open']
                break
    overdue = sum((r['open'] for r in rows if r['overdue_days'] > 0), ZERO)
    return {'buckets': buckets, 'overdue': overdue, 'open': sum((r['open'] for r in rows), ZERO), 'bills': rows}


def statement(account):
    """Chronological lines with a running balance."""
    lines = []
    for c in charges(account):
        b = c.booking
        lines.append({'date': timezone.localtime(c.received_at).date(), 'kind': 'bill', 'ref': b.invoice_number or b.booking_id,
                      'text': f'{b.guest_name} · room {b.room.room_number} · {b.check_in_date:%d %b}–{b.check_out_date:%d %b}',
                      'debit': c.signed_amount, 'credit': ZERO, 'booking': b})
    for s in settlements(account):
        lines.append({'date': s.received_on, 'kind': 'payment', 'ref': s.reference or s.get_method_display(),
                      'text': s.note or f'Payment by {s.get_method_display().lower()}', 'debit': ZERO, 'credit': s.amount, 'settlement': s})
    lines.sort(key=lambda l: (l['date'], l['kind'] == 'payment'))
    run = ZERO
    for l in lines:
        run += l['debit'] - l['credit']
        l['balance'] = run
    return lines


def bill_to_company(booking, user, account, amount=None, override_limit=False):
    """Move (part of) a folio balance to the company's account. Returns (payment, error)."""
    from hotel.folio import Folio, record_payment
    if account is None or not account.is_active:
        return None, 'Choose an active company account.'
    if account.restaurant_id != booking.branch.restaurant_id:
        return None, 'That company belongs to another business.'
    due = Folio(booking).balance
    amount = Decimal(str(amount)) if amount not in (None, '') else due
    if amount <= 0:
        return None, 'There is nothing left to bill on this folio.'
    if amount > due:
        return None, f'Only ₹{due:,.2f} is due on this folio.'
    if account.credit_limit is not None and not override_limit:
        after = balance(account) + amount
        if after > account.credit_limit:
            return None, (f'{account.name} would owe ₹{after:,.2f}, over its ₹{account.credit_limit:,.2f} credit limit. '
                          'Collect payment, raise the limit, or tick “override credit limit”.')
    pay, err = record_payment(booking, user, amount, FolioPayment.Method.COMPANY, note=f'Billed to {account.name}', company_account=account)
    if err:
        return None, err
    fields = []
    if booking.company_account_id != account.pk:
        booking.company_account = account
        fields.append('company_account')
    if not booking.company_name:
        booking.company_name = account.name
        fields.append('company_name')
    if not booking.company_gstin and account.gstin:
        booking.company_gstin = account.gstin
        fields.append('company_gstin')
    if fields:
        booking.save(update_fields=fields + ['updated_at'])
    return pay, None


def record_settlement(account, user, amount, method, reference='', note='', received_on=None):
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:
        return None, 'Enter the amount as a number.'
    if amount <= 0:
        return None, 'Amount must be more than zero.'
    if method not in FolioPayment.Method.values or method == FolioPayment.Method.COMPANY:
        return None, 'Choose how the company paid.'
    return LedgerPayment.objects.create(account=account, amount=amount, method=method, reference=(reference or '')[:100],
                                        note=(note or '')[:200], received_on=received_on or timezone.localdate(), received_by=user), None


def summary_rows(restaurant):
    rows = []
    for a in CompanyAccount.objects.filter(restaurant=restaurant).order_by('-is_active', 'name'):
        ag = aging(a)
        bal = balance(a)
        rows.append({'a': a, 'balance': bal, 'overdue': ag['overdue'],
                     'used_pct': int(min(100, bal / a.credit_limit * 100)) if a.credit_limit else None,
                     'bookings': a.bookings.count()})
    return rows
