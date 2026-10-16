from django.urls import path
from hotel import views_admin
from hotel import views_staff_hotel
from super_admin import views_logs

app_name = 'hotel_admin'

urlpatterns = [
    path('activity-logs/', views_admin.hotel_admin_required(views_logs.hotel_activity_logs_view), name='activity_logs'),
    # Dashboard & Front Desk
    path('', views_admin.dashboard_view, name='dashboard'),
    path('tape-chart/', views_admin.tape_chart_view, name='tape_chart'),

    # Room Inventory & Housekeeping
    path('rooms/', views_admin.rooms_view, name='rooms'),
    path('rooms/create-edit/', views_admin.room_create_edit_action, name='room_create_edit'),
    path('rooms/<int:room_id>/status/', views_admin.room_status_update_action, name='room_status_update'),

    # Room Categories & Tariffs
    path('categories/', views_admin.categories_view, name='categories'),
    path('categories/create-edit/', views_admin.category_create_edit_action, name='category_create_edit'),
    path('categories/<int:category_id>/cover/delete/', views_admin.category_delete_cover_action, name='category_delete_cover'),
    path('categories/images/<int:image_id>/delete/', views_admin.category_delete_image_action, name='category_delete_image'),
    path('categories/images/<int:image_id>/set-cover/', views_admin.category_set_cover_action, name='category_set_cover'),
    path('categories/<int:category_id>/videos/<int:video_idx>/delete/', views_admin.category_delete_video_action, name='category_delete_video'),

    # Bookings & Guest Folios
    path('bookings/', views_admin.bookings_view, name='bookings'),
    path('bookings/create/', views_admin.booking_create_action, name='booking_create'),
    path('bookings/<int:booking_id>/check-in/', views_admin.booking_check_in_action, name='booking_check_in'),
    path('bookings/<int:booking_id>/check-out/', views_admin.booking_check_out_action, name='booking_check_out'),
    path('bookings/<int:booking_id>/cancel/', views_admin.booking_cancel_action, name='booking_cancel'),

    # Hotel Staff & Operations Team (PMS User Accounts)
    path('staff/', views_admin.staff_list_view, name='staff_list'),
    path('staff/create/', views_admin.staff_create_view, name='staff_create'),
    path('staff/<int:user_id>/edit/', views_admin.staff_edit_view, name='staff_edit'),
    path('staff/<int:user_id>/toggle-active/', views_admin.staff_toggle_active_view, name='staff_toggle_active'),
    path('staff/<int:user_id>/delete/', views_admin.staff_delete_view, name='staff_delete'),

    # Phase 1: Daily Muster Roll & Shift Attendance for Hotel
    path('staff/attendance/', views_staff_hotel.hotel_muster_roll_view, name='muster_roll'),
    path('staff/attendance/api/mark-all-present/', views_staff_hotel.hotel_api_mark_all_present, name='api_mark_all_present'),
    path('staff/attendance/api/save/', views_staff_hotel.hotel_api_save_single_attendance, name='api_save_single_attendance'),

    # Phase 2: Ground Staff Directory (Housekeeping, Room Attendants, Bellhops)
    path('staff/ground-staff/', views_staff_hotel.hotel_ground_staff_list, name='ground_staff_list'),
    path('staff/ground-staff/create/', views_staff_hotel.hotel_ground_staff_create, name='ground_staff_create'),
    path('staff/ground-staff/<int:pk>/edit/', views_staff_hotel.hotel_ground_staff_edit, name='ground_staff_edit'),
    path('staff/ground-staff/<int:pk>/toggle-active/', views_staff_hotel.hotel_ground_staff_toggle_active, name='ground_staff_toggle_active'),

    # Phase 3: Automated Monthly Payroll Engine & Printable Salary Slips
    path('staff/payroll/', views_staff_hotel.hotel_payroll_dashboard, name='payroll_dashboard'),
    path('staff/payroll/api/generate/', views_staff_hotel.hotel_api_generate_monthly_payroll, name='api_generate_monthly_payroll'),
    path('staff/payroll/<int:pk>/update-adjustment/', views_staff_hotel.hotel_api_update_payroll_adjustment, name='api_update_payroll_adjustment'),
    path('staff/payroll/api/finalize-all/', views_staff_hotel.hotel_api_finalize_all_payroll, name='api_finalize_all_payroll'),
    path('staff/payroll/<int:pk>/salary-slip/', views_staff_hotel.hotel_salary_slip_view, name='salary_slip_view'),

    # Phase 4: Hotel Operational Task SOP Board
    path('staff/sop/', views_staff_hotel.hotel_sop_dashboard_view, name='sop_dashboard'),
    path('staff/sop/api/toggle-status/', views_staff_hotel.hotel_api_toggle_sop_task_status, name='api_toggle_sop_task_status'),
    path('staff/sop/api/seed-defaults/', views_staff_hotel.hotel_api_seed_default_sops, name='api_seed_default_sops'),
    path('staff/sop/api/create-task/', views_staff_hotel.hotel_api_create_sop_task, name='api_create_sop_task'),
    path('staff/sop/<int:pk>/delete/', views_staff_hotel.hotel_api_delete_sop_task, name='api_delete_sop_task'),

    # Travel Desk & Cab Management
    path('cabs/', views_admin.cabs_dispatch_view, name='cabs_dispatch'),
    path('cabs/packages/create-edit/', views_admin.cab_package_create_edit_action, name='cab_package_create_edit'),
    path('cabs/packages/<int:package_id>/delete/', views_admin.cab_package_delete_action, name='cab_package_delete'),
    path('cabs/fleet/create-edit/', views_admin.cab_fleet_create_edit_action, name='cab_fleet_create_edit'),
    path('cabs/fleet/<int:vehicle_id>/delete/', views_admin.cab_fleet_delete_action, name='cab_fleet_delete'),
    path('cabs/fleet/<int:vehicle_id>/gallery/delete/', views_admin.cab_fleet_delete_image_action, name='cab_fleet_delete_image'),
    path('cabs/bookings/<int:cab_id>/assign-driver/', views_admin.cab_assign_driver_action, name='cab_assign_driver'),
    path('cabs/bookings/<int:cab_id>/reschedule/', views_admin.cab_reschedule_action, name='cab_reschedule'),
    path('cabs/bookings/<int:cab_id>/status/', views_admin.cab_status_action, name='cab_status'),
    path('cabs/bookings/<int:cab_id>/payment-status/', views_admin.cab_payment_status_action, name='cab_payment_status'),
    path('cabs/bookings/manual-create/', views_admin.cab_manual_create_action, name='cab_manual_create'),

    # Hotel Configuration & Settings
    path('settings/', views_admin.hotel_settings_view, name='settings'),
    path('settings/test-email/', views_admin.hotel_test_email_view, name='test_email'),
]
