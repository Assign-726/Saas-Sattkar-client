"""Custom roles: area-by-area access (No access / View / Manage) for admin logins.

How it fits with the built-in roles
-----------------------------------
A custom role *narrows* an admin login. The login keeps an administrator base role, chosen
automatically from the areas the role opens (restaurant areas → Restaurant Admin, hotel areas
→ Hotel Admin, both → General Manager), so every existing screen keeps working; then
``RolePermissionMiddleware`` blocks the areas the role does not open:

* **No access** — the screen is hidden from the sidebar and opening it redirects to the
  first screen the role can use (or answers 403 for API calls).
* **View**     — read-only: GET requests to non-mutating screens only.
* **Manage**   — full use.

Every URL in the Restaurant and Hotel admin is mapped to an area below. A test fails if a
new URL is added without a mapping, and unmapped URLs are refused (fail closed).
"""
import re

from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

NONE, VIEW, MANAGE = 'none', 'view', 'manage'
LEVELS = [(NONE, 'No access'), (VIEW, 'View'), (MANAGE, 'Manage')]
RANK = {NONE: 0, VIEW: 1, MANAGE: 2}

R, H, S = 'restaurant', 'hotel', 'shared'

# key, label, module, icon, description
AREAS = [
    ('dashboard', 'Dashboard & Morning Brief', R, 'fa-gauge', 'Restaurant dashboard and the daily brief'),
    ('analytics', 'Analytics & sales', R, 'fa-chart-line', 'Sales analytics, menu engineering and reports'),
    ('pos', 'Point of sale & billing', R, 'fa-cash-register', 'POS, orders, receipts, credit notes, shifts and counters'),
    ('kitchen', 'Kitchen display', R, 'fa-fire-burner', 'KDS tickets and marking items ready'),
    ('dine_in', 'Tables & reservations', R, 'fa-chair', 'Floor plan, tables, zones, reservations and the walk-in waitlist'),
    ('menu', 'Menu & marketing', R, 'fa-utensils', 'Menu items, categories, recipes, coupons and QR codes'),
    ('events', 'Club & events', R, 'fa-champagne-glasses', 'Events, guest lists, tickets and the gate scanner'),
    ('inventory', 'Stock & purchases', R, 'fa-boxes-stacked', 'Inventory, wastage, stock counts, purchases, challans and parties'),
    ('delivery', 'Zomato & Swiggy', R, 'fa-motorcycle', 'Online orders, accept/reject, menu sync and payouts per platform'),
    ('front_desk', 'Front desk & bookings', H, 'fa-concierge-bell', 'Hotel dashboard, tape chart, bookings, check-in and check-out'),
    ('rooms', 'Rooms & housekeeping', H, 'fa-bed', 'Rooms, room types and housekeeping status'),
    ('cabs', 'Cabs & fleet', H, 'fa-car', 'Cab bookings, dispatch, fleet and packages'),
    ('revenue', 'Rates, reports & night audit', H, 'fa-chart-line', 'Seasonal rates, occupancy/ADR/RevPAR reports, the forecast and closing the day'),
    ('guest_services', 'Guest services', H, 'fa-bell-concierge', 'Guest requests & complaints, laundry, spa and lost & found'),
    ('banquets', 'Banquets & events', H, 'fa-champagne-glasses', 'Venues, menus, event bookings, BEOs and event payments'),
    ('sales', 'Sales, packages & promotions', H, 'fa-handshake', 'Sales pipeline, follow-ups, stay packages and promo codes'),
    ('back_office', 'Security & back office', H, 'fa-shield-halved', 'Visitors, incidents, licences, filings, safety checks, assets, contracts and energy'),
    ('guests', 'Guests, loyalty & gift cards', S, 'fa-address-book', 'Guest profiles, points and gift cards'),
    ('feedback', 'Guest feedback', S, 'fa-comment-dots', 'Ratings, complaints and follow-up'),
    ('campaigns', 'Campaigns', S, 'fa-bullhorn', 'WhatsApp, SMS and email campaigns'),
    ('finance', 'Expenses and P&L', S, 'fa-scale-balanced', 'Expenses, receipts, the profit & loss report and the hotel city ledger'),
    ('staff', 'Staff & payroll', S, 'fa-users-gear', 'Logins, ground staff, attendance, payroll and SOPs'),
    ('settings', 'Settings', S, 'fa-sliders', 'Business, branch, billing and tax settings'),
    ('integrations', 'Integrations', S, 'fa-plug', 'Channels, API keys and webhooks'),
    ('logs', 'Activity logs', S, 'fa-clock-rotate-left', 'Who did what, and when'),
]
AREA_KEYS = [a[0] for a in AREAS]
AREA_MODULE = {a[0]: a[2] for a in AREAS}
AREA_LABEL = {a[0]: a[1] for a in AREAS}

