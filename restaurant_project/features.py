"""
features.py — Centralized Feature & Branch Type Management System.

Architecture:
1. TAG NAMES / BRANCH TYPES (Top Section)
   - Configured presets aligned with Sattkar Master Admin 4 Primary Package Tiers:
     1. POS Only (Billing & Quick Order)
     2. Starter Package (Dhaba & Cafe)
     3. Complete Restaurant without Alcohol (Full Restaurant)
     4. Complete Restaurant with Bar / Fine Dine
   - Extensible dynamic registry hook (support for dynamic tags loaded later from DB or Master Admin API).

2. ALLOWED FEATURES GROUPED BY ADMIN (Middle Section)
   - Restaurant Admin Features (POS, Dine-In Tables, Menu, Kitchen KDS, Stock, Staff, Finance, Delivery, Guests & Loyalty, Integrations, VAT)
   - Hotel Admin Features (PMS Lodging, Rooms, Front Desk, Tape Chart, Folios, Housekeeping, Hotel Website, Spas, Cabs)
   - Events & Club Admin Features (Ticketed Events, VIP Tiers, Gate QR Scanner, Guestlist)
   - Website & Storefront Features (Minimal Ordering Mode vs. Full Website CMS, Table Reservations, Online Payments)

3. CENTRAL RESOLUTION ENGINE (Bottom Section)
   - Unified feature registry with aliases (e.g. loyalty -> guests, zomato -> delivery, pnl -> finance, etc.)
   - is_feature_enabled(branch, feature_key) helper
   - Helper methods for serializers, context processors, and Super Admin UI serialization.
"""

from typing import Dict, Any, List, Set, Optional
import json


# ==============================================================================
# SECTION 1: TAG NAMES & BRANCH TYPES (Configured Presets & Dynamic Registry)
# ==============================================================================

# Dynamic registry for custom tags that can be loaded dynamically later (e.g. via Master API or DB)
_DYNAMIC_TAG_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Tag key aliases to map Master Admin keys, slug variations & legacy choices
TAG_KEY_ALIASES: Dict[str, str] = {
    'only-pos': 'only_pos',
    'pos_only': 'only_pos',
    'pos': 'only_pos',
    'tier_only_pos': 'only_pos',
    'starter': 'starter_dhaba',
    'dhaba': 'starter_dhaba',
    'dhaba_cafe': 'starter_dhaba',
    'starter_package': 'starter_dhaba',
    'tier_starter_dhaba': 'starter_dhaba',
    'complete_restaurant_without_alcohol': 'restaurant_standard',
    'complete-restaurant-without-alcohol': 'restaurant_standard',
    'tier_restaurant_standard': 'restaurant_standard',
    'standard_restaurant': 'restaurant_standard',
    'standard restaurant': 'restaurant_standard',
    'standard': 'restaurant_standard',
    'dine_in': 'restaurant_standard',
    'full': 'restaurant_standard',
    'complete': 'restaurant_standard',
    'complete_restaurant': 'restaurant_standard',
    'complete_restaurant_with_alcohol': 'fine_dine_bar',
    'complete-restaurant-with-alcohol': 'fine_dine_bar',
    'complete_restaurant_with_bar': 'fine_dine_bar',
    'tier_fine_dine_bar': 'fine_dine_bar',
    'fine_dine': 'fine_dine_bar',
    'bar': 'fine_dine_bar',
}


def normalize_tag_key(tag_key: str) -> str:
    """Normalizes any branch type/tag key to authoritative canonical key."""
    clean = str(tag_key or '').strip().lower().replace('-', '_').replace(' ', '_')
    return TAG_KEY_ALIASES.get(clean, TAG_KEY_ALIASES.get(str(tag_key or '').strip().lower(), clean))


