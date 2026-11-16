from django import template

from hotel.models import Room

register = template.Library()


@register.simple_tag
def bookable_rooms(branch):
    """Active rooms for the header's New Reservation modal, on every hotel page."""
    if not branch:
        return Room.objects.none()
    return (Room.objects.filter(branch=branch, is_active=True)
            .select_related('category').order_by('category__name', 'room_number'))


@register.simple_tag(takes_context=True)
def reservation_draft(context):
    """Values + field errors of a New Reservation form the server rejected (shown once, then cleared)."""
    request = context.get('request')
    if not request or not hasattr(request, 'session'):
        return {}
    return request.session.pop('nb_draft', None) or {}


@register.simple_tag
def hotel_rules(branch):
    """The branch's HotelSettings (front-desk rules such as the guest ID requirement)."""
    from hotel.models import HotelSettings
    return HotelSettings.get_settings(branch) if branch else None
