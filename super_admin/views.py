from django.conf import settings
import os
import io
import json
import qrcode
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages

from django.urls import reverse
from django.db.models import Sum, Count, Avg, F, Q
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
from django.http import JsonResponse, HttpResponse
import csv
import logging

from django.core.files.storage import default_storage

from super_admin.forms import (
    RestaurantForm,
    RestaurantAdminCreateForm,
    OnboardRestaurantForm,
    RestaurantAdminUpdateForm,
    RestaurantLandingForm,
    GalleryImageForm,
    TeamMemberForm,
    ReviewForm,
    EventForm,
    WhyChooseUsForm,
    BranchContactSocialForm,
    BlogPostForm,
    AdminSetupForm,
    JobPostingForm,
    JobApplicationStatusForm,
)
from super_admin.models import (
    Restaurant,
    RestaurantUser,
    GstRegistration,
    RestaurantBranch,
    DocumentSeries,
    UserSecuritySetting,
    CustomerInquiry,
    NewsletterSubscriber,
    JobPosting,
    JobApplication,
)
from core.models import RestaurantLanding, GalleryImage, TeamMember, Review, Event, BlogPost, BlogComment, Order, OrderItem, MenuItem, Table, WhyChooseUs
from restaurant_project import features
from super_admin.services import MasterBillingService
from restaurant_project import constants

logger = logging.getLogger(__name__)


def superuser_required(view):
    return user_passes_test(lambda u: u.is_active and u.is_superuser)(view)


@superuser_required
def landing_index(request):
    if not constants.SHOW_WEBSITE_PAGES or not constants.is_module_enabled('ENABLE_WEBSITE_CMS'):
        messages.warning(request, "Website Pages CMS is not included in your current subscription plan. Digital menu ordering is active.")
        return redirect('super_admin:dashboard')
    restaurants = Restaurant.objects.all().order_by('name')
    return render(request, 'super_admin/landing_list.html', {
        'restaurants': restaurants,
    })


@superuser_required
def dashboard(request):
    # Auto-refresh live subscription & feature flags from Master Admin
    restaurant = Restaurant.objects.first()
    if restaurant:
        try:
            now = timezone.now()
            # Fast refresh if never synced or synced > 30 seconds ago or manual sync
            if request.GET.get('sync') or not restaurant.features_last_synced_at or (now - restaurant.features_last_synced_at).total_seconds() > 30:
                telemetry = MasterBillingService.fetch_subscription_details(restaurant, force=bool(request.GET.get('sync')))
                if request.GET.get('sync'):
                    if telemetry.get('connection_status') == 'CONNECTED':
                        messages.success(request, "Live modules and subscription refreshed from Sattkar.")
                    else:
                        reason = telemetry.get('message') or telemetry.get('connection_status') or 'unknown'
                        messages.warning(request, f"Sattkar is currently not connected ({reason}).")
                    return redirect('super_admin:dashboard')
        except Exception as sync_err:
            logger.warning(f"[Dashboard] Subscription sync skipped: {sync_err}")

    restaurants = Restaurant.objects.all().prefetch_related('branches', 'branches__gst_registration').order_by('name')
    admins = RestaurantUser.objects.select_related('user', 'restaurant', 'branch').order_by('restaurant__name', 'user__username')
    total_branches = sum(r.branches.count() for r in restaurants)
    return render(request, 'super_admin/dashboard.html', {
        'restaurants': restaurants,
        'admins': admins,
        'total_branches': total_branches,
    })


@superuser_required
def restaurant_create(request):
    effective_max_businesses = constants.get_effective_max_businesses()
    if effective_max_businesses > 0 and Restaurant.objects.count() >= effective_max_businesses:
        existing = Restaurant.objects.first()
        messages.warning(
            request,
            f"Business limit reached ({effective_max_businesses} allowed). You can edit '{existing.trade_name or existing.name}' details and manage outlets below."
        )
        return redirect('super_admin:restaurant_edit', pk=existing.pk)

    if request.method == 'POST':
        form = RestaurantForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Restaurant created successfully.')
            return redirect('super_admin:dashboard')
    else:
        form = RestaurantForm()
    return render(request, 'super_admin/restaurant_form.html', {'form': form})


@superuser_required
def restaurant_admin_create(request):
    if request.method == 'POST':
        form = RestaurantAdminCreateForm(request.POST)
        if form.is_valid():
            ru = form.save()
            messages.success(request, f'Restaurant admin {ru.user.username} created for {ru.restaurant.name}.')
            return redirect('super_admin:dashboard')
    else:
        form = RestaurantAdminCreateForm()
    return render(request, 'super_admin/restaurant_admin_form.html', {'form': form})


import json
import os


@superuser_required
def onboard_restaurant(request):
    existing_restaurant = Restaurant.objects.first()

    # If a restaurant already exists on GET, seamlessly redirect to edit mode for that single business
    if request.method == 'GET' and existing_restaurant:
        return redirect('super_admin:restaurant_edit', pk=existing_restaurant.pk)

    if request.method == 'POST':
        # If a restaurant exists, bind to it to guarantee update rather than duplicate creation
        target_instance = existing_restaurant
        form = OnboardRestaurantForm(request.POST, instance=target_instance)
        branches_json = request.POST.get('branches_json', '[]')
        try:
            branches_data = json.loads(branches_json)
        except Exception:
            branches_data = []

        if form.is_valid():
            restaurant = form.save(instance=target_instance, branches_data=branches_data)

            # ── Auto-sync to MasterPanel ─────────────────────────────────────
            master_url = getattr(settings, 'MASTER_DASHBOARD_URL', None) or os.environ.get('MASTER_DASHBOARD_URL', 'http://localhost:3000')
            api_secret = getattr(settings, 'MASTER_API_SECRET', None) or os.environ.get('MASTER_API_SECRET', '')
            if master_url:
                try:
                    sync_result = restaurant.sync_to_master_dashboard(master_url, api_secret)
                    if sync_result.get('license_key') and not restaurant.master_license_key:
                        restaurant.master_license_key = sync_result['license_key']
                        restaurant.save(update_fields=['master_license_key'])
                except Exception:
                    pass

            action_word = "updated" if target_instance else "onboarded"
            messages.success(
                request,
                f"Business '{restaurant.trade_name or restaurant.name}' {action_word} successfully with {restaurant.branches.count()} branch outlet(s)."
            )

            return redirect('super_admin:dashboard')
    else:
        form = OnboardRestaurantForm()

    all_restaurants = Restaurant.objects.all().order_by('name')
    return render(request, 'super_admin/onboard.html', {
        'form': form,
        'all_restaurants': all_restaurants,
        'features_config_json': json.dumps(features.get_features_js_config()),
    })