DEFAULT_BRANCH_TAGS: Dict[str, Dict[str, Any]] = {
    # --------------------------------------------------------------------------
    # SATTKAR MASTER ADMIN 4 PRIMARY PACKAGE TIERS
    # --------------------------------------------------------------------------
    'only_pos': {
        'name': 'POS Only (Billing & Quick Order)',
        'tag': 'POS ONLY',
        'badge_color': '#6366f1',
        'icon': 'fa-cash-register',
        'description': 'Designed for small sweets shops, bakeries, or takeaway counters providing only POS checkout and order management.',
        'base_features': ['pos', 'menu', 'website_minimal'],
        'recommended_addons': ['dine_in', 'kitchen', 'inventory', 'staff', 'delivery', 'finance', 'guests', 'integrations', 'online_payments'],
    },
    'starter_dhaba': {
        'name': 'Starter Package (Dhaba & Cafe)',
        'tag': 'STARTER',
        'badge_color': '#f59e0b',
        'icon': 'fa-utensils',
        'description': 'Designed for small food joints, dhabas, and kiosks. Minimal ordering menu, direct table QR, and counter order creation only.',
        'base_features': ['pos', 'dine_in', 'kitchen', 'menu', 'website_minimal'],
        'recommended_addons': ['inventory', 'staff', 'delivery', 'finance', 'guests', 'integrations', 'online_payments'],
    },
    'restaurant_standard': {
        'name': 'Complete Restaurant without Alcohol',
        'tag': 'STANDARD RESTAURANT',
        'badge_color': '#10b981',
        'icon': 'fa-store',
        'description': 'Full restaurant operations suite: POS billing, Dine-in tables, Kitchen Display (KDS), stock inventory & purchases, staff operations & payroll, cashflow P&L finance, Zomato/Swiggy delivery integrations, guest loyalty & CRM, third-party integrations, and branded website CMS.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff',
            'delivery', 'finance', 'guests', 'feedback', 'campaigns', 'integrations',
            'analytics', 'website_cms', 'website_minimal', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['alcohol_vat', 'events', 'hotel'],
    },
    'fine_dine_bar': {
        'name': 'Complete Restaurant with Bar / Fine Dine',
        'tag': 'FINE DINE & BAR',
        'badge_color': '#ec4899',
        'icon': 'fa-wine-glass',
        'description': 'All Complete Restaurant capabilities PLUS State VAT tax calculations, liquor stock tracking, excise license compliance, and beverage billing.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff',
            'delivery', 'finance', 'guests', 'feedback', 'campaigns', 'integrations',
            'alcohol_vat', 'analytics', 'website_cms', 'website_minimal', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['events', 'hotel'],
    },

    # --------------------------------------------------------------------------
    # SPECIALIZED VENUES & LEGACY COMPATIBILITY PRESETS
    # --------------------------------------------------------------------------
    'takeaway_qsr': {
        'name': 'Takeaway / QSR',
        'tag': 'QSR',
        'badge_color': '#0ea5e9',
        'icon': 'fa-burger',
        'description': 'Optimized for quick-service counters and takeaway joints. Counter POS, kitchen KDS, digital menu, and delivery aggregator pickups.',
        'base_features': ['pos', 'kitchen', 'menu', 'delivery', 'integrations', 'website_minimal'],
        'recommended_addons': ['inventory', 'staff', 'finance', 'guests', 'website_cms'],
    },
    'cloud_kitchen': {
        'name': 'Cloud Kitchen',
        'tag': 'CLOUD KITCHEN',
        'badge_color': '#8b5cf6',
        'icon': 'fa-fire-burner',
        'description': 'Pure delivery and dark kitchen operations. Delivery channel dispatch, raw ingredient stock, kitchen KDS, and recipe management.',
        'base_features': ['pos', 'kitchen', 'menu', 'inventory', 'delivery', 'integrations'],
        'recommended_addons': ['staff', 'finance', 'guests'],
    },
    'hotel_resort': {
        'name': 'Hotel & Resort',
        'tag': 'HOTEL & RESORT',
        'badge_color': '#3b82f6',
        'icon': 'fa-hotel',
        'description': 'Comprehensive hospitality with lodging and food. Hotel PMS (rooms, front desk, folios), dining POS, banquets, and guest service.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff', 'finance',
            'delivery', 'guests', 'feedback', 'campaigns', 'integrations',
            'hotel', 'hotel_rooms', 'front_desk', 'tape_chart', 'hotel_folios', 'housekeeping',
            'website_cms', 'website_minimal', 'analytics', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['events', 'alcohol_vat', 'hotel_spa', 'hotel_cabs'],
    },
    'club_lounge': {
        'name': 'Club & Lounge',
        'tag': 'CLUB & LOUNGE',
        'badge_color': '#a855f7',
        'icon': 'fa-champagne-glasses',
        'description': 'Nightclub, lounge & entertainment venue. Ticketed club events, VIP passes, gate entry QR scanner, bar POS, and liquor billing.',
        'base_features': ['pos', 'menu', 'events', 'club_events', 'ticket_tiers', 'gate_scanner', 'guestlist', 'alcohol_vat', 'website_cms', 'analytics'],
        'recommended_addons': ['dine_in', 'inventory', 'staff', 'finance', 'guests'],
    },
    'custom': {
        'name': 'Custom / Enterprise',
        'tag': 'ENTERPRISE',
        'badge_color': '#64748b',
        'icon': 'fa-sliders',
        'description': 'Tailored configuration for specialized operations. Start with basic tools and selectively enable required add-ons.',
        'base_features': ['pos', 'menu'],
        'recommended_addons': ['dine_in', 'kitchen', 'inventory', 'staff', 'finance', 'website_cms', 'delivery', 'guests', 'integrations', 'events', 'hotel', 'alcohol_vat'],
    },
    # Direct alias pointers for legacy databases
    'dine_in': {
        'name': 'Standard Restaurant (Dine-in)',
        'tag': 'STANDARD RESTAURANT',
        'badge_color': '#10b981',
        'icon': 'fa-store',
        'description': 'Full-fledged dine-in restaurant with POS, tables, kitchen, inventory, staff, finance, delivery, guest loyalty and website CMS.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff',
            'delivery', 'finance', 'guests', 'feedback', 'campaigns', 'integrations',
            'analytics', 'website_cms', 'website_minimal', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['alcohol_vat', 'events', 'hotel'],
    },
    'starter': {
        'name': 'Starter Package (Dhaba & Cafe)',
        'tag': 'STARTER',
        'badge_color': '#f59e0b',
        'icon': 'fa-utensils',
        'description': 'Designed for small food joints, dhabas, and kiosks. Minimal ordering menu, direct table QR, and counter order creation only.',
        'base_features': ['pos', 'dine_in', 'kitchen', 'menu', 'website_minimal'],
        'recommended_addons': ['inventory', 'staff', 'delivery', 'finance', 'guests', 'integrations', 'online_payments'],
    },
    'complete_restaurant_without_alcohol': {
        'name': 'Complete Restaurant without Alcohol',
        'tag': 'STANDARD RESTAURANT',
        'badge_color': '#10b981',
        'icon': 'fa-store',
        'description': 'Full restaurant operations suite without Alcohol & State VAT.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff',
            'delivery', 'finance', 'guests', 'feedback', 'campaigns', 'integrations',
            'analytics', 'website_cms', 'website_minimal', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['alcohol_vat', 'events', 'hotel'],
    },
    'complete_restaurant_with_alcohol': {
        'name': 'Complete Restaurant with Bar / Fine Dine',
        'tag': 'FINE DINE & BAR',
        'badge_color': '#ec4899',
        'icon': 'fa-wine-glass',
        'description': 'Full restaurant operations suite with Alcohol Service & State VAT compliance.',
        'base_features': [
            'pos', 'dine_in', 'kitchen', 'menu', 'inventory', 'staff',
            'delivery', 'finance', 'guests', 'feedback', 'campaigns', 'integrations',
            'alcohol_vat', 'analytics', 'website_cms', 'website_minimal', 'table_booking', 'online_payments',
        ],
        'recommended_addons': ['events', 'hotel'],
    },
}


