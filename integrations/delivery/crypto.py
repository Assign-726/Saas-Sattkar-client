"""Symmetric encryption for partner credentials.

Uses ``DELIVERY_CREDENTIALS_KEY`` (a Fernet key) when set, otherwise a key derived from
SECRET_KEY. Set a dedicated key in production so rotating SECRET_KEY doesn't lock credentials.
"""
import base64
import hashlib
import json

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception
from django.conf import settings


def _fernet():
    if Fernet is None:
        return None
    key = getattr(settings, 'DELIVERY_CREDENTIALS_KEY', '') or ''
    if not key:
        digest = hashlib.sha256(('delivery-credentials:' + settings.SECRET_KEY).encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_json(data):
    f = _fernet()
    if not f:
        return json.dumps(data)
    return f.encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token):
    if not token:
        return {}
    f = _fernet()
    if not f:
        try:
            return json.loads(token)
        except Exception:
            return {}
    try:
        return json.loads(f.decrypt(token.encode()))
    except (InvalidToken, Exception):
        return {}
    except (InvalidToken, ValueError):
        return {}


def encrypt_text(value):
    f = _fernet()
    if not f or not value:
        return value or ''
    return f.encrypt(value.encode()).decode()


def decrypt_text(token):
    if not token:
        return ''
    f = _fernet()
    if not f:
        return token
    try:
        return f.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, Exception):
        return ''
    except (InvalidToken, ValueError):
        return ''


def mask(value, keep=4):
    """Show that a secret exists without revealing it: ••••••••a1b2"""
    if not value:
        return ''
    return '•' * 8 + (value[-keep:] if len(value) > keep * 3 else '')
