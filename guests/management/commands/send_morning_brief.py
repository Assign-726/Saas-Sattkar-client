"""Email each business's Morning Brief to its managers.

Schedule daily, e.g. cron:  0 8 * * *  python manage.py send_morning_brief --base-url https://admin.example.com
"""
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.urls import reverse

from core.emails import get_mail_connection_and_sender
from guests.brief import compute_brief
from super_admin.models import Restaurant, RestaurantUser

MANAGER_ROLES = ('GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN')


class Command(BaseCommand):
    help = "Email the Morning Brief to each business's managers."

    def add_arguments(self, parser):
        parser.add_argument('--restaurant', help='Restaurant slug (default: every active business)')
        parser.add_argument('--to', action='append', help='Send only to this address (repeatable)')
        parser.add_argument('--base-url', default='', help='Public admin URL used for the "Open full brief" button')

    def handle(self, *args, **opts):
        qs = Restaurant.objects.filter(is_active=True)
        if opts.get('restaurant'):
            qs = qs.filter(slug=opts['restaurant'])
        for restaurant in qs:
            recipients = opts.get('to') or sorted({
                ru.user.email for ru in RestaurantUser.objects.filter(restaurant=restaurant, role__in=MANAGER_ROLES).select_related('user')
                if ru.user.email and ru.user.is_active
            })
            if not recipients:
                self.stdout.write(f'{restaurant.name}: no manager emails, skipped')
                continue
            brief = compute_brief(restaurant)
            path = reverse('restaurant_admin:morning_brief', kwargs={'restaurant_slug': restaurant.slug})
            url = (opts['base_url'].rstrip('/') + path) if opts.get('base_url') else ''
            html = render_to_string('guests/email/morning_brief.html', {'brief': brief, 'restaurant': restaurant, 'dashboard_url': url})
            alerts = brief['alerts']
            subject = f"Morning Brief · {restaurant.name} · {brief['today']:%d %b}" + (f" · {len(alerts)} to review" if alerts else ' · all clear')
            text = '\n'.join([subject, ''] + [f"- {a['title']}: {a['detail']}" for a in alerts] + ([f'\nOpen: {url}'] if url else []))
            connection, from_email, _ = get_mail_connection_and_sender(branch=restaurant.branches.first())
            msg = EmailMultiAlternatives(subject, text, from_email, recipients, connection=connection)
            msg.attach_alternative(html, 'text/html')
            msg.send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f'{restaurant.name}: sent to {len(recipients)} recipient(s)'))
