import calendar
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import models, transaction
from django.utils import timezone

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from hotel.views_admin import hotel_admin_required
from restaurant_admin.models import (
    Department,
    ShiftType,
    StaffMember,
    AttendanceStatus,
    DailyAttendance,
    DailyMusterLock,
    MonthlyPayroll,
    SOPShiftTiming,
    DailySOPTask,
    DailyTaskLog,
    SHIFT_STANDARD_TIMINGS,
)

SHIFT_STANDARD_TIMINGS_JSON = {
    k: {
        'in': v['in'],
        'out': v['out'],
        'hours': float(v['hours']),
        'name': v['name']
    }
    for k, v in SHIFT_STANDARD_TIMINGS.items()
}


def check_hotel_muster_locked(restaurant, active_branch, target_date):
    """
    Check whether Hotel attendance for target_date is locked either by:
    1. Monthly payroll finalized for that month.
    2. Daily muster lock placed by hotel management.
    """
    payroll_finalized = MonthlyPayroll.objects.filter(
        restaurant=restaurant,
        year=target_date.year,
        month=target_date.month,
        is_finalized=True,
        staff_member__department__in=HOTEL_DEPARTMENTS
    ).exists()
    if payroll_finalized:
        return True, f"Monthly payroll for {target_date.strftime('%B %Y')} has been finalized & locked. Attendance is read-only.", True, None

    lock = DailyMusterLock.objects.filter(
        restaurant=restaurant,
        branch=active_branch,
        date=target_date,
        is_locked=True
    ).select_related('locked_by').first()

    if lock:
        user_name = lock.locked_by.get_full_name() or lock.locked_by.username if lock.locked_by else "Management"
        at_str = f" at {lock.locked_at.strftime('%H:%M, %d %b')}" if lock.locked_at else ""
        reason = f"Hotel muster roll finalized & locked by {user_name}{at_str}."
        return True, reason, False, lock

    return False, "", False, None
from restaurant_admin.views_hrms import number_to_words

logger = logging.getLogger(__name__)

# Strict Hotel PMS Department Scope: Only hotel-relevant departments
HOTEL_DEPARTMENTS = [
    Department.HOUSEKEEPING,
    Department.FRONT_DESK,
    Department.SECURITY,
    Department.MANAGEMENT,
]

HOTEL_DEPARTMENT_CHOICES = [
    (d, label) for d, label in Department.choices if d in HOTEL_DEPARTMENTS
]


def seed_sample_hotel_staff(restaurant, branch=None):
    """
    Automatic staff seeding disabled as per requirements.
    Managers create ground staff manually through the PMS Ground Staff form.
    """
    return 0


def seed_hotel_standard_sops(restaurant, branch=None):
    """
    Seeds exactly 2 sample hotel operational SOPs for the first time
    (1 Housekeeping Room Turnover routine + 1 Front Desk Keycard/Cash audit).
    """
    templates = [
        {
            'department': Department.HOUSEKEEPING,
            'shift_timing': SOPShiftTiming.MORNING_PREP,
            'title': 'Morning Checkout Room Turnover Priority List',
            'description': 'Receive checkout list from front desk, inspect departure rooms, and assign priority cleaning before 2:00 PM check-in.',
            'expected_standard': 'Target: Clean & inspected before 13:30',
            'requires_numeric_reading': True,
            'unit': 'Rooms',
            'is_critical': True,
            'sort_order': 10,
        },
        {
            'department': Department.FRONT_DESK,
            'shift_timing': SOPShiftTiming.MORNING_PREP,
            'title': 'Front Desk Cash Drawer Float & Keycard Stock Audit',
            'description': 'Count petty cash float (₹5,000 baseline) and RFID keycard dispenser levels before morning check-outs.',
            'expected_standard': 'Cash float ₹5,000 exact, min 50 encoded keycards',
            'requires_numeric_reading': True,
            'unit': 'Keycards',
            'is_critical': True,
            'sort_order': 20,
        },
    ]

    created_count = 0
    for item in templates:
        filter_kwargs = {'restaurant': restaurant, 'title': item['title']}
        if branch is not None:
            filter_kwargs['branch'] = branch
        else:
            filter_kwargs['branch__isnull'] = True

        if not DailySOPTask.objects.filter(**filter_kwargs).exists():
            DailySOPTask.objects.create(
                restaurant=restaurant,
                branch=branch,
                is_active=True,
                **item
            )
            created_count += 1
    return created_count


# ==============================================================================
# PHASE 1: DAILY MUSTER ROLL ATTENDANCE FOR HOTEL
# ==============================================================================

