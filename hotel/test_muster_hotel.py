import json
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from restaurant_admin.models import (
    Department,
    ShiftType,
    StaffMember,
    AttendanceStatus,
    DailyAttendance,
    DailyMusterLock,
    MonthlyPayroll,
)


class HotelMusterRollTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Admin User
        self.user = User.objects.create_superuser(
            username='hotel_mgr',
            email='mgr@hotel.com',
            password='Password123!',
            is_staff=True,
            is_superuser=True
        )

        # Restaurant & Hotel-enabled Branch
        self.restaurant = Restaurant.objects.create(
            name="Grand Hotel Resort",
            slug="grand-hotel",
            is_active=True
        )
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="City Center Hotel",
            slug="city-center-hotel",
            has_hotel_module=True
        )

        self.profile = RestaurantUser.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.GENERAL_MANAGER,
            is_primary_admin=True
        )

        self.client.force_login(self.user)

        # Create Hotel Staff Members
        self.staff_hk = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            employee_id="HK-001",
            name="Ramesh Housekeeper",
            department=Department.HOUSEKEEPING,
            designation="Room Attendant",
            monthly_base_salary=Decimal('18000.00'),
            default_shift=ShiftType.MORNING,
            standard_shift_hours=Decimal('8.5'),
            is_active=True
        )
        self.staff_fd = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            employee_id="FD-001",
            name="Sunita Reception",
            department=Department.FRONT_DESK,
            designation="Front Desk Executive",
            monthly_base_salary=Decimal('25000.00'),
            default_shift=ShiftType.GENERAL,
            standard_shift_hours=Decimal('9.0'),
            is_active=True
        )

        self.today = date.today()
        self.today_str = self.today.strftime('%Y-%m-%d')

    def test_hotel_muster_roll_page_loads(self):
        url = reverse('hotel_admin:muster_roll', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hotel Daily Muster Roll")
        self.assertContains(response, "Ramesh Housekeeper")
        self.assertContains(response, "Sunita Reception")

    def test_save_single_attendance_with_calculated_ot(self):
        url = reverse('hotel_admin:api_save_single_attendance', kwargs={'restaurant_slug': self.restaurant.slug})
        payload = {
            'staff_id': self.staff_hk.id,
            'date': self.today_str,
            'status': AttendanceStatus.PRESENT,
            'shift_type': ShiftType.MORNING,
            'check_in_time': '07:00',
            'check_out_time': '18:30',  # 11.5 hrs worked (std 8.5) -> 3.0 hrs OT
            'ot_hours': 3.0,
            'penalty_amount': 0.0,
            'notes': 'Deep room cleaning completed'
        }
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['hours_worked'], 11.5)
        self.assertEqual(data['suggested_ot'], 3.0)
        self.assertEqual(data['ot_hours'], 3.0)

        # Verify record in database
        att = DailyAttendance.objects.get(staff_member=self.staff_hk, date=self.today)
        self.assertEqual(att.status, AttendanceStatus.PRESENT)
        self.assertEqual(att.ot_hours, Decimal('3.0'))

    def test_toggle_muster_lock(self):
        url = reverse('hotel_admin:api_toggle_muster_lock', kwargs={'restaurant_slug': self.restaurant.slug})
        
        # 1. Lock day
        payload = {'date': self.today_str, 'reason': 'Audit confirmed'}
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_locked'])

        # Check DB
        lock = DailyMusterLock.objects.get(restaurant=self.restaurant, branch=self.branch, date=self.today)
        self.assertTrue(lock.is_locked)

        # 2. Saving attendance should now be blocked (HTTP 403)
        save_url = reverse('hotel_admin:api_save_single_attendance', kwargs={'restaurant_slug': self.restaurant.slug})
        save_payload = {
            'staff_id': self.staff_hk.id,
            'date': self.today_str,
            'status': AttendanceStatus.ABSENT,
        }
        res_blocked = self.client.post(save_url, data=json.dumps(save_payload), content_type='application/json')
        self.assertEqual(res_blocked.status_code, 403)

        # 3. Unlock day
        res_unlock = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res_unlock.status_code, 200)
        self.assertFalse(res_unlock.json()['is_locked'])

    def test_bulk_actions(self):
        bulk_url = reverse('hotel_admin:api_bulk_muster_action', kwargs={'restaurant_slug': self.restaurant.slug})

        # 1. mark_unmarked_absent
        payload = {
            'action': 'mark_unmarked_absent',
            'date': self.today_str,
            'department': 'ALL'
        }
        res = self.client.post(bulk_url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 2)

        # Both should now be marked ABSENT
        self.assertEqual(DailyAttendance.objects.filter(date=self.today, status=AttendanceStatus.ABSENT).count(), 2)

    def test_mark_all_present_with_shift_times(self):
        mark_url = reverse('hotel_admin:api_mark_all_present', kwargs={'restaurant_slug': self.restaurant.slug})
        payload = {
            'date': self.today_str,
            'department': 'ALL',
            'fill_shift_times': True
        }
        res = self.client.post(mark_url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 2)

        # HK-001 has MORNING shift (07:00 - 15:30)
        att_hk = DailyAttendance.objects.get(staff_member=self.staff_hk, date=self.today)
        self.assertEqual(att_hk.status, AttendanceStatus.PRESENT)
        self.assertEqual(att_hk.check_in_time.strftime('%H:%M'), '07:00')
        self.assertEqual(att_hk.check_out_time.strftime('%H:%M'), '15:30')