def register_dynamic_branch_tag(tag_key: str, config: Dict[str, Any]) -> None:
    """
    Registers a new branch type/tag dynamically at runtime.
    Allows dynamic tags to be populated from Master Admin API or database.
    """
    _DYNAMIC_TAG_REGISTRY[normalize_tag_key(tag_key)] = config


def get_all_branch_tags() -> Dict[str, Dict[str, Any]]:
    """
    Returns combined tag registry (static presets + runtime dynamic tags).
    """
    merged = dict(DEFAULT_BRANCH_TAGS)
    merged.update(_DYNAMIC_TAG_REGISTRY)
    return merged


def get_branch_tag_config(tag_key: str) -> Dict[str, Any]:
    """
    Returns the configuration for a given branch tag or defaults to restaurant_standard.
    """
    all_tags = get_all_branch_tags()
    clean_key = normalize_tag_key(tag_key)
    return all_tags.get(clean_key, all_tags.get('restaurant_standard', {}))


# ==============================================================================
# SECTION 2: ALLOWED FEATURES GROUPED BY ADMIN DOMAINS
# ==============================================================================

RESTAURANT_ADMIN_FEATURES: Dict[str, Dict[str, Any]] = {
    'pos': {
        'key': 'pos',
        'name': 'Point of Sale (POS)',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-cash-register',
        'desc': 'Counter checkout, bill creation, and quick billing',
        'is_addon': False,
    },
    'dine_in': {
        'key': 'dine_in',
        'name': 'Dine-In & Tables',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-chair',
        'desc': 'Table QR codes, floor layout, and table management',
        'is_addon': False,
    },
    'menu': {
        'key': 'menu',
        'name': 'Menu Management',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-book-open',
        'desc': 'Dishes, categories, add-ons, pricing, and variants',
        'is_addon': False,
    },
    'kitchen': {
        'key': 'kitchen',
        'name': 'Kitchen Display (KDS)',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-kitchen-set',
        'desc': 'Live kitchen order screen and preparation timer',
        'is_addon': False,
    },
    'inventory': {
        'key': 'inventory',
        'name': 'Inventory & Purchases',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-boxes-stacked',
        'desc': 'Raw material stock, suppliers, purchase orders & wastage',
        'is_addon': True,
        'addon_label': 'Stock & Inventory Add-on',
    },
    'staff': {
        'key': 'staff',
        'name': 'Staff & Operations',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-users',
        'desc': 'Employee profiles, roles, attendance, and payroll',
        'is_addon': True,
        'addon_label': 'Staff & Payroll Add-on',
    },
    'finance': {
        'key': 'finance',
        'name': 'Finance & Cashflow P&L',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-calculator',
        'desc': 'Operating expenses, register closing, and profit/loss statements',
        'is_addon': True,
        'addon_label': 'Finance & P&L Add-on',
    },
    'delivery': {
        'key': 'delivery',
        'name': 'Delivery Aggregators (Zomato / Swiggy)',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-motorcycle',
        'desc': 'Zomato & Swiggy online order integration and rider dispatch',
        'is_addon': True,
        'addon_label': 'Delivery Integrations Add-on',
    },
    'guests': {
        'key': 'guests',
        'name': 'Guests & Loyalty CRM',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-address-book',
        'desc': 'Guest profiles, visit history, loyalty points, and digital gift cards',
        'is_addon': True,
        'addon_label': 'Guests & Loyalty Add-on',
    },
    'feedback': {
        'key': 'feedback',
        'name': 'Guest Feedback & Reviews',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-comment-dots',
        'desc': 'Guest ratings, dining feedback, complaint follow-ups, and NPS',
        'is_addon': False,
    },
    'campaigns': {
        'key': 'campaigns',
        'name': 'Marketing Campaigns',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-bullhorn',
        'desc': 'Targeted WhatsApp, SMS, and email marketing campaigns for diners',
        'is_addon': False,
    },
    'integrations': {
        'key': 'integrations',
        'name': 'Platform Integrations & APIs',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-plug',
        'desc': 'Zomato, Swiggy partner credentials, API access keys, and webhook endpoints',
        'is_addon': True,
        'addon_label': 'Integrations & Webhooks Add-on',
    },
    'alcohol_vat': {
        'key': 'alcohol_vat',
        'name': 'State VAT & Liquor Service',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-wine-glass',
        'desc': 'Excise license, separate State VAT calculations, and bar inventory',
        'is_addon': True,
        'addon_label': 'State VAT & Bar Add-on',
    },
    'analytics': {
        'key': 'analytics',
        'name': 'Sales Analytics',
        'admin_group': 'restaurant_admin',
        'icon': 'fa-chart-pie',
        'desc': 'Revenue dashboards, sales breakdown, and item trends',
        'is_addon': False,
    },
}

