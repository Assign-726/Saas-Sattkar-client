"""Create notifications and query them per user.

Only events that need someone's attention create a notification. Orders rung up by staff at
the POS never do — the cashier already knows about them. Guest QR / table orders, online and
API orders, Zomato and Swiggy orders, kitchen "food ready", reservations, guest feedback and
stock alerts do.
"""
import logging

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification, NotificationState

logger = logging.getLogger(__name__)

SOURCE_LABELS = {'pos': 'POS', 'qr': 'Table QR', 'online': 'Online', 'website': 'Website', 'api': 'API', 'zomato': 'Zomato', 'swiggy': 'Swiggy',
                 'kitchen': 'Kitchen', 'guest': 'Guest', 'inventory': 'Stock', 'magicpin': 'magicpin', 'ondc': 'ONDC'}
KIND_STYLE = {
    'order_new': ('fa-receipt', 'blue'), 'order_addon': ('fa-plus', 'indigo'), 'order_ready': ('fa-bell-concierge', 'green'),
    'kot_new': ('fa-fire-burner', 'orange'),
    'delivery_new': ('fa-motorcycle', 'brand'), 'delivery_cancelled': ('fa-ban', 'red'), 'delivery_attention': ('fa-triangle-exclamation', 'orange'),
    'reservation': ('fa-calendar-check', 'teal'), 'room_booking': ('fa-bed', 'indigo'), 'room_alert': ('fa-triangle-exclamation', 'red'), 'feedback': ('fa-comment-dots', 'purple'), 'low_stock': ('fa-boxes-stacked', 'orange'),
    'system': ('fa-circle-info', 'gray'),
}
PLATFORM_COLORS = {'zomato': '#E23744', 'swiggy': '#FC8019'}


def notify(restaurant, kind, title, *, body='', branch=None, url='', source='', order=None, level='info', sound=True,
           dedupe_key='', data=None):
    """Create a notification. A repeated ``dedupe_key`` returns the existing one instead."""
    if restaurant is None:
        return None
    try:
        with transaction.atomic():
            return Notification.objects.create(
                restaurant=restaurant, branch=branch, kind=kind, title=title[:160], body=(body or '')[:300], url=url[:300],
                source=source[:20], order=order, level=level, sound=sound, dedupe_key=dedupe_key[:120], data=data or {})
    except IntegrityError:
        return Notification.objects.filter(restaurant=restaurant, dedupe_key=dedupe_key).first()
    except Exception:
        logger.exception('Could not create notification %s', kind)
        return None


def _order_url(order):
    try:
        return reverse('restaurant_admin:order_detail', kwargs={'restaurant_slug': order.restaurant.slug, 'pk': order.pk})
    except Exception:
        return ''


def order_data(order):
    table = order.table.name if order.table_id and order.table else (f'Table {order.table_number}' if order.table_number else order.get_order_type_display())
    return {
        'order_number': order.order_number or f'#{order.pk}', 'table_name': table, 'order_type': order.get_order_type_display(),
        'customer_name': order.customer_name or 'Guest', 'total_amount': float(order.total_amount or 0),
        'items_count': sum(i.quantity for i in order.items.all()),
    }


def notify_new_order(order, source='qr'):
    """A guest / online / partner order arrived (never called for POS orders)."""
    d = order_data(order)
    label = SOURCE_LABELS.get(source, source.title() or 'Online')
    where = d['table_name'] if order.order_type == 'dine_in' else d['order_type']
    return notify(order.restaurant, 'order_new', f'New order {d["order_number"]} · {where}',
                  body=f'{d["customer_name"]} · {d["items_count"]} item{"s" if d["items_count"] != 1 else ""} · ₹{d["total_amount"]:,.0f} · via {label}',
                  branch=order.branch, url=_order_url(order), source=source, order=order, level='info',
                  dedupe_key=f'order_new:{order.pk}', data=d)


def notify_addon(order, added_items, source='qr'):
    d = order_data(order)
    return notify(order.restaurant, 'order_addon', f'{d["table_name"]} added {added_items} item{"s" if added_items != 1 else ""}',
                  body=f'Order {d["order_number"]} · {d["customer_name"]} · new kitchen ticket', branch=order.branch,
                  url=_order_url(order), source=source, order=order, data=d)


def notify_kot(order, lines, items_count, round_no=1):
    """A kitchen ticket was fired from the POS (first round or an add-on). The kitchen and the
    KDS need it even though the cashier rang it up; QR / online orders already notify as order_new."""
    d = order_data(order)
    where = d['table_name'] if order.order_type == 'dine_in' else d['order_type']
    label = f'KOT {d["order_number"]}' + (f' · round {round_no}' if round_no > 1 else '')
    return notify(order.restaurant, 'kot_new', f'{label} · {where}',
                  body=f'{items_count} item{"s" if items_count != 1 else ""}: {lines}'[:300],
                  branch=order.branch, url=_kds_url(order), source='pos', order=order, level='info',
                  dedupe_key=f'kot:{order.pk}:{round_no}', data={**d, 'round': round_no})


