from django.apps import AppConfig


class SuperAdminConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'super_admin'
    verbose_name = 'Super Admin'

    def ready(self):
        # Registers sign-in / sign-out / failed sign-in activity logging
        from super_admin import activity  # noqa: F401
