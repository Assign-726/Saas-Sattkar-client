"""The Morning Brief: yesterday compared with the business's own normal days,
plus what needs attention today. Plain rules; every alert says why and links to the fix.

``compute_brief(restaurant)`` for one business, ``compute_brief(None)`` for the whole platform.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

ZERO = Decimal('0')


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, start + timedelta(days=1)


def _scope(qs, restaurant, field='restaurant'):
    return qs.filter(**{field: restaurant}) if restaurant is not None else qs


def _pct(now, before):
    if not before:
        return None
    return round(float((Decimal(now) - Decimal(before)) / Decimal(before) * 100))


def compute_brief(restaurant=None, today=None, module='all'):
    """``module`` is 'restaurant', 'hotel' or 'all' — each admin shell sees only its own operation."""
    from core.models import InventoryItem, Order, OrderItem
    from events.models import ClubEvent, EventBooking
    from guests.models import Feedback, Guest
    from hotel.models import Room, RoomBooking
    from reservations.models import Reservation

    today = today or timezone.localdate()
    yesterday = today - timedelta(days=1)
    y_start, y_end = _day_bounds(yesterday)
    t_start, t_end = _day_bounds(today)

    orders = _scope(Order.objects.exclude(status='DRAFT'), restaurant)
    live_orders = orders.exclude(status='C')

    # One grouped query for the 36 days the brief needs (yesterday + 4 weeks back + baseline)
    window_start, _ = _day_bounds(yesterday - timedelta(days=35))
    per_day = {
        row['day']: (int(row['n'] or 0), Decimal(row['rev'] or 0))
        for row in live_orders.filter(order_time__gte=window_start, order_time__lt=y_end)
        .annotate(day=TruncDate('order_time', tzinfo=timezone.get_current_timezone()))
        .values('day').annotate(n=Count('id'), rev=Sum('total_amount'))
    }

    def day_figures(day):
        return per_day.get(day, (0, ZERO))

    y_orders, y_food_rev = day_figures(yesterday)
    y_cancelled = orders.filter(status='C', order_time__gte=y_start, order_time__lt=y_end).count()

    stays = _scope(RoomBooking.objects.all(), restaurant, 'branch__restaurant')
    y_room_rev = Decimal(stays.filter(booking_status='CHECKED_OUT', check_out_date=yesterday).aggregate(s=Sum('grand_total'))['s'] or 0)
    tickets = _scope(EventBooking.objects.filter(payment_status='SUCCESS'), restaurant, 'event__branch__restaurant')
    y_event_rev = Decimal(tickets.filter(created_at__gte=y_start, created_at__lt=y_end).aggregate(s=Sum('total_amount'))['s'] or 0)
    show_fb = module in ('all', 'restaurant')
    show_rooms = module in ('all', 'hotel')
    y_revenue = (y_food_rev + y_event_rev if show_fb else ZERO) + (y_room_rev if show_rooms else ZERO)

    # Baselines: last 28 days (before yesterday) and the same weekday over 4 weeks (food & beverage)
    base_days = [day_figures(yesterday - timedelta(days=i)) for i in range(1, 29)]
    base_avg_orders = sum(n for n, _ in base_days) / 28
    base_avg_rev = sum((r for _, r in base_days), ZERO) / 28
    same_weekday = [day_figures(yesterday - timedelta(days=7 * w))[1] for w in range(1, 5)]
    same_weekday_avg = sum(same_weekday, ZERO) / 4
    base_cancel_avg = _scope(Order.objects.filter(status='C', order_time__gte=y_start - timedelta(days=28), order_time__lt=y_start), restaurant).count() / 28

    top_items = list(
        OrderItem.objects.filter(order__in=live_orders.filter(order_time__gte=y_start, order_time__lt=y_end))
        .values('menu_item__name').annotate(qty=Sum('quantity')).order_by('-qty')[:5])

    # Today
    arrivals = stays.filter(check_in_date=today).exclude(booking_status='CANCELLED').select_related('room')
    departures = stays.filter(check_out_date=today, booking_status='CHECKED_IN').select_related('room')
    in_house = stays.filter(booking_status='CHECKED_IN').count()
    rooms = _scope(Room.objects.filter(is_active=True), restaurant, 'branch__restaurant')
    rooms_total = rooms.count()
    rooms_cleaning = rooms.filter(status='CLEANING').count()
    rooms_maintenance = rooms.filter(status='MAINTENANCE').count()
    occupancy = round(in_house * 100 / rooms_total) if rooms_total else None
    stay_due = Decimal(stays.filter(booking_status='CHECKED_OUT', payment_status__in=['PENDING', 'PARTIALLY_PAID'])
                       .aggregate(s=Sum('grand_total'))['s'] or 0)
    reservations = _scope(Reservation.objects.filter(date=today).exclude(status__in=['X', 'N']), restaurant).order_by('start_time')
    events = _scope(ClubEvent.objects.filter(start_datetime__gte=t_start, start_datetime__lt=t_end), restaurant, 'branch__restaurant')

    # Guests
    guests = _scope(Guest.objects.all(), restaurant)
    week = [today + timedelta(days=i) for i in range(8)]
    occasion_q = Q()
    for d in week:
        occasion_q |= Q(birthday__month=d.month, birthday__day=d.day) | Q(anniversary__month=d.month, anniversary__day=d.day)
    occasions = []
    for g in guests.filter(occasion_q)[:50]:
        for label, value in (('Birthday', g.birthday), ('Anniversary', g.anniversary)):
            if value:
                for d in week:
                    if (value.month, value.day) == (d.month, d.day):
                        occasions.append({'guest': g, 'label': label, 'date': d, 'in_days': (d - today).days})
    occasions.sort(key=lambda o: o['in_days'])
    new_guests = guests.filter(created_at__gte=y_start, created_at__lt=y_end).count()

    # Feedback
    from super_admin.modules import FEEDBACK_AREAS
    feedback = _scope(Feedback.objects.all(), restaurant)
    if module in FEEDBACK_AREAS:
        feedback = feedback.filter(area__in=FEEDBACK_AREAS[module])
    y_feedback = feedback.filter(created_at__gte=y_start, created_at__lt=y_end)
    y_fb_stats = y_feedback.aggregate(n=Count('id'), avg=Avg('rating'))
    open_complaints = feedback.filter(rating__lte=2, status__in=['NEW', 'IN_PROGRESS'])
    open_complaints_count = open_complaints.count()

    # Attention
    low_stock_qs = _scope(InventoryItem.objects.filter(is_active=True, quantity__lte=F('reorder_level')), restaurant)
    low_stock = list(low_stock_qs.order_by('quantity').values('name', 'quantity', 'reorder_level', 'unit')[:8])
    low_stock_count = low_stock_qs.count()
    unpaid = live_orders.filter(payment_status__in=['unpaid', 'partially_paid']).aggregate(n=Count('id'), due=Sum('due_amount'))

    figures = {
        'revenue': y_revenue, 'food_revenue': y_food_rev, 'room_revenue': y_room_rev, 'event_revenue': y_event_rev,
        'other_revenue': y_room_rev + y_event_rev,
        'orders': y_orders, 'average_order': (y_food_rev / y_orders) if y_orders else ZERO,
        'cancelled': y_cancelled, 'new_guests': new_guests,
        'vs_weekday_pct': _pct(y_food_rev, same_weekday_avg), 'vs_baseline_pct': _pct(y_food_rev, base_avg_rev),
        'baseline_revenue': base_avg_rev, 'baseline_orders': base_avg_orders,
    }

    # Rules → alerts
    alerts = []
    had_business = base_avg_orders >= 3 and show_fb
    if had_business and y_orders == 0:
        alerts.append(('critical', 'No orders yesterday', f'You usually take about {round(base_avg_orders)} orders a day. Check that billing and online ordering are working.', 'Open POS', 'pos'))
    elif show_fb and figures['vs_weekday_pct'] is not None and figures['vs_weekday_pct'] <= -25:
        alerts.append(('warning', f'Food & beverage sales down {abs(figures["vs_weekday_pct"])}%',
                       f'Yesterday brought ₹{y_food_rev:,.0f} against ₹{same_weekday_avg:,.0f} on a typical {yesterday:%A}.', 'See analytics', 'analytics'))
    elif show_fb and figures['vs_weekday_pct'] is not None and figures['vs_weekday_pct'] >= 25:
        alerts.append(('good', f'Sales up {figures["vs_weekday_pct"]}% on a normal {yesterday:%A}',
                       f'₹{y_food_rev:,.0f} against ₹{same_weekday_avg:,.0f} usually. Worth knowing what worked.', 'See analytics', 'analytics'))
    if show_fb and y_cancelled >= max(3, base_cancel_avg * 2):
        alerts.append(('warning', f'{y_cancelled} cancelled orders yesterday', f'That is above your usual {base_cancel_avg:.1f} a day. Review the reasons in Order Records.', 'Review orders', 'orders'))
    if show_fb and low_stock_count:
        alerts.append(('warning', f'{low_stock_count} stock item{"s" if low_stock_count != 1 else ""} at or below reorder level',
                       ', '.join(i['name'] for i in low_stock[:4]) + ('…' if low_stock_count > 4 else ''), 'Reorder stock', 'inventory'))
    if show_fb and unpaid['n']:
        alerts.append(('warning', f'{unpaid["n"]} unpaid bill{"s" if unpaid["n"] != 1 else ""} · ₹{Decimal(unpaid["due"] or 0):,.0f} due',
                       'Collect or settle outstanding balances.', 'Collect payments', 'orders'))
    if open_complaints_count:
        oldest = open_complaints.order_by('created_at').first()
        hours = int((timezone.now() - oldest.created_at).total_seconds() // 3600)
        alerts.append(('critical' if hours >= 24 else 'warning',
                       f'{open_complaints_count} unhappy guest{"s" if open_complaints_count != 1 else ""} waiting for a reply',
                       (f'The oldest has waited {hours} hour{"s" if hours != 1 else ""}.' if hours else 'It came in within the last hour.')
                       + ' A quick call usually saves the review.', 'Open feedback', 'feedback'))
    if show_rooms and rooms_cleaning:
        alerts.append(('warning' if arrivals.exists() else 'info', f'{rooms_cleaning} room{"s" if rooms_cleaning != 1 else ""} still being cleaned',
                       'Get them ready before today\'s arrivals check in.' if arrivals.exists() else 'Mark them clean once housekeeping is done.', 'Housekeeping', 'rooms'))
    if show_rooms and stay_due:
        alerts.append(('warning', f'₹{stay_due:,.0f} unpaid on checked-out stays', 'Settle the folios with the guests or their companies.', 'Bookings', 'bookings'))
    if show_rooms and arrivals.exists():
        alerts.append(('info', f'{arrivals.count()} arrival{"s" if arrivals.count() != 1 else ""} today',
                       f'{departures.count()} departure{"s" if departures.count() != 1 else ""} due · {in_house} room{"s" if in_house != 1 else ""} occupied.', 'Front desk', 'hotel'))
    if show_fb and reservations.exists():
        alerts.append(('info', f'{reservations.count()} table reservation{"s" if reservations.count() != 1 else ""} today',
                       f'{sum(r.party_size for r in reservations)} covers booked.', 'Reservations', 'reservations'))
    if occasions:
        soon = [o for o in occasions if o['in_days'] <= 1]
        alerts.append(('good', f'{len(occasions)} guest celebration{"s" if len(occasions) != 1 else ""} this week',
                       (f'{len(soon)} today or tomorrow. ' if soon else '') + 'A small gesture brings regulars back.', 'See guests', 'guests'))

    return {
        'today': today, 'yesterday': yesterday, 'restaurant': restaurant,
        'module': module, 'show_fb': show_fb, 'show_rooms': show_rooms,
        'rooms_total': rooms_total, 'rooms_cleaning': rooms_cleaning, 'rooms_maintenance': rooms_maintenance,
        'occupancy': occupancy, 'stay_due': stay_due,
        'figures': figures, 'top_items': top_items if show_fb else [],
        'arrivals': list(arrivals[:10]), 'arrivals_count': arrivals.count(),
        'departures_count': departures.count(), 'in_house': in_house,
        'reservations': list(reservations[:10]), 'reservations_count': reservations.count(),
        'events': list(events[:5]), 'occasions': occasions[:12],
        'low_stock': low_stock if show_fb else [], 'low_stock_count': low_stock_count if show_fb else 0,
        'unpaid_count': unpaid['n'] or 0, 'unpaid_due': Decimal(unpaid['due'] or 0),
        'feedback_count': y_fb_stats['n'] or 0, 'feedback_avg': round(y_fb_stats['avg'], 1) if y_fb_stats['avg'] else None,
        'open_complaints': open_complaints_count,
        'alerts': [{'severity': s, 'title': t, 'detail': d, 'action_label': al, 'action_key': ak} for s, t, d, al, ak in alerts],
    }
