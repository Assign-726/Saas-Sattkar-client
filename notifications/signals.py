"""Turn restaurant events into notifications (orders are notified explicitly at their entry points)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def _later(fn):
    def run():
        try:
            fn()
        except Exception:
            logger.exception('Notification failed')
    transaction.on_commit(run)


# Kitchen → floor: an order became READY ------------------------------------------------

@receiver(pre_save, sender='core.Order')
def _order_before(sender, instance, **kwargs):
    instance._nt_prev_status = None
    if instance.pk and instance.status == 'READY':
        instance._nt_prev_status = sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()


@receiver(post_save, sender='core.Order')
def _order_after(sender, instance, created, **kwargs):
    if created or instance.status != 'READY' or getattr(instance, '_nt_prev_status', None) in (None, 'READY'):
        return
    pk = instance.pk

    def run():
        from core.models import Order
        from notifications.service import notify_ready
        o = Order.objects.select_related('restaurant', 'branch', 'table').filter(pk=pk).first()
        if o:
            notify_ready(o)
    _later(run)


# Guest reservations ---------------------------------------------------------------------

@receiver(post_save, sender='reservations.Reservation')
def _reservation(sender, instance, created, **kwargs):
    if not created:
        return
    r = instance

    def run():
        from notifications.service import notify
        try:
            url = reverse('restaurant_admin:reservation_detail', kwargs={'restaurant_slug': r.restaurant.slug, 'pk': r.pk})
        except Exception:
            url = ''
        when = f'{r.date:%a %d %b} at {r.start_time:%I:%M %p}'.replace(' 0', ' ')
        notify(r.restaurant, 'reservation', f'New reservation · {r.customer_name}', body=f'{r.party_size} guests · {when}'
               + (f' · {r.occasion}' if r.occasion else ''), branch=r.branch, url=url, source='guest',
               level='info', dedupe_key=f'reservation:{r.pk}')
    _later(run)


# Guest feedback -------------------------------------------------------------------------

@receiver(post_save, sender='guests.Feedback')
def _feedback(sender, instance, created, **kwargs):
    if not created:
        return
    f = instance

    def run():
        from notifications.service import notify
        try:
            url = reverse('restaurant_admin:feedback_detail', kwargs={'restaurant_slug': f.restaurant.slug, 'pk': f.pk})
        except Exception:
            url = ''
        low = f.rating <= 2
        who = f.name or (f.guest.name if f.guest_id else 'A guest')
        notify(f.restaurant, 'feedback', f'{"⚠ " if low else ""}{f.rating}★ feedback from {who}', body=(f.comment or 'No comment')[:200],
               branch=f.branch, url=url, source='guest', level='warning' if low else 'info', sound=low, dedupe_key=f'feedback:{f.pk}')
    _later(run)


# Stock dropping to the reorder level ----------------------------------------------------

@receiver(pre_save, sender='core.InventoryItem')
def _stock_before(sender, instance, **kwargs):
    instance._nt_prev_qty = None
    if instance.pk:
        instance._nt_prev_qty = sender.objects.filter(pk=instance.pk).values_list('quantity', flat=True).first()


@receiver(post_save, sender='core.InventoryItem')
def _stock_after(sender, instance, created, **kwargs):
    prev = getattr(instance, '_nt_prev_qty', None)
    if created or prev is None or not instance.is_active or instance.restaurant_id is None:
        return
    if not (prev > instance.reorder_level >= instance.quantity):
        return
    item = instance

    def run():
        from notifications.service import notify
        try:
            url = reverse('restaurant_admin:inventory_list', kwargs={'restaurant_slug': item.restaurant.slug})
        except Exception:
            url = ''
        out = item.quantity <= 0
        notify(item.restaurant, 'low_stock', f'{"Out of stock" if out else "Low stock"} · {item.name}',
               body=f'{item.quantity} {item.unit} left (reorder at {item.reorder_level})', branch=item.branch, url=url,
               source='inventory', level='critical' if out else 'warning', sound=False,
               dedupe_key=f'stock:{item.pk}:{timezone.localdate():%Y%m%d}')
    _later(run)