@hotel_admin_required
def hotel_muster_roll_view(request, restaurant_slug, ctx):
    """
    Hotel PMS Daily Muster Roll Attendance Cockpit.
    Precision timekeeping, tamper-evident locking, auto-calculated hours and overtime.
    Strictly scoped to hotel ground workforce: Housekeeping, Front Desk, Security, Management.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    rslug = ctx['rslug']

    # Date resolution
    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)
    is_today = (selected_date == date.today())

    # Check lock status for the selected date
    is_locked, lock_reason, is_payroll_finalized, lock_obj = check_hotel_muster_locked(restaurant, active_branch, selected_date)

    # Department filter (strictly limited to Hotel departments)
    dept_filter = request.GET.get('department', 'ALL').strip()
    search_query = request.GET.get('q', '').strip()

    # Base ground staff queryset strictly scoped to Hotel departments and active branch
    staff_qs = StaffMember.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        staff_qs = staff_qs.filter(branch=active_branch)
    else:
        staff_qs = staff_qs.filter(branch__isnull=True)

    total_active_staff = staff_qs.count()

    if dept_filter and dept_filter != 'ALL' and dept_filter in HOTEL_DEPARTMENTS:
        staff_qs = staff_qs.filter(department=dept_filter)

    if search_query:
        staff_qs = staff_qs.filter(
            models.Q(name__icontains=search_query) |
            models.Q(employee_id__icontains=search_query) |
            models.Q(designation__icontains=search_query)
        )

    staff_members = list(staff_qs.order_by('department', 'name'))

    # Load existing attendance records for the selected date
    existing_attendance = {
        att.staff_member_id: att
        for att in DailyAttendance.objects.filter(
            restaurant=restaurant,
            date=selected_date,
            staff_member__in=staff_members
        ).select_related('staff_member', 'marked_by')
    }

    # Aggregate daily status counters across ALL active hotel staff for this date
    all_active_hotel_staff = StaffMember.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        all_active_hotel_staff = all_active_hotel_staff.filter(branch=active_branch)
    else:
        all_active_hotel_staff = all_active_hotel_staff.filter(branch__isnull=True)
    all_active_ids = list(all_active_hotel_staff.values_list('id', flat=True))

    all_attendances_today = DailyAttendance.objects.filter(
        restaurant=restaurant,
        date=selected_date,
        staff_member_id__in=all_active_ids
    )

    marked_count = all_attendances_today.count()
    present_count = all_attendances_today.filter(status=AttendanceStatus.PRESENT).count()
    absent_count = all_attendances_today.filter(status=AttendanceStatus.ABSENT).count()
    half_day_count = all_attendances_today.filter(status=AttendanceStatus.HALF_DAY).count()
    leave_count = all_attendances_today.filter(status__in=[AttendanceStatus.PAID_LEAVE, AttendanceStatus.UNPAID_LEAVE]).count()
    weekly_off_count = all_attendances_today.filter(status=AttendanceStatus.WEEKLY_OFF).count()
    total_ot_hours_today = all_attendances_today.aggregate(total_ot=models.Sum('ot_hours'))['total_ot'] or Decimal('0.0')
    total_penalties_today = all_attendances_today.aggregate(total_pen=models.Sum('penalty_amount'))['total_pen'] or Decimal('0.00')

    # Department badges with counts (only for Hotel departments)
    dept_counts = []
    for dept_code, dept_label in HOTEL_DEPARTMENT_CHOICES:
        dept_qs = StaffMember.objects.filter(
            restaurant=restaurant,
            department=dept_code,
            is_active=True
        )
        if active_branch:
            dept_qs = dept_qs.filter(branch=active_branch)
        else:
            dept_qs = dept_qs.filter(branch__isnull=True)
        c = dept_qs.count()
        if c > 0:
            dept_counts.append({
                'code': dept_code,
                'label': dept_label,
                'total': c,
            })

    # Prepare rows for the template grid
    grid_rows = []
    for staff in staff_members:
        att = existing_attendance.get(staff.id)
        current_shift = att.shift_type if (att and att.shift_type) else staff.default_shift
        shift_info = SHIFT_STANDARD_TIMINGS.get(current_shift, SHIFT_STANDARD_TIMINGS[ShiftType.GENERAL])
        hours_worked = float(att.hours_worked) if att else 0.0
        suggested_ot = float(att.suggested_ot_hours) if att else 0.0
        shift_variance = float(att.shift_variance) if att else 0.0
        is_late = att.is_late_arrival if att else False
        marked_by_name = (att.marked_by.get_full_name() or att.marked_by.username) if (att and att.marked_by) else ''
        updated_at_str = att.updated_at.strftime('%H:%M') if att else ''

        grid_rows.append({
            'staff': staff,
            'attendance': att,
            'is_marked': att is not None,
            'status': att.status if att else '',
            'shift_type': current_shift,
            'check_in': att.check_in_time.strftime('%H:%M') if (att and att.check_in_time) else '',
            'check_out': att.check_out_time.strftime('%H:%M') if (att and att.check_out_time) else '',
            'hours_worked': hours_worked,
            'suggested_ot': suggested_ot,
            'shift_variance': shift_variance,
            'is_late': is_late,
            'ot_hours': float(att.ot_hours) if (att and att.ot_hours) else 0.0,
            'penalty_amount': float(att.penalty_amount) if (att and att.penalty_amount) else 0.0,
            'notes': att.notes if att else '',
            'marked_by_name': marked_by_name,
            'updated_at_str': updated_at_str,
            'standard_hours': float(staff.standard_shift_hours or Decimal('9.0')),
            'default_shift_in': shift_info['in'],
            'default_shift_out': shift_info['out'],
        })

    view_ctx = {
        **ctx,
        'active_menu': 'muster_roll',
        'active_group': 'staff_operations',
        'selected_date': selected_date,
        'selected_date_str': selected_date.isoformat(),
        'prev_date_str': prev_date.isoformat(),
        'next_date_str': next_date.isoformat(),
        'is_today': is_today,
        'is_locked': is_locked,
        'lock_reason': lock_reason,
        'is_payroll_finalized': is_payroll_finalized,
        'lock_obj': lock_obj,
        'selected_department': dept_filter,
        'search_query': search_query,
        'grid_rows': grid_rows,
        'total_active_staff': len(all_active_ids),
        'marked_count': marked_count,
        'unmarked_count': max(0, len(all_active_ids) - marked_count),
        'present_count': present_count,
        'absent_count': absent_count,
        'half_day_count': half_day_count,
        'leave_count': leave_count,
        'weekly_off_count': weekly_off_count,
        'total_ot_hours_today': total_ot_hours_today,
        'total_penalties_today': total_penalties_today,
        'dept_counts': dept_counts,
        'departments': HOTEL_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
        'shift_timings_json': json.dumps(SHIFT_STANDARD_TIMINGS_JSON),
        'attendance_statuses': AttendanceStatus.choices,
        'muster_config': {
            'selectedDate': selected_date.isoformat(),
            'selectedDepartment': dept_filter if dept_filter else 'ALL',
            'isLocked': is_locked,
            'lockReason': lock_reason,
            'isPayrollFinalized': is_payroll_finalized,
            'shiftTimings': SHIFT_STANDARD_TIMINGS_JSON,
            'metrics': {
                'present': present_count,
                'absent': absent_count,
                'half_day': half_day_count,
                'leave': leave_count,
                'weekly_off': weekly_off_count,
                'total_ot': total_ot_hours_today,
                'marked': marked_count,
                'total_staff': len(all_active_ids),
            },
            'urls': {
                'saveAttendance': reverse('hotel_admin:api_save_single_attendance', kwargs={'restaurant_slug': rslug}),
                'toggleLock': reverse('hotel_admin:api_toggle_muster_lock', kwargs={'restaurant_slug': rslug}),
                'markAllPresent': reverse('hotel_admin:api_mark_all_present', kwargs={'restaurant_slug': rslug}),
                'bulkAction': reverse('hotel_admin:api_bulk_muster_action', kwargs={'restaurant_slug': rslug}),
            }
        },
    }
    return render(request, 'hotel_admin/staff/muster_roll.html', view_ctx)


@hotel_admin_required
@require_POST
def hotel_api_mark_all_present(request, restaurant_slug, ctx):
    """
    1-click 'Mark All Present' batch population endpoint for the active hotel branch.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    date_str = data.get('date', '').strip()
    dept_filter = data.get('department', 'ALL').strip()
    fill_shift_times = bool(data.get('fill_shift_times', False))

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    is_locked, lock_reason, _, _ = check_hotel_muster_locked(restaurant, active_branch, target_date)
    if is_locked:
        return JsonResponse({'success': False, 'error': f"Cannot mark attendance: {lock_reason}"}, status=403)

    staff_qs = StaffMember.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        staff_qs = staff_qs.filter(branch=active_branch)
    else:
        staff_qs = staff_qs.filter(branch__isnull=True)

    if dept_filter and dept_filter != 'ALL' and dept_filter in HOTEL_DEPARTMENTS:
        staff_qs = staff_qs.filter(department=dept_filter)

    created_or_updated = 0
    with transaction.atomic():
        for staff in staff_qs:
            shift_to_use = staff.default_shift
            check_in_time = None
            check_out_time = None
            ot_hours = Decimal('0.0')

            if fill_shift_times:
                std_timing = SHIFT_STANDARD_TIMINGS.get(shift_to_use, SHIFT_STANDARD_TIMINGS[ShiftType.GENERAL])
                check_in_time = datetime.strptime(std_timing['in'], '%H:%M').time()
                check_out_time = datetime.strptime(std_timing['out'], '%H:%M').time()

            att, created = DailyAttendance.objects.get_or_create(
                staff_member=staff,
                date=target_date,
                defaults={
                    'restaurant': restaurant,
                    'branch': active_branch,
                    'status': AttendanceStatus.PRESENT,
                    'shift_type': shift_to_use,
                    'check_in_time': check_in_time,
                    'check_out_time': check_out_time,
                    'ot_hours': ot_hours,
                    'marked_by': request.user,
                }
            )
            if not created:
                att.status = AttendanceStatus.PRESENT
                att.shift_type = shift_to_use
                if fill_shift_times:
                    att.check_in_time = check_in_time
                    att.check_out_time = check_out_time
                    att.ot_hours = ot_hours
                att.marked_by = request.user
                att.save()
            created_or_updated += 1

    return JsonResponse({
        'success': True,
        'count': created_or_updated,
        'message': f"Successfully marked {created_or_updated} hotel staff members as PRESENT for {target_date.strftime('%d %b %Y')}."
    })


