"""Club & Events in the Restaurant admin and the Hotel admin (same views, two shells)."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import Table, Zone
from events.models import ClubEvent, EventBooking, TicketType
from super_admin.models import CustomRole, Restaurant, RestaurantBranch, RestaurantUser

R = '/dashboard/spice-main/club-events/'
H = '/hotel-admin/spice-main/club-events/'


def local(dt):
    return timezone.localtime(dt).strftime('%Y-%m-%dT%H:%M')


class ClubEventsTests(TestCase):
    def setUp(self):
        self.r = Restaurant.objects.create(name='Spice', slug='spice-co')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Main', slug='spice-main',
                                                 has_club_module=True, has_hotel_module=True, has_restaurant_module=True)
        self.b2 = RestaurantBranch.objects.create(restaurant=self.r, name='Annex', slug='spice-annex', has_club_module=True)
        self.owner = User.objects.create_user('owner', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.owner, restaurant=self.r, branch=self.b, role='GENERAL_MANAGER')
        start = timezone.now() + timedelta(days=2)
        self.ev = ClubEvent.objects.create(branch=self.b, title='Neon Night', start_datetime=start, end_datetime=start + timedelta(hours=5),
                                           is_published=True)
        self.ga = TicketType.objects.create(event=self.ev, name='General', base_price=Decimal('500'), total_allocated=10)
        self.vip = TicketType.objects.create(event=self.ev, name='VIP', base_price=Decimal('1500'), total_allocated=2)
        self.client.force_login(self.owner)

    def book(self, tier=None, qty=1, **kw):
        tier = tier or self.ga
        b = EventBooking.objects.create(booking_reference=EventBooking.generate_booking_reference(), event=tier.event, ticket_type=tier,
                                        guest_name=kw.pop('name', 'Asha'), guest_email='', guest_phone='9876543210', quantity=qty,
                                        unit_price=tier.base_price, total_amount=tier.base_price * qty,
                                        payment_status=EventBooking.PaymentStatus.SUCCESS, **kw)
        tier.quantity_sold += qty
        tier.save()
        return b

    def test_every_screen_opens_in_both_admins(self):
        self.book()
        Table.objects.create(restaurant=self.r, branch=self.b, zone=Zone.objects.create(restaurant=self.r, branch=self.b, name='Deck'),
                             name='T1', table_number=1, capacity=4)
        for base in (R, H):
            for page in ('', 'create/', f'{self.ev.pk}/edit/', 'guestlist/', f'{self.ev.pk}/guestlist/', 'ticket-grid/',
                         f'{self.ev.pk}/ticket-grid/', f'{self.ev.pk}/ticket-grid/api/', 'floor-plan/', f'{self.ev.pk}/floor-plan/',
                         f'{self.ev.pk}/floor-plan/api/', 'gate-scanner/', f'{self.ev.pk}/gate-scanner/', f'{self.ev.pk}/guestlist/export/'):
                resp = self.client.get(base + page)
                self.assertEqual(resp.status_code, 200, base + page)
        hotel = self.client.get(H)
        self.assertContains(hotel, 'href="/hotel-admin/spice-main/club-events/create/"')
        self.assertContains(hotel, 'Events &amp; Tickets')  # hotel sidebar entry
        self.assertTemplateUsed(hotel, 'hotel_admin/base_hotel.html')
        self.assertTemplateUsed(self.client.get(R), 'restaurant_admin/base.html')

    def test_empty_branch_shows_empty_states_not_redirects(self):
        ClubEvent.objects.all().delete()
        for base in (R, H):
            for page in ('', 'guestlist/', 'ticket-grid/', 'floor-plan/', 'gate-scanner/'):
                self.assertEqual(self.client.get(base + page).status_code, 200, base + page)

    def test_create_validates_and_keeps_input(self):
        start = timezone.now() + timedelta(days=5)
        data = {'title': 'Retro Night', 'start_datetime': local(start), 'end_datetime': local(start - timedelta(hours=1)),
                'tier_id[]': [''], 'tier_name[]': ['Entry'], 'tier_price[]': ['300'], 'tier_capacity[]': ['0'], 'tier_limit[]': ['4'],
                'tier_description[]': ['']}
        resp = self.client.post(H + 'create/', data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'must end after it starts')
        self.assertContains(resp, 'total tickets must be')
        self.assertContains(resp, 'value="Retro Night"')
        self.assertFalse(ClubEvent.objects.filter(title='Retro Night').exists())

        data.update({'end_datetime': local(start + timedelta(hours=4)), 'tier_capacity[]': ['80'], 'is_published': 'on'})
        resp = self.client.post(H + 'create/', data)
        self.assertRedirects(resp, H, fetch_redirect_response=False)
        ev = ClubEvent.objects.get(title='Retro Night')
        self.assertEqual((ev.branch, ev.is_published), (self.b, True))
        self.assertEqual(list(ev.ticket_types.values_list('name', 'total_allocated', 'max_per_order')), [('Entry', 80, 4)])

    def test_edit_cannot_drop_below_sold_and_hides_removed_tiers_with_sales(self):
        self.book(self.vip, 2)
        base = {'title': 'Neon Night', 'start_datetime': local(self.ev.start_datetime), 'end_datetime': local(self.ev.end_datetime),
                'tier_id[]': [str(self.ga.pk), str(self.vip.pk)], 'tier_name[]': ['General', 'VIP'], 'tier_price[]': ['500', '1500'],
                'tier_capacity[]': ['10', '1'], 'tier_limit[]': ['10', '2'], 'tier_description[]': ['', '']}
        resp = self.client.post(R + f'{self.ev.pk}/edit/', base)
        self.assertContains(resp, 'can’t go below that')
        # Remove the VIP tier: it has sales, so it is hidden rather than deleted
        base.update({'tier_id[]': [str(self.ga.pk)], 'tier_name[]': ['General'], 'tier_price[]': ['600'], 'tier_capacity[]': ['12'],
                     'tier_limit[]': ['6'], 'tier_description[]': ['']})
        self.client.post(R + f'{self.ev.pk}/edit/', base)
        self.vip.refresh_from_db()
        self.ga.refresh_from_db()
        self.assertFalse(self.vip.is_active)
        self.assertEqual((self.ga.base_price, self.ga.total_allocated, self.ga.max_per_order), (Decimal('600.00'), 12, 6))

    def test_duplicate_copies_a_week_later_as_draft(self):
        self.book()
        resp = self.client.post(R + f'{self.ev.pk}/duplicate/')
        copy = ClubEvent.objects.exclude(pk=self.ev.pk).get()
        self.assertRedirects(resp, R + f'{copy.pk}/edit/', fetch_redirect_response=False)
        self.assertFalse(copy.is_published)
        self.assertEqual(copy.start_datetime - self.ev.start_datetime, timedelta(days=7))
        self.assertEqual(list(copy.ticket_types.values_list('name', 'quantity_sold')), [('General', 0), ('VIP', 0)])
        self.assertEqual(self.client.get(R + f'{self.ev.pk}/duplicate/').status_code, 405)

    def test_door_pass_validation_capacity_and_redirect_back(self):
        url = H + f'{self.ev.pk}/manual-booking/'
        bad = self.client.post(url, {'ticket_type_id': self.vip.pk, 'guest_name': 'Ravi', 'guest_phone': '123', 'quantity': 1})
        self.assertRedirects(bad, H + f'{self.ev.pk}/guestlist/', fetch_redirect_response=False)
        self.assertFalse(EventBooking.objects.exists())
        self.client.post(url, {'ticket_type_id': self.vip.pk, 'guest_name': 'Ravi', 'guest_phone': '98765 43210', 'quantity': 3})
        self.assertFalse(EventBooking.objects.exists())  # only 2 VIP tickets
        resp = self.client.post(url, {'ticket_type_id': self.vip.pk, 'guest_name': 'Ravi', 'guest_phone': '98765 43210', 'quantity': 2,
                                      'payment_method': 'COMPLIMENTARY', 'next': H + f'{self.ev.pk}/ticket-grid/'})
        self.assertRedirects(resp, H + f'{self.ev.pk}/ticket-grid/', fetch_redirect_response=False)
        b = EventBooking.objects.get()
        self.vip.refresh_from_db()
        self.assertEqual((b.total_amount, b.guest_email, self.vip.quantity_sold), (Decimal('0.00'), '', 2))

    def test_checkin_is_post_only_and_answers_json(self):
        b = self.book()
        url = R + f'booking/{b.pk}/toggle-checkin/'
        self.assertEqual(self.client.get(url).status_code, 405)
        d = self.client.post(url, HTTP_ACCEPT='application/json').json()
        self.assertTrue(d['success'] and d['checked_in'])
        self.assertFalse(self.client.post(url, HTTP_ACCEPT='application/json').json()['checked_in'])

    def test_cancel_releases_tickets_and_clears_table(self):
        t = Table.objects.create(restaurant=self.r, branch=self.b, name='T1', table_number=1, capacity=4)
        b = self.book(self.vip, 2, assigned_table=t)
        self.client.post(R + f'booking/{b.pk}/cancel/', {'reason': 'Guest asked'})
        b.refresh_from_db()
        self.vip.refresh_from_db()
        self.assertEqual((b.payment_status, b.assigned_table, self.vip.quantity_sold), ('CANCELLED', None, 0))
        self.assertIn('Guest asked', b.notes)
        b2 = self.book(checked_in=True)
        self.client.post(R + f'booking/{b2.pk}/cancel/')
        b2.refresh_from_db()
        self.assertEqual(b2.payment_status, 'SUCCESS')  # already inside: can't cancel

    def test_guestlist_filters_and_csv_export(self):
        self.book(name='Asha')
        self.book(self.vip, name='Kabir', checked_in=True)
        resp = self.client.get(R + f'{self.ev.pk}/guestlist/', {'checked_in': '1'})
        self.assertContains(resp, 'Kabir')
        self.assertNotContains(resp, '>Asha<')
        body = self.client.get(R + f'{self.ev.pk}/guestlist/export/').content.decode('utf-8-sig')
        self.assertTrue(body.startswith('Booking ref,Guest,Phone'))
        self.assertIn('Kabir', body)
        self.assertIn('Asha', body)

    def test_scanner_refuses_pass_for_another_event(self):
        other = ClubEvent.objects.create(branch=self.b, title='Jazz', start_datetime=self.ev.start_datetime + timedelta(days=7),
                                         end_datetime=self.ev.end_datetime + timedelta(days=7))
        tier = TicketType.objects.create(event=other, name='Entry', base_price=Decimal('100'), total_allocated=5)
        b = self.book(tier)
        url = '/api/events/admin/events/scan-pass/'
        resp = self.client.post(url, json.dumps({'qr_payload': b.booking_reference, 'event_id': self.ev.pk, 'branch_id': self.b.pk}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.json()['wrong_event'])
        b.refresh_from_db()
        self.assertFalse(b.checked_in)
        ok = self.client.post(url, json.dumps({'qr_payload': b.booking_reference, 'event_id': other.pk, 'branch_id': self.b.pk}),
                              content_type='application/json')
        self.assertTrue(ok.json()['valid'])

    def test_events_of_another_branch_are_out_of_reach(self):
        start = timezone.now() + timedelta(days=1)
        annex = ClubEvent.objects.create(branch=self.b2, title='Annex Party', start_datetime=start, end_datetime=start + timedelta(hours=3))
        for page in (f'{annex.pk}/edit/', f'{annex.pk}/guestlist/', f'{annex.pk}/ticket-grid/api/'):
            self.assertEqual(self.client.get(R + page).status_code, 404, page)
        self.assertEqual(self.client.post(R + f'{annex.pk}/delete/').status_code, 404)
        self.assertNotContains(self.client.get(R + '?tab=all'), 'Annex Party')

    def test_table_assignment_conflict_and_swap(self):
        t = Table.objects.create(restaurant=self.r, branch=self.b, name='VIP 1', table_number=1, capacity=4)
        a, c = self.book(self.vip, name='Asha'), self.book(self.vip, name='Kabir')
        url = H + f'{self.ev.pk}/floor-plan/assign/'
        post = lambda body: self.client.post(url, json.dumps(body), content_type='application/json')
        self.assertTrue(post({'booking_id': a.pk, 'table_id': t.pk}).json()['success'])
        self.assertEqual(post({'booking_id': c.pk, 'table_id': t.pk}).status_code, 409)
        self.assertTrue(post({'booking_id': c.pk, 'table_id': t.pk, 'force_swap': True}).json()['success'])
        a.refresh_from_db()
        self.assertIsNone(a.assigned_table)
        data = self.client.get(H + f'{self.ev.pk}/floor-plan/api/').json()
        self.assertEqual(data['tables'][0]['booking']['guest_name'], 'Kabir')
        self.assertEqual([u['guest_name'] for u in data['unassigned']], ['Asha'])

    def test_module_off_shows_upgrade_page(self):
        self.b.has_club_module = False
        self.b.save()
        for base in (R, H):
            resp = self.client.get(base)
            self.assertContains(resp, 'enabled for Main')

    def test_view_only_role_can_look_but_not_change(self):
        role = CustomRole.objects.create(restaurant=self.r, name='Door viewer', permissions={'events': 'view', 'front_desk': 'view'})
        prof = self.owner.restaurant_profile
        prof.custom_role = role
        prof.save()
        self.assertEqual(self.client.get(R).status_code, 200)
        self.assertNotEqual(self.client.post(R + f'{self.ev.pk}/duplicate/').status_code, 302)
        self.assertEqual(ClubEvent.objects.count(), 1)