# URL names that every signed-in admin may use, whatever their role
ALWAYS = {
    ('restaurant_admin', 'switch_branch'), ('restaurant_admin', 'detect_location_ip_api'),
    ('restaurant_admin', 'order_notifications_poll'), ('restaurant_admin', 'notifications_list'), ('restaurant_admin', 'notifications_update'),
    ('hotel_admin', 'notifications_poll'), ('hotel_admin', 'notifications_list'), ('hotel_admin', 'notifications_update'),
    ('hotel_admin', 'action_centre'),  # read-only; shows only the areas the role can open
    ('restaurant_admin', 'two_factor_update'), ('hotel_admin', 'two_factor_update'),  # your own sign-in security
}

_RESTAURANT_RULES = [
    ('dashboard', r'^(dashboard|morning_brief)$'),
    ('analytics', r'^(sales_analytics|report_.*)$'),
    ('kitchen', r'^(kitchen_kds.*|order_mark_ready)$'),
    ('pos', r'^(order_.*|orders_stream|pos_.*|receipt_.*|credit_note_.*|transaction_desk_create|custom_charge_.*|guest_pos_lookup)$'),
    ('dine_in', r'^(floor_plan_.*|table_.*|zone_.*|regenerate_table_qrs|reservation_.*)$'),
    ('menu', r'^(menu_.*|category_.*|subcategory_.*|get_subcategories|coupon_.*|qr_code_generator|generate_qr_code|generate_menu_item_qr_code)$'),
    ('events', r'^club_.*$'),
    ('inventory', r'^(inventory_.*|purchase_.*|challan_.*|party_.*)$'),
    ('delivery', r'^delivery_.*$'),
    ('guests', r'^(guest_list|guest_detail|gift_card_.*)$'),
    ('feedback', r'^feedback_.*$'),
    ('campaigns', r'^campaign_.*$'),
    ('finance', r'^(expenses|expense_receipt|profit_loss)$'),
    ('staff', r'^(staff_.*|ground_staff_.*|muster_roll|payroll_dashboard|salary_slip_view|sop_dashboard|api_.*|roles|role_.*)$'),
    ('settings', r'^(settings|test_email|test|test_static|debug_menu)$'),
    ('integrations', r'^integration.*$'),
    ('logs', r'^activity_logs$'),
]
_HOTEL_RULES = [
    ('front_desk', r'^(dashboard|tape_chart|bookings|booking_.*|morning_brief)$'),
    ('rooms', r'^(rooms|room_.*|categories|category_.*)$'),
    ('cabs', r'^(cab_.*|cabs_.*)$'),
    ('revenue', r'^(rates|rate_.*|reports|reports_.*|night_audit|night_audit_.*)$'),
    ('guest_services', r'^(requests|request_.*|lostfound|lostfound_.*|laundry|laundry_.*|spa|spa_.*)$'),
    ('banquets', r'^(banquets|banquet_.*)$'),
    ('sales', r'^(sales|sales_.*|packages|package_.*|promo_.*)$'),
    ('back_office', r'^(security|security_.*|compliance|compliance_.*|assets|asset_.*|contract_.*|energy|energy_.*)$'),
    ('guests', r'^(guest_list|guest_detail|gift_card_.*)$'),
    ('feedback', r'^feedback_.*$'),
    ('campaigns', r'^campaign_.*$'),
    ('finance', r'^(expenses|expense_receipt|profit_loss|ledger|ledger_.*|cashier|cashier_.*)$'),
    ('staff', r'^(staff_.*|ground_staff_.*|muster_roll|payroll_dashboard|salary_slip_view|sop_dashboard|api_.*|roles|role_.*)$'),
    ('settings', r'^(settings|test_email)$'),
    ('integrations', r'^integration.*$'),
    ('logs', r'^activity_logs$'),
]
RULES = {'restaurant_admin': [(a, re.compile(p)) for a, p in _RESTAURANT_RULES],
         'hotel_admin': [(a, re.compile(p)) for a, p in _HOTEL_RULES]}

