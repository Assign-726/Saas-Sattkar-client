import os
import time
import json
import hmac
import hashlib
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class MasterBillingService:
    """
    Client service for communicating with MasterRestro Master Admin Cloud.
    Retrieves live subscription contracts, plans, renewal countdown timers, and invoices.
    """

    @staticmethod
    def get_config():
        master_url = getattr(settings, 'MASTER_DASHBOARD_URL', None) or os.environ.get('MASTER_DASHBOARD_URL', 'http://localhost:3000')
        master_secret = getattr(settings, 'MASTER_API_SECRET', None) or os.environ.get('MASTER_API_SECRET', 'T9@fK!xR!!72m$Qv%Lp&6Za!Wc@3Ns')
        return master_url.rstrip('/'), master_secret

    @classmethod
    def get_headers(cls, body_json: str = None):
        _, secret = cls.get_config()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        }
        if body_json is not None:
            timestamp = str(int(time.time()))
            message = f"{timestamp}.{body_json}".encode('utf-8')
            signature = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
            headers["X-Timestamp"] = timestamp
            headers["X-Signature"] = signature
        return headers

    @classmethod
    def fetch_subscription_details(cls, restaurant=None) -> dict:
        """
        Fetches live subscription details from Master Admin.
        Falls back to local restaurant state if Master Admin is offline or unreachable.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"

        params = {}
        if restaurant:
            # Send email, phone, and business name for seamless matching without strict domain/license checks
            email = getattr(restaurant, 'email', None)
            if email:
                params['email'] = email.strip()
            phone = getattr(restaurant, 'phone', None)
            if phone:
                params['phone'] = phone.strip()
            trade_name = getattr(restaurant, 'trade_name', '') or getattr(restaurant, 'name', '')
            if trade_name:
                params['business_name'] = trade_name.strip()
            license_key = getattr(restaurant, 'master_license_key', None)
            if license_key:
                params['license_key'] = license_key
            slug = getattr(restaurant, 'slug', None)
            if slug:
                params['domain'] = slug

        headers = cls.get_headers()

        try:
            resp = requests.get(endpoint, params=params, headers=headers, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    data['connection_status'] = 'CONNECTED'
                    data['master_url'] = master_url

                    # Auto-bind / update local license key when matched from Master Admin
                    client_payload = data.get('client', {})
                    master_key = client_payload.get('license_key')
                    if restaurant and master_key:
                        current_key = getattr(restaurant, 'master_license_key', '')
                        current_status = getattr(restaurant, 'master_sync_status', '')
                        if current_key != master_key or current_status != 'SYNCED':
                            try:
                                from super_admin.models import Restaurant
                                Restaurant.objects.filter(pk=restaurant.pk).update(
                                    master_license_key=master_key,
                                    master_sync_status='SYNCED'
                                )
                                restaurant.master_license_key = master_key
                                restaurant.master_sync_status = 'SYNCED'
                                logger.info(f"[MasterBillingService] Auto-bound license key '{master_key}' to '{restaurant.name}'")
                            except Exception as db_err:
                                logger.warning(f"[MasterBillingService] Could not persist license key to local DB: {db_err}")

                    # Normalize plan fields so price and monthly_fee are consistently available
                    if 'available_plans' in data and isinstance(data['available_plans'], list):
                        for p in data['available_plans']:
                            p_price = p.get('price') if p.get('price') is not None else p.get('monthly_fee')
                            if p_price is None:
                                p_price = p.get('monthlyPrice') or p.get('amount') or 0
                            p['price'] = p_price
                            p['monthly_fee'] = p_price
                            if 'setup_fee' not in p or p.get('setup_fee') is None:
                                p['setup_fee'] = p.get('setupFee') or 0

                    if 'current_plan' in data and isinstance(data['current_plan'], dict):
                        cp = data['current_plan']
                        cp_price = cp.get('price') if cp.get('price') is not None else cp.get('monthly_fee')
                        if cp_price is None:
                            cp_price = cp.get('monthlyPrice') or cp.get('amount') or 0
                        cp['price'] = cp_price
                        cp['monthly_fee'] = cp_price
                        if 'setup_fee' not in cp or cp.get('setup_fee') is None:
                            cp['setup_fee'] = cp.get('setupFee') or 0

                    return data
            logger.warning(f"[MasterBillingService] Server returned status {resp.status_code}: {resp.text[:200]}")
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = "SERVER_ERROR"
            fallback["master_url"] = master_url
            fallback["message"] = f"Master Admin returned status {resp.status_code}"
            return fallback
        except requests.exceptions.RequestException as exc:
            logger.warning(f"[MasterBillingService] Master Admin offline / unreachable: {exc}")
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = "OFFLINE"
            fallback["master_url"] = master_url
            fallback["message"] = f"Could not connect to Master Admin at {master_url}."
            return fallback
        except Exception as exc:
            logger.error(f"[MasterBillingService] Unexpected error: {exc}")
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = "ERROR"
            fallback["master_url"] = master_url
            fallback["message"] = str(exc)
            return fallback

    @classmethod
    def submit_upgrade_request(cls, restaurant, requested_plan_name: str, requested_billing_cycle: str = "MONTHLY", notes: str = "", contact_phone: str = "", contact_email: str = "") -> dict:
        """
        Pushes a plan change / renewal upgrade request to Master Admin enquiries desk.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"

        payload = {
            "license_key": getattr(restaurant, 'master_license_key', '') if restaurant else "",
            "domain": getattr(restaurant, 'slug', '') if restaurant else "",
            "business_name": (getattr(restaurant, 'trade_name', '') or getattr(restaurant, 'name', '')) if restaurant else "Restaurant Super Admin",
            "action": "UPGRADE_OR_RENEWAL_REQUEST",
            "requested_plan_name": requested_plan_name,
            "requested_billing_cycle": requested_billing_cycle,
            "notes": notes,
            "contact_phone": contact_phone or (getattr(restaurant, 'phone', '') if restaurant else ""),
            "contact_email": contact_email or (getattr(restaurant, 'email', '') if restaurant else ""),
        }

        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            return {"status": "error", "message": f"Master Admin returned status {resp.status_code}: {resp.text}"}
        except Exception as exc:
            return {"status": "error", "message": f"Network error contacting Master Admin: {exc}"}

    @classmethod
    def _build_local_fallback(cls, restaurant=None) -> dict:
        """
        Builds local data structure using actual local DB models when Master Admin is unreachable.
        Does NOT inject fake subscriptions, mock plans, or hardcoded test license keys.
        """
        branch_count = 0
        try:
            if restaurant and hasattr(restaurant, 'branches'):
                branch_count = restaurant.branches.count()
        except Exception:
            branch_count = 0

        business_name = ""
        legal_name = ""
        domain = ""
        license_key = ""
        email = ""
        phone = ""

        if restaurant:
            business_name = getattr(restaurant, 'trade_name', '') or getattr(restaurant, 'name', '') or ""
            legal_name = getattr(restaurant, 'legal_name', '') or ""
            domain = getattr(restaurant, 'slug', '') or ""
            license_key = getattr(restaurant, 'master_license_key', '') or ""
            email = getattr(restaurant, 'email', '') or ""
            phone = getattr(restaurant, 'phone', '') or ""

        return {
            "status": "offline",
            "connection_status": "OFFLINE",
            "message": "Master Admin is currently not connected or unreachable.",
            "client": {
                "business_name": business_name or "Registered Business",
                "legal_name": legal_name,
                "domain": domain,
                "license_key": license_key or "Not Assigned",
                "subscription_status": "NOT_CONNECTED",
                "email": email,
                "phone": phone,
                "branch_count": branch_count,
            },
            "subscription": None,
            "current_plan": None,
            "available_plans": [],
            "invoices": [],
        }
