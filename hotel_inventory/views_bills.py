"""Hotel purchase bills: the vendor's tax invoice, entered against deliveries already received
(GRNs) or, for a small direct purchase, together with the goods. Rules live in
``hotel_inventory.services.post_bill`` / ``cancel_bill``."""
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from hotel.paging import paginate
from hotel.views_admin import hotel_admin_required
from hotel.views_pms import _can, _date, _report
from hotel_inventory import services as S
from hotel_inventory.models import GoodsReceipt, GRNLine, Location, PurchaseBill, StockMove, StoreItem, Vendor, VendorPayment
from hotel_inventory.views import AREA, _branch, _deny, _get, _locations, _page, _url

MAX_ATTACHMENT = 5 * 1024 * 1024


def _bill_lines_from_grns(grns):
    """Rows for the bill form: each accepted GRN line, rate and GST pre-filled from the GRN."""
    rows = []
    for g in grns:
        for gl in g.lines.select_related('item'):
            qty = gl.received_qty - gl.rejected_qty
            if qty > 0:
                rows.append({'gl': gl, 'grn': g, 'qty': qty, 'unit': gl.unit_name or gl.item.unit})
    return rows


@hotel_admin_required
def bills(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = PurchaseBill.objects.filter(branch=branch).select_related('vendor', 'created_by').order_by('-invoice_date', '-id')
    vendor = _get(Vendor, branch, request.GET.get('vendor'))
    show = request.GET.get('show', '')
    q = (request.GET.get('q') or '').strip()
    if vendor:
        qs = qs.filter(vendor=vendor)
    if q:
        qs = qs.filter(Q(invoice_number__icontains=q) | Q(bill_number__icontains=q) | Q(vendor__name__icontains=q))
    rows = list(qs)
    if show == 'unpaid':
        rows = [b for b in rows if b.status == PurchaseBill.Status.POSTED and not b.is_paid]
    elif show == 'overdue':
        rows = [b for b in rows if b.is_overdue]
    page_obj = paginate(request, rows)

    pending = list(S.pending_grns(branch))
    posted = list(PurchaseBill.objects.filter(branch=branch, status=PurchaseBill.Status.POSTED))
    month_start = timezone.localdate().replace(day=1)
    kpis = {'pending_count': len(pending), 'pending_value': sum((g.total_amount for g in pending), S.D0),
            'due': sum((b.due_amount for b in posted), S.D0), 'overdue': sum(1 for b in posted if b.is_overdue),
            'month': sum((b.total_amount for b in posted if b.invoice_date >= month_start), S.D0)}
    pending_by_vendor = {}
    for g in pending:
        pending_by_vendor.setdefault(g.vendor, []).append(g)
    return _page(request, ctx, '_bills.html', 'Purchase Bills', 'stores_bills', rows=page_obj.object_list, page_obj=page_obj,
                 kpis=kpis, pending_by_vendor=sorted(pending_by_vendor.items(), key=lambda kv: kv[0].name),
                 vendors=Vendor.objects.filter(branch=branch, is_active=True), vendor=vendor, show=show, q=q)


@hotel_admin_required
def bill_new(request, restaurant_slug, ctx):
    """The bill entry form. ?vendor=&grn=1&grn=2 bills those deliveries; ?direct=1 is a purchase
    whose goods arrive with the bill."""
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    vendor = _get(Vendor, branch, request.GET.get('vendor'))
    direct = request.GET.get('direct') == '1'
    pending = list(S.pending_grns(branch, vendor)) if vendor and not direct else []
    chosen_ids = {int(x) for x in request.GET.getlist('grn') if x.isdigit()}
    chosen = [g for g in pending if g.pk in chosen_ids]
    return _page(request, ctx, '_bill_form.html', 'Enter Purchase Bill', 'stores_bills',
                 vendors=Vendor.objects.filter(branch=branch, is_active=True), vendor=vendor, direct=direct, pending=pending,
                 chosen=chosen, chosen_ids=chosen_ids, lines=_bill_lines_from_grns(chosen),
                 items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units').order_by('name'),
                 locations=_locations(branch), today=timezone.localdate())


def _lines_from_post(branch, p, direct):
    if direct:
        lines = []
        for item_id, qty, unit, rate, tax in zip(p.getlist('item_id'), p.getlist('quantity'), p.getlist('unit_name'),
                                                 p.getlist('rate'), p.getlist('tax_percent')):
            item = _get(StoreItem, branch, item_id)
            qty = S.dec(qty)
            if item is None or qty is None:
                continue
            if qty <= 0:
                raise S.StockError(f'{item.name}: quantity must be more than zero.')
            S.unit_factor(item, unit)  # a pack size the item does not have is refused here, before anything posts
            lines.append({'item': item, 'quantity': qty, 'unit_name': unit.strip(), 'rate': S.dec(rate),
                          'tax_percent': S.dec(tax, item.effective_gst_rate or S.D0)})
        return lines, ()
    grns = list(GoodsReceipt.objects.filter(branch=branch, pk__in=[x for x in p.getlist('grn') if x.isdigit()]))
    if not grns:
        raise S.StockError('Choose the deliveries (GRNs) this bill is for.')
    gl_by_id = {gl.pk: gl for gl in GRNLine.objects.filter(grn__in=grns).select_related('item', 'grn')}
    lines = []
    for gl_id, rate, tax in zip(p.getlist('grn_line'), p.getlist('rate'), p.getlist('tax_percent')):
        gl = gl_by_id.get(int(gl_id)) if gl_id.isdigit() else None
        if gl is None:
            raise S.StockError('A bill line does not belong to the selected deliveries.')
        lines.append({'grn_line': gl, 'rate': S.dec(rate), 'tax_percent': S.dec(tax)})
    return lines, grns


@hotel_admin_required
@require_POST
def bill_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    p = request.POST
    direct = p.get('direct') == '1'
    vendor = _get(Vendor, branch, p.get('vendor'))
    back = _url(ctx, 'stores_bill_new')
    if vendor is None:
        _report(request, 'Choose the vendor.')
        return redirect(back + ('?direct=1' if direct else ''))
    back += f'?vendor={vendor.pk}' + ('&direct=1' if direct else ''.join(f'&grn={g}' for g in p.getlist('grn') if g.isdigit()))
    attachment = request.FILES.get('attachment')
    if attachment is not None:
        ok_type = (attachment.content_type or '').startswith('image/') or attachment.content_type == 'application/pdf'
        if attachment.size > MAX_ATTACHMENT or not ok_type:
            _report(request, 'The bill copy must be a photo or PDF of 5 MB or less.')
            return redirect(back)
    try:
        lines, grns = _lines_from_post(branch, p, direct)
        bill = S.post_bill(branch, vendor=vendor, invoice_number=p.get('invoice_number', ''),
                           invoice_date=_date(p.get('invoice_date')), lines=lines, grns=grns,
                           location=_get(Location, branch, p.get('location')) if direct else None,
                           other_charges=p.get('other_charges') or 0, discount=p.get('discount') or 0,
                           round_off=p.get('round_off') or 0, is_itc_eligible=p.get('is_itc_eligible') == 'on',
                           notes=p.get('notes', '').strip(), attachment=attachment, user=request.user)
    except S.StockError as e:
        _report(request, str(e))
        return redirect(back)
    diff = bill.cost_difference
    note = '' if not diff else f' Stock cost {"raised" if diff > 0 else "lowered"} by ₹{abs(diff)} to match the bill.'
    _report(request, None, f'Bill {bill.invoice_number} posted for ₹{bill.total_amount}.{note}')
    return redirect(_url(ctx, 'stores_bill_detail', pk=bill.pk))


@hotel_admin_required
def bill_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    bill = get_object_or_404(PurchaseBill.objects.select_related('vendor', 'created_by', 'cancelled_by'), branch=branch, pk=pk)
    return _page(request, ctx, '_bill_detail.html', f'Bill {bill.invoice_number}', 'stores_bills', bill=bill,
                 lines=bill.lines.select_related('item', 'grn_line__grn'), grns=bill.grns.select_related('location', 'po'),
                 payments=bill.payments.select_related('created_by'),
                 adjustments=StockMove.objects.filter(branch=branch, ref_type='bill', ref_id=bill.pk).select_related('item'),
                 methods=VendorPayment.Method.choices, today=timezone.localdate())


@hotel_admin_required
@require_POST
def bill_cancel(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    bill = get_object_or_404(PurchaseBill, branch=branch, pk=pk)
    try:
        S.cancel_bill(bill, request.user, request.POST.get('reason', ''))
        _report(request, None, f'Bill {bill.invoice_number} cancelled.' +
                ('' if bill.is_direct else ' Its deliveries are waiting for a bill again.'))
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_bill_detail', pk=bill.pk))
