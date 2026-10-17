from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from integrations import dispatch
from integrations.models import WebhookDelivery


class Command(BaseCommand):
    help = 'Retry failed webhook deliveries whose back-off has elapsed. Run every minute from cron.'

    def add_arguments(self, parser):
        parser.add_argument('--prune-days', type=int, default=30, help='Delete delivered/abandoned log rows older than this many days (0 = keep).')

    def handle(self, *args, **opts):
        sent, ok = dispatch.retry_due()
        # Deliveries stuck in PENDING (e.g. the server restarted mid-send) are picked up after 2 minutes
        stale = WebhookDelivery.objects.filter(status=WebhookDelivery.Status.PENDING, created_at__lte=timezone.now() - timedelta(minutes=2),
                                               endpoint__is_active=True).select_related('endpoint')[:200]
        for d in stale:
            sent += 1
            ok += dispatch.deliver(d)
        pruned = 0
        if opts['prune_days']:
            pruned, _ = WebhookDelivery.objects.filter(created_at__lt=timezone.now() - timedelta(days=opts['prune_days']),
                                                       status__in=[WebhookDelivery.Status.SUCCESS, WebhookDelivery.Status.GAVE_UP]).delete()
        self.stdout.write(f'Sent {sent}, delivered {ok}, pruned {pruned}.')
