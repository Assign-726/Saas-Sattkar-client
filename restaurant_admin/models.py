import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.contrib.auth.models import User
from super_admin.models import Restaurant, RestaurantBranch


class Department(models.TextChoices):
    KITCHEN = 'KITCHEN', 'Kitchen & Culinary (BOH)'
    BAR = 'BAR', 'Bar & Beverage'
    FLOOR = 'FLOOR', 'Floor & Service (FOH)'
    HOUSEKEEPING = 'HOUSEKEEPING', 'Housekeeping & Rooms'
    FRONT_DESK = 'FRONT_DESK', 'Front Desk & Reception'
    SECURITY = 'SECURITY', 'Security & Door Staff'
    MANAGEMENT = 'MANAGEMENT', 'Management & Admin'


class ShiftType(models.TextChoices):
    MORNING = 'MORNING', 'Morning Shift (07:00 - 15:30)'
    EVENING = 'EVENING', 'Evening Shift (15:30 - 00:00)'
    NIGHT = 'NIGHT', 'Night Shift (23:00 - 07:30)'
    SPLIT = 'SPLIT', 'Hospitality Split Shift (Lunch & Dinner)'
    GENERAL = 'GENERAL', 'General Shift (09:00 - 18:00)'


class StaffMember(models.Model):
    """
    Ground staff profile (Chefs, line cooks, stewards, bartenders, housekeeping).
    Staff do not require Django User logins; managers execute all actions via bulk grids.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='ground_staff')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='ground_staff')
    employee_id = models.CharField(max_length=50, verbose_name="Staff ID / Code")
    name = models.CharField(max_length=150, verbose_name="Full Name")
    department = models.CharField(max_length=30, choices=Department.choices, default=Department.FLOOR)
    designation = models.CharField(max_length=100, help_text="e.g. Demi Chef De Partie, Head Bartender, Room Attendant, Senior Captain")
    phone = models.CharField(max_length=20, blank=True)
    default_shift = models.CharField(max_length=20, choices=ShiftType.choices, default=ShiftType.GENERAL)
    monthly_base_salary = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('18000.00'),
        help_text="Standard monthly base wage"
    )
    standard_shift_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=Decimal('9.0'),
        help_text="Standard daily shift duration in hours (e.g. 9 or 8)"
    )
    joining_date = models.DateField(default=date.today)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['department', 'name']
        unique_together = ('restaurant', 'employee_id')
        verbose_name = 'Ground Staff Member'
        verbose_name_plural = 'Ground Staff Members'

    def __str__(self):
        return f"{self.name} ({self.employee_id}) - {self.get_department_display()}"

    @property
    def daily_wage_reference(self):
        """Calculates standard reference daily wage based on 30-day month."""
        return (self.monthly_base_salary / Decimal('30.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def hourly_ot_rate_reference(self):
        """Calculates reference hourly overtime pay rate."""
        shift_hrs = self.standard_shift_hours if self.standard_shift_hours > 0 else Decimal('9.0')
        return (self.daily_wage_reference / shift_hrs).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class AttendanceStatus(models.TextChoices):
    PRESENT = 'PRESENT', 'Present (P)'
    ABSENT = 'ABSENT', 'Absent (A)'
    HALF_DAY = 'HALF_DAY', 'Half Day (HD)'
    PAID_LEAVE = 'PAID_LEAVE', 'Paid Leave (PL)'
    WEEKLY_OFF = 'WEEKLY_OFF', 'Weekly Off (WO)'
    UNPAID_LEAVE = 'UNPAID_LEAVE', 'Unpaid Leave (UL)'


class DailyAttendance(models.Model):
    """
    Fast-track muster roll daily attendance record.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='daily_attendances')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='daily_attendances')
    staff_member = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=20, choices=AttendanceStatus.choices, default=AttendanceStatus.PRESENT)
    shift_type = models.CharField(max_length=20, choices=ShiftType.choices, default=ShiftType.GENERAL)
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_time = models.TimeField(null=True, blank=True)
    ot_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=Decimal('0.0'),
        help_text="Overtime hours worked"
    )
    penalty_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Late penalty or disciplinary deduction for the day"
    )
    notes = models.CharField(max_length=255, blank=True)
    marked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('staff_member', 'date')
        ordering = ['date', 'staff_member__department', 'staff_member__name']
        verbose_name = 'Daily Attendance'
        verbose_name_plural = 'Daily Attendances'

    def __str__(self):
        return f"{self.staff_member.name} [{self.date}] - {self.get_status_display()}"


