"""Purchase cycle screens: purchase orders, goods receipts, bills from receipts, debit notes,
bill-wise payments and the purchase reports. Rules live in restaurant_admin/procurement.py."""
import csv
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import GoodsReceiptNote, ProcurementOrder, PurchaseOrder, PurchaseReturn
from restaurant_admin import dashboard_metrics as M
from restaurant_admin import procurement as P
from restaurant_admin.sequence_helpers import get_document_sequence_info
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant

SHELL = 'guests/shell_restaurant.html'


def _env(request, slug):
    restaurant = resolve_restaurant(request, slug)
    if isinstance(restaurant, HttpResponse):
        return None, restaurant
    return {'restaurant': restaurant, 'branch': get_active_branch(request, restaurant),
            'base_url': reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': restaurant.slug})}, None


def _render(request, env, partial, menu, title, **ctx):
    return render(request, SHELL, {**ctx, 'restaurant': env['restaurant'], 'current_branch': env['branch'], 'base_url': env['base_url'],
                                   'partial': partial, 'active_menu': menu, 'page_title': title})


def _go(env, name, **kw):
    return redirect(f'restaurant_admin:{name}', restaurant_slug=env['restaurant'].slug, **kw)


def _scoped(env, model):
    return P._scoped(model.objects, env['restaurant'], env['branch'])


def _error(request, e):
    messages.error(request, ' '.join(e.messages) if isinstance(e, ValidationError) else str(e))


def _csv(name, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(header)
    w.writerows(rows)
    return resp


def _editor_items(env):
    """Stock items for the line editor."""
    out = []
    for i in P.items_for(env['restaurant'], env['branch']).filter(is_active=True).select_related('category', 'category__parent').order_by('name'):
        gst = i.get_effective_value('default_gst_rate')
        out.append({'id': i.pk, 'name': i.name, 'unit': i.unit or '', 'sku': i.sku or '', 'stock': i.quantity or 0,
                    'rate': float(i.unit_cost), 'gst': float(gst if gst is not None else 0),
                    'hsn': str(i.get_effective_value('default_hsn_code') or '')})
    return out


# ============================================================ purchase orders

@login_required
@user_passes_test(is_admin)
def purchase_orders(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    qs = _scoped(env, ProcurementOrder).select_related('party').annotate(n_lines=Count('lines')).order_by('-order_date', '-id')
    status = request.GET.get('status', '')
    if status == 'pending':
        qs = qs.filter(status__in=ProcurementOrder.OPEN_STATUSES)
    elif status in ProcurementOrder.Status.values:
        qs = qs.filter(status=status)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(supplier_name__icontains=q))
    if request.GET.get('export') == 'csv':
        return _csv('purchase-orders.csv', ['PO no', 'Date', 'Supplier', 'GSTIN', 'Expected', 'Status', 'Taxable', 'GST', 'Total'],
                    [[o.number, o.order_date, o.supplier_name, o.supplier_gstin, o.expected_date or '', o.get_status_display(),
                      o.subtotal, o.tax_amount, o.grand_total] for o in qs])
    all_orders = _scoped(env, ProcurementOrder)
    open_orders = all_orders.filter(status__in=ProcurementOrder.OPEN_STATUSES)
    today = timezone.localdate()
    return _render(request, env, 'procurement/_orders.html', 'purchase_orders', 'Purchase orders',
                   page=Paginator(qs, 25).get_page(request.GET.get('page')), status=status, q=q,
                   statuses=ProcurementOrder.Status.choices,
                   kpi_open=open_orders.count(), kpi_open_value=open_orders.aggregate(s=Sum('grand_total'))['s'] or 0,
                   kpi_drafts=all_orders.filter(status='draft').count(),
                   kpi_overdue=open_orders.filter(expected_date__lt=today).count(),
                   kpi_reorder=len(P.reorder_suggestions(env['restaurant'], env['branch'])))


def _order_form(request, env, order=None, initial_lines=None, header=None):
    seq = get_document_sequence_info(env['restaurant'], 'purchase_order', branch=env['branch'])
    return _render(request, env, 'procurement/_order_form.html', 'purchase_orders', 'Edit purchase order' if order else 'New purchase order',
                   order=order, seq=seq, vendors=P.vendors(env['restaurant'], env['branch']), items_data=_editor_items(env),
                   lines_data=initial_lines or [], h=header or {}, today=timezone.localdate())


def _posted_order(request, env):
    raw = [{'item': i, 'quantity': q, 'rate': r, 'discount_pct': d, 'gst_rate': g} for i, q, r, d, g in zip(
        request.POST.getlist('item'), request.POST.getlist('quantity'), request.POST.getlist('rate'),
        request.POST.getlist('discount_pct'), request.POST.getlist('gst_rate'))]
    header = {'number': request.POST.get('number', ''), 'order_date': P.parse_date(request.POST.get('order_date')),
              'expected_date': P.parse_date(request.POST.get('expected_date')), 'credit_days': request.POST.get('credit_days'),
              'freight': request.POST.get('freight'), 'reverse_charge': request.POST.get('reverse_charge') == '1',
              'terms': request.POST.get('terms', ''), 'narration': request.POST.get('narration', ''),
              'approve': request.POST.get('op') == 'approve', 'party': P._int(request.POST.get('party')),
              'supplier_name': request.POST.get('supplier_name', ''), 'supplier_gstin': request.POST.get('supplier_gstin', '')}
    return raw, header


def _save_order(request, env, order=None):
    raw, header = _posted_order(request, env)
    try:
        lines = P.parse_lines(env['restaurant'], env['branch'], raw)
        party = P.resolve_party(env['restaurant'], env['branch'], P._int(header['party']), header['supplier_name'], header['supplier_gstin'])
        order = P.save_order(env['restaurant'], env['branch'], request.user, party, header, lines, order=order)
    except ValidationError as e:
        _error(request, e)
        keep = [r for r in raw if r['item']]
        return _order_form(request, env, order=order, initial_lines=keep, header=header)
    messages.success(request, f'{order.number} saved' + (' and approved — it is now open for receiving.' if order.status == 'open' else ' as a draft.'))
    return _go(env, 'purchase_order_detail', pk=order.pk)


@login_required
@user_passes_test(is_admin)
def purchase_order_new(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    if request.method == 'POST':
        return _save_order(request, env)
    lines, header = [], {}
    if request.GET.get('reorder'):
        picked = set(request.GET.getlist('item'))
        for r in P.reorder_suggestions(env['restaurant'], env['branch']):
            if picked and str(r['item'].pk) not in picked:
                continue
            lines.append({'item': r['item'].pk, 'quantity': str(r['suggest']), 'rate': str(r['rate'])})
            header.setdefault('party', r['party_id'])
    copy = P._int(request.GET.get('copy'))
    if copy:
        src = _scoped(env, ProcurementOrder).filter(pk=copy).first()
        if src:
            lines = [{'item': l.inventory_item_id, 'quantity': str(l.quantity), 'rate': str(l.rate), 'discount_pct': str(l.discount_pct),
                      'gst_rate': str(l.gst_rate)} for l in src.lines.all()]
            header = {'party': src.party_id, 'credit_days': src.credit_days, 'terms': src.terms, 'reverse_charge': src.reverse_charge,
                      'freight': src.freight_amount}
    return _order_form(request, env, initial_lines=lines, header=header)


def _order(env, pk):
    return get_object_or_404(_scoped(env, ProcurementOrder).select_related('party', 'created_by', 'approved_by'), pk=pk)


@login_required
@user_passes_test(is_admin)
def purchase_order_edit(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    order = _order(env, pk)
    if order.status not in ('draft', 'open') or order.receipts.exists():
        messages.error(request, 'Only orders with nothing received yet can be edited.')
        return _go(env, 'purchase_order_detail', pk=order.pk)
    if request.method == 'POST':
        return _save_order(request, env, order=order)
    lines = [{'item': l.inventory_item_id, 'quantity': str(l.quantity), 'rate': str(l.rate), 'discount_pct': str(l.discount_pct),
              'gst_rate': str(l.gst_rate)} for l in order.lines.all()]
    header = {'party': order.party_id, 'order_date': order.order_date, 'expected_date': order.expected_date, 'credit_days': order.credit_days,
              'freight': order.freight_amount, 'reverse_charge': order.reverse_charge, 'terms': order.terms, 'narration': order.narration}
    return _order_form(request, env, order=order, initial_lines=lines, header=header)


@login_required
@user_passes_test(is_admin)
def purchase_order_detail(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    order = _order(env, pk)
    lines = list(order.lines.select_related('inventory_item'))
    ctx = dict(order=order, lines=lines, receipts=order.receipts.select_related('bill').order_by('receipt_date', 'id'),
               bills=order.bills.all().order_by('purchase_date'),
               pending_value=sum(((l.pending_qty * l.net_rate) for l in lines), P.ZERO).quantize(P.CENT))
    if request.GET.get('print'):
        branch = order.branch or env['restaurant'].branches.first()
        return render(request, 'procurement/order_print.html', {**ctx, 'restaurant': env['restaurant'], 'branch': branch})
    return _render(request, env, 'procurement/_order_detail.html', 'purchase_orders', order.number, **ctx)


@login_required
@user_passes_test(is_admin)
@require_POST
def purchase_order_action(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    order = _order(env, pk)
    op = request.POST.get('op', '')
    try:
        P.order_action(order, op, request.user, request.POST.get('reason', ''))
        messages.success(request, {'approve': f'{order.number} approved.', 'close': f'{order.number} short-closed; the pending quantity is dropped.',
                                   'cancel': f'{order.number} cancelled.', 'reopen': f'{order.number} reopened.'}[op])
    except ValidationError as e:
        _error(request, e)
    return _go(env, 'purchase_order_detail', pk=order.pk)


# ============================================================ goods receipt notes

@login_required
@user_passes_test(is_admin)
def purchase_grn_new(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    order = _order(env, pk)
    if request.method == 'POST':
        rows = {}
        for line in order.lines.all():
            p = f'l{line.pk}_'
            rows[line.pk] = {'received': request.POST.get(p + 'received'), 'rejected': request.POST.get(p + 'rejected'),
                             'reason': request.POST.get(p + 'reason', ''), 'batch': request.POST.get(p + 'batch', ''),
                             'expiry': request.POST.get(p + 'expiry', '')}
        header = {'number': request.POST.get('number', ''), 'receipt_date': P.parse_date(request.POST.get('receipt_date')),
                  'challan_no': request.POST.get('challan_no', ''), 'vehicle_no': request.POST.get('vehicle_no', ''),
                  'note': request.POST.get('note', '')}
        try:
            grn = P.receive_goods(env['restaurant'], env['branch'], request.user, order, rows, header)
        except ValidationError as e:
            _error(request, e)
            return _go(env, 'purchase_grn_new', pk=order.pk)
        messages.success(request, f'{grn.number} recorded — stock updated. Book the supplier bill when it arrives.')
        return _go(env, 'purchase_grn_detail', pk=grn.pk)
    if order.status not in ProcurementOrder.OPEN_STATUSES:
        messages.error(request, 'Approve the order first.' if order.status == 'draft' else 'This order is not open for receiving.')
        return _go(env, 'purchase_order_detail', pk=order.pk)
    return _render(request, env, 'procurement/_grn_form.html', 'purchase_grns', f'Receive goods · {order.number}', order=order,
                   lines=[l for l in order.lines.select_related('inventory_item') if l.pending_qty > 0],
                   seq=get_document_sequence_info(env['restaurant'], 'grn', branch=env['branch']), today=timezone.localdate())


@login_required
@user_passes_test(is_admin)
def purchase_grns(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    qs = _scoped(env, GoodsReceiptNote).select_related('order', 'bill')
    status = request.GET.get('status', '')
    if status in GoodsReceiptNote.Status.values:
        qs = qs.filter(status=status)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(supplier_name__icontains=q) | Q(challan_no__icontains=q) | Q(order__number__icontains=q))
    pending = _scoped(env, GoodsReceiptNote).filter(status='pending')
    return _render(request, env, 'procurement/_grns.html', 'purchase_grns', 'Goods receipts',
                   page=Paginator(qs, 25).get_page(request.GET.get('page')), status=status, q=q, statuses=GoodsReceiptNote.Status.choices,
                   kpi_pending=pending.count(), kpi_pending_value=pending.aggregate(s=Sum('value'))['s'] or 0,
                   kpi_open_orders=_scoped(env, ProcurementOrder).filter(status__in=ProcurementOrder.OPEN_STATUSES).count())


def _grn(env, pk):
    return get_object_or_404(_scoped(env, GoodsReceiptNote).select_related('order', 'bill', 'party'), pk=pk)


@login_required
@user_passes_test(is_admin)
def purchase_grn_detail(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    grn = _grn(env, pk)
    lines = list(grn.lines.select_related('inventory_item', 'order_line'))
    order = grn.order
    default_due = grn.receipt_date + datetime.timedelta(days=order.credit_days) if order and order.credit_days else grn.receipt_date
    return _render(request, env, 'procurement/_grn_detail.html', 'purchase_grns', grn.number, grn=grn, lines=lines,
                   default_due=default_due, methods=PurchaseOrder.PaymentMethod.choices,
                   rejected=sum((l.rejected_qty for l in lines), P.ZERO))


@login_required
@user_passes_test(is_admin)
@require_POST
def purchase_grn_bill(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    grn = _grn(env, pk)
    data = {'invoice_number': request.POST.get('invoice_number', ''), 'bill_date': P.parse_date(request.POST.get('bill_date')),
            'due_date': P.parse_date(request.POST.get('due_date')), 'freight': request.POST.get('freight'),
            'round_off': request.POST.get('round_off'), 'reverse_charge': request.POST.get('reverse_charge') == '1',
            'itc_eligible': request.POST.get('itc_eligible') == '1', 'payment_method': request.POST.get('payment_method'),
            'paid_now': request.POST.get('paid_now') == '1', 'payment_ref': request.POST.get('payment_ref', ''),
            'narration': request.POST.get('narration', ''),
            'rates': {l.pk: request.POST.get(f'rate_{l.pk}') for l in grn.lines.all() if request.POST.get(f'rate_{l.pk}')}}
    try:
        bill = P.bill_from_grn(env['restaurant'], env['branch'], request.user, grn, data)
    except ValidationError as e:
        _error(request, e)
        return _go(env, 'purchase_grn_detail', pk=grn.pk)
    messages.success(request, f'Bill {bill.invoice_number} booked for ₹{bill.grand_total} (due {bill.due_date:%d %b %Y}).')
    return _go(env, 'purchase_invoice', pk=bill.pk)


# ============================================================ payments & debit notes

def _bill(env, pk):
    return get_object_or_404(_scoped(env, PurchaseOrder), pk=pk)


@login_required
@user_passes_test(is_admin)
@require_POST
def purchase_bill_pay(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    bill = _bill(env, pk)
    try:
        P.record_payment(bill, request.POST.get('amount'), P.parse_date(request.POST.get('paid_on')), request.POST.get('method', ''),
                         request.POST.get('reference', ''), request.user)
        bill.refresh_from_db()
        messages.success(request, f'Payment recorded against {bill.invoice_number}. Balance ₹{bill.balance_due}.')
    except ValidationError as e:
        _error(request, e)
    nxt = request.POST.get('next', '')
    return redirect(nxt) if nxt.startswith('/') and not nxt.startswith('//') else _go(env, 'purchase_invoice', pk=bill.pk)


@login_required
@user_passes_test(is_admin)
def purchase_return_new(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    bill = _bill(env, pk)
    left = P.returnable(bill)
    if request.method == 'POST':
        rows = {it.pk: request.POST.get(f'qty_{it.pk}') for it in bill.items.all()}
        header = {'number': request.POST.get('number', ''), 'return_date': P.parse_date(request.POST.get('return_date')),
                  'reason': request.POST.get('reason'), 'note': request.POST.get('note', '')}
        try:
            ret = P.create_return(env['restaurant'], env['branch'], request.user, bill, rows, header)
        except ValidationError as e:
            _error(request, e)
            return _go(env, 'purchase_return_new', pk=bill.pk)
        messages.success(request, f'Debit note {ret.number} for ₹{ret.grand_total} recorded; stock reduced.')
        return _go(env, 'purchase_return_detail', pk=ret.pk)
    items = [(it, left.get(it.pk, P.ZERO)) for it in bill.items.select_related('inventory_item')]
    return _render(request, env, 'procurement/_return_form.html', 'purchase_returns', f'Return goods · {bill.invoice_number}', bill=bill,
                   items=items, reasons=PurchaseReturn.Reason.choices, today=timezone.localdate(),
                   seq=get_document_sequence_info(env['restaurant'], 'debit_note', branch=env['branch']))


@login_required
@user_passes_test(is_admin)
def purchase_returns(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    qs = _scoped(env, PurchaseReturn).select_related('bill')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(supplier_name__icontains=q) | Q(bill__invoice_number__icontains=q))
    if request.GET.get('export') == 'csv':
        return _csv('debit-notes.csv', ['Debit note', 'Date', 'Supplier', 'GSTIN', 'Against bill', 'Reason', 'Taxable', 'CGST', 'SGST', 'IGST', 'Total'],
                    [[r.number, r.return_date, r.supplier_name, r.supplier_gstin, r.bill.invoice_number if r.bill else '',
                      r.get_reason_display(), r.subtotal, r.cgst_amount, r.sgst_amount, r.igst_amount, r.grand_total] for r in qs])
    month_start = timezone.localdate().replace(day=1)
    return _render(request, env, 'procurement/_returns.html', 'purchase_returns', 'Purchase returns', q=q,
                   page=Paginator(qs, 25).get_page(request.GET.get('page')),
                   kpi_month=qs.filter(return_date__gte=month_start).aggregate(s=Sum('grand_total'), n=Count('id')),
                   recent_bills=_scoped(env, PurchaseOrder).order_by('-purchase_date', '-id')[:30])


@login_required
@user_passes_test(is_admin)
def purchase_return_detail(request, pk, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    ret = get_object_or_404(_scoped(env, PurchaseReturn).select_related('bill', 'party'), pk=pk)
    return _render(request, env, 'procurement/_return_detail.html', 'purchase_returns', ret.number, ret=ret,
                   lines=ret.lines.select_related('inventory_item'))


# ============================================================ reports

REPORT_TABS = [('outstanding', 'Order outstanding'), ('pending', 'Bills pending'), ('payables', 'Payables ageing'),
               ('gst', 'Input GST'), ('scorecard', 'Supplier scorecard'), ('prices', 'Price history'), ('reorder', 'Reorder')]


@login_required
@user_passes_test(is_admin)
def purchase_reports(request, restaurant_slug=None):
    env, err = _env(request, restaurant_slug)
    if err:
        return err
    r, b = env['restaurant'], env['branch']
    tab = request.GET.get('tab') if request.GET.get('tab') in dict(REPORT_TABS) else 'outstanding'
    key, label, start, end = M.resolve_period(request.GET.get('period') or 'this_month', request.GET.get('from', ''), request.GET.get('to', ''))
    export = request.GET.get('export') == 'csv'
    ctx = {'tab': tab, 'tabs': REPORT_TABS, 'period': key, 'label': label, 'start': start, 'end': end, 'periods': M.PERIODS}

    if tab == 'outstanding':
        rows = P.outstanding_orders(r, b)
        if export:
            return _csv('order-outstanding.csv', ['PO', 'Supplier', 'Item', 'Ordered', 'Received', 'Pending', 'Pending value', 'Expected', 'Days late'],
                        [[x['order'].number, x['order'].supplier_name, x['item'].name, x['ordered'], x['received'], x['pending'], x['value'],
                          x['expected'] or '', x['days_late']] for x in rows])
        ctx.update(rows=rows, total=sum((x['value'] for x in rows), P.ZERO))
    elif tab == 'pending':
        rows = P.pending_bills(r, b)
        if export:
            return _csv('purchase-bills-pending.csv', ['GRN', 'Date', 'Supplier', 'Challan', 'PO', 'Value', 'Age (days)'],
                        [[x['grn'].number, x['grn'].receipt_date, x['grn'].supplier_name, x['grn'].challan_no,
                          x['grn'].order.number if x['grn'].order else '', x['grn'].value, x['age']] for x in rows])
        ctx.update(rows=rows, total=sum((x['grn'].value for x in rows), P.ZERO))
    elif tab == 'payables':
        rows, buckets = P.payables(r, b)
        if export:
            return _csv('payables-ageing.csv', ['Bill', 'Bill date', 'Supplier', 'Due date', 'Total', 'Paid', 'Balance', 'Days overdue'],
                        [[x['bill'].invoice_number, x['bill'].purchase_date, x['bill'].supplier_name, x['bill'].due_date or '',
                          x['bill'].grand_total, x['bill'].paid_amount, x['due'], x['overdue']] for x in rows])
        ctx.update(rows=rows, buckets=buckets, total=sum((x['due'] for x in rows), P.ZERO), methods=PurchaseOrder.PaymentMethod.choices,
                   today=timezone.localdate())
    elif tab == 'gst':
        slabs, summary = P.input_gst(r, b, start, end)
        if export:
            return _csv(f'input-gst-{start}-{end}.csv', ['GST rate %', 'Taxable', 'CGST', 'SGST', 'IGST'],
                        [[rate, v['taxable'], v['cgst'], v['sgst'], v['igst']] for rate, v in slabs]
                        + [[], ['Eligible ITC', summary['eligible']], ['Reverse charge', summary['rcm']], ['Ineligible ITC', summary['ineligible']],
                           ['Reversed by debit notes', summary['reversed']], ['Net ITC', summary['net_itc']]])
        ctx.update(slabs=slabs, summary=summary)
    elif tab == 'scorecard':
        rows = P.supplier_scorecard(r, b, start, end)
        if export:
            return _csv('supplier-scorecard.csv', ['Supplier', 'Orders', 'GRNs', 'On-time %', 'Fill rate %', 'Rejection %', 'Spend'],
                        [[x['name'], x['orders'], x['grns'], x['on_time_pct'], x['fill_pct'], x['reject_pct'], x['spend']] for x in rows])
        ctx.update(rows=rows)
    elif tab == 'prices':
        items = P.items_for(r, b).filter(is_active=True).order_by('name')
        item = items.filter(pk=P._int(request.GET.get('item'), 0)).first()
        rows, suppliers = P.price_history(r, b, item) if item else ([], [])
        ctx.update(items=items, item=item, rows=rows, suppliers=suppliers)
    else:
        rows = P.reorder_suggestions(r, b)
        if export:
            return _csv('reorder.csv', ['Item', 'Stock', 'Reorder level', 'On order', 'Suggested', 'Last supplier', 'Last rate'],
                        [[x['item'].name, x['stock'], x['level'], x['on_order'], x['suggest'], x['supplier'], x['rate']] for x in rows])
        ctx.update(rows=rows)
    return _render(request, env, 'procurement/_reports.html', 'purchase_reports', 'Purchase reports', **ctx)