# --- Restaurant CRUD ---
@superuser_required
def restaurant_edit(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    try:
        now = timezone.now()
        if not restaurant.features_last_synced_at or (now - restaurant.features_last_synced_at).total_seconds() > 3:
            MasterBillingService.fetch_subscription_details(restaurant)
            restaurant.refresh_from_db()
    except Exception:
        pass

    if request.method == 'POST':
        form = OnboardRestaurantForm(request.POST, instance=restaurant)
        branches_json = request.POST.get('branches_json', '[]')
        try:
            branches_data = json.loads(branches_json)
        except Exception:
            branches_data = []

        if form.is_valid():
            restaurant = form.save(instance=restaurant, branches_data=branches_data)

            # ── Auto-sync to MasterPanel ─────────────────────────────────────
            master_url = getattr(settings, 'MASTER_DASHBOARD_URL', None) or os.environ.get('MASTER_DASHBOARD_URL', 'http://localhost:3000')
            api_secret = getattr(settings, 'MASTER_API_SECRET', None) or os.environ.get('MASTER_API_SECRET', '')
            if master_url:
                try:
                    sync_result = restaurant.sync_to_master_dashboard(master_url, api_secret)
                    if sync_result.get('license_key') and not restaurant.master_license_key:
                        restaurant.master_license_key = sync_result['license_key']
                        restaurant.save(update_fields=['master_license_key'])
                except Exception:
                    pass

            messages.success(request, f"Business '{restaurant.trade_name or restaurant.name}' updated successfully.")
            return redirect('super_admin:dashboard')
    else:
        initial = {
            'legal_name': restaurant.legal_name,
            'trade_name': restaurant.trade_name,
            'business_type': restaurant.business_type,
            'pan': restaurant.pan,
            'cin_llpin': restaurant.cin_llpin,
            'restaurant_address_line_1': restaurant.address_line_1,
            'restaurant_address_line_2': restaurant.address_line_2,
            'restaurant_city': restaurant.city,
            'restaurant_state': restaurant.state,
            'restaurant_pincode': restaurant.pincode,
            'restaurant_email': restaurant.email,
            'restaurant_phone': restaurant.phone,
            'is_gst_registered': restaurant.is_gst_registered,
            'restaurant_is_active': restaurant.is_active and getattr(restaurant, 'feature_website_pages', True),
        }
        form = OnboardRestaurantForm(initial=initial, instance=restaurant)

    existing_branches = []
    for b in restaurant.branches.all():
            b_type = getattr(b, 'outlet_type', 'restaurant_standard') or 'restaurant_standard'
            existing_branches.append({
                'id': b.id,
                'name': b.name,
                'is_setup_completed': getattr(b, 'is_setup_completed', True),
                'is_draft': getattr(b, 'is_draft', False),
                'outlet_type': b_type,
                'branch_type': b_type,
                'branch_addons': getattr(b, 'branch_addons', []) or [],
                'phone': b.phone,
                'use_company_address': b.use_company_address,
                'address_line_1': b.address_line_1,
                'address_line_2': b.address_line_2,
                'city': b.city,
                'state': b.state or restaurant.state,
                'pincode': b.pincode,
                'fssai_license_no': b.fssai_license_no,
                'serves_alcohol': b.serves_alcohol,
                'vat_tin': b.vat_tin or '',
                'excise_license_no': b.excise_license_no or '',
                'vat_beer_rate': float(b.vat_beer_rate) if b.vat_beer_rate is not None else 60.0,
                'vat_imfl_rate': float(b.vat_imfl_rate) if b.vat_imfl_rate is not None else 65.0,
                'vat_wine_rate': float(b.vat_wine_rate) if b.vat_wine_rate is not None else 60.0,
                'gstin': b.gst_registration.gstin if b.gst_registration else b.gstin,
                'gst_registration_type': b.gst_registration_type,
                'is_hotel_tariff_7500_plus': b.is_hotel_tariff_7500_plus,
                'e_invoicing_enabled': b.e_invoicing_enabled,
                'has_club_module': bool(getattr(b, 'has_club_module', False)),
                'is_club_licensed': bool(getattr(b, 'is_club_licensed', False)),
                'has_hotel_module': bool(getattr(b, 'has_hotel_module', False)),
                'is_hotel_licensed': bool(getattr(b, 'is_hotel_licensed', False)),
                'is_spa_licensed': bool(getattr(b, 'is_spa_licensed', False)),
                'is_cabs_licensed': bool(getattr(b, 'is_cabs_licensed', False)),
                'branch_addons': getattr(b, 'branch_addons', []) or [],
                'has_restaurant_module': bool(getattr(b, 'has_restaurant_module', True)),
                'is_restaurant_licensed': bool(getattr(b, 'is_restaurant_licensed', True)),
                'is_alcohol_licensed': bool(getattr(b, 'is_alcohol_licensed', False)),
                'has_hotel_website': bool(getattr(b, 'has_hotel_website', False)),
                'plan_entitlements': getattr(b, 'plan_entitlements', {}) or {},
            })

    is_master_synced = bool(restaurant and restaurant.master_sync_status == 'SYNCED' and restaurant.master_license_key)
    master_plan_name = getattr(restaurant, 'subscription_plan_name', '') or 'Sattkar Cloud Plan'

    return render(request, 'super_admin/onboard.html', {
        'form': form,
        'is_edit': True,
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'all_restaurants': [restaurant],
        'initial_branches_json': json.dumps(existing_branches),
        'features_config_json': json.dumps(features.get_features_js_config()),
        'is_master_synced': is_master_synced,
        'master_plan_name': master_plan_name,
    })


@superuser_required
def restaurant_delete(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    if request.method == 'POST':
        restaurant.delete()
        messages.success(request, 'Restaurant deleted.')
        return redirect('super_admin:dashboard')
    return render(request, 'super_admin/restaurant_confirm_delete.html', {'restaurant': restaurant})


@superuser_required
def restaurant_toggle_active(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    if not restaurant.is_active and not getattr(restaurant, 'feature_website_pages', True):
        messages.error(request, "Cannot activate: The Public Website & CMS module is not included in your current subscription plan. Please upgrade your subscription to enable online website access.")
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'json' in request.headers.get('Accept', ''):
            return JsonResponse({'success': False, 'error': 'The Public Website & CMS module is not included in your current subscription plan.'}, status=400)
        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('super_admin:dashboard')
        return redirect(next_url)

    restaurant.is_active = not restaurant.is_active
    restaurant.save(update_fields=['is_active'])
    status_str = "Active" if restaurant.is_active else "Inactive"
    messages.success(request, f"Business '{restaurant.name}' website status updated to {status_str}.")
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'json' in request.headers.get('Accept', ''):
        return JsonResponse({'success': True, 'is_active': restaurant.is_active, 'status': status_str})
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('super_admin:dashboard')
    return redirect(next_url)



# --- Restaurant Admin CRUD ---
@superuser_required
def restaurant_admin_edit(request, pk):
    ru = get_object_or_404(RestaurantUser.objects.select_related('user'), pk=pk)
    if request.method == 'POST':
        form = RestaurantAdminUpdateForm(request.POST, instance=ru)
        if form.is_valid():
            form.save()
            messages.success(request, 'Restaurant admin updated successfully.')
            return redirect('super_admin:dashboard')
    else:
        form = RestaurantAdminUpdateForm(instance=ru)
    return render(request, 'super_admin/restaurant_admin_update_form.html', {'form': form})


@superuser_required
def restaurant_admin_delete(request, pk):
    ru = RestaurantUser.objects.select_related('user', 'restaurant').get(pk=pk)
    if request.method == 'POST':
        # Decide whether to also delete the User. Here we delete both.
        user = ru.user
        ru.delete()
        user.delete()
        messages.success(request, 'Restaurant admin deleted.')
        return redirect('super_admin:dashboard')
    return render(request, 'super_admin/restaurant_admin_confirm_delete.html', {'ru': ru})


# --- Landing page management ---
@superuser_required
def landing_edit(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    if not constants.SHOW_WEBSITE_PAGES or not constants.is_module_enabled('ENABLE_WEBSITE_CMS', restaurant):
        messages.warning(request, "Website Pages CMS is not included in your current subscription plan. Digital menu ordering is active.")
        return redirect('super_admin:dashboard')
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)

    if request.method == 'POST' and 'save_landing' in request.POST:
        form = RestaurantLandingForm(request.POST, request.FILES, instance=landing)
        if form.is_valid():
            landing_obj = form.save(commit=False)
            if 'clear_logo' in request.POST:
                if landing_obj.logo:
                    landing_obj.logo.delete(save=False)
                landing_obj.logo = None
            if 'clear_favicon' in request.POST:
                if landing_obj.favicon:
                    landing_obj.favicon.delete(save=False)
                landing_obj.favicon = None
            if 'clear_hero_image' in request.POST:
                if landing_obj.hero_image:
                    landing_obj.hero_image.delete(save=False)
                landing_obj.hero_image = None
            if 'clear_hero_video' in request.POST:
                if landing_obj.hero_video:
                    landing_obj.hero_video.delete(save=False)
                landing_obj.hero_video = None
            if 'clear_about_image' in request.POST:
                if landing_obj.about_image:
                    landing_obj.about_image.delete(save=False)
                landing_obj.about_image = None
            if 'clear_events_hero_image' in request.POST:
                if landing_obj.events_hero_image:
                    landing_obj.events_hero_image.delete(save=False)
                landing_obj.events_hero_image = None
            if 'clear_events_hero_video' in request.POST:
                if landing_obj.events_hero_video:
                    landing_obj.events_hero_video.delete(save=False)
                landing_obj.events_hero_video = None

            # Handle events_page_config cards and stats
            cards = []
            for i in range(1, 4):
                c_title = request.POST.get(f'events_card_title_{i}', '').strip()
                c_desc = request.POST.get(f'events_card_desc_{i}', '').strip()
                c_sched = request.POST.get(f'events_card_schedule_{i}', '').strip()
                c_icon = request.POST.get(f'events_card_icon_{i}', 'music').strip()
                if c_title or c_desc:
                    cards.append({
                        'title': c_title,
                        'description': c_desc,
                        'schedule': c_sched,
                        'icon': c_icon
                    })

            stats = []
            for i in range(1, 5):
                s_val = request.POST.get(f'events_stat_val_{i}', '').strip()
                s_label = request.POST.get(f'events_stat_label_{i}', '').strip()
                s_desc = request.POST.get(f'events_stat_desc_{i}', '').strip()
                if s_val or s_label:
                    stats.append({
                        'value': s_val,
                        'label': s_label,
                        'desc': s_desc
                    })

            events_cfg = landing_obj.events_page_config if isinstance(landing_obj.events_page_config, dict) else {}
            events_cfg['cards'] = cards
            events_cfg['stats'] = stats

            # Handle branch-specific events configuration
            branches_cfg = events_cfg.get('branches', {}) if isinstance(events_cfg.get('branches'), dict) else {}
            for b in restaurant.branches.all():
                b_id_str = str(b.id)
                prefix = f'events_branch_{b.id}_'
                b_hero_title = request.POST.get(f'{prefix}hero_title', '').strip()
                b_hero_suptitle = request.POST.get(f'{prefix}hero_suptitle', '').strip()
                b_hero_subtitle = request.POST.get(f'{prefix}hero_subtitle', '').strip()
                b_hero_bg_type = request.POST.get(f'{prefix}hero_bg_type', 'inherit').strip()
                b_hero_video_url = request.POST.get(f'{prefix}hero_video_url', '').strip()
                b_hero_image_url = request.POST.get(f'{prefix}hero_image_url', '').strip()

                # Handle branch video file upload
                b_video_file = request.FILES.get(f'{prefix}hero_video')
                if b_video_file:
                    saved_path = default_storage.save(f'landing/events_videos/branch_{b.id}_{b_video_file.name}', b_video_file)
                    b_hero_video_url = default_storage.url(saved_path)
                elif request.POST.get(f'clear_{prefix}hero_video'):
                    b_hero_video_url = ''

                # Handle branch image file upload
                b_image_file = request.FILES.get(f'{prefix}hero_image')
                if b_image_file:
                    saved_path = default_storage.save(f'landing/events_hero/branch_{b.id}_{b_image_file.name}', b_image_file)
                    b_hero_image_url = default_storage.url(saved_path)
                elif request.POST.get(f'clear_{prefix}hero_image'):
                    b_hero_image_url = ''

                b_about_suptitle = request.POST.get(f'{prefix}about_suptitle', '').strip()
                b_about_title = request.POST.get(f'{prefix}about_title', '').strip()
                b_about_subtitle = request.POST.get(f'{prefix}about_subtitle', '').strip()

                # Branch cards
                b_cards = []
                for i in range(1, 4):
                    bc_title = request.POST.get(f'{prefix}card_title_{i}', '').strip()
                    bc_desc = request.POST.get(f'{prefix}card_desc_{i}', '').strip()
                    bc_sched = request.POST.get(f'{prefix}card_schedule_{i}', '').strip()
                    bc_icon = request.POST.get(f'{prefix}card_icon_{i}', 'music').strip()
                    if bc_title or bc_desc:
                        b_cards.append({
                            'title': bc_title,
                            'description': bc_desc,
                            'schedule': bc_sched,
                            'icon': bc_icon
                        })

                # Branch stats
                b_stats = []
                for i in range(1, 5):
                    bs_val = request.POST.get(f'{prefix}stat_val_{i}', '').strip()
                    bs_label = request.POST.get(f'{prefix}stat_label_{i}', '').strip()
                    bs_desc = request.POST.get(f'{prefix}stat_desc_{i}', '').strip()
                    if bs_val or bs_label:
                        b_stats.append({
                            'value': bs_val,
                            'label': bs_label,
                            'desc': bs_desc
                        })

                existing_b_cfg = branches_cfg.get(b_id_str, {})
                if not b_hero_video_url and not request.POST.get(f'clear_{prefix}hero_video') and existing_b_cfg.get('hero_video_url'):
                    b_hero_video_url = existing_b_cfg.get('hero_video_url')
                if not b_hero_image_url and not request.POST.get(f'clear_{prefix}hero_image') and existing_b_cfg.get('hero_image_url'):
                    b_hero_image_url = existing_b_cfg.get('hero_image_url')

                has_any_override = any([
                    b_hero_title, b_hero_suptitle, b_hero_subtitle,
                    b_hero_video_url, b_hero_image_url,
                    (b_hero_bg_type and b_hero_bg_type != 'inherit'),
                    b_about_suptitle, b_about_title, b_about_subtitle,
                    b_cards, b_stats
                ])

                if has_any_override:
                    branches_cfg[b_id_str] = {
                        'hero_title': b_hero_title,
                        'hero_suptitle': b_hero_suptitle,
                        'hero_subtitle': b_hero_subtitle,
                        'hero_bg_type': b_hero_bg_type,
                        'hero_video_url': b_hero_video_url,
                        'hero_image_url': b_hero_image_url,
                        'about_suptitle': b_about_suptitle,
                        'about_title': b_about_title,
                        'about_subtitle': b_about_subtitle,
                        'cards': b_cards,
                        'stats': b_stats,
                    }
                elif b_id_str in branches_cfg:
                    del branches_cfg[b_id_str]

            events_cfg['branches'] = branches_cfg
            landing_obj.events_page_config = events_cfg
            
            # Handle pages_seo JSON
            pages_seo_raw = request.POST.get('pages_seo')
            if pages_seo_raw:
                try:
                    if isinstance(pages_seo_raw, str):
                        landing_obj.pages_seo = json.loads(pages_seo_raw)
                    elif isinstance(pages_seo_raw, dict):
                        landing_obj.pages_seo = pages_seo_raw
                except Exception as e:
                    print("Error parsing pages_seo JSON:", e)

            # Handle pages_faq JSON
            pages_faq_raw = request.POST.get('pages_faq')
            if pages_faq_raw:
                try:
                    if isinstance(pages_faq_raw, str):
                        landing_obj.pages_faq = json.loads(pages_faq_raw)
                    elif isinstance(pages_faq_raw, dict):
                        landing_obj.pages_faq = pages_faq_raw
                except Exception as e:
                    print("Error parsing pages_faq JSON:", e)

            # Handle theme_config JSON
            theme_config_raw = request.POST.get('theme_config')
            if theme_config_raw:
                try:
                    if isinstance(theme_config_raw, str):
                        landing_obj.theme_config = json.loads(theme_config_raw)
                    elif isinstance(theme_config_raw, dict):
                        landing_obj.theme_config = theme_config_raw
                except Exception as e:
                    print("Error parsing theme_config JSON:", e)

            # Handle logo_shape and brand_display_type
            logo_shape_val = request.POST.get('logo_shape')
            if logo_shape_val in ('round', 'square', 'rectangle'):
                landing_obj.logo_shape = logo_shape_val
            brand_display_val = request.POST.get('brand_display_type')
            if brand_display_val in ('both', 'logo_only', 'name_only'):
                landing_obj.brand_display_type = brand_display_val

            # Keep theme_config in sync with logo_shape and brand_display_type
            if isinstance(landing_obj.theme_config, dict):
                landing_obj.theme_config['logo_shape'] = landing_obj.logo_shape
                landing_obj.theme_config['brand_display_type'] = landing_obj.brand_display_type

            landing_obj.save()
            messages.success(request, 'Landing page saved.')
            return redirect('super_admin:landing_edit', pk=restaurant.pk)
    elif request.method == 'POST' and 'save_branch_contact' in request.POST:
        branch_id = request.POST.get('branch_id')
        target_branch = get_object_or_404(RestaurantBranch, pk=branch_id, restaurant=restaurant)
        branch_form = BranchContactSocialForm(request.POST, instance=target_branch)
        if branch_form.is_valid():
            branch_form.save()
            messages.success(request, f'Contact & Social coordinates updated for {target_branch.name}.')
            return redirect('super_admin:landing_edit', pk=restaurant.pk)
        else:
            messages.error(request, 'Please correct errors in the branch contact details.')
    else:
        form = RestaurantLandingForm(instance=landing)

    branches = restaurant.branches.all()
    gallery_form = GalleryImageForm(restaurant=restaurant)
    images = landing.gallery_images.select_related('branch').all()
    team_form = TeamMemberForm(restaurant=restaurant)
    members = landing.team_members.select_related('branch').all().order_by('display_order', 'id')
    review_form = ReviewForm(restaurant=restaurant)
    reviews = landing.reviews.select_related('branch').all().order_by('display_order', 'id')
    event_form = EventForm(restaurant=restaurant)
    events = landing.events.select_related('branch').all().order_by('display_order', 'id')
    why_us_form = WhyChooseUsForm(restaurant=restaurant)
    why_choose_us_items = landing.why_choose_us_items.select_related('branch').all().order_by('display_order', 'id')
    blog_form = BlogPostForm()
    blog_posts = restaurant.blog_posts.all().order_by('display_order', '-created_at')
    
    pages_seo_dict = landing.pages_seo if isinstance(landing.pages_seo, dict) else {}
    pages_seo_json = json.dumps(pages_seo_dict)

    pages_faq_dict = landing.pages_faq if isinstance(landing.pages_faq, dict) else {}
    pages_faq_json = json.dumps(pages_faq_dict)

    theme_config_dict = landing.theme_config if isinstance(landing.theme_config, dict) else {}
    if 'logo_shape' not in theme_config_dict:
        theme_config_dict['logo_shape'] = landing.logo_shape or 'round'
    if 'brand_display_type' not in theme_config_dict:
        theme_config_dict['brand_display_type'] = landing.brand_display_type or 'both'
    theme_config_json = json.dumps(theme_config_dict)

    job_form = JobPostingForm(restaurant=restaurant)
    job_postings = restaurant.job_postings.select_related('branch').all().order_by('display_order', '-created_at')

    frontend_base_url = Table.get_frontend_base_url()

    events_cfg = landing.events_page_config if isinstance(landing.events_page_config, dict) else {}
    events_cards = list(events_cfg.get('cards', []))
    if not events_cards:
        events_cards = [
            {
                'icon': 'music',
                'title': 'Live Acoustic & Jazz Soirées',
                'description': 'Intimate candlelit evenings curated with handpicked jazz trios, soulful vocalists, and acoustic ensembles paired with reserve cellar flights.',
                'schedule': 'Every Friday & Sunday'
            },
            {
                'icon': 'flame',
                'title': 'VIP Club Lounge & DJ Nights',
                'description': 'Electrifying Saturday late-nights with bespoke bottle service, neon mood installations, and signature house & melodic techno DJ sets.',
                'schedule': 'Saturday Nights • 10 PM'
            },
            {
                'icon': 'wine',
                'title': 'Signature Galas & Masterclasses',
                'description': 'Intimate culinary masterclasses, Michelin-grade truffle dinners, and sommelier guided Grand Cru wine flights.',
                'schedule': 'Featured Monthly Nights'
            }
        ]
    while len(events_cards) < 3:
        events_cards.append({'icon': 'music', 'title': '', 'description': '', 'schedule': ''})

    events_stats = list(events_cfg.get('stats', []))
    if not events_stats:
        events_stats = [
            {'value': '250+', 'label': 'Guest Capacity', 'desc': 'Multi-zone venue space'},
            {'value': '4', 'label': 'Distinct Club Zones', 'desc': 'Terrace, Lounge, Vault, Ballroom'},
            {'value': '100%', 'label': 'VIP Table Service', 'desc': 'Dedicated sommelier & hostess'},
            {'value': '4.9★', 'label': 'Guest Rating', 'desc': 'Over 1,200+ event reviews'}
        ]
    while len(events_stats) < 4:
        events_stats.append({'value': '', 'label': '', 'desc': ''})

    saved_branches_cfg = events_cfg.get('branches', {}) if isinstance(events_cfg, dict) else {}
    branches_events_data = {}
    for b in branches:
        b_id_str = str(b.id)
        b_cfg = saved_branches_cfg.get(b_id_str, {})
        b_cards = list(b_cfg.get('cards', []))
        while len(b_cards) < 3:
            b_cards.append({'icon': 'music', 'title': '', 'description': '', 'schedule': ''})
        b_stats = list(b_cfg.get('stats', []))
        while len(b_stats) < 4:
            b_stats.append({'value': '', 'label': '', 'desc': ''})
        branches_events_data[b.id] = {
            'branch': b,
            'config': b_cfg,
            'cards': b_cards,
            'stats': b_stats,
        }

    return render(request, 'super_admin/landing_edit.html', {
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'branches': branches,
        'landing': landing,
        'form': form,
        'gallery_form': gallery_form,
        'images': images,
        'team_form': team_form,
        'members': members,
        'review_form': review_form,
        'reviews': reviews,
        'event_form': event_form,
        'events': events,
        'why_us_form': why_us_form,
        'why_choose_us_items': why_choose_us_items,
        'blog_form': blog_form,
        'blog_posts': blog_posts,
        'job_form': job_form,
        'job_postings': job_postings,
        'pages_seo_json': pages_seo_json,
        'pages_seo': pages_seo_dict,
        'pages_faq_json': pages_faq_json,
        'pages_faq': pages_faq_dict,
        'theme_config_json': theme_config_json,
        'theme_config': theme_config_dict,
        'frontend_base_url': frontend_base_url,
        'events_cards': events_cards,
        'events_stats': events_stats,
        'branches_events_data': branches_events_data,
    })


@superuser_required
def gallery_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = GalleryImageForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            gi: GalleryImage = form.save(commit=False)
            gi.landing = landing
            gi.save()
            messages.success(request, 'Image added to gallery.')
        else:
            messages.error(request, 'Please correct errors in the image form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def gallery_edit(request, gid):
    img = get_object_or_404(GalleryImage.objects.select_related('landing__restaurant'), pk=gid)
    restaurant = img.landing.restaurant
    if request.method == 'POST':
        form = GalleryImageForm(request.POST, request.FILES, instance=img, restaurant=restaurant)
        if not request.FILES.get('image'):
            form.fields['image'].required = False
        if form.is_valid():
            saved_img = form.save(commit=False)
            if 'image' in request.FILES:
                saved_img.image = request.FILES['image']
            saved_img.save()
            messages.success(request, 'Gallery image updated successfully.')
        else:
            messages.error(request, 'Please correct errors in the gallery image form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def gallery_delete(request, gid):
    img = GalleryImage.objects.select_related('landing__restaurant').get(pk=gid)
    restaurant_id = img.landing.restaurant.pk
    if request.method == 'POST':
        img.delete()
        messages.success(request, 'Image removed.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def team_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = TeamMemberForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            tm: TeamMember = form.save(commit=False)
            tm.landing = landing
            tm.save()
            messages.success(request, 'Team member added.')
        else:
            messages.error(request, 'Please correct errors in the team form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def team_edit(request, mid):
    m = get_object_or_404(TeamMember.objects.select_related('landing__restaurant'), pk=mid)
    restaurant = m.landing.restaurant
    if request.method == 'POST':
        form = TeamMemberForm(request.POST, request.FILES, instance=m, restaurant=restaurant)
        if not request.FILES.get('photo'):
            form.fields['photo'].required = False
        if form.is_valid():
            saved_m = form.save(commit=False)
            if 'photo' in request.FILES:
                saved_m.photo = request.FILES['photo']
            saved_m.save()
            messages.success(request, f'Team member "{m.name}" updated successfully.')
        else:
            messages.error(request, 'Please correct errors in the team form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def team_delete(request, mid):
    m = TeamMember.objects.select_related('landing__restaurant').get(pk=mid)
    restaurant_id = m.landing.restaurant.pk
    if request.method == 'POST':
        m.delete()
        messages.success(request, 'Team member removed.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def review_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = ReviewForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            rv: Review = form.save(commit=False)
            rv.landing = landing
            rv.save()
            messages.success(request, 'Review added.')
        else:
            messages.error(request, 'Please correct errors in the review form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def review_edit(request, rid):
    rv = get_object_or_404(Review.objects.select_related('landing__restaurant'), pk=rid)
    restaurant = rv.landing.restaurant
    if request.method == 'POST':
        form = ReviewForm(request.POST, instance=rv, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, f'Review by "{rv.name}" updated successfully.')
        else:
            messages.error(request, 'Please correct errors in the review form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def review_delete(request, rid):
    rv = Review.objects.select_related('landing__restaurant').get(pk=rid)
    restaurant_id = rv.landing.restaurant.pk
    if request.method == 'POST':
        rv.delete()
        messages.success(request, 'Review removed.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def review_toggle_approve(request, rid):
    rv = get_object_or_404(Review.objects.select_related('landing__restaurant'), pk=rid)
    restaurant_id = rv.landing.restaurant.pk
    if request.method == 'POST':
        rv.active = not rv.active
        rv.save()
        status_label = "published to website testimonials" if rv.active else "hidden / unapproved"
        messages.success(request, f'Review from "{rv.name}" is now {status_label}.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def restaurant_feedback_qr(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    branch_id = request.GET.get('branch_id')
    table_no = request.GET.get('table_no', '').strip()
    download = request.GET.get('download') == '1'

    # Determine frontend URL base dynamically from .env or settings
    frontend_base = Table.get_frontend_base_url()

    url = f"{frontend_base.rstrip('/')}/feedback?restaurant={restaurant.slug}"
    branch_name = "All-Outlets"
    if branch_id:
        branch = restaurant.branches.filter(id=branch_id).first()
        if branch:
            url += f"&branch={branch.slug}"
            branch_name = branch.name.replace(' ', '_')

    if table_no:
        url += f"&table={table_no}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    buffer = io.BytesIO()
    # pyrefly: ignore [unexpected-keyword]
    img.save(buffer, format="PNG")
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    filename = f"feedback_qr_{restaurant.slug}_{branch_name}.png"
    if download:
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
    else:
        response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@superuser_required
def event_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            evt = form.save(commit=False)
            evt.landing = landing
            evt.save()
            messages.success(request, 'Event added successfully.')
        else:
            messages.error(request, 'Please correct errors in the event form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def event_edit(request, ev_id):
    evt = get_object_or_404(Event.objects.select_related('landing__restaurant'), pk=ev_id)
    restaurant = evt.landing.restaurant
    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES, instance=evt, restaurant=restaurant)
        if not request.FILES.get('image'):
            form.fields['image'].required = False
        if form.is_valid():
            saved_evt = form.save(commit=False)
            if 'image' in request.FILES:
                saved_evt.image = request.FILES['image']
            saved_evt.save()
            messages.success(request, f'Event "{evt.title}" updated successfully.')
        else:
            messages.error(request, 'Please correct errors in the event form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def event_delete(request, ev_id):
    evt = Event.objects.select_related('landing__restaurant').get(pk=ev_id)
    restaurant_id = evt.landing.restaurant.pk
    if request.method == 'POST':
        evt.delete()
        messages.success(request, 'Event removed successfully.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def why_us_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = WhyChooseUsForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            item = form.save(commit=False)
            item.landing = landing
            item.save()
            messages.success(request, 'Why Choose Us / Service card added.')
        else:
            messages.error(request, 'Please correct errors in the feature block form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def why_us_edit(request, wid):
    item = get_object_or_404(WhyChooseUs.objects.select_related('landing__restaurant'), pk=wid)
    restaurant = item.landing.restaurant
    if request.method == 'POST':
        form = WhyChooseUsForm(request.POST, instance=item, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, f'Service block "{item.title}" updated successfully.')
        else:
            messages.error(request, 'Please correct errors in the feature block form.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def why_us_delete(request, wid):
    item = WhyChooseUs.objects.select_related('landing__restaurant').get(pk=wid)
    restaurant_id = item.landing.restaurant.pk
    if request.method == 'POST':
        item.delete()
        messages.success(request, 'Service block removed.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def api_gstins_by_state(request):
    restaurant_id = request.GET.get('restaurant_id')
    state_code = request.GET.get('state_code', '').strip().upper()

    qs = GstRegistration.objects.all()
    if restaurant_id:
        qs = qs.filter(restaurant_id=restaurant_id)
    if state_code:
        qs = qs.filter(state_code=state_code)

    gstins = [
        {
            'id': reg.id,
            'gstin': reg.gstin,
            'state_code': reg.state_code,
            'reg_type': reg.reg_type,
            'specified_premises': reg.specified_premises,
            'einvoice_enabled': reg.einvoice_enabled,
        }
        for reg in qs
    ]
    return JsonResponse({'gstins': gstins})


@superuser_required
def api_branches_by_restaurant(request):
    restaurant_id = request.GET.get('restaurant_id')
    branches = []
    restaurant_data = None
    if restaurant_id and str(restaurant_id).isdigit():
        r = Restaurant.objects.filter(id=int(restaurant_id)).first()
        if r:
            restaurant_data = {
                'id': r.id,
                'legal_name': r.legal_name,
                'trade_name': r.trade_name,
                'business_type': r.business_type,
                'pan': r.pan,
                'cin_llpin': r.cin_llpin,
                'address_line_1': r.address_line_1,
                'address_line_2': r.address_line_2,
                'city': r.city,
                'state': r.state,
                'pincode': r.pincode,
                'email': r.email,
                'phone': r.phone,
                'is_gst_registered': r.is_gst_registered,
                'is_active': r.is_active,
            }
            for b in r.branches.all():
                branches.append({
                    'id': b.id,
                    'name': b.name,
                    'outlet_type': getattr(b, 'outlet_type', 'restaurant_standard') or 'restaurant_standard',
                    'branch_type': getattr(b, 'outlet_type', 'restaurant_standard') or 'restaurant_standard',
                    'branch_addons': getattr(b, 'branch_addons', []) or [],
                    'phone': b.phone,
                    'use_company_address': b.use_company_address,
                    'address_line_1': b.address_line_1,
                    'address_line_2': b.address_line_2,
                    'city': b.city,
                    'state': b.state or r.state,
                    'pincode': b.pincode,
                    'fssai_license_no': b.fssai_license_no,
                    'serves_alcohol': b.serves_alcohol,
                    'vat_tin': b.vat_tin or '',
                    'excise_license_no': b.excise_license_no or '',
                    'vat_beer_rate': float(b.vat_beer_rate) if b.vat_beer_rate is not None else 60.0,
                    'vat_imfl_rate': float(b.vat_imfl_rate) if b.vat_imfl_rate is not None else 65.0,
                    'vat_wine_rate': float(b.vat_wine_rate) if b.vat_wine_rate is not None else 60.0,
                    'gstin': b.gst_registration.gstin if b.gst_registration else b.gstin,
                    'gst_registration_type': b.gst_registration_type,
                    'is_hotel_tariff_7500_plus': b.is_hotel_tariff_7500_plus,
                    'e_invoicing_enabled': b.e_invoicing_enabled,
                    'has_club_module': bool(getattr(b, 'has_club_module', False)),
                    'has_hotel_module': bool(getattr(b, 'has_hotel_module', False)),
                    'has_restaurant_module': bool(getattr(b, 'has_restaurant_module', True)),
                    'has_hotel_website': bool(getattr(b, 'has_hotel_website', False)),
                })
    return JsonResponse({'restaurant': restaurant_data, 'branches': branches})


@superuser_required
def api_toggle_branch_club_module(request, pk):
    branch = get_object_or_404(RestaurantBranch, pk=pk)
    if not constants.is_module_enabled('ENABLE_CLUB_EVENTS_MODULE', branch.restaurant):
        return JsonResponse({'success': False, 'error': 'Club & Events module is disabled in system configuration (.env).'}, status=403)
    if request.method == 'POST':
        try:
            body = json.loads(request.body.decode('utf-8'))
            target_state = bool(body['has_club_module']) if 'has_club_module' in body else not branch.has_club_module
        except Exception:
            target_state = not branch.has_club_module

        # Enforce branch-level licensing from Sattkar Master Admin
        if target_state and not branch.is_module_licensed('club'):
            return JsonResponse({
                'success': False,
                'error': f"Club & Events Module is not licensed for '{branch.name}' in your Sattkar subscription plan. Please request an add-on or tier upgrade from Master Admin to activate it."
            }, status=403)

        branch.has_club_module = target_state
        branch.save(update_fields=['has_club_module'])
        return JsonResponse({
            'success': True,
            'branch_id': branch.id,
            'branch_name': branch.name,
            'has_club_module': branch.has_club_module
        })
    return JsonResponse({'error': 'POST method required'}, status=405)


def api_toggle_branch_hotel_module(request, pk):
    branch = get_object_or_404(RestaurantBranch, pk=pk)
    if not constants.is_module_enabled('ENABLE_HOTEL_MODULE', branch.restaurant):
        return JsonResponse({'success': False, 'error': 'Hotel & Rooms PMS module is disabled in system configuration (.env).'}, status=403)
    if request.method == 'POST':
        try:
            body = json.loads(request.body.decode('utf-8'))
            target_state = bool(body['has_hotel_module']) if 'has_hotel_module' in body else not branch.has_hotel_module
        except Exception:
            target_state = not branch.has_hotel_module

        # Enforce branch-level licensing from Sattkar Master Admin
        if target_state and not branch.is_module_licensed('hotel'):
            return JsonResponse({
                'success': False,
                'error': f"Hotel PMS Module is not licensed for '{branch.name}' in your Sattkar subscription plan. Please request an add-on or tier upgrade from Master Admin to activate it."
            }, status=403)

        branch.has_hotel_module = target_state
        branch.save(update_fields=['has_hotel_module'])
        return JsonResponse({
            'success': True,
            'branch_id': branch.id,
            'branch_name': branch.name,
            'has_hotel_module': branch.has_hotel_module
        })
    return JsonResponse({'error': 'POST method required'}, status=405)


def api_toggle_branch_restaurant_module(request, pk):
    branch = get_object_or_404(RestaurantBranch, pk=pk)
    if request.method == 'POST':
        is_allowed = constants.is_module_enabled('ENABLE_RESTAURANT_MODULE', branch.restaurant)
        try:
            body = json.loads(request.body.decode('utf-8'))
            if 'has_restaurant_module' in body:
                target_state = bool(body['has_restaurant_module'])
            else:
                target_state = not branch.has_restaurant_module
        except Exception:
            target_state = not branch.has_restaurant_module

        if target_state and not is_allowed:
            return JsonResponse({
                'success': False,
                'error': 'Restaurant POS Module is not included in your current subscription plan.'
            }, status=403)

        branch.has_restaurant_module = target_state
        branch.save(update_fields=['has_restaurant_module'])
        return JsonResponse({
            'success': True,
            'branch_id': branch.id,
            'branch_name': branch.name,
            'has_restaurant_module': branch.has_restaurant_module
        })
    return JsonResponse({'error': 'POST method required'}, status=405)


def api_toggle_branch_hotel_website(request, pk):
    branch = get_object_or_404(RestaurantBranch, pk=pk)
    if request.method == 'POST':
        is_allowed = constants.is_module_enabled('ENABLE_HOTEL_WEBSITE', branch.restaurant)
        try:
            body = json.loads(request.body.decode('utf-8'))
            if 'has_hotel_website' in body:
                target_state = bool(body['has_hotel_website'])
            else:
                target_state = not branch.has_hotel_website
        except Exception:
            target_state = not branch.has_hotel_website

        if target_state and not is_allowed:
            return JsonResponse({
                'success': False,
                'error': 'Hotel Website Mode is not included in your current subscription plan. Please upgrade your subscription to activate this mode.'
            }, status=403)

        branch.has_hotel_website = target_state
        branch.save(update_fields=['has_hotel_website'])
        return JsonResponse({
            'success': True,
            'branch_id': branch.id,
            'branch_name': branch.name,
            'has_hotel_website': branch.has_hotel_website
        })
    return JsonResponse({'error': 'POST method required'}, status=405)


@superuser_required
def blog_add(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    if request.method == 'POST':
        form = BlogPostForm(request.POST, request.FILES)
        if form.is_valid():
            post: BlogPost = form.save(commit=False)
            post.restaurant = restaurant
            post.save()
            messages.success(request, f'Blog post "{post.title}" created successfully.')
        else:
            messages.error(request, 'Error creating blog post. Please check form errors.')
    return redirect('super_admin:landing_edit', pk=restaurant.pk)


@superuser_required
def blog_edit(request, pk, blog_id):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    blog_post = get_object_or_404(BlogPost, pk=blog_id, restaurant=restaurant)
    if request.method == 'POST':
        form = BlogPostForm(request.POST, request.FILES, instance=blog_post)
        if form.is_valid():
            form.save()
            messages.success(request, f'Blog post "{blog_post.title}" updated successfully.')
            return redirect('super_admin:landing_edit', pk=restaurant.pk)
        else:
            messages.error(request, 'Error updating blog post. Please check form errors.')
    else:
        form = BlogPostForm(instance=blog_post)

    return render(request, 'super_admin/blog_edit.html', {
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'blog_post': blog_post,
        'form': form,
    })


@superuser_required
def blog_delete(request, blog_id):
    blog_post = get_object_or_404(BlogPost, pk=blog_id)
    restaurant_id = blog_post.restaurant.pk
    if request.method == 'POST':
        title = blog_post.title
        blog_post.delete()
        messages.success(request, f'Blog post "{title}" deleted.')
    return redirect('super_admin:landing_edit', pk=restaurant_id)


@superuser_required
def setup_view(request):
    user = request.user

    if request.method == 'POST':
        form = AdminSetupForm(request.POST, instance_user=user, request=request)
        if form.is_valid():
            form.save()
            messages.success(request, "Account settings and 2FA security preferences updated successfully.")
            return redirect('super_admin:setup')
        else:
            messages.error(request, "Please correct the form errors below.")
    else:
        form = AdminSetupForm(instance_user=user, request=request)

    sec_setting = UserSecuritySetting.objects.filter(user=user).first()

    return render(request, 'super_admin/setup.html', {
        'form': form,
        'user_obj': user,
        'sec_setting': sec_setting,
    })






from django.db import models
from super_admin.models import CustomerInquiry
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.http import JsonResponse


@superuser_required
def inquiries_list(request):
    status_filter = request.GET.get('status', 'all')
    restaurant_filter = request.GET.get('restaurant', '')
    query = request.GET.get('q', '').strip()

    qs = CustomerInquiry.objects.select_related('restaurant').all()

    if status_filter == 'unread':
        qs = qs.filter(is_read=False)
    elif status_filter == 'read':
        qs = qs.filter(is_read=True)

    if restaurant_filter:
        qs = qs.filter(restaurant_id=restaurant_filter)

    if query:
        qs = qs.filter(
            models.Q(name__icontains=query) |
            models.Q(email__icontains=query) |
            models.Q(phone__icontains=query) |
            models.Q(subject__icontains=query) |
            models.Q(message__icontains=query)
        )

    unread_count = CustomerInquiry.objects.filter(is_read=False).count()
    total_count = CustomerInquiry.objects.count()
    read_count = max(0, total_count - unread_count)
    restaurants = Restaurant.objects.filter(is_active=True).order_by('name')

    paginator = Paginator(qs, 15)
    page = request.GET.get('page')
    try:
        inquiries = paginator.page(page)
    except PageNotAnInteger:
        inquiries = paginator.page(1)
    except EmptyPage:
        inquiries = paginator.page(paginator.num_pages)

    return render(request, 'super_admin/inquiries_list.html', {
        'inquiries': inquiries,
        'unread_count': unread_count,
        'read_count': read_count,
        'total_count': total_count,
        'status_filter': status_filter,
        'restaurant_filter': restaurant_filter,
        'restaurants': restaurants,
        'query': query,
    })


@superuser_required
def inquiry_toggle_read(request, pk):
    inquiry = get_object_or_404(CustomerInquiry, pk=pk)
    if request.method == 'POST':
        inquiry.is_read = not inquiry.is_read
        inquiry.save(update_fields=['is_read', 'updated_at'])
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'success': True, 'is_read': inquiry.is_read})
        messages.success(request, f'Inquiry #{inquiry.id} marked as {"Read" if inquiry.is_read else "Unread"}.')
    return redirect('super_admin:inquiries')


@superuser_required
def inquiry_delete(request, pk):
    inquiry = get_object_or_404(CustomerInquiry, pk=pk)
    if request.method == 'POST':
        inquiry.delete()
        messages.success(request, f'Inquiry #{pk} deleted successfully.')
    return redirect('super_admin:inquiries')


import csv
from django.http import HttpResponse
from super_admin.models import NewsletterSubscriber


@superuser_required
def subscribers_list(request):
    restaurant_filter = request.GET.get('restaurant', '')
    status_filter = request.GET.get('status', 'all')
    query = request.GET.get('q', '').strip()

    qs = NewsletterSubscriber.objects.select_related('restaurant').all()

    if restaurant_filter:
        qs = qs.filter(restaurant_id=restaurant_filter)

    if status_filter == 'active':
        qs = qs.filter(is_active=True)
    elif status_filter == 'inactive':
        qs = qs.filter(is_active=False)

    if query:
        qs = qs.filter(models.Q(email__icontains=query) | models.Q(source__icontains=query))

    total_count = NewsletterSubscriber.objects.count()
    active_count = NewsletterSubscriber.objects.filter(is_active=True).count()
    inactive_count = max(0, total_count - active_count)
    restaurants = Restaurant.objects.filter(is_active=True).order_by('name')

    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    try:
        subscribers = paginator.page(page)
    except PageNotAnInteger:
        subscribers = paginator.page(1)
    except EmptyPage:
        subscribers = paginator.page(paginator.num_pages)

    return render(request, 'super_admin/subscribers_list.html', {
        'subscribers': subscribers,
        'total_count': total_count,
        'active_count': active_count,
        'inactive_count': inactive_count,
        'restaurant_filter': restaurant_filter,
        'status_filter': status_filter,
        'restaurants': restaurants,
        'query': query,
    })


@superuser_required
def subscribers_export_csv(request):
    restaurant_filter = request.GET.get('restaurant', '')
    status_filter = request.GET.get('status', 'all')
    query = request.GET.get('q', '').strip()

    qs = NewsletterSubscriber.objects.select_related('restaurant').all()

    if restaurant_filter:
        qs = qs.filter(restaurant_id=restaurant_filter)

    if status_filter == 'active':
        qs = qs.filter(is_active=True)
    elif status_filter == 'inactive':
        qs = qs.filter(is_active=False)

    if query:
        qs = qs.filter(models.Q(email__icontains=query) | models.Q(source__icontains=query))

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="newsletter_subscribers.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'Email', 'Business / Restaurant', 'Source', 'Status', 'Subscribed Date'])

    for sub in qs:
        rest_name = sub.restaurant.name if sub.restaurant else 'General'
        status_text = 'Active' if sub.is_active else 'Inactive'
        date_text = sub.subscribed_at.strftime('%Y-%m-%d %H:%M') if sub.subscribed_at else ''
        writer.writerow([sub.id, sub.email, rest_name, sub.source, status_text, date_text])

    return response


@superuser_required
def subscriber_toggle_active(request, pk):
    sub = get_object_or_404(NewsletterSubscriber, pk=pk)
    if request.method == 'POST':
        sub.is_active = not sub.is_active
        sub.save(update_fields=['is_active'])
        messages.success(request, f'Subscriber {sub.email} status changed to {"Active" if sub.is_active else "Inactive"}.')
    return redirect('super_admin:subscribers')


@superuser_required
def subscriber_delete(request, pk):
    sub = get_object_or_404(NewsletterSubscriber, pk=pk)
    if request.method == 'POST':
        email = sub.email
        sub.delete()
        messages.success(request, f'Subscriber {email} removed successfully.')
    return redirect('super_admin:subscribers')


# ── Billing & Subscription Management (MasterPanel Integration) ───────────────

@superuser_required
def billing_view(request):
    """
    Renders the live Subscription & Billing control center.
    Fetches real-time contract details, plan entitlements, renewal countdown,
    available upgrade plans, and invoice records from MasterRestro Master Admin.
    """
    try:
        restaurant = Restaurant.objects.first()
    except Exception:
        restaurant = None

    try:
        billing_data = MasterBillingService.fetch_subscription_details(restaurant, force=True)
    except Exception:
        billing_data = MasterBillingService._build_local_fallback(restaurant)

    client_info = billing_data.get('client') or {}
    subscription = billing_data.get('subscription') or None
    current_plan = billing_data.get('current_plan') or None
    available_plans = billing_data.get('available_plans') or []

    for p in available_plans:
        p_price = p.get('price') if p.get('price') is not None else p.get('monthly_fee')
        if p_price is None:
            p_price = p.get('monthlyPrice') or p.get('amount') or 0
        p['price'] = p_price
        p['monthly_fee'] = p_price
        if 'setup_fee' not in p or p.get('setup_fee') is None:
            p['setup_fee'] = p.get('setupFee') or 0

    connection_status = billing_data.get('connection_status', 'OFFLINE')
    is_connected = bool(connection_status == 'CONNECTED')

    is_trial = bool(subscription.get('is_trial', False) if subscription else False)
    has_assigned_plan = bool(subscription.get('has_assigned_plan', False) or is_trial if subscription else False)
    trial_days_remaining = int(subscription.get('trial_days_remaining', 0) if subscription else 0)
    trial_ends_at = subscription.get('trial_ends_at') if subscription else None

    if has_assigned_plan:
        if current_plan:
            cp_price = current_plan.get('price') if current_plan.get('price') is not None else current_plan.get('monthly_fee')
            if cp_price is None:
                cp_price = current_plan.get('monthlyPrice') or current_plan.get('amount') or 0
            current_plan['price'] = cp_price
            current_plan['monthly_fee'] = cp_price

        if is_trial:
            active_plan_name = (
                (current_plan.get('name') if current_plan else None) or
                (subscription.get('plan_name') if subscription else None) or
                f"{trial_days_remaining}-Day Free Trial"
            )
            if not current_plan:
                current_plan = {
                    'name': active_plan_name,
                    'description': f"Full access free trial ({trial_days_remaining} days remaining). Unrestricted access to all modules.",
                    'max_branches': 5,
                    'monthly_fee': 0,
                    'price': 0,
                    'features': ['Cloud POS, Dine-in & KDS', 'Dynamic Brand Website & Digital Menu', 'Front Desk & Hotel PMS', 'Club & Events Passes', 'Alcohol VAT Compliance'],
                    'is_trial': True,
                }
            else:
                current_plan['name'] = active_plan_name
                current_plan['is_trial'] = True
        else:
            cand_name = (
                (subscription.get('plan_name') if subscription else None) or
                (current_plan.get('name') if current_plan else None) or
                (getattr(restaurant, 'subscription_plan_name', '') if restaurant else '') or
                'Custom Modular Stack'
            )
            if 'trial' in cand_name.lower():
                cand_name = (
                    client_info.get('billing', {}).get('plan_name') or
                    getattr(restaurant, 'subscription_plan_name', '') or
                    'Custom Modular Stack'
                )
                if 'trial' in cand_name.lower():
                    cand_name = 'Custom Modular Stack'
            active_plan_name = cand_name
            if not current_plan:
                current_plan = {
                    'name': active_plan_name,
                    'description': 'Active subscription tier',
                    'max_branches': 3,
                    'monthly_fee': subscription.get('monthly_fee', 0) if subscription else 0,
                    'price': subscription.get('monthly_fee', 0) if subscription else 0,
                    'features': ['Cloud POS', 'Inventory Management', 'Billing & Analytics'],
                }
            elif not current_plan.get('name'):
                current_plan['name'] = active_plan_name
    else:
        active_plan_name = "No Plan Assigned"
        current_plan = None

    if not subscription:
        subscription = {
            'status': getattr(restaurant, 'subscription_status', 'NO_PLAN') if restaurant else 'NO_PLAN',
            'plan_name': active_plan_name,
            'billing_cycle': 'MONTHLY',
            'currency': '₹',
            'monthly_fee': 0,
            'days_remaining': 0,
            'hours_remaining': 0,
            'minutes_remaining': 0,
            'seconds_remaining': 0,
            'total_seconds_remaining': 0,
            'in_grace_period': False,
            'grace_days_remaining': 0,
            'grace_period_expired': False,
            'is_locked': False,
            'has_assigned_plan': False,
            'is_trial': False,
            'trial_days_remaining': 0,
        }

    invoices = billing_data.get('invoices') or []
    master_url = billing_data.get('master_url', 'http://localhost:3000')

    # Calculate outlet quota
    try:
        branch_count = restaurant.branches.count() if restaurant else 0
    except Exception:
        branch_count = 0

    max_branches = restaurant.get_max_allowed_branches() if restaurant else (current_plan.get('max_branches', 3) if current_plan else 3)
    branch_usage_percent = min(100, int((branch_count / max(1, max_branches)) * 100))

    # Calculate 30-day cycle progress
    days_left = subscription.get('days_remaining', 0) if subscription else 0
    if days_left is None:
        days_left = 0
    cycle_days = 30
    days_passed = max(0, cycle_days - days_left) if has_assigned_plan else 0
    cycle_progress_percent = min(100, int((days_passed / cycle_days) * 100)) if has_assigned_plan else 0

    context = {
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'billing_data': billing_data,
        'client_info': client_info,
        'subscription': subscription,
        'current_plan': current_plan,
        'active_plan_name': active_plan_name,
        'has_assigned_plan': has_assigned_plan,
        'available_plans': available_plans,
        'invoices': invoices,
        'connection_status': connection_status,
        'is_connected': is_connected,
        'master_url': master_url,
        'branch_count': branch_count,
        'max_branches': max_branches,
        'branch_usage_percent': branch_usage_percent,
        'cycle_progress_percent': cycle_progress_percent,
        'total_seconds_remaining': subscription.get('total_seconds_remaining', 0) if subscription else 0,
        'next_billing_date': subscription.get('next_billing_date', '') if subscription else '',
        'formatted_next_billing': subscription.get('formatted_next_billing', '-') if subscription else '-',
        'pending_payment': billing_data.get('pending_payment') or None,
        'pending_addons': billing_data.get('pending_addons') or [],
        'is_trial': is_trial,
        'trial_days_remaining': trial_days_remaining,
        'trial_ends_at': trial_ends_at or '',
    }
    return render(request, 'super_admin/billing.html', context)


@superuser_required
def api_billing_sync(request):
    """
    Live AJAX refresh endpoint for instant billing telemetry and countdown sync.
    """
    try:
        restaurant = Restaurant.objects.first()
    except Exception:
        restaurant = None

    if restaurant:
        try:
            master_url = getattr(settings, 'MASTER_DASHBOARD_URL', None) or os.environ.get('MASTER_DASHBOARD_URL', 'http://localhost:3000')
            api_secret = getattr(settings, 'MASTER_API_SECRET', None) or os.environ.get('MASTER_API_SECRET', '')
            if master_url:
                restaurant.sync_to_master_dashboard(master_url, api_secret)
        except Exception:
            pass

    try:
        billing_data = MasterBillingService.fetch_subscription_details(restaurant, force=True)
    except Exception:
        billing_data = MasterBillingService._build_local_fallback(restaurant)

    return JsonResponse(billing_data)


@superuser_required
def api_request_plan_upgrade(request):
    """
    Submits a plan change / tier upgrade or renewal request directly to Master Admin,
    or triggers real-time Free Trial activation / Reset.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST method required'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST.dict()

    action = body.get('action') or 'UPGRADE_OR_RENEWAL_REQUEST'
    if action == 'RESET_PLAN':
        return JsonResponse({'status': 'error', 'message': 'Plan reset is only available from Sattkar Master Admin.'}, status=403)
    # Trial length is decided by Master Admin; whatever the browser sends is ignored there.
    trial_days = 14
    plan_name = body.get('plan_name') or body.get('requested_plan_name', 'Professional Tier')
    billing_cycle = body.get('billing_cycle') or body.get('requested_billing_cycle', 'MONTHLY')
    notes = body.get('notes', '')
    contact_phone = body.get('contact_phone', '')
    contact_email = body.get('contact_email', '')

    try:
        restaurant = Restaurant.objects.first()
    except Exception:
        restaurant = None

    stack_configuration = body.get('stack_configuration') or body.get('configured_stack')

    result = MasterBillingService.submit_upgrade_request(
        restaurant=restaurant,
        requested_plan_name=plan_name,
        requested_billing_cycle=billing_cycle,
        notes=notes,
        contact_phone=contact_phone,
        contact_email=contact_email,
        stack_configuration=stack_configuration,
        action=action,
        trial_days=trial_days,
    )
    return JsonResponse(result)


@superuser_required
def api_billing_end_trial(request):
    """
    Handles self-serve 'End Trial & Activate Subscription' request from Super Admin.
    Transitions account to regular billing and returns the first Razorpay invoice link.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    restaurant = Restaurant.objects.first()
    res = MasterBillingService.end_trial(restaurant=restaurant)
    return JsonResponse(res)


@superuser_required
def api_billing_ive_paid(request):
    """
    Handles 'I've paid' confirmation button from billing dashboard.
    Verifies the pending payment link / invoice with Master Admin & Razorpay.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST.dict()

    invoice_no = body.get('invoice_no')
    invoice_id = body.get('invoice_id')
    payment_link_id = body.get('payment_link_id')

    restaurant = Restaurant.objects.first()
    res = MasterBillingService.check_invoice_status(
        restaurant=restaurant,
        invoice_no=invoice_no,
        invoice_id=invoice_id,
        payment_link_id=payment_link_id,
    )
    return JsonResponse(res)


@superuser_required
def api_billing_addons_catalog(request):
    """
    Fetches available add-ons catalog, outlets, and orders for self-serve store.
    """
    restaurant = Restaurant.objects.first()
    data = MasterBillingService.fetch_addons(restaurant=restaurant)
    return JsonResponse(data)


@superuser_required
def api_billing_addon_quote(request):
    """
    Live quotation for selected add-on modules and new outlets.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST.dict()

    items = body.get('items', [])
    new_outlets = body.get('new_outlets', [])

    restaurant = Restaurant.objects.first()
    quote = MasterBillingService.quote_addons(restaurant=restaurant, items=items, new_outlets=new_outlets)
    return JsonResponse(quote)


@superuser_required
def api_billing_addon_order(request):
    """
    Creates an add-on order and generates Razorpay payment link.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST.dict()

    items = body.get('items', [])
    new_outlets = body.get('new_outlets', [])
    notes = body.get('notes', '')
    req_name = request.user.get_full_name() or request.user.username or 'Admin'

    restaurant = Restaurant.objects.first()
    result = MasterBillingService.order_addons(
        restaurant=restaurant,
        items=items,
        new_outlets=new_outlets,
        notes=notes,
        requested_by_name=req_name,
    )
    return JsonResponse(result)


@superuser_required
def api_billing_addon_cancel(request):
    """
    Cancels an unpaid add-on order.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST.dict()

    order_id = body.get('order_id')
    if not order_id:
        return JsonResponse({'status': 'error', 'message': 'order_id required'}, status=400)

    restaurant = Restaurant.objects.first()
    result = MasterBillingService.cancel_addon_order(restaurant=restaurant, order_id=order_id)
    return JsonResponse(result)


@superuser_required
def billing_invoice_view(request, invoice_id):
    """
    Renders a dedicated, printable SaaS invoice receipt.
    """
    try:
        restaurant = Restaurant.objects.first()
    except Exception:
        restaurant = None

    try:
        billing_data = MasterBillingService.fetch_subscription_details(restaurant)
    except Exception:
        billing_data = MasterBillingService._build_local_fallback(restaurant)

    invoices = billing_data.get('invoices', [])
    invoice = next((inv for inv in invoices if inv.get('id') == invoice_id), None)

    if not invoice:
        messages.warning(request, f"Invoice {invoice_id} not found or not synced from Master Admin.")
        return redirect('super_admin:billing')

    return render(request, 'super_admin/invoice_receipt.html', {
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'invoice': invoice,
        'client_info': billing_data.get('client', {}),
        'subscription': billing_data.get('subscription', {}),
    })


# ── Super Analytics (Multi-Branch Intelligence & Control Center) ─────────────

@superuser_required
def super_analytics(request):
    """
    Consolidated Multi-Branch Analytics & Sales Control Center for Super Admin.
    Provides bird's-eye enterprise metrics, branch comparative leaderboards,
    channel splits, hourly rush analysis, and staff operational activity.
    """
    restaurant = Restaurant.objects.first()
    if not restaurant:
        messages.warning(request, "Please onboard a restaurant business first.")
        return redirect('super_admin:dashboard')

    branches = restaurant.branches.all().order_by('name')

    # ── 1. Filters (Branch & Date Range) ─────────────────────────────────────
    branch_param = request.GET.get('branch_id', 'all')
    selected_branch = None
    if branch_param and branch_param != 'all':
        try:
            selected_branch = branches.filter(id=int(branch_param)).first()
        except (ValueError, TypeError):
            selected_branch = None

    range_preset = request.GET.get('range', 'this_month')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')

    now = timezone.now()
    today = now.date()

    if range_preset == 'today':
        start_date = today
        end_date = today
        range_label = "Today"
    elif range_preset == 'yesterday':
        start_date = today - timedelta(days=1)
        end_date = start_date
        range_label = "Yesterday"
    elif range_preset == 'last_7_days':
        start_date = today - timedelta(days=6)
        end_date = today
        range_label = "Last 7 Days"
    elif range_preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
        range_label = "This Month"
    elif range_preset == 'this_year':
        start_date = today.replace(month=1, day=1)
        end_date = today
        range_label = "This Year"
    elif range_preset == 'custom' and start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            range_label = f"{start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')}"
        except ValueError:
            start_date = today.replace(day=1)
            end_date = today
            range_label = "This Month"
    else:
        range_preset = 'all_time'
        start_date = None
        end_date = None
        range_label = "All Time"

    # ── 2. Query Orders ──────────────────────────────────────────────────────
    base_orders = Order.objects.filter(restaurant=restaurant).exclude(status__in=['C', Order.OrderStatus.DRAFT])

    if start_date and end_date:
        filtered_orders = base_orders.filter(order_time__date__range=[start_date, end_date])
    else:
        filtered_orders = base_orders

    # Scope orders (All branches or Single branch)
    if selected_branch:
        scope_orders = filtered_orders.filter(branch=selected_branch)
    else:
        scope_orders = filtered_orders

    # ── 3. Executive Multi-Branch Aggregations ────────────────────────────────
    total_orders = scope_orders.count()
    total_guests = scope_orders.aggregate(t=Sum('guest_count'))['t'] or 0

    dine_in_orders = scope_orders.filter(order_type='dine_in')
    takeaway_orders = scope_orders.filter(order_type='takeaway')
    delivery_orders = scope_orders.filter(order_type='delivery')

    dine_in_count = dine_in_orders.count()
    takeaway_count = takeaway_orders.count()
    delivery_count = delivery_orders.count()

    total_net_revenue = Decimal('0.00')
    total_gross_revenue = Decimal('0.00')
    total_taxes = Decimal('0.00')
    total_discounts = Decimal('0.00')

    dine_in_revenue = Decimal('0.00')
    takeaway_revenue = Decimal('0.00')
    delivery_revenue = Decimal('0.00')

    payment_breakdown = {
        'cash': Decimal('0.00'),
        'upi': Decimal('0.00'),
        'card': Decimal('0.00'),
        'credit': Decimal('0.00'),
        'other': Decimal('0.00')
    }
    hourly_orders = [0] * 24
    hourly_revenue = [0.0] * 24
    date_sales_map = {}

    for ord_obj in scope_orders.select_related('branch'):
        try:
            breakdown = ord_obj.calculate_bill_breakdown()
            final_total = Decimal(str(breakdown.get('final_total', ord_obj.total_amount or 0.0)))
            subtotal = Decimal(str(breakdown.get('subtotal', ord_obj.subtotal or 0.0)))
            taxes = Decimal(str(breakdown.get('total_tax', ord_obj.tax_amount or 0.0)))
            discount = Decimal(str(breakdown.get('discount_amount', ord_obj.discount_amount or 0.0)))
        except Exception:
            final_total = ord_obj.total_amount or Decimal('0.00')
            subtotal = ord_obj.subtotal or Decimal('0.00')
            taxes = ord_obj.tax_amount or Decimal('0.00')
            discount = ord_obj.discount_amount or Decimal('0.00')

        total_net_revenue += final_total
        total_gross_revenue += (subtotal + taxes)
        total_taxes += taxes
        total_discounts += discount

        if ord_obj.order_type == 'dine_in':
            dine_in_revenue += final_total
        elif ord_obj.order_type == 'takeaway':
            takeaway_revenue += final_total
        elif ord_obj.order_type == 'delivery':
            delivery_revenue += final_total

        pm = (ord_obj.payment_method or 'cash').lower()
        if pm in payment_breakdown:
            payment_breakdown[pm] += final_total
        else:
            payment_breakdown['other'] += final_total

        if ord_obj.order_time:
            local_time = timezone.localtime(ord_obj.order_time)
            hr = local_time.hour
            hourly_orders[hr] += 1
            hourly_revenue[hr] += float(final_total)

            d_str = local_time.strftime('%Y-%m-%d')
            b_id = ord_obj.branch_id or 0
            if d_str not in date_sales_map:
                date_sales_map[d_str] = {'total': 0.0, 'branches': {}}
            date_sales_map[d_str]['total'] += float(final_total)
            date_sales_map[d_str]['branches'][b_id] = date_sales_map[d_str]['branches'].get(b_id, 0.0) + float(final_total)

    aov = (total_net_revenue / total_orders) if total_orders > 0 else Decimal('0.00')
    spend_per_guest = (total_net_revenue / total_guests) if total_guests > 0 else Decimal('0.00')

    max_hr_orders = max(hourly_orders) if hourly_orders else 0
    if max_hr_orders > 0:
        peak_hr_idx = hourly_orders.index(max_hr_orders)
        peak_rush_label = f"{peak_hr_idx % 12 or 12} {'AM' if peak_hr_idx < 12 else 'PM'} - {(peak_hr_idx + 1) % 12 or 12} {'AM' if (peak_hr_idx + 1) < 12 else 'PM'} ({max_hr_orders} orders)"
    else:
        peak_rush_label = "No Rush Data Yet"

    # ── 4. Multi-Branch Comparative Matrix & Leaderboard ─────────────────────
    branch_performance_list = []

    for b in branches:
        b_orders = filtered_orders.filter(branch=b)
        b_count = b_orders.count()
        b_guests = b_orders.aggregate(t=Sum('guest_count'))['t'] or 0
        b_revenue = Decimal('0.00')
        b_taxes = Decimal('0.00')
        b_dine_in = b_orders.filter(order_type='dine_in').count()
        b_takeaway = b_orders.filter(order_type='takeaway').count()
        b_delivery = b_orders.filter(order_type='delivery').count()

        for bo in b_orders:
            try:
                bk = bo.calculate_bill_breakdown()
                b_revenue += Decimal(str(bk.get('final_total', bo.total_amount or 0.0)))
                b_taxes += Decimal(str(bk.get('total_tax', bo.tax_amount or 0.0)))
            except Exception:
                b_revenue += bo.total_amount or Decimal('0.00')
                b_taxes += bo.tax_amount or Decimal('0.00')

        b_aov = (b_revenue / b_count) if b_count > 0 else Decimal('0.00')
        b_admins = RestaurantUser.objects.filter(restaurant=restaurant, branch=b).select_related('user')

        top_item_obj = OrderItem.objects.filter(order__in=b_orders).values('menu_item__name').annotate(
            qty=Sum('quantity')
        ).order_by('-qty').first()
        top_item_name = top_item_obj['menu_item__name'] if top_item_obj and top_item_obj['menu_item__name'] else "No Orders Yet"

        branch_performance_list.append({
            'branch': b,
            'orders_count': b_count,
            'guests_count': b_guests,
            'revenue': b_revenue,
            'taxes': b_taxes,
            'aov': b_aov,
            'dine_in_count': b_dine_in,
            'takeaway_count': b_takeaway,
            'delivery_count': b_delivery,
            'admins': b_admins,
            'top_item': top_item_name,
            'is_selected': selected_branch and selected_branch.id == b.id,
        })

    # Sort leaderboard by revenue descending
    branch_performance_list.sort(key=lambda x: x['revenue'], reverse=True)

    global_total_rev = sum(bp['revenue'] for bp in branch_performance_list)
    for idx, bp in enumerate(branch_performance_list):
        bp['rank'] = idx + 1
        if global_total_rev > 0:
            bp['share_percent'] = round(float((bp['revenue'] / global_total_rev) * 100), 1)
        else:
            bp['share_percent'] = 0.0

    # ── 5. Top 10 Selling Menu Items Across Outlets ──────────────────────────
    top_items_qs = OrderItem.objects.filter(order__in=scope_orders).values(
        item_name=F('menu_item__name')
    ).annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('unit_price'))
    ).order_by('-total_qty')[:10]

    # ── 6. Branch Admins & Staff Activity ────────────────────────────────────
    all_admins = RestaurantUser.objects.filter(restaurant=restaurant).select_related('user', 'branch')

    # ── 7. Chart Data JSON for Chart.js ──────────────────────────────────────
    sorted_dates = sorted(date_sales_map.keys())
    if not sorted_dates and start_date and end_date:
        curr = start_date
        while curr <= end_date:
            sorted_dates.append(curr.strftime('%Y-%m-%d'))
            curr += timedelta(days=1)

    chart_labels = [datetime.strptime(d, '%Y-%m-%d').strftime('%d %b') if len(sorted_dates) > 1 else d for d in sorted_dates]
    branch_colors = ['#f26522', '#3b82f6', '#10b981', '#8b5cf6', '#ec4899', '#f59e0b', '#06b6d4']

    trend_datasets = []
    if not selected_branch:
        for b_idx, b in enumerate(branches):
            color = branch_colors[b_idx % len(branch_colors)]
            data_points = []
            for d in sorted_dates:
                val = date_sales_map.get(d, {}).get('branches', {}).get(b.id, 0.0)
                data_points.append(val)
            trend_datasets.append({
                'label': b.name,
                'data': data_points,
                'borderColor': color,
                'backgroundColor': color + '22',
                'borderWidth': 2.5,
                'tension': 0.3,
                'fill': False,
            })
    else:
        color = '#f26522'
        data_points = [date_sales_map.get(d, {}).get('total', 0.0) for d in sorted_dates]
        trend_datasets.append({
            'label': selected_branch.name,
            'data': data_points,
            'borderColor': color,
            'backgroundColor': color + '33',
            'borderWidth': 3,
            'tension': 0.3,
            'fill': True,
        })

    donut_labels = [bp['branch'].name for bp in branch_performance_list]
    donut_data = [float(bp['revenue']) for bp in branch_performance_list]
    donut_colors = [branch_colors[i % len(branch_colors)] for i in range(len(branch_performance_list))]

    hourly_labels = [f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}" for h in range(24)]

    payment_labels = ['UPI', 'Cash', 'Card', 'Credit / Other']
    payment_data = [
        float(payment_breakdown['upi']),
        float(payment_breakdown['cash']),
        float(payment_breakdown['card']),
        float(payment_breakdown['credit'] + payment_breakdown['other']),
    ]

    context = {
        'restaurant': restaurant,
        'branches': restaurant.branches.all() if restaurant else [],
        'branches': branches,
        'selected_branch': selected_branch,
        'branch_param': branch_param,
        'range_preset': range_preset,
        'range_label': range_label,
        'start_date_str': start_date_str or (start_date.strftime('%Y-%m-%d') if start_date else ''),
        'end_date_str': end_date_str or (end_date.strftime('%Y-%m-%d') if end_date else ''),

        # 8 Core KPIs
        'total_orders': total_orders,
        'total_guests': total_guests,
        'total_net_revenue': total_net_revenue,
        'total_gross_revenue': total_gross_revenue,
        'total_taxes': total_taxes,
        'total_discounts': total_discounts,
        'aov': aov,
        'spend_per_guest': spend_per_guest,
        'peak_rush_label': peak_rush_label,

        # Channels
        'dine_in_count': dine_in_count,
        'takeaway_count': takeaway_count,
        'delivery_count': delivery_count,
        'dine_in_revenue': dine_in_revenue,
        'takeaway_revenue': takeaway_revenue,
        'delivery_revenue': delivery_revenue,

        # Performance & Rosters
        'branch_performance_list': branch_performance_list,
        'top_items': top_items_qs,
        'all_admins': all_admins,

        # Chart Data JSON
        'chart_labels_json': json.dumps(chart_labels),
        'trend_datasets_json': json.dumps(trend_datasets),
        'donut_labels_json': json.dumps(donut_labels),
        'donut_data_json': json.dumps(donut_data),
        'donut_colors_json': json.dumps(donut_colors),
        'hourly_labels_json': json.dumps(hourly_labels),
        'hourly_orders_json': json.dumps(hourly_orders),
        'hourly_revenue_json': json.dumps(hourly_revenue),
        'payment_labels_json': json.dumps(payment_labels),
        'payment_data_json': json.dumps(payment_data),
    }
    return render(request, 'super_admin/analytics.html', context)


@superuser_required
def super_analytics_export_csv(request):
    """
    Exports multi-branch sales, revenue, tax, and order channel data to a downloadable CSV.
    """
    restaurant = Restaurant.objects.first()
    if not restaurant:
        return redirect('super_admin:dashboard')

    branches = restaurant.branches.all().order_by('name')
    range_preset = request.GET.get('range', 'this_month')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')

    now = timezone.now()
    today = now.date()

    if range_preset == 'today':
        start_date = today
        end_date = today
    elif range_preset == 'yesterday':
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif range_preset == 'last_7_days':
        start_date = today - timedelta(days=6)
        end_date = today
    elif range_preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
    elif range_preset == 'this_year':
        start_date = today.replace(month=1, day=1)
        end_date = today
    elif range_preset == 'custom' and start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_date = today.replace(day=1)
            end_date = today
    else:
        start_date = None
        end_date = None

    response = HttpResponse(content_type='text/csv')
    date_tag = f"{start_date}_{end_date}" if start_date and end_date else "all_time"
    response['Content-Disposition'] = f'attachment; filename="super_analytics_report_{date_tag}.csv"'

    writer = csv.writer(response)
    writer.writerow(['SUPER ADMIN ENTERPRISE SALES & REVENUE ANALYTICS REPORT'])
    writer.writerow(['Restaurant Business', restaurant.trade_name or restaurant.name])
    writer.writerow(['Date Range', f"{start_date or 'Beginning'} to {end_date or 'Present'}"])
    writer.writerow([])
    writer.writerow(['Rank', 'Branch Name', 'Location / City', 'Assigned Admins', 'Total Orders', 'Guests (Covers)', 'Net Revenue (INR)', 'Taxes (INR)', 'AOV (INR)', 'Dine-In Orders', 'Takeaway Orders', 'Delivery Orders', 'Revenue Share (%)'])

    base_orders = Order.objects.filter(restaurant=restaurant).exclude(status__in=['C', Order.OrderStatus.DRAFT])
    if start_date and end_date:
        filtered_orders = base_orders.filter(order_time__date__range=[start_date, end_date])
    else:
        filtered_orders = base_orders

    branch_data = []
    for b in branches:
        b_orders = filtered_orders.filter(branch=b)
        b_count = b_orders.count()
        b_guests = b_orders.aggregate(t=Sum('guest_count'))['t'] or 0
        b_rev = Decimal('0.00')
        b_tax = Decimal('0.00')
        for bo in b_orders:
            try:
                bk = bo.calculate_bill_breakdown()
                b_rev += Decimal(str(bk.get('final_total', bo.total_amount or 0.0)))
                b_tax += Decimal(str(bk.get('total_tax', bo.tax_amount or 0.0)))
            except Exception:
                b_rev += bo.total_amount or Decimal('0.00')
                b_tax += bo.tax_amount or Decimal('0.00')

        b_aov = (b_rev / b_count) if b_count > 0 else Decimal('0.00')
        b_dine = b_orders.filter(order_type='dine_in').count()
        b_takeaway = b_orders.filter(order_type='takeaway').count()
        b_delivery = b_orders.filter(order_type='delivery').count()
        admins_str = ", ".join([u.user.username for u in RestaurantUser.objects.filter(branch=b).select_related('user')])

        branch_data.append({
            'name': b.name,
            'city': b.city or restaurant.city or '',
            'admins': admins_str or 'Unassigned',
            'orders': b_count,
            'guests': b_guests,
            'revenue': b_rev,
            'taxes': b_tax,
            'aov': b_aov,
            'dine_in': b_dine,
            'takeaway': b_takeaway,
            'delivery': b_delivery,
        })

    branch_data.sort(key=lambda x: x['revenue'], reverse=True)
    tot_rev = sum(bd['revenue'] for bd in branch_data) or Decimal('1.00')

    for idx, bd in enumerate(branch_data):
        share = round(float((bd['revenue'] / tot_rev) * 100), 1) if tot_rev > 0 else 0.0
        writer.writerow([
            idx + 1,
            bd['name'],
            bd['city'],
            bd['admins'],
            bd['orders'],
            bd['guests'],
            f"{bd['revenue']:.2f}",
            f"{bd['taxes']:.2f}",
            f"{bd['aov']:.2f}",
            bd['dine_in'],
            bd['takeaway'],
            bd['delivery'],
            f"{share}%"
        ])

    return response


# -------------------------------------------------------------------
# Careers & Job Postings Views
# -------------------------------------------------------------------

@superuser_required
def careers_list(request):
    restaurant_filter = request.GET.get('restaurant', '')
    branch_filter = request.GET.get('branch', '')
    department_filter = request.GET.get('department', '')
    status_filter = request.GET.get('status', 'all')
    query = request.GET.get('q', '').strip()

    qs = JobPosting.objects.select_related('restaurant', 'branch').all()

    if restaurant_filter and restaurant_filter.isdigit():
        qs = qs.filter(restaurant_id=int(restaurant_filter))

    if branch_filter and branch_filter.isdigit():
        qs = qs.filter(branch_id=int(branch_filter))

    if department_filter:
        qs = qs.filter(department__iexact=department_filter)

    if status_filter == 'active':
        qs = qs.filter(is_active=True)
    elif status_filter == 'inactive':
        qs = qs.filter(is_active=False)

    if query:
        qs = qs.filter(
            Q(title__icontains=query) |
            Q(department__icontains=query) |
            Q(location__icontains=query) |
            Q(roles_responsibilities__icontains=query) |
            Q(requirements__icontains=query) |
            Q(restaurant__name__icontains=query)
        )

    total_jobs = JobPosting.objects.count()
    active_jobs = JobPosting.objects.filter(is_active=True).count()
    closed_jobs = max(0, total_jobs - active_jobs)
    total_applications = JobApplication.objects.count()
    pending_applications = JobApplication.objects.filter(status='PENDING').count()

    restaurants = Restaurant.objects.filter(is_active=True).order_by('name')

    filter_branches = RestaurantBranch.objects.none()
    if restaurant_filter and restaurant_filter.isdigit():
        filter_branches = RestaurantBranch.objects.filter(restaurant_id=int(restaurant_filter)).order_by('name')

    departments = (
        JobPosting.objects.values_list('department', flat=True)
        .distinct()
        .order_by('department')
    )

    paginator = Paginator(qs.order_by('display_order', '-created_at'), 12)
    page = request.GET.get('page')
    try:
        jobs = paginator.page(page)
    except PageNotAnInteger:
        jobs = paginator.page(1)
    except EmptyPage:
        jobs = paginator.page(paginator.num_pages)

    form = JobPostingForm()

    return render(request, 'super_admin/careers_list.html', {
        'jobs': jobs,
        'form': form,
        'total_jobs': total_jobs,
        'active_jobs': active_jobs,
        'closed_jobs': closed_jobs,
        'total_applications': total_applications,
        'pending_applications': pending_applications,
        'restaurants': restaurants,
        'filter_branches': filter_branches,
        'departments': departments,
        'restaurant_filter': restaurant_filter,
        'branch_filter': branch_filter,
        'department_filter': department_filter,
        'status_filter': status_filter,
        'query': query,
    })


@superuser_required
def job_posting_create(request):
    if request.method == 'POST':
        restaurant_id = request.POST.get('restaurant')
        restaurant = get_object_or_404(Restaurant, pk=restaurant_id) if restaurant_id else None
        form = JobPostingForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            job = form.save()
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'success': True, 'id': job.id, 'title': job.title})
            messages.success(request, f'Job posting "{job.title}" created successfully!')
            return redirect('super_admin:careers_list')
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'success': False, 'errors': form.errors}, status=400)
            messages.error(request, 'Please check the form for errors.')
    return redirect('super_admin:careers_list')


@superuser_required
def job_posting_edit(request, pk):
    job = get_object_or_404(JobPosting, pk=pk)
    if request.method == 'GET' and (request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json'):
        return JsonResponse({
            'success': True,
            'id': job.id,
            'restaurant_id': job.restaurant_id,
            'branch_id': job.branch_id or '',
            'title': job.title,
            'slug': job.slug,
            'department': job.department,
            'job_type': job.job_type,
            'experience_level': job.experience_level,
            'location': job.location,
            'eligibility': job.eligibility,
            'salary_range': job.salary_range,
            'openings_count': job.openings_count,
            'roles_responsibilities': job.roles_responsibilities,
            'requirements': job.requirements,
            'is_active': job.is_active,
            'display_order': job.display_order,
        })

    if request.method == 'POST':
        restaurant_id = request.POST.get('restaurant') or job.restaurant_id
        restaurant = Restaurant.objects.filter(pk=restaurant_id).first() or job.restaurant
        form = JobPostingForm(request.POST, instance=job, restaurant=restaurant)
        if form.is_valid():
            form.save()
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'success': True, 'id': job.id, 'title': job.title})
            messages.success(request, f'Job posting "{job.title}" updated successfully.')
            return redirect('super_admin:careers_list')
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'success': False, 'errors': form.errors}, status=400)
            messages.error(request, 'Please correct the errors in the form.')
    return redirect('super_admin:careers_list')


@superuser_required
def job_posting_delete(request, pk):
    job = get_object_or_404(JobPosting, pk=pk)
    if request.method == 'POST':
        title = job.title
        job.delete()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'success': True})
        messages.success(request, f'Job posting "{title}" deleted.')
    return redirect('super_admin:careers_list')


@superuser_required
def job_posting_toggle_active(request, pk):
    job = get_object_or_404(JobPosting, pk=pk)
    if request.method == 'POST':
        job.is_active = not job.is_active
        job.save(update_fields=['is_active', 'updated_at'])
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'success': True, 'is_active': job.is_active})
        messages.success(request, f'Job posting "{job.title}" is now {"Active" if job.is_active else "Closed"}.')
    return redirect('super_admin:careers_list')


# -------------------------------------------------------------------
# Job Applications / Enquiries Views
# -------------------------------------------------------------------

@superuser_required
def job_applications_list(request):
    job_filter = request.GET.get('job', '')
    restaurant_filter = request.GET.get('restaurant', '')
    branch_filter = request.GET.get('branch', '')
    status_filter = request.GET.get('status', 'all')
    experience_filter = request.GET.get('experience', 'all')
    query = request.GET.get('q', '').strip()

    qs = JobApplication.objects.select_related('job', 'restaurant', 'branch').all()

    if job_filter and job_filter.isdigit():
        qs = qs.filter(job_id=int(job_filter))

    if restaurant_filter and restaurant_filter.isdigit():
        qs = qs.filter(restaurant_id=int(restaurant_filter))

    if branch_filter and branch_filter.isdigit():
        qs = qs.filter(branch_id=int(branch_filter))

    if status_filter != 'all' and status_filter:
        qs = qs.filter(status=status_filter.upper())

    if experience_filter == 'yes':
        qs = qs.filter(has_experience=True)
    elif experience_filter == 'no':
        qs = qs.filter(has_experience=False)

    if query:
        qs = qs.filter(
            Q(full_name__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query) |
            Q(current_organization__icontains=query) |
            Q(reason_to_join__icontains=query) |
            Q(job__title__icontains=query)
        )

    total_count = JobApplication.objects.count()
    pending_count = JobApplication.objects.filter(status='PENDING').count()
    shortlisted_count = JobApplication.objects.filter(status__in=['SHORTLISTED', 'INTERVIEWING']).count()
    hired_count = JobApplication.objects.filter(status='HIRED').count()

    restaurants = Restaurant.objects.filter(is_active=True).order_by('name')
    jobs = JobPosting.objects.select_related('restaurant').order_by('title')

    paginator = Paginator(qs.order_by('-created_at'), 15)
    page = request.GET.get('page')
    try:
        applications = paginator.page(page)
    except PageNotAnInteger:
        applications = paginator.page(1)
    except EmptyPage:
        applications = paginator.page(paginator.num_pages)

    return render(request, 'super_admin/job_applications_list.html', {
        'applications': applications,
        'total_count': total_count,
        'pending_count': pending_count,
        'shortlisted_count': shortlisted_count,
        'hired_count': hired_count,
        'restaurants': restaurants,
        'jobs': jobs,
        'job_filter': job_filter,
        'restaurant_filter': restaurant_filter,
        'branch_filter': branch_filter,
        'status_filter': status_filter,
        'experience_filter': experience_filter,
        'query': query,
    })


@superuser_required
def job_application_detail(request, pk):
    app = get_object_or_404(JobApplication.objects.select_related('job', 'restaurant', 'branch'), pk=pk)
    if not app.is_read:
        app.is_read = True
        app.save(update_fields=['is_read', 'updated_at'])

    return JsonResponse({
        'id': app.id,
        'full_name': app.full_name,
        'email': app.email,
        'country_code': app.country_code,
        'phone': app.phone,
        'formatted_phone': app.formatted_phone,
        'job_title': app.job.title,
        'department': app.job.department,
        'job_experience_level': app.job.experience_level,
        'restaurant_name': app.restaurant.name,
        'branch_name': app.branch.name if app.branch else 'All Outlets / Global',
        'has_experience': app.has_experience,
        'experience_display': app.experience_display,
        'experience_years': app.experience_years,
        'experience_months': app.experience_months,
        'current_ctc': app.current_ctc or 'N/A',
        'expected_ctc': app.expected_ctc or 'N/A',
        'current_organization': app.current_organization or 'N/A',
        'notice_period_days': f"{app.notice_period_days} Days" if app.notice_period_days is not None else 'Immediate / None',
        'reason_to_join': app.reason_to_join,
        'resume_url': app.resume.url if app.resume else '',
        'status': app.status,
        'status_display': app.get_status_display(),
        'admin_notes': app.admin_notes,
        'created_at': app.created_at.strftime('%b %d, %Y at %I:%M %p'),
    })


@superuser_required
def job_application_status_update(request, pk):
    app = get_object_or_404(JobApplication, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        admin_notes = request.POST.get('admin_notes')
        if new_status and new_status in dict(JobApplication.Status.choices):
            app.status = new_status
        if admin_notes is not None:
            app.admin_notes = admin_notes
        app.is_read = True
        app.save(update_fields=['status', 'admin_notes', 'is_read', 'updated_at'])

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'success': True, 'status': app.status, 'status_display': app.get_status_display()})
        messages.success(request, f'Application #{app.id} for {app.full_name} updated to {app.get_status_display()}.')
    return redirect('super_admin:job_applications_list')


@superuser_required
def job_application_delete(request, pk):
    app = get_object_or_404(JobApplication, pk=pk)
    if request.method == 'POST':
        name = app.full_name
        app.delete()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'success': True})
        messages.success(request, f'Application for {name} deleted.')
    return redirect('super_admin:job_applications_list')


@superuser_required
def job_applications_export_csv(request):
    job_filter = request.GET.get('job', '')
    restaurant_filter = request.GET.get('restaurant', '')
    branch_filter = request.GET.get('branch', '')
    status_filter = request.GET.get('status', 'all')
    experience_filter = request.GET.get('experience', 'all')
    query = request.GET.get('q', '').strip()

    qs = JobApplication.objects.select_related('job', 'restaurant', 'branch').all()

    if job_filter and job_filter.isdigit():
        qs = qs.filter(job_id=int(job_filter))
    if restaurant_filter and restaurant_filter.isdigit():
        qs = qs.filter(restaurant_id=int(restaurant_filter))
    if branch_filter and branch_filter.isdigit():
        qs = qs.filter(branch_id=int(branch_filter))
    if status_filter != 'all' and status_filter:
        qs = qs.filter(status=status_filter.upper())
    if experience_filter == 'yes':
        qs = qs.filter(has_experience=True)
    elif experience_filter == 'no':
        qs = qs.filter(has_experience=False)
    if query:
        qs = qs.filter(
            Q(full_name__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query) |
            Q(current_organization__icontains=query) |
            Q(reason_to_join__icontains=query) |
            Q(job__title__icontains=query)
        )

    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="job_applications_{timestamp}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'ID',
        'Candidate Name',
        'Email',
        'Phone',
        'Job Title',
        'Department',
        'Restaurant',
        'Branch',
        'Has Experience',
        'Experience Display',
        'Current Organization',
        'Current CTC',
        'Expected CTC',
        'Notice Period (Days)',
        'Reason To Join',
        'Status',
        'Is Read',
        'Resume URL',
        'Applied Date'
    ])

    for app in qs.order_by('-created_at'):
        writer.writerow([
            app.id,
            app.full_name,
            app.email,
            app.formatted_phone,
            app.job.title,
            app.job.department,
            app.restaurant.name,
            app.branch.name if app.branch else 'All Outlets / Global',
            'Yes' if app.has_experience else 'No',
            app.experience_display,
            app.current_organization,
            app.current_ctc,
            app.expected_ctc,
            app.notice_period_days if app.notice_period_days is not None else '',
            app.reason_to_join,
            app.get_status_display(),
            'Yes' if app.is_read else 'No',
            app.resume.url if app.resume else '',
            app.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        ])

    return response


def api_features_config(request):
    """
    Returns the centralized branch tags and features configuration JSON.
    Can be consumed by Super Admin UI and frontend applications.
    """
    return JsonResponse(features.get_features_js_config())
