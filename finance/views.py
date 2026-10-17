"""Expenses and the profit & loss report, in the Restaurant and Hotel admin shells."""
import csv
import json
import os
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, ProtectedError, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from finance import pnl
from finance.models import Expense, ExpenseCategory
from super_admin.activity import log_activity, user_role

MANAGE_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN', 'CLUB_ADMIN'}
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
RECEIPT_TYPES = ('.pdf', '.jpg', '.jpeg', '.png', '.webp', '.heic')


def can_manage(user):
    return user.is_authenticated and (user.is_superuser or user_role(user) in MANAGE_ROLES)


def _money(v):
    try:
        d = Decimal(str(v).replace(',', '').strip())
    except (InvalidOperation, AttributeError):
        return None
    return d if d.is_finite() else None


def _can_consolidate(user):
    return user.is_superuser or user_role(user) == 'GENERAL_MANAGER'


def _departments(request, env):
    """Expense departments this shell may record and list: its own module plus shared costs."""
    return [env['area'], Expense.Department.SHARED]


def _locked_branch(user):
    profile = getattr(user, 'restaurant_profile', None)
    return getattr(profile, 'branch', None) if profile else None


def _branches(request, restaurant):
    locked = _locked_branch(request.user)
    if locked:
        return [locked], locked, True
    branches = list(restaurant.branches.all().order_by('name'))
    wanted = request.GET.get('branch', '')
    chosen = next((b for b in branches if str(b.id) == wanted), None)
    return branches, chosen, False


