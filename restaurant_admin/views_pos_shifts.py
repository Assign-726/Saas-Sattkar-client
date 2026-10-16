import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Q, Sum

from core.models import POSCounter, POSShift, Order, CreditNote, RestaurantSettings
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from restaurant_admin.views import is_admin, resolve_restaurant, get_active_branch, get_user_active_shift

logger = logging.getLogger(__name__)


def serialize_financial_summary(summary):
    if not summary:
        return {}
    res = {}
    for k, v in summary.items():
        if isinstance(v, Decimal):
            res[k] = float(v)
        else:
            res[k] = v
    return res


def serialize_shift(shift):
    if not shift:
        return None
    summary = serialize_financial_summary(shift.get_financial_summary())
    return {
        'id': shift.id,
        'counter_id': shift.counter.id,
        'counter_name': shift.counter.name,
        'counter_place': shift.counter.place or '',
        'cashier_id': shift.cashier.id,
        'cashier_name': shift.cashier.get_full_name() or shift.cashier.username,
        'status': shift.status,
        'opened_at': shift.opened_at.strftime('%d %b %Y, %I:%M %p'),
        'opened_at_iso': shift.opened_at.isoformat(),
        'closed_at': shift.closed_at.strftime('%d %b %Y, %I:%M %p') if shift.closed_at else None,
        'opening_amount': float(shift.opening_amount),
        'closing_cash_actual': float(shift.closing_cash_actual) if shift.closing_cash_actual is not None else None,
        'closing_cash_expected': float(shift.closing_cash_expected),
        'cash_discrepancy': float(shift.cash_discrepancy),
        'summary': summary
    }


# ── COUNTER MANAGEMENT (ADMIN) ───────────────────────────────────────────────

@login_required
def pos_counter_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    counters_qs = POSCounter.objects.filter(restaurant=restaurant)
    if active_branch:
        counters_qs = counters_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    counters_qs = counters_qs.order_by('name')

    # Pre-fetch active shifts for counters
    open_shifts = POSShift.objects.filter(
        restaurant=restaurant,
        status=POSShift.Status.OPEN
    ).select_related('cashier')
    if active_branch:
        open_shifts = open_shifts.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    active_shift_map = {s.counter_id: s for s in open_shifts}

    counter_list_data = []
    for c in counters_qs:
        shift = active_shift_map.get(c.id)
        counter_list_data.append({
            'counter': c,
            'active_shift': shift,
            'is_occupied': bool(shift),
            'cashier_name': (shift.cashier.get_full_name() or shift.cashier.username) if shift else None
        })

    context = {
        'restaurant': restaurant,
        'rslug': restaurant.slug,
        'active_branch': active_branch,
        'counters_data': counter_list_data,
        'active_menu': 'pos_counters',
    }
    return render(request, 'restaurant_admin/shifts/counter_list.html', context)


@login_required
def pos_counter_save_api(request, restaurant_slug=None):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Restaurant not found'}, status=403)

    active_branch = get_active_branch(request, restaurant)
    data = request.POST
    if request.content_type == 'application/json' or 'json' in request.META.get('HTTP_ACCEPT', ''):
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST

    counter_id = data.get('counter_id')
    name = (data.get('name') or '').strip()
    place = (data.get('place') or '').strip()
    try:
        opening_amt = Decimal(str(data.get('standard_opening_amount') or '0.00'))
    except Exception:
        opening_amt = Decimal('0.00')

    is_active = data.get('is_active') in [True, 'true', 1, '1', 'on']

    if not name:
        return JsonResponse({'success': False, 'error': 'Counter name is required.'}, status=400)

    try:
        if counter_id:
            counter = get_object_or_404(POSCounter, pk=counter_id, restaurant=restaurant)
            counter.name = name
            counter.place = place
            counter.standard_opening_amount = opening_amt
            counter.is_active = is_active
            counter.save()
            messages.success(request, f"Counter '{counter.name}' updated successfully.")
        else:
            counter = POSCounter.objects.create(
                restaurant=restaurant,
                branch=active_branch,
                name=name,
                place=place,
                standard_opening_amount=opening_amt,
                is_active=is_active
            )
            messages.success(request, f"Counter '{counter.name}' created successfully.")

        return JsonResponse({
            'success': True,
            'counter': {
                'id': counter.id,
                'name': counter.name,
                'place': counter.place,
                'standard_opening_amount': float(counter.standard_opening_amount),
                'is_active': counter.is_active
            }
        })
    except Exception as e:
        logger.exception("Error saving POS counter")
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@login_required
def pos_counter_toggle_api(request, pk, restaurant_slug=None):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    counter = get_object_or_404(POSCounter, pk=pk, restaurant=restaurant)

    # Check if there is an active shift before disabling
    if counter.is_active and counter.shifts.filter(status=POSShift.Status.OPEN).exists():
        return JsonResponse({
            'success': False,
            'error': f"Cannot disable '{counter.name}' while a shift is currently active on it."
        }, status=400)

    counter.is_active = not counter.is_active
    counter.save(update_fields=['is_active', 'updated_at'])
    return JsonResponse({
        'success': True,
        'is_active': counter.is_active,
        'message': f"Counter '{counter.name}' is now {'Active' if counter.is_active else 'Disabled'}."
    })


