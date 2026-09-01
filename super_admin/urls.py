from django.urls import path
from super_admin import views

app_name = 'super_admin'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('landing/', views.landing_index, name='landing_index'),
    path('restaurants/add/', views.restaurant_create, name='restaurant_create'),
    path('restaurants/<int:pk>/edit/', views.restaurant_edit, name='restaurant_edit'),
    path('restaurants/<int:pk>/delete/', views.restaurant_delete, name='restaurant_delete'),
    path('restaurants/<int:pk>/toggle-active/', views.restaurant_toggle_active, name='restaurant_toggle_active'),
    path('restaurants/<int:pk>/landing/', views.landing_edit, name='landing_edit'),
    path('restaurants/<int:pk>/landing/gallery/add/', views.gallery_add, name='gallery_add'),
    path('restaurants/landing/gallery/<int:gid>/delete/', views.gallery_delete, name='gallery_delete'),
    path('restaurants/<int:pk>/landing/team/add/', views.team_add, name='team_add'),
    path('restaurants/landing/team/<int:mid>/delete/', views.team_delete, name='team_delete'),
    path('restaurants/<int:pk>/landing/review/add/', views.review_add, name='review_add'),
    path('restaurants/landing/review/<int:rid>/delete/', views.review_delete, name='review_delete'),
    path('restaurants/<int:pk>/landing/event/add/', views.event_add, name='event_add'),
    path('restaurants/landing/event/<int:ev_id>/delete/', views.event_delete, name='event_delete'),
    path('restaurants/<int:pk>/blogs/add/', views.blog_add, name='blog_add'),
    path('restaurants/<int:pk>/blogs/<int:blog_id>/edit/', views.blog_edit, name='blog_edit'),
    path('restaurants/blogs/<int:blog_id>/delete/', views.blog_delete, name='blog_delete'),
    path('restaurants/admins/add/', views.restaurant_admin_create, name='restaurant_admin_create'),
    path('restaurants/admins/<int:pk>/edit/', views.restaurant_admin_edit, name='restaurant_admin_edit'),
    path('restaurants/admins/<int:pk>/delete/', views.restaurant_admin_delete, name='restaurant_admin_delete'),
    path('onboard/', views.onboard_restaurant, name='onboard'),
    path('setup/', views.setup_view, name='setup'),
    path('api/gstins-by-state/', views.api_gstins_by_state, name='api_gstins_by_state'),
    path('api/branches-by-restaurant/', views.api_branches_by_restaurant, name='api_branches_by_restaurant'),
]


