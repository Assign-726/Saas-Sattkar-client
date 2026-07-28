from django.contrib.auth.views import LoginView as BaseLoginView, LogoutView as BaseLogoutView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator

class CustomLoginView(BaseLoginView):
    template_name = 'admin/login.html'
    redirect_authenticated_user = True

    def _user_restaurant_slug(self, user):
        try:
            profile = getattr(user, 'restaurant_profile', None)
            if profile and getattr(profile, 'restaurant', None):
                return profile.restaurant.slug
        except Exception:
            pass
        return None

    def get_success_url(self):
        # Prefer next= if provided and safe
        next_url = self.request.GET.get('next') or self.request.POST.get('next')
        if next_url:
            return next_url
        # Redirect staff restaurant admins to their slugged dashboard
        slug = self._user_restaurant_slug(self.request.user)
        if slug:
            return reverse_lazy('restaurant_admin:dashboard', kwargs={'restaurant_slug': slug})
        # Superusers or others without a restaurant go to super admin or home
        if self.request.user.is_superuser:
            return reverse_lazy('super_admin:dashboard')
        return reverse_lazy('home')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            slug = self._user_restaurant_slug(request.user)
            if slug:
                return redirect('restaurant_admin:dashboard', restaurant_slug=slug)
            if request.user.is_superuser:
                return redirect('super_admin:dashboard')
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)

class CustomLogoutView(BaseLogoutView):
    next_page = 'login'
    http_method_names = ['get', 'post', 'options']
    
    def dispatch(self, request, *args, **kwargs):
        # Handle both GET and POST requests
        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)
    
    def get(self, request, *args, **kwargs):
        # Handle GET request by calling the post method
        return self.post(request, *args, **kwargs)
    
    def post(self, request, *args, **kwargs):
        # Get the next page from the request parameters or use the default
        next_page = request.GET.get('next') or request.POST.get('next')
        if next_page:
            self.next_page = next_page
        
        # Log the user out
        from django.contrib.auth import logout
        logout(request)
        
        # Redirect to the next page or the default
        return self.redirect_to_login(self.next_page)
    
    def redirect_to_login(self, next_page):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        from urllib.parse import urlparse, urlunparse
        
        if next_page:
            # Ensure the next URL is safe
            from django.utils.http import url_has_allowed_host_and_scheme
            if not url_has_allowed_host_and_scheme(
                url=next_page,
                allowed_hosts={self.request.get_host()},
                require_https=self.request.is_secure(),
            ):
                next_page = self.next_page
        else:
            next_page = self.next_page
        
        # If next_page is a URL name, resolve it
        if not (next_page.startswith('http://') or next_page.startswith('https://')):
            next_page = reverse(next_page)
            
        return HttpResponseRedirect(next_page)
