"""
Elite Zen purchase voucher — the server side of the keyboard voucher screen (static/restaurant_admin/purchase_zen).

The screen (app.js) is the Elite Zen prototype unchanged; it works on the masters built here (`build_boot`) and
posts the finished voucher to `save_voucher`, which checks it, stores every part of it (lines, charges, payments,
bill copies) and brings the stock in at its landed cost — all in one transaction.
"""
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils import timezone

from core.models import (
    InventoryItem, InventoryTransaction, Party, PurchaseLedgerAccount, PurchaseOrder, PurchaseOrderAttachment,
    PurchaseOrderCharge, PurchaseOrderItem, PurchaseOrderPayment, PurchaseVoucherSetting, Supplier, VendorAdvance,
)
from restaurant_admin.sequence_helpers import get_and_increment_document_number, get_document_sequence_info

CENT = Decimal('0.01')
MILLI = Decimal('0.001')
MAX_FILE_BYTES = 10 * 1024 * 1024
ALLOWED_FILE_TYPES = ('image/', 'application/pdf')

# GSTIN state codes, so a supplier with only a GSTIN still gets the right state (intra / inter-state GST)
GST_STATE_CODES = {
    '01': 'Jammu and Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab', '04': 'Chandigarh', '05': 'Uttarakhand',
    '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan', '09': 'Uttar Pradesh', '10': 'Bihar', '11': 'Sikkim',
    '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur', '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya',
    '18': 'Assam', '19': 'West Bengal', '20': 'Jharkhand', '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh',
    '24': 'Gujarat', '26': 'Dadra and Nagar Haveli and Daman and Diu', '27': 'Maharashtra', '28': 'Andhra Pradesh',
    '29': 'Karnataka', '30': 'Goa', '31': 'Lakshadweep', '32': 'Kerala', '33': 'Tamil Nadu', '34': 'Puducherry',
    '35': 'Andaman and Nicobar Islands', '36': 'Telangana', '37': 'Andhra Pradesh', '38': 'Ladakh',
}
_STATE_BY_NAME = {v.lower().replace('&', 'and'): v for v in GST_STATE_CODES.values()}
_STATE_BY_NAME.update({'orissa': 'Odisha', 'pondicherry': 'Puducherry', 'nct of delhi': 'Delhi', 'new delhi': 'Delhi'})

# The item master's units, in the voucher's unit codes (the voucher knows kg, ltr, pcs, packet, …)
UNIT_CODE = {
    'kg': 'kg', 'kgs': 'kg', 'kilogram': 'kg', 'gm': 'gm', 'g': 'gm', 'gram': 'gm', 'gms': 'gm',
    'litre': 'ltr', 'liter': 'ltr', 'ltr': 'ltr', 'l': 'ltr', 'ml': 'ml',
    'piece': 'pcs', 'pieces': 'pcs', 'pcs': 'pcs', 'pc': 'pcs', 'nos': 'pcs', 'unit': 'pcs', '': 'pcs',
    'pack': 'packet', 'packet': 'packet', 'pkt': 'packet', 'box': 'box', 'crate': 'crate', 'bottle': 'bottle',
    'dozen': 'dozen', 'bag': 'bag', 'katta': 'katta', 'tin': 'tin', 'bundle': 'bundle', 'tray': 'tray',
}

# The usual charges / discounts and places, put in once for a restaurant (editable in the admin afterwards)
DEFAULT_LEDGERS = [
    ('charge', 'Freight Inward', 'bhada transport cartage', 'charge', '', 'items', None),
    ('charge', 'Labour / Hamali', 'majdoori palledari', 'charge', '', 'items', None),
    ('charge', 'Packing Charges', 'packing', 'charge', '', 'items', None),
    ('charge', 'Mandi Commission', 'muddat aadhat arhat', 'charge', '', 'none', Decimal('6')),
    ('charge', 'Tulayi (Weighing)', 'tulai tulayi', 'charge', '', 'items', None),
    ('charge', 'Loading / Unloading', 'loading', 'charge', '', 'items', None),
    ('charge', 'Crate / Jar Deposit', 'deposit security jamanat crate jar cylinder bottle', 'deposit', '', 'none', None),
    ('charge', 'Trade Discount', 'discount disc chhoot chhut kasar scheme', 'discount', 'before', 'none', None),
    ('charge', 'Cash Discount (after tax)', 'cd discount settlement', 'discount', 'after', 'none', None),
    ('cash', 'Main Counter Galla', 'cash nakad galla', '', '', '', None),
    ('cash', 'Store Petty Cash', 'cash nakad petty', '', '', '', None),
    ('godown', 'Main Store', '', '', '', '', None),
    ('godown', 'Kitchen', '', '', '', '', None),
    ('godown', 'Bar', '', '', '', '', None),
    ('godown', 'Cold Room', '', '', '', '', None),
]
LEDGER_PREFIX = {'cash': 'K', 'bank': 'B', 'charge': 'C', 'godown': 'G'}


