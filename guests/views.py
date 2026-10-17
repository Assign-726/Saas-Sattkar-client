"""Guests (CRM), Loyalty, Gift Cards and the Morning Brief.

The same screens render inside the Restaurant, Hotel PMS or Super Admin shell:
each public view is a thin wrapper that resolves the business and permissions
(``_env``) and hands off to a shared implementation.
"""
import csv
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from guests import services
from guests.brief import compute_brief
from guests.models import GiftCard, Guest, GuestNote, LoyaltySettings
from super_admin.activity import log_activity, user_role

VIEW_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN', 'CLUB_ADMIN', 'CASHIER', 'FRONT_DESK', 'WAITER'}
MANAGE_ROLES = {'GENERAL_MANAGER', 'RESTAURANT_ADMIN', 'HOTEL_ADMIN', 'CLUB_ADMIN'}
SEGMENTS = [('', 'All guests'), ('vip', 'VIP'), ('regular', 'Regulars (5+ visits)'), ('new', 'New (30 days)'),
            ('lapsed', 'Lapsed (90+ days)'), ('occasions', 'Birthdays & anniversaries'), ('points', 'Has points')]
SORTS = [('recent', 'Last visit'), ('spend', 'Lifetime spend'), ('visits', 'Most visits'), ('points', 'Most points'), ('name', 'Name')]


def can_view(user):
    return user.is_superuser or user_role(user) in VIEW_ROLES


def can_manage(user):
    return user.is_superuser or user_role(user) in MANAGE_ROLES


def _money(value):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, AttributeError):
        return None


# --------------------------------------------------------------------------
# Shell resolution
# --------------------------------------------------------------------------