HOTEL_ADMIN_FEATURES: Dict[str, Dict[str, Any]] = {
    'hotel': {
        'key': 'hotel',
        'name': 'Hotel Lodging & PMS',
        'admin_group': 'hotel_admin',
        'icon': 'fa-hotel',
        'desc': 'Master room inventory, reservations, folios & housekeeping',
        'is_addon': True,
        'addon_label': 'Hotel PMS Add-on',
    },
    'hotel_rooms': {
        'key': 'hotel_rooms',
        'name': 'Room Inventory & Categories',
        'admin_group': 'hotel_admin',
        'icon': 'fa-bed',
        'desc': 'Room numbers, suites, amenities, and tariff rates',
        'is_addon': False,
    },
    'front_desk': {
        'key': 'front_desk',
        'name': 'Front Desk & Check-In',
        'admin_group': 'hotel_admin',
        'icon': 'fa-id-card',
        'desc': 'Guest check-in, check-out, and registration card printing',
        'is_addon': False,
    },
    'tape_chart': {
        'key': 'tape_chart',
        'name': 'Room Tape Chart',
        'admin_group': 'hotel_admin',
        'icon': 'fa-calendar-days',
        'desc': 'Visual room booking timeline and drag-and-drop allocation',
        'is_addon': False,
    },
    'hotel_folios': {
        'key': 'hotel_folios',
        'name': 'Guest Folios & Billing',
        'admin_group': 'hotel_admin',
        'icon': 'fa-receipt',
        'desc': 'Room service charges, payments, and consolidated invoices',
        'is_addon': False,
    },
    'housekeeping': {
        'key': 'housekeeping',
        'name': 'Housekeeping & Maintenance',
        'admin_group': 'hotel_admin',
        'icon': 'fa-broom',
        'desc': 'Room cleanliness tracking and maintenance logs',
        'is_addon': False,
    },
    'hotel_spa': {
        'key': 'hotel_spa',
        'name': 'Spas & Wellness',
        'admin_group': 'hotel_admin',
        'icon': 'fa-spa',
        'desc': 'Complete spa treatments, wellness therapy packages, and appointment booking',
        'is_addon': True,
        'addon_label': 'Spa & Wellness Add-on',
    },
    'hotel_cabs': {
        'key': 'hotel_cabs',
        'name': 'Cabs & Guest Transportation',
        'admin_group': 'hotel_admin',
        'icon': 'fa-taxi',
        'desc': 'Cab booking, airport transfers, chauffeur dispatch, and transportation billing',
        'is_addon': True,
        'addon_label': 'Cabs & Transportation Add-on',
    },
}

