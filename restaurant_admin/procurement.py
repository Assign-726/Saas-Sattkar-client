"""Purchase cycle modelled on TallyPrime order processing.

Purchase Order (no stock, no money) -> Goods Receipt Note (stock in, "bill pending")
-> Purchase Bill (supplier owes nothing more until paid; ITC booked) -> Purchase Return / Debit Note
(stock out, payable reduced) and bill-wise payments. Every number comes from the document
sequences, and every stock movement is logged on InventoryTransaction with a link back to its voucher.
"""
import datetime
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import (GoodsReceiptLine, GoodsReceiptNote, InventoryItem, InventoryTransaction, Party, ProcurementOrder,
                         ProcurementOrderLine, PurchaseOrder, PurchaseOrderItem, PurchasePayment, PurchaseReturn,
                         PurchaseReturnLine, RestaurantSettings, Supplier)
from restaurant_admin.sequence_helpers import get_and_increment_document_number

ZERO = Decimal('0')
CENT = Decimal('0.01')
HUNDRED = Decimal('100')


def dec(value, default=ZERO):
    try:
        d = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, AttributeError, TypeError):
        return default
    return d if d.is_finite() else default


def parse_date(value, default=None):
    try:
        return datetime.datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def stock_units(qty):
    """Stock is kept in whole units (InventoryItem.quantity is an integer)."""
    return int(Decimal(qty).to_integral_value(rounding=ROUND_HALF_UP))


def line_amounts(qty, rate, discount_pct=ZERO, gst_rate=ZERO):
    taxable = (qty * rate * (HUNDRED - discount_pct) / HUNDRED).quantize(CENT)
    return taxable, (taxable * gst_rate / HUNDRED).quantize(CENT)


def split_tax(tax, interstate):
    if interstate:
        return ZERO, ZERO, tax
    half = (tax / 2).quantize(CENT)
    return half, tax - half, ZERO


def _state_code(gstin):
    gstin = (gstin or '').strip()
    return gstin[:2] if len(gstin) >= 2 and gstin[:2].isdigit() else ''


def is_interstate(restaurant, branch, supplier_gstin='', supplier_state=''):
    """IGST when the supplier is in another state than the receiving branch (GSTIN state code
    first, state names as a fallback). Unknown means intra-state, as on the purchase invoice."""
    ours_gstin = (getattr(branch, 'gstin', '') or '') if branch else ''
    if not ours_gstin:
        settings_obj = RestaurantSettings.get_settings(restaurant)
        ours_gstin = getattr(settings_obj, 'gst_number', '') or ''
    ours_state = (getattr(branch, 'state', '') or '') if branch else ''
    a, b = _state_code(ours_gstin), _state_code(supplier_gstin)
    if a and b:
        return a != b
    if ours_state.strip() and (supplier_state or '').strip():
        return ours_state.strip().lower() != supplier_state.strip().lower()
    return False


def vendors(restaurant, branch):
    qs = Party.objects.filter(restaurant=restaurant, party_type='vendor')
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return qs.order_by('name')


def items_for(restaurant, branch):
    qs = InventoryItem.objects.filter(restaurant=restaurant)
    return qs.filter(branch=branch) if branch else qs


def resolve_party(restaurant, branch, party_id=None, name='', gstin=''):
    """The chosen vendor, or a new vendor ledger for a typed-in name (Tally lets you create the
    ledger on the fly from the voucher)."""
    if party_id:
        party = vendors(restaurant, branch).filter(pk=party_id).first()
        if party:
            return party
    name = (name or '').strip()
    if not name:
        raise ValidationError('Choose the supplier.')
    lookup = {'restaurant': restaurant, 'name__iexact': name, 'branch': branch}
    party = Party.objects.filter(**lookup).first()
    if not party:
        party = Party.objects.create(restaurant=restaurant, branch=branch, name=name, party_type=Party.PartyType.VENDOR,
                                     gstin=(gstin or '').strip(),
                                     gst_registration_type='regular' if (gstin or '').strip() else 'unregistered')
    return party


