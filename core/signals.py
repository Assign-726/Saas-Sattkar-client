from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Order
from core.sse import publish


@receiver(post_save, sender=Order)
def order_created_signal(sender, instance: Order, created: bool, **kwargs):
    try:
        restaurant = instance.restaurant
        slug = getattr(restaurant, 'slug', None)
        if not slug:
            return
        # Publish only on create
        if created:
            publish(slug, {
                'type': 'new_order',
                'order_id': instance.id,
                'at': instance.order_time.isoformat() if instance.order_time else ''
            })
    except Exception:
        # best-effort, do not break request cycle
        pass
