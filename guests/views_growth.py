"""Feedback desk, campaigns, and the two public pages (feedback form, unsubscribe).

Screens reuse the Guests shells (Restaurant / Hotel) from ``guests.views``.
"""
import base64
import csv
import io
from datetime import timedelta

from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from guests import growth, services
from guests.models import Campaign, CampaignRecipient, Feedback, FeedbackUpdate, Guest, MarketingSettings
from guests.views import SEGMENTS, _render, _wrap, _wrap_hotel, can_manage
from super_admin.activity import log_activity


def _client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')) or 'unknown'


def _qr_data_uri(url):
    try:
        import qrcode
        img = qrcode.make(url, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ''


def _feedback_links(request, restaurant):
    links = []
    for b in restaurant.branches.all().order_by('name'):
        url = request.build_absolute_uri(reverse('guest_feedback_public', kwargs={'restaurant_slug': restaurant.slug, 'branch_id': b.id}))
        links.append({'branch': b, 'url': url, 'qr': _qr_data_uri(url)})
    return links


# --------------------------------------------------------------------------
# Feedback desk
# --------------------------------------------------------------------------

def _module_feedback(restaurant, env):
    """Each shell handles its own feedback: dining/delivery/events in the restaurant, stays in the hotel."""
    from super_admin.modules import FEEDBACK_AREAS
    qs = Feedback.objects.filter(restaurant=restaurant)
    return qs.filter(area__in=FEEDBACK_AREAS[env['area']]) if env['area'] in FEEDBACK_AREAS else qs


def _module_areas(env):
    from super_admin.modules import FEEDBACK_AREAS
    allowed = FEEDBACK_AREAS.get(env['area'])
    return [(v, l) for v, l in Feedback.Area.choices if not allowed or v in allowed]


def _feedback_qs(restaurant, params, env):
    qs = _module_feedback(restaurant, env).select_related('branch', 'guest', 'assigned_to')
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(comment__icontains=q))
    status = params.get('status', '')
    if status == 'open':
        qs = qs.filter(status__in=['NEW', 'IN_PROGRESS'])
    elif status in Feedback.Status.values:
        qs = qs.filter(status=status)
    mood = params.get('mood', '')
    if mood == 'complaints':
        qs = qs.filter(rating__lte=2)
    elif mood == 'neutral':
        qs = qs.filter(rating=3)
    elif mood == 'happy':
        qs = qs.filter(rating__gte=4)
    if params.get('area') in Feedback.Area.values:
        qs = qs.filter(area=params['area'])
    if str(params.get('branch', '')).isdigit():
        qs = qs.filter(branch_id=int(params['branch']))
    return qs