def _sync_supplier(restaurant, branch, party):
    """Keep the legacy Supplier row the Purchase Register still reads."""
    return (Supplier.objects.filter(restaurant=restaurant, branch=branch, name__iexact=party.name).first()
            or Supplier.objects.create(restaurant=restaurant, branch=branch, name=party.name, gstin=party.gstin or '', phone=party.phone))


# ============================================================ purchase orders

def parse_lines(restaurant, branch, raw_lines):
    """[{item, quantity, rate, discount_pct, gst_rate}] from posted rows; skips empty rows."""
    items = {i.pk: i for i in items_for(restaurant, branch).filter(pk__in=[r.get('item') for r in raw_lines if r.get('item')])}
    out = []
    for r in raw_lines:
        item = items.get(_int(r.get('item')))
        qty = dec(r.get('quantity'))
        if not item and qty <= 0:
            continue
        if not item:
            raise ValidationError('One of the rows has no stock item.')
        if qty <= 0:
            raise ValidationError(f'Enter a quantity for {item.name}.')
        rate = dec(r.get('rate'), item.unit_cost)
        disc = dec(r.get('discount_pct'))
        gst = dec(r.get('gst_rate'), dec(item.get_effective_value('default_gst_rate')))
        if rate < 0 or not (ZERO <= disc <= HUNDRED) or not (ZERO <= gst <= HUNDRED):
            raise ValidationError(f'Check the rate, discount and GST for {item.name}.')
        out.append({'item': item, 'quantity': qty, 'rate': rate, 'discount_pct': disc, 'gst_rate': gst,
                    'hsn_code': str(item.get_effective_value('default_hsn_code') or '')})
    if not out:
        raise ValidationError('Add at least one item.')
    return out


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@transaction.atomic
def save_order(restaurant, branch, user, party, header, lines, order=None):
    """Create a PO, or rewrite a draft/open one that has nothing received yet."""
    if order and order.receipts.exists():
        raise ValidationError('Goods have been received against this order, so its lines are locked.')
    interstate = is_interstate(restaurant, branch, party.gstin, party.state)
    if not order:
        number = get_and_increment_document_number(restaurant, 'purchase_order', branch=branch, user_input_no=header.get('number'))
        order = ProcurementOrder(restaurant=restaurant, branch=branch, number=number, created_by=user)
    order.party = party
    order.supplier_name, order.supplier_gstin, order.supplier_state = party.name, party.gstin or '', party.state or ''
    order.order_date = header.get('order_date') or timezone.localdate()
    order.expected_date = header.get('expected_date')
    order.credit_days = max(_int(header.get('credit_days'), 0) or 0, 0)
    order.freight_amount = max(dec(header.get('freight')), ZERO).quantize(CENT)
    order.reverse_charge = bool(header.get('reverse_charge'))
    order.terms = (header.get('terms') or '').strip()
    order.narration = (header.get('narration') or '').strip()
    order.is_interstate = interstate
    if order.expected_date and order.expected_date < order.order_date:
        raise ValidationError('Expected delivery cannot be before the order date.')
    order.save()
    order.lines.all().delete()
    subtotal = tax = ZERO
    for ln in lines:
        taxable, t = line_amounts(ln['quantity'], ln['rate'], ln['discount_pct'], ln['gst_rate'])
        ProcurementOrderLine.objects.create(order=order, inventory_item=ln['item'], quantity=ln['quantity'], rate=ln['rate'],
                                            discount_pct=ln['discount_pct'], gst_rate=ln['gst_rate'], hsn_code=ln['hsn_code'],
                                            taxable_value=taxable, tax_amount=t)
        subtotal += taxable
        tax += t
    order.subtotal, order.tax_amount = subtotal, tax
    order.cgst_amount, order.sgst_amount, order.igst_amount = split_tax(tax, interstate)
    # Under reverse charge the supplier does not charge GST; we pay it to the government.
    order.grand_total = subtotal + order.freight_amount + (ZERO if order.reverse_charge else tax)
    if header.get('approve') and order.status == ProcurementOrder.Status.DRAFT:
        order.status = ProcurementOrder.Status.OPEN
        order.approved_by, order.approved_at = user, timezone.now()
    order.save()
    return order


