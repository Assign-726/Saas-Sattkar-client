"""
URL configuration for restaurant_project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
"""
import os
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.contrib.staticfiles.views import serve as staticfiles_serve
from django.shortcuts import redirect
from django.views.generic import RedirectView
from core.throttling import GuardedTokenObtainPairView, GuardedTokenRefreshView
from core import views_health
from django.contrib.auth import views as auth_views
from integrations.delivery import views as delivery_views

from core.views import (
    redirect_to_frontend,
    redirect_to_frontend_menu,
)
from core.views_auth import (
    CustomLoginView,
    CustomLogoutView,
    VerifyOTPView,
    ResendOTPView,
    SubmitEnquiryView,
)
from core.admin_custom import custom_admin_site
from restaurant_admin import urls as restaurant_admin_urls
from hotel import views as views_hotel
from guests import views_growth as guest_growth_views


# Use the custom admin site
admin.site = custom_admin_site
admin.autodiscover()

urlpatterns = [
    # Favicon
    path('healthz/', views_health.healthz, name='healthz'),
    path('favicon.ico', RedirectView.as_view(url=settings.STATIC_URL + 'favicon.ico', permanent=True)),

    # Admin / Authentication at root
    path('', CustomLoginView.as_view(template_name='admin/login.html'), name='home'),
    path('login/', CustomLoginView.as_view(template_name='admin/login.html'), name='login'),
    path('login/verify-otp/', VerifyOTPView.as_view(), name='verify_otp'),
    path('login/resend-otp/', ResendOTPView.as_view(), name='resend_otp'),
    path('api/enquiries', SubmitEnquiryView.as_view(), name='submit_enquiry_no_slash'),
    path('api/enquiries/', SubmitEnquiryView.as_view(), name='submit_enquiry'),
    path('logout/', CustomLogoutView.as_view(), name='logout'),
    path('dj-admin/logout/', CustomLogoutView.as_view(), name='admin_logout'),

    # Customer pages (Redirect to Next.js Frontend)
    path('reserve/', redirect_to_frontend, name='reserve_entry'),
    path('cart/', redirect_to_frontend, name='cart'),
    path('r/<slug:restaurant_slug>cart/', redirect_to_frontend, name='restaurant_cart_typo'),
    path('r/<slug:restaurant_slug>/', redirect_to_frontend_menu, name='restaurant_menu'),
    path('r/<slug:restaurant_slug>/reserve/', redirect_to_frontend, name='restaurant_reserve'),
    path('r/<slug:restaurant_slug>/cart/', redirect_to_frontend, name='restaurant_cart'),
    path('r/<slug:restaurant_slug>/orders/', redirect_to_frontend, name='restaurant_orders'),
    path('orders/', redirect_to_frontend, name='orders_list'),
    path('orders/<int:id>/', redirect_to_frontend, name='order_detail'),
    path('orders/<int:id>/confirm/', redirect_to_frontend, name='order_confirm'),

    # Admin URLs (using custom admin site)
    path('dj-admin/', custom_admin_site.urls),
    # Restaurant Admin (slug-scoped). Example: /dashboard/itc-rajasthan/menu/
    path('dashboard/<slug:restaurant_slug>/', include(('restaurant_admin.urls', 'restaurant_admin'), namespace='restaurant_admin')),

    # Dedicated Hotel Admin PMS (slug-scoped). Example: /hotel-admin/sharma-hotel/
    path('hotel-admin/', include([
        path('', lambda req: redirect('hotel_admin:dashboard', restaurant_slug=(
            req.user.restaurant_profile.restaurant.slug if req.user.is_authenticated and hasattr(req.user, 'restaurant_profile') and req.user.restaurant_profile.restaurant else (
                __import__('super_admin.models').models.Restaurant.objects.first().slug if __import__('super_admin.models').models.Restaurant.objects.exists() else 'default'
            )
        ))),
        path('<slug:restaurant_slug>/', include(('hotel.urls_admin', 'hotel_admin'), namespace='hotel_admin')),
    ])),
    
    # Super Admin
    path('super/', include(('super_admin.urls', 'super_admin'), namespace='super_admin')),
    
    # Public guest pages: feedback form (one per branch) and email unsubscribe
    path('feedback/<slug:restaurant_slug>/<int:branch_id>/', guest_growth_views.feedback_public, name='guest_feedback_public'),
    path('g/unsubscribe/<str:token>/', guest_growth_views.unsubscribe, name='guest_unsubscribe'),

    # Public partner API (branch-scoped API keys) — see Integrations › Developer guide
    path('api/v1/', include('integrations.urls_api')),
    path('integrations/delivery/<str:provider>/webhook/', delivery_views.webhook_shared, name='delivery_webhook_shared'),
    path('integrations/delivery/<str:provider>/<uuid:public_id>/webhook/', delivery_views.webhook, name='delivery_webhook'),

    # API endpoints
    path('api/', include('core.urls')),
    path('api/', include('reservations.urls')),
    path('api/events/', include(('events.urls', 'events'), namespace='events')),
    path('api/hotel/', include(('hotel.urls', 'hotel'), namespace='hotel')),
    path('api/public/branches/<int:branch_id>/availability/', include('hotel.urls_public')),
    path('api/public/hotel/<int:branch_id>/cab-packages/', views_hotel.PublicCabPackagesView.as_view(), name='api_public_hotel_cab_packages_branch'),
    path('api/public/hotel/cab-packages/', views_hotel.PublicCabPackagesView.as_view(), name='api_public_hotel_cab_packages'),
    path('api/public/hotel/checkout/', views_hotel.PublicHotelCheckoutView.as_view(), name='api_public_hotel_checkout'),
    
    # Public Hotel / Rooms Booking Engine endpoints
    path('api/rooms/', views_hotel.PublicBranchAvailabilityView.as_view(), name='api_rooms_availability'),
    path('api/rooms/bookings/', views_hotel.PublicBookingCreateView.as_view(), name='api_rooms_bookings'),
    path('api/rooms/checkout/', views_hotel.PublicHotelCheckoutView.as_view(), name='api_rooms_checkout'),
    path('api/rooms/verify-payment/', views_hotel.PublicHotelVerifyPaymentView.as_view(), name='api_rooms_verify_payment'),
    path('api/rooms/cancel-unpaid/', views_hotel.PublicHotelCancelUnpaidView.as_view(), name='api_rooms_cancel_unpaid'),
    path('api/public/hotel/verify-payment/', views_hotel.PublicHotelVerifyPaymentView.as_view(), name='api_public_hotel_verify_payment'),
    path('api/public/hotel/cancel-unpaid/', views_hotel.PublicHotelCancelUnpaidView.as_view(), name='api_public_hotel_cancel_unpaid'),

    # Public Cab & VIP Chauffeur Service endpoints
    path('api/cabs/bookings/', views_hotel.PublicCabBookingCreateView.as_view(), name='api_cabs_bookings'),
    path('api/cabs/bookings/<str:booking_id>/', views_hotel.PublicCabBookingDetailView.as_view(), name='api_cabs_booking_detail'),
    path('api/cabs/fleet/', views_hotel.PublicCabFleetView.as_view(), name='api_cabs_fleet'),
    path('api/public/hotel/cabs/book/', views_hotel.PublicCabBookingCreateView.as_view(), name='api_public_hotel_cabs_book'),
    path('api/public/hotel/cabs/booking/<str:booking_id>/', views_hotel.PublicCabBookingDetailView.as_view(), name='api_public_hotel_cab_detail'),
    path('api/public/hotel/cabs/fleet/', views_hotel.PublicCabFleetView.as_view(), name='api_public_hotel_cabs_fleet'),


    # API Token Authentication
    path('api/token/', GuardedTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', GuardedTokenRefreshView.as_view(), name='token_refresh'),
]

# Media and Static files serving (Development & Production fallback)
media_prefix = settings.MEDIA_URL.lstrip('/')
static_prefix = settings.STATIC_URL.lstrip('/')

urlpatterns += [
    re_path(rf'^{media_prefix}(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    re_path(rf'^{static_prefix}(?P<path>.*)$', staticfiles_serve, {'insecure': True}),
]