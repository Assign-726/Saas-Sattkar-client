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
        # Prefer configured URL, fallback to localhost:3000
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
            
            # If production URL returned no modules or failed, and DEBUG is active, try local Master Admin
            if (resp.status_code != 200 or not (resp.json().get('modules'))) and getattr(settings, 'DEBUG', False) and master_url != 'http://localhost:3000':
                try:
                    local_endpoint = "http://localhost:3000/api/billing/subscription"
                    local_resp = requests.get(local_endpoint, params=params, headers=headers, timeout=2)
                    if local_resp.status_code == 200:
                        local_data = local_resp.json()
                        if local_data.get('status') == 'success' and local_data.get('modules'):
                            resp = local_resp
                            master_url = "http://localhost:3000"
                except Exception:
                    pass

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

                    # Sync remote module entitlements from Master Admin
                    modules = data.get('modules') or {}
                    if restaurant and modules and isinstance(modules, dict):
                        try:
                            from django.utils import timezone
                            from super_admin.models import Restaurant
                            mod_updates = {}
                            if 'SHOW_WEBSITE_PAGES' in modules:
                                has_website = bool(modules['SHOW_WEBSITE_PAGES'])
                                mod_updates['feature_website_pages'] = has_website
                                restaurant.feature_website_pages = has_website
                                mod_updates['is_active'] = has_website
                                restaurant.is_active = has_website
                            if 'ENABLE_ALCOHOL_VAT' in modules:
                                mod_updates['feature_alcohol_vat'] = bool(modules['ENABLE_ALCOHOL_VAT'])
                                restaurant.feature_alcohol_vat = mod_updates['feature_alcohol_vat']
                            if 'ENABLE_CLUB_EVENTS_MODULE' in modules:
                                mod_updates['feature_club_events'] = bool(modules['ENABLE_CLUB_EVENTS_MODULE'])
                                restaurant.feature_club_events = mod_updates['feature_club_events']
                            if 'ENABLE_HOTEL_MODULE' in modules:
                                mod_updates['feature_hotel_pms'] = bool(modules['ENABLE_HOTEL_MODULE'])
                                restaurant.feature_hotel_pms = mod_updates['feature_hotel_pms']
                            if 'ENABLE_RESTAURANT_MODULE' in modules:
                                mod_updates['feature_restaurant_pos'] = bool(modules['ENABLE_RESTAURANT_MODULE'])
                                restaurant.feature_restaurant_pos = mod_updates['feature_restaurant_pos']
                            if 'ENABLE_HOTEL_WEBSITE' in modules:
                                mod_updates['feature_hotel_website'] = bool(modules['ENABLE_HOTEL_WEBSITE'])
                                restaurant.feature_hotel_website = mod_updates['feature_hotel_website']
                            if mod_updates:
                                mod_updates['features_last_synced_at'] = timezone.now()
                                restaurant.features_last_synced_at = mod_updates['features_last_synced_at']
                                Restaurant.objects.filter(pk=restaurant.pk).update(**mod_updates)
                                if 'ENABLE_RESTAURANT_MODULE' in modules:
                                    if not restaurant.feature_restaurant_pos:
                                        # When Master Admin revokes Restaurant POS, disable on all branches
                                        restaurant.branches.all().update(has_restaurant_module=False)
                                if 'ENABLE_HOTEL_WEBSITE' in modules:
                                    if not restaurant.feature_hotel_website:
                                        # When Master Admin revokes Hotel Website, disable on all branches and landing
                                        restaurant.branches.all().update(has_hotel_website=False)
                                        try:
                                            from core.models import RestaurantLanding
                                            RestaurantLanding.objects.filter(restaurant=restaurant).update(has_hotel_website=False)
                                        except Exception:
                                            pass
                                logger.info(f"[MasterBillingService] Synced modules for '{restaurant.name}': {mod_updates}")
                        except Exception as mod_err:
                            logger.warning(f"[MasterBillingService] Could not persist modules to DB: {mod_err}")

                    # Sync remote subscription & overdue lockout state from Master Admin
                    sub_payload = data.get('subscription') or {}
                    if restaurant and sub_payload and isinstance(sub_payload, dict):
                        try:
                            from super_admin.models import Restaurant
                            sub_status = sub_payload.get('status', 'ACTIVE')
                            is_overdue = bool(sub_payload.get('is_overdue', False))
                            grace_expired = bool(sub_payload.get('grace_period_expired', False))
                            grace_days_left = int(sub_payload.get('grace_days_remaining', 0) or 0)
                            overdue_days = int(sub_payload.get('overdue_days', 0) or 0)
                            is_suspended = (sub_status == 'SUSPENDED')
                            is_locked = bool(sub_payload.get('is_locked', False) or is_suspended or grace_expired)
                            plan_name = sub_payload.get('plan_name', '')

                            sub_updates = {
                                'subscription_status': sub_status,
                                'subscription_is_overdue': is_overdue,
                                'subscription_grace_period_expired': grace_expired,
                                'subscription_grace_days_remaining': grace_days_left,
                                'subscription_overdue_days': overdue_days,
                                'subscription_is_locked': is_locked,
                                'subscription_plan_name': plan_name,
                            }
                            next_bill_str = sub_payload.get('next_billing_date')
                            if next_bill_str:
                                try:
                                    from django.utils.dateparse import parse_datetime
                                    parsed_dt = parse_datetime(next_bill_str)
                                    if parsed_dt:
                                        sub_updates['subscription_next_billing_date'] = parsed_dt
                                except Exception:
                                    pass

                            Restaurant.objects.filter(pk=restaurant.pk).update(**sub_updates)
                            for k, v in sub_updates.items():
                                setattr(restaurant, k, v)
                            logger.info(f"[MasterBillingService] Synced subscription lock status: is_locked={is_locked}, status={sub_status}, overdue_days={overdue_days}, grace_expired={grace_expired}")
                        except Exception as sub_err:
                            logger.warning(f"[MasterBillingService] Could not persist subscription state: {sub_err}")

                    # Sync remote quota limits (max branches & max businesses)
                    # Priority 1: Custom client limit in client payload
                    # Priority 2: Current plan limit in current_plan payload
                    client_payload = data.get('client', {}) or {}
                    current_plan_payload = data.get('current_plan', {}) or {}

                    remote_max_branches = None
                    if 'max_branches' in client_payload and client_payload['max_branches'] is not None:
                        try:
                            remote_max_branches = int(client_payload['max_branches'])
                        except (ValueError, TypeError):
                            pass
                    if remote_max_branches is None and 'max_branches' in current_plan_payload and current_plan_payload['max_branches'] is not None:
                        try:
                            remote_max_branches = int(current_plan_payload['max_branches'])
                        except (ValueError, TypeError):
                            pass
                    if remote_max_branches is None and 'max_branches' in data and data['max_branches'] is not None:
                        try:
                            remote_max_branches = int(data['max_branches'])
                        except (ValueError, TypeError):
                            pass

                    remote_max_businesses = None
                    if 'max_businesses' in client_payload and client_payload['max_businesses'] is not None:
                        try:
                            remote_max_businesses = int(client_payload['max_businesses'])
                        except (ValueError, TypeError):
                            pass
                    if remote_max_businesses is None and 'max_businesses' in current_plan_payload and current_plan_payload['max_businesses'] is not None:
                        try:
                            remote_max_businesses = int(current_plan_payload['max_businesses'])
                        except (ValueError, TypeError):
                            pass
                    if remote_max_businesses is None and 'max_businesses' in data and data['max_businesses'] is not None:
                        try:
                            remote_max_businesses = int(data['max_businesses'])
                        except (ValueError, TypeError):
                            pass

                    if restaurant:
                        quota_updates = {}
                        if remote_max_branches is not None and getattr(restaurant, 'max_branches', None) != remote_max_branches:
                            quota_updates['max_branches'] = remote_max_branches
                            restaurant.max_branches = remote_max_branches
                        if remote_max_businesses is not None and getattr(restaurant, 'max_businesses', None) != remote_max_businesses:
                            quota_updates['max_businesses'] = remote_max_businesses
                            restaurant.max_businesses = remote_max_businesses

                        if quota_updates:
                            try:
                                from super_admin.models import Restaurant
                                Restaurant.objects.filter(pk=restaurant.pk).update(**quota_updates)
                                logger.info(f"[MasterBillingService] Synced quota limits for '{restaurant.name}': {quota_updates}")
                            except Exception as q_err:
                                logger.warning(f"[MasterBillingService] Could not persist quota updates to DB: {q_err}")

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
    def submit_upgrade_request(cls, restaurant, requested_plan_name: str, requested_billing_cycle: str = "MONTHLY", notes: str = "", contact_phone: str = "", contact_email: str = "", stack_configuration: dict = None) -> dict:
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
        if stack_configuration:
            payload["stack_configuration"] = stack_configuration

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

        from restaurant_project import constants
        modules = {
            "SHOW_WEBSITE_PAGES": getattr(restaurant, 'feature_website_pages', constants._DEFAULT_SHOW_WEBSITE_PAGES) if restaurant else constants._DEFAULT_SHOW_WEBSITE_PAGES,
            "ENABLE_ALCOHOL_VAT": getattr(restaurant, 'feature_alcohol_vat', constants._DEFAULT_ENABLE_ALCOHOL_VAT) if restaurant else constants._DEFAULT_ENABLE_ALCOHOL_VAT,
            "ENABLE_CLUB_EVENTS_MODULE": getattr(restaurant, 'feature_club_events', constants._DEFAULT_ENABLE_CLUB_EVENTS_MODULE) if restaurant else constants._DEFAULT_ENABLE_CLUB_EVENTS_MODULE,
            "ENABLE_HOTEL_MODULE": getattr(restaurant, 'feature_hotel_pms', constants._DEFAULT_ENABLE_HOTEL_MODULE) if restaurant else constants._DEFAULT_ENABLE_HOTEL_MODULE,
            "ENABLE_RESTAURANT_MODULE": getattr(restaurant, 'feature_restaurant_pos', constants._DEFAULT_ENABLE_RESTAURANT_MODULE) if restaurant else constants._DEFAULT_ENABLE_RESTAURANT_MODULE,
            "ENABLE_HOTEL_WEBSITE": getattr(restaurant, 'feature_hotel_website', constants._DEFAULT_ENABLE_HOTEL_WEBSITE) if restaurant else constants._DEFAULT_ENABLE_HOTEL_WEBSITE,
        }

        return {
            "status": "offline",
            "connection_status": "OFFLINE",
            "message": "Master Admin is currently not connected or unreachable.",
            "modules": modules,
            "client": {
                "business_name": business_name or "Registered Business",
                "legal_name": legal_name,
                "domain": domain,
                "license_key": license_key or "Not Assigned",
                "subscription_status": "NOT_CONNECTED",
                "email": email,
                "phone": phone,
                "branch_count": branch_count,
                "max_branches": restaurant.get_max_allowed_branches() if restaurant else constants.MAX_ALLOWED_BRANCHES,
                "max_businesses": restaurant.get_max_allowed_businesses() if restaurant else constants.MAX_ALLOWED_BUSINESSES,
            },
            "subscription": {
                "status": getattr(restaurant, 'subscription_status', 'ACTIVE') if restaurant else 'ACTIVE',
                "plan_name": getattr(restaurant, 'subscription_plan_name', '') or "Standard Plan",
                "billing_cycle": "MONTHLY",
                "currency": "₹",
                "monthly_fee": 0,
                "days_remaining": getattr(restaurant, 'subscription_grace_days_remaining', 30) if restaurant else 30,
                "hours_remaining": 0,
                "minutes_remaining": 0,
                "seconds_remaining": 0,
                "total_seconds_remaining": 0,
                "in_grace_period": getattr(restaurant, 'subscription_is_overdue', False) if restaurant else False,
                "grace_days_remaining": getattr(restaurant, 'subscription_grace_days_remaining', 0) if restaurant else 0,
                "grace_period_expired": getattr(restaurant, 'subscription_grace_period_expired', False) if restaurant else False,
                "is_locked": getattr(restaurant, 'subscription_is_locked', False) if restaurant else False,
            },
            "current_plan": {
                "name": getattr(restaurant, 'subscription_plan_name', '') or "Standard Plan",
                "description": "Standard Plan (Local Fallback)",
                "max_branches": restaurant.get_max_allowed_branches() if restaurant else constants.MAX_ALLOWED_BRANCHES,
                "monthly_fee": 0,
                "price": 0,
                "features": ["Cloud POS", "Multi-Branch Operations", "Inventory Control", "Live Billing & Reports"],
            },
            "available_plans": [],
            "invoices": [],
        }
