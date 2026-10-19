"""Delete notifications older than N days (default 90). Run daily from cron."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.models import Notification


class Command(BaseCommand):
    help = 'Delete admin notifications older than --days (default 90).'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90)

    def handle(self, *args, days, **options):
        n, _ = Notification.objects.filter(created_at__lt=timezone.now() - timedelta(days=max(7, days))).delete()
        self.stdout.write(f'Deleted {n} old notification rows.')
