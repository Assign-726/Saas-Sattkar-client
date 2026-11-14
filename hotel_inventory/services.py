"""Hotel stores business rules. ``move`` is the only code that changes stock.

Costing is weighted average (WAC), kept per item: an inward move with a cost re-averages the
item's cost over everything on hand; every other move is valued at the current WAC.
"""
import csv
import datetime
import io
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from hotel_inventory.models import (
    ItemUnit, Location, StockLevel, StockMove, StoreCategory, StoreItem, Vendor,
    PurchaseOrder, POLine, GoodsReceipt, GRNLine, VendorPayment,
    Requisition, RequisitionLine, StockAdjustment, StockCount, StockCountLine,
    MinibarProfile, LinenPar, LaundryManifest, LaundryManifestLine, PurchaseBill, PurchaseBillLine
)

D0 = Decimal('0')
Q3 = Decimal('0.001')
Q4 = Decimal('0.0001')
CENT = Decimal('0.01')

INWARD = {StockMove.Type.OPENING, StockMove.Type.RECEIPT}


class StockError(Exception):
    """A move that cannot be posted (not enough stock, wrong branch, bad quantity)."""


def dec(value, default=None):
    if value is None or value == '':
        return default
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return default


def fmt_qty(value):
    value = Decimal(value or 0).quantize(Q3)
    return f"{value.normalize():f}" if value == value.to_integral() else f"{value:f}".rstrip('0')


# ------------------------------------------------------------------ units

def unit_factor(item, unit_name):
    """Base units in one ``unit_name`` of this item (1 for the base unit)."""
    unit_name = (unit_name or '').strip()
    if not unit_name or unit_name.lower() == item.unit.lower():
        return Decimal('1')
    for u in item.units.all():
        if u.name.lower() == unit_name.lower():
            return u.factor
    raise StockError(f'"{unit_name}" is not a unit of {item.name}. Add it on the item first.')


def to_base(item, qty, unit_name=''):
    return (Decimal(qty) * unit_factor(item, unit_name)).quantize(Q3)


# ------------------------------------------------------------------ the ledger

def move(branch, item, qty, *, move_type, src=None, dst=None, unit_cost=None, unit='', user=None,
         reason='', note='', ref_type='', ref_id=None, ref_label='', allow_negative=False, reverses=None):
    """Post one stock movement and return the ``StockMove``.

    ``src`` only = stock goes out (issue to a guest, wastage); ``dst`` only = stock comes in
    (opening, goods received); both = a transfer between locations. ``qty`` is in ``unit``
    (default the item's base unit). Raises ``StockError`` instead of going below zero.
    """
    qty = dec(qty)
    if qty is None or qty <= 0:
        raise StockError('Quantity must be more than zero.')
    if src is None and dst is None:
        raise StockError('Choose where the stock comes from or goes to.')
    if src is not None and dst is not None and src.pk == dst.pk:
        raise StockError('From and to are the same location.')
    if item.branch_id != branch.pk or any(loc is not None and loc.branch_id != branch.pk for loc in (src, dst)):
        raise StockError('Item and locations must belong to this hotel.')
    for loc in (src, dst):
        if loc is not None and not loc.is_active:
            raise StockError(f'{loc.path} is closed.')

    entered_qty, entered_unit = qty.quantize(Q3), (unit or '').strip()
    base_qty = to_base(item, qty, entered_unit)
    if base_qty <= 0:
        raise StockError('Quantity is too small.')
    if unit_cost is not None:
        unit_cost = dec(unit_cost)
        if unit_cost is None or unit_cost < 0:
            raise StockError('Cost cannot be negative.')
        unit_cost = unit_cost / unit_factor(item, entered_unit)  # cost was per the entered unit

    with transaction.atomic():
        item = StoreItem.objects.select_for_update().get(pk=item.pk)  # serialises WAC updates for the item
        levels = {}
        for loc in sorted((l for l in (src, dst) if l is not None), key=lambda l: l.pk):  # fixed order: no deadlocks
            lvl, _ = _level(item, loc)
            levels[loc.pk] = StockLevel.objects.select_for_update().get(pk=lvl.pk)

        if src is not None:
            have = levels[src.pk].quantity
            if have - base_qty < 0 and not allow_negative:
                raise StockError(f'Only {fmt_qty(have)} {item.unit} of {item.name} at {src.path}.')

        cost = item.wac
        if src is None and move_type in INWARD and unit_cost is not None:
            on_hand = StockLevel.objects.filter(item=item).aggregate(s=Sum('quantity'))['s'] or D0
            if on_hand <= 0:
                item.wac = unit_cost.quantize(Q4)
            else:
                item.wac = ((on_hand * item.wac + base_qty * unit_cost) / (on_hand + base_qty)).quantize(Q4)
            item.save(update_fields=['wac', 'updated_at'])
            cost = unit_cost

        if src is not None:
            lv = levels[src.pk]
            lv.quantity = (lv.quantity - base_qty).quantize(Q3)
            lv.save(update_fields=['quantity', 'updated_at'])
        if dst is not None:
            lv = levels[dst.pk]
            lv.quantity = (lv.quantity + base_qty).quantize(Q3)
            lv.save(update_fields=['quantity', 'updated_at'])

        mv = StockMove.objects.create(
            branch=branch, item=item, from_location=src, to_location=dst, move_type=move_type, quantity=base_qty,
            entered_qty=entered_qty, entered_unit=entered_unit if entered_unit.lower() != item.unit.lower() else '',
            unit_cost=Decimal(cost).quantize(Q4), value=(base_qty * Decimal(cost)).quantize(CENT),
            from_balance=levels[src.pk].quantity if src is not None else None,
            to_balance=levels[dst.pk].quantity if dst is not None else None,
            wac_after=item.wac, reason=reason[:40], note=(note or '')[:255], ref_type=ref_type, ref_id=ref_id,
            ref_label=(ref_label or '')[:60], reverses=reverses, performed_by=user if getattr(user, 'pk', None) else None)

    if src is not None:
        transaction.on_commit(lambda: check_low_stock(branch, item, src))
    return mv


def _level(item, loc):
    try:
        return StockLevel.objects.get_or_create(item=item, location=loc)
    except IntegrityError:  # created by a parallel move a moment ago
        return StockLevel.objects.get(item=item, location=loc), False


def reverse_move(mv, user, note=''):
    """Undo a posted move with an equal and opposite one (the original stays in the ledger)."""
    if StockMove.objects.filter(reverses=mv).exists():
        raise StockError('This move has already been reversed.')
    if mv.move_type == StockMove.Type.REVERSAL:
        raise StockError('A reversal cannot itself be reversed.')
    return move(mv.branch, mv.item, mv.quantity, move_type=StockMove.Type.REVERSAL, src=mv.to_location, dst=mv.from_location,
                user=user, note=note or f'Reverses #{mv.pk}', ref_type=mv.ref_type, ref_id=mv.ref_id, ref_label=mv.ref_label,
                reverses=mv)


# ------------------------------------------------------------------ alerts

def check_low_stock(branch, item, location=None):
    """Raise a (once a day) low-stock notification for this item, at a location or overall."""
    today = timezone.localdate().isoformat()
    try:
        from notifications import service
        url = reverse('hotel_admin:stores', kwargs={'restaurant_slug': branch.restaurant.slug}) + '?low=1'
        if location is not None:
            lvl = StockLevel.objects.filter(item=item, location=location).first()
            if lvl and lvl.is_low:
                service.notify(branch.restaurant, 'low_stock', f'Low stock · {item.name}', branch=branch, url=url, source='hotel',
                               level='warning', dedupe_key=f'hinv-low-{item.pk}-{location.pk}-{today}',
                               body=f'{fmt_qty(lvl.quantity)} {item.unit} left at {location.path} (minimum {fmt_qty(lvl.min_level)}).')
        total = item.on_hand()
        if item.reorder_level > 0 and total <= item.reorder_level:
            service.notify(branch.restaurant, 'low_stock', f'Reorder · {item.name}', branch=branch, url=url, source='hotel',
                           level='warning', dedupe_key=f'hinv-reorder-{item.pk}-{today}',
                           body=f'{fmt_qty(total)} {item.unit} left in all stores (reorder level {fmt_qty(item.reorder_level)}).')
    except Exception:  # an alert must never break a stock posting
        pass


