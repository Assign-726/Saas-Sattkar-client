import logging
from django.contrib.auth.views import LoginView as BaseLoginView, LogoutView as BaseLogoutView
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.shortcuts import render, redirect
from django.urls import reverse_lazy
from django.views import View
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from core.models import EmailOTP
from core.emails import send_otp_email, mask_email

logger = logging.getLogger(__name__)


def resolve_user_redirect(request, user, next_url=None):
    """
    Resolves the smart redirect URL based on user role.
    """
    if next_url:
        from django.utils.http import url_has_allowed_host_and_scheme
        if url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure()
        ):
            return next_url

    # Redirect restaurant admins to their slugged dashboard
    try:
        profile = getattr(user, 'restaurant_profile', None)
        if profile and getattr(profile, 'restaurant', None):
            return reverse_lazy('restaurant_admin:dashboard', kwargs={'restaurant_slug': profile.restaurant.slug})
    except Exception:
        pass

    # Superusers or staff without a restaurant go to super admin
    if user.is_superuser or user.is_staff:
        return reverse_lazy('super_admin:dashboard')

    return reverse_lazy('login')


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
        context['frontend_url'] = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
        return context

    def get_success_url(self):
        next_url = self.request.GET.get('next') or self.request.POST.get('next')
        return resolve_user_redirect(self.request, self.request.user, next_url)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            slug = self._user_restaurant_slug(request.user)
            if slug:
                return redirect('restaurant_admin:dashboard', restaurant_slug=slug)
            if request.user.is_superuser or request.user.is_staff:
                return redirect('super_admin:dashboard')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """
        Intercepts successful credential verification and triggers OTP flow if 2FA is active.
        """
        user = form.get_user()
        
        # Check user's 2FA preference (ON by default)
        user_2fa_active = True
        try:
            from super_admin.models import UserSecuritySetting
            sec_setting = UserSecuritySetting.objects.filter(user=user).first()
            if sec_setting is not None:
                user_2fa_active = sec_setting.is_2fa_enabled
            else:
                user_2fa_active = True
        except Exception:
            user_2fa_active = True

        # If 2FA is active and user has an email configured, require OTP 2FA
        if user_2fa_active and user.email and user.email.strip():
            x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
            ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else self.request.META.get('REMOTE_ADDR')

            try:
                otp_record = EmailOTP.generate_otp(
                    user=user,
                    email=user.email.strip(),
                    purpose=EmailOTP.Purpose.LOGIN,
                    expiry_minutes=getattr(settings, 'OTP_EXPIRY_MINUTES', 10),
                    max_attempts=getattr(settings, 'OTP_MAX_ATTEMPTS', 5),
                    ip_address=ip,
                )
                success, error = send_otp_email(user, otp_record.otp_code, purpose="login", request=self.request)
                if not success:
                    logger.warning(f"Could not dispatch OTP email to {user.email}: {error}")
            except Exception as e:
                logger.error(f"Error generating OTP for user {user.username}: {e}", exc_info=True)

            # Store pre-auth state in session
            self.request.session['pre_auth_user_id'] = user.id
            self.request.session['pre_auth_backend'] = getattr(user, 'backend', 'core.auth_backends.EmailOrUsernameModelBackend')
            self.request.session['pre_auth_next'] = self.request.POST.get('next') or self.request.GET.get('next') or ''
            
            return redirect('verify_otp')

        # If 2FA is disabled, or fallback for legacy accounts without email configured
        if user_2fa_active and (not user.email or not user.email.strip()):
            messages.warning(
                self.request,
                "Your account has Two-Factor Authentication enabled, but no email address is registered. Please configure an email address in account settings."
            )
        auth_login(self.request, user)
        return redirect(self.get_success_url())




class VerifyOTPView(View):
    """
    Renders and validates the 6-digit Email OTP for user login.
    """
    template_name = 'admin/login_otp.html'

    def get_pre_auth_user(self, request):
        user_id = request.session.get('pre_auth_user_id')
        if not user_id:
            return None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.filter(id=user_id).first()

    def get(self, request, *args, **kwargs):
        user = self.get_pre_auth_user(request)
        if not user:
            return redirect('login')

        active_otp = EmailOTP.objects.filter(
            user=user,
            purpose=EmailOTP.Purpose.LOGIN,
            is_used=False
        ).order_by('-created_at').first()

        cooldown_remaining = 0
        if active_otp:
            cooldown_seconds = getattr(settings, 'OTP_RESEND_COOLDOWN_SECONDS', 60)
            elapsed = (timezone.now() - active_otp.created_at).total_seconds()
            cooldown_remaining = max(0, int(cooldown_seconds - elapsed))

        context = {
            'masked_email': mask_email(user.email),
            'email': user.email,
            'expiry_minutes': getattr(settings, 'OTP_EXPIRY_MINUTES', 10),
            'cooldown_remaining': cooldown_remaining,
            'next': request.session.get('pre_auth_next', ''),
            'frontend_url': getattr(settings, 'FRONTEND_URL', 'http://localhost:3000'),
        }
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        user = self.get_pre_auth_user(request)
        if not user:
            return redirect('login')

        # Read submitted code from single input or separate 6-digit boxes
        code_input = request.POST.get('otp_code', '').strip()
        if not code_input:
            digits = [request.POST.get(f'digit_{i}', '') for i in range(1, 7)]
            code_input = ''.join(digits).strip()

        if not code_input:
            messages.error(request, "Please enter the 6-digit verification code.")
            return self.get(request, *args, **kwargs)

        active_otp = EmailOTP.objects.filter(
            user=user,
            purpose=EmailOTP.Purpose.LOGIN,
            is_used=False
        ).order_by('-created_at').first()

        if not active_otp:
            messages.error(request, "No active verification code found. Please request a new one.")
            return self.get(request, *args, **kwargs)

        is_valid, error_message = active_otp.verify(code_input)

        if not is_valid:
            messages.error(request, error_message or "Invalid verification code.")
            return self.get(request, *args, **kwargs)

        # Successful OTP validation! Log in user
        backend = request.session.get('pre_auth_backend', 'core.auth_backends.EmailOrUsernameModelBackend')
        auth_login(request, user, backend=backend)

        # Clear pre-auth session data
        next_url = request.session.pop('pre_auth_next', None)
        request.session.pop('pre_auth_user_id', None)
        request.session.pop('pre_auth_backend', None)

        target_url = resolve_user_redirect(request, user, next_url)
        return redirect(target_url)


