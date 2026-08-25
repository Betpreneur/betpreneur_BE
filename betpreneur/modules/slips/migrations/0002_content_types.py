"""Point the slips content types at slips."""
from django.db import migrations

MODELS = [
    "slipreview", "slipselection", "slipleganalysiscache", "slipreviewevent",
    "slipreviewstreamtoken", "sliprepair"
]


def _move(apps, from_label, to_label):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in MODELS:
        if ContentType.objects.filter(app_label=to_label, model=model).exists():
            ContentType.objects.filter(app_label=from_label, model=model).delete()
            continue
        ContentType.objects.filter(app_label=from_label, model=model).update(app_label=to_label)


def forwards(apps, schema_editor):
    _move(apps, "algo", "slips")


def backwards(apps, schema_editor):
    _move(apps, "slips", "algo")


class Migration(migrations.Migration):
    dependencies = [
        ("slips", "0001_move_to_picks_and_slips"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
