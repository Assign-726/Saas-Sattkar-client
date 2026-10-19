"""Where each Zomato / Swiggy setting comes from, and OAuth tokens.

Every value resolves outlet override → brand account (``PlatformAccount``) → Sattkar default
(``settings.DELIVERY_PARTNERS``, from .env). Secrets are decrypted only here, in memory, at
the moment a request is signed or verified.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SECRET_KEYS = ['client_id', 'client_secret', 'access_token', 'webhook_secret']
CONFIG_KEYS = ['api_base_url', 'token_url', 'auth_header', 'signature_header']
SOURCE_LABELS = {'outlet': 'This branch', 'brand': 'Shared brand settings', 'sattkar': 'Server default (.env)', '': 'Not set'}


@dataclass
class Resolved:
    values: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    path_sources: dict = field(default_factory=dict)

    def get(self, key, default=''):
        return self.values.get(key) or default

    @property
    def uses_oauth(self):
        return bool(self.get('client_id') and self.get('client_secret') and self.get('token_url'))

    @property
    def has_auth(self):
        return bool(self.get('access_token') or self.uses_oauth)

    @property
    def api_ready(self):
        return bool(self.get('api_base_url') and self.has_auth)


def env_defaults(provider):
    return dict((getattr(settings, 'DELIVERY_PARTNERS', {}) or {}).get(provider) or {})


def resolve(conn):
    outlet_secrets = conn.get_credentials()
    if outlet_secrets.get('api_key') and not outlet_secrets.get('access_token'):
        outlet_secrets['access_token'] = outlet_secrets['api_key']  # older outlets
    outlet = {**outlet_secrets, 'api_base_url': conn.api_base_url, 'token_url': conn.token_url,
              'auth_header': conn.auth_header if conn.auth_header not in ('', 'Authorization') else '',
              'signature_header': conn.signature_header if conn.signature_header not in ('', 'X-Signature') else ''}
    acc = conn.account
    brand = {}
    if acc:
        brand = {**acc.get_secrets(), 'api_base_url': acc.api_base_url, 'token_url': acc.token_url,
                 'auth_header': acc.auth_header, 'signature_header': acc.signature_header}
    sattkar = env_defaults(conn.provider)
    r = Resolved()
    for key in SECRET_KEYS + CONFIG_KEYS:
        for src, data in (('outlet', outlet), ('brand', brand), ('sattkar', sattkar)):
            if data.get(key):
                r.values[key], r.sources[key] = data[key], src
                break
        else:
            r.sources[key] = ''
    r.values.setdefault('auth_header', 'Authorization')
    r.values['auth_header'] = r.values.get('auth_header') or 'Authorization'
    r.values['signature_header'] = r.values.get('signature_header') or 'X-Signature'
    for src, paths in (('sattkar', sattkar.get('api_paths') or {}), ('brand', (acc.api_paths if acc else {}) or {}), ('outlet', conn.api_paths or {})):
        for action, path in paths.items():
            if path:
                r.paths[action], r.path_sources[action] = path, src
    return r


def webhook_secrets(conn):
    """Every secret an inbound request for this outlet may be signed with (most specific first)."""
    out = []
    for s in (conn.get_webhook_secret(), conn.account.get_secrets().get('webhook_secret') if conn.account else '',
              env_defaults(conn.provider).get('webhook_secret')):
        if s and s not in out:
            out.append(s)
    return out


class TokenError(Exception):
    pass


def _token_cache_key(conn, r):
    return f'delivery-token:{conn.provider}:{r.sources.get("client_id")}:{conn.restaurant_id if r.sources.get("client_id") == "brand" else conn.pk}'


def access_token(conn, force=False, resolved=None):
    """A usable bearer token: static token, or an OAuth client-credentials token (cached, refreshed early)."""
    r = resolved or resolve(conn)
    if not r.uses_oauth:
        return r.get('access_token')
    src = r.sources.get('client_id')
    # Tokens are cached encrypted next to the keys they came from: the branch, the brand, or (for .env keys) the cache.
    holder = conn if src == 'outlet' else (conn.account if src == 'brand' else None)
    if not force:
        if holder is not None:
            tok = holder.get_cached_token()
            if tok:
                return tok
        else:
            tok = cache.get(_token_cache_key(conn, r))
            if tok:
                return tok
    from integrations.dispatch import UnsafeURL, check_url
    import requests
    try:
        check_url(r.get('token_url'))
    except UnsafeURL as exc:
        raise TokenError(f'Token URL: {exc}')
    try:
        resp = requests.post(r.get('token_url'), data={'grant_type': 'client_credentials', 'client_id': r.get('client_id'),
                                                       'client_secret': r.get('client_secret')},
                             headers={'Accept': 'application/json'}, timeout=10, allow_redirects=False)
    except requests.RequestException as exc:
        raise TokenError(f'Could not reach the token endpoint ({exc.__class__.__name__}).')
    if resp.status_code != 200:
        raise TokenError(f'The platform refused the client ID / secret ({resp.status_code}).')
    try:
        data = resp.json()
    except ValueError:
        raise TokenError('The token endpoint did not return JSON.')
    token = data.get('access_token') if isinstance(data, dict) else None
    if not token:
        raise TokenError('No access_token in the token response.')
    try:
        ttl = max(60, int(data.get('expires_in') or 3600))
    except (TypeError, ValueError):
        ttl = 3600
    ttl = max(30, ttl - 120)  # refresh two minutes early
    if holder is not None:
        holder.cache_token(token, timezone.now() + timedelta(seconds=ttl))
    else:
        cache.set(_token_cache_key(conn, r), token, ttl)
    return token


def describe(conn):
    """Rows for the Settings page: what is configured and where it comes from (never the secret itself)."""
    from integrations.delivery.crypto import mask
    r = resolve(conn)
    rows = []
    for key, label in (('client_id', 'Client ID'), ('client_secret', 'Client secret'), ('access_token', 'Access token'),
                       ('webhook_secret', 'Platform webhook secret'), ('api_base_url', 'API base URL'), ('token_url', 'Token URL')):
        val = r.values.get(key, '')
        shown = val if key in ('api_base_url', 'token_url') else (mask(val) if key != 'client_id' else (val[:4] + '…' if val else ''))
        rows.append({'key': key, 'label': label, 'value': shown, 'source': r.sources.get(key, ''),
                     'source_label': SOURCE_LABELS[r.sources.get(key, '')]})
    return r, rows
