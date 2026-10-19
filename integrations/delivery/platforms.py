"""Zomato and Swiggy adapters.

This is the only module that reads or writes a partner's wire format. Everything else in the
app works with the normalised shapes returned here.

About the wire format
---------------------
Zomato and Swiggy share their restaurant-POS API contracts only with approved integration
partners. Until Sattkar has those documents and test payloads, each adapter:

* accepts the **Sattkar canonical format** (documented on the Settings tab and used by the
  sandbox simulator) and the common field spellings of both platforms' order relays, through
  the alias tables below — extend ``ALIASES`` / ``STATUS_MAP`` from the partner's fixtures;
* sends actions only to endpoint paths configured per outlet (``Connection.api_paths``), never
  to guessed URLs. An action with no configured path is reported as *unsupported*, and an
  outlet in *manual* mode records the action for staff to repeat on the partner tablet.
"""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime

PLATFORMS = {
    'zomato': {
        'key': 'zomato', 'label': 'Zomato', 'color': '#E23744', 'rgb': '226, 55, 68', 'initial': 'Z',
        'tagline': 'Orders, menu sync and payouts for your Zomato outlet',
        'partner_portal': 'https://www.zomato.com/developer/integration/docs/overview/',
        'reject_reasons': [
            ('items_out_of_stock', 'Items out of stock'),
            ('kitchen_busy', 'Kitchen is too busy'),
            ('outlet_closing', 'Outlet is closing'),
            ('cannot_serve_address', 'Cannot serve this order'),
            ('other', 'Other'),
        ],
        # Zomato's own riders mark pickup; the restaurant confirms, rejects and marks ready.
        'capabilities': {'accept': True, 'reject': True, 'mark_ready': True, 'mark_picked_up': False,
                         'item_availability': True, 'store_status': True, 'menu_sync': True},
        'settlement_hint': 'Upload the payout / settlement CSV from the Zomato restaurant partner dashboard.',
    },
    'swiggy': {
        'key': 'swiggy', 'label': 'Swiggy', 'color': '#FC8019', 'rgb': '252, 128, 25', 'initial': 'S',
        'tagline': 'Orders, menu sync and payouts for your Swiggy outlet',
        'partner_portal': 'https://developers.swiggy.com/',
        'reject_reasons': [
            ('items_out_of_stock', 'Items out of stock'),
            ('kitchen_busy', 'Kitchen is too busy'),
            ('outlet_closing', 'Outlet is closing'),
            ('price_mismatch', 'Price or menu mismatch'),
            ('other', 'Other'),
        ],
        'capabilities': {'accept': True, 'reject': True, 'mark_ready': True, 'mark_picked_up': True,
                         'item_availability': True, 'store_status': True, 'menu_sync': True},
        'settlement_hint': 'Upload the payout / annexure CSV from the Swiggy partner dashboard.',
    },
}
DELIVERY_PROVIDERS = list(PLATFORMS)
CAPABILITY_LABELS = [
    ('accept', 'Accept orders'), ('reject', 'Reject orders'), ('mark_ready', 'Mark food ready'),
    ('mark_picked_up', 'Confirm hand-over to rider'), ('item_availability', 'Switch items in/out of stock'),
    ('store_status', 'Take the outlet online/offline'), ('menu_sync', 'Push the full menu'),
]
ACTIONS = ['accept', 'reject', 'mark_ready', 'mark_picked_up', 'item_availability', 'store_status', 'menu_sync']


def meta(provider):
    return PLATFORMS[provider]


def default_capabilities(provider):
    return dict(PLATFORMS.get(provider, {}).get('capabilities', {}))


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class Result:
    state: str                      # succeeded | manual | unsupported | retry | failed
    message: str = ''
    code: int = None
    body: str = ''
    data: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.state in ('succeeded', 'manual')


class PayloadError(ValueError):
    """The request is not something we can turn into an order or event."""


def _dec(value, default='0'):
    if value in (None, ''):
        value = default
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise PayloadError(f'"{value}" is not an amount')


def _first(d, *keys, default=None):
    for k in keys:
        cur = d
        for part in k.split('.'):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ''):
            return cur
    return default