EVENTS_ADMIN_FEATURES: Dict[str, Dict[str, Any]] = {
    'events': {
        'key': 'events',
        'name': 'Club & Ticketed Events',
        'admin_group': 'events_admin',
        'icon': 'fa-champagne-glasses',
        'desc': 'Nightclub events, ticket tiers, gate pass verification, and guestlist',
        'is_addon': True,
        'addon_label': 'Club & Events Add-on',
    },
    'club_events': {
        'key': 'club_events',
        'name': 'Event Scheduling & Lineup',
        'admin_group': 'events_admin',
        'icon': 'fa-calendar-star',
        'desc': 'Concerts, DJ nights, artist lineup, and event scheduling',
        'is_addon': False,
    },
    'ticket_tiers': {
        'key': 'ticket_tiers',
        'name': 'Ticket Pricing & VIP Tiers',
        'admin_group': 'events_admin',
        'icon': 'fa-ticket',
        'desc': 'Early bird, General Admission, and VIP bottle service passes',
        'is_addon': False,
    },
    'gate_scanner': {
        'key': 'gate_scanner',
        'name': 'Gate Entry QR Scanner',
        'admin_group': 'events_admin',
        'icon': 'fa-qrcode',
        'desc': 'Mobile/web entry validation and fraud-proof pass scanning',
        'is_addon': False,
    },
    'guestlist': {
        'key': 'guestlist',
        'name': 'VIP Guestlist Management',
        'admin_group': 'events_admin',
        'icon': 'fa-clipboard-list',
        'desc': 'VIP entry lists, promoter tracking, and complimentary passes',
        'is_addon': False,
    },
}

WEBSITE_STOREFRONT_FEATURES: Dict[str, Dict[str, Any]] = {
    'website_minimal': {
        'key': 'website_minimal',
        'name': 'Minimal Ordering Website',
        'admin_group': 'website_storefront',
        'icon': 'fa-mobile-screen',
        'desc': 'Lightweight menu view and digital ordering without extra landing pages',
        'is_addon': False,
    },
    'website_cms': {
        'key': 'website_cms',
        'name': 'Full Website & CMS Pages',
        'admin_group': 'website_storefront',
        'icon': 'fa-globe',
        'desc': 'Branded About Us, Chef profiles, Gallery, Reviews, Careers, and FAQs',
        'is_addon': True,
        'addon_label': 'Full Website & CMS Add-on',
    },
    'table_booking': {
        'key': 'table_booking',
        'name': 'Online Table Reservations',
        'admin_group': 'website_storefront',
        'icon': 'fa-calendar-check',
        'desc': 'Customer table reservation requests from website',
        'is_addon': True,
        'addon_label': 'Table Reservation Add-on',
    },
    'online_payments': {
        'key': 'online_payments',
        'name': 'Digital Payment Gateway',
        'admin_group': 'website_storefront',
        'icon': 'fa-credit-card',
        'desc': 'Razorpay / UPI digital payments for customer orders',
        'is_addon': True,
        'addon_label': 'Online Payments Add-on',
    },
}


