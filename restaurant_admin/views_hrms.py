import calendar
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import models, transaction
from django.utils import timezone

from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser
from restaurant_admin.views import resolve_restaurant, get_active_branch
from super_admin.modules import RESTAURANT, RESTAURANT_DEPARTMENTS, department_choices
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

logger = logging.getLogger(__name__)

# The restaurant shell manages restaurant-side departments; Housekeeping & Front Desk live in the Hotel PMS.
RESTAURANT_DEPARTMENT_CHOICES = department_choices(RESTAURANT)


def is_management_user(user):
    """Only managers and administrators can access staff operations and muster roll."""
    if not (user.is_authenticated and user.is_staff):
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'restaurant_profile', None)
    if not profile:
        return False
    role = getattr(user, 'role', None) or profile.role
    return role in [
        RestaurantUser.Role.GENERAL_MANAGER,
        RestaurantUser.Role.RESTAURANT_ADMIN,
        RestaurantUser.Role.HOTEL_ADMIN,
        RestaurantUser.Role.CLUB_ADMIN,
    ]


def number_to_words(n):
    """Utility to convert a rupee amount into readable words for salary slips."""
    try:
        n = int(round(float(n)))
        if n == 0:
            return "Zero Rupees Only"
        
        units = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
                 "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
                 "Seventeen", "Eighteen", "Nineteen"]
        tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

        def helper(num):
            if num == 0:
                return ""
            elif num < 20:
                return units[num] + " "
            elif num < 100:
                return tens[num // 10] + " " + units[num % 10] + (" " if num % 10 != 0 else "")
            elif num < 1000:
                return units[num // 100] + " Hundred " + helper(num % 100)
            elif num < 100000:
                return helper(num // 1000) + "Thousand " + helper(num % 1000)
            elif num < 10000000:
                return helper(num // 100000) + "Lakh " + helper(num % 100000)
            else:
                return helper(num // 10000000) + "Crore " + helper(num % 10000000)

        result = helper(n).strip()
        return f"{result} Rupees Only"
    except Exception:
        return ""


# ==============================================================================
# PHASE 1: FAST-TRACK DAILY MUSTER ROLL & DEPARTMENT ALLOCATION
# ==============================================================================

@login_required
def muster_roll_view(request, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Muster roll requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    # Date resolution
    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)
    is_today = (selected_date == date.today())

    # Department filter
    dept_filter = request.GET.get('department', 'ALL').strip()
    search_query = request.GET.get('q', '').strip()

    # Base ground staff queryset
    staff_qs = StaffMember.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS, is_active=True)
    if active_branch:
        staff_qs = staff_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    total_active_staff = staff_qs.count()

    if dept_filter and dept_filter != 'ALL':
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
            staff_member__department__in=RESTAURANT_DEPARTMENTS,
            date=selected_date,
            staff_member__in=staff_members
        ).select_related('staff_member')
    }

    # Aggregate daily status counters across ALL active staff for this date
    all_active_ids = StaffMember.objects.filter(
        restaurant=restaurant,
        department__in=RESTAURANT_DEPARTMENTS,
        is_active=True
    )
    if active_branch:
        all_active_ids = all_active_ids.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))
    all_active_ids = list(all_active_ids.values_list('id', flat=True))

    all_attendances_today = DailyAttendance.objects.filter(
        restaurant=restaurant,
            staff_member__department__in=RESTAURANT_DEPARTMENTS,
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

    # Department badges with counts
    dept_counts = []
    for dept_code, dept_label in RESTAURANT_DEPARTMENT_CHOICES:
        dept_total = StaffMember.objects.filter(restaurant=restaurant, department=dept_code, is_active=True).count()
        if dept_total > 0:
            dept_counts.append({
                'code': dept_code,
                'label': dept_label,
                'total': dept_total,
            })

    # Prepare rows for the template grid
    grid_rows = []
    for staff in staff_members:
        att = existing_attendance.get(staff.id)
        grid_rows.append({
            'staff': staff,
            'attendance': att,
            'is_marked': att is not None,
            'status': att.status if att else '',
            'shift_type': att.shift_type if att else staff.default_shift,
            'check_in': att.check_in_time.strftime('%H:%M') if (att and att.check_in_time) else '',
            'check_out': att.check_out_time.strftime('%H:%M') if (att and att.check_out_time) else '',
            'ot_hours': float(att.ot_hours) if (att and att.ot_hours) else 0.0,
            'penalty_amount': float(att.penalty_amount) if (att and att.penalty_amount) else 0.0,
            'notes': att.notes if att else '',
        })

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'active_menu': 'attendance_muster',
        'active_group': 'staff_operations',
        'selected_date': selected_date,
        'selected_date_str': selected_date.isoformat(),
        'prev_date_str': prev_date.isoformat(),
        'next_date_str': next_date.isoformat(),
        'is_today': is_today,
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
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
        'attendance_statuses': AttendanceStatus.choices,
    }
    return render(request, 'restaurant_admin/staff/muster_roll.html', context)


