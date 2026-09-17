from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core import views


# API Router
router = DefaultRouter()
router.register(r'categories', views.CategoryViewSet)
router.register(r'subcategories', views.SubCategoryViewSet)
router.register(r'menu-items', views.MenuItemViewSet, basename='menuitem')
router.register(r'orders', views.OrderViewSet, basename='order')
router.register(r'restaurants', views.RestaurantViewSet, basename='restaurant')
router.register(r'branches', views.RestaurantBranchViewSet, basename='branch')
router.register(r'landing', views.RestaurantLandingViewSet, basename='landing')
router.register(r'settings', views.RestaurantSettingsViewSet, basename='settings')
router.register(r'zones', views.ZoneViewSet, basename='zone')
router.register(r'tables', views.TableViewSet, basename='table')
router.register(r'custom-charges', views.CustomChargeViewSet, basename='custom_charge')
router.register(r'blogs', views.BlogPostViewSet, basename='blog')
router.register(r'contact', views.CustomerInquiryViewSet, basename='contact')
router.register(r'newsletter/subscribers', views.NewsletterSubscriberViewSet, basename='newsletter_subscribers')
router.register(r'careers/jobs', views.JobPostingViewSet, basename='careers_jobs')
router.register(r'careers/applications', views.JobApplicationViewSet, basename='careers_applications')
router.register(r'reviews', views.ReviewViewSet, basename='review')

# API URL patterns
urlpatterns = [
    path('', include(router.urls)),
    #path('test-menu-items/', views.test_menu_items, name='test-menu-items'),
]