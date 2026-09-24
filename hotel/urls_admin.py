from django.urls import path
from hotel import views_admin

app_name = 'hotel_admin'

urlpatterns = [
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
    path('categories/images/<int:image_id>/delete/', views_admin.category_delete_image_action, name='category_delete_image'),
    path('categories/images/<int:image_id>/set-cover/', views_admin.category_set_cover_action, name='category_set_cover'),
    path('categories/<int:category_id>/videos/<int:video_idx>/delete/', views_admin.category_delete_video_action, name='category_delete_video'),

    # Bookings & Guest Folios
    path('bookings/', views_admin.bookings_view, name='bookings'),
    path('bookings/create/', views_admin.booking_create_action, name='booking_create'),
    path('bookings/<int:booking_id>/check-in/', views_admin.booking_check_in_action, name='booking_check_in'),
    path('bookings/<int:booking_id>/check-out/', views_admin.booking_check_out_action, name='booking_check_out'),
    path('bookings/<int:booking_id>/cancel/', views_admin.booking_cancel_action, name='booking_cancel'),

    # Hotel Configuration & Settings
    path('settings/', views_admin.hotel_settings_view, name='settings'),
]