@login_required
@require_POST
def api_mark_all_present(request, restaurant_slug=None):
    """
    Exposes a 'Mark All Present' batch-creation endpoint to populate DailyAttendance
    records instantly for the selected date across all (or department-filtered) staff.
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    active_branch = get_active_branch(request, restaurant)

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    date_str = data.get('date', '').strip()
    dept_filter = data.get('department', 'ALL').strip()

    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date format'}, status=400)

    staff_qs = StaffMember.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS, is_active=True)
    if active_branch:
        staff_qs = staff_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    if dept_filter and dept_filter != 'ALL':
        staff_qs = staff_qs.filter(department=dept_filter)

    updated_count = 0
    with transaction.atomic():
        for staff in staff_qs:
            att, created = DailyAttendance.objects.get_or_create(
                staff_member=staff,
                date=att_date,
                defaults={
                    'restaurant': restaurant,
                    'branch': active_branch,
                    'status': AttendanceStatus.PRESENT,
                    'shift_type': staff.default_shift,
                    'ot_hours': Decimal('0.0'),
                    'penalty_amount': Decimal('0.00'),
                    'marked_by': request.user,
                }
            )
            if not created:
                # If already marked, update to PRESENT and set marked_by
                att.status = AttendanceStatus.PRESENT
                if not att.shift_type:
                    att.shift_type = staff.default_shift
                att.marked_by = request.user
                att.save()
            updated_count += 1

    return JsonResponse({
        'success': True,
        'count': updated_count,
        'message': f"Marked {updated_count} staff member(s) as Present for {att_date.strftime('%d %b %Y')}.",
        'date': att_date.isoformat(),
    })


@login_required
@require_POST
def api_save_single_attendance(request, restaurant_slug=None):
    """
    Dynamic fast auto-save endpoint utilizing Alpine.js or HTMX without full-page reloads.
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    active_branch = get_active_branch(request, restaurant)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Invalid JSON payload: {str(e)}'}, status=400)

    staff_id = data.get('staff_id')
    date_str = data.get('date')
    status_val = data.get('status', AttendanceStatus.PRESENT)
    shift_val = data.get('shift_type')
    ot_hours_val = data.get('ot_hours', 0.0)
    penalty_val = data.get('penalty_amount', 0.0)
    notes_val = data.get('notes', '').strip()
    check_in_str = data.get('check_in_time', '').strip()
    check_out_str = data.get('check_out_time', '').strip()

    staff = get_object_or_404(StaffMember, id=staff_id, restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)

    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid date'}, status=400)

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

    try:
        penalty_amount = Decimal(str(max(0.0, float(penalty_val))))
    except (ValueError, TypeError):
        penalty_amount = Decimal('0.00')

    valid_statuses = [c[0] for c in AttendanceStatus.choices]
    if status_val not in valid_statuses:
        status_val = AttendanceStatus.PRESENT

    valid_shifts = [c[0] for c in ShiftType.choices]
    if shift_val not in valid_shifts:
        shift_val = staff.default_shift

    att, _ = DailyAttendance.objects.update_or_create(
        staff_member=staff,
        date=att_date,
        defaults={
            'restaurant': restaurant,
            'branch': active_branch,
            'status': status_val,
            'shift_type': shift_val,
            'check_in_time': check_in_time,
            'check_out_time': check_out_time,
            'ot_hours': ot_hours,
            'penalty_amount': penalty_amount,
            'notes': notes_val,
            'marked_by': request.user,
        }
    )

    # Recalculate summary metrics for today
    today_records = DailyAttendance.objects.filter(restaurant=restaurant, staff_member__department__in=RESTAURANT_DEPARTMENTS, date=att_date)
    return JsonResponse({
        'success': True,
        'attendance_id': att.id,
        'status': att.status,
        'status_display': att.get_status_display(),
        'ot_hours': float(att.ot_hours),
        'penalty_amount': float(att.penalty_amount),
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


# ==============================================================================
# GROUND STAFF DIRECTORY
# ==============================================================================

@login_required
def ground_staff_list(request, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Staff directory requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    dept_filter = request.GET.get('department', '').strip()
    shift_filter = request.GET.get('shift', '').strip()
    search = request.GET.get('q', '').strip()

    staff_qs = StaffMember.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)
    if active_branch:
        staff_qs = staff_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    if dept_filter:
        staff_qs = staff_qs.filter(department=dept_filter)
    if shift_filter:
        staff_qs = staff_qs.filter(default_shift=shift_filter)
    if search:
        staff_qs = staff_qs.filter(
            models.Q(name__icontains=search) |
            models.Q(employee_id__icontains=search) |
            models.Q(designation__icontains=search) |
            models.Q(phone__icontains=search)
        )

    staff_list = staff_qs.order_by('department', 'name')

    # Department breakdown statistics
    total_staff = staff_list.count()
    total_monthly_payroll = staff_list.filter(is_active=True).aggregate(
        total_payroll=models.Sum('monthly_base_salary')
    )['total_payroll'] or Decimal('0.00')

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'staff_list': staff_list,
        'total_staff': total_staff,
        'total_monthly_payroll': total_monthly_payroll,
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
        'selected_department': dept_filter,
        'selected_shift': shift_filter,
        'search_query': search,
    }
    return render(request, 'restaurant_admin/staff/ground_staff_list.html', context)


@login_required
def ground_staff_create(request, restaurant_slug=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Creating staff requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        employee_id = request.POST.get('employee_id', '').strip()
        department = request.POST.get('department', Department.FLOOR)
        if department not in RESTAURANT_DEPARTMENTS:
            department = Department.FLOOR
        designation = request.POST.get('designation', '').strip()
        phone = request.POST.get('phone', '').strip()
        default_shift = request.POST.get('default_shift', ShiftType.GENERAL)
        salary_str = request.POST.get('monthly_base_salary', '18000')
        shift_hours_str = request.POST.get('standard_shift_hours', '9.0')
        joining_date_str = request.POST.get('joining_date', '')
        notes = request.POST.get('notes', '').strip()

        # Generate employee ID if empty
        if not employee_id:
            count = StaffMember.objects.filter(restaurant=restaurant).count() + 1
            employee_id = f"EMP-{count:03d}"

        if not name or not designation:
            messages.error(request, "Please provide full name and designation.")
            return render(request, 'restaurant_admin/staff/ground_staff_form.html', {
                'restaurant': restaurant,
                'rslug': restaurant.slug,
                'departments': RESTAURANT_DEPARTMENT_CHOICES,
                'shift_types': ShiftType.choices,
                'is_edit': False,
            })

        try:
            salary = Decimal(salary_str)
        except Exception:
            salary = Decimal('18000.00')

        try:
            shift_hours = Decimal(shift_hours_str)
        except Exception:
            shift_hours = Decimal('9.0')

        try:
            joining_date = datetime.strptime(joining_date_str, '%Y-%m-%d').date() if joining_date_str else date.today()
        except ValueError:
            joining_date = date.today()

        # Check unique employee ID
        if StaffMember.objects.filter(restaurant=restaurant, employee_id=employee_id).exists():
            messages.error(request, f"Employee ID '{employee_id}' is already assigned. Please use a unique ID.")
            return render(request, 'restaurant_admin/staff/ground_staff_form.html', {
                'restaurant': restaurant,
                'rslug': restaurant.slug,
                'departments': RESTAURANT_DEPARTMENT_CHOICES,
                'shift_types': ShiftType.choices,
                'is_edit': False,
            })

        StaffMember.objects.create(
            restaurant=restaurant,
            branch=active_branch,
            name=name,
            employee_id=employee_id,
            department=department,
            designation=designation,
            phone=phone,
            default_shift=default_shift,
            monthly_base_salary=salary,
            standard_shift_hours=shift_hours,
            joining_date=joining_date,
            notes=notes,
            is_active=True,
        )
        messages.success(request, f"Staff member '{name}' ({employee_id}) added successfully.")
        return redirect('restaurant_admin:ground_staff_list', restaurant_slug=restaurant.slug)

    # Next suggested ID
    suggested_id = f"EMP-{StaffMember.objects.filter(restaurant=restaurant).count() + 1:03d}"

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
        'is_edit': False,
        'suggested_id': suggested_id,
        'today_date': date.today().isoformat(),
    }
    return render(request, 'restaurant_admin/staff/ground_staff_form.html', context)


@login_required
def ground_staff_edit(request, restaurant_slug=None, pk=None):
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Staff editing requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    staff = get_object_or_404(StaffMember, id=pk, restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)

    if request.method == 'POST':
        staff.name = request.POST.get('name', '').strip() or staff.name
        staff.employee_id = request.POST.get('employee_id', '').strip() or staff.employee_id
        if request.POST.get('department') in RESTAURANT_DEPARTMENTS:
            staff.department = request.POST['department']
        staff.designation = request.POST.get('designation', '').strip() or staff.designation
        staff.phone = request.POST.get('phone', '').strip()
        staff.default_shift = request.POST.get('default_shift', staff.default_shift)
        staff.is_active = (request.POST.get('is_active') == 'on')
        staff.notes = request.POST.get('notes', '').strip()

        salary_str = request.POST.get('monthly_base_salary', '')
        if salary_str:
            try:
                staff.monthly_base_salary = Decimal(salary_str)
            except Exception:
                pass

        shift_hours_str = request.POST.get('standard_shift_hours', '')
        if shift_hours_str:
            try:
                staff.standard_shift_hours = Decimal(shift_hours_str)
            except Exception:
                pass

        staff.save()
        messages.success(request, f"Updated profile for {staff.name}.")
        return redirect('restaurant_admin:ground_staff_list', restaurant_slug=restaurant.slug)

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'rslug': restaurant.slug,
        'active_menu': 'ground_staff_directory',
        'active_group': 'staff_operations',
        'staff': staff,
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'shift_types': ShiftType.choices,
        'is_edit': True,
    }
    return render(request, 'restaurant_admin/staff/ground_staff_form.html', context)


