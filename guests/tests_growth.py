from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests import growth, services
from guests.brief import compute_brief
from guests.models import Campaign, Feedback, Guest, MarketingSettings
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser


class Base(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Royal Palace', slug='royal-palace')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main')
        self.gm = User.objects.create_user('gm', 'gm@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.gm, restaurant=self.r, role='GENERAL_MANAGER')
        self.cashier = User.objects.create_user('cash', 'c@example.com', 'x', is_staff=True)
        RestaurantUser.objects.create(user=self.cashier, restaurant=self.r, role='CASHIER')

    def url(self, name, **kw):
        return reverse(f'restaurant_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})


class FeedbackTests(Base):
    def public_url(self):
        return reverse('guest_feedback_public', kwargs={'restaurant_slug': self.r.slug, 'branch_id': self.b.id})

    def test_public_form_creates_ticket_and_links_guest(self):
        self.assertEqual(self.client.get(self.public_url()).status_code, 200)
        resp = self.client.post(self.public_url(), {'rating': '2', 'food': '1', 'recommend': '3', 'comment': 'Cold food',
                                                    'name': 'Anita', 'phone': '9876543210'})
        self.assertContains(resp, 'manager will get in touch')
        fb = Feedback.objects.get()
        self.assertEqual((fb.rating, fb.food, fb.branch), (2, 1, self.b))
        self.assertEqual(fb.guest.phone, '9876543210')
        self.assertTrue(fb.is_complaint)

    def test_public_form_validation_honeypot_and_throttle(self):
        self.assertEqual(self.client.post(self.public_url(), {'comment': 'no stars'}).status_code, 400)
        self.client.post(self.public_url(), {'rating': '5', 'website': 'spam.example'})
        self.assertFalse(Feedback.objects.exists())
        for _ in range(9):
            last = self.client.post(self.public_url(), {'rating': '5'})
        self.assertEqual(last.status_code, 429)

    def test_other_business_branch_404(self):
        other = Restaurant.objects.create(name='Other', slug='other')
        url = reverse('guest_feedback_public', kwargs={'restaurant_slug': other.slug, 'branch_id': self.b.id})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_desk_workflow_and_make_good(self):
        fb = growth.submit_feedback(self.r, self.b, {'rating': 1, 'name': 'Ravi', 'phone': '9000000001'})
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(self.url('feedback_list')).status_code, 200)
        detail = self.url('feedback_detail', pk=fb.pk)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.client.post(detail, {'op': 'note', 'text': 'Called and apologised'})
        fb.refresh_from_db()
        self.assertEqual(fb.status, 'IN_PROGRESS')
        self.assertEqual(self.client.post(detail, {'op': 'gift_card', 'value': '500'}).status_code, 403)
        self.client.force_login(self.gm)
        self.client.post(detail, {'op': 'gift_card', 'value': '500'})
        self.assertEqual(fb.guest.gift_cards.count(), 1)
        self.client.post(detail, {'op': 'status', 'status': 'RESOLVED', 'note': 'Comped dessert'})
        fb.refresh_from_db()
        self.assertEqual(fb.status, 'RESOLVED')
        self.assertIsNotNone(fb.resolved_at)
        stats = growth.feedback_stats(Feedback.objects.all())
        self.assertEqual(stats['open_complaints'], 0)

    def test_brief_flags_open_complaints(self):
        growth.submit_feedback(self.r, self.b, {'rating': 1})
        alerts = compute_brief(self.r)['alerts']
        self.assertTrue(any('unhappy guest' in a['title'] for a in alerts))

    def test_nps(self):
        for score in (10, 9, 8, 3):
            growth.submit_feedback(self.r, self.b, {'rating': 4, 'recommend': score})
        self.assertEqual(growth.feedback_stats(Feedback.objects.all())['nps'], 25)


class CampaignTests(Base):
    def setUp(self):
        super().setUp()
        self.anita = services.upsert_guest(self.r, 'Anita Sharma', '9876543210', 'anita@example.com')
        services.adjust_points(self.anita, 120, 'Welcome', None)
        self.ravi = services.upsert_guest(self.r, 'Ravi', '9000000001', 'ravi@example.com')
        self.ravi.marketing_opt_in = False
        self.ravi.save()
        self.client.force_login(self.gm)

    def test_create_whatsapp_campaign_respects_opt_out_and_personalises(self):
        resp = self.client.post(self.url('campaign_new'), {'name': 'Weekend', 'channel': 'whatsapp', 'segment': '',
                                                           'message': 'Hi {first_name}, you have {points} points at {business}. Code {offer_code}', 'offer_code': 'WKND'})
        c = Campaign.objects.get()
        self.assertRedirects(resp, self.url('campaign_detail', pk=c.pk), fetch_redirect_response=False)
        self.assertEqual(c.audience_count, 1)
        r = c.recipients.get()
        self.assertEqual(r.message, 'Hi Anita, you have 120 points at Royal Palace. Code WKND')
        page = self.client.get(self.url('campaign_detail', pk=c.pk)).content.decode()
        self.assertIn('https://wa.me/919876543210?text=', page)
        self.client.post(self.url('campaign_detail', pk=c.pk), {'op': 'mark_sent', 'one': r.pk}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        c.refresh_from_db()
        self.assertEqual((c.sent_count, c.status), (1, 'SENT'))

    def test_email_campaign_sends_with_unsubscribe(self):
        self.client.post(self.url('campaign_new'), {'name': 'News', 'channel': 'email', 'segment': '', 'subject': 'Hello {first_name}', 'message': 'Hi {name}'})
        c = Campaign.objects.get()
        with mock.patch('core.emails.get_mail_connection_and_sender', return_value=(None, 'noreply@example.com', None)):
            self.client.post(self.url('campaign_detail', pk=c.pk), {'op': 'send_email'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Hello Anita')
        unsub = mail.outbox[0].extra_headers['List-Unsubscribe'].strip('<>')
        path = unsub.split('testserver', 1)[-1]
        self.assertEqual(self.client.get(path).status_code, 200)
        self.client.post(path)
        self.anita.refresh_from_db()
        self.assertFalse(self.anita.marketing_opt_in)

    def test_cashier_cannot_run_campaigns(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(self.url('campaign_list')).status_code, 403)

    def test_birthday_automation_runs_once_a_day(self):
        today = timezone.localdate()
        self.anita.birthday = today.replace(year=1990)
        self.anita.save()
        ms = MarketingSettings.for_restaurant(self.r)
        ms.birthday_enabled = True
        ms.save()
        with mock.patch('core.emails.get_mail_connection_and_sender', return_value=(None, 'noreply@example.com', None)):
            growth.run_automations(self.r, 'https://admin.example.com')
            growth.run_automations(self.r, 'https://admin.example.com')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Happy birthday, Anita', mail.outbox[0].body)
