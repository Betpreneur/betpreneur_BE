"""Point the token content types at billing.

django_content_type rows carry an app_label. Moving the models between apps
leaves those rows saying "algo", which orphans admin log entries and any
permission lookup that goes through the content type. Django does not do this
automatically for a cross-app move, so it is done here.

Permission rows follow via their content_type FK, and their codenames are
model-scoped rather than app-scoped, so they stay valid untouched.
"""
from django.db import migrations

MODELS = ["tokenwallet", "tokentransaction", "tokenreservation", "tokenpurchase"]


def _move(apps, from_label, to_label):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in MODELS:
        # Guard against a row already existing under the target label, which
        # would violate the (app_label, model) unique constraint.
        if ContentType.objects.filter(app_label=to_label, model=model).exists():
            ContentType.objects.filter(app_label=from_label, model=model).delete()
            continue
        ContentType.objects.filter(app_label=from_label, model=model).update(
            app_label=to_label
        )


def forwards(apps, schema_editor):
    _move(apps, "algo", "billing")


def backwards(apps, schema_editor):
    _move(apps, "billing", "algo")


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_move_tokens_to_billing"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
