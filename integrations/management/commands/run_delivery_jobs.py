"""Retry Zomato / Swiggy events and actions that failed temporarily.

Run every minute from cron or your scheduler:
    * * * * *  cd /srv/app && .venv/bin/python manage.py run_delivery_jobs
"""
from django.core.management.base import BaseCommand

from integrations.delivery.service import run_jobs


class Command(BaseCommand):
    help = 'Retry failed Zomato / Swiggy webhook events and platform actions that are due.'

    def handle(self, *args, **options):
        done = run_jobs()
        self.stdout.write(f'Retried {done["events"]} event(s) and {done["actions"]} action(s).')