def _restaurant_env(request, slug):
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    if not request.user.is_staff or not can_manage(request.user):
        return HttpResponseForbidden('Expenses and profit & loss are available to administrators.')
    return {'restaurant': restaurant, 'shell': 'guests/shell_restaurant.html', 'area': 'restaurant', 'ctx': {'restaurant': restaurant},
            'base_url': reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': slug})}


def _hotel_env(request, slug, ctx):
    if not can_manage(request.user):
        return HttpResponseForbidden('Expenses and profit & loss are available to administrators.')
    return {'restaurant': ctx['restaurant'], 'shell': 'guests/shell_hotel.html', 'area': 'hotel', 'ctx': ctx,
            'base_url': reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': slug})}


def _render(request, env, partial, context, menu):
    branches, selected, locked = _branches(request, env['restaurant'])
    data = {**env['ctx'], **context, 'partial': partial, 'base_url': env['base_url'], 'active_menu': menu,
            'branches': branches, 'selected_branch': selected, 'branch_locked': locked}
    return render(request, env['shell'], data)


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------

def _save_expense(request, env, expense=None):
    r = env['restaurant']
    branches, _, locked = _branches(request, r)
    p = request.POST
    amount, tax = _money(p.get('amount')), _money(p.get('tax_amount') or '0')
    day = parse_date(p.get('date') or '')
    category = ExpenseCategory.objects.filter(restaurant=r, pk=p.get('category') or 0).first()
    errors = []
    if amount is None or amount <= 0:
        errors.append('Enter the amount paid.')
    if tax is None or tax < 0 or (amount is not None and tax > amount):
        errors.append('GST must be between zero and the amount.')
    if not day:
        errors.append('Pick the date of the expense.')
    elif day > timezone.localdate():
        errors.append('The date cannot be in the future.')
    if not category:
        errors.append('Choose a category.')
    branch = next((b for b in branches if str(b.id) == p.get('branch')), None)
    if locked:
        branch = branches[0]
    receipt = request.FILES.get('receipt')
    if receipt:
        if receipt.size > MAX_RECEIPT_BYTES:
            errors.append('Receipts must be smaller than 8 MB.')
        elif not receipt.name.lower().endswith(RECEIPT_TYPES):
            errors.append('Upload the receipt as a PDF or photo (JPG, PNG, WEBP, HEIC).')
    if errors:
        for e in errors:
            messages.error(request, e)
        return None
    e = expense or Expense(restaurant=r, created_by=request.user)
    e.branch, e.category, e.date, e.amount, e.tax_amount = branch, category, day, amount, tax
    e.department = p.get('department') if p.get('department') in _departments(request, env) else env['area']
    e.payment_method = p.get('payment_method') if p.get('payment_method') in Expense.Method.values else Expense.Method.CASH
    e.paid_to, e.reference, e.note = p.get('paid_to', '').strip()[:150], p.get('reference', '').strip()[:80], p.get('note', '').strip()[:300]
    if receipt:
        e.receipt = receipt
    e.save()
    return e


def _expenses(request, env, slug):
    r = env['restaurant']
    cats = ExpenseCategory.ensure_defaults(r)
    branches, selected, locked = _branches(request, r)
    if request.method == 'POST':
        op = request.POST.get('op')
        if op in ('add', 'edit'):
            existing = None
            if op == 'edit':
                existing = get_object_or_404(Expense, pk=request.POST.get('pk'), restaurant=r, department__in=_departments(request, env), **({'branch__in': branches} if locked else {}))
            e = _save_expense(request, env, existing)
            if e:
                log_activity(request, action='CREATE' if op == 'add' else 'UPDATE', message=f'{"Recorded" if op == "add" else "Edited"} expense · {e.category.name} ₹{e.amount:,.2f}',
                             area=env['area'], restaurant=r, branch=e.branch, object_type='Expense', object_id=e.pk)
                messages.success(request, 'Expense saved.')
        elif op == 'delete':
            e = get_object_or_404(Expense, pk=request.POST.get('pk'), restaurant=r, department__in=_departments(request, env), **({'branch__in': branches} if locked else {}))
            log_activity(request, action='DELETE', message=f'Deleted expense · {e.category.name} ₹{e.amount:,.2f} on {e.date:%d %b %Y}', area=env['area'], restaurant=r)
            e.delete()
            messages.success(request, 'Expense deleted.')
        elif op == 'add_category' and not locked:
            name = request.POST.get('name', '').strip()[:80]
            if name and not cats.filter(name__iexact=name).exists():
                ExpenseCategory.objects.create(restaurant=r, name=name)
                messages.success(request, f'Category "{name}" added.')
        elif op == 'toggle_category' and not locked:
            c = get_object_or_404(ExpenseCategory, pk=request.POST.get('pk'), restaurant=r)
            c.is_active = not c.is_active
            c.save(update_fields=['is_active'])
        nxt = request.POST.get('next', '')
        return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else request.path)

    today = timezone.localdate()
    start = parse_date(request.GET.get('from') or '') or today.replace(day=1)
    end = parse_date(request.GET.get('to') or '') or today
    depts = _departments(request, env)
    qs = Expense.objects.filter(restaurant=r, department__in=depts, date__gte=start, date__lte=end).select_related('category', 'branch', 'created_by')
    if locked or selected:
        qs = qs.filter(branch=selected or branches[0])
    if request.GET.get('category', '').isdigit():
        qs = qs.filter(category_id=int(request.GET['category']))
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(paid_to__icontains=q) | Q(reference__icontains=q) | Q(note__icontains=q))

    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="expenses-{start}-{end}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Category', 'Belongs to', 'Branch', 'Paid to', 'Bill no.', 'Method', 'Amount', 'GST', 'Net', 'Note'])
        for e in qs.order_by('date'):
            w.writerow([e.date.isoformat(), e.category.name, e.get_department_display(), e.branch.name if e.branch else 'All branches', e.paid_to, e.reference,
                        e.get_payment_method_display(), e.amount, e.tax_amount, e.net_amount, e.note])
        return resp

    totals = qs.aggregate(total=Sum('amount'), tax=Sum('tax_amount'), n=Count('id'))
    p_start, p_end = pnl.previous_period(start, end)
    prev_qs = Expense.objects.filter(restaurant=r, department__in=depts, date__gte=p_start, date__lte=p_end)
    if locked or selected:
        prev_qs = prev_qs.filter(branch=selected or branches[0])
    prev_total = prev_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    total = totals['total'] or Decimal('0')
    by_cat = list(qs.values('category__name', 'category__icon').annotate(s=Sum('amount')).order_by('-s')[:6])
    for row in by_cat:
        row['pct'] = round(float(row['s'] / total * 100)) if total else 0
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    return _render(request, env, 'finance/_expenses.html', {
        'page': page, 'cats': cats, 'active_cats': cats.filter(is_active=True), 'methods': Expense.Method.choices,
        'start': start, 'end': end, 'total': total, 'tax': totals['tax'] or 0, 'count': totals['n'],
        'prev_total': prev_total, 'change_pct': round(float((total - prev_total) / prev_total * 100)) if prev_total else None,
        'by_cat': by_cat, 'today': timezone.localdate(), 'here': request.get_full_path(), 'page_title': 'Expenses',
        'module': env['area'], 'module_label': 'Hotel' if env['area'] == 'hotel' else 'Restaurant',
        'filters': request.GET,
    }, 'expenses')


# --------------------------------------------------------------------------
# Profit & loss
# --------------------------------------------------------------------------

