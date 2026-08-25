from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "betpreneur.modules.billing"
    label = "billing"

    def ready(self) -> None:
        # Subscribing here rather than at import time keeps ordering predictable.
        from .handlers import register

        register()
