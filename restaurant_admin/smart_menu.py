import math
import logging
from datetime import timedelta
from typing import Dict, List, Optional, Any
from django.utils import timezone
from django.core.cache import cache
from django.db.models import Sum, Q

from core.models import MenuItem, Order, OrderItem

logger = logging.getLogger(__name__)

# Meal Slots Definition
# Time partitions covering the entire 24-hour cycle
MEAL_SLOTS = [
    {
        'key': 'breakfast',
        'label': 'Breakfast',
        'emoji': '🌅',
        'start_hour': 6,
        'start_min': 0,
        'end_hour': 11,
        'end_min': 0,
        'hours_display': '06:00 AM – 11:00 AM',
    },
    {
        'key': 'lunch',
        'label': 'Lunch',
        'emoji': '☀️',
        'start_hour': 11,
        'start_min': 0,
        'end_hour': 16,
        'end_min': 0,
        'hours_display': '11:00 AM – 04:00 PM',
    },
    {
        'key': 'evening_snacks',
        'label': 'Evening Snacks',
        'emoji': '☕',
        'start_hour': 16,
        'start_min': 0,
        'end_hour': 19,
        'end_min': 0,
        'hours_display': '04:00 PM – 07:00 PM',
    },
    {
        'key': 'dinner',
        'label': 'Dinner',
        'emoji': '🌙',
        'start_hour': 19,
        'start_min': 0,
        'end_hour': 23,
        'end_min': 30,
        'hours_display': '07:00 PM – 11:30 PM',
    },
    {
        'key': 'late_night',
        'label': 'Late Night',
        'emoji': '🦉',
        'start_hour': 23,
        'start_min': 30,
        'end_hour': 6,
        'end_min': 0,
        'hours_display': '11:30 PM – 06:00 AM',
    },
]

# Exponential Decay Constant for 7-day half-life:
# lambda = ln(2) / 7 days ~= 0.099021
LAMBDA_DECAY = math.log(2) / 7.0

# Minimum meal-slot affinity required to prevent accidental outliers
# If less than 5% of an item's sales occur in a slot, it is rejected from that slot
MIN_SLOT_AFFINITY = 0.05

# Cache TTL: 3 hours in seconds (10800s)
CACHE_TIMEOUT = 10800


def get_current_meal_slot(dt: Optional[timezone.datetime] = None) -> Dict[str, Any]:
    """
    Determine the active meal slot based on the local time.
    """
    if dt is None:
        dt = timezone.localtime(timezone.now())
    elif timezone.is_aware(dt):
        dt = timezone.localtime(dt)

    current_minutes = dt.hour * 60 + dt.minute

    for slot in MEAL_SLOTS:
        start_m = slot['start_hour'] * 60 + slot['start_min']
        end_m = slot['end_hour'] * 60 + slot['end_min']

        if start_m < end_m:
            # Normal daytime range (e.g., 06:00 to 11:00)
            if start_m <= current_minutes < end_m:
                return slot
        else:
            # Overnight range (e.g., 23:30 to 06:00)
            if current_minutes >= start_m or current_minutes < end_m:
                return slot

    return MEAL_SLOTS[1]  # Default fallback to lunch


def is_order_in_slot(order_time: timezone.datetime, slot: Dict[str, Any]) -> bool:
    """
    Check if a given order timestamp falls within a meal slot's window.
    """
    if timezone.is_aware(order_time):
        order_time = timezone.localtime(order_time)
    minutes = order_time.hour * 60 + order_time.minute
    start_m = slot['start_hour'] * 60 + slot['start_min']
    end_m = slot['end_hour'] * 60 + slot['end_min']

    if start_m < end_m:
        return start_m <= minutes < end_m
    else:
        return minutes >= start_m or minutes < end_m


def is_weekend(dt: timezone.datetime) -> bool:
    """
    F&B Weekend definition: Friday, Saturday, Sunday.
    """
    return dt.weekday() in (4, 5, 6)


