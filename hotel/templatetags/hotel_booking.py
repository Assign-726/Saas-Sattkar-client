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
