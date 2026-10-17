"""Turn model changes into webhook events. Each handler remembers the previous state in
``pre_save`` and emits only real transitions, after the transaction commits."""
import logging

from django.db import transaction
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from integrations import dispatch, payloads

logger = logging.getLogger(__name__)


def _later(fn):
    def run():
        try:
            fn()
        except Exception:
            logger.exception('Webhook event failed')
    transaction.on_commit(run)


def _cache_key(restaurant_id):
    return f'integrations:has_webhooks:{restaurant_id}'


def _has_endpoints(restaurant_id):
    """Cheap check so ordinary saves in a business without webhooks cost nothing extra."""
    if not restaurant_id:
        return False
    found = cache.get(_cache_key(restaurant_id))
    if found is None:
        from integrations.models import WebhookEndpoint
        found = WebhookEndpoint.objects.filter(restaurant_id=restaurant_id, is_active=True).exists()
        cache.set(_cache_key(restaurant_id), found, 300)
    return found


@receiver([post_save, post_delete], sender='integrations.WebhookEndpoint')
def _endpoints_changed(sender, instance, **kwargs):
    cache.delete(_cache_key(instance.restaurant_id))


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@receiver(pre_save, sender='core.Order')
def _order_before(sender, instance, **kwargs):
    instance._wh_prev = None
    if instance.pk and _has_endpoints(instance.restaurant_id):
        instance._wh_prev = sender.objects.filter(pk=instance.pk).values('status', 'payment_status').first()


@receiver(post_save, sender='core.Order')
def _order_after(sender, instance, created, **kwargs):
    if not _has_endpoints(instance.restaurant_id):
        return
    prev = getattr(instance, '_wh_prev', None) or {}
    status, before = instance.status, prev.get('status')
    events = []
    if status != 'DRAFT' and (created or before == 'DRAFT'):
        events.append('order.created')
    elif before and status != before:
        events.append('order.cancelled' if status == 'C' else 'order.status_changed')
    if instance.payment_status == 'paid' and prev.get('payment_status') != 'paid' and status not in ('DRAFT', 'C'):
        events.append('order.paid')
    if not events:
        return
    pk = instance.pk

    def fire():
        from core.models import Order
        o = Order.objects.select_related('branch', 'restaurant').filter(pk=pk).first()
        if not o:
            return
        data = payloads.order(o)
        if before and before != o.status:
            data['previous_status'] = payloads.ORDER_STATUS.get(before, before)
        for ev in events:
            dispatch.emit(ev, restaurant=o.restaurant, branch=o.branch, data=data)
    _later(fire)


# --------------------------------------------------------------------------
# Room bookings
# --------------------------------------------------------------------------

BOOKING_EVENTS = {'CHECKED_IN': 'booking.checked_in', 'CHECKED_OUT': 'booking.checked_out', 'CANCELLED': 'booking.cancelled',
                  'NO_SHOW': 'booking.cancelled'}
HOLDS_ROOM = ('RESERVED', 'CHECKED_IN')


@receiver(pre_save, sender='hotel.RoomBooking')
def _booking_before(sender, instance, **kwargs):
    branch_restaurant = getattr(instance.branch, 'restaurant_id', None) if instance.branch_id else None
    instance._wh_prev = (sender.objects.filter(pk=instance.pk).values('booking_status', 'check_in_date', 'check_out_date', 'room_id').first()
                         if instance.pk and _has_endpoints(branch_restaurant) else None)