# ==============================================================================
# SECTION 3: UNIFIED REGISTRY, ADDONS CATALOG & ALIASES
# ==============================================================================

ALL_FEATURES_REGISTRY: Dict[str, Dict[str, Any]] = {
    **RESTAURANT_ADMIN_FEATURES,
    **HOTEL_ADMIN_FEATURES,
    **EVENTS_ADMIN_FEATURES,
    **WEBSITE_STOREFRONT_FEATURES,
}

# Aliases to gracefully resolve alternate terms
FEATURE_ALIASES: Dict[str, str] = {
    'tables': 'dine_in',
    'table_management': 'dine_in',
    'stock': 'inventory',
    'purchases': 'inventory',
    'purchase': 'inventory',
    'payroll': 'staff',
    'employees': 'staff',
    'accounts': 'finance',
    'accounting': 'finance',
    'pnl': 'finance',
    'expenses': 'finance',
    'guest': 'guests',
    'loyalty': 'guests',
    'crm': 'guests',
    'guest_loyalty': 'guests',
    'gift_cards': 'guests',
    'gift_card': 'guests',
    'feedback': 'feedback',
    'reviews': 'feedback',
    'campaign': 'campaigns',
    'campaigns': 'campaigns',
    'marketing': 'campaigns',
    'integrations': 'integrations',
    'integration': 'integrations',
    'api': 'integrations',
    'api_keys': 'integrations',
    'webhooks': 'integrations',
    'webhook': 'integrations',
    'zomato': 'delivery',
    'swiggy': 'delivery',
    'aggregators': 'delivery',
    'delivery_integrations': 'delivery',
    'online_delivery': 'delivery',
    'website': 'website_cms',
    'cms': 'website_cms',
    'landing': 'website_cms',
    'club': 'events',
    'ticketing': 'events',
    'liquor': 'alcohol_vat',
    'bar': 'alcohol_vat',
    'vat': 'alcohol_vat',
    'hotel_pms': 'hotel',
    'rooms': 'hotel',
    'spa': 'hotel_spa',
    'spas': 'hotel_spa',
    'wellness': 'hotel_spa',
    'spas_wellness': 'hotel_spa',
    'cab': 'hotel_cabs',
    'cabs': 'hotel_cabs',
    'transport': 'hotel_cabs',
    'transportation': 'hotel_cabs',
}


def normalize_feature_key(key: str) -> str:
    """Normalizes a feature key using alias mappings."""
    clean = str(key or '').strip().lower()
    return FEATURE_ALIASES.get(clean, clean)


def get_addon_catalog() -> Dict[str, Dict[str, Any]]:
    """Returns only features marked as add-on eligible."""
    catalog = {}
    for k, feat in ALL_FEATURES_REGISTRY.items():
        if feat.get('is_addon'):
            catalog[k] = {
                'key': k,
                'name': feat.get('addon_label', feat['name']),
                'icon': feat['icon'],
                'hint': feat['desc'],
                'admin_group': feat['admin_group'],
            }
    return catalog


def get_features_by_admin(admin_group: str) -> Dict[str, Dict[str, Any]]:
    """Returns feature dict filtered by admin group."""
    clean_group = str(admin_group or '').strip().lower()
    if 'rest' in clean_group:
        return dict(RESTAURANT_ADMIN_FEATURES)
    if 'hotel' in clean_group:
        return dict(HOTEL_ADMIN_FEATURES)
    if 'event' in clean_group or 'club' in clean_group:
        return dict(EVENTS_ADMIN_FEATURES)
    if 'web' in clean_group or 'site' in clean_group:
        return dict(WEBSITE_STOREFRONT_FEATURES)
    return {k: v for k, v in ALL_FEATURES_REGISTRY.items() if v['admin_group'] == clean_group}


def get_branch_type_defaults() -> Dict[str, Set[str]]:
    """Returns dict of branch_type -> set of default feature keys."""
    all_tags = get_all_branch_tags()
    return {k: set(cfg.get('base_features', [])) for k, cfg in all_tags.items()}


# ==============================================================================
# SECTION 4: CENTRAL EVALUATION & ACCESS HELPERS
# ==============================================================================