@login_required
@require_POST
def ground_staff_toggle_active(request, restaurant_slug=None, pk=None):
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    staff = get_object_or_404(StaffMember, id=pk, restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)
    staff.is_active = not staff.is_active
    staff.save()
    status_str = "Active" if staff.is_active else "Inactive"
    messages.info(request, f"{staff.name} is now marked as {status_str}.")
    return redirect('restaurant_admin:ground_staff_list', restaurant_slug=restaurant.slug)


# ==============================================================================
# PHASE 2: AUTOMATED PAYROLL ENGINE & SALARY SLIPS
# ==============================================================================

@login_required
def payroll_dashboard(request, restaurant_slug=None):
    """
    Manager's monthly payroll review cockpit.
    Permits manual incentive or penalty additions before finalizing.
    """
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Payroll management requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    now = timezone.now()
    year = int(request.GET.get('year', now.year))
    month = int(request.GET.get('month', now.month))
    dept_filter = request.GET.get('department', 'ALL').strip()
    status_filter = request.GET.get('status', 'ALL').strip()

    days_in_month = calendar.monthrange(year, month)[1]
    month_name = calendar.month_name[month]

    # Payroll records for the selected month
    payroll_qs = MonthlyPayroll.objects.filter(
        restaurant=restaurant,
        staff_member__department__in=RESTAURANT_DEPARTMENTS,
        year=year,
        month=month
    ).select_related('staff_member', 'finalized_by')

    if active_branch:
        payroll_qs = payroll_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    if dept_filter and dept_filter != 'ALL':
        payroll_qs = payroll_qs.filter(staff_member__department=dept_filter)

    if status_filter and status_filter != 'ALL':
        payroll_qs = payroll_qs.filter(payment_status=status_filter)

    payrolls = list(payroll_qs.order_by('staff_member__department', 'staff_member__name'))

    # Aggregate summaries
    total_processed = len(payrolls)
    total_base_earned = sum(p.base_earned for p in payrolls)
    total_ot_amount = sum(p.ot_amount for p in payrolls)
    total_penalties = sum(p.penalties for p in payrolls)
    total_incentives = sum(p.incentives for p in payrolls)
    total_net_payable = sum(p.net_salary for p in payrolls)
    finalized_count = sum(1 for p in payrolls if p.is_finalized)
    paid_count = sum(1 for p in payrolls if p.payment_status == MonthlyPayroll.PaymentStatus.PAID)

    # Active staff count for comparison
    active_staff_count = StaffMember.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS, is_active=True).count()

    months_list = [(i, calendar.month_name[i]) for i in range(1, 13)]
    years_list = [now.year - 1, now.year, now.year + 1]

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'active_menu': 'payroll_dashboard',
        'active_group': 'staff_operations',
        'year': year,
        'month': month,
        'month_name': month_name,
        'days_in_month': days_in_month,
        'payrolls': payrolls,
        'total_processed': total_processed,
        'active_staff_count': active_staff_count,
        'unprocessed_count': max(0, active_staff_count - total_processed),
        'total_base_earned': total_base_earned,
        'total_ot_amount': total_ot_amount,
        'total_penalties': total_penalties,
        'total_incentives': total_incentives,
        'total_net_payable': total_net_payable,
        'finalized_count': finalized_count,
        'paid_count': paid_count,
        'months_list': months_list,
        'years_list': years_list,
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'selected_dept': dept_filter,
        'selected_status': status_filter,
        'payment_statuses': MonthlyPayroll.PaymentStatus.choices,
    }
    return render(request, 'restaurant_admin/staff/payroll_dashboard.html', context)