@receiver(post_save, sender='hotel.RoomBooking')
def _booking_after(sender, instance, created, **kwargs):
    branch = instance.branch
    if not branch or not _has_endpoints(branch.restaurant_id):
        return
    before = getattr(instance, '_wh_prev', None) or {}
    moved = bool(before) and (before['check_in_date'], before['check_out_date'], before['room_id']) != (
        instance.check_in_date, instance.check_out_date, instance.room_id)
    if created:
        event = 'booking.created'
    elif before.get('booking_status') != instance.booking_status and instance.booking_status in BOOKING_EVENTS:
        event = BOOKING_EVENTS[instance.booking_status]
    elif moved:
        event = 'booking.modified'
    else:
        event = None
    pk = instance.pk

    if event:
        def fire():
            from hotel.models import RoomBooking
            b = RoomBooking.objects.select_related('room__category', 'branch__restaurant').filter(pk=pk).first()
            if b:
                dispatch.emit(event, restaurant=b.branch.restaurant, branch=b.branch, data=payloads.booking(b))
        _later(fire)

    # Inventory changes when a stay starts or stops holding a room, or moves
    held_before = before.get('booking_status') in HOLDS_ROOM if before else False
    held_now = instance.booking_status in HOLDS_ROOM
    if created and held_now or held_before != held_now or moved and held_now:
        starts = [d for d in (instance.check_in_date, before.get('check_in_date')) if d]
        ends = [d for d in (instance.check_out_date, before.get('check_out_date')) if d]
        cats = {instance.room.category_id} if instance.room_id else set()
        if before.get('room_id') and before['room_id'] != instance.room_id:
            from hotel.models import Room
            cats |= set(Room.objects.filter(pk=before['room_id']).values_list('category_id', flat=True))
        ari_changed(branch, cats, min(starts), max(ends), 'availability')


def ari_changed(branch, category_ids, start, end, reason):
    """Tell channel managers to re-read /api/v1/rates for these room types and nights."""
    if branch is None or not _has_endpoints(branch.restaurant_id):
        return
    data = {'branch_id': branch.id, 'room_type_ids': sorted(c for c in category_ids if c), 'reason': reason,
            'from': start.isoformat() if start else None, 'to': end.isoformat() if end else None,
            'rates_url': '/api/v1/rates'}

    def fire():
        dispatch.emit('ari.changed', restaurant=branch.restaurant, branch=branch, data=data)
    _later(fire)


@receiver([post_save, post_delete], sender='hotel.RateOverride')
def _rate_changed(sender, instance, **kwargs):
    cat = instance.category
    ari_changed(cat.branch, {cat.id}, instance.start_date, instance.end_date, 'rates')


@receiver(post_save, sender='hotel.MaintenanceIssue')
def _ooo_changed(sender, instance, **kwargs):
    if instance.out_of_order:
        from django.utils import timezone
        ari_changed(instance.branch, {instance.room.category_id}, instance.block_from or timezone.localdate(), instance.block_until, 'availability')


@receiver([post_save, post_delete], sender='integrations.ChannelRoomMapping')
def _mapping_changed(sender, instance, **kwargs):
    from django.utils import timezone
    ari_changed(instance.connection.branch, {instance.category_id}, timezone.localdate(), None, 'channel_settings')


# --------------------------------------------------------------------------
# Menu items — availability and price only (what partners must mirror)
# --------------------------------------------------------------------------

@receiver(pre_save, sender='core.MenuItem')
def _item_before(sender, instance, **kwargs):
    instance._wh_prev = (sender.objects.filter(pk=instance.pk).values('availability', 'price', 'markup_price').first()
                         if instance.pk and _has_endpoints(instance.restaurant_id) else None)


@receiver(post_save, sender='core.MenuItem')
def _item_after(sender, instance, created, **kwargs):
    prev = getattr(instance, '_wh_prev', None)
    if created or not prev or not _has_endpoints(instance.restaurant_id):
        return
    if (prev['availability'], prev['price'], prev['markup_price']) == (instance.availability, instance.price, instance.markup_price):
        return
    pk = instance.pk

    def fire():
        from core.models import MenuItem
        m = MenuItem.objects.select_related('category', 'restaurant', 'branch').filter(pk=pk).first()
        if m:
            data = payloads.menu_item(m)
            data['previous'] = {'available': prev['availability'], 'price': str(prev['price'])}
            dispatch.emit('menu.item_updated', restaurant=m.restaurant, branch=m.branch, data=data)
    _later(fire)