# A URL whose name contains one of these words changes data, even when opened with GET
_MUTATING = re.compile(r'(^|_)(add|create|new|edit|update|delete|toggle|save|cancel|confirm|mark|settle|generate|regenerate|finalize|'
                       r'adjust|check_in|check_out|checkin|assign|reschedule|status|bulk|quick|seed|close|open|action|sync|draft|'
                       r'position|publish|received|payment|driver|set|cover|image|video|recipe_delete|manual)(_|$)')

HOME = {
    'dashboard': 'restaurant_admin:dashboard', 'analytics': 'restaurant_admin:sales_analytics', 'pos': 'restaurant_admin:order_create_manual',
    'kitchen': 'restaurant_admin:kitchen_kds', 'dine_in': 'restaurant_admin:table_list', 'menu': 'restaurant_admin:menu_list',
    'events': 'restaurant_admin:club_events_list', 'inventory': 'restaurant_admin:inventory_list', 'delivery': 'restaurant_admin:delivery_home',
    'front_desk': 'hotel_admin:dashboard', 'rooms': 'hotel_admin:rooms', 'cabs': 'hotel_admin:cabs_dispatch',
    'revenue': 'hotel_admin:reports', 'guest_services': 'hotel_admin:requests', 'banquets': 'hotel_admin:banquets',
    'sales': 'hotel_admin:sales', 'back_office': 'hotel_admin:security',
    'guests': '{ns}:guest_list', 'feedback': '{ns}:feedback_list', 'campaigns': '{ns}:campaign_list', 'finance': '{ns}:expenses',
    'staff': '{ns}:staff_list', 'settings': '{ns}:settings', 'integrations': '{ns}:integrations', 'logs': '{ns}:activity_logs',
}

PRESETS = [
    ('Accountant', 'Books, costs and reports — read-only on operations.',
     {'finance': MANAGE, 'analytics': VIEW, 'inventory': VIEW, 'pos': VIEW, 'logs': VIEW, 'staff': VIEW}),
    ('Store manager', 'Runs stock and purchasing for the kitchen.',
     {'inventory': MANAGE, 'menu': VIEW, 'kitchen': VIEW, 'dashboard': VIEW}),
    ('Marketing manager', 'Guests, loyalty, campaigns and feedback.',
     {'guests': MANAGE, 'campaigns': MANAGE, 'feedback': MANAGE, 'menu': VIEW, 'analytics': VIEW, 'dashboard': VIEW}),
    ('Shift supervisor', 'Runs the floor: billing, kitchen, tables and complaints.',
     {'pos': MANAGE, 'kitchen': MANAGE, 'dine_in': MANAGE, 'guests': VIEW, 'feedback': MANAGE, 'dashboard': VIEW, 'menu': VIEW}),
    ('Front office lead', 'Hotel front desk, rooms, cabs and guest issues.',
     {'front_desk': MANAGE, 'rooms': MANAGE, 'cabs': MANAGE, 'guests': MANAGE, 'feedback': MANAGE, 'revenue': VIEW,
      'guest_services': MANAGE}),
    ('Banquet & sales manager', 'Events, the sales pipeline, packages and promo codes.',
     {'banquets': MANAGE, 'sales': MANAGE, 'guests': VIEW, 'campaigns': MANAGE, 'front_desk': VIEW}),
    ('Security & facilities', 'Visitors, incidents, compliance, assets, energy and maintenance.',
     {'back_office': MANAGE, 'rooms': VIEW, 'front_desk': VIEW}),
    ('Auditor', 'Can look at everything, change nothing.',
     {k: VIEW for k in AREA_KEYS}),
]

