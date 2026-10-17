"""Feedback desk and campaigns: the business rules behind the screens."""
import logging
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

from django.core import signing
from django.db.models import Avg, Count, Q
from django.utils import timezone

from guests import services
from guests.models import Campaign, CampaignRecipient, Feedback, FeedbackUpdate, Guest

logger = logging.getLogger(__name__)

UNSUB_SALT = 'guests.unsubscribe'


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------

def _score(value, low=1, high=5):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if low <= v <= high else None


def submit_feedback(restaurant, branch, data, source=Feedback.Source.QR, user=None, order=None):
    """Create a feedback ticket from form/API data. Returns the ticket or raises ``ValueError``."""
    rating = _score(data.get('rating'))
    if rating is None:
        raise ValueError('Choose an overall rating from 1 to 5 stars.')
    area = data.get('area') if data.get('area') in Feedback.Area.values else Feedback.Area.DINING
    name = str(data.get('name') or '').strip()[:150]
    phone = str(data.get('phone') or '').strip()[:20]
    email = str(data.get('email') or '').strip()[:254]
    fb = Feedback.objects.create(
        restaurant=restaurant, branch=branch, order=order, area=area, source=source, rating=rating,
        food=_score(data.get('food')), service=_score(data.get('service')), ambience=_score(data.get('ambience')),
        value=_score(data.get('value')), recommend=_score(data.get('recommend'), 0, 10),
        comment=str(data.get('comment') or '').strip()[:4000], name=name, phone=phone, email=email,
        may_contact=str(data.get('may_contact', 'on')).lower() in ('on', 'true', '1', 'yes'),
    )
    guest = services.upsert_guest(restaurant, name, phone, email) if (phone or email) else None
    if guest:
        fb.guest = guest
        fb.save(update_fields=['guest'])
    if user is not None:
        FeedbackUpdate.objects.create(feedback=fb, kind='note', text='Logged by staff.', author=user)
    try:
        from integrations import dispatch
        dispatch.emit('feedback.received', restaurant=restaurant, branch=branch, data=feedback_payload(fb))
    except Exception:
        logger.exception('feedback webhook failed')
    return fb


def feedback_payload(fb):
    return {'id': fb.id, 'rating': fb.rating, 'area': fb.area, 'recommend': fb.recommend,
            'aspects': {k: getattr(fb, k) for k, _ in Feedback.ASPECTS}, 'comment': fb.comment,
            'guest': {'name': fb.name, 'phone': fb.phone, 'email': fb.email}, 'status': fb.status.lower(),
            'order_id': fb.order_id, 'created_at': fb.created_at.isoformat()}


def feedback_stats(qs):
    agg = qs.aggregate(n=Count('id'), avg=Avg('rating'), food=Avg('food'), service=Avg('service'),
                       ambience=Avg('ambience'), value=Avg('value'))
    dist = {r: 0 for r in range(1, 6)}
    for row in qs.values('rating').annotate(c=Count('id')):
        dist[row['rating']] = row['c']
    n = agg['n'] or 0
    rec = qs.filter(recommend__isnull=False)
    rec_n = rec.count()
    nps = None
    if rec_n:
        nps = round((rec.filter(recommend__gte=9).count() - rec.filter(recommend__lte=6).count()) * 100 / rec_n)
    resolved = qs.filter(resolved_at__isnull=False).values_list('created_at', 'resolved_at')
    hours = [(r - c).total_seconds() / 3600 for c, r in resolved]
    return {
        'count': n, 'avg': round(agg['avg'], 1) if agg['avg'] else None, 'nps': nps,
        'aspects': [(label, round(agg[key], 1) if agg[key] else None) for key, label in Feedback.ASPECTS],
        'distribution': [(r, dist[r], round(dist[r] * 100 / n) if n else 0) for r in range(5, 0, -1)],
        'open_complaints': qs.filter(rating__lte=2, status__in=['NEW', 'IN_PROGRESS']).count(),
        'avg_resolution_hours': round(sum(hours) / len(hours), 1) if hours else None,
    }


