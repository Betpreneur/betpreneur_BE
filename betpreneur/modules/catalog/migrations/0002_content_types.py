"""Point the fixture and provider-map content types at catalog.

Includes StatPalFixtureCoverage, which is a proxy: it owns no table but does
own a django_content_type row, so it needs moving like any other model.
"""
from django.db import migrations

MODELS = [
    "fixturecache",
    "statpalfixturecoverage",
    "statpalfixturesnapshot",
    "bookmakerleaguemap",
    "teamaliasmap",
    "providerteammap",
    "providerplayermap",
    "providerfixturemap",
    "slipreviewmarketcache",
]


def _move(apps, from_label, to_label):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in MODELS:
        if ContentType.objects.filter(app_label=to_label, model=model).exists():
            ContentType.objects.filter(app_label=from_label, model=model).delete()
            continue
        ContentType.objects.filter(app_label=from_label, model=model).update(app_label=to_label)


def forwards(apps, schema_editor):
    _move(apps, "algo", "catalog")


def backwards(apps, schema_editor):
    _move(apps, "catalog", "algo")


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_move_to_catalog_and_scoring"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