class VoucherError(Exception):
    """A voucher that cannot be saved as sent; the message is shown to the operator."""


# --- helpers ---------------------------------------------------------------------------------------------------
ASSET_DIR = os.path.join(os.path.dirname(__file__), 'static', 'restaurant_admin', 'purchase_zen')


def asset_version():
    """Newest change time of the voucher's JS/CSS, put on their URLs (?v=) so a browser never keeps running an old
    copy after an update (the files are served without cache headers, and Chrome otherwise reuses them for a while)."""
    newest = 0
    for folder, _dirs, files in os.walk(ASSET_DIR):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
            except OSError:
                pass
    return str(int(newest))


def dec(value, default='0', q=CENT):
    try:
        if value is None or value == '':
            value = default
        return Decimal(str(value)).quantize(q)
    except (InvalidOperation, ValueError, TypeError):
        raise VoucherError(f'Not a number: {value!r}')


def fnum(value):
    return float(value) if value is not None else None


def parse_iso(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], '%Y-%m-%d').date()
    except ValueError:
        raise VoucherError(f'Not a date: {text!r}')


def canonical_state(name, gstin=None):
    if gstin and len(gstin) >= 2 and gstin[:2] in GST_STATE_CODES:
        return GST_STATE_CODES[gstin[:2]]
    key = (name or '').strip().lower().replace('&', 'and')
    return _STATE_BY_NAME.get(key, (name or '').strip().title())


def unit_code(unit):
    raw = (unit or '').strip().lower()
    return UNIT_CODE.get(raw, re.sub(r'[^a-z0-9]+', '', raw) or 'pcs')


def scoped(qs, branch, nullable=True):
    if not branch:
        return qs
    return qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if nullable else qs.filter(branch=branch)


def ensure_default_ledgers(restaurant, branch):
    if PurchaseLedgerAccount.objects.filter(restaurant=restaurant).exists():
        return
    PurchaseLedgerAccount.objects.bulk_create([
        PurchaseLedgerAccount(restaurant=restaurant, branch=None, kind=k, name=n, alias=a, charge_type=ct, stage=st,
                              gst=g, default_rate=r, sort_order=i)
        for i, (k, n, a, ct, st, g, r) in enumerate(DEFAULT_LEDGERS)
    ])


def ledger_entry(acc):
    e = {'id': f'{LEDGER_PREFIX[acc.kind]}{acc.id}', 'name': acc.name, 'alias': acc.alias or ''}
    if acc.kind == 'charge':
        e['ctype'] = acc.charge_type or 'charge'
        if acc.stage:
            e['stage'] = acc.stage
        g = (acc.gst or 'none').strip()
        e['gst'] = g if g in ('items', 'none') else float(dec(g))
        if acc.default_rate is not None:
            e['rate'] = float(acc.default_rate)
    return e


def ledger_from_id(restaurant, voucher_id, kinds):
    m = re.fullmatch(r'([A-Z])(\d+)', str(voucher_id or ''))
    if not m:
        return None
    kind = {v: k for k, v in LEDGER_PREFIX.items()}.get(m.group(1))
    if kind not in kinds:
        return None
    return PurchaseLedgerAccount.objects.filter(restaurant=restaurant, kind=kind, pk=int(m.group(2))).first()


GSTIN_RE = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$')


def party_type(p):
    """regular charges GST; composition / unregistered cannot. A supplier left at the default 'unregistered' but
    holding a valid 15-character GSTIN is a registered (regular) supplier."""
    t = (p.gst_registration_type or '').lower()
    gstin = (p.gstin or '').strip().upper()
    if t == 'composition':
        return 'composition'
    if t == 'regular' or GSTIN_RE.match(gstin):
        return 'regular'
    return 'unregistered'


