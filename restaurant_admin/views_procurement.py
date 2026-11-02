"""Bill-wise payments against Purchase Register bills. Rules live in restaurant_admin/procurement.py."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import PurchaseOrder
from restaurant_admin import procurement as P
from restaurant_admin.views import get_active_branch, is_admin, resolve_restaurant


def _env(request, slug):
    restaurant = resolve_restaurant(request, slug)
    if isinstance(restaurant, HttpResponse):
        return None, restaurant
    return {'restaurant': restaurant, 'branch': get_active_branch(request, restaurant),
            'base_url': reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': restaurant.slug})}, None


def _go(env, name, **kw):
    return redirect(f'restaurant_admin:{name}', restaurant_slug=env['restaurant'].slug, **kw)


def _scoped(env, model):
    return P._scoped(model.objects, env['restaurant'], env['branch'])


def _error(request, e):
    messages.error(request, ' '.join(e.messages) if isinstance(e, ValidationError) else str(e))

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