def low_stock(branch):
    """(location rows below their minimum, items below their reorder level)."""
    rows = [l for l in StockLevel.objects.filter(item__branch=branch, item__is_active=True, min_level__gt=0)
            .select_related('item', 'location', 'location__parent') if l.quantity <= l.min_level]
    totals = {r['item']: r['s'] for r in StockLevel.objects.filter(item__branch=branch).values('item').annotate(s=Sum('quantity'))}
    items = []
    for it in StoreItem.objects.filter(branch=branch, is_active=True, reorder_level__gt=0).select_related('preferred_vendor'):
        it.total = totals.get(it.pk, D0)
        if it.total <= it.reorder_level:
            items.append(it)
    return rows, items


# ------------------------------------------------------------------ setup

def ensure_main_store(branch):
    """Every hotel starts with a Main Store so stock always has somewhere to go."""
    if not Location.objects.filter(branch=branch).exists():
        Location.objects.create(branch=branch, name='Main Store', kind=Location.Kind.STORE)


# ------------------------------------------------------------------ CSV import

ITEM_COLUMNS = ['name', 'category', 'unit', 'kind', 'sku', 'barcode', 'hsn_code', 'gst_rate', 'reorder_level', 'reorder_qty',
                'sale_price', 'pack_unit', 'pack_factor']
OPENING_COLUMNS = ['item', 'location', 'quantity', 'unit', 'unit_cost']


def _rows(upload):
    raw = upload.read()
    if len(raw) > 2 * 1024 * 1024:
        raise StockError('The file is larger than 2 MB.')
    text = raw.decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [(f or '').strip().lower().replace(' ', '_') for f in (reader.fieldnames or [])]
    return list(reader)


def import_items(branch, upload):
    """Create or update items from a CSV (``ITEM_COLUMNS``). Returns (created, updated, errors)."""
    created = updated = 0
    errors = []
    kinds = {k.lower(): k for k in StoreItem.Kind.values}
    for n, row in enumerate(_rows(upload), start=2):
        name = (row.get('name') or '').strip()[:150]
        if not name:
            continue
        try:
            with transaction.atomic():
                cat = None
                if (row.get('category') or '').strip():
                    cat, _ = StoreCategory.objects.get_or_create(branch=branch, parent=None, name=row['category'].strip()[:100])
                item, is_new = StoreItem.objects.get_or_create(branch=branch, name=name, defaults={'unit': (row.get('unit') or 'pc').strip()[:20] or 'pc'})
                if cat:
                    item.category = cat
                if (row.get('kind') or '').strip().lower() in kinds:
                    item.kind = kinds[row['kind'].strip().lower()]
                for f in ('sku', 'barcode', 'hsn_code'):
                    if (row.get(f) or '').strip():
                        setattr(item, f, row[f].strip()[:64 if f == 'barcode' else 50 if f == 'sku' else 20])
                for f in ('gst_rate', 'reorder_level', 'reorder_qty', 'sale_price'):
                    v = dec(row.get(f))
                    if v is not None:
                        setattr(item, f, v)
                item.save()
                pu, pf = (row.get('pack_unit') or '').strip()[:20], dec(row.get('pack_factor'))
                if pu and pf and pf > 0 and pu.lower() != item.unit.lower():
                    ItemUnit.objects.update_or_create(item=item, name=pu, defaults={'factor': pf, 'is_purchase_default': True})
            created += is_new
            updated += not is_new
        except IntegrityError:
            errors.append(f'Row {n}: barcode "{row.get("barcode")}" is already used by another item.')
        except Exception as e:  # keep going; report the row
            errors.append(f'Row {n}: {e}')
    return created, updated, errors


def import_opening(branch, upload, user):
    """Post opening stock from a CSV (``OPENING_COLUMNS``). Returns (posted, errors)."""
    posted, errors = 0, []
    items = {i.name.lower(): i for i in StoreItem.objects.filter(branch=branch).prefetch_related('units')}
    locs = {}
    for loc in Location.objects.filter(branch=branch, is_active=True).select_related('parent'):
        locs[loc.name.lower()] = loc
        locs[loc.path.lower()] = loc
    for n, row in enumerate(_rows(upload), start=2):
        name = (row.get('item') or '').strip().lower()
        if not name:
            continue
        item = items.get(name)
        loc = locs.get((row.get('location') or 'Main Store').strip().lower())
        if item is None:
            errors.append(f'Row {n}: no item called "{row.get("item")}".')
            continue
        if loc is None:
            errors.append(f'Row {n}: no location called "{row.get("location")}".')
            continue
        try:
            move(branch, item, row.get('quantity'), move_type=StockMove.Type.OPENING, dst=loc, unit=row.get('unit') or '',
                 unit_cost=dec(row.get('unit_cost')), user=user, note='Opening stock (import)')
            posted += 1
        except StockError as e:
            errors.append(f'Row {n}: {e}')
    return posted, errors


def stock_value(branch, location=None):
    """Value of what is on hand at WAC — everywhere, or at a location and the ones under it."""
    from django.db.models import DecimalField, ExpressionWrapper
    levels = StockLevel.objects.filter(item__branch=branch)
    if location is not None:
        levels = levels.filter(location_id__in=location.descendant_ids())
    total = levels.aggregate(v=Sum(ExpressionWrapper(F('quantity') * F('item__wac'),
                                                     output_field=DecimalField(max_digits=20, decimal_places=4))))['v']
    return Decimal(total or 0).quantize(CENT)


def stock_rows(branch, *, location=None, category=None, search='', low_only=False, include_inactive=False):
    """Items with their quantity (at ``location`` and below it, or everywhere) and value at WAC."""
    items = StoreItem.objects.filter(branch=branch).select_related('category', 'preferred_vendor')
    if not include_inactive:
        items = items.filter(is_active=True)
    if category:
        items = items.filter(category_id__in=[category.pk] + list(category.children.values_list('pk', flat=True)))
    if search:
        items = items.filter(Q(name__icontains=search) | Q(sku__iexact=search) | Q(barcode=search))
    levels = StockLevel.objects.filter(item__branch=branch)
    if location is not None:
        levels = levels.filter(location_id__in=location.descendant_ids())
    qty = {r['item']: r['s'] for r in levels.values('item').annotate(s=Sum('quantity'))}
    low_at = set(StockLevel.objects.filter(item__branch=branch, min_level__gt=0, quantity__lte=F('min_level'))
                 .values_list('item_id', flat=True))
    totals = qty if location is None else {r['item']: r['s'] for r in StockLevel.objects.filter(item__branch=branch)
                                                                         .values('item').annotate(s=Sum('quantity'))}
    rows = []
    for it in items:
        it.qty = qty.get(it.pk, D0)
        it.value = (it.qty * it.wac).quantize(CENT)
        total_low = it.reorder_level > 0 and totals.get(it.pk, D0) <= it.reorder_level
        it.low = total_low or it.pk in low_at
        if low_only and not it.low:
            continue
        rows.append(it)
    return rows


# ============================================================ helpers & numbers

def next_doc_number(model_cls, field_name, prefix, branch):
    ym = timezone.localdate().strftime('%Y%m')
    lead = f"{prefix}-{ym}-"
    last = model_cls.objects.filter(branch=branch, **{f"{field_name}__startswith": lead}).order_by(f"-{field_name}").first()
    seq = 1
    if last:
        val = getattr(last, field_name)
        try:
            seq = int(val.split('-')[-1]) + 1
        except (ValueError, IndexError):
            seq = model_cls.objects.filter(branch=branch).count() + 1
    return f"{lead}{seq:04d}"