def vendor_parties(restaurant, branch):
    return scoped(Party.objects.filter(restaurant=restaurant, party_type__in=['vendor', 'both']), branch).order_by('name')


def party_balances(restaurant, parties, exclude_po=None):
    """What we owe each supplier now: opening balance + udhaar of purchase bills − advances still unlinked."""
    ids = [p.id for p in parties]
    owed = {}
    pos = PurchaseOrder.objects.filter(restaurant=restaurant, party_id__in=ids)
    if exclude_po is not None:
        pos = pos.exclude(pk=exclude_po.pk)
    for po in pos.only('party_id', 'voucher_data', 'credit_amount', 'grand_total', 'payment_status'):
        if po.voucher_data is not None:
            amt = po.credit_amount
        else:  # bills entered before the voucher screen: unpaid = all of it
            amt = Decimal('0') if po.payment_status in ('paid', 'P') else po.grand_total
        owed[po.party_id] = owed.get(po.party_id, Decimal('0')) + (amt or 0)
    out = {}
    for p in parties:
        opening = Decimal(str(p.opening_balance or 0))
        if p.balance_type == Party.BalanceType.RECEIVABLE:
            opening = -opening
        out[p.id] = opening + owed.get(p.id, Decimal('0'))
    return out


# --- the masters the screen works on -------------------------------------------------------------------------
def build_boot(request, restaurant, branch, po=None):
    ensure_default_ledgers(restaurant, branch)
    setting, _ = PurchaseVoucherSetting.objects.get_or_create(restaurant=restaurant)

    company_state = canonical_state(
        (branch.state if branch and getattr(branch, 'state', '') else '') or restaurant.state,
        (getattr(branch, 'gstin', '') if branch else '') or None,
    )
    company = {
        'name': (branch.name if branch else None) or restaurant.trade_name or restaurant.name,
        'state': company_state,
        'gstin': (getattr(branch, 'gstin', '') if branch else '') or '',
        # A branch registered "Regular (18% GST with ITC)" claims input GST; a 5% restaurant cannot (GST is cost)
        'gstCredit': bool(setting.gst_credit) or (getattr(branch, 'gst_registration_type', '') == 'R18'),
    }

    # Suppliers
    parties = list(vendor_parties(restaurant, branch))
    balances = party_balances(restaurant, parties, exclude_po=po)
    last_bill = {}
    for row in (PurchaseOrder.objects.filter(restaurant=restaurant, party__in=parties).exclude(pk=po.pk if po else None)
                .order_by('created_at').values('party_id', 'invoice_number')):
        last_bill[row['party_id']] = row['invoice_number']
    party_list = []
    for p in parties:
        e = {
            'id': f'P{p.id}', 'name': p.name, 'alias': p.alias or '', 'gstin': (p.gstin or '').upper(),
            'state': canonical_state(p.state or p.place_of_supply, p.gstin) or company_state,
            'type': party_type(p), 'creditDays': p.credit_days or 0, 'interest': float(p.interest_rate or 0),
            'msme': bool(p.is_msme), 'defaultPay': p.default_payment or 'credit', 'balance': float(balances[p.id].quantize(CENT)),
        }
        if last_bill.get(p.id):
            e['lastBillNo'] = last_bill[p.id]
        party_list.append(e)

    # Items: the stock unit first (stock is kept in it), then the alternate units of the master
    items_qs = InventoryItem.objects.filter(restaurant=restaurant, is_active=True)
    if branch:
        items_qs = items_qs.filter(branch=branch)
    extra_units, extra_rates, item_list = {}, set(), []
    for it in items_qs.select_related('category', 'category__parent', 'restaurant').order_by('name'):
        gst = it.get_effective_value('default_gst_rate')
        gst = float(gst) if gst is not None else 0.0
        if gst not in (0, 5, 18, 40):
            extra_rates.add(gst)
        hsn = str(it.get_effective_value('default_hsn_code') or it.hsn_sac_code or '')
        ptype = it.purchase_type or 'stock'
        e = {'id': f'I{it.id}', 'name': it.name, 'alias': it.alias or '', 'gst': gst, 'cess': 0}
        if ptype != 'stock':
            e['type'] = ptype
        if ptype == 'service':
            e['units'] = []
            e['sac'] = hsn
            if it.last_purchase_rate:
                e['lastAmount'] = float(it.last_purchase_rate)
        else:
            base = unit_code(it.unit)
            if base not in UNIT_CODE.values():
                extra_units[base] = it.unit.strip() or base
            units = [{'u': base, 'f': 1}]
            for alt in it.purchase_units or []:
                try:
                    u, f = unit_code(alt.get('u')), float(alt.get('f'))
                except (TypeError, ValueError, AttributeError):
                    continue
                if f > 0 and u != base and all(x['u'] != u for x in units):
                    units.append({'u': u, 'f': f})
                    if u not in UNIT_CODE.values():
                        extra_units[u] = str(alt.get('u'))
            e['units'] = units
            e['hsn'] = hsn
            rate = it.last_purchase_rate or it.cost_price or it.price_per_unit
            if rate:
                e['lastRate'] = float(rate)
            if it.track_expiry:
                e['expiry'] = True
                if it.shelf_life_days:
                    e['life'] = it.shelf_life_days
            if it.track_batch:
                e['batch'] = True
            if it.mrp:
                e['mrp'] = float(it.mrp)
            if it.pieces_per_unit:
                e['pcs'] = it.pieces_per_unit
        item_list.append(e)

    accounts = scoped(PurchaseLedgerAccount.objects.filter(restaurant=restaurant, is_active=True), branch)
    by_kind = {'cash': [], 'bank': [], 'charge': [], 'godown': []}
    for acc in accounts:
        by_kind[acc.kind].append(ledger_entry(acc))

    # Payments made before their bill, with what is still left of each (this bill's own links count as left)
    now = timezone.localtime()
    unlinked = []
    for adv in scoped(VendorAdvance.objects.filter(restaurant=restaurant, party__in=parties), branch).select_related('party'):
        left = adv.amount_left(exclude_po=po)
        if left <= Decimal('0.004'):
            continue
        paid = timezone.localtime(adv.paid_at)
        unlinked.append({
            'id': f'U{adv.id}', 'partyId': f'P{adv.party_id}', 'amount': float(left),
            'mode': 'cash' if adv.mode == 'cash' else 'upi' if adv.mode == 'upi' else 'bank',
            'daysAgo': max((now.date() - paid.date()).days, 0), 'time': paid.strftime('%H:%M'), 'note': adv.note or '',
        })

    # Saved bills, as the screen reads them: duplicate bill no., last bill of a supplier, last MRP of an item
    saved = []
    recent = (PurchaseOrder.objects.filter(restaurant=restaurant).exclude(pk=po.pk if po else None)
              .select_related('party').order_by('-created_at')[:2000])
    for r in reversed(list(recent)):
        vd = r.voucher_data or {}
        saved.append({
            'purchaseNo': r.po_number or '', 'partyId': f'P{r.party_id}' if r.party_id else None,
            'supplier': r.supplier_name, 'billNo': r.invoice_number or '',
            'billDate': (r.bill_date or r.purchase_date).isoformat(), 'purchaseDate': r.purchase_date.isoformat(),
            'totals': {'grandTotal': float(r.grand_total or 0)},
            'items': [{'itemId': l.get('itemId'), 'mrp': l.get('mrp')} for l in vd.get('items', []) if l.get('mrp')],
            'payments': [], 'credit': None,
        })

    if po is not None:
        next_no = po.po_number or ''
    else:
        next_no = get_document_sequence_info(restaurant, 'purchase', branch=branch)['next_number']

    slug = restaurant.slug
    windows = {
        'sale': reverse('restaurant_admin:order_create_manual', kwargs={'restaurant_slug': slug}),
        'receipt': reverse('restaurant_admin:receipt_list', kwargs={'restaurant_slug': slug}),
        'credit': reverse('restaurant_admin:credit_note_desk', kwargs={'restaurant_slug': slug}),
        'register': reverse('restaurant_admin:purchase_list', kwargs={'restaurant_slug': slug}),
    }
    boot = {
        'company': company,
        'ledger': {'party': party_list, 'item': item_list, **by_kind},
        'extraUnits': [{'u': u, 'label': l} for u, l in extra_units.items()],
        'extraRates': sorted(extra_rates),
        'unlinked': unlinked,
        'saved': saved,
        'nextPurchaseNo': next_no,
        'csrf': get_token(request),
        'saveUrl': request.path,
        'createUrl': reverse('restaurant_admin:purchase_zen_ledger_create', kwargs={'restaurant_slug': slug}),
        'itemCreateUrl': reverse('restaurant_admin:inventory_list', kwargs={'restaurant_slug': slug}),
        'partyUrl': reverse('restaurant_admin:party_ledger_detail', kwargs={'restaurant_slug': slug, 'pk': 0}),
        'listUrl': windows['register'],
        'windows': windows,
        'draftScope': f'{restaurant.id}:{branch.id if branch else 0}' + (f':edit{po.pk}' if po else ''),
        'editing': po is not None,
        'editSnapshot': po.voucher_snapshot if po is not None else None,
    }
    return boot