def _kds_url(order):
    try:
        return reverse('restaurant_admin:kitchen_kds', kwargs={'restaurant_slug': order.restaurant.slug})
    except Exception:
        return ''


def notify_ready(order):
    d = order_data(order)
    where = d['table_name'] if order.order_type == 'dine_in' else d['order_type']
    return notify(order.restaurant, 'order_ready', f'Food ready · {where}', body=f'Order {d["order_number"]} is cooked and ready to serve',
                  branch=order.branch, url=_order_url(order), source='kitchen', order=order, level='success', data=d)


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

TABS = [('inbox', 'Inbox'), ('unread', 'Unread'), ('read', 'Read'), ('archived', 'Archived')]
CATEGORIES = [('all', 'All'), ('orders', 'Orders'), ('online', 'Zomato & Swiggy'), ('kitchen', 'Kitchen'), ('alerts', 'Alerts')]
CATEGORY_KINDS = {}
for _k, _c in Notification.CATEGORY.items():
    CATEGORY_KINDS.setdefault(_c, []).append(_k)


# Which notification kinds each admin shell shows
MODULE_KINDS = {
    'hotel': ['room_booking', 'room_alert', 'feedback', 'system'],
    'restaurant': [k for k in Notification.CATEGORY if k not in ('room_booking', 'room_alert')],
    # Kitchen / KDS logins: only what the line needs to cook
    'kitchen': ['kot_new', 'order_new', 'order_addon', 'delivery_new', 'delivery_cancelled'],
}


def visible(user, restaurant, branch, module='restaurant'):
    """Notifications this user can see: their business, the branch they work in, plus business-wide ones."""
    qs = Notification.objects.filter(restaurant=restaurant, kind__in=MODULE_KINDS.get(module, MODULE_KINDS['restaurant']))
    if branch is not None:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    state = NotificationState.objects.filter(notification=OuterRef('pk'), user=user)
    return qs.annotate(read_at=Subquery(state.values('read_at')[:1]), archived_at=Subquery(state.values('archived_at')[:1]))


def filtered(qs, tab='inbox', category='all'):
    if tab == 'archived':
        qs = qs.filter(archived_at__isnull=False)
    else:
        qs = qs.filter(archived_at__isnull=True)
        if tab == 'unread':
            qs = qs.filter(read_at__isnull=True)
        elif tab == 'read':
            qs = qs.filter(read_at__isnull=False)
    if category in CATEGORY_KINDS:
        qs = qs.filter(kind__in=CATEGORY_KINDS[category])
    return qs


def unread_count(qs):
    return qs.filter(archived_at__isnull=True, read_at__isnull=True).count()


def serialize(n):
    icon, tone = KIND_STYLE.get(n.kind, ('fa-bell', 'gray'))
    return {
        'id': n.pk, 'kind': n.kind, 'category': n.category, 'level': n.level, 'title': n.title, 'body': n.body, 'url': n.url,
        'source': n.source, 'source_label': SOURCE_LABELS.get(n.source, n.source.title()), 'icon': icon, 'tone': tone,
        'brand': PLATFORM_COLORS.get(n.source, ''), 'sound': n.sound, 'created_at': n.created_at.isoformat(),
        'read': bool(getattr(n, 'read_at', None)), 'archived': bool(getattr(n, 'archived_at', None)), 'data': n.data or {},
        'order_id': n.order_id,
    }


def set_state(user, ids, *, read=None, archived=None):
    """Mark notifications read/unread and archived/unarchived for one user. Returns how many changed."""
    now = timezone.now()
    ids = list(ids)
    existing = {s.notification_id: s for s in NotificationState.objects.filter(user=user, notification_id__in=ids)}
    create, update = [], []
    for nid in ids:
        s = existing.get(nid) or NotificationState(user=user, notification_id=nid)
        if read is not None:
            s.read_at = (s.read_at or now) if read else None
        if archived is not None:
            s.archived_at = (s.archived_at or now) if archived else None
            if archived and s.read_at is None:
                s.read_at = now  # archiving something also means you've seen it
        (update if s.pk else create).append(s)
    NotificationState.objects.bulk_create(create, ignore_conflicts=True)
    if update:
        NotificationState.objects.bulk_update(update, ['read_at', 'archived_at'])
    return len(create) + len(update)