class ResendOTPView(View):
    """
    Handles resending verification OTP with cooldown rate-limiting.
    """
    def post(self, request, *args, **kwargs):
        user_id = request.session.get('pre_auth_user_id')
        if not user_id:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
                return JsonResponse({'success': False, 'message': 'Session expired. Please log in again.'}, status=400)
            return redirect('login')

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(id=user_id).first()
        if not user or not user.email:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
                return JsonResponse({'success': False, 'message': 'User has no valid email address.'}, status=400)
            return redirect('login')

        active_otp = EmailOTP.objects.filter(
            user=user,
            purpose=EmailOTP.Purpose.LOGIN,
        ).order_by('-created_at').first()

        cooldown_seconds = getattr(settings, 'OTP_RESEND_COOLDOWN_SECONDS', 60)
        if active_otp:
            elapsed = (timezone.now() - active_otp.created_at).total_seconds()
            if elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
                    return JsonResponse({
                        'success': False,
                        'message': f"Please wait {remaining} seconds before requesting a new code.",
                        'cooldown_remaining': remaining
                    }, status=429)
                messages.warning(request, f"Please wait {remaining} seconds before requesting a new code.")
                return redirect('verify_otp')

        # Generate fresh OTP
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.META.get('REMOTE_ADDR')

        new_otp = EmailOTP.generate_otp(
            user=user,
            email=user.email,
            purpose=EmailOTP.Purpose.LOGIN,
            expiry_minutes=getattr(settings, 'OTP_EXPIRY_MINUTES', 10),
            max_attempts=getattr(settings, 'OTP_MAX_ATTEMPTS', 5),
            ip_address=ip,
        )

        success, error = send_otp_email(user, new_otp.otp_code, purpose="login", request=request)

        if not success:
            logger.warning(f"Could not resend OTP to {user.email}: {error}")
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
                return JsonResponse({'success': False, 'message': f"Failed to send email: {error}"}, status=500)
            messages.error(request, f"Could not send email: {error}")
            return redirect('verify_otp')

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
            return JsonResponse({
                'success': True,
                'message': 'A new verification code has been sent to your email.',
                'cooldown_remaining': cooldown_seconds
            })

        messages.success(request, 'A new verification code has been sent to your email.')
        return redirect('verify_otp')

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)


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
        from django.utils.http import url_has_allowed_host_and_scheme

        next_page = request.GET.get('next') or request.POST.get('next')
        auth_logout(request)
        
        # Clear any leftover pre-auth session keys
        request.session.pop('pre_auth_user_id', None)
        request.session.pop('pre_auth_backend', None)
        request.session.pop('pre_auth_next', None)

        if next_page and url_has_allowed_host_and_scheme(
            url=next_page,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_page)
        return redirect('login')


@method_decorator(csrf_exempt, name='dispatch')
class SubmitEnquiryView(View):
    """
    Relays modal enquiry submissions server-to-server to master_admin_restro /api/enquiries.
    Eliminates CORS preflight and cross-origin blocking.
    """
    def post(self, request, *args, **kwargs):
        import requests
        import json

        try:
            if request.content_type == 'application/json' and request.body:
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST.dict()

            full_name = (data.get('full_name') or '').strip()
            email = (data.get('email') or '').strip()
            phone = (data.get('phone') or '').strip()
            message = (data.get('message') or '').strip()

            if not full_name or not email or not phone or not message:
                return JsonResponse({
                    'success': False,
                    'error': 'Please fill out all required fields.'
                }, status=400)

            # Master API target
            master_url = getattr(settings, 'MASTER_DASHBOARD_URL', 'http://localhost:3000').rstrip('/')
            target_endpoint = f"{master_url}/api/enquiries"

            payload = {
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'message': message,
            }

            try:
                resp = requests.post(target_endpoint, json=payload, timeout=8)
                if resp.status_code in (200, 201):
                    return JsonResponse({
                        'success': True,
                        'message': 'Enquiry submitted successfully.'
                    })
                else:
                    try:
                        err_json = resp.json()
                        error_msg = err_json.get('error') or err_json.get('message') or f"Server returned {resp.status_code}"
                    except Exception:
                        error_msg = f"Master server returned {resp.status_code}"
                    return JsonResponse({'success': False, 'error': error_msg}, status=resp.status_code)
            except requests.exceptions.RequestException as exc:
                logger.error(f"Failed to connect to MasterPanel enquiry API at {target_endpoint}: {exc}")
                return JsonResponse({
                    'success': False,
                    'error': f"Could not connect to Master Admin on {master_url}. Please ensure the server is active."
                }, status=502)

        except Exception as exc:
            logger.error(f"Unexpected error in SubmitEnquiryView: {exc}", exc_info=True)
            return JsonResponse({'success': False, 'error': 'An unexpected error occurred.'}, status=500)

