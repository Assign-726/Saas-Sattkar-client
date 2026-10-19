"""Email-OTP two-factor sign-in: one rule for the login screen and every settings page."""
from super_admin.models import UserSecuritySetting

# Floor roles sign in on shared devices many times a shift, so they default to no OTP.
OPERATIONAL_ROLES = {'CASHIER', 'KITCHEN', 'WAITER', 'HOUSEKEEPING', 'GATE_SCANNER'}


def is_enabled(user):
    """Whether this login must enter an emailed code. An explicit choice wins; otherwise
    management logins default to on and floor roles to off."""
    try:
        setting = UserSecuritySetting.objects.filter(user=user).first()
        if setting is not None:
            return setting.is_2fa_enabled
        return getattr(user, 'role', None) not in OPERATIONAL_ROLES
    except Exception:
        return True


def set_enabled(user, enabled, password=''):
    """Save the user's choice. Turning it on needs an email on the account; turning it off
    needs the current password, so an unattended open session can't quietly disable it."""
    if enabled and not (user.email or '').strip():
        return 'Add an email address to your account first — the sign-in code is sent there.'
    if not enabled and is_enabled(user) and not user.check_password(password or ''):
        return 'Enter your current password to turn off two-factor sign-in.'
    UserSecuritySetting.objects.update_or_create(user=user, defaults={'is_2fa_enabled': bool(enabled)})
    return None
