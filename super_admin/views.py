from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages

from django.urls import reverse
from django.http import JsonResponse

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
    BlogPostForm,
)
from super_admin.models import Restaurant, RestaurantUser, GstRegistration, RestaurantBranch, DocumentSeries
from core.models import RestaurantLanding, GalleryImage, TeamMember, Review, Event, BlogPost, BlogComment
from restaurant_project import constants



def superuser_required(view):
    return user_passes_test(lambda u: u.is_active and u.is_superuser)(view)


@superuser_required
def landing_index(request):
    restaurants = Restaurant.objects.all().order_by('name')
    return render(request, 'super_admin/landing_list.html', {
        'restaurants': restaurants,
    })


@superuser_required
def dashboard(request):
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
    if constants.MAX_ALLOWED_BUSINESSES > 0 and Restaurant.objects.count() >= constants.MAX_ALLOWED_BUSINESSES:
        existing = Restaurant.objects.first()
        messages.warning(
            request,
            f"Business limit reached ({constants.MAX_ALLOWED_BUSINESSES} allowed). You can edit '{existing.trade_name or existing.name}' details and manage outlets below."
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
            master_url = os.environ.get('MASTER_DASHBOARD_URL', '')
            api_secret = os.environ.get('MASTER_API_SECRET', '')
            if master_url and not restaurant.master_license_key:
                sync_result = restaurant.sync_to_master_dashboard(master_url, api_secret)
                if sync_result.get('license_key'):
                    messages.success(
                        request,
                        f"Restaurant '{restaurant.name}' onboarded. License key: {sync_result['license_key']}"
                    )
                else:
                    messages.warning(
                        request,
                        f"Restaurant '{restaurant.name}' saved, but MasterPanel sync failed. Retry from dashboard."
                    )
            else:
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
    })


# --- Restaurant CRUD ---
@superuser_required
def restaurant_edit(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)

    if request.method == 'POST':
        form = OnboardRestaurantForm(request.POST, instance=restaurant)
        branches_json = request.POST.get('branches_json', '[]')
        try:
            branches_data = json.loads(branches_json)
        except Exception:
            branches_data = []

        if form.is_valid():
            restaurant = form.save(instance=restaurant, branches_data=branches_data)
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
            'restaurant_is_active': restaurant.is_active,
        }
        form = OnboardRestaurantForm(initial=initial, instance=restaurant)

    existing_branches = []
    for b in restaurant.branches.all():
        existing_branches.append({
            'id': b.id,
            'name': b.name,
            'phone': b.phone,
            'use_company_address': b.use_company_address,
            'address_line_1': b.address_line_1,
            'address_line_2': b.address_line_2,
            'city': b.city,
            'state': b.state or restaurant.state,
            'pincode': b.pincode,
            'fssai_license_no': b.fssai_license_no,
            'serves_alcohol': b.serves_alcohol,
            'gstin': b.gst_registration.gstin if b.gst_registration else b.gstin,
            'gst_registration_type': b.gst_registration_type,
            'is_hotel_tariff_7500_plus': b.is_hotel_tariff_7500_plus,
            'e_invoicing_enabled': b.e_invoicing_enabled,
        })

    return render(request, 'super_admin/onboard.html', {
        'form': form,
        'is_edit': True,
        'restaurant': restaurant,
        'all_restaurants': [restaurant],
        'initial_branches_json': json.dumps(existing_branches),
    })


@superuser_required
def restaurant_delete(request, pk):
    restaurant = Restaurant.objects.get(pk=pk)
    if request.method == 'POST':
        restaurant.delete()
        messages.success(request, 'Restaurant deleted.')
        return redirect('super_admin:dashboard')
    return render(request, 'super_admin/restaurant_confirm_delete.html', {'restaurant': restaurant})


