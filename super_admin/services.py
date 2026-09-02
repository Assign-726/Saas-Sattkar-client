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
            if restaurant.master_license_key:
                params['license_key'] = restaurant.master_license_key
            if restaurant.slug:
                params['domain'] = restaurant.slug

        headers = cls.get_headers()

        try:
            resp = requests.get(endpoint, params=params, headers=headers, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    data['connection_status'] = 'CONNECTED'
                    data['master_url'] = master_url
                    return data
            logger.warning(f"[MasterBillingService] Server returned status {resp.status_code}: {resp.text[:200]}")
            return {
                "status": "error",
                "connection_status": "SERVER_ERROR",
                "message": f"Master Admin returned status {resp.status_code}",
                "master_url": master_url,
                "fallback": cls._build_local_fallback(restaurant)
            }
        except requests.exceptions.ConnectionError as exc:
            logger.warning(f"[MasterBillingService] Master Admin offline / connection error: {exc}")
            return {
                "status": "error",
                "connection_status": "OFFLINE",
                "message": f"Could not connect to Master Admin at {master_url}. Please ensure the server is running.",
                "master_url": master_url,
                "fallback": cls._build_local_fallback(restaurant)
            }
        except Exception as exc:
            logger.error(f"[MasterBillingService] Unexpected error: {exc}")
            return {
                "status": "error",
                "connection_status": "ERROR",
                "message": str(exc),
                "master_url": master_url,
                "fallback": cls._build_local_fallback(restaurant)
            }

    @classmethod
    def submit_upgrade_request(cls, restaurant, requested_plan_name: str, requested_billing_cycle: str = "MONTHLY", notes: str = "", contact_phone: str = "", contact_email: str = "") -> dict:
        """
        Pushes a plan change / renewal upgrade request to Master Admin enquiries desk.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"

        payload = {
            "license_key": restaurant.master_license_key if restaurant else "",
            "domain": restaurant.slug if restaurant else "",
            "business_name": restaurant.trade_name or restaurant.name if restaurant else "Restaurant Super Admin",
            "action": "UPGRADE_OR_RENEWAL_REQUEST",
            "requested_plan_name": requested_plan_name,
            "requested_billing_cycle": requested_billing_cycle,
            "notes": notes,
            "contact_phone": contact_phone or (restaurant.phone if restaurant else ""),
            "contact_email": contact_email or (restaurant.email if restaurant else ""),
        }

        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=6)
            if resp.status_code == 200:
                return resp.json()
            return {"status": "error", "message": f"Master Admin returned status {resp.status_code}: {resp.text}"}
        except Exception as exc:
            return {"status": "error", "message": f"Network error contacting Master Admin: {exc}"}

    @classmethod
    def _build_local_fallback(cls, restaurant) -> dict:
        """
        Builds a safe offline fallback data structure using local DB models.
        """
        branch_count = restaurant.branches.count() if restaurant else 0
        return {
            "client": {
                "business_name": (restaurant.trade_name or restaurant.name) if restaurant else "Restaurant Business",
                "legal_name": restaurant.legal_name if restaurant else "",
                "domain": restaurant.slug if restaurant else "",
                "license_key": restaurant.master_license_key if restaurant else "PENDING-SYNC",
                "subscription_status": "ACTIVE",
                "email": restaurant.email if restaurant else "",
                "phone": restaurant.phone if restaurant else "",
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
            },
            "current_plan": {
                "name": "Professional Tier",
                "code": "pro",
                "price": 2999,
                "billing_cycle": "MONTHLY",
                "description": "Comprehensive package for busy dine-ins & growing bar bistros.",
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
                    "billing_cycle": "MONTHLY",
                    "description": "Best for single-outlet cafes & quick-service restaurants.",
                    "max_branches": 1,
                    "features": ["1 Active POS Terminal Outlet", "Digital Menu & QR Ordering", "GST Tax Invoicing & KOT"],
                    "is_current": False,
                },
                {
                    "name": "Professional Tier",
                    "code": "pro",
                    "price": 2999,
                    "billing_cycle": "MONTHLY",
                    "description": "Comprehensive package for busy dine-ins & growing bar bistros.",
                    "max_branches": 3,
                    "features": ["Up to 3 Branch Outlets", "Kitchen Display System (KDS)", "Inventory & Recipe Stock Tracking"],
                    "is_current": True,
                },
                {
                    "name": "Enterprise Suite",
                    "code": "enterprise",
                    "price": 6999,
                    "billing_cycle": "MONTHLY",
                    "description": "Full-scale multi-chain operations with unlimited capabilities.",
                    "max_branches": 10,
                    "features": ["Up to 10 Branch Outlets", "Unlimited KOT & POS Terminals", "Dedicated 24/7 Account Manager"],
                    "is_current": False,
                },
            ],
            "invoices": [],
        }