def ensure_special_location(branch, kind, default_name):
    loc = Location.objects.filter(branch=branch, kind=kind).first()
    if not loc:
        loc = Location.objects.create(branch=branch, kind=kind, name=default_name)
    return loc


def _post_expense(branch, category_name, amount, note, date=None):
    try:
        from finance.models import Expense, ExpenseCategory
        if not amount or amount <= 0:
            return None
        ExpenseCategory.ensure_defaults(branch.restaurant)
        cat, _ = ExpenseCategory.objects.get_or_create(restaurant=branch.restaurant, name=category_name)
        return Expense.objects.create(
            restaurant=branch.restaurant,
            branch=branch,
            category=cat,
            department=Expense.Department.HOTEL,
            date=date or timezone.localdate(),
            amount=amount,
            payment_method=Expense.Method.CREDIT,
            note=note[:300]
        )
    except Exception:
        return None


# ============================================================ Phase 2: Procurement

def create_po(branch, vendor, lines_data, user=None, expected_date=None, notes=''):
    """Create a PurchaseOrder with lines. lines_data: list of dict(item, unit_name, quantity, unit_price, tax_percent)."""
    if not lines_data:
        raise StockError('Add at least one item to the purchase order.')
    po_num = next_doc_number(PurchaseOrder, 'po_number', 'PO', branch)
    with transaction.atomic():
        po = PurchaseOrder.objects.create(
            branch=branch, vendor=vendor, po_number=po_num, status=PurchaseOrder.Status.DRAFT,
            expected_date=expected_date, notes=notes, created_by=user
        )
        total = D0
        for l in lines_data:
            item = l['item']
            qty = dec(l.get('quantity'), D0)
            price = dec(l.get('unit_price'), D0)
            tax_pct = dec(l.get('tax_percent'), item.effective_gst_rate or D0)
            u_name = (l.get('unit_name') or item.unit).strip()
            if qty <= 0:
                continue
            if price < 0:
                raise StockError(f'{item.name}: price cannot be negative.')
            unit_factor(item, u_name)  # raises for a pack size the item does not have
            line_tot = (qty * price * (Decimal('1') + (tax_pct / Decimal('100')))).quantize(CENT)
            POLine.objects.create(
                po=po, item=item, unit_name=u_name, quantity=qty, unit_price=price,
                tax_percent=tax_pct, total_amount=line_tot
            )
            total += line_tot
        if not po.lines.exists():
            raise StockError('Add at least one item with a quantity to the purchase order.')
        po.total_amount = total
        po.save(update_fields=['total_amount'])
    return po


def approve_po(po, user=None):
    if po.status != PurchaseOrder.Status.DRAFT:
        raise StockError(f'Only draft purchase orders can be approved (currently {po.get_status_display()}).')
    po.status = PurchaseOrder.Status.APPROVED
    po.approved_by = user
    po.approved_at = timezone.now()
    po.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return po


def cancel_po(po, user=None):
    if po.status in (PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CLOSED):
        raise StockError('Cannot cancel a received or closed purchase order.')
    po.status = PurchaseOrder.Status.CANCELLED
    po.save(update_fields=['status', 'updated_at'])
    return po


def auto_draft_pos(branch, user=None):
    """Group items below reorder level by their preferred vendor and create draft POs."""
    _, low_items = low_stock(branch)
    # Items already on an order that is still open are not ordered twice
    on_order = set(POLine.objects.filter(po__branch=branch, po__status__in=[
        PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIALLY_RECEIVED])
        .values_list('item_id', flat=True))
    by_vendor = {}
    for item in low_items:
        if item.preferred_vendor and item.preferred_vendor.is_active and item.pk not in on_order:
            by_vendor.setdefault(item.preferred_vendor, []).append(item)
    created_pos = []
    for vendor, items in by_vendor.items():
        lines = []
        for item in items:
            order_qty = item.reorder_qty if item.reorder_qty > 0 else max(item.reorder_level * 2, Decimal('10'))
            default_unit = item.units.filter(is_purchase_default=True).first()
            u_name = default_unit.name if default_unit else item.unit
            u_factor = default_unit.factor if default_unit else Decimal('1')
            qty_in_unit = (order_qty / u_factor).quantize(Q3)
            unit_price = (item.wac * u_factor).quantize(Q4) if item.wac > 0 else Decimal('10.0000')
            lines.append({
                'item': item,
                'unit_name': u_name,
                'quantity': max(qty_in_unit, Decimal('1')),
                'unit_price': unit_price,
                'tax_percent': item.effective_gst_rate or D0
            })
        if lines:
            po = create_po(branch, vendor, lines, user=user, notes='Auto-drafted from reorder levels')
            created_pos.append(po)
    return created_pos


def receive_grn(branch, *, vendor, location, lines_data, po=None, invoice_number='', invoice_date=None,
                is_itc_eligible=True, notes='', user=None):
    """Receive goods from vendor into location, updating stock, WAC, GST breakdown, and PO rollup."""
    if not lines_data:
        raise StockError('Add at least one line to the goods receipt.')
    if po is not None:
        if po.status not in (PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIALLY_RECEIVED):
            raise StockError(f'{po.po_number} is {po.get_status_display().lower()} — only approved orders can be received.')
        if po.vendor_id != vendor.pk:
            raise StockError(f'{po.po_number} was placed with {po.vendor.name}, not {vendor.name}.')
    grn_num = next_doc_number(GoodsReceipt, 'grn_number', 'GRN', branch)
    with transaction.atomic():
        grn = GoodsReceipt.objects.create(
            branch=branch, grn_number=grn_num, po=po, vendor=vendor, location=location,
            status=GoodsReceipt.Status.RECEIVED, invoice_number=(invoice_number or '').strip()[:60],
            invoice_date=invoice_date, is_itc_eligible=is_itc_eligible, notes=notes, received_by=user
        )
        subtotal = D0
        tax_total = D0
        cgst_total = D0
        sgst_total = D0
        igst_total = D0

        branch_state = (branch.state or '').strip().lower()
        vendor_state = (vendor.state or '').strip().lower()
        is_interstate = bool(branch_state and vendor_state and branch_state != vendor_state)

        for ld in lines_data:
            item = ld['item']
            po_line = ld.get('po_line')
            rec_qty = dec(ld.get('received_qty'), D0)
            rej_qty = dec(ld.get('rejected_qty'), D0)
            u_cost = dec(ld.get('unit_cost'), D0)
            tax_pct = dec(ld.get('tax_percent'), item.effective_gst_rate or D0)
            u_name = (ld.get('unit_name') or item.unit).strip()
            batch = (ld.get('batch_number') or '').strip()[:60]
            exp_date = ld.get('expiry_date')

            if rec_qty < 0 or rej_qty < 0 or u_cost < 0:
                raise StockError(f'{item.name}: quantities and cost cannot be negative.')
            if rej_qty > rec_qty:
                raise StockError(f'{item.name}: rejected ({fmt_qty(rej_qty)}) is more than received ({fmt_qty(rec_qty)}).')
            if po_line is not None and po_line.po_id != getattr(po, 'pk', None):
                raise StockError(f'{item.name}: that order line is not on this purchase order.')
            accepted = rec_qty - rej_qty  # rejected goods go back on the vendor's truck: no stock, no bill
            line_sub = (accepted * u_cost).quantize(CENT)
            line_tax = (line_sub * (tax_pct / Decimal('100'))).quantize(CENT)
            line_tot = line_sub + line_tax

            GRNLine.objects.create(
                grn=grn, po_line=po_line, item=item, unit_name=u_name,
                received_qty=rec_qty, rejected_qty=rej_qty, unit_cost=u_cost,
                tax_percent=tax_pct, batch_number=batch, expiry_date=exp_date,
                subtotal=line_sub, tax_amount=line_tax, total_amount=line_tot
            )

            if accepted > 0:
                move(branch, item, accepted, move_type=StockMove.Type.RECEIPT, dst=location, unit=u_name,
                     unit_cost=u_cost, user=user, ref_type='grn', ref_id=grn.pk, ref_label=grn.grn_number,
                     note=f"GRN #{grn.grn_number}" + (f" (Batch {batch})" if batch else ""))
                if po_line:
                    # The order line may be in another pack size than this receipt line (cases vs pieces)
                    in_po_unit = (to_base(item, accepted, u_name) / unit_factor(item, po_line.unit_name)).quantize(Q3)
                    po_line.received_qty += in_po_unit
                    po_line.save(update_fields=['received_qty'])

            subtotal += line_sub
            tax_total += line_tax
            if is_interstate:
                igst_total += line_tax
            else:
                cgst_total += (line_tax / Decimal('2')).quantize(CENT)
                sgst_total += (line_tax / Decimal('2')).quantize(CENT)

        grn.subtotal = subtotal
        grn.tax_amount = tax_total
        grn.cgst_amount = cgst_total
        grn.sgst_amount = sgst_total
        grn.igst_amount = igst_total
        grn.total_amount = subtotal + tax_total
        grn.save(update_fields=['subtotal', 'tax_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount'])

        if po:
            po.update_received_status()

    return grn