# Old per-login switches kept in step with the role, so screens that read them agree
LEGACY_FLAGS = {
    'can_manage_menu': ('menu', MANAGE), 'can_manage_settings': ('settings', MANAGE),
    'can_view_analytics': ('analytics', VIEW), 'can_view_inventory': ('inventory', VIEW),
    'can_apply_discounts': ('pos', MANAGE), 'can_issue_refunds': ('pos', MANAGE),
}


def area_for(namespace, url_name):
    for area, pattern in RULES.get(namespace, []):
        if pattern.match(url_name or ''):
            return area
    return None


def is_mutating(url_name):
    return bool(_MUTATING.search(url_name or ''))


def clean_permissions(data):
    return {k: (data.get(k) if data.get(k) in RANK else NONE) for k in AREA_KEYS}


def modules_used(permissions):
    used = {AREA_MODULE[k] for k, v in permissions.items() if RANK.get(v, 0) > 0 and k in AREA_MODULE}
    return used


def base_role_for(permissions):
    used = modules_used(permissions)
    if R in used and H in used:
        return 'GENERAL_MANAGER'
    if H in used:
        return 'HOTEL_ADMIN'
    return 'RESTAURANT_ADMIN'


def custom_role_of(user):
    if not user or not user.is_authenticated or user.is_superuser:
        return None
    profile = getattr(user, 'restaurant_profile', None)
    if profile is None:
        return None
    try:
        return profile.custom_role
    except Exception:
        return None


def level(user, area):
    role = custom_role_of(user)
    if role is None:
        return MANAGE
    return role.level(area)


def access_map(user):
    """{area: level} for templates. Users without a custom role get MANAGE everywhere (their built-in role applies)."""
    role = custom_role_of(user)
    if role is None:
        return {k: MANAGE for k in AREA_KEYS}
    return {k: role.level(k) for k in AREA_KEYS}


def home_url(role, namespace):
    """URL name of the first screen this role may open, preferring the shell the user is in."""
    own = R if namespace == 'restaurant_admin' else H
    for key in AREA_KEYS:
        if role.level(key) == NONE:
            continue
        if AREA_MODULE[key] == S:
            return HOME[key].format(ns=namespace)
        if AREA_MODULE[key] == own:
            return HOME[key]
    for key in AREA_KEYS:
        if role.level(key) != NONE and AREA_MODULE[key] != S:
            return HOME[key]
    return None


class RolePermissionMiddleware:
    """Enforce custom roles on every Restaurant and Hotel admin URL."""

    SAFE = ('GET', 'HEAD', 'OPTIONS')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, 'resolver_match', None)
        if match is None or match.namespace not in RULES:
            return None
        role = custom_role_of(request.user)
        if role is None:
            return None
        key = (match.namespace, match.url_name)
        if key in ALWAYS or (match.url_name or '').endswith('logout'):
            return None
        area = area_for(*key)
        have = role.level(area) if area else NONE
        need = VIEW if request.method in self.SAFE and not is_mutating(match.url_name) else MANAGE
        if RANK[have] >= RANK[need]:
            return None
        return self._deny(request, role, area, have, match.namespace, view_kwargs)

    def _deny(self, request, role, area, have, namespace, kwargs):
        wants_json = (request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Accept', '')
                      or 'application/json' in (request.content_type or '') or '/api/' in request.path or request.path.rstrip('/').endswith('_api'))
        label = AREA_LABEL.get(area, 'this screen')
        msg = (f'Your role "{role.name}" can only view {label}.' if have == VIEW
               else f'Your role "{role.name}" does not include {label}.')
        if wants_json:
            return JsonResponse({'success': False, 'error': msg}, status=403)
        if request.method in self.SAFE and have == NONE:
            target = home_url(role, namespace)
            slug = kwargs.get('restaurant_slug')
            if target and slug:
                try:
                    url = reverse(target, kwargs={'restaurant_slug': slug})
                except NoReverseMatch:
                    url = None
                if url and url != request.path:
                    messages.warning(request, msg)
                    return redirect(url)
        if have == VIEW and request.method not in self.SAFE:
            messages.error(request, msg)
            ref = request.META.get('HTTP_REFERER', '')
            if ref and request.get_host() in ref:
                return redirect(ref)
        return HttpResponseForbidden(msg)
