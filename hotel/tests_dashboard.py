import datetime
from decimal import Decimal

from django.utils import timezone

from hotel import dashboard as D
from hotel.models import ComplianceLicence, RoomBooking
from hotel.tests_ops import Base
from super_admin.models import CustomRole


class HotelDashboardTests(Base):
    def test_figures_match_reports_and_compare_with_previous_period(self):
        # 2 of 4 rooms sold last night (previous day) vs 1 of 4 the night before
        y = self.today - datetime.timedelta(days=1)
        for room in (0, 1):
            RoomBooking.objects.create(room=self.rooms[room], branch=self.b, guest_name='A', guest_email='a@x.com', guest_phone='9876543210',
                                       check_in_date=y, check_out_date=self.today, booking_status='CHECKED_OUT')
        RoomBooking.objects.create(room=self.rooms[2], branch=self.b, guest_name='B', guest_email='b@x.com', guest_phone='9876543211',
                                   check_in_date=y - datetime.timedelta(days=1), check_out_date=y, booking_status='CHECKED_OUT')
        d = D.compute(self.b, y, y)
        self.assertEqual(d['cur']['sold'], 2)
        self.assertEqual(d['cur']['occupancy'], 50.0)
        self.assertEqual(d['prev']['occupancy'], 25.0)
        self.assertEqual(d['deltas']['occupancy'], 25.0)
        self.assertEqual(d['cur']['adr'], Decimal('4000.00'))
        self.assertEqual(len(d['outlook']), 14)

    def test_live_snapshot(self):
        self.stay(room=0)                                   # in house
        self.stay(room=1, status='RESERVED')                # arriving today
        live = D.live(self.b)
        self.assertEqual(live['in_house'], 1)
        self.assertEqual(live['arrivals_due'], 1)
        self.assertEqual(live['tonight'], 2)
        self.assertEqual(live['tonight_pct'], 50)
        self.assertGreater(live['balance_due'], 0)

    def test_page_renders_every_period_and_exports(self):
        self.stay()
        for q in ['', '?period=today', '?period=30d', '?period=next_30', '?period=last_month',
                  '?period=custom&from=2026-01-01&to=2026-01-31', '?period=custom&from=bad', '?period=nope']:
            r = self.client.get(self.url('management_dashboard') + q)
            self.assertEqual(r.status_code, 200, q)
        r = self.client.get(self.url('management_dashboard') + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')

    def test_needs_attention_shows_on_dashboard_and_overview(self):
        ComplianceLicence.objects.create(branch=self.b, name='Fire NOC', expires_on=self.today + datetime.timedelta(days=5))
        r = self.client.get(self.url('management_dashboard'))
        self.assertContains(r, 'Licences expired or expiring within 30 days')
        r = self.client.get(self.url('dashboard'))
        self.assertContains(r, 'ov-attn')
        self.assertContains(r, 'Licences expired or expiring within 30 days')

    def test_sidebar_and_access_follow_the_revenue_area(self):
        r = self.client.get(self.url('dashboard'))
        self.assertContains(r, self.url('management_dashboard'))
        self.assertNotContains(r, '<span>Action Centre</span>', html=False)
        role = CustomRole.objects.create(restaurant=self.r, name='Desk only', permissions={'front_desk': 'manage'})
        profile = self.user.restaurant_profile
        profile.custom_role = role
        profile.save()
        r = self.client.get(self.url('dashboard'))
        self.assertNotContains(r, self.url('management_dashboard'))
        r = self.client.get(self.url('management_dashboard'))
        self.assertNotEqual(r.status_code, 200)
        # the action centre itself stays reachable for every role (it filters by area)
        self.assertEqual(self.client.get(self.url('action_centre')).status_code, 200)
