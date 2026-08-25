from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "betpreneur.modules.identity"

    # The label stays "accounts" deliberately. It is what django_migrations,
    # django_content_type and AUTH_USER_MODEL already record in production, and
    # renaming it would buy nothing but cosmetics at the cost of hand-written
    # SQL against the live database. Code reads identity.api either way.
    label = "accounts"