@hotel_admin_required
@require_POST
def hotel_api_save_single_attendance(request, restaurant_slug, ctx):
    """
    Live saving endpoint for an individual staff member's muster row in Hotel PMS.
    Enforces tamper-proof locking and calculates overtime.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    staff_id = data.get('staff_id')
    date_str = data.get('date', '').strip()
    status_val = data.get('status', '').strip()
    shift_val = data.get('shift_type', '').strip()
    check_in_str = data.get('check_in_time', data.get('check_in', '')).strip()
    check_out_str = data.get('check_out_time', data.get('check_out', '')).strip()
    ot_hours_val = data.get('ot_hours', 0)
    penalty_val = data.get('penalty_amount', 0)
    notes_val = data.get('notes', '').strip()
    auto_calc_ot = bool(data.get('auto_calc_ot', False))

    if not staff_id or not status_val:
        return JsonResponse({'success': False, 'error': 'Staff ID and Status are required'}, status=400)

    staff = get_object_or_404(
        StaffMember,
        id=staff_id,
        restaurant=restaurant,
        department__in=HOTEL_DEPARTMENTS
    )

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    is_locked, lock_reason, _, _ = check_hotel_muster_locked(restaurant, active_branch, target_date)
    if is_locked:
        return JsonResponse({'success': False, 'error': f"Attendance is locked: {lock_reason}"}, status=403)

    check_in_time = None
    if check_in_str:
        try:
            check_in_time = datetime.strptime(check_in_str, '%H:%M').time()
        except ValueError:
            pass

    check_out_time = None
    if check_out_str:
        try:
            check_out_time = datetime.strptime(check_out_str, '%H:%M').time()
        except ValueError:
            pass

    try:
        ot_hours = Decimal(str(max(0.0, float(ot_hours_val))))
    except (ValueError, TypeError):
        ot_hours = Decimal('0.0')

    if auto_calc_ot and check_in_time and check_out_time:
        in_m = check_in_time.hour * 60 + check_in_time.minute
        out_m = check_out_time.hour * 60 + check_out_time.minute
        if out_m < in_m:
            out_m += 1440
        dur_h = (out_m - in_m) / 60.0
        std_h = float(staff.standard_shift_hours or Decimal('9.0'))
        if dur_h > std_h:
            ot_hours = Decimal(str(round(dur_h - std_h, 1)))
        else:
            ot_hours = Decimal('0.0')

    try:
        penalty_amount = Decimal(str(max(0.0, float(penalty_val))))
    except (ValueError, TypeError):
        penalty_amount = Decimal('0.00')

    valid_statuses = [c[0] for c in AttendanceStatus.choices]
    if status_val not in valid_statuses:
        return JsonResponse({'success': False, 'error': f"Invalid status: {status_val}"}, status=400)

    valid_shifts = [c[0] for c in ShiftType.choices]
    if shift_val and shift_val not in valid_shifts:
        shift_val = staff.default_shift

    att, created = DailyAttendance.objects.update_or_create(
        staff_member=staff,
        date=target_date,
        defaults={
            'restaurant': restaurant,
            'branch': active_branch or staff.branch,
            'status': status_val,
            'shift_type': shift_val or staff.default_shift,
            'check_in_time': check_in_time,
            'check_out_time': check_out_time,
            'ot_hours': ot_hours,
            'penalty_amount': penalty_amount,
            'notes': notes_val,
            'marked_by': request.user,
        }
    )

    today_records = DailyAttendance.objects.filter(
        restaurant=restaurant,
        staff_member__department__in=HOTEL_DEPARTMENTS,
        date=target_date
    )
    if active_branch:
        today_records = today_records.filter(branch=active_branch)

    return JsonResponse({
        'success': True,
        'attendance_id': att.id,
        'status': att.status,
        'status_display': att.get_status_display(),
        'check_in': att.check_in_time.strftime('%H:%M') if att.check_in_time else '',
        'check_out': att.check_out_time.strftime('%H:%M') if att.check_out_time else '',
        'hours_worked': float(att.hours_worked),
        'suggested_ot': float(att.suggested_ot_hours),
        'shift_variance': float(att.shift_variance),
        'is_late': att.is_late_arrival,
        'ot_hours': float(att.ot_hours),
        'penalty_amount': float(att.penalty_amount),
        'marked_by_name': request.user.get_full_name() or request.user.username,
        'updated_at': att.updated_at.strftime('%H:%M'),
        'summary': {
            'marked': today_records.count(),
            'present': today_records.filter(status=AttendanceStatus.PRESENT).count(),
            'absent': today_records.filter(status=AttendanceStatus.ABSENT).count(),
            'half_day': today_records.filter(status=AttendanceStatus.HALF_DAY).count(),
            'leave': today_records.filter(status__in=[AttendanceStatus.PAID_LEAVE, AttendanceStatus.UNPAID_LEAVE]).count(),
            'weekly_off': today_records.filter(status=AttendanceStatus.WEEKLY_OFF).count(),
            'total_ot': float(today_records.aggregate(models.Sum('ot_hours'))['ot_hours__sum'] or 0.0),
        }
    })


@hotel_admin_required
@require_POST
def hotel_api_toggle_muster_lock(request, restaurant_slug, ctx):
    """
    Lock or unlock hotel muster roll for target_date.
    Finalizing locks attendance and creates audit history.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    date_str = data.get('date', '').strip()
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    if MonthlyPayroll.objects.filter(restaurant=restaurant, year=target_date.year, month=target_date.month, is_finalized=True, staff_member__department__in=HOTEL_DEPARTMENTS).exists():
        return JsonResponse({
            'success': False,
            'error': f"Monthly payroll for {target_date.strftime('%B %Y')} is finalized. Daily attendance records are permanently locked."
        }, status=403)

    lock, created = DailyMusterLock.objects.get_or_create(
        restaurant=restaurant,
        branch=active_branch,
        date=target_date,
        defaults={'is_locked': False}
    )

    lock.is_locked = not lock.is_locked
    if lock.is_locked:
        lock.locked_by = request.user
        lock.locked_at = timezone.now()
        lock.lock_reason = data.get('reason', 'Hotel muster roll finalized by management').strip()
    else:
        lock.locked_by = None
        lock.locked_at = None
        lock.lock_reason = ''
    lock.save()

    user_name = request.user.get_full_name() or request.user.username
    return JsonResponse({
        'success': True,
        'is_locked': lock.is_locked,
        'locked_by': user_name if lock.is_locked else None,
        'locked_at': lock.locked_at.strftime('%H:%M, %d %b') if (lock.is_locked and lock.locked_at) else None,
        'reason': lock.lock_reason,
        'message': f"Hotel muster roll {'locked & finalized' if lock.is_locked else 'unlocked'} for {target_date.strftime('%d %b %Y')}."
    })


