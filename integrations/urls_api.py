from django.urls import path

from integrations import api

urlpatterns = [
    path('branch', api.branch_info, name='api_v1_branch'),
    path('menu', api.menu, name='api_v1_menu'),
    path('menu/items/<int:item_id>', api.menu_item_update, name='api_v1_menu_item'),
    path('orders', api.orders_collection, name='api_v1_orders'),
    path('orders/<int:order_id>', api.order_detail, name='api_v1_order'),
    path('orders/<int:order_id>/status', api.order_status, name='api_v1_order_status'),
    path('rooms/availability', api.room_availability, name='api_v1_room_availability'),
    path('bookings', api.bookings_collection, name='api_v1_bookings'),
    path('bookings/<str:booking_id>/cancel', api.cancel_booking, name='api_v1_booking_cancel'),
    path('bookings/<str:booking_id>/modify', api.modify_booking, name='api_v1_booking_modify'),
    path('rates', api.rates, name='api_v1_rates'),
]
