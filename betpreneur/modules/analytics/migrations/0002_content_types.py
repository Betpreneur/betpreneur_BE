"""Point the bankroll/report content types at analytics."""
from django.db import migrations

MOVES = {"bankroll": ["bankrollsnapshot"], "reports": ["report"]}


def forwards(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for old_label, models in MOVES.items():
        for model in models:
            if ContentType.objects.filter(app_label="analytics", model=model).exists():
                ContentType.objects.filter(app_label=old_label, model=model).delete()
                continue
            ContentType.objects.filter(app_label=old_label, model=model).update(
                app_label="analytics"
            )


def backwards(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for old_label, models in MOVES.items():
        ContentType.objects.filter(app_label="analytics", model__in=models).update(
            app_label=old_label
        )


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