@hotel_admin_required
@require_POST
def hotel_api_bulk_muster_action(request, restaurant_slug, ctx):
    """
    Bulk operations for faster hotel workforce timekeeping:
    1. 'mark_unmarked_absent': Marks all staff without an attendance entry as ABSENT.
    2. 'fill_shift_timings': Fills standard shift in/out times for all staff currently marked PRESENT.
    3. 'recalculate_all_ot': Auto-calculates and updates OT hours based on recorded check-in/out times.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    action = data.get('action', '').strip()
    date_str = data.get('date', '').strip()
    dept_filter = data.get('department', 'ALL').strip()

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    is_locked, lock_reason, _, _ = check_hotel_muster_locked(restaurant, active_branch, target_date)
    if is_locked:
        return JsonResponse({'success': False, 'error': f"Cannot execute bulk action: {lock_reason}"}, status=403)

    staff_qs = StaffMember.objects.filter(restaurant=restaurant, department__in=HOTEL_DEPARTMENTS, is_active=True)
    if active_branch:
        staff_qs = staff_qs.filter(branch=active_branch)
    else:
        staff_qs = staff_qs.filter(branch__isnull=True)
    if dept_filter and dept_filter != 'ALL' and dept_filter in HOTEL_DEPARTMENTS:
        staff_qs = staff_qs.filter(department=dept_filter)

    updated_count = 0
    with transaction.atomic():
        if action == 'mark_unmarked_absent':
            existing_ids = DailyAttendance.objects.filter(
                restaurant=restaurant,
                date=target_date,
                staff_member__in=staff_qs
            ).values_list('staff_member_id', flat=True)

            unmarked_staff = staff_qs.exclude(id__in=existing_ids)
            for staff in unmarked_staff:
                DailyAttendance.objects.create(
                    restaurant=restaurant,
                    branch=active_branch,
                    staff_member=staff,
                    date=target_date,
                    status=AttendanceStatus.ABSENT,
                    shift_type=staff.default_shift,
                    ot_hours=Decimal('0.0'),
                    penalty_amount=Decimal('0.00'),
                    marked_by=request.user,
                    notes='Marked absent via bulk close'
                )
                updated_count += 1
            msg = f"Marked {updated_count} unmarked hotel staff member(s) as Absent."

        elif action == 'fill_shift_timings':
            present_records = DailyAttendance.objects.filter(
                restaurant=restaurant,
                date=target_date,
                status=AttendanceStatus.PRESENT,
                staff_member__in=staff_qs
            ).select_related('staff_member')

            for att in present_records:
                shift_key = att.shift_type or att.staff_member.default_shift
                std_timing = SHIFT_STANDARD_TIMINGS.get(shift_key, SHIFT_STANDARD_TIMINGS[ShiftType.GENERAL])
                att.check_in_time = datetime.strptime(std_timing['in'], '%H:%M').time()
                att.check_out_time = datetime.strptime(std_timing['out'], '%H:%M').time()
                att.marked_by = request.user
                att.save()
                updated_count += 1
            msg = f"Applied standard shift timings for {updated_count} present hotel staff member(s)."

        elif action == 'recalculate_all_ot':
            records_with_times = DailyAttendance.objects.filter(
                restaurant=restaurant,
                date=target_date,
                check_in_time__isnull=False,
                check_out_time__isnull=False,
                staff_member__in=staff_qs
            ).select_related('staff_member')

            for att in records_with_times:
                new_ot = att.suggested_ot_hours
                att.ot_hours = new_ot
                att.marked_by = request.user
                att.save()
                updated_count += 1
            msg = f"Recalculated overtime for {updated_count} hotel staff member(s)."

        else:
            return JsonResponse({'success': False, 'error': f"Unknown bulk action '{action}'"}, status=400)

    return JsonResponse({
        'success': True,
        'count': updated_count,
        'message': msg,
        'action': action
    })


# ==============================================================================
# PHASE 2: HOTEL GROUND STAFF DIRECTORY
# ==============================================================================

@hotel_admin_required
def hotel_ground_staff_list(request, restaurant_slug, ctx):
    """
    Hotel Ground Staff Directory (Housekeeping, Concierge, Room Service, Maintenance, Security).
    Strictly scoped to hotel workforce and active hotel branch.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    rslug = ctx['rslug']

    dept_filter = request.GET.get('department', '').strip()
    status_filter = request.GET.get('status', 'ACTIVE').strip()
    search_query = request.GET.get('q', '').strip()

    qs = StaffMember.objects.filter(
        restaurant=restaurant,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        qs = qs.filter(branch=active_branch)
    else:
        qs = qs.filter(branch__isnull=True)

    total_count = qs.count()
    active_count = qs.filter(is_active=True).count()
    inactive_count = qs.filter(is_active=False).count()

    if status_filter == 'ACTIVE':
        qs = qs.filter(is_active=True)
    elif status_filter == 'INACTIVE':
        qs = qs.filter(is_active=False)

    if dept_filter and dept_filter in HOTEL_DEPARTMENTS:
        qs = qs.filter(department=dept_filter)

    if search_query:
        qs = qs.filter(
            models.Q(name__icontains=search_query) |
            models.Q(employee_id__icontains=search_query) |
            models.Q(designation__icontains=search_query) |
            models.Q(phone__icontains=search_query)
        )

    staff_members = list(qs.order_by('department', 'name'))

    view_ctx = {
        **ctx,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'staff_members': staff_members,
        'total_count': total_count,
        'active_count': active_count,
        'inactive_count': inactive_count,
        'selected_department': dept_filter,
        'selected_status': status_filter,
        'search_query': search_query,
        'departments': HOTEL_DEPARTMENT_CHOICES,
    }
    return render(request, 'hotel_admin/staff/ground_staff_list.html', view_ctx)


@hotel_admin_required
def hotel_ground_staff_create(request, restaurant_slug, ctx):
    """
    Create a new hotel ground staff profile (e.g. Room Attendant, Bellhop, Laundry Attendant, Security).
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    rslug = ctx['rslug']

    if request.method == 'POST':
        emp_id = request.POST.get('employee_id', '').strip()
        name = request.POST.get('name', '').strip()
        department = request.POST.get('department', Department.HOUSEKEEPING).strip()
        if department not in HOTEL_DEPARTMENTS:
            department = Department.HOUSEKEEPING

        designation = request.POST.get('designation', '').strip()
        phone = request.POST.get('phone', '').strip()
        shift = request.POST.get('default_shift', ShiftType.MORNING).strip()
        salary_str = request.POST.get('monthly_base_salary', '18000.00').strip()
        shift_hours_str = request.POST.get('standard_shift_hours', '8.0').strip()
        notes = request.POST.get('notes', '').strip()

        if not name:
            messages.error(request, "Staff member's full name is required.")
            return redirect('hotel_admin:ground_staff_create', restaurant_slug=rslug)

        if not emp_id:
            next_num = StaffMember.objects.filter(restaurant=restaurant, department__in=HOTEL_DEPARTMENTS).count() + 101
            emp_id = f"HTL-{next_num}"

        try:
            salary = Decimal(salary_str)
        except Exception:
            salary = Decimal('18000.00')

        try:
            shift_hours = Decimal(shift_hours_str)
        except Exception:
            shift_hours = Decimal('8.0')

        staff = StaffMember.objects.create(
            restaurant=restaurant,
            branch=active_branch,
            employee_id=emp_id,
            name=name,
            department=department,
            designation=designation,
            phone=phone,
            default_shift=shift,
            monthly_base_salary=salary,
            standard_shift_hours=shift_hours,
            is_active=True,
            notes=notes,
        )
        messages.success(request, f"Hotel staff member '{staff.name}' ({staff.employee_id}) added successfully.")
        return redirect('hotel_admin:ground_staff_list', restaurant_slug=rslug)

    view_ctx = {
        **ctx,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'is_edit': False,
        'departments': HOTEL_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
    }
    return render(request, 'hotel_admin/staff/ground_staff_form.html', view_ctx)


@hotel_admin_required
def hotel_ground_staff_edit(request, restaurant_slug, ctx, pk):
    """
    Edit an existing hotel ground staff member's profile.
    """
    restaurant = ctx['restaurant']
    rslug = ctx['rslug']
    staff = get_object_or_404(
        StaffMember,
        id=pk,
        restaurant=restaurant,
        department__in=HOTEL_DEPARTMENTS
    )

    if request.method == 'POST':
        staff.name = request.POST.get('name', '').strip() or staff.name
        staff.employee_id = request.POST.get('employee_id', '').strip() or staff.employee_id
        dept_val = request.POST.get('department', staff.department).strip()
        if dept_val in HOTEL_DEPARTMENTS:
            staff.department = dept_val
        staff.designation = request.POST.get('designation', '').strip()
        staff.phone = request.POST.get('phone', '').strip()
        staff.default_shift = request.POST.get('default_shift', staff.default_shift).strip()
        salary_str = request.POST.get('monthly_base_salary', str(staff.monthly_base_salary)).strip()
        shift_hours_str = request.POST.get('standard_shift_hours', str(staff.standard_shift_hours)).strip()
        staff.notes = request.POST.get('notes', '').strip()

        try:
            staff.monthly_base_salary = Decimal(salary_str)
        except Exception:
            pass

        try:
            staff.standard_shift_hours = Decimal(shift_hours_str)
        except Exception:
            pass

        staff.save()
        messages.success(request, f"Updated profile for '{staff.name}'.")
        return redirect('hotel_admin:ground_staff_list', restaurant_slug=rslug)

    view_ctx = {
        **ctx,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'staff': staff,
        'is_edit': True,
        'departments': HOTEL_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
    }
    return render(request, 'hotel_admin/staff/ground_staff_form.html', view_ctx)


@hotel_admin_required
@require_POST
def hotel_ground_staff_toggle_active(request, restaurant_slug, ctx, pk):
    """
    Toggle active/inactive status of a hotel ground staff member.
    """
    restaurant = ctx['restaurant']
    rslug = ctx['rslug']
    staff = get_object_or_404(
        StaffMember,
        id=pk,
        restaurant=restaurant,
        department__in=HOTEL_DEPARTMENTS
    )
    staff.is_active = not staff.is_active
    staff.save()
    status_word = "Activated" if staff.is_active else "Deactivated"
    messages.info(request, f"Staff member '{staff.name}' has been {status_word}.")
    return redirect('hotel_admin:ground_staff_list', restaurant_slug=rslug)


# ==============================================================================
# PHASE 3: AUTOMATED MONTHLY PAYROLL ENGINE FOR HOTEL
# ==============================================================================

@hotel_admin_required
def hotel_payroll_dashboard(request, restaurant_slug, ctx):
    """
    Hotel Automated Monthly Payroll Engine Dashboard.
    Calculates monthly base wages, overtime hours, discipline/late deductions,
    performance allowances, and yields ready-to-print salary slips.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    rslug = ctx['rslug']

    today = date.today()
    try:
        selected_year = int(request.GET.get('year', today.year))
        selected_month = int(request.GET.get('month', today.month))
    except (ValueError, TypeError):
        selected_year = today.year
        selected_month = today.month

    dept_filter = request.GET.get('department', 'ALL').strip()
    status_filter = request.GET.get('status', 'ALL').strip()

    # Pre-seed sample staff if empty
    staff_count = StaffMember.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        staff_count = staff_count.filter(branch=active_branch)
    else:
        staff_count = staff_count.filter(branch__isnull=True)

    # Fetch existing payroll records for month/year strictly scoped to Hotel staff
    payroll_qs = MonthlyPayroll.objects.filter(
        restaurant=restaurant,
        year=selected_year,
        month=selected_month,
        staff_member__department__in=HOTEL_DEPARTMENTS,
    ).select_related('staff_member')

    if active_branch:
        payroll_qs = payroll_qs.filter(staff_member__branch=active_branch)
    else:
        payroll_qs = payroll_qs.filter(staff_member__branch__isnull=True)

    if dept_filter and dept_filter != 'ALL' and dept_filter in HOTEL_DEPARTMENTS:
        payroll_qs = payroll_qs.filter(staff_member__department=dept_filter)

    if status_filter and status_filter != 'ALL':
        payroll_qs = payroll_qs.filter(payment_status=status_filter)

    payrolls = list(payroll_qs.order_by('staff_member__department', 'staff_member__name'))

    total_gross_disbursed = sum(p.base_earned for p in payrolls) if payrolls else Decimal('0.00')
    total_net_payable = sum(p.net_salary for p in payrolls) if payrolls else Decimal('0.00')
    total_ot_payout = sum(p.ot_amount for p in payrolls) if payrolls else Decimal('0.00')
    total_penalties = sum(p.penalties for p in payrolls) if payrolls else Decimal('0.00')
    finalized_count = sum(1 for p in payrolls if p.is_finalized or p.payment_status == MonthlyPayroll.PaymentStatus.FINALIZED)
    draft_count = len(payrolls) - finalized_count

    # Month options
    months_list = [{'num': m, 'name': calendar.month_name[m]} for m in range(1, 13)]
    years_list = [today.year - 1, today.year, today.year + 1]

    view_ctx = {
        **ctx,
        'active_menu': 'payroll_dashboard',
        'active_group': 'staff_operations',
        'selected_year': selected_year,
        'selected_month': selected_month,
        'selected_month_name': calendar.month_name[selected_month],
        'months_list': months_list,
        'years_list': years_list,
        'selected_department': dept_filter,
        'selected_status': status_filter,
        'payrolls': payrolls,
        'total_count': len(payrolls),
        'total_gross_disbursed': total_gross_disbursed,
        'total_net_payable': total_net_payable,
        'total_ot_payout': total_ot_payout,
        'total_penalties': total_penalties,
        'finalized_count': finalized_count,
        'draft_count': draft_count,
        'departments': HOTEL_DEPARTMENT_CHOICES,
    }
    return render(request, 'hotel_admin/staff/payroll_dashboard.html', view_ctx)


@hotel_admin_required
@require_POST
def hotel_api_generate_monthly_payroll(request, restaurant_slug, ctx):
    """
    One-Click Automated Payroll Engine for Hotel.
    Aggregates all daily muster inputs, OT hours, and calculates:
    1. Payable Days = Present Days + Weekly Offs + Paid Leaves + (0.5 * Half Days)
    2. Base Earned = (Monthly Base Salary / Days in Month) * Payable Days
    3. OT Amount = Total OT Hours * (Daily Wage / Standard Shift Hours)
    4. Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    try:
        year = int(data.get('year', timezone.now().year))
        month = int(data.get('month', timezone.now().month))
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid month or year'}, status=400)

    days_in_month = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, days_in_month)

    staff_qs = StaffMember.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        staff_qs = staff_qs.filter(branch=active_branch)
    else:
        staff_qs = staff_qs.filter(branch__isnull=True)

    processed_count = 0
    with transaction.atomic():
        for staff in staff_qs:
            attendances = DailyAttendance.objects.filter(
                staff_member=staff,
                date__range=[start_date, end_date]
            )

            p_days = Decimal(str(attendances.filter(status=AttendanceStatus.PRESENT).count()))
            wo_days = Decimal(str(attendances.filter(status=AttendanceStatus.WEEKLY_OFF).count()))
            pl_days = Decimal(str(attendances.filter(status=AttendanceStatus.PAID_LEAVE).count()))
            hd_count = attendances.filter(status=AttendanceStatus.HALF_DAY).count()
            a_days = Decimal(str(attendances.filter(status=AttendanceStatus.ABSENT).count()))
            ul_days = Decimal(str(attendances.filter(status=AttendanceStatus.UNPAID_LEAVE).count()))

            ot_hours_agg = attendances.aggregate(total_ot=models.Sum('ot_hours'))['total_ot'] or Decimal('0.0')
            daily_penalties_agg = attendances.aggregate(total_pen=models.Sum('penalty_amount'))['total_pen'] or Decimal('0.00')

            payroll, created = MonthlyPayroll.objects.get_or_create(
                staff_member=staff,
                year=year,
                month=month,
                defaults={
                    'restaurant': restaurant,
                    'branch': active_branch,
                    'days_in_month': days_in_month,
                    'monthly_base_salary': staff.monthly_base_salary,
                    'standard_shift_hours': staff.standard_shift_hours,
                    'penalties': daily_penalties_agg,
                    'incentives': Decimal('0.00'),
                }
            )

            payroll.days_in_month = days_in_month
            payroll.monthly_base_salary = staff.monthly_base_salary
            payroll.standard_shift_hours = staff.standard_shift_hours

            payroll.present_days = p_days
            payroll.weekly_offs = wo_days
            payroll.paid_leaves = pl_days
            payroll.half_days = hd_count
            payroll.absent_days = a_days
            payroll.unpaid_leaves = ul_days
            payroll.total_ot_hours = ot_hours_agg

            if created or payroll.penalties == Decimal('0.00'):
                payroll.penalties = daily_penalties_agg

            payroll.calculate_values()
            payroll.save()
            processed_count += 1

    return JsonResponse({
        'success': True,
        'count': processed_count,
        'message': f"Successfully generated hotel payroll for {processed_count} staff members ({calendar.month_name[month]} {year}).",
    })


@hotel_admin_required
@require_POST
def hotel_api_update_payroll_adjustment(request, restaurant_slug, ctx, pk):
    """
    Manager manual incentive/penalty adjustment before final sign-off.
    """
    restaurant = ctx['restaurant']
    payroll = get_object_or_404(
        MonthlyPayroll,
        id=pk,
        restaurant=restaurant,
        staff_member__department__in=HOTEL_DEPARTMENTS
    )

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    try:
        incentives = Decimal(str(data.get('incentives', payroll.incentives)))
        penalties = Decimal(str(data.get('penalties', payroll.penalties)))
        remarks = data.get('remarks', payroll.remarks)

        payroll.incentives = incentives
        payroll.penalties = penalties
        payroll.remarks = remarks
        payroll.calculate_values()
        payroll.save()

        return JsonResponse({
            'success': True,
            'net_salary': float(payroll.net_salary),
            'gross_earned': float(payroll.base_earned),
            'base_earned': float(payroll.base_earned),
            'ot_amount': float(payroll.ot_amount),
            'message': f"Updated adjustments for {payroll.staff_member.name}. Net salary: ₹{payroll.net_salary:,.2f}"
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@hotel_admin_required
@require_POST
def hotel_api_finalize_all_payroll(request, restaurant_slug, ctx):
    """
    Locks and finalizes all draft payrolls for the given month for Hotel staff.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    try:
        year = int(data.get('year', timezone.now().year))
        month = int(data.get('month', timezone.now().month))
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid month or year'}, status=400)

    qs = MonthlyPayroll.objects.filter(
        restaurant=restaurant,
        year=year,
        month=month,
        payment_status=MonthlyPayroll.PaymentStatus.DRAFT,
        staff_member__department__in=HOTEL_DEPARTMENTS,
    )
    if active_branch:
        qs = qs.filter(staff_member__branch=active_branch)
    else:
        qs = qs.filter(staff_member__branch__isnull=True)

    count = qs.update(
        payment_status=MonthlyPayroll.PaymentStatus.FINALIZED,
        is_finalized=True,
        finalized_at=timezone.now(),
        finalized_by=request.user
    )

    return JsonResponse({
        'success': True,
        'count': count,
        'message': f"Finalized {count} hotel payroll records for {calendar.month_name[month]} {year}."
    })


@hotel_admin_required
def hotel_salary_slip_view(request, restaurant_slug, ctx, pk):
    """
    Generates a printable, formal salary voucher / pay slip for hotel staff.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    payroll = get_object_or_404(
        MonthlyPayroll.objects.select_related('staff_member', 'branch', 'restaurant'),
        id=pk,
        restaurant=restaurant,
        staff_member__department__in=HOTEL_DEPARTMENTS
    )

    amount_in_words = number_to_words(payroll.net_salary)
    month_name = calendar.month_name[payroll.month]

    view_ctx = {
        **ctx,
        'payroll': payroll,
        'staff': payroll.staff_member,
        'month_name': month_name,
        'amount_in_words': amount_in_words,
    }
    return render(request, 'hotel_admin/staff/salary_slip.html', view_ctx)


# ==============================================================================
# PHASE 4: DAILY OPERATIONAL SOP BOARD FOR HOTEL
# ==============================================================================

@hotel_admin_required
def hotel_sop_dashboard_view(request, restaurant_slug, ctx):
    """
    Hotel Operations Daily SOP Board.
    Tracking Room turnover, Linen audits, Public zone sanitization, Front Desk keycards, Night audit.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']
    rslug = ctx['rslug']

    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)
    is_today = (selected_date == date.today())

    shift_filter = request.GET.get('shift', 'ALL').strip()
    dept_filter = request.GET.get('department', 'ALL').strip()

    tasks_qs = DailySOPTask.objects.filter(
        restaurant=restaurant,
        is_active=True,
        department__in=HOTEL_DEPARTMENTS
    )
    if active_branch:
        tasks_qs = tasks_qs.filter(branch=active_branch)
    else:
        tasks_qs = tasks_qs.filter(branch__isnull=True)

    if shift_filter and shift_filter != 'ALL':
        tasks_qs = tasks_qs.filter(shift_timing=shift_filter)

    if dept_filter and dept_filter != 'ALL' and dept_filter in HOTEL_DEPARTMENTS:
        tasks_qs = tasks_qs.filter(department=dept_filter)

    tasks = list(tasks_qs.order_by('shift_timing', 'sort_order', 'id'))

    # Load completion logs for the selected date
    logs = {
        log.sop_task_id: log
        for log in DailyTaskLog.objects.filter(
            restaurant=restaurant,
            date=selected_date,
            sop_task__in=tasks
        ).select_related('completed_by')
    }

    # Prepare structured view items
    sop_items = []
    completed_count = 0
    critical_pending_count = 0

    for t in tasks:
        log = logs.get(t.id)
        is_done = (log is not None and log.status == DailyTaskLog.TaskStatus.COMPLETED)
        if is_done:
            completed_count += 1
        elif t.is_critical:
            critical_pending_count += 1

        sop_items.append({
            'task': t,
            'log': log,
            'is_completed': is_done,
            'numeric_reading': log.numeric_reading if log else '',
            'notes': log.verifier_notes if log else '',
            'completed_by_name': log.completed_by.get_full_name() or log.completed_by.username if (log and log.completed_by) else '',
            'completed_at_time': log.completed_at.strftime('%H:%M') if (log and log.completed_at) else '',
        })

    total_tasks = len(tasks)
    completion_pct = int(round((completed_count / total_tasks * 100))) if total_tasks > 0 else 0

    view_ctx = {
        **ctx,
        'active_menu': 'sop_dashboard',
        'active_group': 'staff_operations',
        'selected_date': selected_date,
        'selected_date_str': selected_date.isoformat(),
        'prev_date_str': prev_date.isoformat(),
        'next_date_str': next_date.isoformat(),
        'is_today': is_today,
        'selected_shift': shift_filter,
        'selected_department': dept_filter,
        'sop_items': sop_items,
        'total_tasks': total_tasks,
        'completed_count': completed_count,
        'pending_count': total_tasks - completed_count,
        'critical_pending_count': critical_pending_count,
        'completion_pct': completion_pct,
        'shift_timings': SOPShiftTiming.choices,
        'departments': HOTEL_DEPARTMENT_CHOICES,
    }
    return render(request, 'hotel_admin/staff/sop_dashboard.html', view_ctx)


