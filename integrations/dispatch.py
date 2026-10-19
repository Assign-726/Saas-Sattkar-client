"""Webhook fan-out and delivery.

``emit(event, branch, data)`` records one delivery per matching endpoint and, once the
surrounding transaction commits, sends them from a background thread so a slow partner
never slows the till. Failed deliveries are retried with back-off by the
``deliver_webhooks`` command (run it every minute from cron) or from the delivery log.

Each request is signed: ``X-Webhook-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>``.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

USER_AGENT = 'HospitalitySuite-Webhooks/1.0'
TIMEOUT_SECONDS = 8


# --------------------------------------------------------------------------
# URL safety — never let a webhook reach into our own network
# --------------------------------------------------------------------------

class UnsafeURL(ValueError):
    pass


def check_url(url):
    """Raise ``UnsafeURL`` unless ``url`` is a public http(s) address. Plain http is allowed only in DEBUG."""
    parsed = urlparse(url or '')
    if parsed.scheme not in ('https', 'http') or not parsed.hostname:
        raise UnsafeURL('Enter a full URL starting with https://')
    if parsed.scheme == 'http' and not settings.DEBUG:
        raise UnsafeURL('Webhook URLs must use https://')
    if settings.DEBUG and getattr(settings, 'WEBHOOKS_ALLOW_PRIVATE_IN_DEBUG', True):
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    except socket.gaierror:
        raise UnsafeURL('That host name does not resolve.')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeURL('Webhook URLs must point to a public internet address.')


def sign(secret, timestamp, body):
    mac = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={mac}'


def verify(secret, header, body, tolerance=300):
    """Helper for partners and tests: check an ``X-Webhook-Signature`` header."""
    try:
        parts = dict(p.split('=', 1) for p in header.split(','))
        ts = int(parts['t'])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), header)


# --------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------

def endpoints_for(event, restaurant, branch):
    from django.db.models import Q
    from integrations.models import WebhookEndpoint
    qs = WebhookEndpoint.objects.filter(restaurant=restaurant, is_active=True)
    qs = qs.filter(Q(branch__isnull=True) | Q(branch=branch)) if branch is not None else qs.filter(branch__isnull=True)
    return [ep for ep in qs if event in (ep.events or [])]


def emit(event, *, restaurant, branch, data):
    """Queue ``event`` for every endpoint subscribed to it. Never raises."""
    try:
        from integrations.models import WebhookDelivery
        if restaurant is None:
            return []
        targets = endpoints_for(event, restaurant, branch)
        if not targets:
            return []
        event_id = 'evt_' + uuid.uuid4().hex[:24]
        envelope = {
            'id': event_id, 'event': event, 'created_at': timezone.now().isoformat(),
            'business': {'id': restaurant.id, 'name': restaurant.name, 'slug': restaurant.slug},
            'branch': {'id': branch.id, 'name': branch.name, 'slug': branch.slug} if branch else None,
            'data': data,
        }
        envelope = json.loads(json.dumps(envelope, cls=DjangoJSONEncoder))
        deliveries = [WebhookDelivery.objects.create(endpoint=ep, event=event, event_id=event_id, payload=envelope) for ep in targets]
        ids = [d.id for d in deliveries]
        transaction.on_commit(lambda: _send_in_background(ids))
        return deliveries
    except Exception:
        logger.exception('Could not queue webhook %s', event)
        return []


def _send_in_background(ids):
    if getattr(settings, 'WEBHOOKS_SYNC', False):
        _deliver_ids(ids)
        return
    threading.Thread(target=_deliver_ids, args=(ids,), daemon=True, name='webhook-delivery').start()


def _deliver_ids(ids):
    from integrations.models import WebhookDelivery
    try:
        for d in WebhookDelivery.objects.filter(id__in=ids).select_related('endpoint'):
            deliver(d)
    except Exception:
        logger.exception('Webhook delivery thread failed')
    finally:
        if threading.current_thread() is not threading.main_thread():
            close_old_connections()


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def deliver(delivery):
    """Send one delivery now and record the outcome. Returns True on a 2xx."""
    import requests
    from integrations.models import WebhookDelivery

    ep = delivery.endpoint
    body = json.dumps(delivery.payload, separators=(',', ':'), cls=DjangoJSONEncoder).encode()
    ts = int(time.time())
    headers = {
        'Content-Type': 'application/json', 'User-Agent': USER_AGENT,
        'X-Webhook-Event': delivery.event, 'X-Webhook-Id': delivery.event_id,
        'X-Webhook-Timestamp': str(ts), 'X-Webhook-Signature': sign(ep.secret, ts, body),
    }
    delivery.attempts += 1
    started = time.monotonic()
    code, text, error = None, '', ''
    try:
        check_url(ep.url)
        resp = requests.post(ep.url, data=body, headers=headers, timeout=TIMEOUT_SECONDS, allow_redirects=False)
        code, text = resp.status_code, (resp.text or '')[:1000]
    except UnsafeURL as exc:
        error = str(exc)
    except Exception as exc:  # network errors, timeouts
        error = f'{type(exc).__name__}: {exc}'[:300]
    delivery.duration_ms = int((time.monotonic() - started) * 1000)
    delivery.response_code, delivery.response_body, delivery.error = code, text, error

    now = timezone.now()
    ok = code is not None and 200 <= code < 300
    ep.refresh_from_db(fields=['failure_streak', 'is_active', 'disabled_reason'])
    if ok:
        delivery.status, delivery.delivered_at, delivery.next_attempt_at = WebhookDelivery.Status.SUCCESS, now, None
        ep.failure_streak = 0
    else:
        if delivery.attempts >= WebhookDelivery.MAX_ATTEMPTS:
            delivery.status, delivery.next_attempt_at = WebhookDelivery.Status.GAVE_UP, None
        else:
            wait = WebhookDelivery.BACKOFF_MINUTES[min(delivery.attempts - 1, len(WebhookDelivery.BACKOFF_MINUTES) - 1)]
            delivery.status, delivery.next_attempt_at = WebhookDelivery.Status.FAILED, now + timedelta(minutes=wait)
        ep.failure_streak += 1
        if ep.failure_streak >= ep.AUTO_DISABLE_AFTER and ep.is_active:
            ep.is_active = False
            ep.disabled_reason = f'Turned off after {ep.failure_streak} failed deliveries in a row ({now:%d %b %H:%M}).'
    delivery.save()
    ep.last_delivery_at, ep.last_status_code = now, code
    ep.save(update_fields=['failure_streak', 'is_active', 'disabled_reason', 'last_delivery_at', 'last_status_code'])
    return ok


def retry_due(limit=200):
    """Send every failed delivery whose back-off has elapsed. Returns (sent, succeeded)."""
    from integrations.models import WebhookDelivery
    due = (WebhookDelivery.objects.filter(status=WebhookDelivery.Status.FAILED, next_attempt_at__lte=timezone.now(), endpoint__is_active=True)
           .select_related('endpoint').order_by('next_attempt_at')[:limit])
    sent = ok = 0
    for d in due:
        sent += 1
        ok += deliver(d)
    return sent, ok


def send_test(endpoint):
    from integrations.models import WebhookDelivery
    payload = {
        'id': 'evt_test_' + uuid.uuid4().hex[:16], 'event': 'webhook.test', 'created_at': timezone.now().isoformat(),
        'business': {'id': endpoint.restaurant_id, 'name': endpoint.restaurant.name, 'slug': endpoint.restaurant.slug},
        'branch': {'id': endpoint.branch_id, 'name': endpoint.branch.name, 'slug': endpoint.branch.slug} if endpoint.branch else None,
        'data': {'message': 'This is a test delivery. If you can read this, the endpoint works.'},
    }
    d = WebhookDelivery.objects.create(endpoint=endpoint, event='webhook.test', event_id=payload['id'], payload=payload)
    deliver(d)
    return d
