from django.apps import AppConfig


class SlipsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "betpreneur.modules.slips"
    label = "slips"

    def ready(self) -> None:
        from .handlers import register

        register()