def refresh_order_status(order):
    if order.status in (ProcurementOrder.Status.DRAFT, ProcurementOrder.Status.CANCELLED, ProcurementOrder.Status.CLOSED):
        return order
    lines = list(order.lines.all())
    if lines and all(l.pending_qty <= 0 for l in lines):
        order.status = ProcurementOrder.Status.RECEIVED
    elif any(l.received_qty > 0 for l in lines):
        order.status = ProcurementOrder.Status.PARTIAL
    else:
        order.status = ProcurementOrder.Status.OPEN
    order.save(update_fields=['status', 'updated_at'])
    return order


def order_action(order, op, user, reason=''):
    S = ProcurementOrder.Status
    if op == 'approve':
        if order.status != S.DRAFT:
            raise ValidationError('Only a draft order can be approved.')
        order.status, order.approved_by, order.approved_at = S.OPEN, user, timezone.now()
    elif op == 'close':
        if order.status not in (S.OPEN, S.PARTIAL):
            raise ValidationError('Only an open order can be short-closed.')
        order.status, order.close_reason = S.CLOSED, (reason or 'Short-closed').strip()[:255]
    elif op == 'cancel':
        if order.receipts.exists():
            raise ValidationError('Goods were received against this order — short-close it instead.')
        if order.status in (S.CANCELLED, S.RECEIVED, S.CLOSED):
            raise ValidationError('This order is already finished.')
        order.status, order.close_reason = S.CANCELLED, (reason or 'Cancelled').strip()[:255]
    elif op == 'reopen':
        if order.status != S.CLOSED:
            raise ValidationError('Only a short-closed order can be reopened.')
        order.status, order.close_reason = S.OPEN, ''
        order.save()
        return refresh_order_status(order)
    else:
        raise ValidationError('Unknown action.')
    order.save()
    return order


# ============================================================ goods receipt