def _restaurant_env(request, restaurant_slug):
    from restaurant_admin.views import resolve_restaurant
    restaurant = resolve_restaurant(request, restaurant_slug)
    if isinstance(restaurant, HttpResponse):
        return restaurant
    if not request.user.is_staff or not can_view(request.user):
        return HttpResponseForbidden('Guest tools are available to front-of-house staff and administrators.')
    return {
        'restaurant': restaurant, 'shell': 'guests/shell_restaurant.html', 'area': 'restaurant',
        'base_url': reverse('restaurant_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug}),
        'ctx': {'restaurant': restaurant},
    }


def _hotel_env(request, restaurant_slug, ctx):
    if not can_view(request.user):
        return HttpResponseForbidden('Guest tools are available to front desk staff and administrators.')
    return {
        'restaurant': ctx['restaurant'], 'shell': 'guests/shell_hotel.html', 'area': 'hotel',
        'base_url': reverse('hotel_admin:dashboard', kwargs={'restaurant_slug': restaurant_slug}),
        'ctx': ctx,
    }


def _render(request, env, partial, context, active_menu):
    data = {**env['ctx'], **context, 'partial': partial, 'base_url': env['base_url'], 'active_menu': active_menu,
            'can_manage_guests': can_manage(request.user)}
    return render(request, env['shell'], data)


# --------------------------------------------------------------------------
# Guests list
# --------------------------------------------------------------------------

def _guest_queryset(restaurant, params):
    now = timezone.now()
    qs = Guest.objects.filter(restaurant=restaurant).annotate(
        visits=F('restaurant_visits') + F('hotel_stays') + F('reservations') + F('event_bookings'))
    q = (params.get('q') or '').strip()
    if q:
        digits = services.normalize_phone(q) or ''.join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(email__icontains=q) | Q(tags__icontains=q)
        if digits:
            cond |= Q(phone__contains=digits)
        qs = qs.filter(cond)
    segment = params.get('segment', '')
    if segment == 'vip':
        qs = qs.filter(is_vip=True)
    elif segment == 'regular':
        qs = qs.filter(visits__gte=5)
    elif segment == 'new':
        qs = qs.filter(first_seen__gte=now - timedelta(days=30))
    elif segment == 'lapsed':
        qs = qs.filter(last_seen__lt=now - timedelta(days=90))
    elif segment == 'points':
        qs = qs.filter(loyalty_points__gt=0)
    elif segment == 'occasions':
        today = timezone.localdate()
        cond = Q()
        for i in range(8):
            d = today + timedelta(days=i)
            cond |= Q(birthday__month=d.month, birthday__day=d.day) | Q(anniversary__month=d.month, anniversary__day=d.day)
        qs = qs.filter(cond)
    tag = params.get('tag')
    if tag:
        qs = qs.filter(tags__icontains=tag)
    order = {'spend': '-lifetime_spend', 'visits': '-visits', 'points': '-loyalty_points', 'name': 'name'}.get(params.get('sort'), '-last_seen')
    return qs.order_by(order, '-id')


def _guest_list(request, env):
    restaurant = env['restaurant']
    manage = can_manage(request.user)
    loyalty = LoyaltySettings.for_restaurant(restaurant)

    if request.method == 'POST':
        op = request.POST.get('op')
        if not manage:
            return HttpResponseForbidden('Only administrators can change this.')
        if op == 'rebuild':
            created, updated = services.rebuild_guests(restaurant)
            log_activity(request, action='IMPORT', message=f'Rebuilt guest profiles · {created} new, {updated} refreshed', area=env['area'], restaurant=restaurant)
            messages.success(request, f'Guest profiles rebuilt from history — {created} new, {updated} refreshed.')
        elif op == 'loyalty_settings':
            per100, value, minimum = _money(request.POST.get('points_per_100')), _money(request.POST.get('point_value')), request.POST.get('min_redeem_points', '')
            if per100 is None or value is None or not minimum.isdigit() or per100 < 0 or value < 0:
                messages.error(request, 'Enter valid loyalty numbers.')
            else:
                loyalty.enabled = request.POST.get('enabled') == 'on'
                loyalty.earn_on_hotel_stays = request.POST.get('earn_on_hotel_stays') == 'on'
                loyalty.points_per_100, loyalty.point_value, loyalty.min_redeem_points = per100, value, int(minimum)
                loyalty.save()
                log_activity(request, action='SETTINGS', message='Updated loyalty programme settings', area=env['area'], restaurant=restaurant)
                messages.success(request, 'Loyalty settings saved.')
        return redirect(request.get_full_path())

    qs = _guest_queryset(restaurant, request.GET)

    if request.GET.get('export') == 'csv':
        if not manage:
            return HttpResponseForbidden('Only administrators can export guests.')
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{restaurant.slug}-guests-{timezone.localdate():%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow(['Name', 'Phone', 'Email', 'VIP', 'Tags', 'Visits', 'Dining', 'Stays', 'Reservations', 'Events',
                    'Lifetime spend', 'Points', 'Birthday', 'Anniversary', 'First seen', 'Last seen', 'Marketing opt-in'])
        for g in qs.iterator(chunk_size=500):
            w.writerow([g.name, g.phone, g.email, 'Yes' if g.is_vip else '', ', '.join(g.tags), g.visits, g.restaurant_visits,
                        g.hotel_stays, g.reservations, g.event_bookings, g.lifetime_spend, g.loyalty_points,
                        g.birthday or '', g.anniversary or '', g.first_seen and timezone.localtime(g.first_seen).date(),
                        g.last_seen and timezone.localtime(g.last_seen).date(), 'Yes' if g.marketing_opt_in else 'No'])
        log_activity(request, action='EXPORT', message=f'Exported {qs.count()} guests', area=env['area'], restaurant=restaurant)
        return resp

    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    base = Guest.objects.filter(restaurant=restaurant)
    total = base.count()
    repeat = base.annotate(v=F('restaurant_visits') + F('hotel_stays') + F('reservations') + F('event_bookings')).filter(v__gte=2).count()
    all_tags = sorted({t for tags in base.exclude(tags=[]).values_list('tags', flat=True)[:2000] for t in tags})
    keep = {k: v for k, v in request.GET.items() if k in ('q', 'segment', 'tag', 'sort') and v}
    from urllib.parse import urlencode
    context = {
        'guests': page, 'page_obj': page, 'filters': {k: request.GET.get(k, '') for k in ('q', 'segment', 'tag', 'sort')},
        'filter_qs_amp': ('&' + urlencode(keep)) if keep else '', 'segments': SEGMENTS, 'sorts': SORTS, 'all_tags': all_tags,
        'loyalty': loyalty,
        'stats': {
            'total': total,
            'new_month': base.filter(first_seen__gte=timezone.now() - timedelta(days=30)).count(),
            'vip': base.filter(is_vip=True).count(),
            'repeat_rate': round(repeat * 100 / total) if total else 0,
            'points': base.aggregate(s=Sum('loyalty_points'))['s'] or 0,
            'occasions': _guest_queryset(restaurant, {'segment': 'occasions'}).count(),
        },
    }
    return _render(request, env, 'guests/_guest_list.html', context, 'guests')


# --------------------------------------------------------------------------
# Guest 360
# --------------------------------------------------------------------------

def _guest_detail(request, env, pk):
    restaurant = env['restaurant']
    guest = get_object_or_404(Guest, pk=pk, restaurant=restaurant)
    manage = can_manage(request.user)
    loyalty = LoyaltySettings.for_restaurant(restaurant)

    if request.method == 'POST':
        op = request.POST.get('op')
        name = request.user.get_full_name() or request.user.username
        try:
            if op == 'add_note':
                text = (request.POST.get('text') or '').strip()
                if text:
                    GuestNote.objects.create(guest=guest, author=request.user, author_name=name, text=text[:2000],
                                             pinned=request.POST.get('pinned') == 'on')
                    messages.success(request, 'Note added.')
            elif op == 'pin_note':
                note = get_object_or_404(GuestNote, pk=request.POST.get('note'), guest=guest)
                note.pinned = not note.pinned
                note.save(update_fields=['pinned'])
            elif op == 'delete_note':
                note = get_object_or_404(GuestNote, pk=request.POST.get('note'), guest=guest)
                if manage or note.author_id == request.user.id:
                    note.delete()
                    messages.success(request, 'Note deleted.')
            elif op == 'redeem':
                tx = services.redeem_points(guest, request.POST.get('points') or 0, request.POST.get('reason', ''), request.user)
                value = abs(tx.points) * loyalty.point_value
                log_activity(request, action='STATUS', message=f'Redeemed {abs(tx.points)} points for {guest.name} (₹{value:,.2f})', area=env['area'], restaurant=restaurant)
                messages.success(request, f'Redeemed {abs(tx.points)} points — give ₹{value:,.2f} off the bill.')
            elif op in ('adjust', 'update_profile') and not manage:
                return HttpResponseForbidden('Only administrators can change this.')
            elif op == 'adjust':
                tx = services.adjust_points(guest, request.POST.get('points') or 0, request.POST.get('reason', ''), request.user)
                log_activity(request, action='UPDATE', message=f'Adjusted {guest.name} points by {tx.points:+d}', area=env['area'], restaurant=restaurant)
                messages.success(request, f'Points adjusted by {tx.points:+d}.')
            elif op == 'update_profile':
                guest.name = (request.POST.get('name') or guest.name).strip()[:150]
                guest.email = services.normalize_email(request.POST.get('email'))
                phone = services.normalize_phone(request.POST.get('phone'))
                if phone and Guest.objects.filter(restaurant=restaurant, phone=phone).exclude(pk=guest.pk).exists():
                    messages.error(request, 'Another guest already has that phone number.')
                    return redirect(request.path)
                guest.phone = phone
                guest.birthday = parse_date(request.POST.get('birthday') or '') if request.POST.get('birthday') else None
                guest.anniversary = parse_date(request.POST.get('anniversary') or '') if request.POST.get('anniversary') else None
                guest.preferences = (request.POST.get('preferences') or '').strip()[:2000]
                guest.is_vip = request.POST.get('is_vip') == 'on'
                guest.marketing_opt_in = request.POST.get('marketing_opt_in') == 'on'
                guest.tags = sorted({t.strip()[:30] for t in (request.POST.get('tags') or '').split(',') if t.strip()})[:15]
                guest.save()
                log_activity(request, action='UPDATE', message=f'Updated guest profile · {guest.name}', area=env['area'], restaurant=restaurant)
                messages.success(request, 'Guest profile saved.')
        except services.LoyaltyError as exc:
            messages.error(request, str(exc))
        return redirect(request.path)

    activity = services.guest_activity(guest)
    services.refresh_guest_stats(guest, activity)  # keep the header numbers exact
    context = {
        'guest': guest,
        'activity': activity[:40],
        'activity_total': len(activity),
        'favourites': services.favourite_items(guest),
        'notes': guest.notes.all()[:50],
        'ledger': guest.loyalty_transactions.select_related('created_by')[:25],
        'gift_cards': guest.gift_cards.all()[:10],
        'loyalty': loyalty,
        'points_value': guest.loyalty_points * loyalty.point_value,
        'average_spend': (guest.lifetime_spend / guest.total_visits) if guest.total_visits else 0,
    }
    return _render(request, env, 'guests/_guest_detail.html', context, 'guests')


# --------------------------------------------------------------------------
# Gift cards
# --------------------------------------------------------------------------

def _gift_cards(request, env):
    restaurant = env['restaurant']
    if request.method == 'POST':
        if not can_manage(request.user):
            return HttpResponseForbidden('Only administrators can issue gift cards.')
        value = _money(request.POST.get('value'))
        try:
            if value is None:
                raise services.GiftCardError('Enter the card value.')
            phone = services.normalize_phone(request.POST.get('recipient_phone'))
            email = services.normalize_email(request.POST.get('recipient_email'))
            name = (request.POST.get('recipient_name') or '').strip()
            guest = services.upsert_guest(restaurant, name, phone, email) if (phone or email) else None
            card = services.issue_gift_card(
                restaurant, value, request.user, guest=guest, recipient_name=name[:150], recipient_phone=phone,
                recipient_email=email, expires_on=parse_date(request.POST.get('expires_on') or '') or None,
                note=(request.POST.get('note') or '')[:200])
            log_activity(request, action='CREATE', message=f'Issued gift card {card.code} · ₹{card.initial_value:,.0f}', area=env['area'], restaurant=restaurant)
            messages.success(request, f'Gift card {card.code} issued for ₹{card.initial_value:,.2f}.')
            return redirect(f"{env['base_url']}gift-cards/{card.pk}/")
        except services.GiftCardError as exc:
            messages.error(request, str(exc))
        return redirect(request.path)

    qs = GiftCard.objects.filter(restaurant=restaurant).select_related('guest')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(code__icontains=q.replace(' ', '')) | Q(recipient_name__icontains=q) | Q(recipient_phone__contains=q) | Q(recipient_email__icontains=q))
    status = request.GET.get('status', '')
    if status in GiftCard.Status.values:
        qs = qs.filter(status=status)
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    month_start = timezone.localdate().replace(day=1)
    base = GiftCard.objects.filter(restaurant=restaurant)
    from guests.models import GiftCardTransaction
    tx = GiftCardTransaction.objects.filter(card__restaurant=restaurant, created_at__date__gte=month_start)
    context = {
        'cards': page, 'page_obj': page, 'filters': {'q': q, 'status': status}, 'status_choices': GiftCard.Status.choices,
        'stats': {
            'active': base.filter(status='ACTIVE').count(),
            'liability': base.filter(status='ACTIVE').aggregate(s=Sum('balance'))['s'] or 0,
            'issued_month': tx.filter(kind__in=['ISSUE', 'TOPUP']).aggregate(s=Sum('amount'))['s'] or 0,
            'redeemed_month': tx.filter(kind='REDEEM').aggregate(s=Sum('amount'))['s'] or 0,
        },
        'today': timezone.localdate(),
    }
    return _render(request, env, 'guests/_gift_cards.html', context, 'gift_cards')


def _gift_card_detail(request, env, pk):
    restaurant = env['restaurant']
    card = get_object_or_404(GiftCard, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'redeem':
                card = services.redeem_gift_card(card, _money(request.POST.get('amount')) or 0, request.POST.get('note', ''), request.user)
                log_activity(request, action='STATUS', message=f'Redeemed ₹{_money(request.POST.get("amount")):,.2f} from gift card {card.code}', area=env['area'], restaurant=restaurant)
                messages.success(request, f'Redeemed. ₹{card.balance:,.2f} left on the card.')
            elif not can_manage(request.user):
                return HttpResponseForbidden('Only administrators can change this card.')
            elif op == 'topup':
                card = services.topup_gift_card(card, _money(request.POST.get('amount')) or 0, request.POST.get('note', ''), request.user)
                log_activity(request, action='UPDATE', message=f'Topped up gift card {card.code}', area=env['area'], restaurant=restaurant)
                messages.success(request, f'Topped up. New balance ₹{card.balance:,.2f}.')
            elif op == 'void':
                card = services.void_gift_card(card, request.POST.get('note', ''), request.user)
                log_activity(request, action='DELETE', message=f'Voided gift card {card.code}', area=env['area'], restaurant=restaurant)
                messages.success(request, 'Card voided.')
            elif op == 'extend':
                new_date = parse_date(request.POST.get('expires_on') or '')
                card.expires_on = new_date
                card.refresh_status()
                card.save(update_fields=['expires_on', 'status'])
                messages.success(request, 'Expiry updated.')
        except services.GiftCardError as exc:
            messages.error(request, str(exc))
        return redirect(request.path)
    card.refresh_status()
    context = {'card': card, 'transactions': card.transactions.select_related('created_by')[:100],
               'used': card.initial_value - card.balance if card.status != 'VOID' else card.initial_value}
    return _render(request, env, 'guests/_gift_card_detail.html', context, 'gift_cards')


# --------------------------------------------------------------------------
# Morning Brief
# --------------------------------------------------------------------------

def _action_urls(restaurant_slug, base_url):
    def r(name):
        return reverse(name, kwargs={'restaurant_slug': restaurant_slug})
    return {
        'pos': r('restaurant_admin:order_create_manual'), 'analytics': r('restaurant_admin:sales_analytics'),
        'orders': r('restaurant_admin:order_records'), 'inventory': r('restaurant_admin:inventory_list'),
        'reservations': r('restaurant_admin:reservation_list'), 'hotel': r('hotel_admin:dashboard'),
        'rooms': r('hotel_admin:rooms'), 'bookings': r('hotel_admin:bookings'),
        'guests': f'{base_url}guests/?segment=occasions',
        'feedback': f'{base_url}feedback/?mood=complaints&status=open',
    }


def _brief(request, env, restaurant_slug):
    data = compute_brief(env['restaurant'], module=env['area'])
    urls = _action_urls(restaurant_slug, env['base_url'])
    for a in data['alerts']:
        a['url'] = urls.get(a['action_key'])
    return _render(request, env, 'guests/_brief.html', {'brief': data, 'guest_base': env['base_url']}, 'brief')


# --------------------------------------------------------------------------
# Public views — Restaurant shell
# --------------------------------------------------------------------------

def _wrap(impl):
    @login_required
    def restaurant_view(request, restaurant_slug, *args, **kwargs):
        env = _restaurant_env(request, restaurant_slug)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, restaurant_slug, *args, **kwargs)
    return restaurant_view


def _wrap_hotel(impl):
    from hotel.views_admin import hotel_admin_required

    @hotel_admin_required
    def hotel_view(request, restaurant_slug, ctx, *args, **kwargs):
        env = _hotel_env(request, restaurant_slug, ctx)
        if isinstance(env, HttpResponse):
            return env
        return impl(request, env, restaurant_slug, *args, **kwargs)
    return hotel_view


def _list_impl(request, env, slug):
    return _guest_list(request, env)


def _detail_impl(request, env, slug, pk):
    return _guest_detail(request, env, pk)


def _cards_impl(request, env, slug):
    return _gift_cards(request, env)


def _card_impl(request, env, slug, pk):
    return _gift_card_detail(request, env, pk)


def _brief_impl(request, env, slug):
    return _brief(request, env, slug)


guest_list = _wrap(_list_impl)
guest_detail = _wrap(_detail_impl)
gift_card_list = _wrap(_cards_impl)
gift_card_detail = _wrap(_card_impl)
morning_brief = _wrap(_brief_impl)

hotel_guest_list = _wrap_hotel(_list_impl)
hotel_guest_detail = _wrap_hotel(_detail_impl)
hotel_gift_card_list = _wrap_hotel(_cards_impl)
hotel_gift_card_detail = _wrap_hotel(_card_impl)
hotel_morning_brief = _wrap_hotel(_brief_impl)


# --------------------------------------------------------------------------
# Super Admin — platform-wide brief
# --------------------------------------------------------------------------

def super_morning_brief(request):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return redirect('login')
    from super_admin.models import Restaurant
    data = compute_brief(None)
    analytics = reverse('super_admin:analytics')
    for a in data['alerts']:
        a['url'] = analytics if a['action_key'] in ('analytics', 'orders', 'pos') else None
    businesses = []
    for r in Restaurant.objects.filter(is_active=True).order_by('name')[:50]:
        b = compute_brief(r)
        businesses.append({'restaurant': r, 'figures': b['figures'], 'alerts': len([a for a in b['alerts'] if a['severity'] in ('critical', 'warning')]),
                           'guests': Guest.objects.filter(restaurant=r).count()})
    return render(request, 'guests/shell_super.html', {
        'partial': 'guests/_brief.html', 'brief': data, 'businesses': businesses, 'platform': True,
        'can_manage_guests': True,
    })


# --------------------------------------------------------------------------
# POS — points & gift card lookup for the settle screen
# --------------------------------------------------------------------------

@login_required
def pos_lookup(request, restaurant_slug):
    from django.http import JsonResponse
    from guests.pos import lookup
    env = _restaurant_env(request, restaurant_slug)
    if isinstance(env, HttpResponse):
        return JsonResponse({'error': 'Not allowed.'}, status=403)
    if request.GET.get('rooms'):
        from guests.pos import in_house_guests
        from restaurant_admin.views import get_active_branch
        branch = get_active_branch(request, env['restaurant'])
        return JsonResponse({'rooms': in_house_guests(env['restaurant'], branch)})
    phone = (request.GET.get('phone') or '').strip()[:20]
    code = (request.GET.get('code') or '').strip()[:40]
    return JsonResponse(lookup(env['restaurant'], phone=phone, code=code))