@superuser_required
def restaurant_toggle_active(request, pk):
    restaurant = get_object_or_404(Restaurant, pk=pk)
    restaurant.is_active = not restaurant.is_active
    restaurant.save()
    status_str = "Active" if restaurant.is_active else "Inactive"
    messages.success(request, f"Business '{restaurant.name}' website status updated to {status_str}.")
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'json' in request.headers.get('Accept', ''):
        return JsonResponse({'success': True, 'is_active': restaurant.is_active, 'status': status_str})
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('super_admin:dashboard')
    return redirect(next_url)



# --- Restaurant Admin CRUD ---
@superuser_required
def restaurant_admin_edit(request, pk):
    ru = RestaurantUser.objects.select_related('user').get(pk=pk)
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
    restaurant = Restaurant.objects.get(pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)

    if request.method == 'POST' and 'save_landing' in request.POST:
        form = RestaurantLandingForm(request.POST, request.FILES, instance=landing)
        if form.is_valid():
            landing_obj = form.save(commit=False)
            if 'clear_logo' in request.POST:
                if landing_obj.logo:
                    landing_obj.logo.delete(save=False)
                landing_obj.logo = None
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
            landing_obj.save()
            messages.success(request, 'Landing page saved.')
            return redirect('super_admin:landing_edit', pk=restaurant.pk)
    else:
        form = RestaurantLandingForm(instance=landing)

    gallery_form = GalleryImageForm()
    images = landing.gallery_images.all()
    team_form = TeamMemberForm()
    members = landing.team_members.all().order_by('display_order', 'id')
    review_form = ReviewForm()
    reviews = landing.reviews.all().order_by('display_order', 'id')
    event_form = EventForm()
    events = landing.events.all().order_by('display_order', 'id')
    blog_form = BlogPostForm()
    blog_posts = restaurant.blog_posts.all().order_by('display_order', '-created_at')
    return render(request, 'super_admin/landing_edit.html', {
        'restaurant': restaurant,
        'form': form,
        'gallery_form': gallery_form,
        'images': images,
        'team_form': team_form,
        'members': members,
        'review_form': review_form,
        'reviews': reviews,
        'event_form': event_form,
        'events': events,
        'blog_form': blog_form,
        'blog_posts': blog_posts,
    })


@superuser_required
def gallery_add(request, pk):
    restaurant = Restaurant.objects.get(pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = GalleryImageForm(request.POST, request.FILES)
        if form.is_valid():
            gi: GalleryImage = form.save(commit=False)
            gi.landing = landing
            gi.save()
            messages.success(request, 'Image added to gallery.')
        else:
            messages.error(request, 'Please correct errors in the image form.')
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
    restaurant = Restaurant.objects.get(pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = TeamMemberForm(request.POST, request.FILES)
        if form.is_valid():
            tm: TeamMember = form.save(commit=False)
            tm.landing = landing
            tm.save()
            messages.success(request, 'Team member added.')
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
    restaurant = Restaurant.objects.get(pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = ReviewForm(request.POST)
        if form.is_valid():
            rv: Review = form.save(commit=False)
            rv.landing = landing
            rv.save()
            messages.success(request, 'Review added.')
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
def event_add(request, pk):
    restaurant = Restaurant.objects.get(pk=pk)
    landing, _ = RestaurantLanding.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES)
        if form.is_valid():
            evt = form.save(commit=False)
            evt.landing = landing
            evt.save()
            messages.success(request, 'Event added successfully.')
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
                    'phone': b.phone,
                    'use_company_address': b.use_company_address,
                    'address_line_1': b.address_line_1,
                    'address_line_2': b.address_line_2,
                    'city': b.city,
                    'state': b.state or r.state,
                    'pincode': b.pincode,
                    'fssai_license_no': b.fssai_license_no,
                    'serves_alcohol': b.serves_alcohol,
                    'gstin': b.gst_registration.gstin if b.gst_registration else b.gstin,
                    'gst_registration_type': b.gst_registration_type,
                    'is_hotel_tariff_7500_plus': b.is_hotel_tariff_7500_plus,
                    'e_invoicing_enabled': b.e_invoicing_enabled,
                })
    return JsonResponse({'restaurant': restaurant_data, 'branches': branches})


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



