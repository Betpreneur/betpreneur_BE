from django.apps import AppConfig


class AlgoConfig(AppConfig):
    """Migration history only — this app owns no code.

    Every algo_* table was created by apps/algo/migrations/0001-0039, and the
    modules under betpreneur/modules/ adopted those tables through state-only
    migrations. Deleting this history would leave a fresh database with no
    tables to adopt, so the app stays registered with nothing in it.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.algo"