class MonthlyPayroll(models.Model):
    """
    Automated Monthly Payroll Engine Aggregate.
    Formulas:
    Payable Days = Present Days + Weekly Offs + Paid Leaves + (0.5 * Half Days)
    Base Earned = (Monthly Base Salary / Days In Month) * Payable Days
    OT Amount = Total OT Hours * (Daily Wage / Standard Shift Hours)
    Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
    """
    class PaymentStatus(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft / Review'
        FINALIZED = 'FINALIZED', 'Finalized'
        PAID = 'PAID', 'Disbursed / Paid'
        ON_HOLD = 'ON_HOLD', 'On Hold'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='payrolls')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='payrolls')
    staff_member = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='payrolls')
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()
    days_in_month = models.PositiveIntegerField(default=30)

    # Attendance Tallies
    present_days = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('0.0'))
    weekly_offs = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('0.0'))
    paid_leaves = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('0.0'))
    half_days = models.PositiveIntegerField(default=0)
    absent_days = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('0.0'))
    unpaid_leaves = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('0.0'))

    # Payable Days = Present + Weekly Offs + Paid Leaves + (0.5 * Half Days)
    payable_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))

    # Base Earnings
    monthly_base_salary = models.DecimalField(max_digits=10, decimal_places=2)
    daily_wage = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    base_earned = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # Overtime
    total_ot_hours = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('0.0'))
    standard_shift_hours = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('9.0'))
    hourly_ot_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    ot_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # Adjustments
    penalties = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Aggregated daily penalties plus manager manual disciplinary deductions"
    )
    incentives = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Performance incentive, service charge share, or tips addition"
    )

    # Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
    net_salary = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.DRAFT)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finalized_payrolls')
    payment_date = models.DateField(null=True, blank=True)
    payment_mode = models.CharField(max_length=50, blank=True, default="Bank Transfer")
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('staff_member', 'year', 'month')
        ordering = ['-year', '-month', 'staff_member__department', 'staff_member__name']
        verbose_name = 'Monthly Payroll'
        verbose_name_plural = 'Monthly Payrolls'

    def __str__(self):
        return f"{self.staff_member.name} ({calendar.month_name[self.month]} {self.year}) - Net: ₹{self.net_salary}"

    def calculate_values(self):
        """
        Executes the exact formulas specified in the problem statement:
        1. Payable Days = Present Days + Weekly Offs + Paid Leaves + (0.5 * Half Days)
        2. Daily Wage = Monthly Base Salary / Days in Month
        3. Base Earned = Daily Wage * Payable Days
        4. Hourly OT Rate = Daily Wage / Standard Shift Hours
        5. OT Amount = Total OT Hours * Hourly OT Rate
        6. Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
        """
        dim = Decimal(str(self.days_in_month if self.days_in_month > 0 else 30))
        std_shift = Decimal(str(self.standard_shift_hours if self.standard_shift_hours > 0 else 9.0))
        base_sal = Decimal(str(self.monthly_base_salary or '0.00'))

        p_days = Decimal(str(self.present_days or '0.0'))
        wo_days = Decimal(str(self.weekly_offs or '0.0'))
        pl_days = Decimal(str(self.paid_leaves or '0.0'))
        hd_count = Decimal(str(self.half_days or '0'))

        # Formula 1: Payable Days
        self.payable_days = (p_days + wo_days + pl_days + (Decimal('0.5') * hd_count)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

        # Formula 2 & 3: Daily Wage & Base Earned
        self.daily_wage = (base_sal / dim).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.base_earned = (self.daily_wage * self.payable_days).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Formula 4 & 5: Hourly OT Rate & OT Amount
        self.hourly_ot_rate = (self.daily_wage / std_shift).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        ot_hrs = Decimal(str(self.total_ot_hours or '0.0'))
        self.ot_amount = (ot_hrs * self.hourly_ot_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Formula 6: Net Salary Payable
        penalties_val = Decimal(str(self.penalties or '0.00'))
        incentives_val = Decimal(str(self.incentives or '0.00'))
        raw_net = self.base_earned + self.ot_amount - penalties_val + incentives_val
        self.net_salary = max(Decimal('0.00'), raw_net).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class SOPShiftTiming(models.TextChoices):
    MORNING_PREP = 'MORNING_PREP', 'Morning Opening / Prep'
    AFTERNOON_SERVICE = 'AFTERNOON_SERVICE', 'Afternoon Service / Turnaround'
    EVENING_HANDOVER = 'EVENING_HANDOVER', 'Evening Handover'
    NIGHT_CLOSING = 'NIGHT_CLOSING', 'Night Closing & Audit'
    ALL_DAY = 'ALL_DAY', 'All Day Routine'


class DailySOPTask(models.Model):
    """
    Pre-configured hospitality Standard Operating Procedure (SOP) Master Task.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='sop_tasks')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='sop_tasks')
    department = models.CharField(max_length=30, choices=Department.choices, default=Department.KITCHEN)
    shift_timing = models.CharField(max_length=30, choices=SOPShiftTiming.choices, default=SOPShiftTiming.MORNING_PREP)
    title = models.CharField(max_length=200, help_text="e.g. Freezer & Chiller Temp Log, Speed Rail Bar Count")
    description = models.TextField(blank=True, help_text="Standard operating procedure steps or safety guideline")
    expected_standard = models.CharField(max_length=200, blank=True, help_text="e.g. -18°C or lower, Clean sanitized towels")
    requires_numeric_reading = models.BooleanField(default=False, help_text="Requires manager to log a numeric value (e.g. °C temp or bottle count)")
    unit = models.CharField(max_length=20, blank=True, help_text="e.g. °C, Units, PSI")
    is_critical = models.BooleanField(default=False, help_text="HACCP or high-priority safety check")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['department', 'shift_timing', 'sort_order', 'title']
        verbose_name = 'Daily SOP Task Template'
        verbose_name_plural = 'Daily SOP Task Templates'

    def __str__(self):
        return f"[{self.get_department_display()}] {self.title}"


class DailyTaskLog(models.Model):
    """
    Shift-wise daily inspection and check-off log performed by the manager on tablet.
    """
    class TaskStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        COMPLETED = 'COMPLETED', 'Completed & Verified'
        FLAGGED_ISSUE = 'FLAGGED_ISSUE', 'Issue Flagged'
        SKIPPED = 'SKIPPED', 'Skipped / N/A'

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='sop_logs')
    branch = models.ForeignKey(RestaurantBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='sop_logs')
    sop_task = models.ForeignKey(DailySOPTask, on_delete=models.CASCADE, related_name='logs')
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING)
    numeric_reading = models.CharField(max_length=50, blank=True, help_text="Logged reading e.g. -18°C or 24 bottles")
    verifier_notes = models.TextField(blank=True, help_text="Manager remarks, corrective actions, or handover notes")
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('sop_task', 'date')
        ordering = ['date', 'sop_task__department', 'sop_task__shift_timing', 'sop_task__sort_order']
        verbose_name = 'Daily SOP Task Log'
        verbose_name_plural = 'Daily SOP Task Logs'

    def __str__(self):
        return f"{self.sop_task.title} ({self.date}) - {self.get_status_display()}"