def _when(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), tz=timezone.utc)
    dt = parse_datetime(str(value))
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

class PlatformAdapter:
    provider = ''
    # Field spellings, most specific first. Extend from the partner's payload fixtures.
    ALIASES = {
        'event_type': ['event', 'event_type', 'type', 'action'],
        'event_id': ['event_id', 'eventId', 'id', 'request_id'],
        'order_id': ['order.id', 'order.order_id', 'order_id', 'orderId', 'data.order_id', 'order.orderId'],
        'outlet_id': ['order.outlet_id', 'outlet_id', 'res_id', 'restaurant_id', 'outletId', 'order.res_id', 'store_id'],
        'status': ['status', 'order_status', 'order.status', 'state'],
        'items': ['order.items', 'order.cart.items', 'items', 'cart.items', 'order.dishes'],
        'customer_name': ['order.customer.name', 'customer.name', 'customer_name'],
        'customer_phone': ['order.customer.phone', 'customer.phone', 'customer_phone'],
        'customer_address': ['order.customer.address', 'customer.address', 'delivery_address'],
        'instructions': ['order.instructions', 'order.special_instructions', 'instructions', 'special_instructions'],
        'payment': ['order.payment_type', 'order.payment_mode', 'payment_type', 'payment_mode'],
        'placed_at': ['order.created_at', 'order.placed_at', 'created_at', 'placed_at', 'order_time'],
        'accept_by': ['order.accept_by', 'accept_by', 'order.auto_reject_at'],
        'item_total': ['order.amounts.item_total', 'order.item_total', 'order.subtotal', 'amounts.item_total', 'subtotal'],
        'tax_total': ['order.amounts.tax_total', 'order.tax', 'order.taxes', 'amounts.tax_total', 'tax'],
        'discount_total': ['order.amounts.discount_total', 'order.discount', 'amounts.discount_total', 'discount'],
        'merchant_discount': ['order.amounts.merchant_discount', 'order.restaurant_discount', 'amounts.merchant_discount'],
        'packaging_fee': ['order.amounts.packaging_fee', 'order.packaging_charge', 'amounts.packaging_fee', 'packaging_charge'],
        'delivery_fee': ['order.amounts.delivery_fee', 'order.delivery_charge', 'amounts.delivery_fee'],
        'payable': ['order.amounts.customer_payable', 'order.total', 'order.order_total', 'order.bill_total', 'amounts.customer_payable', 'total'],
        'rider_name': ['rider.name', 'order.rider.name', 'delivery_partner.name', 'rider_name'],
        'rider_phone': ['rider.phone', 'order.rider.phone', 'delivery_partner.phone', 'rider_phone'],
        'rider_eta': ['rider.eta', 'order.rider.eta', 'rider_eta', 'delivery_partner.eta'],
        'pickup_code': ['order.pickup_code', 'pickup_code', 'order.otp', 'rider.otp'],
        'reason': ['reason', 'cancel_reason', 'order.cancel_reason', 'cancellation_reason'],
        'cancelled_by': ['cancelled_by', 'order.cancelled_by', 'initiated_by'],
        'available': ['available', 'in_stock', 'is_available'],
        'item_id': ['item_id', 'external_item_id'],
        'store_open': ['is_open', 'store_open', 'outlet_open', 'online'],
        'sync_reference': ['reference', 'job_id', 'menu_job_id', 'request_id'],
        'sync_errors': ['errors', 'failures'],
    }
    # Partner event / status words → our normalised event types
    EVENT_MAP = {
        'order.placed': 'order.placed', 'order_placed': 'order.placed', 'new_order': 'order.placed', 'order.new': 'order.placed',
        'order.relay': 'order.placed', 'order.created': 'order.placed',
        'order.cancelled': 'order.cancelled', 'order_cancelled': 'order.cancelled', 'cancelled': 'order.cancelled',
        'order.status': 'order.status', 'order_status_update': 'order.status', 'status_update': 'order.status',
        'rider.update': 'rider.update', 'rider_assigned': 'rider.update', 'rider.assigned': 'rider.update',
        'rider_arrived': 'rider.update', 'rider.arrived': 'rider.update', 'delivery_partner_update': 'rider.update',
        'menu.sync_result': 'menu.sync_result', 'menu_status': 'menu.sync_result', 'catalogue_status': 'menu.sync_result',
        'store.status': 'store.status', 'outlet_status': 'store.status', 'store_status': 'store.status',
        'item.stock': 'item.stock', 'item_stock_update': 'item.stock',
    }
    STATUS_MAP = {
        'picked_up': 'picked_up', 'pickedup': 'picked_up', 'dispatched': 'picked_up', 'out_for_delivery': 'picked_up',
        'delivered': 'delivered', 'completed': 'delivered',
        'cancelled': 'cancelled', 'canceled': 'cancelled',
        'accepted': 'accepted', 'confirmed': 'accepted', 'ready': 'ready', 'food_ready': 'ready',
    }

    def __init__(self, connection):
        self.connection = connection

    def get(self, payload, name, default=None):
        return _first(payload, *self.ALIASES[name], default=default)

    # ---- inbound -------------------------------------------------------------------------

    def verify(self, raw_body, headers):
        """HMAC-SHA256 of the raw body (hex or base64, optional ``sha256=`` prefix), or the secret as a
        bearer token. Any of the outlet's, the brand's or Sattkar's webhook secrets is accepted, so a
        platform-issued secret and Sattkar's own per-outlet secret both work. Constant-time comparisons."""
        import base64
        from integrations.delivery.credentials import resolve, webhook_secrets
        secrets_ = [s.encode() for s in webhook_secrets(self.connection)]
        if not secrets_:
            return False
        header = resolve(self.connection).get('signature_header', 'X-Signature')
        sig = (headers.get(header) or headers.get('X-Signature') or '').strip()
        if sig:
            sig = sig.split('=', 1)[1] if sig.lower().startswith('sha256=') else sig
            for secret in secrets_:
                digest = hmac.new(secret, raw_body, hashlib.sha256)
                if hmac.compare_digest(sig, digest.hexdigest()) or hmac.compare_digest(sig, base64.b64encode(digest.digest()).decode()):
                    return True
            return False
        auth = (headers.get('Authorization') or '').strip()
        if auth.lower().startswith('bearer '):
            token = auth[7:].strip().encode()
            return any(hmac.compare_digest(token, secret) for secret in secrets_)
        return False

    def event_type(self, payload):
        raw = str(self.get(payload, 'event_type', '') or '').strip().lower()
        if raw in self.EVENT_MAP:
            return self.EVENT_MAP[raw]
        if not raw and self.get(payload, 'items'):
            return 'order.placed'
        status = str(self.get(payload, 'status', '') or '').lower()
        if status in ('cancelled', 'canceled'):
            return 'order.cancelled'
        if status:
            return 'order.status'
        return raw or 'unknown'

    def dedupe_key(self, payload, raw_body):
        event_id = self.get(payload, 'event_id')
        etype = self.event_type(payload)
        order_id = self.get(payload, 'order_id', '')
        if event_id and str(event_id) != str(order_id):
            return f'evt:{event_id}'[:200]
        # No stable event ID: an order can only be placed or cancelled once; status updates are keyed by status.
        if etype in ('order.placed', 'order.cancelled'):
            return f'{etype}:{order_id}'[:200]
        if etype in ('order.status', 'rider.update'):
            status = self.get(payload, 'status', '') or self.get(payload, 'rider_name', '')
            return f'{etype}:{order_id}:{status}'[:200]
        return 'sha:' + hashlib.sha256(raw_body).hexdigest()

    def normalize_status(self, payload):
        return self.STATUS_MAP.get(str(self.get(payload, 'status', '') or '').strip().lower())

    def normalize_order(self, payload):
        order_id = self.get(payload, 'order_id')
        if not order_id:
            raise PayloadError('The order has no ID.')
        raw_items = self.get(payload, 'items')
        if not isinstance(raw_items, list) or not raw_items:
            raise PayloadError('The order has no items.')
        items = []
        for idx, it in enumerate(raw_items):
            if not isinstance(it, dict):
                raise PayloadError(f'Item {idx + 1} is not an object.')
            qty = _first(it, 'quantity', 'qty', 'count', default=1)
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                raise PayloadError(f'Item {idx + 1} has an invalid quantity.')
            if qty <= 0:
                raise PayloadError(f'Item {idx + 1} has an invalid quantity.')
            addons = []
            for group in ('addons', 'add_ons', 'customizations', 'variants', 'modifiers'):
                for a in it.get(group) or []:
                    if isinstance(a, dict):
                        addons.append({'name': str(_first(a, 'name', 'title', default=''))[:80], 'price': str(_dec(_first(a, 'price', 'unit_price', default=0)))})
            items.append({
                'external_id': str(_first(it, 'external_item_id', 'item_id', 'id', 'itemId', default=''))[:120],
                'variant_id': str(_first(it, 'variant_id', 'variantId', default=''))[:120],
                'name': str(_first(it, 'name', 'title', default='Item'))[:200],
                'quantity': qty,
                'unit_price': _dec(_first(it, 'unit_price', 'price', default=0)),
                'addons': addons,
                'notes': str(_first(it, 'notes', 'instructions', default=''))[:300],
            })
        item_total = _dec(self.get(payload, 'item_total'), default=None) if self.get(payload, 'item_total') is not None else \
            sum((i['unit_price'] * i['quantity'] for i in items), Decimal('0'))
        payable = self.get(payload, 'payable')
        out = {
            'order_id': str(order_id)[:120],
            'outlet_id': str(self.get(payload, 'outlet_id', '') or ''),
            'customer': {
                'name': str(self.get(payload, 'customer_name', '') or '')[:120],
                'phone': str(self.get(payload, 'customer_phone', '') or '')[:20],
                'address': str(self.get(payload, 'customer_address', '') or '')[:300],
            },
            'items': items,
            'instructions': str(self.get(payload, 'instructions', '') or '')[:500],
            'payment': 'cod' if str(self.get(payload, 'payment', '') or '').lower() in ('cod', 'cash', 'cash_on_delivery') else 'prepaid',
            'placed_at': _when(self.get(payload, 'placed_at')),
            'accept_by': _when(self.get(payload, 'accept_by')),
            'pickup_code': str(self.get(payload, 'pickup_code', '') or '')[:20],
            'amounts': {
                'item_total': item_total,
                'tax_total': _dec(self.get(payload, 'tax_total')),
                'discount_total': _dec(self.get(payload, 'discount_total')),
                'merchant_discount': _dec(self.get(payload, 'merchant_discount')),
                'packaging_fee': _dec(self.get(payload, 'packaging_fee')),
                'delivery_fee': _dec(self.get(payload, 'delivery_fee')),
            },
        }
        a = out['amounts']
        a['customer_payable'] = _dec(payable) if payable is not None else \
            a['item_total'] + a['tax_total'] + a['packaging_fee'] + a['delivery_fee'] - a['discount_total']
        if a['customer_payable'] < 0:
            raise PayloadError('The order total is negative.')
        return out

    def rider(self, payload):
        return {
            'name': str(self.get(payload, 'rider_name', '') or '')[:120],
            'phone': str(self.get(payload, 'rider_phone', '') or '')[:20],
            'eta': _when(self.get(payload, 'rider_eta')),
            'status': str(self.get(payload, 'status', '') or '')[:30],
        }

    # ---- outbound ------------------------------------------------------------------------

    def body_for(self, action, link=None, **kw):
        """Request body for an action. Override per platform once the partner contract is known."""
        body = {'outlet_id': self.connection.external_store_id}
        if link is not None:
            body['order_id'] = link.external_id
        body.update({k: v for k, v in kw.items() if v is not None})
        return body

    def send(self, action, link=None, **kw):
        from integrations.delivery.credentials import TokenError, access_token, resolve
        conn = self.connection
        label = meta(self.provider)['label']
        if not conn.can(action):
            return Result('unsupported', f'{label} does not offer “{action.replace("_", " ")}” for this outlet.')
        if conn.mode != conn.Mode.API:
            return Result('manual', f'Recorded. Also do this on the {label} partner app — this outlet is in manual mode.')
        r = resolve(conn)
        path = r.paths.get(action)
        if not r.get('api_base_url') or not path:
            return Result('unsupported', f'No partner endpoint is configured for “{action.replace("_", " ")}”. Add it under Settings.')
        if not r.has_auth:
            return Result('failed', f"No {label} credentials: add this branch's access token or client ID + secret in Settings.")
        from integrations.dispatch import UnsafeURL, check_url
        try:
            url = r.get('api_base_url').rstrip('/') + '/' + path.lstrip('/').format(
                order_id=link.external_id if link else '', outlet_id=conn.external_store_id, item_id=kw.get('item_id', ''))
            check_url(url)
        except (KeyError, IndexError):
            return Result('failed', 'The configured endpoint path has an unknown {placeholder}.')
        except UnsafeURL as exc:
            return Result('failed', str(exc))
        body = json.dumps(self.body_for(action, link, **kw), default=str).encode()
        extra = {**((conn.account.get_secrets().get('extra_headers') if conn.account else None) or {}),
                 **(conn.get_credentials().get('extra_headers') or {})}
        import requests
        started = time.monotonic()
        resp = None
        for attempt in (1, 2):
            try:
                token = access_token(conn, force=attempt == 2, resolved=r)
            except TokenError as exc:
                return Result('retry' if 'reach' in str(exc) else 'failed', str(exc))
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
            header = r.get('auth_header', 'Authorization')
            headers[header] = f'Bearer {token}' if header.lower() == 'authorization' and not token.lower().startswith(('bearer ', 'basic ')) else token
            for k, v in extra.items():
                headers[str(k)[:60]] = str(v)[:300]
            try:
                resp = requests.post(url, data=body, headers=headers, timeout=10, allow_redirects=False)
            except requests.Timeout:
                return Result('retry', f'{label} did not answer within 10 seconds.')
            except requests.RequestException as exc:
                return Result('retry', f'Could not reach {label}: {exc.__class__.__name__}.')
            if resp.status_code == 401 and attempt == 1 and r.uses_oauth:
                continue  # token expired early: fetch a fresh one and try once more
            break
        text = resp.text[:2000]
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            return Result('succeeded', f'Confirmed in {int((time.monotonic() - started) * 1000)} ms.', resp.status_code, text, data if isinstance(data, dict) else {})
        if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
            return Result('retry', f'{label} answered {resp.status_code}; retrying.', resp.status_code, text)
        if resp.status_code in (401, 403):
            return Result('failed', f"{label} rejected our credentials ({resp.status_code}). Check this branch's API keys in Settings.", resp.status_code, text)
        return Result('failed', f'{label} refused the request ({resp.status_code}).', resp.status_code, text)