def _feedback_list(request, env, slug):
    restaurant = env['restaurant']
    if request.method == 'POST':
        branch = restaurant.branches.filter(pk=request.POST.get('branch') or 0).first()
        data = request.POST.copy()
        if data.get('area') not in dict(_module_areas(env)):
            data['area'] = 'stay' if env['area'] == 'hotel' else 'dining'
        try:
            fb = growth.submit_feedback(restaurant, branch, data, source=Feedback.Source.STAFF, user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(request.path)
        log_activity(request, action='CREATE', message=f'Logged {fb.rating}★ feedback from {fb.name or "a guest"}', area=env['area'], restaurant=restaurant)
        messages.success(request, 'Feedback logged.')
        return redirect(f'{env["base_url"]}feedback/{fb.pk}/')

    params = request.GET
    if 'status' not in params and 'mood' not in params and not params.get('q'):
        params = params.copy()
        params['status'] = 'open'
    qs = _feedback_qs(restaurant, params, env)
    month = _module_feedback(restaurant, env).filter(created_at__gte=timezone.now() - timedelta(days=30))
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    return _render(request, env, 'guests/_feedback_list.html', {
        'page': page, 'stats': growth.feedback_stats(month), 'filters': params,
        'statuses': Feedback.Status.choices, 'areas': _module_areas(env), 'branches': restaurant.branches.all().order_by('name'),
        'links': _feedback_links(request, restaurant), 'page_title': 'Guest Feedback',
        'open_count': _module_feedback(restaurant, env).filter(status__in=['NEW', 'IN_PROGRESS']).count(),
    }, 'feedback')


def _feedback_detail(request, env, slug, pk):
    restaurant = env['restaurant']
    fb = get_object_or_404(_module_feedback(restaurant, env).select_related('guest', 'branch', 'order', 'assigned_to'), pk=pk)
    manage = can_manage(request.user)
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'note':
                text = (request.POST.get('text') or '').strip()[:2000]
                if text:
                    FeedbackUpdate.objects.create(feedback=fb, kind='note', text=text, author=request.user)
                    if fb.status == Feedback.Status.NEW:
                        growth.set_status(fb, Feedback.Status.IN_PROGRESS, request.user)
            elif op == 'assign_me':
                fb.assigned_to = request.user
                fb.save(update_fields=['assigned_to'])
                FeedbackUpdate.objects.create(feedback=fb, kind='assigned', text=f'Assigned to {request.user.get_full_name() or request.user.username}', author=request.user)
                if fb.status == Feedback.Status.NEW:
                    growth.set_status(fb, Feedback.Status.IN_PROGRESS, request.user)
            elif op == 'status':
                growth.set_status(fb, request.POST.get('status'), request.user, (request.POST.get('note') or '').strip()[:2000])
                log_activity(request, action='STATUS', message=f'Feedback #{fb.pk} → {fb.get_status_display()}', area=env['area'], restaurant=restaurant)
            elif op in ('gift_card', 'points'):
                if not manage:
                    return HttpResponseForbidden('Only administrators can give make-goods.')
                guest = fb.guest or services.upsert_guest(restaurant, fb.name, fb.phone, fb.email)
                if op == 'gift_card':
                    card = services.issue_gift_card(restaurant, request.POST.get('value') or '0', user=request.user, guest=guest,
                                                    recipient_name=fb.name, recipient_phone=fb.phone, recipient_email=fb.email,
                                                    note=f'Make-good for feedback #{fb.pk}')
                    text = f'Issued gift card {card.code} worth ₹{card.initial_value:,.0f}'
                else:
                    if not guest:
                        messages.error(request, 'Add the guest\'s phone or email first to give points.')
                        return redirect(request.path)
                    tx = services.adjust_points(guest, request.POST.get('points') or 0, f'Make-good for feedback #{fb.pk}', request.user)
                    text = f'Added {tx.points} loyalty points'
                FeedbackUpdate.objects.create(feedback=fb, kind='make_good', text=text, author=request.user)
                log_activity(request, action='CREATE', message=f'{text} · feedback #{fb.pk}', area=env['area'], restaurant=restaurant)
                messages.success(request, text + '.')
        except (services.GiftCardError, services.LoyaltyError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect(request.path)

    return _render(request, env, 'guests/_feedback_detail.html', {
        'fb': fb, 'updates': fb.updates.select_related('author'), 'statuses': Feedback.Status.choices,
        'wa': growth.wa_link(fb.phone, f'Hi {fb.name.split(" ")[0] if fb.name else ""}, this is {restaurant.name}. Thank you for your feedback — ') if fb.phone else '',
        'page_title': 'Feedback',
    }, 'feedback')


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------

def _require_manager(request):
    if not can_manage(request.user):
        return HttpResponseForbidden('Campaigns are run by administrators.')
    return None


def _campaign_list(request, env, slug):
    denied = _require_manager(request)
    if denied:
        return denied
    restaurant = env['restaurant']
    ms = MarketingSettings.for_restaurant(restaurant)
    if request.method == 'POST' and request.POST.get('op') == 'automations':
        for kind in ('birthday', 'anniversary', 'winback'):
            setattr(ms, f'{kind}_enabled', request.POST.get(f'{kind}_enabled') == 'on')
            msg = (request.POST.get(f'{kind}_message') or '').strip()
            if msg:
                setattr(ms, f'{kind}_message', msg[:1000])
        try:
            ms.winback_days = max(14, min(int(request.POST.get('winback_days') or 60), 365))
        except ValueError:
            pass
        ms.winback_offer_code = (request.POST.get('winback_offer_code') or '').strip()[:40]
        ms.save()
        log_activity(request, action='UPDATE', message='Updated automatic guest messages', area=env['area'], restaurant=restaurant)
        messages.success(request, 'Automatic messages saved.')
        return redirect(request.path)

    campaigns = Campaign.objects.filter(restaurant=restaurant)
    page = Paginator(campaigns, 20).get_page(request.GET.get('page'))
    month = timezone.now() - timedelta(days=30)
    stats = {
        'sent_30d': CampaignRecipient.objects.filter(campaign__restaurant=restaurant, status='SENT', sent_at__gte=month).count(),
        'campaigns_30d': campaigns.filter(created_at__gte=month).count(),
        'reachable': Guest.objects.filter(restaurant=restaurant, marketing_opt_in=True).exclude(phone='', email='').count(),
        'opted_out': Guest.objects.filter(restaurant=restaurant, marketing_opt_in=False).count(),
        'automations_on': sum([ms.birthday_enabled, ms.anniversary_enabled, ms.winback_enabled]),
    }
    return _render(request, env, 'guests/_campaign_list.html', {'page': page, 'stats': stats, 'ms': ms, 'page_title': 'Campaigns'}, 'campaigns')


def _campaign_new(request, env, slug):
    denied = _require_manager(request)
    if denied:
        return denied
    restaurant = env['restaurant']
    if request.GET.get('preview'):
        channel, segment = request.GET.get('channel', 'whatsapp'), request.GET.get('segment', '')
        qs = growth.audience(restaurant, segment, channel)
        sample = qs.first() or Guest(name='Anita Sharma', loyalty_points=240, restaurant=restaurant)
        return JsonResponse({'count': qs.count(), 'preview': growth.render_message(request.GET.get('message', ''), sample, restaurant, request.GET.get('offer_code', '')),
                             'sample_name': sample.name})
    if request.method == 'POST':
        name, message = (request.POST.get('name') or '').strip()[:150], (request.POST.get('message') or '').strip()[:2000]
        channel = request.POST.get('channel') if request.POST.get('channel') in Campaign.Channel.values else Campaign.Channel.WHATSAPP
        segment = request.POST.get('segment') if request.POST.get('segment') in dict(SEGMENTS) else ''
        if not name or not message:
            messages.error(request, 'Give the campaign a name and a message.')
            return redirect(request.path)
        c = Campaign.objects.create(restaurant=restaurant, name=name, channel=channel, segment=segment, message=message,
                                    subject=(request.POST.get('subject') or '').strip()[:150], offer_code=(request.POST.get('offer_code') or '').strip()[:40],
                                    created_by=request.user)
        n = growth.prepare(c)
        log_activity(request, action='CREATE', message=f'Created campaign "{c.name}" for {n} guests ({c.get_channel_display()})', area=env['area'], restaurant=restaurant)
        messages.success(request, f'Campaign ready for {n} guest{"s" if n != 1 else ""}. Review and send.')
        return redirect(f'{env["base_url"]}campaigns/{c.pk}/')
    return _render(request, env, 'guests/_campaign_new.html', {'segments': SEGMENTS, 'channels': Campaign.Channel.choices, 'page_title': 'New campaign'}, 'campaigns')


def _campaign_detail(request, env, slug, pk):
    denied = _require_manager(request)
    if denied:
        return denied
    restaurant = env['restaurant']
    c = get_object_or_404(Campaign, pk=pk, restaurant=restaurant)
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'send_email' and c.channel == Campaign.Channel.EMAIL:
            sent, failed = growth.send_email(c, request.build_absolute_uri('/'), request.user, limit=300)
            log_activity(request, action='OTHER', message=f'Sent campaign "{c.name}" · {sent} emails, {failed} failed', area=env['area'], restaurant=restaurant)
            (messages.success if not failed else messages.warning)(request, f'Sent {sent} email{"s" if sent != 1 else ""}' + (f', {failed} failed' if failed else '') + '.')
        elif op == 'mark_sent':
            ids = request.POST.getlist('rcpt') or ([request.POST['one']] if request.POST.get('one') else [])
            qs = c.recipients.filter(status='PENDING')
            if request.POST.get('all') != '1':
                qs = qs.filter(pk__in=[i for i in ids if str(i).isdigit()])
            n = qs.update(status='SENT', sent_at=timezone.now(), sent_by=request.user)
            if c.status == Campaign.Status.DRAFT and n:
                c.status = Campaign.Status.SENDING
                c.save(update_fields=['status'])
            growth.refresh_counts(c)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': True, 'sent': c.sent_count, 'pending': c.recipients.filter(status='PENDING').count()})
            messages.success(request, f'Marked {n} as sent.')
        elif op == 'rebuild' and c.status == Campaign.Status.DRAFT:
            n = growth.prepare(c)
            messages.success(request, f'Audience refreshed · {n} guests.')
        elif op == 'delete' and c.status == Campaign.Status.DRAFT:
            c.delete()
            messages.success(request, 'Draft deleted.')
            return redirect(f'{env["base_url"]}campaigns/')
        return redirect(request.path + ('?' + request.GET.urlencode() if request.GET else ''))

    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="campaign-{c.pk}.csv"'
        w = csv.writer(resp)
        w.writerow(['Name', 'Phone / email', 'Message', 'Status'])
        for r in c.recipients.select_related('guest'):
            w.writerow([r.guest.name, r.address, r.message, r.get_status_display()])
        return resp

    rcpts = c.recipients.select_related('guest')
    show = request.GET.get('show', '')
    if show in CampaignRecipient.Status.values:
        rcpts = rcpts.filter(status=show)
    page = Paginator(rcpts, 50).get_page(request.GET.get('page'))
    for r in page.object_list:
        if c.channel == Campaign.Channel.WHATSAPP:
            r.link = growth.wa_link(r.address, r.message)
        elif c.channel == Campaign.Channel.SMS:
            r.link = growth.sms_link(r.address, r.message)
    pending = c.recipients.filter(status='PENDING').count()
    return _render(request, env, 'guests/_campaign_detail.html', {'c': c, 'page': page, 'pending': pending, 'show': show,
                                                                   'segment_label': dict(SEGMENTS).get(c.segment, 'All guests'), 'page_title': c.name}, 'campaigns')


feedback_list, feedback_detail = _wrap(_feedback_list), _wrap(_feedback_detail)
campaign_list, campaign_new, campaign_detail = _wrap(_campaign_list), _wrap(_campaign_new), _wrap(_campaign_detail)
hotel_feedback_list, hotel_feedback_detail = _wrap_hotel(_feedback_list), _wrap_hotel(_feedback_detail)
hotel_campaign_list, hotel_campaign_new, hotel_campaign_detail = _wrap_hotel(_campaign_list), _wrap_hotel(_campaign_new), _wrap_hotel(_campaign_detail)


# --------------------------------------------------------------------------
# Public pages
# --------------------------------------------------------------------------

def feedback_public(request, restaurant_slug, branch_id):
    from super_admin.models import Restaurant
    restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
    branch = get_object_or_404(restaurant.branches, pk=branch_id)
    from super_admin.modules import FEEDBACK_AREAS
    has_rooms, has_dining = getattr(branch, 'has_hotel_module', False), getattr(branch, 'has_restaurant_module', True)
    allowed = set(FEEDBACK_AREAS['hotel'] if has_rooms else []) | set(FEEDBACK_AREAS['restaurant'] if has_dining else [])
    ctx = {'restaurant': restaurant, 'branch': branch, 'aspects': Feedback.ASPECTS,
           'areas': [(v, l) for v, l in Feedback.Area.choices if not allowed or v in allowed],
           'default_area': 'stay' if getattr(branch, 'has_hotel_module', False) and not getattr(branch, 'has_restaurant_module', True) else 'dining'}
    if request.method == 'POST':
        if request.POST.get('website'):  # honeypot
            return render(request, 'guests/public/feedback.html', {**ctx, 'done': True})
        key = f'fb-public:{_client_ip(request)}'
        cache.add(key, 0, 3600)
        try:
            n = cache.incr(key)
        except ValueError:
            n = 1
        if n > 8:
            return render(request, 'guests/public/feedback.html', {**ctx, 'error': 'Thank you — we have already received several responses from this device. Please try again later.'}, status=429)
        try:
            fb = growth.submit_feedback(restaurant, branch, request.POST)
        except ValueError as exc:
            return render(request, 'guests/public/feedback.html', {**ctx, 'error': str(exc), 'posted': request.POST}, status=400)
        return render(request, 'guests/public/feedback.html', {**ctx, 'done': True, 'fb': fb})
    return render(request, 'guests/public/feedback.html', ctx)


def unsubscribe(request, token):
    guest = growth.guest_from_unsubscribe(token)
    if not guest:
        return render(request, 'guests/public/unsubscribe.html', {'invalid': True}, status=400)
    if request.method == 'POST':
        guest.marketing_opt_in = False
        guest.save(update_fields=['marketing_opt_in'])
        return render(request, 'guests/public/unsubscribe.html', {'guest': guest, 'done': True})
    return render(request, 'guests/public/unsubscribe.html', {'guest': guest})
