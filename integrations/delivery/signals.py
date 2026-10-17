"""Keep Zomato / Swiggy in step with what happens in the restaurant.

* The kitchen marks a platform order READY on the KDS → tell the platform the food is ready.
* A menu item is switched off / on → update its stock status on every platform it is listed on.
Both run after the transaction commits, so a rolled-back save never reaches a platform.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_save, sender='core.Order')
def _order_ready_before(sender, instance, **kwargs):
    instance._dl_prev_status = None
    if instance.pk and instance.status == 'READY' and not getattr(instance, '_from_delivery', False) and instance.order_type == 'delivery':
        instance._dl_prev_status = sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()


@receiver(post_save, sender='core.Order')
def _order_ready_after(sender, instance, created, **kwargs):
    if created or getattr(instance, '_from_delivery', False) or instance.status != 'READY':
        return
    if getattr(instance, '_dl_prev_status', None) in (None, 'READY'):
        return
    pk = instance.pk

    def run():
        from integrations.delivery import service
        from integrations.models import ExternalOrderLink
        link = ExternalOrderLink.objects.filter(order_id=pk, source__in=['zomato', 'swiggy'], platform_status='accepted').first()
        if link:
            try:
                service.perform(link, 'mark_ready', None, source='kitchen')
            except Exception:
                logger.exception('Could not tell the platform that order %s is ready', pk)
    transaction.on_commit(run)


@receiver(pre_save, sender='core.MenuItem')
def _item_before(sender, instance, **kwargs):
    instance._dl_prev_available = None
    if instance.pk:
        instance._dl_prev_available = sender.objects.filter(pk=instance.pk).values_list('availability', flat=True).first()


@receiver(post_save, sender='core.MenuItem')
def _item_after(sender, instance, created, **kwargs):
    prev = getattr(instance, '_dl_prev_available', None)
    if created or prev is None or prev == instance.availability:
        return
    pk, available = instance.pk, instance.availability

    def run():
        from integrations.delivery import service
        from integrations.models import PlatformMenuMapping
        for m in PlatformMenuMapping.objects.filter(menu_item_id=pk, is_listed=True, connection__is_active=True,
                                                    connection__provider__in=['zomato', 'swiggy']).select_related('connection'):
            try:
                service.set_item_available(m, available)
            except Exception:
                logger.exception('Could not sync stock of item %s to %s', pk, m.connection.provider)
    transaction.on_commit(run)