@hotel_admin_required
@require_POST
def hotel_api_toggle_sop_task_status(request, restaurant_slug, ctx):
    """
    Live toggle checkbox endpoint for Hotel SOP dashboard.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    task_id = data.get('task_id')
    date_str = data.get('date', '').strip()
    is_completed = bool(data.get('is_completed', True))
    numeric_reading_val = data.get('numeric_reading', '')
    notes = data.get('notes', '').strip()

    if not task_id:
        return JsonResponse({'success': False, 'error': 'Task ID is required'}, status=400)

    task = get_object_or_404(
        DailySOPTask,
        id=task_id,
        restaurant=restaurant,
        department__in=HOTEL_DEPARTMENTS
    )

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    status_val = DailyTaskLog.TaskStatus.COMPLETED if is_completed else DailyTaskLog.TaskStatus.PENDING

    log, created = DailyTaskLog.objects.get_or_create(
        sop_task=task,
        date=target_date,
        defaults={
            'restaurant': restaurant,
            'branch': active_branch or task.branch,
            'status': status_val,
            'numeric_reading': str(numeric_reading_val or ''),
            'verifier_notes': notes,
            'completed_by': request.user if is_completed else None,
            'completed_at': timezone.now() if is_completed else None,
        }
    )

    if not created:
        log.status = status_val
        if numeric_reading_val is not None:
            log.numeric_reading = str(numeric_reading_val or '')
        if notes:
            log.verifier_notes = notes
        if is_completed:
            log.completed_by = request.user
            log.completed_at = timezone.now()
        else:
            log.completed_by = None
            log.completed_at = None
        log.save()

    return JsonResponse({
        'success': True,
        'task_id': task.id,
        'is_completed': (log.status == DailyTaskLog.TaskStatus.COMPLETED),
        'completed_by': request.user.username if log.status == DailyTaskLog.TaskStatus.COMPLETED else '',
        'completed_at': log.completed_at.strftime('%H:%M') if log.completed_at else '',
        'message': f"Updated '{task.title}' as {'Completed' if log.status == DailyTaskLog.TaskStatus.COMPLETED else 'Pending'}."
    })


@hotel_admin_required
@require_POST
def hotel_api_seed_default_sops(request, restaurant_slug, ctx):
    """
    Reset / seed default hotel standard operational procedures.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    created_count = seed_hotel_standard_sops(restaurant, active_branch)
    return JsonResponse({
        'success': True,
        'created_count': created_count,
        'message': f"Seeded {created_count} hotel standard SOP routines successfully."
    })