def record_vendor_payment(branch, vendor, amount, payment_method='BANK', payment_date=None,
                          reference='', notes='', grn=None, bill=None, user=None):
    amt = dec(amount)
    if amt is None or amt <= 0:
        raise StockError('Payment amount must be greater than zero.')
    if bill is not None:
        if bill.vendor_id != vendor.pk or bill.branch_id != branch.pk:
            raise StockError(f'Bill {bill.bill_number} is not from {vendor.name}.')
        if bill.status != PurchaseBill.Status.POSTED:
            raise StockError(f'Bill {bill.bill_number} is cancelled.')
        if amt > bill.due_amount:
            raise StockError(f'Only ₹{bill.due_amount} is still due on bill {bill.invoice_number}.')
    if grn is not None and grn.bill_id:
        bill, grn = grn.bill, grn  # a billed delivery is paid through its bill
    p_date = payment_date or timezone.localdate()
    return VendorPayment.objects.create(
        branch=branch, vendor=vendor, grn=grn, bill=bill, amount=amt.quantize(CENT),
        payment_date=p_date, payment_method=payment_method, reference=(reference or '').strip()[:80],
        notes=notes, created_by=user
    )


def payable_documents(branch, vendor=None):
    """What the hotel owes, document by document: posted bills, plus deliveries (GRNs) whose bill
    has not been entered yet (a provisional payable at the GRN value)."""
    bills = PurchaseBill.objects.filter(branch=branch, status=PurchaseBill.Status.POSTED).select_related('vendor')
    grns = GoodsReceipt.objects.filter(branch=branch, status=GoodsReceipt.Status.RECEIVED, bill__isnull=True).select_related('vendor')
    if vendor is not None:
        bills, grns = bills.filter(vendor=vendor), grns.filter(vendor=vendor)
    docs = [{'kind': 'bill', 'doc': b, 'vendor': b.vendor, 'number': b.invoice_number, 'ref': b.bill_number,
             'date': b.invoice_date, 'due_date': b.due_date, 'amount': b.total_amount} for b in bills]
    docs += [{'kind': 'grn', 'doc': g, 'vendor': g.vendor, 'number': g.invoice_number or '—', 'ref': g.grn_number,
              'date': g.invoice_date or g.created_at.date(), 'due_date': None, 'amount': g.total_amount} for g in grns]
    return sorted(docs, key=lambda d: (d['date'], d['ref']))


def vendor_payables(branch):
    """Per vendor: billed, paid, balance and ageing (0-30, 31-60, 61-90, 90+ days by bill date).
    Payments settle the oldest documents first."""
    today = timezone.localdate()
    by_vendor = {}
    for d in payable_documents(branch):
        by_vendor.setdefault(d['vendor'].pk, []).append(d)
    paid = {r['vendor']: r['s'] for r in VendorPayment.objects.filter(branch=branch).values('vendor').annotate(s=Sum('amount'))}
    results = []
    for v in Vendor.objects.filter(branch=branch, is_active=True):
        docs = by_vendor.get(v.pk, [])
        total_billed = sum((d['amount'] for d in docs), D0)
        total_paid = paid.get(v.pk) or D0
        buckets = [D0, D0, D0, D0]
        left = total_paid
        for d in docs:
            due = d['amount']
            if left >= due:
                left -= due
                continue
            due, left = due - left, D0
            age = (today - d['date']).days
            buckets[0 if age <= 30 else 1 if age <= 60 else 2 if age <= 90 else 3] += due
        if not docs and not total_paid:
            continue
        results.append({
            'vendor': v, 'total_billed': total_billed, 'total_paid': total_paid,
            'balance': max(total_billed - total_paid, D0), 'advance': max(total_paid - total_billed, D0),
            'current': buckets[0], 'days_31_60': buckets[1], 'days_61_90': buckets[2], 'days_90_plus': buckets[3],
            'unbilled_grns': sum(1 for d in docs if d['kind'] == 'grn'),
        })
    return results


# ============================================================ Purchase bills

def revalue(branch, item, value_delta, *, user=None, ref_type='', ref_id=None, ref_label='', note=''):
    """Put a cost difference into stock without changing quantity: the item's WAC moves so that
    on-hand value changes by ``value_delta``. If nothing is on hand (already used up) WAC cannot
    absorb it; the difference is still recorded in the ledger."""
    value_delta = Decimal(value_delta).quantize(CENT)
    if value_delta == 0:
        return None
    with transaction.atomic():
        item = StoreItem.objects.select_for_update().get(pk=item.pk)
        on_hand = StockLevel.objects.filter(item=item).aggregate(s=Sum('quantity'))['s'] or D0
        if on_hand > 0:
            item.wac = max(((on_hand * item.wac + value_delta) / on_hand).quantize(Q4), D0)
            item.save(update_fields=['wac', 'updated_at'])
        else:
            note = (note + ' — nothing on hand, so average cost was not changed').strip(' —')
        return StockMove.objects.create(
            branch=branch, item=item, move_type=StockMove.Type.COST_ADJ, quantity=D0, unit_cost=D0, value=value_delta,
            wac_after=item.wac, note=note[:255], ref_type=ref_type, ref_id=ref_id, ref_label=ref_label[:60],
            performed_by=user if getattr(user, 'pk', None) else None)


def pending_grns(branch, vendor=None):
    """Deliveries still waiting for the vendor's bill."""
    qs = (GoodsReceipt.objects.filter(branch=branch, status=GoodsReceipt.Status.RECEIVED, bill__isnull=True)
          .select_related('vendor', 'location').order_by('created_at'))
    return qs.filter(vendor=vendor) if vendor is not None else qs


def _split_tax(branch, vendor, tax):
    branch_state = (branch.state or '').strip().lower()
    vendor_state = (vendor.state or '').strip().lower()
    if branch_state and vendor_state and branch_state != vendor_state:
        return D0, D0, tax
    half = (tax / 2).quantize(CENT)
    return half, tax - half, D0


