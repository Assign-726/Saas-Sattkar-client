from django.contrib import admin
from django.contrib.admin import AdminSite
from django.contrib.auth.views import LoginView
from django.urls import path, reverse
from django.http import HttpResponseRedirect
from django.contrib.auth.models import Group, User

class CustomAdminSite(AdminSite):
    site_header = 'Restaurant Admin'
    site_title = 'Restaurant Admin'
    index_title = 'Welcome to Restaurant Admin'
    
    def login(self, request, extra_context=None):
        """
        Override the login method to ensure proper redirection
        """
        if request.method == 'GET' and self.has_permission(request):
            # Already logged-in, redirect to admin index
            index_path = reverse('admin:index', current_app=self.name)
            return HttpResponseRedirect(index_path)
        
        if not request.user.is_authenticated:
            login_url = reverse('login')
            # Extract the actual next path if already set, otherwise get full path
            next_path = request.GET.get('next') or request.get_full_path()
            # Prevent infinite admin login redirect loops/nesting
            if '/login/' in next_path:
                next_path = reverse('admin:index', current_app=self.name)
            return HttpResponseRedirect(f"{login_url}?next={next_path}")
        
        # Since this method gets called in some special cases by the admin,
        # we need to preserve the original behavior for those cases
        return super().login(request, extra_context=extra_context)
    
    def has_permission(self, request):
        """
        Override to ensure only staff users can access the admin
        """
        return request.user.is_active and request.user.is_staff

# Create an instance of the custom admin site
custom_admin_site = CustomAdminSite(name='custom_admin')

# Register default auth models
custom_admin_site.register(Group)
custom_admin_site.register(User)

# Register all models from the core app
try:
    from django.apps import apps
    from django.contrib.admin.sites import AlreadyRegistered
    
    # Get all models from core app
    app_models = apps.get_app_config('core').get_models()
    
    # Register models with the custom admin site
    for model in app_models:
        try:
            # Check if model is already registered with the default admin
            if model in admin.site._registry:
                admin_class = type(admin.site._registry[model])
                custom_admin_site.register(model, admin_class)
            else:
                custom_admin_site.register(model)
        except AlreadyRegistered:
            # Skip if already registered
            continue
except Exception as e:
    # Log error but don't crash
    import logging
    logger = logging.getLogger(__name__)
    logger.error(f"Error registering models with custom admin: {e}")