def _pnl(request, env, slug):
    r = env['restaurant']
    branches, selected, locked = _branches(request, r)
    branch = branches[0] if locked else selected
    named = pnl.periods()
    key = request.GET.get('period', 'this_month')
    if key == 'custom':
        start = parse_date(request.GET.get('from') or '')
        end = parse_date(request.GET.get('to') or '')
        if not start or not end or end < start:
            key, (label, start, end) = 'this_month', named['this_month']
        else:
            label = f'{start:%d %b %Y} – {end:%d %b %Y}'
    else:
        key = key if key in named else 'this_month'
        label, start, end = named[key]

    module = env['area']
    scopes = [(module, 'Hotel' if module == 'hotel' else 'Restaurant')]
    if _can_consolidate(request.user):
        scopes.append(('all', 'Whole business'))
    scope = request.GET.get('scope') if request.GET.get('scope') in dict(scopes) else module
    report = pnl.compute_pnl(r, start, end, branch, scope)
    p_start, p_end = pnl.previous_period(start, end)
    prev = pnl.compute_pnl(r, p_start, p_end, branch, scope)

    def delta(now, before):
        return round(float((now - before) / abs(before) * 100)) if before else None

    compare = []
    if not branch and len(branches) > 1:
        for b in branches:
            p = pnl.compute_pnl(r, start, end, b, scope)
            compare.append({'branch': b, 'revenue': p['revenue'], 'costs': p['commission'] + p['purchases'] + p['operating'],
                            'profit': p['net_profit'], 'margin': p['net_margin']})

    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="profit-and-loss-{start}-{end}.csv"'
        w = csv.writer(resp)
        w.writerow(['Profit & loss', r.name, dict(scopes)[scope], branch.name if branch else 'All branches', f'{start} to {end}'])
        for line in report['revenue_lines']:
            w.writerow(['Revenue', line['name'], line['amount']])
        w.writerow(['Revenue', 'Total revenue', report['revenue']])
        w.writerow(['Cost of sales', 'Partner commission', report['commission']])
        w.writerow(['Cost of sales', 'Purchases (net of GST)', report['purchases']])
        w.writerow(['', 'Gross profit', report['gross_profit']])
        w.writerow(['Operating', 'Payroll', report['payroll']])
        for line in report['expense_lines']:
            w.writerow(['Operating', line['name'], line['amount']])
        w.writerow(['', 'Net profit', report['net_profit']])
        return resp

    chart = pnl.trend(r, 6, branch, module=scope)
    return _render(request, env, 'finance/_pnl.html', {
        'report': report, 'prev': prev, 'label': label, 'period': key, 'periods': [(k, v[0]) for k, v in named.items()],
        'start': start, 'end': end, 'compare': compare,
        'deltas': {'revenue': delta(report['revenue'], prev['revenue']), 'profit': delta(report['net_profit'], prev['net_profit']),
                   'costs': delta(report['commission'] + report['purchases'] + report['operating'], prev['commission'] + prev['purchases'] + prev['operating'])},
        'total_costs': report['commission'] + report['purchases'] + report['operating'],
        'chart_json': json.dumps(chart), 'page_title': 'Profit & Loss', 'scope': scope, 'scopes': scopes,
        'scope_label': dict(scopes)[scope],
    }, 'pnl')


def _receipt(request, env, slug, pk):
    from django.http import FileResponse, Http404
    branches, _, locked = _branches(request, env['restaurant'])
    e = get_object_or_404(Expense, pk=pk, restaurant=env['restaurant'], department__in=_departments(request, env), **({'branch__in': branches} if locked else {}))
    if not e.receipt:
        raise Http404
    return FileResponse(e.receipt.open('rb'), filename=os.path.basename(e.receipt.name))


def _wrap(impl):
    @login_required
    def view(request, restaurant_slug, *args, **kwargs):
        env = _restaurant_env(request, restaurant_slug)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, restaurant_slug, *args, **kwargs)
    return view


def _wrap_hotel(impl):
    from hotel.views_admin import hotel_admin_required

    @hotel_admin_required
    def view(request, restaurant_slug, ctx, *args, **kwargs):
        env = _hotel_env(request, restaurant_slug, ctx)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, restaurant_slug, *args, **kwargs)
    return view


expenses, profit_and_loss, receipt = _wrap(_expenses), _wrap(_pnl), _wrap(_receipt)
hotel_expenses, hotel_profit_and_loss, hotel_receipt = _wrap_hotel(_expenses), _wrap_hotel(_pnl), _wrap_hotel(_receipt)
