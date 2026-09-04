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
        Builds a safe offline fallback data structure using local DB models.
        """
        branch_count = 0
        try:
            if restaurant and hasattr(restaurant, 'branches'):
                branch_count = restaurant.branches.count()
        except Exception:
            branch_count = 0

        business_name = "Restaurant Business"
        if restaurant:
            business_name = getattr(restaurant, 'trade_name', '') or getattr(restaurant, 'name', '') or "Restaurant Business"

        license_key = "LIC-SHA-13831"
        if restaurant:
            license_key = getattr(restaurant, 'master_license_key', '') or "LIC-SHA-13831"

        return {
            "status": "success",
            "connection_status": "OFFLINE",
            "client": {
                "business_name": business_name,
                "legal_name": getattr(restaurant, 'legal_name', '') if restaurant else "",
                "domain": getattr(restaurant, 'slug', '') if restaurant else "",
                "license_key": license_key,
                "subscription_status": "ACTIVE",
                "email": getattr(restaurant, 'email', '') if restaurant else "",
                "phone": getattr(restaurant, 'phone', '') if restaurant else "",
                "branch_count": branch_count,
            },
            "subscription": {
                "plan_name": "Professional Tier",
                "plan_code": "pro",
                "billing_cycle": "MONTHLY",
                "monthly_fee": 2999,
                "setup_fee": 25999,
                "currency": "₹",
                "days_remaining": 28,
                "hours_remaining": 0,
                "minutes_remaining": 0,
                "seconds_remaining": 0,
                "total_seconds_remaining": 28 * 86400,
                "is_overdue": False,
                "is_due_48h": False,
                "is_due_7d": False,
                "status": "ACTIVE",
                "next_billing_date": "2026-09-30",
                "formatted_next_billing": "30 Sept 2026",
            },
            "current_plan": {
                "name": "Professional Tier",
                "code": "pro",
                "price": 2999,
                "monthly_fee": 2999,
                "setup_fee": 25999,
                "billing_cycle": "MONTHLY",
                "description": "Comprehensive package for multi-counter dine-ins, takeaway, KDS, bar operations, and multi-outlet inventory.",
                "max_branches": 3,
                "features": [
                    "Up to 3 Branch Outlets",
                    "Kitchen Display System (KDS)",
                    "Inventory & Recipe Stock Tracking",
                    "Multi-counter Billing & Tax Invoices",
                    "Live Telemetry & Status Monitoring",
                    "Priority WhatsApp & Phone Support",
                ],
                "is_active": True,
            },
            "available_plans": [
                {
                    "name": "Starter Essential",
                    "code": "starter",
                    "price": 1499,
                    "monthly_fee": 1499,
                    "setup_fee": 9999,
                    "billing_cycle": "MONTHLY",
                    "description": "Best for single-outlet cafes & quick-service restaurants.",
                    "max_branches": 1,
                    "features": ["1 Active POS Terminal Outlet", "Digital Menu & QR Ordering", "GST Tax Invoicing & KOT"],
                    "is_current": False,
                    "is_popular": False,
                },
                {
                    "name": "Professional Tier",
                    "code": "pro",
                    "price": 2999,
                    "monthly_fee": 2999,
                    "setup_fee": 25999,
                    "billing_cycle": "MONTHLY",
                    "description": "Comprehensive package for busy dine-ins & growing bar bistros.",
                    "max_branches": 3,
                    "features": ["Up to 3 Branch Outlets", "Kitchen Display System (KDS)", "Inventory & Recipe Stock Tracking", "Multi-counter Billing"],
                    "is_current": True,
                    "is_popular": True,
                },
                {
                    "name": "Enterprise Suite",
                    "code": "enterprise",
                    "price": 6999,
                    "monthly_fee": 6999,
                    "setup_fee": 49999,
                    "billing_cycle": "MONTHLY",
                    "description": "Full-scale multi-chain operations with unlimited capabilities.",
                    "max_branches": 10,
                    "features": ["Up to 10 Branch Outlets", "Unlimited KOT & POS Terminals", "Dedicated 24/7 Account Manager", "Custom ERP & API Integrations"],
                    "is_current": False,
                    "is_popular": False,
                },
            ],
            "invoices": [
                {
                    "id": "INV-2026-0891",
                    "title": "Professional Plan - Monthly SaaS Subscription",
                    "plan_name": "Professional Tier",
                    "period": "01 Sep 2026 - 30 Sep 2026",
                    "date": "2026-09-01",
                    "formatted_date": "01 Sep 2026",
                    "amount": 2999,
                    "currency": "₹",
                    "status": "PAID",
                    "payment_method": "Auto-Debit / UPI",
                },
                {
                    "id": "INV-2026-0742",
                    "title": "Professional Plan - Monthly SaaS Subscription",
                    "plan_name": "Professional Tier",
                    "period": "01 Aug 2026 - 31 Aug 2026",
                    "date": "2026-08-01",
                    "formatted_date": "01 Aug 2026",
                    "amount": 2999,
                    "currency": "₹",
                    "status": "PAID",
                    "payment_method": "Auto-Debit / UPI",
                }
            ],
        }
