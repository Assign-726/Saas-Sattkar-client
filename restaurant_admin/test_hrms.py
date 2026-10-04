import json
import calendar
from datetime import date, datetime, timedelta
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
    MonthlyPayroll,
    SOPShiftTiming,
    DailySOPTask,
    DailyTaskLog,
)
from restaurant_admin.views_hrms import seed_sample_hospitality_staff, seed_standard_hospitality_sops


class HRMSOperationsTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Admin User
        self.user = User.objects.create_superuser(
            username='admin_test',
            email='admin@test.com',
            password='Password123!',
            is_staff=True,
            is_superuser=True
        )

        # Create Restaurant & Branch
        self.restaurant = Restaurant.objects.create(
            name="The Grand Palace Hotel",
            slug="grand-palace",
            is_active=True
        )
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Main Branch",
            slug="main-branch"
        )

        # Create Restaurant Profile
        self.profile = RestaurantUser.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            branch=self.branch,
            role=RestaurantUser.Role.GENERAL_MANAGER,
            is_primary_admin=True
        )

        self.client.force_login(self.user)

    def test_seed_sample_hospitality_staff(self):
        """Test that automatic hospitality ground staff seeding is disabled."""
        count = seed_sample_hospitality_staff(self.restaurant, self.branch)
        self.assertEqual(count, 0)

    def test_mark_all_present_batch_endpoint(self):
        """Test Phase 1: Mark All Present batch creation endpoint."""
        StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Chef Test",
            employee_id="TST-001",
            department=Department.KITCHEN,
            designation="Chef",
            default_shift=ShiftType.GENERAL,
            monthly_base_salary=Decimal('20000.00'),
            standard_shift_hours=Decimal('9.0'),
            is_active=True
        )
        total_staff = StaffMember.objects.filter(restaurant=self.restaurant).count()

        target_date = "2026-10-15"
        url = reverse('restaurant_admin:api_mark_all_present', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.post(
            url,
            data=json.dumps({'date': target_date, 'department': 'ALL'}),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], total_staff)

        # Verify all records created with PRESENT
        attendances = DailyAttendance.objects.filter(restaurant=self.restaurant, date=target_date)
        self.assertEqual(attendances.count(), total_staff)
        for att in attendances:
            self.assertEqual(att.status, AttendanceStatus.PRESENT)

    def test_dynamic_single_attendance_save(self):
        """Test Phase 1: Single attendance Alpine.js auto-save endpoint."""
        staff = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Vikram Singh",
            employee_id="EMP-999",
            department=Department.BAR,
            designation="Mixologist",
            monthly_base_salary=Decimal('24000.00'),
            standard_shift_hours=Decimal('9.0'),
            default_shift=ShiftType.EVENING
        )

        target_date = "2026-10-15"
        url = reverse('restaurant_admin:api_save_single_attendance', kwargs={'restaurant_slug': self.restaurant.slug})
        payload = {
            'staff_id': staff.id,
            'date': target_date,
            'status': AttendanceStatus.PRESENT,
            'shift_type': ShiftType.SPLIT,
            'check_in_time': '11:00',
            'check_out_time': '23:00',
            'ot_hours': 2.5,
            'penalty_amount': 150.0,
            'notes': 'Split shift for dinner banquet'
        }

        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['ot_hours'], 2.5)
        self.assertEqual(data['penalty_amount'], 150.0)

        # Check DB
        att = DailyAttendance.objects.get(staff_member=staff, date=target_date)
        self.assertEqual(att.shift_type, ShiftType.SPLIT)
        self.assertEqual(att.ot_hours, Decimal('2.5'))
        self.assertEqual(att.penalty_amount, Decimal('150.00'))

    def test_payroll_math_formulas(self):
        """
        Test Phase 2: Payroll calculation mathematical correctness.
        Formulas:
        Payable Days = Present Days + Weekly Offs + Paid Leaves + (0.5 * Half Days)
        Daily Wage = Monthly Base Salary / Days in Month
        Base Earned = Daily Wage * Payable Days
        Hourly OT Rate = Daily Wage / Standard Shift Hours
        OT Amount = Total OT Hours * Hourly OT Rate
        Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
        """
        staff = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Chef Anand",
            employee_id="EMP-CHEF",
            department=Department.KITCHEN,
            designation="Executive Chef",
            monthly_base_salary=Decimal('30000.00'),
            standard_shift_hours=Decimal('10.0')  # 10h shift
        )

        # Days in month: 30
        payroll = MonthlyPayroll.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            staff_member=staff,
            year=2026,
            month=9,  # September has 30 days
            days_in_month=30,
            monthly_base_salary=Decimal('30000.00'),
            standard_shift_hours=Decimal('10.0'),
            present_days=Decimal('22.0'),
            weekly_offs=Decimal('4.0'),
            paid_leaves=Decimal('2.0'),
            half_days=2,      # 2 half days = 1 payable day
            absent_days=Decimal('1.0'),
            total_ot_hours=Decimal('15.0'),
            penalties=Decimal('500.00'),
            incentives=Decimal('1200.00')
        )

        payroll.calculate_values()
        payroll.save()

        # Expected calculations:
        # Payable Days = 22 + 4 + 2 + (0.5 * 2) = 29.00
        self.assertEqual(payroll.payable_days, Decimal('29.00'))

        # Daily Wage = 30000 / 30 = 1000.00
        self.assertEqual(payroll.daily_wage, Decimal('1000.00'))

        # Base Earned = 1000.00 * 29.00 = 29000.00
        self.assertEqual(payroll.base_earned, Decimal('29000.00'))

        # Hourly OT Rate = 1000.00 / 10.0 = 100.00
        self.assertEqual(payroll.hourly_ot_rate, Decimal('100.00'))

        # OT Amount = 15.0 * 100.00 = 1500.00
        self.assertEqual(payroll.ot_amount, Decimal('1500.00'))

        # Net Salary Payable = Base Earned (29000.00) + OT Amount (1500.00) - Penalties (500.00) + Incentives (1200.00)
        # = 31200.00
        self.assertEqual(payroll.net_salary, Decimal('31200.00'))

    def test_one_click_monthly_payroll_generation(self):
        """Test Phase 2: api_generate_monthly_payroll aggregates daily attendance."""
        staff = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Rahul Steward",
            employee_id="EMP-STW",
            department=Department.FLOOR,
            designation="Steward",
            monthly_base_salary=Decimal('18000.00'),
            standard_shift_hours=Decimal('9.0')
        )

        year = 2026
        month = 10  # October has 31 days

        # Log 10 present days with 1 hour OT each
        for day in range(1, 11):
            DailyAttendance.objects.create(
                restaurant=self.restaurant,
                branch=self.branch,
                staff_member=staff,
                date=date(year, month, day),
                status=AttendanceStatus.PRESENT,
                ot_hours=Decimal('1.0')
            )

        # Log 2 weekly offs
        DailyAttendance.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            staff_member=staff,
            date=date(year, month, 11),
            status=AttendanceStatus.WEEKLY_OFF
        )
        DailyAttendance.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            staff_member=staff,
            date=date(year, month, 12),
            status=AttendanceStatus.WEEKLY_OFF
        )

        # Trigger generation endpoint
        url = reverse('restaurant_admin:api_generate_monthly_payroll', kwargs={'restaurant_slug': self.restaurant.slug})
        response = self.client.post(
            url,
            data=json.dumps({'year': year, 'month': month}),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])

        # Verify MonthlyPayroll record
        p = MonthlyPayroll.objects.get(staff_member=staff, year=year, month=month)
        self.assertEqual(p.present_days, Decimal('10.0'))
        self.assertEqual(p.weekly_offs, Decimal('2.0'))
        self.assertEqual(p.payable_days, Decimal('12.00'))
        self.assertEqual(p.total_ot_hours, Decimal('10.0'))
        self.assertGreater(p.base_earned, Decimal('0.00'))
        self.assertGreater(p.net_salary, Decimal('0.00'))

    def test_salary_slip_view_rendering(self):
        """Test Phase 2: Salary slip printable page renders."""
        staff = StaffMember.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Kavita Housekeeper",
            employee_id="EMP-HK01",
            department=Department.HOUSEKEEPING,
            designation="Room Attendant",
            monthly_base_salary=Decimal('16000.00')
        )
        payroll = MonthlyPayroll.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            staff_member=staff,
            year=2026,
            month=10,
            days_in_month=31,
            monthly_base_salary=Decimal('16000.00'),
            present_days=Decimal('26.0'),
            weekly_offs=Decimal('4.0'),
            paid_leaves=Decimal('1.0'),
            payable_days=Decimal('31.00')
        )
        payroll.calculate_values()
        payroll.save()

        url = reverse('restaurant_admin:salary_slip_view', kwargs={'restaurant_slug': self.restaurant.slug, 'pk': payroll.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kavita Housekeeper")
        self.assertContains(response, "Confidential Pay Slip")
        self.assertContains(response, "Net Salary Payable")

    def test_daily_sop_board_and_task_verification(self):
        """Test Phase 3: SOP seeding and task verification."""
        # Pre-seed SOP templates
        count = seed_standard_hospitality_sops(self.restaurant, self.branch)
        self.assertEqual(count, 2)

        # Retrieve a freezer temp task
        task = DailySOPTask.objects.filter(
            restaurant=self.restaurant,
            requires_numeric_reading=True
        ).first()
        self.assertIsNotNone(task)

        target_date = "2026-10-15"
        url = reverse('restaurant_admin:api_toggle_sop_task_status', kwargs={'restaurant_slug': self.restaurant.slug})

        # Manager completes task with temperature reading
        payload = {
            'task_id': task.id,
            'date': target_date,
            'status': DailyTaskLog.TaskStatus.COMPLETED,
            'numeric_reading': '-19.2',
            'verifier_notes': 'Deep freezer temp steady below -18C'
        }
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], DailyTaskLog.TaskStatus.COMPLETED)
        self.assertEqual(data['numeric_reading'], '-19.2')

        # Check DB log
        log = DailyTaskLog.objects.get(sop_task=task, date=target_date)
        self.assertEqual(log.status, DailyTaskLog.TaskStatus.COMPLETED)
        self.assertEqual(log.completed_by, self.user)
        self.assertEqual(log.numeric_reading, '-19.2')