def post_bill(branch, *, vendor, invoice_number, invoice_date, lines, grns=(), location=None, other_charges=0,
              discount=0, round_off=0, is_itc_eligible=True, notes='', attachment=None, user=None):
    """Post a vendor bill.

    Against GRNs: ``grns`` are the deliveries it covers and ``lines`` is
    ``[{'grn_line': GRNLine, 'rate': …, 'tax_percent': …}]`` — quantities are what was accepted on
    the GRN; only the price can change. Direct: no ``grns``; ``lines`` is
    ``[{'item', 'quantity', 'unit_name', 'rate', 'tax_percent'}]`` and the goods are received into
    ``location`` on a GRN made for this bill.

    The bill's real cost per line (taxable value, its share of delivery less discount, and GST when
    it cannot be claimed) is compared with what the GRN put into stock; the difference re-values
    the item (WAC) without changing quantities.
    """
    invoice_number = (invoice_number or '').strip()[:60]
    if not invoice_number:
        raise StockError("Enter the bill number printed on the vendor's bill.")
    if invoice_date is None:
        raise StockError('Enter the bill date.')
    if invoice_date > timezone.localdate():
        raise StockError('The bill date cannot be in the future.')
    other_charges, discount, round_off = dec(other_charges, D0), dec(discount, D0), dec(round_off, D0)
    if other_charges < 0 or discount < 0:
        raise StockError('Charges and discount cannot be negative.')
    if abs(round_off) > 10:
        raise StockError('Round-off should be a few rupees at most.')
    if not lines:
        raise StockError('The bill has no items.')
    if PurchaseBill.objects.filter(branch=branch, vendor=vendor, invoice_number__iexact=invoice_number) \
            .exclude(status=PurchaseBill.Status.CANCELLED).exists():
        raise StockError(f'Bill {invoice_number} from {vendor.name} has already been entered.')

    with transaction.atomic():
        is_direct = not grns
        if is_direct:
            if location is None:
                raise StockError('Choose where the goods were put away.')
            grn = receive_grn(branch, vendor=vendor, location=location, user=user, invoice_number=invoice_number,
                              invoice_date=invoice_date, is_itc_eligible=is_itc_eligible, notes='Received on a direct purchase bill',
                              lines_data=[{'item': l['item'], 'received_qty': l['quantity'], 'unit_name': l.get('unit_name') or '',
                                           'unit_cost': l['rate'], 'tax_percent': l.get('tax_percent')} for l in lines])
            grn_lines = list(grn.lines.select_related('item'))
            lines = [{'grn_line': gl, 'rate': l['rate'], 'tax_percent': l.get('tax_percent')} for gl, l in zip(grn_lines, lines)]
            grns = [grn]
        else:
            grn_ids = [g.pk for g in grns]
            grns = list(GoodsReceipt.objects.select_for_update().filter(pk__in=grn_ids, branch=branch))
            if len(grns) != len(set(grn_ids)):
                raise StockError('One of the deliveries was not found.')
            for g in grns:
                if g.vendor_id != vendor.pk:
                    raise StockError(f'{g.grn_number} was delivered by {g.vendor.name}, not {vendor.name}.')
                if g.bill_id or g.status != GoodsReceipt.Status.RECEIVED:
                    raise StockError(f'{g.grn_number} has already been billed or was cancelled.')

        covered = {gl.pk: gl for g in grns for gl in g.lines.select_related('item')}
        seen = set()
        bill = PurchaseBill.objects.create(
            branch=branch, bill_number=next_doc_number(PurchaseBill, 'bill_number', 'PB', branch), vendor=vendor,
            invoice_number=invoice_number, invoice_date=invoice_date, is_direct=is_direct, is_itc_eligible=is_itc_eligible,
            due_date=invoice_date + datetime.timedelta(days=vendor.payment_terms_days or 0),
            other_charges=other_charges, discount=discount, round_off=round_off, notes=notes,
            attachment=attachment or '', created_by=user if getattr(user, 'pk', None) else None)

        rows = []
        for l in lines:
            gl = l['grn_line']
            if gl.pk not in covered or gl.pk in seen:
                raise StockError('A bill line does not belong to the selected deliveries.')
            seen.add(gl.pk)
            qty = gl.received_qty - gl.rejected_qty
            if qty <= 0:
                continue
            rate, tax_pct = dec(l.get('rate')), dec(l.get('tax_percent'), gl.tax_percent)
            if rate is None or rate < 0:
                raise StockError(f'{gl.item.name}: enter the rate on the bill.')
            if tax_pct < 0 or tax_pct > 28:
                raise StockError(f'{gl.item.name}: GST must be between 0% and 28%.')
            taxable = (qty * rate).quantize(CENT)
            rows.append({'gl': gl, 'qty': qty, 'rate': rate, 'tax_pct': tax_pct, 'taxable': taxable,
                         'tax': (taxable * tax_pct / 100).quantize(CENT)})
        if not rows:
            raise StockError('The selected deliveries have nothing accepted to bill.')
        missing = set(covered) - seen
        if missing and any(covered[pk].received_qty - covered[pk].rejected_qty > 0 for pk in missing):
            raise StockError('Every item on the selected deliveries must be on the bill.')

        subtotal = sum((r['taxable'] for r in rows), D0)
        net_charges = other_charges - discount
        if subtotal + net_charges < 0:
            raise StockError('The discount is more than the bill.')
        # Delivery less discount is spread over the lines by value; the last line takes the rounding
        left = net_charges
        for i, r in enumerate(rows):
            share = left if i == len(rows) - 1 else (net_charges * r['taxable'] / subtotal).quantize(CENT) if subtotal else D0
            left -= share
            r['share'] = share
            r['landed'] = r['taxable'] + share + (D0 if is_itc_eligible else r['tax'])
            r['grn_value'] = r['gl'].subtotal

        tax_total = sum((r['tax'] for r in rows), D0)
        cgst, sgst, igst = _split_tax(branch, vendor, tax_total)
        diff_total = D0
        for r in rows:
            gl = r['gl']
            PurchaseBillLine.objects.create(
                bill=bill, grn_line=gl, item=gl.item, unit_name=gl.unit_name, quantity=r['qty'], rate=r['rate'],
                tax_percent=r['tax_pct'], taxable_amount=r['taxable'], tax_amount=r['tax'], charges_share=r['share'],
                landed_cost=r['landed'], grn_value=r['grn_value'])
            diff = r['landed'] - r['grn_value']
            if diff:
                revalue(branch, gl.item, diff, user=user, ref_type='bill', ref_id=bill.pk, ref_label=bill.bill_number,
                        note=f'Bill {invoice_number}: cost {"up" if diff > 0 else "down"} vs GRN {gl.grn.grn_number}')
                diff_total += diff

        bill.subtotal, bill.tax_amount = subtotal, tax_total
        bill.cgst_amount, bill.sgst_amount, bill.igst_amount = cgst, sgst, igst
        bill.total_amount = (subtotal + tax_total + net_charges + round_off).quantize(CENT)
        bill.cost_difference = diff_total
        bill.save(update_fields=['subtotal', 'tax_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount',
                                 'cost_difference'])
        GoodsReceipt.objects.filter(pk__in=[g.pk for g in grns]).update(bill=bill)
        # Money already paid against these deliveries now counts towards the bill
        VendorPayment.objects.filter(grn__in=grns, bill__isnull=True).update(bill=bill)
        if bill.paid_amount > bill.total_amount:
            raise StockError(f'₹{bill.paid_amount} was already paid on these deliveries — more than this bill '
                             f'(₹{bill.total_amount}). Check the rates.')
    return bill


def cancel_bill(bill, user=None, reason=''):
    """Undo a bill: cost corrections are reversed and the deliveries go back to 'bill pending'. A
    direct bill also takes its goods back out of stock (only if they are still there)."""
    if bill.status != PurchaseBill.Status.POSTED:
        raise StockError('This bill is already cancelled.')
    if not (reason or '').strip():
        raise StockError('Say why the bill is being cancelled.')
    if bill.payments.exists():
        raise StockError('Payments have been recorded against this bill; it cannot be cancelled.')
    with transaction.atomic():
        for mv in StockMove.objects.filter(branch=bill.branch, ref_type='bill', ref_id=bill.pk, move_type=StockMove.Type.COST_ADJ):
            revalue(bill.branch, mv.item, -mv.value, user=user, ref_type='bill', ref_id=bill.pk, ref_label=bill.bill_number,
                    note=f'Bill {bill.invoice_number} cancelled')
        grns = list(bill.grns.all())
        GoodsReceipt.objects.filter(bill=bill).update(bill=None)
        if bill.is_direct:
            for g in grns:
                for mv in StockMove.objects.filter(ref_type='grn', ref_id=g.pk, move_type=StockMove.Type.RECEIPT):
                    reverse_move(mv, user, note=f'Direct bill {bill.invoice_number} cancelled')
                g.status = GoodsReceipt.Status.CANCELLED
                g.save(update_fields=['status'])
        bill.status = PurchaseBill.Status.CANCELLED
        bill.cancelled_by = user if getattr(user, 'pk', None) else None
        bill.cancelled_at = timezone.now()
        bill.cancel_reason = reason.strip()[:200]
        bill.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason'])
    return bill


