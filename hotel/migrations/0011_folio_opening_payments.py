from django.db import migrations

METHOD = {'ONLINE_RAZORPAY': 'ONLINE', 'RAZORPAY': 'ONLINE', 'STRIPE': 'ONLINE', 'UPI': 'UPI', 'UPI_DIRECT': 'UPI',
          'CARD': 'CARD', 'CASH': 'CASH', 'BANK': 'BANK', 'OTA': 'OTA'}


def forwards(apps, schema_editor):
    """Bookings already marked paid get one payment line so their folio balances to zero."""
    RoomBooking = apps.get_model('hotel', 'RoomBooking')
    FolioPayment = apps.get_model('hotel', 'FolioPayment')
    RoomBooking.objects.filter(razorpay_order_id__isnull=False).exclude(razorpay_order_id='').update(source='WEBSITE')
    for b in RoomBooking.objects.filter(payment_status='PAID', grand_total__gt=0).iterator():
        FolioPayment.objects.create(booking=b, method=METHOD.get((b.payment_method or '').upper(), 'CASH'), amount=b.grand_total,
                                    reference=b.razorpay_payment_id or b.upi_transaction_ref or '', note='Paid at booking',
                                    received_at=b.updated_at)


class Migration(migrations.Migration):
    dependencies = [('hotel', '0010_pms_folio_housekeeping_rates_audit')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