@transaction.atomic
def receive_goods(restaurant, branch, user, order, rows, header):
    """Receipt Note against an approved PO. rows: {line_id: {received, rejected, reason, batch, expiry}}.
    Only the accepted quantity enters stock and counts against the order."""
    order = ProcurementOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in ProcurementOrder.OPEN_STATUSES:
        raise ValidationError('Approve the order before receiving goods against it.' if order.status == 'draft'
                              else 'This order is not open for receiving.')
    tolerance = HUNDRED + max(dec(header.get('tolerance_pct')), ZERO)
    parsed = []
    for line in order.lines.select_related('inventory_item'):
        r = rows.get(line.pk) or {}
        received, rejected = dec(r.get('received')), dec(r.get('rejected'))
        if received <= 0 and rejected <= 0:
            continue
        if received < 0 or rejected < 0 or rejected > received:
            raise ValidationError(f'{line.inventory_item.name}: rejected cannot be more than received.')
        accepted = received - rejected
        if accepted > (line.pending_qty * tolerance / HUNDRED).quantize(Decimal('0.001')):
            raise ValidationError(f'{line.inventory_item.name}: only {line.pending_qty.normalize()} is pending on this order.')
        parsed.append((line, received, rejected, accepted, r))
    if not parsed or not any(p[3] > 0 for p in parsed):
        raise ValidationError('Enter the quantity accepted for at least one item.')
    number = get_and_increment_document_number(restaurant, 'grn', branch=branch, user_input_no=header.get('number'))
    grn = GoodsReceiptNote.objects.create(
        restaurant=restaurant, branch=branch, party=order.party, order=order, supplier_name=order.supplier_name,
        supplier_gstin=order.supplier_gstin, number=number, receipt_date=header.get('receipt_date') or timezone.localdate(),
        challan_no=(header.get('challan_no') or '').strip()[:100], vehicle_no=(header.get('vehicle_no') or '').strip()[:50],
        note=(header.get('note') or '').strip(), created_by=user)
    value = ZERO
    for line, received, rejected, accepted, r in parsed:
        net_rate = line.net_rate
        GoodsReceiptLine.objects.create(
            grn=grn, order_line=line, inventory_item=line.inventory_item, received_qty=received, rejected_qty=rejected,
            rejection_reason=(r.get('reason') or '').strip()[:255], rate=line.rate, gst_rate=line.gst_rate,
            hsn_code=line.hsn_code, batch_no=(r.get('batch') or '').strip()[:60], expiry_date=parse_date(r.get('expiry')))
        line.received_qty += accepted
        line.save(update_fields=['received_qty'])
        value += (accepted * net_rate).quantize(CENT)
        units = stock_units(accepted)
        if units:
            item = InventoryItem.objects.select_for_update().get(pk=line.inventory_item_id)
            item.adjust(units)
            if net_rate > 0:
                item.cost_price = item.price_per_unit = net_rate
                item.save(update_fields=['cost_price', 'price_per_unit'])
            note = f'GRN {number}' + (f' · batch {r.get("batch")}' if r.get('batch') else '')
            InventoryTransaction.objects.create(inventory_item=item, transaction_type=InventoryTransaction.TransactionType.INWARD_PURCHASE,
                                                delta=units, unit_cost=net_rate, goods_receipt=grn, note=note[:255], performed_by=user)
    grn.value = value
    grn.save(update_fields=['value'])
    refresh_order_status(order)
    return grn


# ============================================================ purchase bill

