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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.conf import settings
        context['frontend_url'] = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
        return context

    def get_success_url(self):
        # Prefer next= if provided and safe
        next_url = self.request.GET.get('next') or self.request.POST.get('next')
        if next_url:
            from django.utils.http import url_has_allowed_host_and_scheme
            if url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()):
                return next_url
        # Redirect staff restaurant admins to their slugged dashboard
        slug = self._user_restaurant_slug(self.request.user)
        if slug:
            return reverse_lazy('restaurant_admin:dashboard', kwargs={'restaurant_slug': slug})
        # Superusers or staff without a restaurant go to super admin
        if self.request.user.is_superuser or self.request.user.is_staff:
            return reverse_lazy('super_admin:dashboard')
        return reverse_lazy('login')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            slug = self._user_restaurant_slug(request.user)
            if slug:
                return redirect('restaurant_admin:dashboard', restaurant_slug=slug)
            if request.user.is_superuser or request.user.is_staff:
                return redirect('super_admin:dashboard')
        return super().dispatch(request, *args, **kwargs)

class CustomLogoutView(BaseLogoutView):
    next_page = 'login'
    http_method_names = ['get', 'post', 'options']
    
    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() in self.http_method_names:
            return self.process_logout(request)
        return self.http_method_not_allowed(request, *args, **kwargs)
    
    def get(self, request, *args, **kwargs):
        return self.process_logout(request)

    def post(self, request, *args, **kwargs):
        return self.process_logout(request)

    def process_logout(self, request):
        from django.contrib.auth import logout
        from django.utils.http import url_has_allowed_host_and_scheme

        next_page = request.GET.get('next') or request.POST.get('next')
        logout(request)
        
        if next_page and url_has_allowed_host_and_scheme(
            url=next_page,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_page)
        return redirect('login')