class ZomatoAdapter(PlatformAdapter):
    provider = 'zomato'
    EVENT_MAP = {**PlatformAdapter.EVENT_MAP, 'order_relay': 'order.placed', 'order.relay.new': 'order.placed',
                 'rider_status': 'rider.update', 'order.rider': 'rider.update'}
    STATUS_MAP = {**PlatformAdapter.STATUS_MAP, 'rider_picked_up': 'picked_up', 'order_delivered': 'delivered',
                  'order_cancelled': 'cancelled', 'merchant_confirmed': 'accepted'}

    def body_for(self, action, link=None, **kw):
        body = super().body_for(action, link, **kw)
        if action == 'accept':
            body['delivery_time'] = kw.get('prep_minutes')
        return body


class SwiggyAdapter(PlatformAdapter):
    provider = 'swiggy'
    EVENT_MAP = {**PlatformAdapter.EVENT_MAP, 'order.new_order': 'order.placed', 'order.de_assigned': 'rider.update',
                 'de_status': 'rider.update'}
    STATUS_MAP = {**PlatformAdapter.STATUS_MAP, 'de_picked_up': 'picked_up', 'de_delivered': 'delivered',
                  'order_picked_up': 'picked_up'}

    def body_for(self, action, link=None, **kw):
        body = super().body_for(action, link, **kw)
        if action == 'accept':
            body['prep_time'] = kw.get('prep_minutes')
        return body


ADAPTERS = {'zomato': ZomatoAdapter, 'swiggy': SwiggyAdapter}


def adapter_for(connection):
    cls = ADAPTERS.get(connection.provider)
    if cls is None:
        raise ValueError(f'{connection.provider} is not a delivery platform')
    return cls(connection)