# ============================================================ Phase 3: Internal Flow

def create_requisition(branch, department, from_location, to_location, lines_data, user=None, notes=''):
    if not lines_data:
        raise StockError('Add at least one item to the requisition.')
    req_num = next_doc_number(Requisition, 'req_number', 'REQ', branch)
    with transaction.atomic():
        req = Requisition.objects.create(
            branch=branch, req_number=req_num, department=department, from_location=from_location,
            to_location=to_location, status=Requisition.Status.DRAFT, requested_by=user, notes=notes
        )
        for ld in lines_data:
            item = ld['item']
            qty = dec(ld.get('quantity'), D0)
            u_name = (ld.get('unit_name') or item.unit).strip()
            if qty > 0:
                RequisitionLine.objects.create(
                    requisition=req, item=item, unit_name=u_name, requested_qty=qty, approved_qty=qty
                )
    return req


def submit_requisition(req, user=None):
    if req.status != Requisition.Status.DRAFT:
        raise StockError(f'Requisition is already {req.get_status_display()}.')
    req.status = Requisition.Status.SUBMITTED
    req.save(update_fields=['status'])
    return req


def approve_requisition(req, user=None, approved_lines=None):
    if req.status not in (Requisition.Status.DRAFT, Requisition.Status.SUBMITTED):
        raise StockError(f'Cannot approve a requisition in {req.get_status_display()} state.')
    with transaction.atomic():
        if approved_lines:
            for line in req.lines.all():
                if line.pk in approved_lines:
                    qty = dec(approved_lines[line.pk])
                    if qty is None or qty < 0:
                        raise StockError(f'{line.item.name}: enter the approved quantity as a number (0 to drop the line).')
                    line.approved_qty = qty
                    line.save(update_fields=['approved_qty'])
        else:
            for line in req.lines.all():
                line.approved_qty = line.requested_qty
                line.save(update_fields=['approved_qty'])
        req.status = Requisition.Status.APPROVED
        req.approved_by = user
        req.approved_at = timezone.now()
        req.save(update_fields=['status', 'approved_by', 'approved_at'])
    return req


def issue_requisition(req, user=None, issue_quantities=None):
    """Issue approved quantities from req.from_location to req.to_location."""
    if req.status not in (Requisition.Status.APPROVED, Requisition.Status.PARTIALLY_ISSUED):
        raise StockError('Only approved requisitions can be issued.')
    with transaction.atomic():
        for line in req.lines.all():
            remaining = max(line.approved_qty - line.issued_qty, D0)
            if issue_quantities and line.pk in issue_quantities:
                qty_to_issue = dec(issue_quantities[line.pk], D0)
                if qty_to_issue > remaining:
                    raise StockError(f'{line.item.name}: only {fmt_qty(remaining)} {line.unit_name or line.item.unit} '
                                     f'is still approved to issue.')
            else:
                qty_to_issue = remaining
            if qty_to_issue > 0:
                move(req.branch, line.item, qty_to_issue, move_type=StockMove.Type.ISSUE,
                     src=req.from_location, dst=req.to_location, unit=line.unit_name, user=user,
                     ref_type='requisition', ref_id=req.pk, ref_label=req.req_number,
                     note=f"Requisition #{req.req_number} issue to {req.to_location.path}")
                line.issued_qty += qty_to_issue
                line.save(update_fields=['issued_qty'])

        all_issued = all(l.issued_qty >= l.approved_qty for l in req.lines.all())
        req.status = Requisition.Status.ISSUED if all_issued else Requisition.Status.PARTIALLY_ISSUED
        req.issued_by = user
        req.issued_at = timezone.now()
        req.save(update_fields=['status', 'issued_by', 'issued_at'])
    return req


def reject_requisition(req, user=None, reason=''):
    if req.status not in (Requisition.Status.DRAFT, Requisition.Status.SUBMITTED, Requisition.Status.APPROVED):
        raise StockError(f'A requisition that is {req.get_status_display().lower()} can no longer be rejected.')
    req.status = Requisition.Status.REJECTED
    req.notes = f"{req.notes}\nRejected by {user}: {reason}".strip()
    req.save(update_fields=['status', 'notes'])
    return req


def adjust(branch, item, location, qty, *, reason=StockAdjustment.Reason.WASTAGE, unit='',
           room=None, photo=None, notes='', user=None, post_to_expense=True):
    """Post an adjustment (wastage, breakage, loss, theft, scrap) and record expense if enabled."""
    qty = dec(qty)
    if qty is None or qty <= 0:
        raise StockError('Quantity must be greater than zero.')

    move_type = {
        StockAdjustment.Reason.BREAKAGE: StockMove.Type.BREAKAGE,
        StockAdjustment.Reason.SCRAP: StockMove.Type.RETIRE,
        StockAdjustment.Reason.CORRECTION: StockMove.Type.ADJUST,
    }.get(reason, StockMove.Type.WASTAGE)

    dst_loc = None
    if reason == StockAdjustment.Reason.SCRAP:
        dst_loc = ensure_special_location(branch, Location.Kind.SCRAP, 'Scrap / Retired')

    with transaction.atomic():
        mv = move(branch, item, qty, move_type=move_type, src=location, dst=dst_loc, unit=unit,
                  user=user, reason=reason, note=notes, ref_type='adjustment')

        exp = None
        if post_to_expense and reason in (StockAdjustment.Reason.WASTAGE, StockAdjustment.Reason.BREAKAGE,
                                          StockAdjustment.Reason.THEFT):
            category_name = 'Housekeeping & laundry' if item.kind == StoreItem.Kind.CONSUMABLE else 'Repairs & maintenance'
            exp_note = f"Store stock adjustment: {reason} - {item.name} ({fmt_qty(qty)} {unit or item.unit})"
            exp = _post_expense(branch, category_name, mv.value, exp_note)

        adj_num = next_doc_number(StockAdjustment, 'adjustment_number', 'ADJ', branch)
        adj = StockAdjustment.objects.create(
            branch=branch, adjustment_number=adj_num, location=location, item=item, quantity=qty,
            unit_name=unit or item.unit, base_quantity=mv.quantity, unit_cost=mv.unit_cost, value=mv.value,
            reason=reason, room=room, notes=notes, photo=photo, expense=exp, move=mv, created_by=user
        )
    return adj


def create_stock_count(branch, location, user=None, notes=''):
    """Prepare a physical stock count sheet for a location."""
    cnt_num = next_doc_number(StockCount, 'count_number', 'CNT', branch)
    with transaction.atomic():
        cnt = StockCount.objects.create(
            branch=branch, count_number=cnt_num, location=location, status=StockCount.Status.DRAFT,
            counted_by=user, notes=notes
        )
        levels = StockLevel.objects.filter(location=location, item__is_active=True).select_related('item')
        for lvl in levels:
            StockCountLine.objects.create(
                count=cnt, item=lvl.item, expected_qty=lvl.quantity, counted_qty=lvl.quantity,
                unit_cost=lvl.item.wac
            )
    return cnt


