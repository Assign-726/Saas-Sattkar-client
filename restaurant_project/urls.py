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
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.contrib.auth import views as auth_views

from core.views import (
    HomeView,
    CartView,
    OrdersListView,
    OrderDetailView,
    OrderConfirmView,
    RestaurantMenuView,
    RestaurantCartView,
    ReserveEntryView,
    ReservationPageView,
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


# Use the custom admin site
admin.site = custom_admin_site
admin.autodiscover()

urlpatterns = [
    # Favicon
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

    # Customer pages
    path('reserve/', ReserveEntryView.as_view(), name='reserve_entry'),
    path('cart/', CartView.as_view(), name='cart'),
    # Fix common typo missing slash before 'cart': /r/<slug>cart/ -> /r/<slug>/cart/
    path('r/<slug:restaurant_slug>cart/', RedirectView.as_view(pattern_name='restaurant_cart', permanent=False)),
    path('r/<slug:restaurant_slug>/', RestaurantMenuView.as_view(), name='restaurant_menu'),
    path('r/<slug:restaurant_slug>/reserve/', ReservationPageView.as_view(), name='restaurant_reserve'),
    path('r/<slug:restaurant_slug>/cart/', RestaurantCartView.as_view(), name='restaurant_cart'),
    path('r/<slug:restaurant_slug>/orders/', OrdersListView.as_view(), name='restaurant_orders'),
    path('orders/', OrdersListView.as_view(), name='orders_list'),
    path('orders/<int:id>/', OrderDetailView.as_view(), name='order_detail'),
    path('orders/<int:id>/confirm/', OrderConfirmView.as_view(), name='order_confirm'),

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
    
    # API endpoints
    path('api/', include('core.urls')),
    path('api/', include('reservations.urls')),
    path('api/events/', include(('events.urls', 'events'), namespace='events')),
    path('api/hotel/', include(('hotel.urls', 'hotel'), namespace='hotel')),
    path('api/public/branches/<int:branch_id>/availability/', include('hotel.urls_public')),
    
    # Public Hotel / Rooms Booking Engine endpoints
    path('api/rooms/', views_hotel.PublicBranchAvailabilityView.as_view(), name='api_rooms_availability'),
    path('api/rooms/bookings/', views_hotel.PublicBookingCreateView.as_view(), name='api_rooms_bookings'),

    # API Token Authentication
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

# Media and Static files serving (Development & Production fallback)
media_prefix = settings.MEDIA_URL.lstrip('/')
static_prefix = settings.STATIC_URL.lstrip('/')

urlpatterns += [
    re_path(rf'^{media_prefix}(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    re_path(rf'^{static_prefix}(?P<path>.*)$', staticfiles_serve, {'insecure': True}),
]