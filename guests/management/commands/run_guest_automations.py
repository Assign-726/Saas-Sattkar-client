from django.core.management.base import BaseCommand, CommandError

from guests import growth
from super_admin.models import Restaurant


class Command(BaseCommand):
    help = 'Send automatic birthday, anniversary and win-back emails. Run once a day (e.g. 10:00) from cron.'

    def add_arguments(self, parser):
        parser.add_argument('--base-url', required=True, help='Public URL of this admin, used for unsubscribe links, e.g. https://admin.example.com')
        parser.add_argument('--restaurant', help='Only this business (slug)')

    def handle(self, *args, **opts):
        qs = Restaurant.objects.filter(is_active=True)
        if opts['restaurant']:
            qs = qs.filter(slug=opts['restaurant'])
            if not qs.exists():
                raise CommandError('No active business with that slug.')
        for r in qs:
            results = growth.run_automations(r, opts['base_url'])
            for kind, (sent, failed) in results.items():
                self.stdout.write(f'{r.name}: {kind} · {sent} sent, {failed} failed')
        self.stdout.write('Done.')