@hotel_admin_required
@require_POST
def hotel_api_create_sop_task(request, restaurant_slug, ctx):
    """
    Create custom SOP routine specifically for hotel.
    """
    restaurant = ctx['restaurant']
    active_branch = ctx['current_branch']

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    title = data.get('title', '').strip()
    if not title:
        return JsonResponse({'success': False, 'error': 'Task Title is required'}, status=400)

    department = data.get('department', Department.HOUSEKEEPING).strip()
    if department not in HOTEL_DEPARTMENTS:
        department = Department.HOUSEKEEPING

    shift_timing = data.get('shift_timing', SOPShiftTiming.MORNING_PREP).strip()
    description = data.get('description', '').strip()
    expected_standard = data.get('expected_standard', '').strip()
    requires_numeric = bool(data.get('requires_numeric_reading', False))
    unit = data.get('unit', '').strip()
    is_critical = bool(data.get('is_critical', False))

    task = DailySOPTask.objects.create(
        restaurant=restaurant,
        branch=active_branch,
        title=title,
        department=department,
        shift_timing=shift_timing,
        description=description,
        expected_standard=expected_standard,
        requires_numeric_reading=requires_numeric,
        unit=unit,
        is_critical=is_critical,
        is_active=True,
    )

    return JsonResponse({
        'success': True,
        'task_id': task.id,
        'title': task.title,
        'message': f"Added new routine '{task.title}' to the SOP board."
    })


@hotel_admin_required
@require_POST
def hotel_api_delete_sop_task(request, restaurant_slug, ctx, pk):
    """
    Deletes or soft-deactivates an SOP routine in hotel admin.
    """
    restaurant = ctx['restaurant']
    task = get_object_or_404(
        DailySOPTask,
        id=pk,
        restaurant=restaurant
    )
    task_title = task.title
    task.delete()
    return JsonResponse({
        'success': True,
        'message': f"Routine '{task_title}' deleted successfully."
    })
