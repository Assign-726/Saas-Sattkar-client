from django.core.management.base import BaseCommand
from django.utils import timezone
from events.models import EventBooking


class Command(BaseCommand):
    help = 'Releases expired 15-minute pending ticket reservations back into available inventory.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired_qs = EventBooking.objects.filter(
            payment_status=EventBooking.PaymentStatus.PENDING,
            expires_at__lt=now
        )
        count = expired_qs.count()
        if count > 0:
            updated = expired_qs.update(payment_status=EventBooking.PaymentStatus.EXPIRED)
            self.stdout.write(self.style.SUCCESS(f"Successfully released {updated} expired pending booking(s) back to inventory."))
        else:
            self.stdout.write(self.style.NOTICE("No expired pending bookings found."))