@login_required
@require_POST
def api_generate_monthly_payroll(request, restaurant_slug=None):
    """
    One-Click Automated Payroll Engine.
    Aggregates all daily muster inputs, OT hours, and calculates:
    1. Payable Days = Present Days + Weekly Offs + Paid Leaves + (0.5 * Half Days)
    2. Base Earned = (Monthly Base Salary / Days in Month) * Payable Days
    3. OT Amount = Total OT Hours * (Daily Wage / Standard Shift Hours)
    4. Net Salary Payable = Base Earned + OT Amount - Penalties + Incentives
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    active_branch = get_active_branch(request, restaurant)

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

    # Fetch all active staff
    staff_qs = StaffMember.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS, is_active=True)
    if active_branch:
        staff_qs = staff_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    processed_count = 0
    with transaction.atomic():
        for staff in staff_qs:
            # Query all attendance records for this month
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

            # Preserve existing manual incentives/penalties if already modified by manager
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

            # If newly created or penalties not set, initialize with aggregated daily penalties
            if created or payroll.penalties == Decimal('0.00'):
                payroll.penalties = daily_penalties_agg

            # Compute formula engine
            payroll.calculate_values()
            payroll.save()
            processed_count += 1

    return JsonResponse({
        'success': True,
        'count': processed_count,
        'message': f"Successfully generated payroll for {processed_count} staff members ({calendar.month_name[month]} {year}).",
    })


@login_required
@require_POST
def api_update_payroll_adjustment(request, restaurant_slug=None, pk=None):
    """
    Manager's monthly review screen adjustment endpoint.
    Permits manual incentive or penalty additions before finalizing.
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    payroll = get_object_or_404(MonthlyPayroll, id=pk, restaurant=restaurant, staff_member__department__in=RESTAURANT_DEPARTMENTS)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Invalid JSON: {str(e)}'}, status=400)

    try:
        if 'penalties' in data:
            payroll.penalties = Decimal(str(max(0.0, float(data['penalties']))))
        if 'incentives' in data:
            payroll.incentives = Decimal(str(max(0.0, float(data['incentives']))))
        if 'payment_status' in data:
            if data['payment_status'] in [c[0] for c in MonthlyPayroll.PaymentStatus.choices]:
                payroll.payment_status = data['payment_status']
                if payroll.payment_status == MonthlyPayroll.PaymentStatus.PAID and not payroll.payment_date:
                    payroll.payment_date = date.today()
        if 'payment_mode' in data:
            payroll.payment_mode = data['payment_mode'].strip()
        if 'remarks' in data:
            payroll.remarks = data['remarks'].strip()

        # Recalculate net salary with adjustments
        payroll.calculate_values()
        payroll.save()

        return JsonResponse({
            'success': True,
            'net_salary': float(payroll.net_salary),
            'base_earned': float(payroll.base_earned),
            'ot_amount': float(payroll.ot_amount),
            'penalties': float(payroll.penalties),
            'incentives': float(payroll.incentives),
            'payment_status': payroll.payment_status,
            'payment_status_display': payroll.get_payment_status_display(),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@login_required
@require_POST
def api_finalize_all_payroll(request, restaurant_slug=None):
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    year = int(data.get('year', timezone.now().year))
    month = int(data.get('month', timezone.now().month))

    updated = MonthlyPayroll.objects.filter(
        restaurant=restaurant,
        staff_member__department__in=RESTAURANT_DEPARTMENTS,
        year=year,
        month=month,
        is_finalized=False
    ).update(
        is_finalized=True,
        finalized_at=timezone.now(),
        finalized_by=request.user,
        payment_status=MonthlyPayroll.PaymentStatus.FINALIZED
    )

    return JsonResponse({
        'success': True,
        'count': updated,
        'message': f"Finalized {updated} payroll records for {calendar.month_name[month]} {year}.",
    })


@login_required
def salary_slip_view(request, restaurant_slug=None, pk=None):
    """
    Clean, printable PDF/HTML template for the Salary Slip detailing
    Gross Pay, Days Worked, Overtime Earned, and final net amount.
    """
    if not is_management_user(request.user):
        messages.error(request, "Access denied: Salary slips require manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    payroll = get_object_or_404(MonthlyPayroll, id=pk, restaurant=restaurant, staff_member__department__in=RESTAURANT_DEPARTMENTS)
    staff = payroll.staff_member

    # Format net amount in words
    net_in_words = number_to_words(payroll.net_salary)

    total_gross_earnings = (payroll.base_earned + payroll.ot_amount + payroll.incentives).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_deductions = payroll.penalties

    context = {
        'restaurant': restaurant,
        'payroll': payroll,
        'staff': staff,
        'rslug': restaurant.slug,
        'month_name': calendar.month_name[payroll.month],
        'net_in_words': net_in_words,
        'total_gross_earnings': total_gross_earnings,
        'total_deductions': total_deductions,
        'printed_at': timezone.now(),
        'active_menu': 'payroll_dashboard',
        'active_group': 'staff_operations',
    }
    return render(request, 'restaurant_admin/staff/salary_slip.html', context)


# ==============================================================================
# PHASE 3: OPERATIONAL TASK SOP BOARD
# ==============================================================================

@login_required
def sop_dashboard_view(request, restaurant_slug=None):
    """
    Responsive tablet view for the manager to inspect, check off, and verify
    daily hospitality SOP duties throughout the shift.
    """
    if not is_management_user(request.user):
        messages.error(request, "Access denied: SOP dashboard requires manager privileges.")
        return redirect('restaurant_admin:dashboard', restaurant_slug=restaurant_slug)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    active_branch = get_active_branch(request, restaurant)

    # Date resolution
    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    dept_filter = request.GET.get('department', 'ALL').strip()
    timing_filter = request.GET.get('timing', 'ALL').strip()

    # Fetch active templates for this branch or venue-wide
    task_qs = DailySOPTask.objects.filter(restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS, is_active=True)
    if active_branch:
        task_qs = task_qs.filter(models.Q(branch=active_branch) | models.Q(branch__isnull=True))

    total_tasks_count = task_qs.count()

    if dept_filter and dept_filter != 'ALL':
        task_qs = task_qs.filter(department=dept_filter)

    if timing_filter and timing_filter != 'ALL':
        task_qs = task_qs.filter(shift_timing=timing_filter)

    tasks = list(task_qs.order_by('department', 'shift_timing', 'sort_order', 'title'))

    # Load existing logs for the selected date
    existing_logs = {
        log.sop_task_id: log
        for log in DailyTaskLog.objects.filter(
            restaurant=restaurant,
            date=selected_date,
            sop_task__in=tasks
        ).select_related('completed_by')
    }

    # Prepare display items
    task_items = []
    completed_count = 0
    flagged_count = 0
    skipped_count = 0
    pending_count = 0

    for task in tasks:
        log = existing_logs.get(task.id)
        status = log.status if log else DailyTaskLog.TaskStatus.PENDING
        if status == DailyTaskLog.TaskStatus.COMPLETED:
            completed_count += 1
        elif status == DailyTaskLog.TaskStatus.FLAGGED_ISSUE:
            flagged_count += 1
        elif status == DailyTaskLog.TaskStatus.SKIPPED:
            skipped_count += 1
        else:
            pending_count += 1

        task_items.append({
            'task': task,
            'log': log,
            'status': status,
            'numeric_reading': log.numeric_reading if log else '',
            'verifier_notes': log.verifier_notes if log else '',
            'completed_at': log.completed_at if log else None,
            'completed_by': log.completed_by.username if (log and log.completed_by) else '',
        })

    # Overall completion percentage for the department/timing scope
    total_scoped = len(tasks)
    completion_rate = int((completed_count / total_scoped * 100)) if total_scoped > 0 else 0

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    context = {
        'restaurant': restaurant,
        'current_restaurant': restaurant,
        'current_branch': active_branch,
        'rslug': restaurant.slug,
        'active_menu': 'sop_dashboard',
        'active_group': 'staff_operations',
        'selected_date': selected_date,
        'selected_date_str': selected_date.isoformat(),
        'prev_date_str': prev_date.isoformat(),
        'next_date_str': next_date.isoformat(),
        'is_today': (selected_date == date.today()),
        'task_items': task_items,
        'total_scoped': total_scoped,
        'total_tasks_count': total_tasks_count,
        'completed_count': completed_count,
        'flagged_count': flagged_count,
        'skipped_count': skipped_count,
        'pending_count': pending_count,
        'completion_rate': completion_rate,
        'departments': RESTAURANT_DEPARTMENT_CHOICES,
        'shift_timings': SOPShiftTiming.choices,
        'selected_dept': dept_filter,
        'selected_timing': timing_filter,
    }
    return render(request, 'restaurant_admin/staff/sop_dashboard.html', context)


@login_required
@require_POST
def api_toggle_sop_task_status(request, restaurant_slug=None):
    """
    One-tap SOP check-off and verification endpoint for tablet view.
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    active_branch = get_active_branch(request, restaurant)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Invalid JSON: {str(e)}'}, status=400)

    task_id = data.get('task_id')
    date_str = data.get('date')
    status_val = data.get('status', DailyTaskLog.TaskStatus.COMPLETED)
    numeric_reading = data.get('numeric_reading', '').strip()
    verifier_notes = data.get('verifier_notes', '').strip()

    task = get_object_or_404(DailySOPTask, id=task_id, restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)

    try:
        log_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        log_date = date.today()

    log, _ = DailyTaskLog.objects.get_or_create(
        sop_task=task,
        date=log_date,
        defaults={
            'restaurant': restaurant,
            'branch': active_branch,
            'status': status_val,
            'numeric_reading': numeric_reading,
            'verifier_notes': verifier_notes,
            'completed_at': timezone.now() if status_val == DailyTaskLog.TaskStatus.COMPLETED else None,
            'completed_by': request.user,
        }
    )

    log.status = status_val
    if numeric_reading:
        log.numeric_reading = numeric_reading
    if verifier_notes:
        log.verifier_notes = verifier_notes

    if status_val == DailyTaskLog.TaskStatus.COMPLETED:
        log.completed_at = timezone.now()
        log.completed_by = request.user
    elif status_val == DailyTaskLog.TaskStatus.PENDING:
        log.completed_at = None
        log.completed_by = None

    log.save()

    # Recalculate daily stats for responsive tablet UI
    all_logs = DailyTaskLog.objects.filter(restaurant=restaurant, date=log_date)
    return JsonResponse({
        'success': True,
        'task_id': task.id,
        'status': log.status,
        'status_display': log.get_status_display(),
        'numeric_reading': log.numeric_reading,
        'verifier_notes': log.verifier_notes,
        'completed_at': log.completed_at.strftime('%H:%M') if log.completed_at else None,
        'completed_by': request.user.username if log.completed_by else '',
        'stats': {
            'completed': all_logs.filter(status=DailyTaskLog.TaskStatus.COMPLETED).count(),
            'flagged': all_logs.filter(status=DailyTaskLog.TaskStatus.FLAGGED_ISSUE).count(),
            'skipped': all_logs.filter(status=DailyTaskLog.TaskStatus.SKIPPED).count(),
        }
    })


@login_required
@require_POST
def api_seed_default_sops(request, restaurant_slug=None):
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    active_branch = get_active_branch(request, restaurant)
    count = seed_standard_hospitality_sops(restaurant, active_branch)
    return JsonResponse({
        'success': True,
        'count': count,
        'message': f"Pre-seeded {count} hospitality standard operating procedure (SOP) templates.",
    })


@login_required
@require_POST
def api_create_sop_task(request, restaurant_slug=None):
    """
    Manager endpoint to add a new custom SOP duty / checklist routine.
    """
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    active_branch = get_active_branch(request, restaurant)

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    title = data.get('title', '').strip()
    if not title:
        return JsonResponse({'success': False, 'error': 'Task title is required.'}, status=400)

    department = data.get('department', Department.KITCHEN)
    if department not in RESTAURANT_DEPARTMENTS:
        department = Department.KITCHEN
    shift_timing = data.get('shift_timing', SOPShiftTiming.MORNING_PREP)
    description = data.get('description', '').strip()
    expected_standard = data.get('expected_standard', '').strip()
    requires_reading = bool(data.get('requires_numeric_reading', False))
    unit = data.get('unit', '').strip()
    is_critical = bool(data.get('is_critical', False))

    task = DailySOPTask.objects.create(
        restaurant=restaurant,
        branch=active_branch,
        department=department,
        shift_timing=shift_timing,
        title=title,
        description=description,
        expected_standard=expected_standard,
        requires_numeric_reading=requires_reading,
        unit=unit,
        is_critical=is_critical,
        is_active=True
    )

    return JsonResponse({
        'success': True,
        'task_id': task.id,
        'title': task.title,
        'message': f"SOP Task '{task.title}' created successfully.",
    })


@login_required
@require_POST
def api_delete_sop_task(request, restaurant_slug=None, pk=None):
    if not is_management_user(request.user):
        return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return JsonResponse({'success': False, 'error': 'Invalid venue'}, status=400)

    task = get_object_or_404(DailySOPTask, id=pk, restaurant=restaurant, department__in=RESTAURANT_DEPARTMENTS)
    task_title = task.title
    task.delete()
    return JsonResponse({'success': True, 'message': f"SOP Task '{task_title}' deleted successfully."})



# ==============================================================================
# PRE-SEEDED HOSPITALITY TEMPLATES & GROUND STAFF INITIALIZER
# ==============================================================================

def seed_sample_hospitality_staff(restaurant, branch=None):
    """
    Automatic staff seeding disabled as per requirements.
    Staff members are registered manually by managers.
    """
    return 0


def seed_standard_hospitality_sops(restaurant, branch=None):
    """
    Pre-seeds exactly 2 standard hospitality SOPs for the first time
    (1 Kitchen walk-in temperature check + 1 Dining floor opening inspection).
    """
    templates = [
        # --- KITCHEN SAMPLE SOP ---
        {
            'department': Department.KITCHEN,
            'shift_timing': SOPShiftTiming.MORNING_PREP,
            'title': 'Walk-in Freezer & Chiller Temp Log',
            'description': 'Inspect main walk-in chiller (safe 1°C - 4°C) and deep freezer (-18°C or lower). Record reading.',
            'expected_standard': 'Chiller <= 4°C | Freezer <= -18°C',
            'requires_numeric_reading': True,
            'unit': '°C',
            'is_critical': True,
            'sort_order': 10,
        },
        # --- FLOOR / FOH SAMPLE SOP ---
        {
            'department': Department.FLOOR,
            'shift_timing': SOPShiftTiming.MORNING_PREP,
            'title': 'Dining Floor Table Alignment & Cutlery Polish Audit',
            'description': 'Inspect all dining tables, chair alignment, clean cloth napkins, and spotless polished silverware.',
            'expected_standard': 'Zero water spots on glassware, tables wobble-free',
            'requires_numeric_reading': False,
            'is_critical': False,
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
