from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'integrations'
    verbose_name = 'Integrations & API'

    def ready(self):
        from integrations import signals  # noqa: F401
        from integrations.delivery import signals as delivery_signals  # noqa: F401
