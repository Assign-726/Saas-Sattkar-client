"""Razorpay checks shared by restaurant orders, room bookings and event tickets.

A browser can only prove a payment by sending the ``razorpay_signature`` Razorpay gave it,
and that signature only covers ``order_id|payment_id``. So a payment is genuine only when:

1. the signature is valid for *our* stored Razorpay order id (never one the client picks,
   or a cheap payment for another order could be replayed against an expensive one), and
2. the payment id has not already been used for something else.

Test keys that start with ``mock`` skip the signature only while ``DEBUG`` is on.
"""
import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

API = 'https://api.razorpay.com/v1'


def is_mock_secret(secret):
    return bool(settings.DEBUG and (secret or '').startswith('mock'))


def signature_ok(secret, order_id, payment_id, signature):
    """True only for a valid HMAC of ``order_id|payment_id``. Missing parts are never OK."""
    if is_mock_secret(secret):
        return True
    if not (secret and order_id and payment_id and signature):
        return False
    expected = hmac.new(secret.encode(), f'{order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def fetch_order(key_id, secret, order_id, timeout=8):
    """The Razorpay order (``amount``, ``amount_paid``, ``status``…) or None when it can't
    be fetched. Callers decide whether an unreachable gateway is fatal."""
    if not (key_id and secret and order_id) or is_mock_secret(secret):
        return None
    req = urllib.request.Request(f'{API}/orders/{order_id}')
    token = base64.b64encode(f'{key_id}:{secret}'.encode()).decode()
    req.add_header('Authorization', f'Basic {token}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as exc:
        logger.warning('Razorpay order %s lookup failed: %s', order_id, exc)
    return None


def paise(amount):
    return int(round(float(amount or 0) * 100))