def post_stock_count(count, counts_dict, user=None):
    """Post variances found during a stock count."""
    if count.status != StockCount.Status.DRAFT:
        raise StockError('This stock count has already been finalized.')
    with transaction.atomic():
        tot_variance_val = D0
        for line in count.lines.select_related('item'):
            c_qty = dec(counts_dict.get(str(line.pk)), line.expected_qty)
            if c_qty < 0:
                raise StockError(f'{line.item.name}: a count cannot be negative.')
            # Compare with the shelf as it is now, not when the sheet was printed: stock issued while
            # the count was open must not be posted a second time as a shortage.
            lvl = StockLevel.objects.filter(item=line.item, location=count.location).first()
            diff = c_qty - (lvl.quantity if lvl else D0)
            line.counted_qty = c_qty
            line.variance_qty = diff
            line.unit_cost = line.item.wac
            line.variance_value = (diff * line.unit_cost).quantize(CENT)
            line.save(update_fields=['counted_qty', 'variance_qty', 'unit_cost', 'variance_value'])
            tot_variance_val += line.variance_value

            if diff > 0:
                move(count.branch, line.item, diff, move_type=StockMove.Type.COUNT, dst=count.location,
                     unit_cost=line.unit_cost, user=user, ref_type='count', ref_id=count.pk,
                     ref_label=count.count_number, note='Stock count physical surplus')
            elif diff < 0:
                move(count.branch, line.item, abs(diff), move_type=StockMove.Type.COUNT, src=count.location,
                     user=user, ref_type='count', ref_id=count.pk, ref_label=count.count_number,
                     note='Stock count physical shortage')

        count.status = StockCount.Status.COMPLETED
        count.counted_by = user
        count.counted_at = timezone.now()
        count.total_variance_value = tot_variance_val
        count.save(update_fields=['status', 'counted_by', 'counted_at', 'total_variance_value'])
    return count


# ============================================================ Phase 4: Hotel-specific (Minibar & Linen)

def minibar_consume(booking, item, qty=1, user=None, charge_id=None):
    """Called when a MINIBAR folio charge is posted. Deducts from the room's minibar location."""
    branch = booking.branch
    room = booking.room
    if not room:
        return None
    minibar_loc, _ = Location.objects.get_or_create(
        branch=branch, kind=Location.Kind.MINIBAR, room=room,
        defaults={'name': f"Minibar - Room {room.room_number}"}
    )
    return move(branch, item, qty, move_type=StockMove.Type.MINIBAR, src=minibar_loc,
                allow_negative=True, user=user, ref_type='folio_charge', ref_id=charge_id,
                ref_label=f"Room {room.room_number}", note=f"Folio minibar charge for {booking.guest_name}")


def minibar_reverse_consume(charge, user=None):
    """Reverse minibar stock move when folio charge is voided."""
    mv = StockMove.objects.filter(branch=charge.booking.branch, ref_type='folio_charge', ref_id=charge.pk,
                                  move_type=StockMove.Type.MINIBAR).first()
    if mv:
        try:
            return reverse_move(mv, user, note=f"Voided minibar charge #{charge.pk}")
        except StockError:
            pass
    return None


def minibar_refill(branch, room, user=None):
    """Refills the room minibar up to category par from the floor pantry or main store."""
    if not room or not room.category:
        raise StockError('Room or category not specified.')
    profiles = MinibarProfile.objects.filter(branch=branch, room_category=room.category, is_active=True).select_related('item')
    if not profiles:
        raise StockError(f'No minibar par profile set for {room.category.name}.')
    minibar_loc, _ = Location.objects.get_or_create(
        branch=branch, kind=Location.Kind.MINIBAR, room=room,
        defaults={'name': f"Minibar - Room {room.room_number}"}
    )
    sources = list(Location.objects.filter(branch=branch, kind__in=[Location.Kind.PANTRY, Location.Kind.STORE], is_active=True))
    if not sources:
        raise StockError('No pantry or store to take refill stock from.')
    refilled, short = 0, []
    with transaction.atomic():
        for prof in profiles:
            lvl, _ = _level(prof.item, minibar_loc)
            deficit = prof.par_quantity - lvl.quantity
            if deficit <= 0:
                continue
            # Take from a pantry that holds enough, else a store; pantries first (they are on the floor)
            have = {l.location_id: l.quantity for l in StockLevel.objects.filter(item=prof.item, location__in=sources)}
            ranked = sorted(sources, key=lambda s: (s.kind != Location.Kind.PANTRY, -have.get(s.pk, D0)))
            src = next((s for s in ranked if have.get(s.pk, D0) >= deficit), None)
            if src is None:
                short.append(prof.item.name)
                continue
            move(branch, prof.item, deficit, move_type=StockMove.Type.TRANSFER, src=src, dst=minibar_loc, user=user,
                 ref_label=f"Room {room.room_number}",
                 note=f"Minibar refill Room {room.room_number} up to par ({fmt_qty(prof.par_quantity)})")
            refilled += 1
    if short and not refilled:
        raise StockError(f'Not enough stock in any pantry or store for: {", ".join(short)}.')
    return refilled


def send_linen_to_laundry(branch, vendor, lines_data, user=None, notes=''):
    """Create OUTBOUND manifest and move soiled linen to laundry."""
    if not lines_data:
        raise StockError('Select items to send to laundry.')
    soiled = ensure_special_location(branch, Location.Kind.LINEN_SOILED, 'Linen — soiled')
    laundry = ensure_special_location(branch, Location.Kind.LINEN_LAUNDRY, 'Linen — at laundry')
    mnf_num = next_doc_number(LaundryManifest, 'manifest_number', 'LAU-OUT', branch)
    with transaction.atomic():
        mnf = LaundryManifest.objects.create(
            branch=branch, manifest_number=mnf_num, manifest_type=LaundryManifest.ManifestType.OUTBOUND,
            vendor=vendor, status=LaundryManifest.Status.SENT, date=timezone.localdate(), notes=notes, created_by=user
        )
        for ld in lines_data:
            item = ld['item']
            qty = dec(ld.get('quantity'), D0)
            if qty > 0:
                LaundryManifestLine.objects.create(
                    manifest=mnf, item=item, sent_qty=qty, unit_cost=item.wac
                )
                move(branch, item, qty, move_type=StockMove.Type.LINEN_OUT, src=soiled, dst=laundry,
                     user=user, ref_type='manifest', ref_id=mnf.pk, ref_label=mnf.manifest_number,
                     note=f"Linen sent to laundry: #{mnf.manifest_number}")
    return mnf


def receive_linen_from_laundry(branch, outbound_manifest, lines_data, user=None, notes=''):
    """Create INBOUND manifest and return clean linen from laundry, logging damage/loss."""
    if not lines_data:
        raise StockError('Select items returned from laundry.')
    clean = ensure_special_location(branch, Location.Kind.LINEN_CLEAN, 'Linen — clean store')
    laundry = ensure_special_location(branch, Location.Kind.LINEN_LAUNDRY, 'Linen — at laundry')
    scrap = ensure_special_location(branch, Location.Kind.SCRAP, 'Scrap / retired')
    mnf_num = next_doc_number(LaundryManifest, 'manifest_number', 'LAU-IN', branch)
    with transaction.atomic():
        inbound = LaundryManifest.objects.create(
            branch=branch, manifest_number=mnf_num, manifest_type=LaundryManifest.ManifestType.INBOUND,
            vendor=outbound_manifest.vendor if outbound_manifest else None,
            related_outbound=outbound_manifest, status=LaundryManifest.Status.RECEIVED,
            date=timezone.localdate(), notes=notes, created_by=user
        )
        for ld in lines_data:
            item = ld['item']
            ret_qty = dec(ld.get('returned_qty'), D0)
            dmg_qty = dec(ld.get('damaged_qty'), D0)
            mis_qty = dec(ld.get('missing_qty'), D0)

            LaundryManifestLine.objects.create(
                manifest=inbound, item=item, returned_qty=ret_qty, damaged_qty=dmg_qty,
                missing_qty=mis_qty, unit_cost=item.wac
            )
            if ret_qty > 0:
                move(branch, item, ret_qty, move_type=StockMove.Type.LINEN_IN, src=laundry, dst=clean,
                     user=user, ref_type='manifest', ref_id=inbound.pk, ref_label=inbound.manifest_number,
                     note=f"Clean linen returned from laundry: #{inbound.manifest_number}")
            if dmg_qty > 0:
                move(branch, item, dmg_qty, move_type=StockMove.Type.BREAKAGE, src=laundry, dst=scrap,
                     user=user, ref_type='manifest', ref_id=inbound.pk, ref_label=inbound.manifest_number,
                     note=f"Linen damaged at laundry: #{inbound.manifest_number}")
            if mis_qty > 0:
                move(branch, item, mis_qty, move_type=StockMove.Type.WASTAGE, src=laundry,
                     user=user, ref_type='manifest', ref_id=inbound.pk, ref_label=inbound.manifest_number,
                     note=f"Linen lost / missing at laundry: #{inbound.manifest_number}")

        if outbound_manifest:
            outbound_manifest.status = LaundryManifest.Status.RECONCILED
            outbound_manifest.save(update_fields=['status'])
    return inbound


