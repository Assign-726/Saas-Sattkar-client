"""Symmetric encryption for partner credentials.

Uses ``DELIVERY_CREDENTIALS_KEY`` (a Fernet key) when set, otherwise a key derived from
SECRET_KEY. Set a dedicated key in production so rotating SECRET_KEY doesn't lock credentials.
"""
import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = getattr(settings, 'DELIVERY_CREDENTIALS_KEY', '') or ''
    if not key:
        digest = hashlib.sha256(('delivery-credentials:' + settings.SECRET_KEY).encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_json(data):
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token):
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return {}


def encrypt_text(value):
    return _fernet().encrypt(value.encode()).decode() if value else ''


def decrypt_text(token):
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ''


def mask(value, keep=4):
    """Show that a secret exists without revealing it: ••••••••a1b2"""
    if not value:
        return ''
    return '•' * 8 + (value[-keep:] if len(value) > keep * 3 else '')
