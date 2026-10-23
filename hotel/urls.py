from django.urls import path, include
from rest_framework.routers import DefaultRouter
from hotel import views

app_name = 'hotel'

router = DefaultRouter()
router.register(r'categories', views.RoomCategoryViewSet, basename='hotel-category')
router.register(r'rooms', views.RoomViewSet, basename='hotel-room')
router.register(r'bookings', views.RoomBookingViewSet, basename='hotel-booking')
router.register(r'cabs/packages', views.CabServicePackageViewSet, basename='hotel-cab-package')
router.register(r'cabs/fleet', views.FleetVehicleViewSet, basename='hotel-cab-fleet')
router.register(r'cabs/bookings', views.CabBookingViewSet, basename='hotel-cab-booking')
router.register(r'spa/treatments', views.SpaTreatmentViewSet, basename='hotel-spa-treatment')
router.register(r'spa/therapists', views.SpaTherapistViewSet, basename='hotel-spa-therapist')
router.register(r'spa/packages', views.SpaPackageViewSet, basename='hotel-spa-package')
router.register(r'spa/appointments', views.SpaAppointmentViewSet, basename='hotel-spa-appointment')

urlpatterns = [
    # Specific booking & room front desk actions
    path('rooms/<int:pk>/status/', views.RoomStatusUpdateView.as_view(), name='room-status-update'),
    path('bookings/<int:pk>/check-in/', views.BookingCheckInView.as_view(), name='booking-check-in'),
    path('bookings/<int:pk>/check-out/', views.BookingCheckOutView.as_view(), name='booking-check-out'),
    path('bookings/<int:pk>/cancel/', views.BookingCancelView.as_view(), name='booking-cancel'),

    # Travel Desk & Cab Dispatch Actions
    path('cabs/dispatch-board/', views.CabDispatchBoardView.as_view(), name='hotel-cabs-dispatch-board'),
    path('cabs/bookings/manual/', views.CabBookingManualCreateView.as_view(), name='hotel-cabs-booking-manual'),
    path('cabs/bookings/<int:pk>/assign-driver/', views.CabBookingAssignDriverView.as_view(), name='hotel-cabs-assign-driver'),
    path('cabs/bookings/<int:pk>/reschedule/', views.CabBookingRescheduleView.as_view(), name='hotel-cabs-reschedule'),
    path('cabs/bookings/<int:pk>/status/', views.CabBookingStatusUpdateView.as_view(), name='hotel-cabs-status-update'),

    # Front Desk Tape Chart & Daily Stats
    path('tape-chart/', views.HotelTapeChartView.as_view(), name='hotel-tape-chart'),
    path('stats/', views.HotelStatsView.as_view(), name='hotel-stats'),

    # Staff Auth & Roles
    path('auth/login/', views.HotelAuthLoginView.as_view(), name='hotel-auth-login'),
    path('auth/me/', views.HotelAuthMeView.as_view(), name='hotel-auth-me'),

    # Public booking & checkout engine endpoints
    path('public/availability/', views.PublicBranchAvailabilityView.as_view(), name='hotel-public-availability-generic'),
    path('public/bookings/', views.PublicBookingCreateView.as_view(), name='hotel-public-bookings'),
    path('public/checkout/', views.PublicHotelCheckoutView.as_view(), name='hotel-public-checkout'),
    path('public/verify-payment/', views.PublicHotelVerifyPaymentView.as_view(), name='hotel-public-verify-payment'),
    path('public/cancel-unpaid/', views.PublicHotelCancelUnpaidView.as_view(), name='hotel-public-cancel-unpaid'),
    path('public/cab-packages/', views.PublicCabPackagesView.as_view(), name='hotel-public-cab-packages'),
    path('public/<int:branch_id>/cab-packages/', views.PublicCabPackagesView.as_view(), name='hotel-public-cab-packages-branch'),
    path('public/cabs/book/', views.PublicCabBookingCreateView.as_view(), name='hotel-public-cabs-book'),
    path('public/cabs/booking/<str:booking_id>/', views.PublicCabBookingDetailView.as_view(), name='hotel-public-cabs-detail'),
    path('public/cabs/fleet/', views.PublicCabFleetView.as_view(), name='hotel-public-cabs-fleet'),

    # Public Spa & Wellness Endpoints
    path('public/spa/catalog/', views.PublicSpaCatalogView.as_view(), name='hotel-public-spa-catalog'),
    path('public/<int:branch_id>/spa/catalog/', views.PublicSpaCatalogView.as_view(), name='hotel-public-spa-catalog-branch'),
    path('public/spa/book/', views.PublicSpaBookingCreateView.as_view(), name='hotel-public-spa-book'),
    path('public/spa/booking/<str:ref_id>/', views.PublicSpaBookingDetailView.as_view(), name='hotel-public-spa-detail'),

    # Router endpoints (categories, rooms, bookings, cab packages, fleet, cab bookings, spa)
    path('', include(router.urls)),
]

