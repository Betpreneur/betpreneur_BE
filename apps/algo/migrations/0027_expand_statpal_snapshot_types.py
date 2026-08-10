from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("algo", "0026_teamrateprofile_shots_on_target"),
    ]

    operations = [
        migrations.AlterField(
            model_name="statpalfixturesnapshot",
            name="snapshot_type",
            field=models.CharField(
                choices=[
                    ("injuries_suspensions", "Injuries & Suspensions"),
                    ("team_stats", "Team Stats"),
                    ("prematch_odds", "Pre-Match Odds"),
                    ("live_odds", "Live Odds"),
                    ("lineups", "Lineups"),
                    ("predictions", "Predictions"),
                    ("detailed_stats", "Detailed Stats"),
                    ("head_to_head", "Head to Head"),
                    ("league_standings", "League Standings"),
                    ("league_stats", "League Stats"),
                    ("weather_forecast", "Weather Forecast"),
                    ("player_stats", "Player Stats"),
                    ("coach", "Coach"),
                    ("images", "Images"),
                    ("live_storylines", "Live Storylines"),
                    ("raw", "Raw"),
                ],
                max_length=40,
            ),
        ),
        migrations.CreateModel(
            name="StatPalFixtureCoverage",
            fields=[],
            options={
                "verbose_name": "StatPal Fixture Coverage",
                "verbose_name_plural": "StatPal Fixture Coverage",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("algo.fixturecache",),
        ),
    ]
