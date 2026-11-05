"""Waiter / Captain rules.

A waiter takes dine-in orders at a table and sends them to the kitchen. Settling the bill,
taking payment, invoices, credit notes and voiding items the kitchen already has belong to the
cashier / biller / admin. These helpers are the server-side gate; the POS also hides the buttons.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

# POS actions a waiter may post to order_create_manual
WAITER_ALLOWED_ACTIONS = {'fire_kot', 'save_kot', 'update_order', 'save_draft', 'draft'}

# Request keys that move money — a waiter's post never carries them
_PAYMENT_KEYS = ('payments', 'paid_amount', 'payment_status', 'payment_method', 'receipt_no',
                 'advance_amount_paid', 'advance_amount', 'gstin_no', 'gstin', 'invoice_number',
                 'excise_bill_no', 'excise', 'vat_bill_no', 'party_gstin', 'customer_gstin', 'party_id')

CLOSED_STATUSES = ('COMPLETED', 'C')


def is_waiter(user):
    if not getattr(user, 'is_authenticated', False) or getattr(user, 'is_superuser', False):
        return False
    profile = getattr(user, 'restaurant_profile', None)
    return bool(profile and profile.role == 'WAITER')


def can_settle(user):
    """Who may take payment, settle, invoice or issue a credit note: everyone but a waiter."""
    return not is_waiter(user)


def _dec(val):
    try:
        return Decimal(str(val or '0'))
    except (InvalidOperation, ValueError):
        return Decimal('0')


def sanitize_waiter_post(data, order=None):
    """Drop payment fields from a waiter's POS post. On an existing order, carry its own payment
    state forward so re-saving the items does not change what was already paid."""
    clean = data.dict() if hasattr(data, 'dict') and not isinstance(data, dict) else dict(data)
    for key in _PAYMENT_KEYS:
        clean.pop(key, None)
    if order is not None:
        clean['payments'] = list(order.payments or [])
        clean['advance_amount_paid'] = str(order.advance_amount_paid or 0)
        clean['payment_method'] = order.payment_method
    return clean


def waiter_order_error(user, order, action, table_obj, order_type, items_data, discount_amount):
    """Why a waiter may not make this POS post, or None when it is fine."""
    if action not in WAITER_ALLOWED_ACTIONS:
        if action == 'void_item':
            return 'Only a cashier or admin can void items already sent to the kitchen.'
        return 'Waiters can send orders to the kitchen but cannot settle, bill or take payment. Ask the cashier.'

    if str(order_type or '').lower() in ('takeaway', 'delivery'):
        return 'Waiters take dine-in orders only.'
    if not table_obj and not (order and order.table_id):
        return 'Pick a table first — waiters take dine-in orders at a table.'

    if order is not None:
        if order.status in CLOSED_STATUSES or order.payment_status == 'paid':
            return 'This bill is already settled. Ask the cashier to reopen it.'

        # voiding / reducing anything the kitchen already has
        if order.status != 'DRAFT':
            before = defaultdict(int)
            for it in order.items.all():
                before[it.menu_item_id] += it.quantity
            after = defaultdict(int)
            for it in (items_data if isinstance(items_data, list) else []):
                mid = it.get('menu_item_id') or it.get('id')
                try:
                    after[int(mid)] += int(it.get('quantity') or 0)
                except (TypeError, ValueError):
                    continue
            if any(after.get(mid, 0) < qty for mid, qty in before.items()):
                return 'Only a cashier or admin can void or reduce items already sent to the kitchen.'

    profile = getattr(user, 'restaurant_profile', None)
    if not getattr(profile, 'can_apply_discounts', False):
        existing_order_disc = _dec(order.discount_amount) if order is not None else Decimal('0')
        existing_item_disc = sum((_dec(i.discount_amount) for i in order.items.all()), Decimal('0')) if order is not None else Decimal('0')
        item_disc = sum((_dec(it.get('discount_amount')) for it in (items_data if isinstance(items_data, list) else [])), Decimal('0'))
        if _dec(discount_amount) > existing_order_disc or item_disc > existing_item_disc:
            return 'You do not have permission to apply discounts.'
    return None


def settle_role_required(view):
    """Block waiters from billing / payment / credit-note endpoints (403)."""
    from functools import wraps
    from django.http import JsonResponse

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if is_waiter(request.user):
            return JsonResponse({'success': False, 'error': 'Only a cashier or admin can settle bills, take payment '
                                                            'or issue credit notes.'}, status=403)
        return view(request, *args, **kwargs)
    return wrapped