def set_status(fb, status, user, note=''):
    if status not in Feedback.Status.values or status == fb.status:
        return
    old = fb.get_status_display()
    fb.status = status
    if status in (Feedback.Status.RESOLVED, Feedback.Status.CLOSED) and not fb.resolved_at:
        fb.resolved_at, fb.resolved_by = timezone.now(), user
        if note:
            fb.resolution = note
    elif status in (Feedback.Status.NEW, Feedback.Status.IN_PROGRESS):
        fb.resolved_at = None
    fb.save()
    FeedbackUpdate.objects.create(feedback=fb, kind='status', author=user,
                                  text=f'{old} → {fb.get_status_display()}' + (f' · {note}' if note else ''))


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------

def audience(restaurant, segment, channel):
    from guests.views import _guest_queryset
    qs = _guest_queryset(restaurant, {'segment': segment}).filter(marketing_opt_in=True)
    if channel == Campaign.Channel.EMAIL:
        qs = qs.exclude(email='')
    else:
        qs = qs.exclude(phone='')
    return qs


def render_message(template, guest, restaurant, offer_code=''):
    from guests.models import LoyaltySettings
    first = (guest.name or '').split(' ')[0] or 'there'
    values = {
        '{name}': guest.name or 'there', '{first_name}': first, '{points}': str(guest.loyalty_points),
        '{points_value}': f'₹{(Decimal(guest.loyalty_points) * LoyaltySettings.for_restaurant(restaurant).point_value):,.0f}',
        '{business}': restaurant.name, '{offer_code}': offer_code or '',
    }
    out = template
    for k, v in values.items():
        out = out.replace(k, v)
    return out.strip()


def prepare(campaign):
    """(Re)build the recipient list for a draft campaign."""
    if campaign.status != Campaign.Status.DRAFT:
        return campaign.recipients.count()
    campaign.recipients.all().delete()
    rows = []
    for g in audience(campaign.restaurant, campaign.segment, campaign.channel):
        address = g.email if campaign.channel == Campaign.Channel.EMAIL else g.phone
        rows.append(CampaignRecipient(campaign=campaign, guest=g, address=address,
                                      message=render_message(campaign.message, g, campaign.restaurant, campaign.offer_code)))
    CampaignRecipient.objects.bulk_create(rows, batch_size=500)
    campaign.audience_count = len(rows)
    campaign.save(update_fields=['audience_count'])
    return len(rows)


def unsubscribe_token(guest):
    return signing.dumps({'g': guest.pk}, salt=UNSUB_SALT)


def guest_from_unsubscribe(token):
    try:
        data = signing.loads(token, salt=UNSUB_SALT, max_age=60 * 60 * 24 * 365)
    except signing.BadSignature:
        return None
    return Guest.objects.filter(pk=data.get('g')).first()