@login_required
def pos_counter_delete_api(request, pk, restaurant_slug=None):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    counter = get_object_or_404(POSCounter, pk=pk, restaurant=restaurant)

    if counter.shifts.exists():
        counter.is_active = False
        counter.save(update_fields=['is_active', 'updated_at'])
        return JsonResponse({
            'success': True,
            'action': 'deactivated',
            'message': f"Counter '{counter.name}' has past shifts and was deactivated instead of permanently deleted."
        })

    counter.delete()
    return JsonResponse({'success': True, 'action': 'deleted', 'message': f"Counter '{counter.name}' deleted successfully."})


# ── SHIFT SESSION LIFECYCLE APIS ─────────────────────────────────────────────

@login_required
def pos_active_shift_api(request, restaurant_slug=None):
    """
    Checks if current cashier has an active shift; returns counter choices.
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Restaurant not found'}, status=403)

    active_branch = get_active_branch(request, restaurant)
    active_shift = get_user_active_shift(request.user, restaurant, active_branch)

    # All active counters for branch
    counters_qs = POSCounter.objects.filter(restaurant=restaurant, is_active=True)
    if active_branch:
        counters_qs = counters_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    counters_qs = counters_qs.order_by('name')

    # Find who is occupying what counter
    occupied_shifts = POSShift.objects.filter(
        restaurant=restaurant,
        status=POSShift.Status.OPEN
    ).select_related('cashier')
    if active_branch:
        occupied_shifts = occupied_shifts.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    occupied_map = {s.counter_id: s for s in occupied_shifts}

    counter_list = []
    for c in counters_qs:
        occ_shift = occupied_map.get(c.id)
        is_my_shift = bool(occ_shift and occ_shift.cashier_id == request.user.id)
        is_other_user = bool(occ_shift and occ_shift.cashier_id != request.user.id)
        counter_list.append({
            'id': c.id,
            'name': c.name,
            'place': c.place or '',
            'standard_opening_amount': float(c.standard_opening_amount),
            'is_occupied': is_other_user,
            'occupied_by': (occ_shift.cashier.get_full_name() or occ_shift.cashier.username) if is_other_user else None,
            'is_my_active_counter': is_my_shift
        })

    profile = getattr(request.user, 'restaurant_profile', None)
    total_counters_count = POSCounter.objects.filter(restaurant=restaurant).count()
    return JsonResponse({
        'success': True,
        'has_active_shift': bool(active_shift),
        'shift': serialize_shift(active_shift),
        'user_name': request.user.get_full_name() or request.user.username,
        'user_has_pin': bool(profile and profile.pos_pin),
        'user_is_staff': bool(request.user.is_staff or request.user.is_superuser),
        'total_counters_count': total_counters_count,
        'available_counters': counter_list
    })


@login_required
def pos_open_shift_api(request, restaurant_slug=None):
    """
    Opens a new cashier shift after validating PIN and counter availability.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Restaurant not found'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    data = request.POST
    if request.content_type == 'application/json' or 'json' in request.META.get('HTTP_ACCEPT', ''):
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST

    counter_id = data.get('counter_id')
    pin = str(data.get('pin') or '').strip()
    try:
        opening_amount = Decimal(str(data.get('opening_amount') or '0.00'))
    except Exception:
        opening_amount = Decimal('0.00')

    # 1. PIN Verification
    user = request.user
    profile = getattr(user, 'restaurant_profile', None)
    settings_obj = RestaurantSettings.get_settings(restaurant)
    manager_pin = getattr(settings_obj, 'manager_pin', '1234') or '1234'

    pin_valid = False
    if profile and profile.pos_pin and profile.pos_pin.strip() == pin:
        pin_valid = True
    elif pin == manager_pin or (user.is_superuser and pin):
        pin_valid = True
    elif not (profile and profile.pos_pin) and (pin == manager_pin or pin == '1234'):
        # Fallback if no individual PIN has been set up yet
        pin_valid = True
    elif not (profile and profile.pos_pin) and (user.is_staff or user.is_superuser) and pin and len(pin) >= 4 and pin.isdigit():
        # First-time PIN setup for staff / admin: persist entered PIN!
        if profile:
            profile.pos_pin = pin
            profile.save(update_fields=['pos_pin'])
        pin_valid = True

    if not pin_valid:
        return JsonResponse({'success': False, 'error': 'Invalid POS Security PIN. Please re-enter (Default: 1234).'}, status=400)

    # 2. Counter Validation
    if not counter_id:
        return JsonResponse({'success': False, 'error': 'Please select a Counter to open register.'}, status=400)

    counter = get_object_or_404(POSCounter, pk=counter_id, restaurant=restaurant)
    if not counter.is_active:
        return JsonResponse({'success': False, 'error': f"Counter '{counter.name}' is currently disabled."}, status=400)

    # Check if another user already has this counter open
    existing_counter_shift = POSShift.objects.filter(
        counter=counter,
        status=POSShift.Status.OPEN
    ).select_related('cashier').first()

    if existing_counter_shift:
        if existing_counter_shift.cashier_id == user.id:
            # Reconnect to already open shift
            request.session['active_pos_shift_id'] = existing_counter_shift.id
            return JsonResponse({
                'success': True,
                'already_open': True,
                'message': f"Reconnected to your active session on {counter.name}.",
                'shift': serialize_shift(existing_counter_shift)
            })
        else:
            other_name = existing_counter_shift.cashier.get_full_name() or existing_counter_shift.cashier.username
            return JsonResponse({
                'success': False,
                'error': f"Counter '{counter.name}' is currently being operated by {other_name}. Close that session first."
            }, status=400)

    # Check if this user already has another counter open
    existing_user_shift = POSShift.objects.filter(
        cashier=user,
        restaurant=restaurant,
        status=POSShift.Status.OPEN
    ).first()
    if existing_user_shift:
        return JsonResponse({
            'success': False,
            'error': f"You already have an open shift on Counter '{existing_user_shift.counter.name}'. Please close it first."
        }, status=400)

    # 3. Create Open Shift
    shift = POSShift.objects.create(
        restaurant=restaurant,
        branch=active_branch,
        counter=counter,
        cashier=user,
        opening_amount=opening_amount,
        status=POSShift.Status.OPEN
    )

    request.session['active_pos_shift_id'] = shift.id
    logger.info(f"POS Shift #{shift.id} opened for user {user.username} on counter {counter.name}")

    return JsonResponse({
        'success': True,
        'message': f"Register opened on {counter.name} with float ₹{shift.opening_amount:.2f}.",
        'shift': serialize_shift(shift)
    })


