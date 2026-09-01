import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from super_admin.models import Restaurant

logger = logging.getLogger(__name__)

def mask_email(email_str):
    """
    Masks an email address for privacy display.
    Example: 'ritik.kumar@example.com' -> 'r***r@example.com'
             'admin@restro.com' -> 'a***n@restro.com'
    """
    if not email_str or '@' not in email_str:
        return email_str or ''
    
    parts = email_str.split('@')
    name_part = parts[0]
    domain_part = parts[1]
    
    if len(name_part) <= 2:
        masked_name = name_part[0] + '***'
    else:
        masked_name = name_part[0] + '***' + name_part[-1]
        
    return f"{masked_name}@{domain_part}"


def send_otp_email(user, otp_code, purpose="login", request=None):
    """
    Sends an OTP verification email to the user.
    Renders both HTML and plain-text email templates.
    """
    if not user or not user.email:
        logger.warning(f"Cannot send OTP: user '{getattr(user, 'username', 'None')}' has no email configured.")
        return False, "User has no email address configured."

    # Resolve branding
    app_name = "Hotel Management System"
    restaurant_name = None
    try:
        profile = getattr(user, 'restaurant_profile', None)
        if profile and profile.restaurant:
            restaurant_name = profile.restaurant.name
            app_name = f"{restaurant_name} Portal"
    except Exception:
        pass

    if purpose == "login":
        subject = f"Your Login Verification Code: {otp_code} - {app_name}"
        template_html = "emails/login_otp.html"
        template_txt = "emails/login_otp.txt"
    elif purpose == "password_reset":
        subject = f"Your Password Reset Code: {otp_code} - {app_name}"
        template_html = "emails/password_reset_otp.html"
        template_txt = "emails/password_reset_otp.txt"
    else:
        subject = f"Your Verification Code: {otp_code} - {app_name}"
        template_html = "emails/login_otp.html"
        template_txt = "emails/login_otp.txt"

    context = {
        'user': user,
        'otp_code': otp_code,
        'app_name': app_name,
        'restaurant_name': restaurant_name,
        'expiry_minutes': getattr(settings, 'OTP_EXPIRY_MINUTES', 10),
        'support_email': getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@restro.com'),
    }

    try:
        html_content = render_to_string(template_html, context)
    except Exception as e:
        logger.warning(f"Could not render HTML email template {template_html}: {e}. Falling back to default text.")
        html_content = None

    try:
        text_content = render_to_string(template_txt, context)
    except Exception:
        text_content = (
            f"Hello {user.first_name or user.username},\n\n"
            f"Your verification code is: {otp_code}\n\n"
            f"This code will expire in {getattr(settings, 'OTP_EXPIRY_MINUTES', 10)} minutes.\n"
            f"If you did not request this, please ignore this email.\n\n"
            f"Regards,\n{app_name}"
        )

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost')
    recipient_list = [user.email]

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, recipient_list)
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        return True, None
    except Exception as exc:
        logger.error(f"Failed to send OTP email to {user.email}: {exc}", exc_info=True)
        return False, f"Could not deliver email: {str(exc)}"