@transaction.atomic
def bill_from_grn(restaurant, branch, user, grn, data):
    """Book the supplier's invoice for a GRN. Stock is already in, so this only posts the payable
    and input GST; rates may differ from the PO (the bill wins, the difference shows in reports)."""
    grn = GoodsReceiptNote.objects.select_for_update().get(pk=grn.pk)
    if grn.status != GoodsReceiptNote.Status.PENDING_BILL:
        raise ValidationError('This receipt has already been billed.')
    invoice_no = (data.get('invoice_number') or '').strip()
    if not invoice_no:
        raise ValidationError("Enter the supplier's bill number.")
    dupes = PurchaseOrder.objects.filter(restaurant=restaurant, invoice_number__iexact=invoice_no)
    dupes = dupes.filter(party=grn.party) if grn.party_id else dupes.filter(supplier_name__iexact=grn.supplier_name)
    if dupes.exists():
        raise ValidationError(f'Bill {invoice_no} from {grn.supplier_name} is already booked.')
    bill_date = data.get('bill_date') or grn.receipt_date
    order = grn.order
    party = grn.party or resolve_party(restaurant, branch, name=grn.supplier_name, gstin=grn.supplier_gstin)
    interstate = order.is_interstate if order else is_interstate(restaurant, branch, party.gstin, party.state)
    reverse_charge = bool(data.get('reverse_charge', order.reverse_charge if order else False))
    due_date = data.get('due_date') or (bill_date + datetime.timedelta(days=order.credit_days) if order and order.credit_days else bill_date)
    rates = data.get('rates') or {}
    freight = max(dec(data.get('freight')), ZERO).quantize(CENT)
    round_off = dec(data.get('round_off')).quantize(CENT)
    if abs(round_off) > Decimal('5'):
        raise ValidationError('Round-off should be within ±₹5.')

    lines, subtotal, tax = [], ZERO, ZERO
    for gl in grn.lines.select_related('order_line', 'inventory_item'):
        qty = gl.accepted_qty
        if qty <= 0:
            continue
        rate = dec(rates.get(gl.pk), gl.rate)
        if rate < 0:
            raise ValidationError(f'Check the rate for {gl.inventory_item.name}.')
        disc = gl.order_line.discount_pct if gl.order_line else ZERO
        taxable, t = line_amounts(qty, rate, disc, gl.gst_rate)
        lines.append((gl, qty, (taxable / qty).quantize(CENT), taxable))
        subtotal += taxable
        tax += t
    cgst, sgst, igst = split_tax(tax, interstate)
    grand = subtotal + freight + round_off + (ZERO if reverse_charge else tax)
    method = data.get('payment_method') if data.get('payment_method') in PurchaseOrder.PaymentMethod.values else PurchaseOrder.PaymentMethod.CREDIT
    bill = PurchaseOrder.objects.create(
        restaurant=restaurant, branch=branch, party=party, supplier=_sync_supplier(restaurant, branch, party),
        supplier_name=party.name, supplier_gstin=party.gstin or grn.supplier_gstin,
        po_number=get_and_increment_document_number(restaurant, 'purchase', branch=branch),
        invoice_number=invoice_no, purchase_date=bill_date, due_date=due_date,
        subtotal=subtotal, tax_amount=tax, cgst_amount=cgst, sgst_amount=sgst, igst_amount=igst, freight_amount=freight,
        round_off=round_off, grand_total=grand, is_interstate=interstate, reverse_charge=reverse_charge,
        itc_eligible=bool(data.get('itc_eligible', True)), payment_method=method,
        payment_status=PurchaseOrder.PaymentStatus.UNPAID, narration=(data.get('narration') or '').strip(),
        source_order=order, stock_via_grn=True, created_by=user)
    for gl, qty, net_rate, taxable in lines:
        PurchaseOrderItem.objects.create(purchase_order=bill, inventory_item=gl.inventory_item, quantity=qty, unit_cost=net_rate,
                                         gst_rate=gl.gst_rate, hsn_code=gl.hsn_code, line_total=taxable, grn_line=gl)
    grn.status, grn.bill = GoodsReceiptNote.Status.BILLED, bill
    grn.save(update_fields=['status', 'bill'])
    InventoryTransaction.objects.filter(goods_receipt=grn).update(purchase_order=bill)
    if data.get('paid_now') and grand > 0:
        record_payment(bill, grand, bill_date, method if method != 'credit' else 'bank_transfer', data.get('payment_ref', ''), user)
    return bill


# ============================================================ payments & returns

def settle_status(bill):
    returned = bill.returned_amount
    due = (bill.grand_total or ZERO) - (bill.paid_amount or ZERO) - returned
    if due <= 0:
        bill.payment_status = PurchaseOrder.PaymentStatus.PAID
    elif (bill.paid_amount or 0) > 0 or returned > 0:
        bill.payment_status = PurchaseOrder.PaymentStatus.PARTIAL
    else:
        bill.payment_status = PurchaseOrder.PaymentStatus.UNPAID
    bill.save(update_fields=['payment_status', 'paid_amount', 'updated_at'])
    return bill


@transaction.atomic
def record_payment(bill, amount, paid_on, method, reference, user):
    bill = PurchaseOrder.objects.select_for_update().get(pk=bill.pk)
    amount = dec(amount).quantize(CENT)
    due = bill.balance_due
    if amount <= 0:
        raise ValidationError('Enter the amount paid.')
    if amount > due:
        raise ValidationError(f'Only ₹{due} is due on this bill.')
    if method not in PurchaseOrder.PaymentMethod.values or method == 'credit':
        method = PurchaseOrder.PaymentMethod.BANK_TRANSFER
    pay = PurchasePayment.objects.create(bill=bill, paid_on=paid_on or timezone.localdate(), amount=amount, method=method,
                                         reference=(reference or '').strip()[:100], created_by=user)
    bill.paid_amount = (bill.paid_amount or ZERO) + amount
    bill.payment_method = method
    settle_status(bill)
    return pay


