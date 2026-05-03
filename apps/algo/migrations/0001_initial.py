import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AlgoRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("success", "Success"), ("failed", "Failed"), ("rest_day", "Rest Day"), ("no_data", "No Data")], default="pending", max_length=20)),
                ("fd_fixtures", models.PositiveIntegerField(default=0)),
                ("aps_fixtures", models.PositiveIntegerField(default=0)),
                ("total_scored", models.PositiveIntegerField(default=0)),
                ("picks_count", models.PositiveIntegerField(default=0)),
                ("bankers", models.PositiveIntegerField(default=0)),
                ("value_gems", models.PositiveIntegerField(default=0)),
                ("wild_cards", models.PositiveIntegerField(default=0)),
                ("bankroll", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("triggered_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="algo_runs", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="Pick",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fixture", models.CharField(max_length=255)),
                ("league", models.CharField(blank=True, max_length=255)),
                ("kickoff", models.CharField(blank=True, max_length=50)),
                ("tier", models.CharField(choices=[("banker", "Banker"), ("value_gem", "Value Gem"), ("wild_card", "Wild Card")], max_length=20)),
                ("market", models.CharField(max_length=100)),
                ("meaning", models.CharField(blank=True, max_length=255)),
                ("confidence", models.PositiveIntegerField()),
                ("odds", models.DecimalField(decimal_places=2, max_digits=8)),
                ("ev", models.DecimalField(decimal_places=3, max_digits=8)),
                ("source", models.CharField(blank=True, max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="picks", to="algo.algorun")),
            ],
            options={
                "ordering": ["tier", "-confidence", "-ev"],
            },
        ),
    ]