# --- saving --------------------------------------------------------------------------------------------------
def _reverse_stock(po, user):
    """Take a voucher's stock back out before it is saved again (edit)."""
    for line in po.items.select_related('inventory_item'):
        qty = line.stock_quantity if po.voucher_data is not None else line.quantity
        if qty and line.inventory_item.purchase_type != InventoryItem.PurchaseType.SERVICE:
            line.inventory_item.adjust(-qty)
            InventoryTransaction.objects.create(
                inventory_item=line.inventory_item, transaction_type=InventoryTransaction.TransactionType.MANUAL,
                delta=-qty, purchase_order=po, note=f'Purchase {po.po_number or po.pk} edited: earlier entry reversed',
                performed_by=user,
            )


def save_voucher(request, restaurant, branch, po=None):
    try:
        inv = json.loads(request.POST.get('voucher') or '{}')
        snapshot = json.loads(request.POST.get('snapshot') or 'null')
    except ValueError:
        raise VoucherError('The voucher could not be read')
    files = request.FILES.getlist('attachments')
    for f in files:
        if f.size > MAX_FILE_BYTES:
            raise VoucherError(f'{f.name} is larger than 10 MB')
        if not (f.content_type or '').startswith(ALLOWED_FILE_TYPES):
            raise VoucherError(f'{f.name}: only photos and PDFs can be attached')

    # Supplier: from this restaurant's ledger only
    m = re.fullmatch(r'P(\d+)', str(inv.get('partyId') or ''))
    party = vendor_parties(restaurant, branch).filter(pk=int(m.group(1))).first() if m else None
    if not party:
        raise VoucherError('Select the supplier from the ledger')

    purchase_date = parse_iso(inv.get('purchaseDate')) or timezone.localdate()
    bill_date = parse_iso(inv.get('billDate')) or purchase_date
    bill_no = (inv.get('billNo') or '').strip()[:100]
    if bill_no:
        dup = PurchaseOrder.objects.filter(restaurant=restaurant, party=party, invoice_number__iexact=bill_no)
        if po is not None:
            dup = dup.exclude(pk=po.pk)
        if dup.exists():
            raise VoucherError(f'Bill {bill_no} of {party.name} is already entered')

    totals = inv.get('totals') or {}
    t = {k: dec(totals.get(k)) for k in ('itemsGross', 'discountBeforeTax', 'itemsTaxable', 'charges', 'cgst', 'sgst',
                                         'igst', 'cess', 'discountAfterTax', 'deposits', 'roundOff', 'grandTotal')}
    lines, extras, pays = inv.get('items') or [], inv.get('extras') or [], inv.get('payments') or []
    if not lines:
        raise VoucherError('Add at least one line')
    regime = inv.get('taxRegime') if inv.get('taxRegime') in ('intra', 'inter', 'none') else 'none'

    # The parts must add up to the total the operator confirmed (the screen works it out; this catches a tampered
    # or half-sent voucher). Charges that are part of the goods are inside itemsTaxable's companion `charges`
    tax_total = t['cgst'] + t['sgst'] + t['igst'] + t['cess']
    expect = t['itemsTaxable'] + t['charges'] + tax_total - t['discountAfterTax'] + t['deposits'] + t['roundOff']
    if abs(expect - t['grandTotal']) > Decimal('1.00'):
        raise VoucherError(f'The bill does not add up (₹{expect} against ₹{t["grandTotal"]}) — check the lines')
    if abs(t['roundOff']) > Decimal('1.00'):
        raise VoucherError('Round off can be at most ₹1')
    line_tax = sum((dec(l.get('tax')) + dec(l.get('chargesTax')) for l in lines), Decimal('0'))
    # A charge at its own GST rate may or may not be shared onto the lines; GST heads typed as printed (Alt+T) may
    # differ by up to ₹1 each — anything beyond that is a voucher that was changed after it was worked out
    own_rate_tax = sum((dec(x.get('tax')) for x in extras if x.get('type') == 'charge' and x.get('gst') not in ('items', 'none')), Decimal('0'))
    gap = min(abs(tax_total - line_tax), abs(tax_total - line_tax - own_rate_tax))
    if regime != 'none' and gap > Decimal('4.00'):
        raise VoucherError('GST of the lines does not match the bill GST — check the GST rates')

    paid = sum((dec(p.get('amount')) for p in pays), Decimal('0'))
    credit = inv.get('credit') or None
    credit_amt = dec(credit.get('amount')) if credit else Decimal('0')
    if paid - t['grandTotal'] > Decimal('0.01'):
        raise VoucherError('Paid more than the bill')
    if abs(paid + credit_amt - t['grandTotal']) > Decimal('0.05'):
        raise VoucherError('Payment and udhaar do not add up to the bill total')

    with transaction.atomic():
        if po is None:
            # A number the operator typed is kept; an automatic one is taken fresh from the series (another counter
            # may have used the one shown). A series in Manual mode always takes the number on the screen
            shown = (inv.get('purchaseNo') or '').strip()[:100]
            try:
                try:
                    po_number = get_and_increment_document_number(
                        restaurant=restaurant, document_type='purchase', branch=branch,
                        user_input_no=shown if inv.get('purchaseNoManual') else None)
                except ValidationError:
                    if inv.get('purchaseNoManual') or not shown:
                        raise
                    po_number = get_and_increment_document_number(
                        restaurant=restaurant, document_type='purchase', branch=branch, user_input_no=shown)
            except ValidationError as ve:
                raise VoucherError(' '.join(ve.messages))
        else:
            po_number = po.po_number
            _reverse_stock(po, request.user)
            po.items.all().delete()
            po.charges.all().delete()
            po.payments.all().delete()

        sup_lookup = {'restaurant': restaurant, 'name__iexact': party.name}
        if branch:
            sup_lookup['branch'] = branch
        supplier, _ = Supplier.objects.get_or_create(**sup_lookup, defaults={'name': party.name, 'gstin': party.gstin or ''})

        if credit_amt <= 0:
            status = PurchaseOrder.PaymentStatus.PAID
        elif paid > 0:
            status = PurchaseOrder.PaymentStatus.PARTIAL
        else:
            status = PurchaseOrder.PaymentStatus.UNPAID
        modes = {p.get('mode') for p in pays if p.get('mode') != 'linked'}
        if credit_amt > 0 and not modes:
            method = PurchaseOrder.PaymentMethod.CREDIT
        elif modes == {'cash'}:
            method = PurchaseOrder.PaymentMethod.CASH
        elif modes == {'upi'}:
            method = PurchaseOrder.PaymentMethod.UPI
        elif not modes:
            method = PurchaseOrder.PaymentMethod.CREDIT if credit_amt > 0 else PurchaseOrder.PaymentMethod.CASH
        else:
            method = PurchaseOrder.PaymentMethod.BANK_TRANSFER

        fields = dict(
            restaurant=restaurant, branch=branch, party=party, supplier=supplier, supplier_name=party.name,
            supplier_gstin=party.gstin or '', po_number=po_number, invoice_number=bill_no or po_number,
            purchase_date=purchase_date, bill_date=bill_date,
            subtotal=t['itemsTaxable'] + t['charges'], tax_amount=tax_total, grand_total=t['grandTotal'],
            payment_method=method, payment_status=status,
            tax_regime=regime, is_tax_inclusive=bool(inv.get('ratesInclusive')),
            items_gross=t['itemsGross'], discount_before_tax=t['discountBeforeTax'], total_charges=t['charges'],
            cgst_amount=t['cgst'], sgst_amount=t['sgst'], igst_amount=t['igst'], cess_amount=t['cess'],
            discount_after_tax=t['discountAfterTax'], refundable_deposits=t['deposits'], round_off=t['roundOff'],
            itc_claimed=dec(inv.get('itcClaimed')), amount_paid=paid, credit_amount=credit_amt,
            credit_days=int(credit.get('dueDays') or 0) if credit else None,
            due_date=parse_iso(credit.get('dueDate')) if credit else None,
            interest_rate=dec(credit.get('interestPA')) if credit else None,
            late_fee=dec(credit.get('lateFee')) if credit else None,
            notes=(inv.get('remarks') or '')[:5000], einvoice_data=inv.get('einvoice') or None,
            voucher_data=inv, voucher_snapshot=snapshot,
        )
        if po is None:
            po = PurchaseOrder.objects.create(created_by=request.user, **fields)
        else:
            for k, v in fields.items():
                setattr(po, k, v)
            po.save()

        # Lines: stock comes in as billed + free, at what it really cost (landed cost per stock unit)
        items_qs = InventoryItem.objects.filter(restaurant=restaurant)
        if branch:
            items_qs = items_qs.filter(branch=branch)
        for l in lines:
            mi = re.fullmatch(r'I(\d+)', str(l.get('itemId') or ''))
            item = items_qs.filter(pk=int(mi.group(1))).first() if mi else None
            if not item:
                raise VoucherError(f'{l.get("name") or "A line"}: select the item from the item ledger')
            is_service = item.purchase_type == InventoryItem.PurchaseType.SERVICE
            stock_qty = Decimal('0') if is_service else dec(l.get('stockQty'), q=MILLI)
            billed_base = dec(l.get('qtyInBase'), q=MILLI)
            amount = dec(l.get('amount'))
            if stock_qty < 0 or amount < 0:
                raise VoucherError(f'{item.name}: quantity and amount cannot be negative')
            landed_cost = dec(l.get('cost'))
            landed_rate = dec(l.get('landedRate')) if l.get('landedRate') is not None else None
            per_unit_billed = (amount / billed_base).quantize(CENT) if billed_base > 0 else dec(l.get('rate'))
            PurchaseOrderItem.objects.create(
                purchase_order=po, inventory_item=item,
                quantity=dec(l.get('qty'), q=MILLI) if not is_service else Decimal('1.000'),
                unit_cost=per_unit_billed if not is_service else amount,
                gst_rate=dec(l.get('gstRate')), hsn_code=(l.get('hsn') or '')[:50], line_total=amount,
                unit=(l.get('unit') or '')[:30], rate_unit=(l.get('rateUnit') or '')[:30], rate=dec(l.get('rate')),
                quantity_in_stock_unit=billed_base, free_quantity=dec(l.get('free'), q=MILLI),
                free_unit=(l.get('freeUnit') or '')[:30], free_in_stock_unit=dec(l.get('freeInBase'), q=MILLI),
                stock_quantity=stock_qty, discount_percent=dec(l.get('disc')),
                taxable_amount=dec(l.get('taxable')), tax_amount=dec(l.get('tax')), cess_rate=dec(l.get('cessRate')),
                charges_share=dec(l.get('charges')), charges_tax_share=dec(l.get('chargesTax')),
                landed_cost=landed_cost, landed_rate=landed_rate,
                batch_number=(l.get('batch') or '')[:50], expiry_date=parse_iso(l.get('expiry')),
                expiry_estimated=bool(l.get('expiryEstimated')),
                mrp=dec(l.get('mrp')) if l.get('mrp') else None,
                godown=_godown_name(restaurant, l.get('godown')), line_remarks=(l.get('remark') or '')[:255],
            )
            update = []
            if is_service:
                item.last_purchase_rate = amount
                update.append('last_purchase_rate')
            else:
                if stock_qty > 0:
                    item.adjust(stock_qty)
                    InventoryTransaction.objects.create(
                        inventory_item=item, transaction_type=InventoryTransaction.TransactionType.INWARD_PURCHASE,
                        delta=stock_qty, purchase_order=po, unit_cost=landed_rate, performed_by=request.user,
                        note=f'Purchase {po_number} · bill {bill_no}'[:255] if bill_no else f'Purchase {po_number}',
                    )
                if landed_rate and landed_rate > 0:
                    item.cost_price = landed_rate
                    item.price_per_unit = landed_rate
                    update += ['cost_price', 'price_per_unit']
                if per_unit_billed > 0:
                    item.last_purchase_rate = per_unit_billed
                    update.append('last_purchase_rate')
                if l.get('mrp'):
                    item.mrp = dec(l.get('mrp'))
                    update.append('mrp')
            if update:
                item.save(update_fields=update)

        for x in extras:
            acc = ledger_from_id(restaurant, x.get('ledgerId'), ('charge',))
            PurchaseOrderCharge.objects.create(
                purchase_order=po, ledger=acc, name=(x.get('name') or (acc.name if acc else 'Charge'))[:100],
                charge_type=(x.get('type') or 'charge')[:20], stage=(x.get('stage') or '')[:10],
                method=(x.get('method') or 'fixed')[:10],
                rate=dec(x.get('rate')) if x.get('rate') is not None else None,
                base=dec(x.get('base')) if x.get('base') is not None else None,
                amount=dec(x.get('amount')), gst_treatment=str(x.get('gst') or 'none')[:20], gst_amount=dec(x.get('tax')),
                applies_to=x.get('appliesTo'), tax_split=x.get('split') or [], in_cost=bool(x.get('inCost', True)),
            )

        for p in pays:
            mode = (p.get('mode') or '')[:20]
            amount = dec(p.get('amount'))
            adv = None
            acc = None
            if mode == 'linked':
                mu = re.fullmatch(r'U(\d+)', str(p.get('linkedPaymentId') or ''))
                adv = (VendorAdvance.objects.select_for_update().filter(restaurant=restaurant, party=party, pk=int(mu.group(1))).first()
                       if mu else None)
                if not adv:
                    raise VoucherError('That earlier payment is not this supplier’s')
                if amount - adv.amount_left(exclude_po=po) > Decimal('0.004'):
                    raise VoucherError(f'Only ₹{adv.amount_left(exclude_po=po)} of that payment is left to link')
            else:
                acc = ledger_from_id(restaurant, p.get('ledgerId'), ('cash', 'bank'))
                if not acc:
                    raise VoucherError('Choose the cash or bank it was paid from')
            PurchaseOrderPayment.objects.create(
                purchase_order=po, mode=mode, ledger=acc, ledger_name=(p.get('ledger') or '')[:150], amount=amount,
                reference_number=(p.get('ref') or '')[:100], payment_date=parse_iso(p.get('date')), vendor_advance=adv,
            )

        for f in files:
            PurchaseOrderAttachment.objects.create(purchase_order=po, file=f, file_name=f.name[:255],
                                                   file_type=(f.content_type or '')[:100], file_size=f.size)
    return po