@login_required
def pos_shift_summary_api(request, shift_id=None, restaurant_slug=None):
    """
    Returns live financial summary (X-Report preview) before closing.
    """
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Restaurant not found'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    if shift_id:
        shift = get_object_or_404(POSShift, pk=shift_id, restaurant=restaurant)
    else:
        shift = get_user_active_shift(request.user, restaurant, active_branch)

    if not shift:
        return JsonResponse({'success': False, 'error': 'No active shift found.'}, status=404)

    expected_cash = shift.calculate_expected_cash()
    summary = serialize_financial_summary(shift.get_financial_summary())

    return JsonResponse({
        'success': True,
        'shift': serialize_shift(shift),
        'expected_cash': float(expected_cash),
        'summary': summary
    })


@login_required
def pos_close_shift_api(request, shift_id=None, restaurant_slug=None):
    """
    Closes the register, calculates expected vs actual cash, logs discrepancy.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Restaurant not found'}, status=403)

    active_branch = get_active_branch(request, restaurant)

    if shift_id:
        shift = get_object_or_404(POSShift, pk=shift_id, restaurant=restaurant)
    else:
        shift = get_user_active_shift(request.user, restaurant, active_branch)

    if not shift or shift.status != POSShift.Status.OPEN:
        return JsonResponse({'success': False, 'error': 'No open register session to close.'}, status=400)

    # Permission check: cashier or manager/admin
    profile = getattr(request.user, 'restaurant_profile', None)
    is_mgmt = request.user.is_superuser or (profile and profile.role in [RestaurantUser.Role.GENERAL_MANAGER, RestaurantUser.Role.RESTAURANT_ADMIN])
    if shift.cashier_id != request.user.id and not is_mgmt:
        return HttpResponseForbidden("You are not authorized to close another cashier's shift.")

    data = request.POST
    if request.content_type == 'application/json' or 'json' in request.META.get('HTTP_ACCEPT', ''):
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST

    try:
        closing_cash_actual = Decimal(str(data.get('closing_cash_actual') or '0.00'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid closing cash amount.'}, status=400)

    notes = (data.get('notes') or '').strip()

    expected_cash = shift.calculate_expected_cash()
    discrepancy = closing_cash_actual - expected_cash

    shift.closing_cash_actual = closing_cash_actual
    shift.closing_cash_expected = expected_cash
    shift.cash_discrepancy = discrepancy
    shift.notes = notes
    shift.closed_at = timezone.now()
    shift.closed_by = request.user
    shift.status = POSShift.Status.CLOSED
    shift.save()

    if 'active_pos_shift_id' in request.session:
        del request.session['active_pos_shift_id']

    logger.info(f"POS Shift #{shift.id} closed. Expected: ₹{expected_cash}, Actual: ₹{closing_cash_actual}, Variance: ₹{discrepancy}")

    try:
        z_report_url = reverse('restaurant_admin:pos_shift_z_report', kwargs={'restaurant_slug': restaurant.slug, 'pk': shift.id})
    except Exception:
        z_report_url = f"/dashboard/{restaurant.slug}/shifts/{shift.id}/z-report/"

    return JsonResponse({
        'success': True,
        'message': f"Register closed successfully. Variance: ₹{discrepancy:+.2f}.",
        'shift': serialize_shift(shift),
        'summary': serialize_financial_summary(shift.get_financial_summary()),
        'z_report_url': z_report_url
    })


# ── AUDIT & Z-REPORT ─────────────────────────────────────────────────────────

@login_required
def pos_shift_history_view(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)
    shifts_qs = POSShift.objects.filter(restaurant=restaurant)
    if active_branch:
        shifts_qs = shifts_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

    # Filters
    counter_id = request.GET.get('counter')
    cashier_id = request.GET.get('cashier')
    status = request.GET.get('status')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    if counter_id:
        shifts_qs = shifts_qs.filter(counter_id=counter_id)
    if cashier_id:
        shifts_qs = shifts_qs.filter(cashier_id=cashier_id)
    if status in ['OPEN', 'CLOSED']:
        shifts_qs = shifts_qs.filter(status=status)
    if start_date:
        shifts_qs = shifts_qs.filter(opened_at__date__gte=start_date)
    if end_date:
        shifts_qs = shifts_qs.filter(opened_at__date__lte=end_date)

    shifts_qs = shifts_qs.select_related('counter', 'cashier', 'closed_by').order_by('-opened_at')

    # Aggregates
    closed_shifts = shifts_qs.filter(status=POSShift.Status.CLOSED)
    total_expected = closed_shifts.aggregate(Sum('closing_cash_expected'))['closing_cash_expected__sum'] or Decimal('0.00')
    total_actual = closed_shifts.aggregate(Sum('closing_cash_actual'))['closing_cash_actual__sum'] or Decimal('0.00')
    total_discrepancy = closed_shifts.aggregate(Sum('cash_discrepancy'))['cash_discrepancy__sum'] or Decimal('0.00')
    open_count = shifts_qs.filter(status=POSShift.Status.OPEN).count()

    paginator = Paginator(shifts_qs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    counters = POSCounter.objects.filter(restaurant=restaurant)
    if active_branch:
        counters = counters.filter(Q(branch=active_branch) | Q(branch__isnull=True))

    cashiers = RestaurantUser.objects.filter(restaurant=restaurant).select_related('user')

    context = {
        'restaurant': restaurant,
        'rslug': restaurant.slug,
        'active_branch': active_branch,
        'page_obj': page_obj,
        'total_expected': total_expected,
        'total_actual': total_actual,
        'total_discrepancy': total_discrepancy,
        'open_count': open_count,
        'counters': counters,
        'cashiers': cashiers,
        'active_menu': 'pos_shifts',
        'query_params': request.GET.urlencode()
    }
    return render(request, 'restaurant_admin/shifts/shift_history.html', context)


@login_required
def pos_shift_z_report_view(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    shift = get_object_or_404(POSShift, pk=pk, restaurant=restaurant)
    summary = shift.get_financial_summary()
    settings_obj = RestaurantSettings.get_settings(restaurant)

    # Order types distribution for this shift
    orders_qs = shift.orders.exclude(status=Order.OrderStatus.CANCELLED)
    dine_in_count = orders_qs.filter(order_type=Order.OrderType.DINE_IN).count()
    takeaway_count = orders_qs.filter(order_type=Order.OrderType.TAKEAWAY).count()
    delivery_count = orders_qs.filter(order_type=Order.OrderType.DELIVERY).count()

    context = {
        'restaurant': restaurant,
        'settings': settings_obj,
        'shift': shift,
        'summary': summary,
        'dine_in_count': dine_in_count,
        'takeaway_count': takeaway_count,
        'delivery_count': delivery_count,
        'generated_at': timezone.now(),
        'generated_by': request.user.get_full_name() or request.user.username
    }
    return render(request, 'restaurant_admin/orders/z_report_thermal.html', context)
