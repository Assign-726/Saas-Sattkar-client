"""Where the Restaurant and Hotel modules draw the line.

One business can run restaurants and hotels side by side. Each admin shell shows only its
own module's data; a few things are deliberately shared because they describe the business
or the guest as a whole (guest profiles, gift cards, campaigns, management staff).
Keep every split in this file so both shells agree.
"""
RESTAURANT = 'restaurant'
HOTEL = 'hotel'
ALL = 'all'

# Ground-staff departments (restaurant_admin.models.Department)
RESTAURANT_DEPARTMENTS = ['KITCHEN', 'BAR', 'FLOOR', 'SECURITY', 'MANAGEMENT']
HOTEL_DEPARTMENTS = ['HOUSEKEEPING', 'FRONT_DESK', 'SECURITY', 'MANAGEMENT']
SHARED_DEPARTMENTS = ['SECURITY', 'MANAGEMENT']

# Payroll attributed in the profit & loss of each module (shared staff only in the consolidated view)
PAYROLL_DEPARTMENTS = {
    RESTAURANT: ['KITCHEN', 'BAR', 'FLOOR'],
    HOTEL: ['HOUSEKEEPING', 'FRONT_DESK'],
}

# Guest feedback "about" areas (guests.models.Feedback.Area)
FEEDBACK_AREAS = {
    RESTAURANT: ['dining', 'delivery', 'event', 'other'],
    HOTEL: ['stay', 'other'],
}

# Integration providers and what each module can grant / subscribe to
PROVIDERS = {
    RESTAURANT: ['zomato', 'swiggy', 'magicpin', 'ondc', 'website', 'custom'],
    HOTEL: ['channel_manager', 'ota', 'website', 'custom'],
}
API_SCOPES = {
    RESTAURANT: ['menu:read', 'menu:write', 'orders:read', 'orders:write'],
    HOTEL: ['rooms:read', 'bookings:read', 'bookings:write'],
}
WEBHOOK_EVENTS = {
    RESTAURANT: ['order.created', 'order.status_changed', 'order.paid', 'order.cancelled', 'menu.item_updated', 'feedback.received'],
    HOTEL: ['booking.created', 'booking.modified', 'booking.checked_in', 'booking.checked_out', 'booking.cancelled', 'ari.changed', 'feedback.received'],
}


def department_choices(module):
    from restaurant_admin.models import Department
    allowed = RESTAURANT_DEPARTMENTS if module == RESTAURANT else HOTEL_DEPARTMENTS
    return [(v, l) for v, l in Department.choices if v in allowed]


def module_of_area(area):
    """The shell an env/area string belongs to ('restaurant' | 'hotel'); anything else is consolidated."""
    return area if area in (RESTAURANT, HOTEL) else ALL
