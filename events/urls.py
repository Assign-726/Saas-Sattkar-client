from django.urls import path, include
from rest_framework.routers import DefaultRouter
from events import views

app_name = 'events'

router = DefaultRouter()
router.register(r'admin/events', views.AdminClubEventViewSet, basename='admin-events')
router.register(r'admin/tickets', views.AdminTicketTypeViewSet, basename='admin-tickets')
router.register(r'public/events', views.PublicClubEventViewSet, basename='public-events')

urlpatterns = [
    # Router endpoints
    path('', include(router.urls)),
    
    # Public ticketing checkout & verification
    path('public/checkout/', views.public_checkout_initiate, name='public-checkout'),
    path('public/verify-payment/', views.public_verify_payment, name='public-verify-payment'),
    path('public/booking/<str:booking_reference>/', views.public_booking_detail, name='public-booking-detail'),

    # Customer Web Portal
    path('portal/', views.customer_events_page, name='customer-events-page'),
    path('portal/pass/<str:booking_reference>/', views.customer_booking_confirmation, name='customer-booking-confirmation'),
]
