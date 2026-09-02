"""
URL configuration for restaurant_project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.contrib.auth import views as auth_views
import os
from core.views import HomeView, CartView, OrdersListView, OrderDetailView, OrderConfirmView, RestaurantMenuView, RestaurantCartView, ReserveEntryView, ReservationPageView
from core.views_auth import CustomLoginView, CustomLogoutView, VerifyOTPView, ResendOTPView, SubmitEnquiryView
from core.admin_custom import custom_admin_site
from restaurant_admin import urls as restaurant_admin_urls


# Use the custom admin site
admin.site = custom_admin_site
admin.autodiscover()

urlpatterns = [
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

    #secondary home page
    #path('/home', HomeView.as_view(), name='home'),
    
    # Admin URLs (using custom admin site)
    path('dj-admin/', custom_admin_site.urls),
    # Restaurant Admin (slug-scoped). Example: /dashboard/itc-rajasthan/menu/
    path('dashboard/<slug:restaurant_slug>/', include(('restaurant_admin.urls', 'restaurant_admin'), namespace='restaurant_admin')),
    # Optional legacy without slug (disable if not needed)
    #path('dashboard/', include(restaurant_admin_urls)),
    
    # Super Admin
    path('super/', include(('super_admin.urls', 'super_admin'), namespace='super_admin')),
    
    # API endpoints
    path('api/', include('core.urls')),
    path('api/', include('reservations.urls')),
    
    # Debug URL
    #path('test-urls/', views.test_urls, name='test_urls'),
    
    # API Token Authentication
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)