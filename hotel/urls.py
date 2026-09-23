from django.urls import path, include
from rest_framework.routers import DefaultRouter
from hotel import views

app_name = 'hotel'

router = DefaultRouter()
router.register(r'categories', views.RoomCategoryViewSet, basename='hotel-category')
router.register(r'rooms', views.RoomViewSet, basename='hotel-room')
router.register(r'bookings', views.RoomBookingViewSet, basename='hotel-booking')

urlpatterns = [
    # Router endpoints (categories, rooms, bookings)
    path('', include(router.urls)),

    # Specific booking & room front desk actions
    path('rooms/<int:pk>/status/', views.RoomStatusUpdateView.as_view(), name='room-status-update'),
    path('bookings/<int:pk>/check-in/', views.BookingCheckInView.as_view(), name='booking-check-in'),
    path('bookings/<int:pk>/check-out/', views.BookingCheckOutView.as_view(), name='booking-check-out'),
    path('bookings/<int:pk>/cancel/', views.BookingCancelView.as_view(), name='booking-cancel'),

    # Front Desk Tape Chart & Daily Stats
    path('tape-chart/', views.HotelTapeChartView.as_view(), name='hotel-tape-chart'),
    path('stats/', views.HotelStatsView.as_view(), name='hotel-stats'),

    # Staff Auth & Roles
    path('auth/login/', views.HotelAuthLoginView.as_view(), name='hotel-auth-login'),
    path('auth/me/', views.HotelAuthMeView.as_view(), name='hotel-auth-me'),

    # Public booking engine endpoints
    path('public/availability/', views.PublicBranchAvailabilityView.as_view(), name='hotel-public-availability-generic'),
    path('public/bookings/', views.PublicBookingCreateView.as_view(), name='hotel-public-bookings'),
]
