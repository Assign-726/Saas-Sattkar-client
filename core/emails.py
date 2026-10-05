import logging
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.conf import settings
from super_admin.models import Restaurant

logger = logging.getLogger(__name__)


def mask_email(email_str):
    """
    Masks an email address for privacy display.
    Example: 'ritik.kumar@example.com' -> 'r***r@example.com'
             'admin@restro.com' -> 'a***n@restro.com'
    """
    if not email_str or '@' not in email_str:
        return email_str or ''
    
    parts = email_str.split('@')
    name_part = parts[0]
    domain_part = parts[1]
    
    if len(name_part) <= 2:
        masked_name = name_part[0] + '***'
    else:
        masked_name = name_part[0] + '***' + name_part[-1]
        
    return f"{masked_name}@{domain_part}"


def get_mail_connection_and_sender(branch=None, hotel_settings=None, sender_name_override=None):
    """
    Dynamically resolves the SMTP connection and 'from_email' address with multi-tier fallback:
    1. If hotel_settings is provided and hotel_settings.email_is_active:
       Uses hotel_settings credentials.
    2. Else if branch is provided and branch.email_is_active:
       Uses branch credentials.
    3. Fallback:
       Uses global Django settings (settings.EMAIL_HOST_USER, settings.DEFAULT_FROM_EMAIL).
       
    Returns: (connection, from_email, source_description)
    """
    global_backend = getattr(settings, 'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
    # Preserve mock/in-memory test or console backends when explicitly configured in Django settings
    smtp_backend = (
        global_backend
        if any(b in global_backend for b in ['locmem', 'console', 'dummy', 'filebased'])
        else 'django.core.mail.backends.smtp.EmailBackend'
    )

    # 1. Check HotelSettings
    if hotel_settings and getattr(hotel_settings, 'email_is_active', False):
        user_email = (hotel_settings.email_host_user or '').strip()
        pwd = (hotel_settings.email_host_password or '').strip()
        if user_email and pwd:
            host = hotel_settings.email_host or 'smtp.gmail.com'
            port = hotel_settings.email_port or 587
            use_tls = hotel_settings.email_use_tls
            use_ssl = hotel_settings.email_use_ssl
            sender_name = sender_name_override or hotel_settings.email_sender_name or hotel_settings.hotel_name or "Hotel Management"
            from_email = f'"{sender_name}" <{user_email}>'
            try:
                conn = get_connection(
                    backend=smtp_backend,
                    host=host,
                    port=port,
                    username=user_email,
                    password=pwd,
                    use_tls=use_tls,
                    use_ssl=use_ssl,
                    timeout=10,
                )
                return conn, from_email, f"Hotel Custom SMTP ({user_email})"
            except Exception as e:
                logger.warning(f"Could not build hotel SMTP connection: {e}. Falling back.")

    # 2. Check RestaurantBranch
    if branch and getattr(branch, 'email_is_active', False):
        user_email = (branch.email_host_user or '').strip()
        pwd = (branch.email_host_password or '').strip()
        if user_email and pwd:
            host = branch.email_host or 'smtp.gmail.com'
            port = branch.email_port or 587
            use_tls = branch.email_use_tls
            use_ssl = branch.email_use_ssl
            sender_name = sender_name_override or branch.email_sender_name or branch.name or "Restaurant Branch"
            from_email = f'"{sender_name}" <{user_email}>'
            try:
                conn = get_connection(
                    backend=smtp_backend,
                    host=host,
                    port=port,
                    username=user_email,
                    password=pwd,
                    use_tls=use_tls,
                    use_ssl=use_ssl,
                    timeout=10,
                )
                return conn, from_email, f"Branch Custom SMTP ({user_email})"
            except Exception as e:
                logger.warning(f"Could not build branch SMTP connection: {e}. Falling back.")

    # 3. Global Fallback
    fallback_from = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost')
    if sender_name_override and '<' not in fallback_from:
        fallback_from = f'"{sender_name_override}" <{fallback_from}>'
    return get_connection(), fallback_from, "System Global SMTP"


def send_test_email(host, port, username, password, use_tls, use_ssl, recipient_email, sender_name=None):
    """
    Attempts to establish an SMTP connection with the given credentials and dispatches a test message.
    Returns: (success: bool, message: str)
    """
    if not recipient_email or '@' not in recipient_email:
        return False, "Please provide a valid recipient email address."

    if not username or not password:
        return False, "SMTP Username and Password (App Password) are required."

    sender_display = sender_name or "Test Mailer"
    from_email = f'"{sender_display}" <{username.strip()}>'

    try:
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=host.strip() if host else 'smtp.gmail.com',
            port=int(port) if port else 587,
            username=username.strip(),
            password=password.strip(),
            use_tls=bool(use_tls),
            use_ssl=bool(use_ssl),
            timeout=12,
        )

        subject = f"✅ Test Email from {sender_display}"
        body_txt = (
            f"Hello,\n\n"
            f"This is a test email sent from {sender_display}.\n\n"
            f"Your custom SMTP mailing credentials are properly configured and operational!\n\n"
            f"Configuration Details:\n"
            f"• SMTP Server: {host}:{port}\n"
            f"• Sender Address: {username}\n"
            f"• Security: {'SSL' if use_ssl else ('TLS' if use_tls else 'None')}\n\n"
            f"Best Regards,\n{sender_display}"
        )

        body_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 550px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background: #ffffff;">
            <div style="text-align: center; margin-bottom: 20px;">
                <div style="display: inline-block; width: 48px; height: 48px; line-height: 48px; background: #dcfce7; color: #15803d; border-radius: 50%; font-size: 24px; font-weight: bold;">✓</div>
                <h2 style="color: #0f172a; margin: 12px 0 4px 0;">SMTP Test Successful!</h2>
                <p style="color: #64748b; font-size: 14px; margin: 0;">Your custom outgoing mail service is verified and working.</p>
            </div>
            <div style="background: #f8fafc; border-radius: 8px; padding: 16px; margin-bottom: 20px; font-size: 13.5px; color: #334155;">
                <p style="margin: 0 0 8px 0;"><strong>Sender Name:</strong> {sender_display}</p>
                <p style="margin: 0 0 8px 0;"><strong>SMTP Server:</strong> {host}:{port}</p>
                <p style="margin: 0 0 8px 0;"><strong>User Account:</strong> {username}</p>
                <p style="margin: 0;"><strong>Security Protocol:</strong> {'SSL' if use_ssl else ('TLS' if use_tls else 'None')}</p>
            </div>
            <p style="color: #64748b; font-size: 12px; text-align: center; margin: 0;">This email was dispatched via live SMTP test from your management settings.</p>
        </div>
        """

        msg = EmailMultiAlternatives(subject, body_txt, from_email, [recipient_email.strip()], connection=connection)
        msg.attach_alternative(body_html, "text/html")
        msg.send(fail_silently=False)
        return True, f"Test email sent successfully to {recipient_email}!"

    except Exception as exc:
        logger.error(f"Test email failed: {exc}", exc_info=True)
        return False, f"SMTP Connection Failed: {str(exc)}"


def send_otp_email(user, otp_code, purpose="login", request=None):
    """
    Sends an OTP verification email to the user.
    Dynamically routes through branch/hotel custom SMTP if available, or falls back to system SMTP.
    """
    if not user or not user.email:
        logger.warning(f"Cannot send OTP: user '{getattr(user, 'username', 'None')}' has no email configured.")
        return False, "User has no email address configured."

    # Resolve branding & branch context
    app_name = "Hotel Management System"
    restaurant_name = None
    branch = getattr(user, 'branch', None)
    hotel_settings = None

    try:
        profile = getattr(user, 'restaurant_profile', None)
        if profile and profile.restaurant:
            restaurant_name = profile.restaurant.name
            app_name = f"{restaurant_name} Portal"
            if not branch and profile.branch:
                branch = profile.branch
            elif not branch:
                branch = profile.restaurant.branches.first()

        # Check if user has hotel role
        role = getattr(user, 'role', None) or (profile.role if profile else None)
        if branch and hasattr(branch, 'hotel_settings'):
            if role in ['HOTEL_ADMIN', 'FRONT_DESK', 'HOUSEKEEPING']:
                hotel_settings = branch.hotel_settings
                if hotel_settings.hotel_name:
                    app_name = f"{hotel_settings.hotel_name} PMS"
    except Exception:
        pass

    if purpose == "login":
        subject = f"Your Login Verification Code: {otp_code} - {app_name}"
        template_html = "emails/login_otp.html"
        template_txt = "emails/login_otp.txt"
    elif purpose == "password_reset":
        subject = f"Your Password Reset Code: {otp_code} - {app_name}"
        template_html = "emails/password_reset_otp.html"
        template_txt = "emails/password_reset_otp.txt"
    else:
        subject = f"Your Verification Code: {otp_code} - {app_name}"
        template_html = "emails/login_otp.html"
        template_txt = "emails/login_otp.txt"

    context = {
        'user': user,
        'otp_code': otp_code,
        'app_name': app_name,
        'restaurant_name': restaurant_name,
        'expiry_minutes': getattr(settings, 'OTP_EXPIRY_MINUTES', 10),
        'support_email': getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@restro.com'),
    }

    try:
        html_content = render_to_string(template_html, context)
    except Exception as e:
        logger.warning(f"Could not render HTML email template {template_html}: {e}. Falling back to default text.")
        html_content = None

    try:
        text_content = render_to_string(template_txt, context)
    except Exception:
        text_content = (
            f"Hello {user.first_name or user.username},\n\n"
            f"Your verification code is: {otp_code}\n\n"
            f"This code will expire in {getattr(settings, 'OTP_EXPIRY_MINUTES', 10)} minutes.\n"
            f"If you did not request this, please ignore this email.\n\n"
            f"Regards,\n{app_name}"
        )

    # Dynamically resolve SMTP connection and sender
    connection, from_email, source = get_mail_connection_and_sender(
        branch=branch,
        hotel_settings=hotel_settings,
        sender_name_override=app_name
    )
    logger.info(f"Dispatching OTP email for {user.username} via {source}")

    recipient_list = [user.email]

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, recipient_list, connection=connection)
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        return True, None
    except Exception as exc:
        logger.error(f"Failed to send OTP email to {user.email} via {source}: {exc}", exc_info=True)
        # Attempt fallback to system SMTP if branch/hotel SMTP failed
        if source != "System Global SMTP":
            try:
                logger.info(f"Retrying OTP email dispatch for {user.username} using System Global SMTP fallback.")
                fallback_from = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost')
                fallback_msg = EmailMultiAlternatives(subject, text_content, fallback_from, recipient_list)
                if html_content:
                    fallback_msg.attach_alternative(html_content, "text/html")
                fallback_msg.send(fail_silently=False)
                return True, None
            except Exception as retry_exc:
                logger.error(f"Fallback dispatch also failed: {retry_exc}")
        return False, f"Could not deliver email: {str(exc)}"


def send_event_ticket_email(booking, request=None):
    """
    Dispatches a branded digital entry pass email with QR code to the guest.
    Dynamically routes through branch's custom SMTP configuration if available,
    or falls back to the system global SMTP.
    """
    if not booking or not getattr(booking, 'guest_email', None):
        logger.info(f"Skipping event ticket email: booking '{getattr(booking, 'booking_reference', 'None')}' has no email configured.")
        return False, "No guest email provided"

    guest_email = (booking.guest_email or '').strip()
    if '@' not in guest_email or guest_email.endswith('.local') or guest_email.endswith('.internal'):
        logger.info(f"Skipping event ticket email for {booking.booking_reference}: '{guest_email}' is internal/mock.")
        return False, "Internal or invalid guest email address."

    # Ensure QR code exists
    if not booking.booking_qr_code:
        try:
            booking.generate_qr_code(force=True)
            booking.save(update_fields=['booking_qr_code'])
        except Exception as e:
            logger.warning(f"Could not generate QR code for {booking.booking_reference}: {e}")

    event = booking.event
    branch = event.branch if event else None
    sender_name = (branch.email_sender_name if branch else None) or (branch.name if branch else None) or (event.title if event else "Club & Events Desk")

    # Dates and times
    start_dt = event.start_datetime if event else None
    end_dt = event.end_datetime if event else None
    event_date = start_dt.strftime("%A, %d %B %Y") if start_dt else "Date TBA"
    start_time = start_dt.strftime("%I:%M %p") if start_dt else ""
    end_time = end_dt.strftime("%I:%M %p") if end_dt else ""

    venue_name = (event.venue_name if event and event.venue_name else None) or (branch.name if branch else "Event Venue")
    venue_address = (event.venue_address if event and event.venue_address else None) or (branch.address if branch else "")

    # Frontend pass link
    frontend_base = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    pass_url = f"{frontend_base}/events/pass/{booking.booking_reference}"

    # Check for QR image bytes
    qr_bytes = None
    if booking.booking_qr_code:
        try:
            booking.booking_qr_code.open('rb')
            qr_bytes = booking.booking_qr_code.read()
            booking.booking_qr_code.close()
        except Exception as qr_err:
            logger.warning(f"Could not read QR code file for {booking.booking_reference}: {qr_err}")

    subject = f"🎟️ Entry Pass Confirmed: {event.title} (Ref: {booking.booking_reference})"
    template_html = "emails/event_ticket.html"
    template_txt = "emails/event_ticket.txt"

    context = {
        'booking': booking,
        'event': event,
        'ticket_type': booking.ticket_type,
        'guest_name': booking.guest_name,
        'booking_reference': booking.booking_reference,
        'security_token': getattr(booking, 'security_token', ''),
        'quantity': booking.quantity,
        'total_amount': booking.total_amount,
        'payment_method_display': booking.get_payment_method_display(),
        'event_date': event_date,
        'start_time': start_time,
        'end_time': end_time,
        'venue_name': venue_name,
        'venue_address': venue_address,
        'pass_url': pass_url,
        'has_qr_cid': bool(qr_bytes),
    }

    try:
        html_content = render_to_string(template_html, context)
    except Exception as e:
        logger.warning(f"Could not render HTML email template {template_html}: {e}. Falling back to default text.")
        html_content = None

    try:
        text_content = render_to_string(template_txt, context)
    except Exception:
        text_content = (
            f"Hello {booking.guest_name},\n\n"
            f"Your entry pass for {event.title} has been confirmed!\n"
            f"Booking Reference: {booking.booking_reference}\n"
            f"Pass Tier: {booking.ticket_type.name}\n"
            f"Quantity: {booking.quantity}\n"
            f"Total: ₹{booking.total_amount}\n\n"
            f"View your digital pass online: {pass_url}\n\n"
            f"Regards,\n{venue_name}"
        )

    connection, from_email, source = get_mail_connection_and_sender(
        branch=branch,
        sender_name_override=sender_name
    )
    logger.info(f"Dispatching Event Ticket email for {booking.booking_reference} to {guest_email} via {source}")

    recipient_list = [guest_email]

    def _build_and_send(conn, sender):
        msg = EmailMultiAlternatives(subject, text_content, sender, recipient_list, connection=conn)
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        if qr_bytes:
            from email.mime.image import MIMEImage
            qr_mime = MIMEImage(qr_bytes)
            qr_mime.add_header('Content-ID', '<ticket_qr>')
            qr_mime.add_header('Content-Disposition', 'inline', filename=f"ticket_{booking.booking_reference}.png")
            msg.attach(qr_mime)
        msg.send(fail_silently=False)

    try:
        _build_and_send(connection, from_email)
        return True, None
    except Exception as exc:
        logger.error(f"Failed to send Event Ticket email to {guest_email} via {source}: {exc}", exc_info=True)
        if source != "System Global SMTP":
            try:
                logger.info(f"Retrying Event Ticket dispatch for {booking.booking_reference} using System Global SMTP fallback.")
                fallback_from = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost')
                if sender_name and '<' not in fallback_from:
                    fallback_from = f'"{sender_name}" <{fallback_from}>'
                _build_and_send(get_connection(), fallback_from)
                return True, None
            except Exception as retry_exc:
                logger.error(f"Fallback dispatch for event ticket also failed: {retry_exc}")
        return False, f"Could not deliver ticket email: {str(exc)}"


def send_hotel_booking_email(room_booking, request=None):
    """
    Dispatches a branded hotel reservation confirmation voucher to the guest.
    Dynamically routes through hotel's custom SMTP configuration if available,
    or falls back to the system global SMTP.
    """
    if not room_booking or not getattr(room_booking, 'guest_email', None):
        logger.info(f"Skipping hotel voucher email: booking '{getattr(room_booking, 'booking_id', 'None')}' has no email configured.")
        return False, "No guest email provided"

    guest_email = (room_booking.guest_email or '').strip()
    if '@' not in guest_email or guest_email.endswith('.internal') or guest_email.endswith('.local'):
        logger.info(f"Skipping hotel voucher email for {room_booking.booking_id}: '{guest_email}' is internal/mock.")
        return False, "Internal or invalid guest email address."

    branch = room_booking.branch
    h_settings = getattr(branch, 'hotel_settings', None)

    hotel_name = (h_settings.hotel_name if h_settings and h_settings.hotel_name else None) or (branch.name if branch else "Hotel & Suites")
    sender_name = (h_settings.email_sender_name if h_settings and h_settings.email_sender_name else None) or hotel_name

    check_in_date = room_booking.check_in_date.strftime("%A, %d %B %Y") if room_booking.check_in_date else ""
    check_out_date = room_booking.check_out_date.strftime("%A, %d %B %Y") if room_booking.check_out_date else ""
    check_in_time = h_settings.check_in_time.strftime("%I:%M %p") if (h_settings and h_settings.check_in_time) else "02:00 PM"
    check_out_time = h_settings.check_out_time.strftime("%I:%M %p") if (h_settings and h_settings.check_out_time) else "11:00 AM"

    hotel_address = (h_settings.address if h_settings and h_settings.address else None) or (branch.address if branch else "")
    hotel_phone = (h_settings.contact_phone if h_settings and h_settings.contact_phone else None) or (branch.phone if branch else "")
    hotel_email = (h_settings.contact_email if h_settings and h_settings.contact_email else None) or (branch.email if branch else "")
    cancellation_policy = h_settings.cancellation_policy if h_settings else ""
    gstin = h_settings.gstin if h_settings else ""

    category_name = room_booking.room.category.name if (room_booking.room and room_booking.room.category) else "Suite"
    room_number = room_booking.room.room_number if room_booking.room else "Assigned upon arrival"

    cabs = list(room_booking.cab_bookings.all()) if hasattr(room_booking, 'cab_bookings') else []

    subject = f"🏨 Reservation Confirmed: {room_booking.booking_id} - {hotel_name}"
    template_html = "emails/hotel_reservation.html"
    template_txt = "emails/hotel_reservation.txt"

    context = {
        'booking': room_booking,
        'hotel_name': hotel_name,
        'hotel_address': hotel_address,
        'hotel_phone': hotel_phone,
        'hotel_email': hotel_email,
        'gstin': gstin,
        'guest_name': room_booking.guest_name,
        'check_in_date': check_in_date,
        'check_out_date': check_out_date,
        'check_in_time': check_in_time,
        'check_out_time': check_out_time,
        'nights': room_booking.nights_count,
        'num_guests': room_booking.num_guests,
        'category_name': category_name,
        'room_number': room_number,
        'subtotal': room_booking.total_room_charges,
        'taxes': room_booking.taxes,
        'grand_total': room_booking.grand_total,
        'payment_status': room_booking.payment_status,
        'payment_method': room_booking.payment_method,
        'is_paid': (room_booking.payment_status == 'PAID'),
        'cancellation_policy': cancellation_policy,
        'cabs': cabs,
    }

    try:
        html_content = render_to_string(template_html, context)
    except Exception as e:
        logger.warning(f"Could not render HTML email template {template_html}: {e}. Falling back to default text.")
        html_content = None

    try:
        text_content = render_to_string(template_txt, context)
    except Exception:
        text_content = (
            f"Dear {room_booking.guest_name},\n\n"
            f"Your reservation at {hotel_name} is confirmed!\n"
            f"Booking Reference: {room_booking.booking_id}\n"
            f"Check-In: {check_in_date} ({check_in_time})\n"
            f"Check-Out: {check_out_date} ({check_out_time})\n"
            f"Suite: {category_name} (Room: {room_number})\n"
            f"Total: ₹{room_booking.grand_total} ({room_booking.payment_status})\n\n"
            f"Regards,\n{hotel_name}"
        )

    connection, from_email, source = get_mail_connection_and_sender(
        branch=branch,
        hotel_settings=h_settings,
        sender_name_override=sender_name
    )
    logger.info(f"Dispatching Hotel Reservation voucher for {room_booking.booking_id} to {guest_email} via {source}")

    recipient_list = [guest_email]

    def _build_and_send(conn, sender):
        msg = EmailMultiAlternatives(subject, text_content, sender, recipient_list, connection=conn)
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    try:
        _build_and_send(connection, from_email)
        return True, None
    except Exception as exc:
        logger.error(f"Failed to send Hotel Reservation email to {guest_email} via {source}: {exc}", exc_info=True)
        if source != "System Global SMTP":
            try:
                logger.info(f"Retrying Hotel Reservation dispatch for {room_booking.booking_id} using System Global SMTP fallback.")
                fallback_from = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost')
                if sender_name and '<' not in fallback_from:
                    fallback_from = f'"{sender_name}" <{fallback_from}>'
                _build_and_send(get_connection(), fallback_from)
                return True, None
            except Exception as retry_exc:
                logger.error(f"Fallback dispatch for hotel reservation also failed: {retry_exc}")
        return False, f"Could not deliver reservation email: {str(exc)}"