def get_branch_effective_features(branch) -> Set[str]:
    """
    Computes the exact authoritative set of enabled features for a given branch
    based on its dynamic plan entitlements from Master Admin and branch configuration.
    Guarantees that a branch gets the exact correct group of features — not less, not extra!
    """
    if not branch:
        return set()

    r = getattr(branch, 'restaurant', None)
    is_master_managed = bool(r and getattr(r, 'master_sync_status', '') == 'SYNCED' and getattr(r, 'master_license_key', ''))
    flags = getattr(branch, 'plan_entitlements', {}) or {}

    # 1. Determine base package tier features from dynamic flags or outlet_type
    b_type = getattr(branch, 'outlet_type', None) or getattr(branch, 'branch_type', None) or 'restaurant_standard'
    b_type = normalize_tag_key(b_type)

    if is_master_managed and flags:
        if flags.get('has_only_pos'):
            tier_key = 'only_pos'
        elif flags.get('has_starter'):
            tier_key = 'starter_dhaba'
        elif flags.get('has_complete_restaurant_without_alcohol'):
            tier_key = 'restaurant_standard'
        elif flags.get('has_complete_restaurant_with_alcohol') or (flags.get('is_alcohol') and not flags.get('has_complete_restaurant_without_alcohol')):
            tier_key = 'fine_dine_bar'
        elif flags.get('has_takeaway_qsr') or flags.get('has_qsr'):
            tier_key = 'takeaway_qsr'
        elif flags.get('has_cloud_kitchen'):
            tier_key = 'cloud_kitchen'
        elif flags.get('has_hotel_resort'):
            tier_key = 'hotel_resort'
        elif flags.get('has_club_lounge'):
            tier_key = 'club_lounge'
        else:
            tier_key = b_type
    else:
        tier_key = b_type

    b_cfg = get_branch_tag_config(tier_key)
    features: Set[str] = set(b_cfg.get('base_features', []))

    # 2. Dynamic Add-ons & Entitlement Flags
    # Allows POS Only or Starter branches to activate specific add-on capabilities
    addons = set(getattr(branch, 'branch_addons', []) or [])
    for a_key in addons:
        norm_addon = normalize_feature_key(a_key)
        if norm_addon in ALL_FEATURES_REGISTRY:
            features.add(norm_addon)

    # Entitlement flags for modular add-ons from Master Admin payload
    if flags.get('has_delivery') or flags.get('has_zomato_swiggy'):
        features.add('delivery')
    if flags.get('has_finance') or flags.get('has_pnl'):
        features.add('finance')
    if flags.get('has_guests') or flags.get('has_loyalty') or flags.get('has_crm'):
        features.add('guests')
    if flags.get('has_integrations'):
        features.add('integrations')
    if flags.get('has_inventory') or flags.get('has_stock'):
        features.add('inventory')
    if flags.get('has_staff') or flags.get('has_payroll'):
        features.add('staff')
    if flags.get('has_tables') or flags.get('has_dine_in'):
        features.add('dine_in')
    if flags.get('has_kitchen') or flags.get('has_kds'):
        features.add('kitchen')

    # Bundle dependent features
    if 'guests' in features:
        features.update(['feedback', 'campaigns'])
    if 'delivery' in features:
        features.add('integrations')

    # 3. Check major standalone modules
    # Hotel PMS (Rooms, Front Desk, Tape Chart, Folios, Housekeeping)
    is_hotel_licensed = branch.is_module_licensed('hotel') if hasattr(branch, 'is_module_licensed') else True
    if getattr(branch, 'has_hotel_module', False) and is_hotel_licensed and getattr(r, 'feature_hotel_pms', True):
        features.update(['hotel', 'hotel_rooms', 'front_desk', 'tape_chart', 'hotel_folios', 'housekeeping'])

        # Sub-options under Hotel: Spas & Wellness and Cabs (only active when licensed & selected)
        is_spa_licensed = branch.is_module_licensed('hotel_spa') if hasattr(branch, 'is_module_licensed') else True
        if is_spa_licensed and (flags.get('has_spas_wellness') or flags.get('has_spa') or flags.get('has_wellness') or 'hotel_spa' in addons or 'spa' in addons):
            features.add('hotel_spa')
        else:
            features.discard('hotel_spa')

        is_cabs_licensed = branch.is_module_licensed('hotel_cabs') if hasattr(branch, 'is_module_licensed') else True
        if is_cabs_licensed and (flags.get('has_cabs') or flags.get('has_cab') or flags.get('has_transport') or 'hotel_cabs' in addons or 'cabs' in addons):
            features.add('hotel_cabs')
        else:
            features.discard('hotel_cabs')
    else:
        features.difference_update(['hotel', 'hotel_rooms', 'front_desk', 'tape_chart', 'hotel_folios', 'housekeeping', 'hotel_spa', 'hotel_cabs'])

    # Club & Events (Events, Tiers, Gate Scanner, Guestlist)
    is_club_licensed = branch.is_module_licensed('club') if hasattr(branch, 'is_module_licensed') else True
    if getattr(branch, 'has_club_module', False) and is_club_licensed and getattr(r, 'feature_club_events', True):
        features.update(['events', 'club_events', 'ticket_tiers', 'gate_scanner', 'guestlist'])
    else:
        features.difference_update(['events', 'club_events', 'ticket_tiers', 'gate_scanner', 'guestlist'])

    # State VAT & Alcohol
    is_alcohol_licensed = branch.is_module_licensed('alcohol_vat') if hasattr(branch, 'is_module_licensed') else True
    if getattr(branch, 'serves_alcohol', False) and is_alcohol_licensed and getattr(r, 'feature_alcohol_vat', True):
        features.add('alcohol_vat')
    else:
        features.discard('alcohol_vat')

    # Restaurant POS restaurant-wide gating
    if not getattr(r, 'feature_restaurant_pos', True):
        features.difference_update(['pos', 'dine_in', 'kitchen', 'menu'])

    # Website restaurant-wide gating
    has_brand_cms = getattr(r, 'feature_website_cms', True) if r else True
    is_menu_only = getattr(r, 'is_menu_only_website', False) if r else False
    if not getattr(r, 'feature_website_pages', True) or not has_brand_cms or is_menu_only:
        features.discard('website_cms')

    # Core administrative utilities always active for any valid branch
    features.update(['dashboard', 'settings', 'logs'])

    return features


