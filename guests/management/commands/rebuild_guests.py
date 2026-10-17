from django.core.management.base import BaseCommand

from guests.services import rebuild_guests
from super_admin.models import Restaurant


class Command(BaseCommand):
    help = 'Build guest profiles from historical orders, stays, reservations and event tickets.'

    def add_arguments(self, parser):
        parser.add_argument('--restaurant', help='Restaurant slug (default: all)')

    def handle(self, *args, **opts):
        qs = Restaurant.objects.all()
        if opts.get('restaurant'):
            qs = qs.filter(slug=opts['restaurant'])
        for r in qs:
            created, updated = rebuild_guests(r)
            self.stdout.write(self.style.SUCCESS(f'{r.name}: {created} new guests, {updated} refreshed'))
