from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.http import HttpResponseBadRequest

from super_admin.models import Restaurant, RestaurantUser
from reservations.models import Reservation, ReservationSetting
from .views import is_admin, resolve_restaurant
from django import forms


@login_required
@user_passes_test(is_admin)
def reservation_list(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    status = request.GET.get('status', '')
    date = request.GET.get('date', '')

    qs = Reservation.objects.filter(restaurant=restaurant).order_by('-date', '-start_time')
    if status:
        qs = qs.filter(status=status)
    if date:
        qs = qs.filter(date=date)

    paginator = Paginator(qs, 12)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(request, 'restaurant_admin/reservations/list.html', {
        'reservations': page_obj,
        'active_menu': 'reservations',
        'restaurant': restaurant,
        'status_filter': status,
        'date_filter': date,
        'status_choices': Reservation.ReservationStatus.choices,
    })


@login_required
@user_passes_test(is_admin)
def reservation_confirm(request, pk, restaurant_slug=None):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    reservation = get_object_or_404(Reservation, pk=pk, restaurant=restaurant)
    reservation.status = Reservation.ReservationStatus.CONFIRMED
    reservation.save(update_fields=['status'])
    messages.success(request, f'Reservation #{reservation.id} confirmed.')
    return redirect('restaurant_admin:reservation_list', restaurant_slug=restaurant.slug)


@login_required
@user_passes_test(is_admin)
def reservation_cancel(request, pk, restaurant_slug=None):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')
    try:
        restaurant = request.user.restaurant_profile.restaurant
    except RestaurantUser.DoesNotExist:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("No restaurant associated with this account.")
    reservation = get_object_or_404(Reservation, pk=pk, restaurant=restaurant)
    reservation.status = Reservation.ReservationStatus.CANCELLED
    reservation.save(update_fields=['status'])
    messages.success(request, f'Reservation #{reservation.id} cancelled.')
    return redirect('restaurant_admin:reservation_list', restaurant_slug=restaurant.slug)


class ReservationSettingForm(forms.ModelForm):
    class Meta:
        model = ReservationSetting
        fields = ['open_time', 'close_time', 'slot_interval_minutes', 'max_party_size', 'max_seats_per_slot']
        widgets = {
            'open_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'close_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'slot_interval_minutes': forms.NumberInput(attrs={'min': 5, 'step': 5, 'class': 'form-control'}),
            'max_party_size': forms.NumberInput(attrs={'min': 1, 'step': 1, 'class': 'form-control'}),
            'max_seats_per_slot': forms.NumberInput(attrs={'min': 1, 'step': 1, 'class': 'form-control'}),
        }


@login_required
@user_passes_test(is_admin)
def reservation_settings(request, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant

    instance, _ = ReservationSetting.objects.get_or_create(restaurant=restaurant)
    if request.method == 'POST':
        form = ReservationSettingForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, 'Reservation settings updated successfully.')
            return redirect('restaurant_admin:reservation_settings', restaurant_slug=restaurant.slug)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = ReservationSettingForm(instance=instance)

    return render(request, 'restaurant_admin/reservations/settings.html', {
        'form': form,
        'restaurant': restaurant,
        'active_menu': 'reservations',
        'title': 'Reservation Settings',
    })


class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = [
            'customer_name', 'phone', 'email', 'party_size',
            'date', 'start_time', 'duration_minutes',
            'occasion', 'notes', 'status'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'duration_minutes': forms.NumberInput(attrs={'min': 15, 'step': 15, 'class': 'form-control'}),
            'party_size': forms.NumberInput(attrs={'min': 1, 'step': 1, 'class': 'form-control'}),
            'occasion': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }


@login_required
@user_passes_test(is_admin)
def reservation_detail(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    reservation = get_object_or_404(Reservation, pk=pk, restaurant=restaurant)
    return render(request, 'restaurant_admin/reservations/detail.html', {
        'reservation': reservation,
        'restaurant': restaurant,
        'active_menu': 'reservations',
    })


@login_required
@user_passes_test(is_admin)
def reservation_edit(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    reservation = get_object_or_404(Reservation, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        form = ReservationForm(request.POST, instance=reservation)
        if form.is_valid():
            form.save()
            messages.success(request, f'Reservation #{reservation.id} updated.')
            return redirect('restaurant_admin:reservation_detail', restaurant_slug=restaurant.slug, pk=reservation.id)
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = ReservationForm(instance=reservation)

    return render(request, 'restaurant_admin/reservations/form.html', {
        'form': form,
        'reservation': reservation,
        'restaurant': restaurant,
        'active_menu': 'reservations',
        'title': f'Edit Reservation #{reservation.id}',
    })


@login_required
@user_passes_test(is_admin)
def reservation_delete(request, pk, restaurant_slug=None):
    restaurant = resolve_restaurant(request, restaurant_slug)
    if not isinstance(restaurant, Restaurant):
        return restaurant
    reservation = get_object_or_404(Reservation, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        reservation.delete()
        messages.success(request, f'Reservation #{reservation.id} deleted.')
        return redirect('restaurant_admin:reservation_list', restaurant_slug=restaurant.slug)

    return render(request, 'restaurant_admin/reservations/delete_confirm.html', {
        'reservation': reservation,
        'restaurant': restaurant,
        'active_menu': 'reservations',
        'title': f'Delete Reservation #{reservation.id}',
    })
