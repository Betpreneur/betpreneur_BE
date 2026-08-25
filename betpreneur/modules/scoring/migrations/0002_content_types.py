"""Point the fitted-model content types at scoring."""
from django.db import migrations

MODELS = [
    "leaguescoremodel",
    "teamstrength",
    "teamrateprofile",
    "fixturelineup",
    "playeravailability",
]


def _move(apps, from_label, to_label):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in MODELS:
        if ContentType.objects.filter(app_label=to_label, model=model).exists():
            ContentType.objects.filter(app_label=from_label, model=model).delete()
            continue
        ContentType.objects.filter(app_label=from_label, model=model).update(app_label=to_label)


def forwards(apps, schema_editor):
    _move(apps, "algo", "scoring")


def backwards(apps, schema_editor):
    _move(apps, "scoring", "algo")


class Migration(migrations.Migration):
    dependencies = [
        ("scoring", "0001_move_to_catalog_and_scoring"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