def returnable(bill):
    """{bill_line_id: qty still returnable}"""
    done = defaultdict(lambda: ZERO)
    for rl in PurchaseReturnLine.objects.filter(bill_line__purchase_order=bill):
        done[rl.bill_line_id] += rl.quantity
    return {it.pk: max(it.quantity - done[it.pk], ZERO) for it in bill.items.all()}


@transaction.atomic
def create_return(restaurant, branch, user, bill, rows, header):
    """Debit Note against a bill: rows {bill_line_id: qty}. Stock goes out at the billed rate."""
    bill = PurchaseOrder.objects.select_for_update().get(pk=bill.pk)
    left = returnable(bill)
    picked = []
    for it in bill.items.select_related('inventory_item'):
        qty = dec(rows.get(it.pk))
        if qty <= 0:
            continue
        if qty > left.get(it.pk, ZERO):
            raise ValidationError(f'{it.inventory_item.name}: only {left.get(it.pk, ZERO).normalize()} can still be returned.')
        picked.append((it, qty))
    if not picked:
        raise ValidationError('Enter the quantity going back for at least one item.')
    reason = header.get('reason') if header.get('reason') in PurchaseReturn.Reason.values else PurchaseReturn.Reason.OTHER
    number = get_and_increment_document_number(restaurant, 'debit_note', branch=branch, user_input_no=header.get('number'))
    ret = PurchaseReturn.objects.create(
        restaurant=restaurant, branch=branch, party=bill.party, bill=bill, supplier_name=bill.supplier_name,
        supplier_gstin=bill.supplier_gstin or '', number=number, return_date=header.get('return_date') or timezone.localdate(),
        reason=reason, is_interstate=bill.is_interstate, note=(header.get('note') or '').strip(), created_by=user)
    subtotal = tax = ZERO
    for it, qty in picked:
        taxable, t = line_amounts(qty, it.unit_cost, ZERO, it.gst_rate or ZERO)
        PurchaseReturnLine.objects.create(purchase_return=ret, bill_line=it, inventory_item=it.inventory_item, quantity=qty,
                                          rate=it.unit_cost, gst_rate=it.gst_rate or ZERO, taxable_value=taxable, tax_amount=t)
        subtotal += taxable
        tax += t
        units = stock_units(qty)
        if units:
            item = InventoryItem.objects.select_for_update().get(pk=it.inventory_item_id)
            item.adjust(-units)
            InventoryTransaction.objects.create(inventory_item=item, transaction_type=InventoryTransaction.TransactionType.RETURN_OUT,
                                                delta=-units, unit_cost=it.unit_cost, purchase_order=bill, purchase_return=ret,
                                                note=f'Debit note {number}'[:255], performed_by=user)
    ret.subtotal, ret.tax_amount = subtotal, tax
    ret.cgst_amount, ret.sgst_amount, ret.igst_amount = split_tax(tax, ret.is_interstate)
    ret.grand_total = subtotal + (ZERO if bill.reverse_charge else tax)
    ret.save()
    settle_status(bill)
    return ret


# ============================================================ reports

def _scoped(qs, restaurant, branch, field='branch'):
    qs = qs.filter(restaurant=restaurant)
    return qs.filter(Q(**{field: branch}) | Q(**{f'{field}__isnull': True})) if branch else qs


def outstanding_orders(restaurant, branch):
    """Order Outstanding: every PO line still waiting for goods."""
    today = timezone.localdate()
    rows = []
    lines = (ProcurementOrderLine.objects.filter(order__in=_scoped(ProcurementOrder.objects, restaurant, branch)
                                                 .filter(status__in=ProcurementOrder.OPEN_STATUSES))
             .select_related('order', 'inventory_item').order_by('order__expected_date', 'order__order_date'))
    for l in lines:
        pending = l.pending_qty
        if pending <= 0:
            continue
        exp = l.order.expected_date
        rows.append({'order': l.order, 'item': l.inventory_item, 'ordered': l.quantity, 'received': l.received_qty, 'pending': pending,
                     'value': (pending * l.net_rate).quantize(CENT), 'expected': exp,
                     'days_late': (today - exp).days if exp and exp < today else 0})
    return rows