def retire_linen(branch, item, qty, location, reason='Torn / Stained', user=None):
    scrap = ensure_special_location(branch, Location.Kind.SCRAP, 'Scrap / retired')
    return move(branch, item, qty, move_type=StockMove.Type.RETIRE, src=location, dst=scrap,
                user=user, reason=reason, note=f"Linen retired to scrap: {reason}")


# ============================================================ Phase 5: Reports & Forecasting

def valuation_report(branch, as_of_date=None):
    """Reconstruct inventory valuation as of a given date from StockMoves."""
    items = StoreItem.objects.filter(branch=branch).order_by('name')
    if as_of_date is None:
        rows = []
        tot_val = D0
        for it in items:
            q = it.on_hand()
            v = (q * it.wac).quantize(CENT)
            tot_val += v
            rows.append({'item': it, 'quantity': q, 'wac': it.wac, 'value': v})
        return rows, tot_val

    moves = StockMove.objects.filter(branch=branch, created_at__date__lte=as_of_date).order_by('created_at', 'id')
    balances = {}
    last_wac = {}
    for m in moves:
        if m.to_location_id:
            balances[m.item_id] = balances.get(m.item_id, D0) + m.quantity
        if m.from_location_id:
            balances[m.item_id] = balances.get(m.item_id, D0) - m.quantity
        last_wac[m.item_id] = m.wac_after

    rows = []
    tot_val = D0
    for it in items:
        q = max(balances.get(it.pk, D0), D0)
        w = last_wac.get(it.pk, it.wac)
        v = (q * w).quantize(CENT)
        tot_val += v
        rows.append({'item': it, 'quantity': q, 'wac': w, 'value': v})
    return rows, tot_val


def department_usage_report(branch, start_date=None, end_date=None, department=None):
    moves = StockMove.objects.filter(branch=branch, move_type__in=[StockMove.Type.ISSUE, StockMove.Type.CONSUME])
    if start_date:
        moves = moves.filter(created_at__date__gte=start_date)
    if end_date:
        moves = moves.filter(created_at__date__lte=end_date)
    if department:
        moves = moves.filter(to_location__department=department)
    results = {}
    for m in moves.select_related('item', 'to_location'):
        dep = m.to_location.get_department_display() if m.to_location and m.to_location.department else (m.to_location.name if m.to_location else 'General')
        key = (dep, m.item.name, m.item.unit)
        data = results.setdefault(key, {'department': dep, 'item': m.item, 'quantity': D0, 'value': D0})
        data['quantity'] += m.quantity
        data['value'] += m.value
    return sorted(results.values(), key=lambda r: (r['department'], r['item'].name))


def gst_input_register(branch, start_date=None, end_date=None):
    """Input GST by vendor bill date. Posted bills are the record; a delivery whose bill has not been
    entered yet is listed at its GRN values (marked ``is_provisional``) so nothing is missed."""
    bills = PurchaseBill.objects.filter(branch=branch, status=PurchaseBill.Status.POSTED).select_related('vendor')
    grns = GoodsReceipt.objects.filter(branch=branch, status=GoodsReceipt.Status.RECEIVED, bill__isnull=True).select_related('vendor')
    if start_date:
        bills = bills.filter(invoice_date__gte=start_date)
        grns = grns.filter(created_at__date__gte=start_date)
    if end_date:
        bills = bills.filter(invoice_date__lte=end_date)
        grns = grns.filter(created_at__date__lte=end_date)
    docs = list(bills)
    for g in grns:
        g.is_provisional = True
        docs.append(g)
    docs.sort(key=lambda d: (d.invoice_date or d.created_at.date(), d.pk))
    rows = []
    tot_taxable = D0
    tot_cgst = D0
    tot_sgst = D0
    tot_igst = D0
    tot_tax = D0
    for g in docs:
        tot_taxable += g.subtotal
        tot_cgst += g.cgst_amount
        tot_sgst += g.sgst_amount
        tot_igst += g.igst_amount
        tot_tax += g.tax_amount
        rows.append(g)
    summary = {'taxable': tot_taxable, 'cgst': tot_cgst, 'sgst': tot_sgst, 'igst': tot_igst, 'total_tax': tot_tax}
    return rows, summary


def vendor_performance_report(branch):
    vendors = Vendor.objects.filter(branch=branch, is_active=True)
    stats = []
    for v in vendors:
        pos = PurchaseOrder.objects.filter(vendor=v)
        grns = GoodsReceipt.objects.filter(vendor=v)
        total_pos = pos.count()
        total_grns = grns.count()
        lines = GRNLine.objects.filter(grn__in=grns)
        tot_rec = sum((l.received_qty for l in lines), D0)
        tot_rej = sum((l.rejected_qty for l in lines), D0)
        rejection_rate = ((tot_rej / (tot_rec + tot_rej)) * Decimal('100')).quantize(CENT) if (tot_rec + tot_rej) > 0 else D0
        stats.append({
            'vendor': v,
            'pos_count': total_pos,
            'grns_count': total_grns,
            'total_received_qty': tot_rec,
            'total_rejected_qty': tot_rej,
            'rejection_rate': rejection_rate
        })
    return stats


def consumption_forecast(branch, days=7):
    """Rule-based forecast: consumption per occupied room-night (last 8 weeks) x booked rooms for next N days."""
    from hotel.models import RoomBooking
    today = timezone.localdate()
    past_8w = today - datetime.timedelta(days=56)
    past_stays = RoomBooking.objects.filter(
        branch=branch, check_in_date__gte=past_8w, check_in_date__lte=today,
        booking_status__in=[RoomBooking.BookingStatus.CHECKED_IN, RoomBooking.BookingStatus.CHECKED_OUT]
    )
    past_room_nights = sum(max((b.check_out_date - b.check_in_date).days, 1) for b in past_stays) or 1

    next_n_end = today + datetime.timedelta(days=days)
    future_bookings = RoomBooking.objects.filter(
        branch=branch, check_in_date__lte=next_n_end, check_out_date__gte=today,
        booking_status__in=[RoomBooking.BookingStatus.RESERVED, RoomBooking.BookingStatus.CHECKED_IN]
    )
    future_room_nights = sum(
        max((min(b.check_out_date, next_n_end) - max(b.check_in_date, today)).days, 1)
        for b in future_bookings
    ) or days

    consumables = StoreItem.objects.filter(branch=branch, is_active=True, kind=StoreItem.Kind.CONSUMABLE)
    past_usage_moves = StockMove.objects.filter(
        branch=branch, item__in=consumables, created_at__date__gte=past_8w,
        move_type__in=[StockMove.Type.ISSUE, StockMove.Type.CONSUME]
    ).values('item_id').annotate(tot=Sum('quantity'))
    usage_map = {r['item_id']: r['tot'] for r in past_usage_moves}

    forecast = []
    for it in consumables:
        past_qty = usage_map.get(it.pk, D0)
        rate_per_rn = past_qty / Decimal(past_room_nights)
        projected_qty = (rate_per_rn * Decimal(future_room_nights)).quantize(Q3)
        on_hand = it.on_hand()
        shortfall = max(projected_qty - on_hand, D0)
        forecast.append({
            'item': it,
            'past_qty': past_qty,
            'rate_per_room_night': rate_per_rn.quantize(Q4),
            'projected_usage': projected_qty,
            'on_hand': on_hand,
            'suggested_procurement': shortfall
        })
    return {
        'forecast_days': days,
        'past_room_nights': past_room_nights,
        'projected_room_nights': future_room_nights,
        'items': sorted(forecast, key=lambda x: -x['suggested_procurement'])
    }