def compute_smart_menu_recommendations(restaurant, branch=None, limit: int = 18) -> Dict[str, Any]:
    """
    Core mathematical calculation engine.
    Calculates the intelligent recommendations for all 5 meal slots simultaneously.
    """
    now = timezone.localtime(timezone.now())
    current_is_weekend = is_weekend(now)
    current_slot = get_current_meal_slot(now)

    # 1. Fetch available menu items
    menu_items_qs = MenuItem.objects.filter(restaurant=restaurant, availability=True)
    if branch:
        menu_items_qs = menu_items_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    available_item_ids = set(menu_items_qs.values_list('id', flat=True))

    if not available_item_ids:
        return {
            'current_slot': current_slot['key'],
            'current_label': current_slot['label'],
            'current_emoji': current_slot['emoji'],
            'current_hours': current_slot['hours_display'],
            'slots': {s['key']: {**s, 'item_ids': []} for s in MEAL_SLOTS}
        }

    # 2. Query non-cancelled orders from the past 30 days
    window_start = now - timedelta(days=30)
    order_filter = {
        'order__restaurant': restaurant,
        'order__order_time__gte': window_start,
        'menu_item_id__in': available_item_ids,
    }
    if branch:
        order_filter['order__branch'] = branch

    recent_order_items = (
        OrderItem.objects.filter(**order_filter)
        .exclude(order__status=Order.OrderStatus.CANCELLED)
        .select_related('order')
        .values('menu_item_id', 'quantity', 'order__order_time')
    )

    # 3. Query ongoing shift orders today (for Live Shift Velocity boost)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    shift_filter = {
        'order__restaurant': restaurant,
        'order__order_time__gte': today_start,
        'menu_item_id__in': available_item_ids,
    }
    if branch:
        shift_filter['order__branch'] = branch

    shift_velocity_qs = (
        OrderItem.objects.filter(**shift_filter)
        .exclude(order__status=Order.OrderStatus.CANCELLED)
        .values('menu_item_id')
        .annotate(shift_qty=Sum('quantity'))
    )
    shift_velocity_map = {row['menu_item_id']: float(row['shift_qty'] or 0) for row in shift_velocity_qs}

    # 4. Aggregate 30-day totals and slot-specific metrics
    item_overall_qty: Dict[int, float] = {}
    item_slot_qty: Dict[str, Dict[int, float]] = {s['key']: {} for s in MEAL_SLOTS}
    item_slot_weighted_scores: Dict[str, Dict[int, float]] = {s['key']: {} for s in MEAL_SLOTS}

    for oi in recent_order_items:
        m_id = oi['menu_item_id']
        qty = float(oi['quantity'] or 0)
        otime = oi['order__order_time']
        if not otime:
            continue
        if timezone.is_aware(otime):
            otime = timezone.localtime(otime)

        # Track total quantity for affinity calculation
        item_overall_qty[m_id] = item_overall_qty.get(m_id, 0.0) + qty

        # Days ago for exponential decay
        delta_days = max(0.0, (now - otime).total_seconds() / 86400.0)
        decay_weight = math.exp(-LAMBDA_DECAY * delta_days)

        # Day-type multiplier
        order_is_weekend = is_weekend(otime)
        day_mult = 1.3 if (order_is_weekend == current_is_weekend) else 0.7

        # Check which slot this historical order fell into
        for slot in MEAL_SLOTS:
            if is_order_in_slot(otime, slot):
                s_key = slot['key']
                item_slot_qty[s_key][m_id] = item_slot_qty[s_key].get(m_id, 0.0) + qty
                
                # Formula contribution: Quantity * Decay * DayMultiplier
                contrib = qty * decay_weight * day_mult
                item_slot_weighted_scores[s_key][m_id] = item_slot_weighted_scores[s_key].get(m_id, 0.0) + contrib

    # 5. Compute overall top items for intelligent fallback (by sales volume, not alphabetical)
    overall_top_ids = sorted(item_overall_qty.keys(), key=lambda x: item_overall_qty[x], reverse=True)
    fallback_items = list(overall_top_ids)
    if len(fallback_items) < limit:
        for mid in menu_items_qs.order_by('category__name', 'name').values_list('id', flat=True)[:limit]:
            if mid not in fallback_items:
                fallback_items.append(mid)

    slots_output = {}
    for slot in MEAL_SLOTS:
        s_key = slot['key']
        scores: Dict[int, float] = {}
        slot_quantities = item_slot_qty[s_key]
        weighted_scores = item_slot_weighted_scores[s_key]

        for m_id, w_score in weighted_scores.items():
            tot_qty = item_overall_qty.get(m_id, 0.0)
            slot_qty = slot_quantities.get(m_id, 0.0)

            # Affinity check: fraction of sales that happen in this slot
            affinity = (slot_qty / tot_qty) if tot_qty > 0 else 0.0
            if affinity < MIN_SLOT_AFFINITY:
                # Outlier elimination: dish does not genuinely belong to this meal session
                continue

            # Live Shift Velocity Boost (+2.5 per unit sold in today's active shift)
            velocity_boost = shift_velocity_map.get(m_id, 0.0) * 2.5

            final_score = (w_score * affinity) + velocity_boost
            scores[m_id] = final_score

        # Rank items descending by final score
        ranked_items = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:limit]

        # Cold-Start / Low-Data Fallback padding
        if len(ranked_items) < 6:
            for f_id in fallback_items:
                if f_id not in ranked_items:
                    ranked_items.append(f_id)
                if len(ranked_items) >= limit:
                    break

        slots_output[s_key] = {
            'key': s_key,
            'label': slot['label'],
            'emoji': slot['emoji'],
            'hours_display': slot['hours_display'],
            'item_ids': ranked_items,
        }

    return {
        'current_slot': current_slot['key'],
        'current_label': current_slot['label'],
        'current_emoji': current_slot['emoji'],
        'current_hours': current_slot['hours_display'],
        'slots': slots_output,
    }


def get_all_meal_slots_recommendations(restaurant, branch=None, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Cached wrapper around compute_smart_menu_recommendations.
    Ensures zero database load on repeated POS renders.
    """
    branch_id = branch.id if branch else 0
    cache_key = f"smart_pos_menu:{restaurant.id}:{branch_id}"

    if not force_refresh:
        cached = cache.get(cache_key)
        if cached:
            # Dynamically verify the current slot in case the hour crossed over
            now = timezone.localtime(timezone.now())
            slot_now = get_current_meal_slot(now)
            cached['current_slot'] = slot_now['key']
            cached['current_label'] = slot_now['label']
            cached['current_emoji'] = slot_now['emoji']
            cached['current_hours'] = slot_now['hours_display']
            return cached

    data = compute_smart_menu_recommendations(restaurant, branch=branch)
    try:
        cache.set(cache_key, data, CACHE_TIMEOUT)
    except Exception as e:
        logger.warning(f"Failed to cache smart POS menu: {e}")
    return data


def invalidate_smart_menu_cache(restaurant, branch=None):
    """
    Invalidate the smart POS cache when a shift changes or new major orders land.
    """
    branch_id = branch.id if branch else 0
    cache_key = f"smart_pos_menu:{restaurant.id}:{branch_id}"
    cache.delete(cache_key)
