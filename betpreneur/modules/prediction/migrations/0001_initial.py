# Generated for Stage 8: canonical prediction calibration dataset.

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PredictionTrainingSample",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fixture_id", models.CharField(max_length=120)),
                ("canonical_market", models.CharField(max_length=160)),
                ("line", models.CharField(blank=True, max_length=32)),
                ("side", models.CharField(blank=True, max_length=40)),
                ("first_prediction_score", models.FloatField(blank=True, null=True)),
                ("last_prediction_score", models.FloatField(blank=True, null=True)),
                ("selected_status", models.CharField(blank=True, max_length=40)),
                ("published_status", models.CharField(blank=True, max_length=40)),
                ("odds_source", models.CharField(blank=True, max_length=60)),
                ("real_odds", models.DecimalField(blank=True, decimal_places=3, max_digits=8, null=True)),
                ("estimated_odds", models.BooleanField(default=False)),
                (
                    "settlement_result",
                    models.CharField(
                        choices=[("win", "Win"), ("loss", "Loss"), ("void", "Void"), ("push", "Push")],
                        max_length=20,
                    ),
                ),
                ("market_family", models.CharField(blank=True, max_length=80)),
                ("league_key", models.CharField(blank=True, max_length=120)),
                ("season", models.CharField(blank=True, max_length=32)),
                ("kickoff", models.DateTimeField(blank=True, null=True)),
                ("prediction_created_at", models.DateTimeField()),
                ("last_prediction_created_at", models.DateTimeField(blank=True, null=True)),
                ("source", models.CharField(blank=True, max_length=40)),
                ("source_reference", models.CharField(blank=True, max_length=160)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "prediction_training_sample",
                "ordering": ["-prediction_created_at", "fixture_id", "canonical_market"],
                "indexes": [
                    models.Index(fields=["league_key", "season"], name="prediction_league_season_idx"),
                    models.Index(fields=["canonical_market", "settlement_result"], name="prediction_market_result_idx"),
                    models.Index(fields=["market_family"], name="prediction_market_family_idx"),
                    models.Index(fields=["odds_source", "estimated_odds"], name="prediction_odds_quality_idx"),
                    models.Index(fields=["kickoff"], name="prediction_kickoff_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("fixture_id", "canonical_market", "line", "side"),
                        name="unique_prediction_training_sample",
                    )
                ],
            },
        ),
    ]
