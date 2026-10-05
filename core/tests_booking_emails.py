from decimal import Decimal
import datetime
from django.test import TestCase, override_settings
from django.core import mail
from django.utils import timezone
from super_admin.models import Restaurant, RestaurantBranch
from events.models import ClubEvent, TicketType, EventBooking
from hotel.models import RoomCategory, Room, RoomBooking, HotelSettings, CabBooking, CabServicePackage
from core.emails import send_event_ticket_email, send_hotel_booking_email


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='system@sharmahotels.com'
)
class BookingEmailsTestCase(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="The Grand Heritage",
            slug="grand-heritage",
            email="info@grandheritage.com"
        )
        self.branch = RestaurantBranch.objects.create(
            restaurant=self.restaurant,
            name="Grand Heritage Palace",
            email_is_active=True,
            email_sender_name="Grand Club Desk",
            email_host_user="club@grandheritage.com",
            email_host_password="secretpassword",
            email_host="smtp.grandheritage.com",
            has_club_module=True,
            has_hotel_module=True,
        )
        self.hotel_settings = HotelSettings.objects.create(
            branch=self.branch,
            hotel_name="Grand Heritage Palace & Suites",
            contact_email="reservations@grandheritage.com",
            contact_phone="+91 141 2345678",
            address="JLN Marg, Jaipur, Rajasthan",
            email_is_active=True,
            email_sender_name="Grand Heritage Concierge",
            email_host_user="reservations@grandheritage.com",
            email_host_password="hotelpassword",
            check_in_time=datetime.time(14, 0),
            check_out_time=datetime.time(11, 0),
        )

    def test_send_event_ticket_email_success(self):
        # 1. Create Club Event & Ticket Tier
        event = ClubEvent.objects.create(
            branch=self.branch,
            title="Royal Masquerade Gala",
            start_datetime=timezone.now() + datetime.timedelta(days=7),
            end_datetime=timezone.now() + datetime.timedelta(days=7, hours=4),
            venue_name="Grand Ballroom",
            venue_address="Level 2, Grand Heritage Palace",
            is_published=True,
        )
        ticket = TicketType.objects.create(
            event=event,
            name="VIP Platinum Access",
            base_price=Decimal('2500.00'),
            total_allocated=100,
            quantity_sold=0,
            is_active=True,
        )
        booking = EventBooking.objects.create(
            booking_reference="TKT-GALA-8899",
            event=event,
            ticket_type=ticket,
            guest_name="Ritik Sharma",
            guest_email="ritik.guest@example.com",
            guest_phone="+91 9876543210",
            quantity=2,
            unit_price=Decimal('2500.00'),
            total_amount=Decimal('5000.00'),
            payment_status=EventBooking.PaymentStatus.SUCCESS,
            payment_method=EventBooking.PaymentMethod.ONLINE,
        )

        mail.outbox.clear()
        success, err = send_event_ticket_email(booking)
        self.assertTrue(success, f"Expected email to succeed, got error: {err}")
        self.assertEqual(len(mail.outbox), 1)

        sent_mail = mail.outbox[0]
        self.assertIn("Royal Masquerade Gala", sent_mail.subject)
        self.assertIn("TKT-GALA-8899", sent_mail.subject)
        self.assertEqual(sent_mail.to, ["ritik.guest@example.com"])
        self.assertIn("club@grandheritage.com", sent_mail.from_email)
        self.assertIn("Grand Club Desk", sent_mail.from_email)

        # Check HTML and alternative text
        self.assertIn("VIP Platinum Access", sent_mail.body)
        self.assertIn("TKT-GALA-8899", sent_mail.body)
        self.assertEqual(len(sent_mail.alternatives), 1)
        html_body, mime_type = sent_mail.alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Royal Masquerade Gala", html_body)
        self.assertIn("TKT-GALA-8899", html_body)
        self.assertIn("Grand Ballroom", html_body)

    def test_send_event_ticket_email_skips_internal_mock_emails(self):
        event = ClubEvent.objects.create(
            branch=self.branch,
            title="Sunset Sessions",
            start_datetime=timezone.now() + datetime.timedelta(days=2),
            end_datetime=timezone.now() + datetime.timedelta(days=2, hours=3),
            is_published=True,
        )
        ticket = TicketType.objects.create(
            event=event,
            name="General Entry",
            base_price=Decimal('1000.00'),
            total_allocated=50,
            quantity_sold=0,
            is_active=True,
        )
        booking = EventBooking.objects.create(
            booking_reference="TKT-WALK-1001",
            event=event,
            ticket_type=ticket,
            guest_name="Walkin Guest",
            guest_email="walkin@venue.local",
            guest_phone="+91 9999999999",
            quantity=1,
            unit_price=Decimal('1000.00'),
            total_amount=Decimal('1000.00'),
            payment_status=EventBooking.PaymentStatus.SUCCESS,
            payment_method=EventBooking.PaymentMethod.DOOR_CASH,
        )

        mail.outbox.clear()
        success, msg = send_event_ticket_email(booking)
        self.assertFalse(success)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_hotel_booking_email_success(self):
        category = RoomCategory.objects.create(
            branch=self.branch,
            name="Royal Maharaja Suite",
            base_price_per_night=Decimal('6000.00'),
            max_occupancy=3,
            is_active=True,
        )
        room = Room.objects.create(
            branch=self.branch,
            category=category,
            room_number="302",
            status=Room.RoomStatus.AVAILABLE,
            is_active=True,
        )
        check_in = timezone.now().date() + datetime.timedelta(days=3)
        check_out = check_in + datetime.timedelta(days=2)

        room_booking = RoomBooking.objects.create(
            booking_id="BK-HTL-TEST001",
            room=room,
            branch=self.branch,
            guest_name="Mr. Amit Verma",
            guest_email="amit.verma@example.com",
            guest_phone="+91 9811122233",
            check_in_date=check_in,
            check_out_date=check_out,
            num_guests=2,
            total_room_charges=Decimal('12000.00'),
            taxes=Decimal('1440.00'),
            grand_total=Decimal('13440.00'),
            booking_status=RoomBooking.BookingStatus.RESERVED,
            payment_status=RoomBooking.PaymentStatus.PAID,
            payment_method="ONLINE_RAZORPAY",
        )

        mail.outbox.clear()
        success, err = send_hotel_booking_email(room_booking)
        self.assertTrue(success, f"Expected hotel voucher email to succeed, got error: {err}")
        self.assertEqual(len(mail.outbox), 1)

        sent_mail = mail.outbox[0]
        self.assertIn("BK-HTL-TEST001", sent_mail.subject)
        self.assertIn("Grand Heritage Palace & Suites", sent_mail.subject)
        self.assertEqual(sent_mail.to, ["amit.verma@example.com"])
        self.assertIn("reservations@grandheritage.com", sent_mail.from_email)
        self.assertIn("Grand Heritage Concierge", sent_mail.from_email)

        self.assertIn("BK-HTL-TEST001", sent_mail.body)
        self.assertIn("Royal Maharaja Suite", sent_mail.body)
        self.assertIn("13440.00", sent_mail.body)

        html_body, mime_type = sent_mail.alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("BK-HTL-TEST001", html_body)
        self.assertIn("Royal Maharaja Suite", html_body)
        self.assertIn("Room 302", html_body)
        self.assertIn("Mandatory Photo ID", html_body)

    def test_send_hotel_booking_email_skips_internal_email(self):
        category = RoomCategory.objects.create(
            branch=self.branch,
            name="Deluxe Room",
            base_price_per_night=Decimal('3000.00'),
            is_active=True,
        )
        room = Room.objects.create(
            branch=self.branch,
            category=category,
            room_number="101",
            status=Room.RoomStatus.AVAILABLE,
        )
        room_booking = RoomBooking.objects.create(
            booking_id="BK-HTL-MOCK99",
            room=room,
            branch=self.branch,
            guest_name="Walk-in Guest",
            guest_email="guest.432101@sharmahotel.internal",
            guest_phone="9876432101",
            check_in_date=timezone.now().date(),
            check_out_date=timezone.now().date() + datetime.timedelta(days=1),
        )

        mail.outbox.clear()
        success, msg = send_hotel_booking_email(room_booking)
        self.assertFalse(success)
        self.assertEqual(len(mail.outbox), 0)
