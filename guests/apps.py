from django.apps import AppConfig


class GuestsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'guests'
    verbose_name = 'Guests, Loyalty & Gift Cards'

    def ready(self):
        # Keeps guest profiles and loyalty points in sync with orders and stays
        from guests import signals  # noqa: F401
