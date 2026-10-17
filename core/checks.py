"""Deployment checks: ``manage.py check --deploy`` (and every startup) warns when
production runs with the development fallbacks that ship in settings.py."""
from django.conf import settings
from django.core.checks import Warning, register

_DEV_DEFAULTS = {
    'SECRET_KEY': 'django-insecure-&6l70up',
    'TABLE_QR_SECRET_KEY': 'table_qr_sec_4f9a8b1c',
    'MASTER_API_SECRET': 'T9@fK!xR!!72m',
}


@register(deploy=True)
def insecure_defaults(app_configs, **kwargs):
    issues = []
    for name, prefix in _DEV_DEFAULTS.items():
        if str(getattr(settings, name, '')).startswith(prefix):
            issues.append(Warning(f'{name} is still the development default.',
                                  hint=f'Set {name} in the server .env to a long random value.', id='hospitality.W001'))
    if '*' in settings.ALLOWED_HOSTS:
        issues.append(Warning("ALLOWED_HOSTS contains '*'.", hint='List your real domains in DJANGO_ALLOWED_HOSTS.',
                              id='hospitality.W002'))
    if not getattr(settings, 'DELIVERY_CREDENTIALS_KEY', ''):
        issues.append(Warning('DELIVERY_CREDENTIALS_KEY is empty, so partner credentials cannot be encrypted.',
                              hint='Generate one with Fernet.generate_key().', id='hospitality.W003'))
    if settings.CACHES['default']['BACKEND'].endswith('LocMemCache'):
        issues.append(Warning('Using the in-process cache: rate limits and login lockouts are per worker.',
                              hint='Set REDIS_URL to share them across workers.', id='hospitality.W004'))
    if getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False) and getattr(settings, 'CORS_ALLOW_CREDENTIALS', False):
        issues.append(Warning('CORS allows every origin *with* credentials.',
                              hint='Set CORS_ALLOWED_ORIGINS to your frontend domains instead.', id='hospitality.W005'))
    return issues
