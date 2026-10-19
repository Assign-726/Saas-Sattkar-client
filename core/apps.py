from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Import signals to register post_save hooks
        from core import signals  # noqa: F401
        from core import api_cache
        api_cache.connect()
        from core import checks  # noqa: F401  (registers deploy checks)
