"""Keep guest profiles and loyalty points in step with orders, stays, reservations and tickets.

Work runs after the surrounding transaction commits and never raises, so a
CRM hiccup can't block a sale or a check-in.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Order
from events.models import EventBooking
from hotel.models import RoomBooking
from reservations.models import Reservation

from guests import pos, services

logger = logging.getLogger(__name__)


def _later(fn):
    def run():
        try:
            fn()
        except Exception:
            logger.exception('Guest sync failed')
    transaction.on_commit(run)


@receiver(post_save, sender=Order)
def order_saved(sender, instance, **kwargs):
    order = instance
    if order.status == 'DRAFT':
        return
    restaurant = order.restaurant or getattr(order.branch, 'restaurant', None)

    def sync():
        guest = services.upsert_guest(restaurant, order.customer_name, order.phone)
        if not guest:
            return
        services.refresh_guest_stats(guest)
        ref = str(order.pk)
        if order.status == 'C' or order.payment_status == 'refunded':
            services.reverse_points(guest, 'order', ref, f'Order {order.order_number} cancelled')
        elif order.payment_status == 'paid':
            # Points spent on the bill do not earn new points
            services.award_points(guest, max(0, order.total_amount - pos.tender_total(order, 'points')), 'order', ref, f'Order {order.order_number}')

    def give_back():
        if order.status == 'C' or order.payment_status == 'refunded':
            pos.restore_tenders(order)

    _later(give_back)
    if not services.is_walk_in(order.customer_name, order.phone, ''):
        _later(sync)


@receiver(post_save, sender=RoomBooking)
def stay_saved(sender, instance, **kwargs):
    booking = instance
    restaurant = getattr(booking.branch, 'restaurant', None)

    def sync():
        guest = services.upsert_guest(restaurant, booking.guest_name, booking.guest_phone, booking.guest_email)
        if not guest:
            return
        services.refresh_guest_stats(guest)
        if booking.booking_status == 'CANCELLED':
            services.reverse_points(guest, 'stay', booking.booking_id, f'Stay {booking.booking_id} cancelled')
        elif booking.booking_status == 'CHECKED_OUT':
            from guests.models import LoyaltySettings
            if LoyaltySettings.for_restaurant(restaurant).earn_on_hotel_stays:
                services.award_points(guest, booking.grand_total, 'stay', booking.booking_id, f'Stay {booking.booking_id}')
    _later(sync)


@receiver(post_save, sender=Reservation)
def reservation_saved(sender, instance, **kwargs):
    res = instance

    def sync():
        guest = services.upsert_guest(res.restaurant, res.customer_name, res.phone, res.email)
        if guest:
            services.refresh_guest_stats(guest)
    _later(sync)


@receiver(post_save, sender=EventBooking)
def ticket_saved(sender, instance, **kwargs):
    booking = instance

    def sync():
        restaurant = getattr(getattr(booking.event, 'branch', None), 'restaurant', None)
        guest = services.upsert_guest(restaurant, booking.guest_name, booking.guest_phone, booking.guest_email)
        if guest:
            services.refresh_guest_stats(guest)
    _later(sync)
