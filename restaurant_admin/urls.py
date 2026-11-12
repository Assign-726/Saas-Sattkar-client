from django.urls import path
from restaurant_admin import views
from restaurant_admin import views_menu_io
from restaurant_admin import views_stock_io
from super_admin import views_logs
from guests import views as guest_views
from guests import views_growth
from integrations import views as integration_views
from integrations.delivery import views as delivery_views
from notifications import views as notification_views
from finance import views as finance_views
from super_admin import views_roles
from restaurant_admin import views_reservations
from restaurant_admin import views_controls
from restaurant_admin import views_procurement
from restaurant_admin import floor_plan_views
from restaurant_admin import views_waiter
from restaurant_admin import views_events
from restaurant_admin import views_staff
from restaurant_admin import views_hrms
from restaurant_admin import views_pos_shifts

app_name = 'restaurant_admin'

urlpatterns = [
    # Zomato & Swiggy — separate workspace per platform, per branch
    path('delivery/', delivery_views.home, name='delivery_home'),
    path('delivery/<str:platform>/', delivery_views.dashboard, name='delivery_dashboard'),
    path('delivery/<str:platform>/live/', delivery_views.live, name='delivery_live'),
    path('delivery/<str:platform>/store/', delivery_views.store, name='delivery_store'),
    path('delivery/<str:platform>/orders/', delivery_views.orders, name='delivery_orders'),
    path('delivery/<str:platform>/orders/<int:pk>/', delivery_views.order_detail, name='delivery_order_detail'),
    path('delivery/<str:platform>/orders/<int:pk>/action/', delivery_views.order_action, name='delivery_order_action'),
    path('delivery/<str:platform>/menu/', delivery_views.menu, name='delivery_menu'),
    path('delivery/<str:platform>/menu/sync/', delivery_views.menu_sync, name='delivery_menu_sync'),
    path('delivery/<str:platform>/menu/export/', delivery_views.menu_export, name='delivery_menu_export'),
    path('delivery/<str:platform>/payouts/', delivery_views.payouts, name='delivery_payouts'),
    path('delivery/<str:platform>/payouts/<int:pk>/', delivery_views.payout_detail, name='delivery_payout_detail'),
    path('delivery/<str:platform>/logs/', delivery_views.logs, name='delivery_logs'),
    path('delivery/<str:platform>/settings/', delivery_views.settings_view, name='delivery_settings'),
    path('delivery/<str:platform>/simulate/', delivery_views.simulate, name='delivery_simulate'),
    path('guests/', guest_views.guest_list, name='guest_list'),
    path('guests/<int:pk>/', guest_views.guest_detail, name='guest_detail'),
    path('gift-cards/', guest_views.gift_card_list, name='gift_card_list'),
    path('gift-cards/<int:pk>/', guest_views.gift_card_detail, name='gift_card_detail'),
    path('brief/', guest_views.morning_brief, name='morning_brief'),
    path('guests/pos-lookup/', guest_views.pos_lookup, name='guest_pos_lookup'),
    path('feedback/', views_growth.feedback_list, name='feedback_list'),
    path('feedback/<int:pk>/', views_growth.feedback_detail, name='feedback_detail'),
    path('campaigns/', views_growth.campaign_list, name='campaign_list'),
    path('campaigns/new/', views_growth.campaign_new, name='campaign_new'),
    path('campaigns/<int:pk>/', views_growth.campaign_detail, name='campaign_detail'),
    path('activity-logs/', views_logs.restaurant_activity_logs, name='activity_logs'),
    path('finance/expenses/', finance_views.expenses, name='expenses'),
    path('finance/expenses/<int:pk>/receipt/', finance_views.receipt, name='expense_receipt'),
    path('finance/profit-loss/', finance_views.profit_and_loss, name='profit_loss'),
    path('roles/', views_roles.roles, name='roles'),
    path('roles/new/', views_roles.role_new, name='role_new'),
    path('roles/<int:pk>/', views_roles.role_edit, name='role_edit'),
    path('integrations/', integration_views.overview, name='integrations'),
    path('integrations/keys/', integration_views.keys, name='integration_keys'),
    path('integrations/webhooks/', integration_views.webhooks, name='integration_webhooks'),
    path('integrations/webhooks/<int:pk>/', integration_views.webhook_detail, name='integration_webhook_detail'),
    path('integrations/webhooks/<int:pk>/deliveries/<int:delivery_pk>/', integration_views.delivery_json, name='integration_delivery'),
    path('integrations/docs/', integration_views.docs, name='integration_docs'),
    # Test URLs
    
    # Unified Transaction Desk
    path('transactions/create/', views.transaction_desk_create, name='transaction_desk_create'),

    # Branch Switching
    path('switch-branch/', views.switch_branch, name='switch_branch'),

    # Dashboard & Analytics
    path('', views.dashboard, name='dashboard'),
    path('analytics/', views.sales_analytics, name='sales_analytics'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/two-factor/', views.two_factor_update, name='two_factor_update'),
    path('settings/test-email/', views.restaurant_test_email_view, name='test_email'),
    path('settings/custom-charges/save/', views.custom_charge_save, name='custom_charge_save'),
    path('settings/custom-charges/<int:pk>/delete/', views.custom_charge_delete, name='custom_charge_delete'),
    
    # Delivery Challans Management
    path('challans/', views.challan_list, name='challan_list'),
    path('challans/export/', views_stock_io.challan_export, name='challan_export'),
    path('challans/create/', views.challan_create, name='challan_create'),
    path('challans/<int:pk>/', views.challan_detail, name='challan_detail'),
    path('challans/<int:pk>/receive/', views.challan_mark_received, name='challan_mark_received'),
    path('challans/<int:pk>/cancel/', views.challan_cancel, name='challan_cancel'),
    
    # Floor Plan & Table Management
    path('floor-plan/', floor_plan_views.floor_plan_dashboard, name='floor_plan_dashboard'),
    # Waiter / Captain: live table board -> POS (send to kitchen only) -> ask the cashier for the bill
    path('waiter/tables/', views_waiter.waiter_tables, name='waiter_tables'),
    path('waiter/api/tables/', views_waiter.waiter_tables_api, name='waiter_tables_api'),
    path('waiter/tables/<int:pk>/open/', views_waiter.waiter_open_table, name='waiter_open_table'),
    path('waiter/orders/<int:pk>/request-bill/', views_waiter.waiter_request_bill, name='waiter_request_bill'),
    path('api/floor-plan/', floor_plan_views.floor_plan_api, name='floor_plan_api'),
    path('api/floor-plan/save-position/', floor_plan_views.floor_plan_save_position_api, name='floor_plan_save_position_api'),
    path('api/floor-plan/book-party/', floor_plan_views.floor_plan_book_party_api, name='floor_plan_book_party_api'),
    path('api/floor-plan/release-party/', floor_plan_views.floor_plan_release_party_api, name='floor_plan_release_party_api'),
    path('api/floor-plan/merge-tables/', floor_plan_views.floor_plan_merge_tables_api, name='floor_plan_merge_tables_api'),
    path('zones/', floor_plan_views.zone_list, name='zone_list'),
    path('zones/add/', floor_plan_views.zone_add, name='zone_add'),
    path('zones/<int:pk>/edit/', floor_plan_views.zone_edit, name='zone_edit'),
    path('zones/<int:pk>/delete/', floor_plan_views.zone_delete, name='zone_delete'),
    path('tables/', floor_plan_views.table_list, name='table_list'),
    path('tables/add/', floor_plan_views.table_add, name='table_add'),
    path('tables/regenerate-qrs/', floor_plan_views.regenerate_table_qrs, name='regenerate_table_qrs'),
    path('tables/<int:pk>/edit/', floor_plan_views.table_edit, name='table_edit'),
    path('tables/<int:pk>/delete/', floor_plan_views.table_delete, name='table_delete'),
    
    # Menu Management
    path('menu/', views.menu_list, name='menu_list'),
    path('menu/add/', views.menu_add, name='menu_add'),
    path('menu/quick-add/', views.menu_quick_add_api, name='menu_quick_add_api'),
    path('menu/bulk-add/', views.menu_bulk_add, name='menu_bulk_add'),
    path('menu/export/', views_menu_io.menu_export, name='menu_export'),
    path('menu/import/', views_menu_io.menu_import, name='menu_import'),
    path('menu/<int:pk>/edit/', views.menu_edit, name='menu_edit'),
    path('menu/<int:pk>/delete/', views.menu_delete, name='menu_delete'),
    path('menu/<int:pk>/toggle-availability/', views.menu_toggle_availability, name='menu_toggle_availability'),
    
    # Order Management
    path('orders/', views.order_list, name='order_list'),
    path('orders/records/', views.order_records, name='order_records'),
    path('orders/records/export/', views.order_records_export, name='order_records_export'),
    path('orders/create/', views.order_create_manual, name='order_create_manual'),
    path('orders/<int:pk>/edit/', views.order_edit_manual, name='order_edit_manual'),
    path('orders/stream/', views.orders_stream, name='orders_stream'),
    path('api/notifications/poll/', notification_views.poll, name='order_notifications_poll'),
    path('api/notifications/stream/', notification_views.stream, name='order_notifications_stream'),
    path('api/notifications/', notification_views.listing, name='notifications_list'),
    path('api/notifications/update/', notification_views.update, name='notifications_update'),
    path('orders/<int:pk>/', views.order_detail, name='order_detail'),
    path('orders/<int:pk>/update-status/', views.order_update_status, name='order_update_status'),
    path('orders/<int:pk>/invoice/', views.order_invoice, name='order_invoice'),
    path('orders/<int:pk>/kot/', views.order_kot, name='order_kot'),
    path('orders/<int:pk>/token-bill/', views.order_token_bill, name='order_token_bill'),
    path('orders/<int:pk>/prep-time/', views.order_set_prep_time, name='order_set_prep_time'),
    path('orders/<int:pk>/mark-ready/', views.order_mark_ready, name='order_mark_ready'),
    # Kitchen Display System (KDS)
    path('kitchen/', views.kitchen_kds_view, name='kitchen_kds'),
    path('kitchen/api/orders/', views.kitchen_kds_orders_api, name='kitchen_kds_orders_api'),
    path('kitchen/api/orders/<int:pk>/action/', views.kitchen_kds_action_api, name='kitchen_kds_action_api'),
    path('kitchen/api/orders/<int:order_pk>/items/<int:item_pk>/toggle/', views.kitchen_kds_item_toggle_api, name='kitchen_kds_item_toggle_api'),
    path('orders/draft/', views.pos_draft_order_api, name='pos_draft_order_api'),
    path('orders/draft/sync-items/', views.pos_sync_draft_items_api, name='pos_sync_draft_items_api'),
    path('orders/gst-company/', views.pos_gst_company_api, name='pos_gst_company_api'),
    path('orders/draft/finalize/', views.pos_finalize_order_api, name='pos_finalize_order_api'),
    path('orders/<int:pk>/settle/', views.order_settle_api, name='order_settle_api'),
    path('orders/api/smart-menu/', views.pos_smart_menu_api, name='pos_smart_menu_api'),
    path('api/orders/omni-search/', views.order_omni_search_api, name='order_omni_search_api'),
    path('api/detect-location/', views.detect_location_ip_api, name='detect_location_ip_api'),
    path('orders/<int:pk>/credit-note/', views.order_credit_note_api, name='order_credit_note_api'),
    path('orders/credit-note/', views.credit_note_desk, name='credit_note_desk'),
    path('credit-notes/<int:pk>/thermal/', views.credit_note_thermal_view, name='credit_note_thermal_view'),

    # POS Counter & Shift Management
    path('counters/', views_pos_shifts.pos_counter_list, name='pos_counter_list'),
    path('counters/api/save/', views_pos_shifts.pos_counter_save_api, name='pos_counter_save_api'),
    path('counters/<int:pk>/toggle/', views_pos_shifts.pos_counter_toggle_api, name='pos_counter_toggle_api'),
    path('counters/<int:pk>/delete/', views_pos_shifts.pos_counter_delete_api, name='pos_counter_delete_api'),
    path('shifts/api/active/', views_pos_shifts.pos_active_shift_api, name='pos_active_shift_api'),
    path('shifts/api/open/', views_pos_shifts.pos_open_shift_api, name='pos_open_shift_api'),
    path('shifts/api/summary/', views_pos_shifts.pos_shift_summary_api, name='pos_shift_summary_api'),
    path('shifts/api/close/', views_pos_shifts.pos_close_shift_api, name='pos_close_shift_api'),
    path('shifts/api/<int:shift_id>/close/', views_pos_shifts.pos_close_shift_api, name='pos_close_shift_id_api'),
    path('shifts/', views_pos_shifts.pos_shift_history_view, name='pos_shift_history'),
    path('shifts/<int:pk>/z-report/', views_pos_shifts.pos_shift_z_report_view, name='pos_shift_z_report'),

    # Inventory & Purchases Management
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/export/', views_stock_io.inventory_export, name='inventory_export'),
    path('inventory/import/', views_stock_io.inventory_import, name='inventory_import'),
    path('inventory/categories/', views.inventory_category_list, name='inventory_category_list'),
    path('inventory/categories/export/', views_stock_io.inventory_category_export, name='inventory_category_export'),
    path('inventory/categories/import/', views_stock_io.inventory_category_import, name='inventory_category_import'),
    path('inventory/categories/add/', views.inventory_category_add, name='inventory_category_add'),
    path('inventory/categories/<int:pk>/edit/', views.inventory_category_edit, name='inventory_category_edit'),
    path('inventory/categories/<int:pk>/delete/', views.inventory_category_delete, name='inventory_category_delete'),
    path('inventory/categories/api/', views.inventory_category_api, name='inventory_category_api'),
    path('inventory/add/', views.inventory_add, name='inventory_add'),
    path('inventory/quick-add-api/', views.inventory_quick_add_api, name='inventory_quick_add_api'),
    path('inventory/<int:pk>/edit/', views.inventory_edit, name='inventory_edit'),
    path('inventory/<int:pk>/delete/', views.inventory_delete, name='inventory_delete'),
    path('inventory/<int:pk>/adjust/', views.inventory_adjust, name='inventory_adjust'),
    path('inventory/logs/', views.inventory_logs, name='inventory_logs'),
    path('inventory/logs/export/', views_stock_io.inventory_logs_export, name='inventory_logs_export'),
    path('inventory/wastage/', views_controls.inventory_wastage, name='inventory_wastage'),
    path('inventory/wastage/export/', views_stock_io.inventory_wastage_export, name='inventory_wastage_export'),
    path('inventory/wastage/import/', views_stock_io.inventory_wastage_import, name='inventory_wastage_import'),
    path('inventory/wastage/add/', views_controls.inventory_wastage_add, name='inventory_wastage_add'),
    path('inventory/counts/', views_controls.inventory_counts, name='inventory_counts'),
    path('inventory/counts/new/', views_controls.inventory_count_new, name='inventory_count_new'),
    path('inventory/counts/<int:pk>/', views_controls.inventory_count_detail, name='inventory_count_detail'),
    path('inventory/counts/<int:pk>/export/', views_stock_io.inventory_count_export, name='inventory_count_export'),
    path('inventory/counts/<int:pk>/import/', views_stock_io.inventory_count_import, name='inventory_count_import'),
    path('inventory/counts/<int:pk>/save/', views_controls.inventory_count_save, name='inventory_count_save'),
    path('reports/menu-engineering/', views_controls.report_menu_engineering, name='report_menu_engineering'),
    path('purchases/', views.purchase_list, name='purchase_list'),
    # Purchase cycle (TallyPrime order processing): PO -> GRN -> bill -> debit note / payment
    path('purchases/<int:pk>/pay/', views_procurement.purchase_bill_pay, name='purchase_bill_pay'),
    path('purchases/export/', views.purchase_records_export, name='purchase_records_export'),
    path('purchases/create/', views.purchase_create, name='purchase_create'),
    path('purchases/<int:pk>/voucher/', views.purchase_voucher_edit, name='purchase_voucher_edit'),
    path('purchases/api/ledger-create/', views.purchase_zen_ledger_create, name='purchase_zen_ledger_create'),
    path('purchases/sidebar-panel/', views.purchase_sidebar_panel, name='purchase_sidebar_panel'),
    # Same drawer outside purchases/, so branches without the inventory module (or staff without it) still get Alt+M
    path('sidebar-panel/', views.purchase_sidebar_panel, name='sidebar_panel'),
    path('purchases/<int:pk>/', views.purchase_invoice, name='purchase_detail'),
    path('purchases/<int:pk>/invoice/', views.purchase_invoice, name='purchase_invoice'),
    path('purchases/<int:pk>/edit/', views.purchase_edit, name='purchase_edit'),

    # Party Management
    path('parties/', views.party_list, name='party_list'),
    path('parties/export/', views_stock_io.party_export, name='party_export'),
    path('parties/import/', views_stock_io.party_import, name='party_import'),
    path('parties/<int:pk>/', views.party_ledger_detail, name='party_ledger_detail'),
    path('parties/<int:pk>/edit/', views.party_edit_api, name='party_edit_api'),

    # Recipe Management
    path('menu/<int:pk>/recipe/', views.menu_recipe, name='menu_recipe'),
    path('menu/recipe/delete/<int:pk>/', views.menu_recipe_delete, name='menu_recipe_delete'),
    
    # Category Management
    path('categories/', views.category_list, name='category_list'),
    path('categories/add/', views.category_add, name='category_add'),
    path('categories/export/', views_menu_io.category_export, name='category_export'),
    path('categories/import/', views_menu_io.category_import, name='category_import'),
    path('categories/<int:pk>/edit/', views.category_edit, name='category_edit'),
    path('categories/<int:pk>/delete/', views.category_delete, name='category_delete'),
    
    # Subcategory Management
    path('subcategories/', views.subcategory_list, name='subcategory_list'),
    path('categories/<int:category_id>/subcategories/add/', views.subcategory_add, name='subcategory_add'),
    path('subcategories/<int:pk>/edit/', views.subcategory_edit, name='subcategory_edit'),
    path('subcategories/<int:pk>/delete/', views.subcategory_delete, name='subcategory_delete'),
    
    # Category Detail View
    path('categories/<int:pk>/', views.category_detail, name='category_detail'),
    
    # AJAX Endpoints
    path('ajax/get-subcategories/', views.get_subcategories, name='get_subcategories'),
    
    # QR Code Generation

    # Coupon Management
    path('coupons/', views.coupon_list, name='coupon_list'),
    path('coupons/add/', views.coupon_add, name='coupon_add'),
    path('coupons/<int:pk>/edit/', views.coupon_edit, name='coupon_edit'),
    path('coupons/<int:pk>/delete/', views.coupon_delete, name='coupon_delete'),

    # Reservations Management
    path('reservations/', views_reservations.reservation_list, name='reservation_list'),
    path('reservations/waitlist/', views_controls.reservation_waitlist, name='reservation_waitlist'),
    path('reservations/waitlist/add/', views_controls.reservation_waitlist_add, name='reservation_waitlist_add'),
    path('reservations/waitlist/<int:pk>/', views_controls.reservation_waitlist_update, name='reservation_waitlist_update'),
    path('reservations/settings/', views_reservations.reservation_settings, name='reservation_settings'),
    path('reservations/<int:pk>/confirm/', views_reservations.reservation_confirm, name='reservation_confirm'),
    path('reservations/<int:pk>/cancel/', views_reservations.reservation_cancel, name='reservation_cancel'),
    path('reservations/<int:pk>/', views_reservations.reservation_detail, name='reservation_detail'),
    path('reservations/<int:pk>/edit/', views_reservations.reservation_edit, name='reservation_edit'),
    path('reservations/<int:pk>/delete/', views_reservations.reservation_delete, name='reservation_delete'),

    # Receipt System (Payment Receipts & Deposits)
    path('receipts/', views.receipt_list, name='receipt_list'),
    path('receipts/create/', views.receipt_create_page, name='receipt_create_page'),
    path('receipts/<int:pk>/edit/', views.receipt_edit_page, name='receipt_edit_page'),
    path('receipts/api/create/', views.receipt_create_api, name='receipt_create_api'),
    path('receipts/<int:pk>/print/', views.receipt_print, name='receipt_print'),
    path('receipts/<int:pk>/cancel/', views.receipt_cancel_api, name='receipt_cancel_api'),
    path('receipts/api/<str:receipt_no>/', views.receipt_detail_api, name='receipt_detail_api'),

    # Club & Events Management
    path('club-events/', views_events.club_events_list, name='club_events_list'),
    path('club-events/create/', views_events.club_event_create, name='club_event_create'),
    path('club-events/<int:pk>/edit/', views_events.club_event_edit, name='club_event_edit'),
    path('club-events/<int:pk>/delete/', views_events.club_event_delete, name='club_event_delete'),
    path('club-events/<int:pk>/toggle-publish/', views_events.club_event_toggle_publish, name='club_event_toggle_publish'),
    path('club-events/<int:pk>/duplicate/', views_events.club_event_duplicate, name='club_event_duplicate'),
    path('club-events/guestlist/', views_events.club_guestlist, name='club_guestlist'),
    path('club-events/<int:event_id>/guestlist/', views_events.club_guestlist, name='club_guestlist_event'),
    path('club-events/<int:event_id>/manual-booking/', views_events.club_manual_booking_create, name='club_manual_booking_create'),
    path('club-events/booking/<int:booking_id>/toggle-checkin/', views_events.club_booking_toggle_checkin, name='club_booking_toggle_checkin'),
    path('club-events/booking/<int:booking_id>/cancel/', views_events.club_booking_cancel, name='club_booking_cancel'),
    path('club-events/<int:event_id>/guestlist/export/', views_events.club_guestlist_export, name='club_guestlist_export'),
    path('club-events/floor-plan/', views_events.club_floor_plan, name='club_floor_plan'),
    path('club-events/<int:event_id>/floor-plan/', views_events.club_floor_plan, name='club_floor_plan_event'),
    path('club-events/<int:event_id>/floor-plan/api/', views_events.club_floor_plan_api, name='club_floor_plan_api'),
    path('club-events/<int:event_id>/floor-plan/assign/', views_events.club_floor_plan_assign_table, name='club_floor_plan_assign_table'),
    path('club-events/ticket-grid/', views_events.club_ticket_grid, name='club_ticket_grid'),
    path('club-events/<int:event_id>/ticket-grid/', views_events.club_ticket_grid, name='club_ticket_grid_event'),
    path('club-events/<int:event_id>/ticket-grid/api/', views_events.club_ticket_grid_api, name='club_ticket_grid_api'),
    path('club-events/gate-scanner/', views_events.club_gate_scanner, name='club_gate_scanner'),
    path('club-events/<int:event_id>/gate-scanner/', views_events.club_gate_scanner, name='club_gate_scanner_event'),

    # Staff & Permissions Management
    path('staff/', views_staff.staff_list, name='staff_list'),
    path('staff/create/', views_staff.staff_create, name='staff_create'),
    path('staff/<int:pk>/edit/', views_staff.staff_edit, name='staff_edit'),
    path('staff/<int:pk>/toggle-active/', views_staff.staff_toggle_active, name='staff_toggle_active'),
    path('staff/<int:pk>/delete/', views_staff.staff_delete, name='staff_delete'),

    # Phase 1: Fast-Track Daily Muster Roll & Department Shift Allocation
    path('staff/attendance/', views_hrms.muster_roll_view, name='muster_roll'),
    path('staff/attendance/api/mark-all-present/', views_hrms.api_mark_all_present, name='api_mark_all_present'),
    path('staff/attendance/api/save/', views_hrms.api_save_single_attendance, name='api_save_single_attendance'),

    # Ground Staff Directory (Chefs, Stewards, Housekeeping - No Django User required)
    path('staff/ground-staff/', views_hrms.ground_staff_list, name='ground_staff_list'),
    path('staff/ground-staff/create/', views_hrms.ground_staff_create, name='ground_staff_create'),
    path('staff/ground-staff/<int:pk>/edit/', views_hrms.ground_staff_edit, name='ground_staff_edit'),
    path('staff/ground-staff/<int:pk>/toggle-active/', views_hrms.ground_staff_toggle_active, name='ground_staff_toggle_active'),

    # Phase 2: Automated Monthly Payroll Engine & Salary Slips
    path('staff/payroll/', views_hrms.payroll_dashboard, name='payroll_dashboard'),
    path('staff/payroll/api/generate/', views_hrms.api_generate_monthly_payroll, name='api_generate_monthly_payroll'),
    path('staff/payroll/<int:pk>/update-adjustment/', views_hrms.api_update_payroll_adjustment, name='api_update_payroll_adjustment'),
    path('staff/payroll/api/finalize-all/', views_hrms.api_finalize_all_payroll, name='api_finalize_all_payroll'),
    path('staff/payroll/<int:pk>/salary-slip/', views_hrms.salary_slip_view, name='salary_slip_view'),

    # Phase 3: Operational Task SOP Board
    path('staff/sop/', views_hrms.sop_dashboard_view, name='sop_dashboard'),
    path('staff/sop/api/toggle-status/', views_hrms.api_toggle_sop_task_status, name='api_toggle_sop_task_status'),
    path('staff/sop/api/seed-defaults/', views_hrms.api_seed_default_sops, name='api_seed_default_sops'),
    path('staff/sop/api/create-task/', views_hrms.api_create_sop_task, name='api_create_sop_task'),
    path('staff/sop/<int:pk>/delete/', views_hrms.api_delete_sop_task, name='api_delete_sop_task'),
]