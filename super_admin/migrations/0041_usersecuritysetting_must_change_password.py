from django.db import migrations, models


def ensure_column(apps, schema_editor):
    """Some databases already have this column (from an older, removed migration),
    others don't. Add it only when missing so both cases end up consistent."""
    Model = apps.get_model('super_admin', 'UserSecuritySetting')
    table = Model._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        columns = [c.name for c in schema_editor.connection.introspection.get_table_description(cursor, table)]
    if 'must_change_password' not in columns:
        field = models.BooleanField(default=False)
        field.set_attributes_from_name('must_change_password')
        schema_editor.add_field(Model, field)
    else:
        schema_editor.execute(f'UPDATE {schema_editor.quote_name(table)} SET must_change_password = %s WHERE must_change_password IS NULL', [False])


class Migration(migrations.Migration):

    dependencies = [
        ('super_admin', '0040_restaurantbranch_is_setup_completed'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='usersecuritysetting',
                    name='must_change_password',
                    field=models.BooleanField(default=False, help_text='Force the user to set a new password on next login'),
                ),
            ],
            database_operations=[
                migrations.RunPython(ensure_column, migrations.RunPython.noop),
            ],
        ),
    ]