def _godown_name(restaurant, voucher_id):
    acc = ledger_from_id(restaurant, voucher_id, ('godown',))
    return acc.name if acc else ''


# --- "Create" from a list ------------------------------------------------------------------------------------
def create_ledger(request, restaurant, branch):
    """Create a supplier / charge / godown / cash-bank ledger straight from the voucher's list (name typed there)."""
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        raise VoucherError('Bad request')
    kind, name = data.get('kind'), (data.get('name') or '').strip()
    if not name:
        raise VoucherError('Type the name first, then Create')
    if kind == 'party':
        p = vendor_parties(restaurant, branch).filter(name__iexact=name).first() or Party.objects.create(
            restaurant=restaurant, branch=branch, name=name[:200], party_type=Party.PartyType.VENDOR,
            gst_registration_type=Party.GstRegistrationType.UNREGISTERED)
        return {'kind': 'party', 'entry': {'id': f'P{p.id}', 'name': p.name, 'alias': '', 'gstin': p.gstin or '',
                'state': canonical_state(p.state, p.gstin) or canonical_state(restaurant.state), 'type': party_type(p),
                'creditDays': p.credit_days or 0, 'interest': float(p.interest_rate or 0), 'msme': p.is_msme,
                'defaultPay': p.default_payment or 'credit', 'balance': 0}}
    if kind in ('charge', 'godown', 'payledger'):
        if kind == 'payledger':
            kind = 'bank' if re.search(r'bank|a/c|account|hdfc|sbi|icici|axis|kotak', name, re.I) else 'cash'
        extra = {}
        if kind == 'charge':
            is_disc = bool(re.search(r'disc|chhoot|chhut|kasar|less', name, re.I))
            extra = {'charge_type': 'discount' if is_disc else 'charge', 'stage': 'before' if is_disc else '',
                     'gst': 'none' if is_disc else 'items'}
        acc = PurchaseLedgerAccount.objects.create(restaurant=restaurant, branch=branch, kind=kind, name=name[:150], **extra)
        return {'kind': kind, 'entry': ledger_entry(acc)}
    raise VoucherError('Items are created in the inventory master')
