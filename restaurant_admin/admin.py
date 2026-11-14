from django.contrib import admin
from restaurant_admin.models import (
    StaffMember,
    DailyAttendance,
    DailyMusterLock,
    MonthlyPayroll,
    DailySOPTask,
    DailyTaskLog,
)


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ('name', 'employee_id', 'department', 'designation', 'default_shift', 'monthly_base_salary', 'is_active', 'restaurant')
    list_filter = ('restaurant', 'department', 'default_shift', 'is_active')
    search_fields = ('name', 'employee_id', 'designation', 'phone')


@admin.register(DailyAttendance)
class DailyAttendanceAdmin(admin.ModelAdmin):
    list_display = ('staff_member', 'date', 'status', 'shift_type', 'ot_hours', 'penalty_amount', 'restaurant')
    list_filter = ('restaurant', 'status', 'shift_type', 'date')
    search_fields = ('staff_member__name', 'staff_member__employee_id')
    date_hierarchy = 'date'


@admin.register(DailyMusterLock)
class DailyMusterLockAdmin(admin.ModelAdmin):
    list_display = ('restaurant', 'branch', 'date', 'is_locked', 'locked_by', 'locked_at')
    list_filter = ('restaurant', 'is_locked', 'date')
    date_hierarchy = 'date'


@admin.register(MonthlyPayroll)
class MonthlyPayrollAdmin(admin.ModelAdmin):
    list_display = ('staff_member', 'year', 'month', 'payable_days', 'base_earned', 'ot_amount', 'net_salary', 'payment_status')
    list_filter = ('restaurant', 'year', 'month', 'payment_status', 'is_finalized')
    search_fields = ('staff_member__name', 'staff_member__employee_id')


@admin.register(DailySOPTask)
class DailySOPTaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'department', 'shift_timing', 'is_critical', 'requires_numeric_reading', 'is_active')
    list_filter = ('restaurant', 'department', 'shift_timing', 'is_critical', 'is_active')
    search_fields = ('title', 'description')


@admin.register(DailyTaskLog)
class DailyTaskLogAdmin(admin.ModelAdmin):
    list_display = ('sop_task', 'date', 'status', 'numeric_reading', 'completed_by', 'completed_at')
    list_filter = ('restaurant', 'date', 'status', 'sop_task__department')
    search_fields = ('sop_task__title', 'verifier_notes')
    date_hierarchy = 'date'
