from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("algo", "0025_fixturelineup"),
    ]

    operations = [
        migrations.AddField(
            model_name="teamrateprofile",
            name="shots_on_target_home",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="teamrateprofile",
            name="shots_on_target_away",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
