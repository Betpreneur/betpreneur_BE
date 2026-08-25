"""Point the StrategyReview content type at picks."""
from django.db import migrations


def _move(apps, from_label, to_label):
    ContentType = apps.get_model("contenttypes", "ContentType")
    if ContentType.objects.filter(app_label=to_label, model="strategyreview").exists():
        ContentType.objects.filter(app_label=from_label, model="strategyreview").delete()
        return
    ContentType.objects.filter(app_label=from_label, model="strategyreview").update(
        app_label=to_label
    )


def forwards(apps, schema_editor):
    _move(apps, "algo", "picks")


def backwards(apps, schema_editor):
    _move(apps, "picks", "algo")


class Migration(migrations.Migration):
    dependencies = [
        ("picks", "0003_move_strategy_review"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