def pending_bills(restaurant, branch):
    """Purchase Bills Pending: goods received, supplier bill not booked yet."""
    today = timezone.localdate()
    qs = _scoped(GoodsReceiptNote.objects, restaurant, branch).filter(status=GoodsReceiptNote.Status.PENDING_BILL)
    return [{'grn': g, 'age': (today - g.receipt_date).days} for g in qs.select_related('order', 'party')]


AGING_BUCKETS = (('not_due', 'Not due'), ('d30', '1–30 days'), ('d60', '31–60 days'), ('d90', '61–90 days'), ('d90p', '90+ days'))


def payables(restaurant, branch):
    """Bill-wise outstanding with ageing on the due date."""
    today = timezone.localdate()
    qs = _scoped(PurchaseOrder.objects, restaurant, branch).exclude(payment_status='paid').select_related('party')
    rows, totals = [], {k: ZERO for k, _ in AGING_BUCKETS}
    for b in qs.order_by('due_date', 'purchase_date'):
        due = b.balance_due
        if due <= 0:
            continue
        overdue = (today - (b.due_date or b.purchase_date)).days
        bucket = 'not_due' if overdue <= 0 else 'd30' if overdue <= 30 else 'd60' if overdue <= 60 else 'd90' if overdue <= 90 else 'd90p'
        totals[bucket] += due
        rows.append({'bill': b, 'due': due, 'overdue': max(overdue, 0), 'bucket': bucket})
    return rows, [(k, label, totals[k]) for k, label in AGING_BUCKETS]


def input_gst(restaurant, branch, start, end):
    """Input tax by rate for the period, split CGST/SGST/IGST, with RCM and ineligible ITC shown
    separately and debit notes reversed — the purchase side of GSTR-3B table 4."""
    bills = (_scoped(PurchaseOrder.objects, restaurant, branch).filter(purchase_date__gte=start, purchase_date__lte=end)
             .prefetch_related('items'))
    slabs = defaultdict(lambda: {'taxable': ZERO, 'cgst': ZERO, 'sgst': ZERO, 'igst': ZERO})
    summary = {k: ZERO for k in ('eligible', 'rcm', 'ineligible', 'reversed', 'taxable', 'bills')}
    for b in bills:
        summary['bills'] += 1
        for it in b.items.all():
            rate = it.gst_rate or ZERO
            t = (it.line_total * rate / HUNDRED).quantize(CENT)
            c, s, i = split_tax(t, b.is_interstate)
            slab = slabs[rate]
            slab['taxable'] += it.line_total
            slab['cgst'] += c
            slab['sgst'] += s
            slab['igst'] += i
            summary['taxable'] += it.line_total
            if b.reverse_charge:
                summary['rcm'] += t
            if not b.itc_eligible:
                summary['ineligible'] += t
            else:
                summary['eligible'] += t
    for r in _scoped(PurchaseReturn.objects, restaurant, branch).filter(return_date__gte=start, return_date__lte=end).select_related('bill'):
        if not r.bill or r.bill.itc_eligible:
            summary['reversed'] += r.tax_amount
    summary['net_itc'] = summary['eligible'] - summary['reversed']
    return sorted(((rate, v) for rate, v in slabs.items()), key=lambda kv: kv[0]), summary