def send_email(campaign, base_url, user=None, limit=None):
    """Send pending email recipients. Returns (sent, failed)."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from core.emails import get_mail_connection_and_sender

    restaurant = campaign.restaurant
    connection, from_email, _ = get_mail_connection_and_sender(branch=restaurant.branches.first())
    pending = campaign.recipients.filter(status=CampaignRecipient.Status.PENDING).select_related('guest')
    if limit:
        pending = pending[:limit]
    sent = failed = 0
    for rcpt in pending:
        unsub = base_url.rstrip('/') + f'/g/unsubscribe/{unsubscribe_token(rcpt.guest)}/'
        subject = render_message(campaign.subject or campaign.name, rcpt.guest, restaurant, campaign.offer_code)
        html = render_to_string('guests/email/campaign.html', {'campaign': campaign, 'message': rcpt.message, 'restaurant': restaurant,
                                                               'unsubscribe_url': unsub, 'offer_code': campaign.offer_code})
        try:
            msg = EmailMultiAlternatives(subject, rcpt.message + f'\n\nUnsubscribe: {unsub}', from_email, [rcpt.address], connection=connection,
                                         headers={'List-Unsubscribe': f'<{unsub}>'})
            msg.attach_alternative(html, 'text/html')
            msg.send()
            rcpt.status, rcpt.sent_at, rcpt.sent_by = CampaignRecipient.Status.SENT, timezone.now(), user
            sent += 1
        except Exception as exc:
            rcpt.status, rcpt.error = CampaignRecipient.Status.FAILED, str(exc)[:200]
            failed += 1
        rcpt.save(update_fields=['status', 'sent_at', 'sent_by', 'error'])
    refresh_counts(campaign)
    return sent, failed


def refresh_counts(campaign):
    counts = dict(campaign.recipients.values_list('status').annotate(c=Count('id')))
    campaign.sent_count = counts.get('SENT', 0)
    campaign.failed_count = counts.get('FAILED', 0)
    pending = counts.get('PENDING', 0)
    if campaign.status != Campaign.Status.DRAFT or campaign.sent_count or campaign.failed_count:
        campaign.status = Campaign.Status.SENDING if pending else Campaign.Status.SENT
        if not pending and not campaign.sent_at:
            campaign.sent_at = timezone.now()
    campaign.save(update_fields=['sent_count', 'failed_count', 'status', 'sent_at'])


def wa_link(phone, message):
    digits = services.normalize_phone(phone)
    if len(digits) == 10:
        digits = '91' + digits
    return f'https://wa.me/{digits}?text={quote(message)}'


def sms_link(phone, message):
    return f'sms:{phone}?body={quote(message)}'


# --------------------------------------------------------------------------
# Automations (birthday, anniversary, win-back) — email
# --------------------------------------------------------------------------

def run_automations(restaurant, base_url, today=None):
    from guests.models import MarketingSettings
    today = today or timezone.localdate()
    ms = MarketingSettings.for_restaurant(restaurant)
    results = {}
    guests = Guest.objects.filter(restaurant=restaurant, marketing_opt_in=True).exclude(email='')
    jobs = []
    if ms.birthday_enabled:
        jobs.append(('birthday', 'Birthday wishes', ms.birthday_message, '', guests.filter(birthday__month=today.month, birthday__day=today.day)))
    if ms.anniversary_enabled:
        jobs.append(('anniversary', 'Anniversary wishes', ms.anniversary_message, '', guests.filter(anniversary__month=today.month, anniversary__day=today.day)))
    if ms.winback_enabled and ms.winback_days:
        start = timezone.now() - timedelta(days=ms.winback_days + 1)
        end = timezone.now() - timedelta(days=ms.winback_days)
        recently = CampaignRecipient.objects.filter(campaign__restaurant=restaurant, campaign__automation='winback',
                                                    sent_at__gte=timezone.now() - timedelta(days=180)).values('guest')
        jobs.append(('winback', 'We miss you', ms.winback_message, ms.winback_offer_code,
                     guests.filter(last_seen__gte=start, last_seen__lt=end).exclude(pk__in=recently)))
    for kind, title, template, code, qs in jobs:
        name = f'{title} · {today:%d %b %Y}'
        if Campaign.objects.filter(restaurant=restaurant, automation=kind, name=name).exists():
            continue
        targets = list(qs)
        if not targets:
            continue
        c = Campaign.objects.create(restaurant=restaurant, name=name, channel=Campaign.Channel.EMAIL, segment='', subject=f'{title} from {restaurant.name}',
                                    message=template, offer_code=code, automation=kind, status=Campaign.Status.SENDING, audience_count=len(targets))
        CampaignRecipient.objects.bulk_create([CampaignRecipient(campaign=c, guest=g, address=g.email,
                                                                 message=render_message(template, g, restaurant, code)) for g in targets])
        results[kind] = send_email(c, base_url)
    return results