def is_feature_enabled(branch, feature_key: str) -> bool:
    """
    Authoritative single-source-of-truth feature check for any branch.
    Evaluates effective features strictly based on dynamic plan entitlements and tier grouping.
    """
    fkey = normalize_feature_key(feature_key)
    if fkey in ('dashboard', 'settings', 'logs'):
        return True
    effective = get_branch_effective_features(branch)
    return fkey in effective


def get_features_js_config() -> Dict[str, Any]:
    """
    Returns a unified JSON-serializable dictionary for frontends and Super Admin templates.
    Includes:
    - branch_tags: tag metadata, name, badge, description, included features
    - addon_catalog: all add-on modules with hints, icons, and labels
    - admin_features: features categorized by admin domain
    """
    # Primary order for clean frontend display
    primary_order = [
        'only_pos',
        'starter_dhaba',
        'restaurant_standard',
        'fine_dine_bar',
        'takeaway_qsr',
        'cloud_kitchen',
        'hotel_resort',
        'club_lounge',
        'custom',
    ]
    all_tags = get_all_branch_tags()
    ordered_keys = [k for k in primary_order if k in all_tags] + [k for k in all_tags if k not in primary_order]

    tags_export = {}
    for key in ordered_keys:
        cfg = all_tags[key]
        included_meta = []
        for f_key in cfg.get('base_features', []):
            feat = ALL_FEATURES_REGISTRY.get(f_key)
            if feat:
                included_meta.append({
                    'key': f_key,
                    'name': feat['name'],
                    'icon': feat['icon'],
                    'admin_group': feat['admin_group'],
                })
        tags_export[key] = {
            'name': cfg['name'],
            'label': cfg['name'],
            'tag': cfg.get('tag', key.upper()),
            'badge_color': cfg.get('badge_color', '#10b981'),
            'icon': cfg.get('icon', 'fa-store'),
            'description': cfg['description'],
            'base_features': cfg.get('base_features', []),
            'included': included_meta,
            'recommended_addons': cfg.get('recommended_addons', []),
        }

    return {
        'branch_tags': tags_export,
        'addon_catalog': get_addon_catalog(),
        'restaurant_features': RESTAURANT_ADMIN_FEATURES,
        'hotel_features': HOTEL_ADMIN_FEATURES,
        'events_features': EVENTS_ADMIN_FEATURES,
        'website_features': WEBSITE_STOREFRONT_FEATURES,
        'feature_aliases': FEATURE_ALIASES,
        'tag_aliases': TAG_KEY_ALIASES,
    }
