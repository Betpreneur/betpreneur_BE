from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("prediction", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PredictionTeamMatchFeedback",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fixture_id", models.CharField(max_length=120)),
                ("provider_match_id", models.CharField(blank=True, max_length=120)),
                ("fixture_name", models.CharField(blank=True, max_length=255)),
                ("match_date", models.DateField(blank=True, null=True)),
                ("league_key", models.CharField(blank=True, max_length=120)),
                ("season", models.CharField(blank=True, max_length=32)),
                ("team_id", models.CharField(blank=True, max_length=120)),
                ("team_name", models.CharField(max_length=255)),
                ("opponent_id", models.CharField(blank=True, max_length=120)),
                ("opponent_name", models.CharField(blank=True, max_length=255)),
                ("side", models.CharField(choices=[("home", "Home"), ("away", "Away")], max_length=10)),
                ("actual_result", models.CharField(choices=[("win", "Win"), ("draw", "Draw"), ("loss", "Loss")], max_length=10)),
                ("goals_for", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("goals_against", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("corners_for", models.FloatField(blank=True, null=True)),
                ("corners_against", models.FloatField(blank=True, null=True)),
                ("cards_for", models.FloatField(blank=True, null=True)),
                ("cards_against", models.FloatField(blank=True, null=True)),
                ("shots_on_target_for", models.FloatField(blank=True, null=True)),
                ("shots_on_target_against", models.FloatField(blank=True, null=True)),
                ("referee_name", models.CharField(blank=True, max_length=160)),
                ("source", models.CharField(blank=True, max_length=40)),
                ("prediction_snapshot", models.JSONField(blank=True, default=dict)),
                ("actual_stats", models.JSONField(blank=True, default=dict)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "prediction_team_match_feedback",
                "ordering": ["-match_date", "team_name", "fixture_id"],
                "indexes": [
                    models.Index(fields=["team_name", "match_date"], name="pred_feedback_team_date_idx"),
                    models.Index(fields=["opponent_name", "match_date"], name="pred_feedback_opp_date_idx"),
                    models.Index(fields=["league_key", "match_date"], name="pred_feedback_league_idx"),
                    models.Index(fields=["fixture_id"], name="pred_feedback_fixture_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("fixture_id", "team_name", "side"),
                        name="unique_prediction_team_feedback",
                    )
                ],
            },
        ),
    ]
