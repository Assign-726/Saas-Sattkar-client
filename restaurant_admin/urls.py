from django.urls import path
from restaurant_admin import views
from restaurant_admin import views_reservations
from restaurant_admin import floor_plan_views

app_name = 'restaurant_admin'

urlpatterns = [
    # Test URLs
    path('test/', views.test_template, name='test'),
    path('test-static/', views.test_static, name='test_static'),
    path('debug-menu/', views.debug_menu_list, name='debug_menu'),
    
    # Unified Transaction Desk
    path('transactions/create/', views.transaction_desk_create, name='transaction_desk_create'),

    # Branch Switching
    path('switch-branch/', views.switch_branch, name='switch_branch'),

    # Dashboard & Analytics
    path('', views.dashboard, name='dashboard'),
    path('analytics/', views.sales_analytics, name='sales_analytics'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/custom-charges/save/', views.custom_charge_save, name='custom_charge_save'),
    path('settings/custom-charges/<int:pk>/delete/', views.custom_charge_delete, name='custom_charge_delete'),
    
    # Delivery Challans Management
    path('challans/', views.challan_list, name='challan_list'),
    path('challans/create/', views.challan_create, name='challan_create'),
    path('challans/<int:pk>/', views.challan_detail, name='challan_detail'),
    path('challans/<int:pk>/receive/', views.challan_mark_received, name='challan_mark_received'),
    path('challans/<int:pk>/cancel/', views.challan_cancel, name='challan_cancel'),
    
    # Floor Plan & Table Management
    path('floor-plan/', floor_plan_views.floor_plan_dashboard, name='floor_plan_dashboard'),
    path('api/floor-plan/', floor_plan_views.floor_plan_api, name='floor_plan_api'),
    path('api/floor-plan/save-position/', floor_plan_views.floor_plan_save_position_api, name='floor_plan_save_position_api'),
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
    path('api/notifications/poll/', views.order_notifications_poll, name='order_notifications_poll'),
    path('orders/<int:pk>/', views.order_detail, name='order_detail'),
    path('orders/<int:pk>/update-status/', views.order_update_status, name='order_update_status'),
    path('orders/<int:pk>/invoice/', views.order_invoice, name='order_invoice'),
    path('orders/<int:pk>/kot/', views.order_kot, name='order_kot'),
    path('orders/<int:pk>/prep-time/', views.order_set_prep_time, name='order_set_prep_time'),
    path('orders/<int:pk>/mark-ready/', views.order_mark_ready, name='order_mark_ready'),
    path('orders/draft/', views.pos_draft_order_api, name='pos_draft_order_api'),
    path('orders/draft/sync-items/', views.pos_sync_draft_items_api, name='pos_sync_draft_items_api'),
    path('orders/draft/finalize/', views.pos_finalize_order_api, name='pos_finalize_order_api'),
    path('orders/<int:pk>/settle/', views.order_settle_api, name='order_settle_api'),
    path('api/orders/omni-search/', views.order_omni_search_api, name='order_omni_search_api'),
    path('api/detect-location/', views.detect_location_ip_api, name='detect_location_ip_api'),
    path('orders/<int:pk>/credit-note/', views.order_credit_note_api, name='order_credit_note_api'),
    path('orders/credit-note/', views.credit_note_desk, name='credit_note_desk'),
    path('credit-notes/<int:pk>/thermal/', views.credit_note_thermal_view, name='credit_note_thermal_view'),

    # Inventory & Purchases Management
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/categories/', views.inventory_category_list, name='inventory_category_list'),
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
    path('purchases/', views.purchase_list, name='purchase_list'),
    path('purchases/create/', views.purchase_create, name='purchase_create'),
    path('purchases/<int:pk>/edit/', views.purchase_edit, name='purchase_edit'),

    # Party Management
    path('parties/', views.party_list, name='party_list'),
    path('parties/<int:pk>/', views.party_ledger_detail, name='party_ledger_detail'),
    path('parties/<int:pk>/edit/', views.party_edit_api, name='party_edit_api'),

    # Recipe Management
    path('menu/<int:pk>/recipe/', views.menu_recipe, name='menu_recipe'),
    path('menu/recipe/delete/<int:pk>/', views.menu_recipe_delete, name='menu_recipe_delete'),
    
    # Category Management
    path('categories/', views.category_list, name='category_list'),
    path('categories/add/', views.category_add, name='category_add'),
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
    path('qr-generator/', views.qr_code_generator, name='qr_code_generator'),
    path('qr-code/', views.generate_qr_code, name='generate_qr_code'),
    path('menu/<int:menu_id>/qr-code/', views.generate_qr_code, name='generate_menu_item_qr_code'),

    # Coupon Management
    path('coupons/', views.coupon_list, name='coupon_list'),
    path('coupons/add/', views.coupon_add, name='coupon_add'),
    path('coupons/<int:pk>/edit/', views.coupon_edit, name='coupon_edit'),
    path('coupons/<int:pk>/delete/', views.coupon_delete, name='coupon_delete'),

    # Reservations Management
    path('reservations/', views_reservations.reservation_list, name='reservation_list'),
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
]