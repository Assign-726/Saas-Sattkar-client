from django.urls import path
from hotel import views_admin
from hotel import views_staff_hotel
from hotel import views_pms
from hotel import views_ops
from notifications import views as notification_views
from super_admin import views_logs
from guests import views as guest_views
from guests import views_growth
from integrations import views as integration_views
from finance import views as finance_views
from super_admin import views_roles
from restaurant_admin import views_events

app_name = 'hotel_admin'


def club(view):
    """Club & Events screens are shared with the Restaurant admin: run them behind the hotel guard,
    handing the hotel context over on the request (see restaurant_admin.views_events)."""
    @views_admin.hotel_admin_required
    def hotel_club_view(request, restaurant_slug, ctx, *args, **kwargs):
        request.hotel_ctx = ctx
        return view(request, restaurant_slug, *args, **kwargs)
    return hotel_club_view

urlpatterns = [
    # Club & Events (shared with the Restaurant admin)
    path('club-events/', club(views_events.club_events_list), name='club_events_list'),
    path('club-events/create/', club(views_events.club_event_create), name='club_event_create'),
    path('club-events/<int:pk>/edit/', club(views_events.club_event_edit), name='club_event_edit'),
    path('club-events/<int:pk>/delete/', club(views_events.club_event_delete), name='club_event_delete'),
    path('club-events/<int:pk>/toggle-publish/', club(views_events.club_event_toggle_publish), name='club_event_toggle_publish'),
    path('club-events/<int:pk>/duplicate/', club(views_events.club_event_duplicate), name='club_event_duplicate'),
    path('club-events/guestlist/', club(views_events.club_guestlist), name='club_guestlist'),
    path('club-events/<int:event_id>/guestlist/', club(views_events.club_guestlist), name='club_guestlist_event'),
    path('club-events/<int:event_id>/manual-booking/', club(views_events.club_manual_booking_create), name='club_manual_booking_create'),
    path('club-events/booking/<int:booking_id>/toggle-checkin/', club(views_events.club_booking_toggle_checkin), name='club_booking_toggle_checkin'),
    path('club-events/booking/<int:booking_id>/cancel/', club(views_events.club_booking_cancel), name='club_booking_cancel'),
    path('club-events/<int:event_id>/guestlist/export/', club(views_events.club_guestlist_export), name='club_guestlist_export'),
    path('club-events/floor-plan/', club(views_events.club_floor_plan), name='club_floor_plan'),
    path('club-events/<int:event_id>/floor-plan/', club(views_events.club_floor_plan), name='club_floor_plan_event'),
    path('club-events/<int:event_id>/floor-plan/api/', club(views_events.club_floor_plan_api), name='club_floor_plan_api'),
    path('club-events/<int:event_id>/floor-plan/assign/', club(views_events.club_floor_plan_assign_table), name='club_floor_plan_assign_table'),
    path('club-events/ticket-grid/', club(views_events.club_ticket_grid), name='club_ticket_grid'),
    path('club-events/<int:event_id>/ticket-grid/', club(views_events.club_ticket_grid), name='club_ticket_grid_event'),
    path('club-events/<int:event_id>/ticket-grid/api/', club(views_events.club_ticket_grid_api), name='club_ticket_grid_api'),
    path('club-events/gate-scanner/', club(views_events.club_gate_scanner), name='club_gate_scanner'),
    path('club-events/<int:event_id>/gate-scanner/', club(views_events.club_gate_scanner), name='club_gate_scanner_event'),
    path('guests/', guest_views.hotel_guest_list, name='guest_list'),
    path('guests/<int:pk>/', guest_views.hotel_guest_detail, name='guest_detail'),
    path('gift-cards/', guest_views.hotel_gift_card_list, name='gift_card_list'),
    path('gift-cards/<int:pk>/', guest_views.hotel_gift_card_detail, name='gift_card_detail'),
    path('brief/', guest_views.hotel_morning_brief, name='morning_brief'),
    path('feedback/', views_growth.hotel_feedback_list, name='feedback_list'),
    path('feedback/<int:pk>/', views_growth.hotel_feedback_detail, name='feedback_detail'),
    path('campaigns/', views_growth.hotel_campaign_list, name='campaign_list'),
    path('campaigns/new/', views_growth.hotel_campaign_new, name='campaign_new'),
    path('campaigns/<int:pk>/', views_growth.hotel_campaign_detail, name='campaign_detail'),
    path('activity-logs/', views_admin.hotel_admin_required(views_logs.hotel_activity_logs_view), name='activity_logs'),
    path('finance/expenses/', finance_views.hotel_expenses, name='expenses'),
    path('finance/expenses/<int:pk>/receipt/', finance_views.hotel_receipt, name='expense_receipt'),
    path('finance/profit-loss/', finance_views.hotel_profit_and_loss, name='profit_loss'),
    path('roles/', views_roles.hotel_roles, name='roles'),
    path('roles/new/', views_roles.hotel_role_new, name='role_new'),
    path('roles/<int:pk>/', views_roles.hotel_role_edit, name='role_edit'),
    path('integrations/', integration_views.hotel_overview, name='integrations'),
    path('integrations/distribution/', integration_views.hotel_distribution, name='integration_distribution'),
    path('integrations/keys/', integration_views.hotel_keys, name='integration_keys'),
    path('integrations/webhooks/', integration_views.hotel_webhooks, name='integration_webhooks'),
    path('integrations/webhooks/<int:pk>/', integration_views.hotel_webhook_detail, name='integration_webhook_detail'),
    path('integrations/webhooks/<int:pk>/deliveries/<int:delivery_pk>/', integration_views.hotel_delivery_json, name='integration_delivery'),
    path('integrations/docs/', integration_views.hotel_docs, name='integration_docs'),
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
    # Notification drawer (channel bookings and alerts)
    path('api/notifications/poll/', notification_views.poll, name='notifications_poll'),
    path('api/notifications/stream/', notification_views.stream, name='notifications_stream'),
    path('api/notifications/', notification_views.listing, name='notifications_list'),
    path('api/notifications/update/', notification_views.update, name='notifications_update'),
    # Booking folio, guest registration and stay changes
    path('bookings/<int:booking_id>/', views_pms.booking_detail, name='booking_detail'),
    path('bookings/<int:booking_id>/charges/add/', views_pms.booking_charge_add, name='booking_charge_add'),
    path('bookings/<int:booking_id>/charges/<int:charge_id>/void/', views_pms.booking_charge_void_action, name='booking_charge_void_action'),
    path('bookings/<int:booking_id>/payments/add/', views_pms.booking_payment_add, name='booking_payment_add'),
    path('bookings/<int:booking_id>/payments/<int:payment_id>/void/', views_pms.booking_payment_void_action, name='booking_payment_void_action'),
    path('bookings/<int:booking_id>/guest/', views_pms.booking_guest_update, name='booking_guest_update'),
    path('bookings/<int:booking_id>/dates/', views_pms.booking_dates_update, name='booking_dates_update'),
    path('bookings/<int:booking_id>/move/', views_pms.booking_move_action, name='booking_move_action'),
    path('bookings/<int:booking_id>/no-show/', views_pms.booking_no_show_action, name='booking_no_show_action'),
    path('bookings/<int:booking_id>/invoice/', views_pms.booking_invoice, name='booking_invoice'),
    path('bookings/<int:booking_id>/registration-card/', views_pms.booking_registration_card, name='booking_registration_card'),
    # Group bookings
    path('bookings/groups/', views_pms.booking_groups, name='booking_groups'),
    path('bookings/groups/new/', views_pms.booking_group_create, name='booking_group_create'),
    path('bookings/groups/<int:pk>/', views_pms.booking_group_detail, name='booking_group_detail'),
    path('bookings/groups/<int:pk>/update/', views_pms.booking_group_update, name='booking_group_update'),
    # City ledger (company accounts)
    path('finance/ledger/', views_pms.ledger, name='ledger'),
    path('finance/ledger/save/', views_pms.ledger_account_save, name='ledger_account_save'),
    path('finance/ledger/<int:pk>/', views_pms.ledger_account, name='ledger_account'),
    path('finance/ledger/<int:pk>/payments/add/', views_pms.ledger_payment_add, name='ledger_payment_add'),
    path('finance/ledger/<int:pk>/payments/<int:payment_id>/void/', views_pms.ledger_payment_void_action, name='ledger_payment_void_action'),
    # Housekeeping & maintenance (under /rooms/ so the Housekeeping role can reach them)
    path('rooms/housekeeping/', views_pms.room_housekeeping, name='room_housekeeping'),
    path('rooms/housekeeping/tasks/add/', views_pms.room_task_create, name='room_task_create'),
    path('rooms/housekeeping/tasks/stayovers/', views_pms.room_task_generate, name='room_task_generate'),
    path('rooms/housekeeping/tasks/<int:task_id>/', views_pms.room_task_update, name='room_task_update'),
    path('rooms/maintenance/', views_pms.room_maintenance, name='room_maintenance'),
    path('rooms/maintenance/add/', views_pms.room_issue_create, name='room_issue_create'),
    path('rooms/maintenance/<int:issue_id>/', views_pms.room_issue_update, name='room_issue_update'),
    # Revenue: rates, reports, night audit
    path('rates/', views_pms.rates, name='rates'),
    path('rates/save/', views_pms.rate_save, name='rate_save'),
    path('rates/<int:rule_id>/delete/', views_pms.rate_delete, name='rate_delete'),
    path('insights/', views_pms.management_dashboard, name='management_dashboard'),
    path('reports/', views_pms.reports_view, name='reports'),
    path('night-audit/', views_pms.night_audit, name='night_audit'),
    path('night-audit/close/', views_pms.night_audit_close, name='night_audit_close'),
    path('night-audit/<int:audit_id>/', views_pms.night_audit_detail, name='night_audit_detail'),

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
    path('settings/two-factor/', views_admin.two_factor_update, name='two_factor_update'),
    path('settings/test-email/', views_admin.hotel_test_email_view, name='test_email'),

    # Action centre
    path('action-centre/', views_ops.action_centre, name='action_centre'),

    # Guest services
    path('guest-requests/', views_ops.requests_view, name='requests'),
    path('guest-requests/add/', views_ops.request_add, name='request_add'),
    path('guest-requests/<int:pk>/action/', views_ops.request_action, name='request_action'),
    path('lost-found/', views_ops.lostfound, name='lostfound'),
    path('lost-found/add/', views_ops.lostfound_add, name='lostfound_add'),
    path('lost-found/<int:pk>/action/', views_ops.lostfound_action, name='lostfound_action'),
    path('laundry/', views_ops.laundry, name='laundry'),
    path('laundry/add/', views_ops.laundry_add, name='laundry_add'),
    path('laundry/<int:pk>/action/', views_ops.laundry_action, name='laundry_action'),
    path('laundry/prices/save/', views_ops.laundry_rate_save, name='laundry_rate_save'),
    path('spa/', views_ops.spa, name='spa'),
    path('spa/add/', views_ops.spa_add, name='spa_add'),
    path('spa/<int:pk>/action/', views_ops.spa_action, name='spa_action'),
    path('spa/setup/', views_ops.spa_setup, name='spa_setup'),
    path('spa/setup/save/', views_ops.spa_setup_save, name='spa_setup_save'),
    path('spa/setup/delete/', views_ops.spa_setup_delete, name='spa_setup_delete'),
    path('spa/setup/reset-catalog/', views_ops.spa_setup_reset_catalog, name='spa_setup_reset_catalog'),

    # Banquets & events
    path('banquets/', views_ops.banquets, name='banquets'),
    path('banquets/save/', views_ops.banquet_save, name='banquet_save'),
    path('banquets/setup/', views_ops.banquet_setup, name='banquet_setup'),
    path('banquets/setup/save/', views_ops.banquet_setup_save, name='banquet_setup_save'),
    path('banquets/<int:pk>/', views_ops.banquet_detail, name='banquet_detail'),
    path('banquets/<int:pk>/action/', views_ops.banquet_action, name='banquet_action'),
    path('banquets/<int:pk>/beo/', views_ops.banquet_beo, name='banquet_beo'),
    path('banquets/<int:pk>/payments/add/', views_ops.banquet_payment_add, name='banquet_payment_add'),
    path('banquets/<int:pk>/payments/<int:payment_id>/void/', views_ops.banquet_payment_action, name='banquet_payment_action'),

    # Sales, packages & promotions
    path('sales/', views_ops.sales, name='sales'),
    path('sales/save/', views_ops.sales_lead_save, name='sales_lead_save'),
    path('sales/<int:pk>/', views_ops.sales_lead, name='sales_lead'),
    path('sales/<int:pk>/action/', views_ops.sales_lead_action, name='sales_lead_action'),
    path('sales/<int:pk>/activities/add/', views_ops.sales_activity_add, name='sales_activity_add'),
    path('sales/activities/<int:activity_id>/action/', views_ops.sales_activity_action, name='sales_activity_action'),
    path('packages/', views_ops.packages, name='packages'),
    path('packages/save/', views_ops.package_save, name='package_save'),
    path('packages/promos/save/', views_ops.promo_save, name='promo_save'),
    path('bookings/<int:booking_id>/offers/add/', views_ops.booking_offer_add, name='booking_offer_add'),

    # Cashier
    path('finance/cashier/', views_ops.cashier, name='cashier'),
    path('finance/cashier/open/', views_ops.cashier_open, name='cashier_open'),
    path('finance/cashier/close/', views_ops.cashier_close, name='cashier_close'),
    path('finance/cashier/paid-outs/add/', views_ops.cashier_paidout_add, name='cashier_paidout_add'),

    # Back office
    path('security/', views_ops.security, name='security'),
    path('security/visitors/add/', views_ops.security_visitor_add, name='security_visitor_add'),
    path('security/visitors/<int:pk>/out/', views_ops.security_visitor_action, name='security_visitor_action'),
    path('security/incidents/add/', views_ops.security_incident_add, name='security_incident_add'),
    path('security/incidents/<int:pk>/close/', views_ops.security_incident_action, name='security_incident_action'),
    path('compliance/', views_ops.compliance, name='compliance'),
    path('compliance/save/', views_ops.compliance_save, name='compliance_save'),
    path('compliance/action/', views_ops.compliance_action, name='compliance_action'),
    path('assets/', views_ops.assets, name='assets'),
    path('assets/save/', views_ops.asset_save, name='asset_save'),
    path('assets/contracts/save/', views_ops.contract_save, name='contract_save'),
    path('energy/', views_ops.energy, name='energy'),
    path('energy/save/', views_ops.energy_save, name='energy_save'),

    # Forecast
    path('reports/forecast/', views_ops.reports_forecast, name='reports_forecast'),
    path('rates/suggestion/apply/', views_ops.rate_suggest_apply, name='rate_suggest_apply'),
]
