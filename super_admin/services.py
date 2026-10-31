import os
import time
import json
import hmac
import hashlib
import logging
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class MasterBillingService:
    """
    Client service for communicating with MasterRestro Master Admin Cloud.
    Retrieves live subscription contracts, plans, renewal countdown timers, and invoices.
    """
    SUBSCRIPTION_CACHE_KEY_PREFIX = "sattkar:subscription"

    @classmethod
    def get_cache_key(cls, restaurant=None) -> str:
        r_id = getattr(restaurant, 'pk', '0') if restaurant else '0'
        return f"{cls.SUBSCRIPTION_CACHE_KEY_PREFIX}:{r_id}"

    @classmethod
    def invalidate_cache(cls, restaurant=None):
        cache.delete(cls.get_cache_key(restaurant))

    @classmethod
    def is_connected(cls, restaurant=None) -> bool:
        """
        Returns True if this instance is actively connected and synchronized with Master Admin.
        """
        if restaurant is None:
            try:
                from super_admin.models import Restaurant
                restaurant = Restaurant.objects.first()
            except Exception:
                restaurant = None
        if not restaurant:
            return False

        if getattr(restaurant, 'master_sync_status', '') != 'SYNCED':
            return False

        if not getattr(restaurant, 'features_last_synced_at', None):
            return False

        try:
            cache_key = cls.get_cache_key(restaurant)
            cached = cache.get(cache_key)
            if cached and isinstance(cached, dict):
                return cached.get('connection_status') == 'CONNECTED'
        except Exception:
            pass

        return True

    @classmethod
    def _build_identity(cls, restaurant=None) -> dict:
        params = {}
        if restaurant:
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
        return params

    @staticmethod
    def get_config():
        # Prefer configured URL, fallback to localhost:3000
        master_url = getattr(settings, 'MASTER_DASHBOARD_URL', None) or os.environ.get('MASTER_DASHBOARD_URL', 'http://localhost:3000')
        master_secret = getattr(settings, 'MASTER_API_SECRET', None) or os.environ.get('MASTER_API_SECRET', '')
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
    def fetch_subscription_details(cls, restaurant=None, force: bool = False) -> dict:
        cache_key = cls.get_cache_key(restaurant)
        if not force:
            cached_val = cache.get(cache_key)
            if cached_val:
                return cached_val
        """
        Fetches live subscription details from Master Admin.
        Falls back to local restaurant state if Master Admin is offline or unreachable.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"

        params = cls._build_identity(restaurant)

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
                            if 'ENABLE_WEBSITE_CMS' in modules:
                                has_cms = bool(modules['ENABLE_WEBSITE_CMS'])
                                mod_updates['feature_website_cms'] = has_cms
                                restaurant.feature_website_cms = has_cms
                            if 'WEBSITE_MODE' in modules:
                                w_mode = str(modules['WEBSITE_MODE']).lower()
                                mod_updates['feature_website_type'] = w_mode
                                restaurant.feature_website_type = w_mode
                            if 'feature_website_cms' in client_payload:
                                mod_updates['feature_website_cms'] = bool(client_payload['feature_website_cms'])
                                restaurant.feature_website_cms = mod_updates['feature_website_cms']
                            if 'website_mode' in client_payload:
                                mod_updates['feature_website_type'] = str(client_payload['website_mode']).lower()
                                restaurant.feature_website_type = mod_updates['feature_website_type']
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

                    # Sync remote branch-level plan tags, tier types & add-ons from Master Admin
                    feature_flags = data.get('feature_flags') or {}
                    remote_branches = feature_flags.get('branches') or []
                    if not remote_branches:
                        client_payload = data.get('client', {})
                        fallback_b = client_payload.get('branches') or []
                        if fallback_b and isinstance(fallback_b, list):
                            remote_branches = [{'name': b.get('name'), 'key': b.get('slug') or str(b.get('id', '')), 'flags': {}} for b in fallback_b if b.get('name')]
                    if restaurant and remote_branches and isinstance(remote_branches, list):
                        try:
                            from super_admin.models import RestaurantBranch
                            db_branches = list(restaurant.branches.all().order_by('id'))
                            for idx, rem_b in enumerate(remote_branches):
                                rem_name = (rem_b.get('name') or '').strip()
                                rem_key = (rem_b.get('key') or '').strip()
                                flags = rem_b.get('flags') or {}
                                if not flags:
                                    continue

                                # Match branch by slug/key, name, or index fallback
                                target_branch = None
                                if rem_key:
                                    for b in db_branches:
                                        if b.slug and (b.slug.lower() == rem_key.lower() or rem_key.lower().startswith(b.slug.lower())):
                                            target_branch = b
                                            break
                                if not target_branch and rem_name:
                                    for b in db_branches:
                                        if b.name.strip().lower() == rem_name.lower():
                                            target_branch = b
                                            break
                                if not target_branch and idx < len(db_branches):
                                    target_branch = db_branches[idx]

                                if not target_branch:
                                    # Auto-provision missing branch as a remote draft outlet within permitted quota
                                    max_b = restaurant.get_max_allowed_branches() if hasattr(restaurant, 'get_max_allowed_branches') else (getattr(restaurant, 'max_branches', 0) or 5)
                                    if restaurant.branches.count() < max_b:
                                        try:
                                            target_branch = RestaurantBranch.objects.create(
                                                restaurant=restaurant,
                                                name=rem_name or f"Branch {idx + 1}",
                                                slug=rem_key or None,
                                                outlet_type='restaurant_standard',
                                                plan_entitlements=flags,
                                                use_company_address=True,
                                                address_line_1=restaurant.address_line_1,
                                                address_line_2=restaurant.address_line_2,
                                                city=restaurant.city,
                                                state=restaurant.state,
                                                pincode=restaurant.pincode,
                                                phone=restaurant.phone,
                                                is_setup_completed=False,
                                            )
                                            db_branches.append(target_branch)
                                            logger.info(f"[MasterBillingService] Auto-provisioned draft branch '{target_branch.name}' for '{restaurant.name}'")
                                        except Exception as prov_err:
                                            logger.warning(f"[MasterBillingService] Failed to auto-provision draft branch: {prov_err}")

                                if target_branch:
                                    branch_updates = {}
                                    # Always persist raw plan entitlements from Master Admin
                                    if target_branch.plan_entitlements != flags:
                                        branch_updates['plan_entitlements'] = flags
                                        target_branch.plan_entitlements = flags

                                    # 1. Resolve Dynamic Branch Outlet Type
                                    new_type = None
                                    if flags.get('has_only_pos'):
                                        new_type = 'only_pos'
                                    elif flags.get('has_starter'):
                                        new_type = 'starter_dhaba'
                                    elif flags.get('has_takeaway_qsr') or flags.get('has_qsr'):
                                        new_type = 'takeaway_qsr'
                                    elif flags.get('has_cloud_kitchen'):
                                        new_type = 'cloud_kitchen'
                                    elif flags.get('has_complete_restaurant_with_alcohol') or flags.get('has_fine_dine_bar') or flags.get('has_fine_dine'):
                                        new_type = 'fine_dine_bar'
                                    elif flags.get('has_hotel_resort'):
                                        new_type = 'hotel_resort'
                                    elif flags.get('has_club_lounge'):
                                        new_type = 'club_lounge'
                                    elif flags.get('has_complete_restaurant_without_alcohol'):
                                        new_type = 'restaurant_standard'
                                    elif flags.get('has_restaurant') or flags.get('is_restaurant'):
                                        new_type = 'restaurant_standard'

                                    if new_type and target_branch.outlet_type != new_type:
                                        branch_updates['outlet_type'] = new_type
                                        target_branch.outlet_type = new_type

                                    # 2. Resolve Serves Alcohol
                                    has_alcohol = bool(
                                        flags.get('has_complete_restaurant_with_alcohol') or
                                        flags.get('is_alcohol') or
                                        flags.get('has_alcohol') or
                                        flags.get('has_fine_dine_bar')
                                    )
                                    if not has_alcohol and (flags.get('has_only_pos') or flags.get('has_starter') or flags.get('has_complete_restaurant_without_alcohol')):
                                        has_alcohol = False

                                    if not getattr(restaurant, 'feature_alcohol_vat', True):
                                        has_alcohol = False

                                    if target_branch.serves_alcohol != has_alcohol:
                                        branch_updates['serves_alcohol'] = has_alcohol
                                        target_branch.serves_alcohol = has_alcohol

                                    # 3. Resolve Module Booleans
                                    has_rest = bool(
                                        flags.get('has_restaurant', True) or
                                        flags.get('is_restaurant', True) or
                                        flags.get('has_only_pos') or
                                        flags.get('has_starter') or
                                        flags.get('has_complete_restaurant_without_alcohol') or
                                        flags.get('has_complete_restaurant_with_alcohol') or
                                        flags.get('has_takeaway_qsr') or
                                        flags.get('has_cloud_kitchen') or
                                        flags.get('has_fine_dine_bar')
                                    ) and getattr(restaurant, 'feature_restaurant_pos', True)
                                    if target_branch.has_restaurant_module != has_rest:
                                        branch_updates['has_restaurant_module'] = has_rest
                                        target_branch.has_restaurant_module = has_rest

                                    has_htl = bool(
                                        flags.get('has_hotel') or
                                        flags.get('is_hotel') or
                                        flags.get('has_hotel_management') or
                                        flags.get('has_hotel_management_room_pms') or
                                        flags.get('has_hotel_resort')
                                    ) and getattr(restaurant, 'feature_hotel_pms', True)
                                    if target_branch.has_hotel_module != has_htl:
                                        branch_updates['has_hotel_module'] = has_htl
                                        target_branch.has_hotel_module = has_htl

                                    has_clb = bool(
                                        flags.get('has_club') or
                                        flags.get('is_bar_club') or
                                        flags.get('has_club_events') or
                                        flags.get('has_event_management') or
                                        flags.get('has_club_event_ticketing_passes') or
                                        flags.get('has_club_lounge')
                                    ) and getattr(restaurant, 'feature_club_events', True)
                                    if target_branch.has_club_module != has_clb:
                                        branch_updates['has_club_module'] = has_clb
                                        target_branch.has_club_module = has_clb

                                    # 4. Resolve Branch Add-ons list
                                    # Major standalone modules only; base tier features are bundled in the tier
                                    active_addons = set()
                                    if has_htl:
                                        active_addons.add('hotel')
                                        if flags.get('has_spas_wellness') or flags.get('has_spa') or flags.get('has_wellness'):
                                            active_addons.add('hotel_spa')
                                        if flags.get('has_cabs') or flags.get('has_cab') or flags.get('has_transport'):
                                            active_addons.add('hotel_cabs')
                                    if has_clb:
                                        active_addons.add('events')

                                    new_addons_list = sorted(list(active_addons))
                                    if target_branch.branch_addons != new_addons_list:
                                        branch_updates['branch_addons'] = new_addons_list
                                        target_branch.branch_addons = new_addons_list

                                    if branch_updates:
                                        RestaurantBranch.objects.filter(pk=target_branch.pk).update(**branch_updates)
                                        logger.info(f"[MasterBillingService] Synced branch '{target_branch.name}' from Master Admin: {branch_updates}")
                        except Exception as br_err:
                            logger.warning(f"[MasterBillingService] Error syncing branch feature flags: {br_err}")

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
                            raw_plan = sub_payload.get('plan_name')
                            plan_name = str(raw_plan) if raw_plan is not None else ''

                            is_trial = bool(sub_payload.get('is_trial', False))
                            trial_days_remaining = int(sub_payload.get('trial_days_remaining', 0) or 0)

                            sub_updates = {
                                'subscription_status': sub_status,
                                'subscription_is_overdue': is_overdue,
                                'subscription_grace_period_expired': grace_expired,
                                'subscription_grace_days_remaining': grace_days_left,
                                'subscription_overdue_days': overdue_days,
                                'subscription_is_locked': is_locked,
                                'subscription_plan_name': plan_name,
                                'subscription_is_trial': is_trial,
                                'subscription_trial_days_remaining': trial_days_remaining,
                                'subscription_trial_ends_at': None,
                                'subscription_next_billing_date': None,
                            }
                            trial_ends_str = sub_payload.get('trial_ends_at')
                            if trial_ends_str:
                                try:
                                    from django.utils.dateparse import parse_datetime
                                    p_trial = parse_datetime(trial_ends_str)
                                    if p_trial:
                                        sub_updates['subscription_trial_ends_at'] = p_trial
                                except Exception:
                                    pass
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

                    cache.set(cache_key, data, 60)
                    return data
            logger.warning(f"[MasterBillingService] Server returned status {resp.status_code}: {resp.text[:200]}")
            conn_status = "NOT_CONNECTED" if resp.status_code == 404 else "SERVER_ERROR"
            if restaurant:
                try:
                    from super_admin.models import Restaurant
                    Restaurant.objects.filter(pk=restaurant.pk).update(master_sync_status=conn_status)
                    restaurant.master_sync_status = conn_status
                except Exception:
                    pass
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = conn_status
            fallback["master_url"] = master_url
            fallback["message"] = f"Master Admin returned status {resp.status_code}"
            cache.set(cache_key, fallback, 30)
            return fallback
        except requests.exceptions.RequestException as exc:
            logger.warning(f"[MasterBillingService] Master Admin offline / unreachable: {exc}")
            if restaurant:
                try:
                    from super_admin.models import Restaurant
                    Restaurant.objects.filter(pk=restaurant.pk).update(master_sync_status="OFFLINE")
                    restaurant.master_sync_status = "OFFLINE"
                except Exception:
                    pass
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = "OFFLINE"
            fallback["master_url"] = master_url
            fallback["message"] = f"Could not connect to Master Admin at {master_url}."
            cache.set(cache_key, fallback, 30)
            return fallback
        except Exception as exc:
            logger.error(f"[MasterBillingService] Unexpected error: {exc}")
            if restaurant:
                try:
                    from super_admin.models import Restaurant
                    Restaurant.objects.filter(pk=restaurant.pk).update(master_sync_status="ERROR")
                    restaurant.master_sync_status = "ERROR"
                except Exception:
                    pass
            fallback = cls._build_local_fallback(restaurant)
            fallback["connection_status"] = "ERROR"
            fallback["master_url"] = master_url
            fallback["message"] = str(exc)
            cache.set(cache_key, fallback, 30)
            return fallback

    @classmethod
    def submit_upgrade_request(cls, restaurant, requested_plan_name: str, requested_billing_cycle: str = "MONTHLY", notes: str = "", contact_phone: str = "", contact_email: str = "", stack_configuration: dict = None, action: str = "UPGRADE_OR_RENEWAL_REQUEST", trial_days: int = 14) -> dict:
        """
        Pushes a plan change / renewal upgrade request to Master Admin enquiries desk.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"

        payload = {
            "license_key": getattr(restaurant, 'master_license_key', '') if restaurant else "",
            "domain": getattr(restaurant, 'slug', '') if restaurant else "",
            "business_name": (getattr(restaurant, 'trade_name', '') or getattr(restaurant, 'name', '')) if restaurant else "Restaurant Super Admin",
            "action": action,
            "trial_days": trial_days,
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
                cls.invalidate_cache(restaurant)
                try:
                    cls.fetch_subscription_details(restaurant, force=True)
                except Exception:
                    pass
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
            "SHOW_WEBSITE_PAGES": constants._DEFAULT_SHOW_WEBSITE_PAGES,
            "ENABLE_ALCOHOL_VAT": constants._DEFAULT_ENABLE_ALCOHOL_VAT,
            "ENABLE_CLUB_EVENTS_MODULE": constants._DEFAULT_ENABLE_CLUB_EVENTS_MODULE,
            "ENABLE_HOTEL_MODULE": constants._DEFAULT_ENABLE_HOTEL_MODULE,
            "ENABLE_RESTAURANT_MODULE": constants._DEFAULT_ENABLE_RESTAURANT_MODULE,
            "ENABLE_HOTEL_WEBSITE": constants._DEFAULT_ENABLE_HOTEL_WEBSITE,
        }

        return {
            "status": "offline",
            "connection_status": "NOT_CONNECTED",
            "message": "Sattkar is currently not connected or unreachable. Local configuration active.",
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
                "max_branches": constants.MAX_ALLOWED_BRANCHES,
                "max_businesses": constants.MAX_ALLOWED_BUSINESSES,
            },
            "subscription": {
                "status": getattr(restaurant, 'subscription_status', 'ACTIVE') if restaurant else 'ACTIVE',
                "plan_name": getattr(restaurant, 'subscription_plan_name', '') or "Standard Plan (Local .env)",
                "billing_cycle": "MONTHLY",
                "currency": "₹",
                "monthly_fee": 0,
                "days_remaining": 30,
                "hours_remaining": 0,
                "minutes_remaining": 0,
                "seconds_remaining": 0,
                "total_seconds_remaining": 0,
                "in_grace_period": False,
                "grace_days_remaining": 0,
                "grace_period_expired": False,
                "is_locked": False,
                "is_trial": False,
                "trial_days": 0,
                "trial_days_remaining": 0,
                "trial_ends_at": None,
                "payment_due": False,
            },
            "pending_payment": None,
            "pending_addons": [],
            "current_plan": {
                "name": getattr(restaurant, 'subscription_plan_name', '') or "Standard Plan (Local)",
                "description": "Standard Plan (Local Fallback)",
                "max_branches": constants.MAX_ALLOWED_BRANCHES,
                "monthly_fee": 0,
                "price": 0,
                "features": ["Cloud POS", "Multi-Branch Operations", "Inventory Control", "Live Billing & Reports"],
            },
            "available_plans": [],
            "invoices": [],
        }

    @classmethod
    def end_trial(cls, restaurant=None) -> dict:
        """
        Ends the restaurant's free trial immediately, transitioning the account to regular billing
        and generating the first subscription invoice with a Razorpay link.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/subscription"
        payload = {
            **cls._build_identity(restaurant),
            "action": "END_TRIAL",
        }
        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)
        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=10)
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "success":
                cls.invalidate_cache(restaurant)
                cls.fetch_subscription_details(restaurant, force=True)
            return data
        except Exception as exc:
            logger.error(f"[MasterBillingService] end_trial failed: {exc}")
            return {"status": "error", "message": f"Could not end trial: {exc}"}

    @classmethod
    def check_invoice_status(cls, restaurant=None, invoice_no=None, invoice_id=None, payment_link_id=None) -> dict:
        """
        Confirms payment status with Master Admin & Razorpay immediately ('I've paid' action).
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/invoice-status"

        body = cls._build_identity(restaurant)
        if invoice_no:
            body["invoice_no"] = str(invoice_no).strip()
        if invoice_id:
            body["invoice_id"] = str(invoice_id).strip()
        if payment_link_id:
            body["payment_link_id"] = str(payment_link_id).strip()

        body_json = json.dumps(body, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=10)
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "success":
                inv = data.get("invoice", {})
                if inv.get("paid"):
                    cls.invalidate_cache(restaurant)
                    # Refresh fresh subscription state directly
                    cls.fetch_subscription_details(restaurant, force=True)
            return data
        except Exception as exc:
            logger.error(f"[MasterBillingService] check_invoice_status failed: {exc}")
            return {"status": "error", "message": f"Could not verify invoice: {exc}"}

    @classmethod
    def fetch_addons(cls, restaurant=None) -> dict:
        """
        Fetches dynamic catalog of available add-on modules, new outlet options,
        existing outlets, and recent add-on orders from Master Admin.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/addons"
        params = cls._build_identity(restaurant)
        headers = cls.get_headers()

        try:
            resp = requests.get(endpoint, params=params, headers=headers, timeout=6)
            return resp.json()
        except Exception as exc:
            logger.error(f"[MasterBillingService] fetch_addons failed: {exc}")
            return {"status": "error", "message": f"Could not load add-on catalog: {exc}"}

    @classmethod
    def quote_addons(cls, restaurant=None, items=None, new_outlets=None) -> dict:
        """
        Requests instant prorated price quotation for selected add-ons from Master Admin.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/addons"

        payload = {
            **cls._build_identity(restaurant),
            "items": items or [],
            "new_outlets": new_outlets or [],
            "preview": True,
        }
        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=6)
            return resp.json()
        except Exception as exc:
            logger.error(f"[MasterBillingService] quote_addons failed: {exc}")
            return {"status": "error", "message": f"Could not calculate quote: {exc}"}

    @classmethod
    def order_addons(cls, restaurant=None, items=None, new_outlets=None, notes="", requested_by_name="") -> dict:
        """
        Places a self-serve add-on order with Master Admin, returning Razorpay payment_url
        or activating immediately if in trial.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/addons"

        payload = {
            **cls._build_identity(restaurant),
            "items": items or [],
            "new_outlets": new_outlets or [],
            "notes": notes,
            "requested_by_name": requested_by_name,
            "preview": False,
        }
        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=8)
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "success":
                cls.invalidate_cache(restaurant)
                cls.fetch_subscription_details(restaurant, force=True)
            return data
        except Exception as exc:
            logger.error(f"[MasterBillingService] order_addons failed: {exc}")
            return {"status": "error", "message": f"Could not place add-on order: {exc}"}

    @classmethod
    def cancel_addon_order(cls, restaurant=None, order_id=None) -> dict:
        """
        Cancels an unpaid add-on order on Master Admin.
        """
        master_url, _ = cls.get_config()
        endpoint = f"{master_url}/api/billing/addons"

        payload = {
            **cls._build_identity(restaurant),
            "action": "cancel",
            "order_id": order_id,
        }
        body_json = json.dumps(payload, separators=(',', ':'))
        headers = cls.get_headers(body_json)

        try:
            resp = requests.post(endpoint, data=body_json, headers=headers, timeout=6)
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "success":
                cls.invalidate_cache(restaurant)
                cls.fetch_subscription_details(restaurant, force=True)
            return data
        except Exception as exc:
            logger.error(f"[MasterBillingService] cancel_addon_order failed: {exc}")
            return {"status": "error", "message": f"Could not cancel add-on order: {exc}"}