def price_history(restaurant, branch, item):
    rows = list(PurchaseOrderItem.objects.filter(inventory_item=item, purchase_order__in=_scoped(PurchaseOrder.objects, restaurant, branch))
                .select_related('purchase_order').order_by('-purchase_order__purchase_date', '-id')[:100])
    by_supplier = defaultdict(list)
    for r in rows:
        by_supplier[r.purchase_order.supplier_name].append(r.unit_cost)
    suppliers = sorted(({'name': n, 'last': v[0], 'low': min(v), 'avg': (sum(v) / len(v)).quantize(CENT), 'count': len(v)}
                        for n, v in by_supplier.items()), key=lambda s: s['avg'])
    return rows, suppliers


def supplier_scorecard(restaurant, branch, start, end):
    """On-time delivery, fill rate, rejection rate and spend per supplier."""
    data = defaultdict(lambda: {'orders': 0, 'ordered': ZERO, 'received': ZERO, 'rejected': ZERO, 'grns': 0, 'on_time': 0, 'spend': ZERO})
    orders = _scoped(ProcurementOrder.objects, restaurant, branch).filter(order_date__gte=start, order_date__lte=end).exclude(status__in=['draft', 'cancelled'])
    for o in orders.prefetch_related('lines'):
        d = data[o.supplier_name]
        d['orders'] += 1
        d['ordered'] += sum((l.quantity for l in o.lines.all()), ZERO)
    grns = _scoped(GoodsReceiptNote.objects, restaurant, branch).filter(receipt_date__gte=start, receipt_date__lte=end).select_related('order')
    for g in grns.prefetch_related('lines'):
        d = data[g.supplier_name]
        d['grns'] += 1
        if not g.order or not g.order.expected_date or g.receipt_date <= g.order.expected_date:
            d['on_time'] += 1
        for l in g.lines.all():
            d['received'] += l.received_qty
            d['rejected'] += l.rejected_qty
    for row in (_scoped(PurchaseOrder.objects, restaurant, branch).filter(purchase_date__gte=start, purchase_date__lte=end)
                .values('supplier_name').annotate(s=Sum('grand_total'))):
        data[row['supplier_name']]['spend'] += row['s'] or ZERO

    def pct(a, b):
        return round(float(a) / float(b) * 100, 1) if b else None
    out = [{'name': n, **d, 'on_time_pct': pct(d['on_time'], d['grns']),
            'fill_pct': pct(d['received'] - d['rejected'], d['ordered']) if d['ordered'] else None,
            'reject_pct': pct(d['rejected'], d['received'])} for n, d in data.items()]
    return sorted(out, key=lambda r: -r['spend'])


def reorder_suggestions(restaurant, branch):
    """Items at or below their reorder level, less what is already on open orders, with the last
    supplier and rate — Tally's 'Order position' + reorder status in one list."""
    on_order = defaultdict(lambda: ZERO)
    for l in ProcurementOrderLine.objects.filter(order__in=_scoped(ProcurementOrder.objects, restaurant, branch)
                                                 .filter(status__in=('draft',) + ProcurementOrder.OPEN_STATUSES)):
        on_order[l.inventory_item_id] += l.pending_qty
    last = {}
    for it in (PurchaseOrderItem.objects.filter(purchase_order__in=_scoped(PurchaseOrder.objects, restaurant, branch))
               .select_related('purchase_order').order_by('purchase_order__purchase_date', 'id')):
        last[it.inventory_item_id] = (it.purchase_order.party_id, it.purchase_order.supplier_name, it.unit_cost)
    rows = []
    for item in items_for(restaurant, branch).filter(is_active=True).order_by('name'):
        qty, level = item.quantity or 0, item.reorder_level or 0
        if qty > level:
            continue
        target = max(level * 2, 1)
        suggest = Decimal(target - qty) - on_order[item.pk]
        if suggest <= 0:
            continue
        party_id, supplier, rate = last.get(item.pk, (None, '', item.unit_cost))
        rows.append({'item': item, 'stock': qty, 'level': level, 'on_order': on_order[item.pk], 'suggest': suggest,
                     'party_id': party_id, 'supplier': supplier, 'rate': rate})
    return rows
