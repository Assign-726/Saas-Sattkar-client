# Generated migration - only master dashboard fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('super_admin', '0012_restaurantbranch_service_charge_percentage'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='master_license_key',
            field=models.CharField(
                blank=True,
                help_text='Auto-assigned by MasterPanel on onboarding',
                max_length=100,
                null=True,
                unique=True,
                verbose_name='Master License Key',
            ),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='master_sync_status',
            field=models.CharField(
                blank=True,
                default='PENDING',
                help_text='PENDING / SYNCED / FAILED',
                max_length=20,
                verbose_name='Master Sync Status',
            ),
        ),
    ]