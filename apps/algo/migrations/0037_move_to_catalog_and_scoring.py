"""Hand the fixture, provider-map and fitted-model tables to catalog and scoring.

State only — the tables are untouched.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('algo', '0036_move_tokens_to_billing'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. These tables already exist in
            # production, and on a fresh build algo's historical migrations
            # create them, so this only moves ownership in migration state.
            database_operations=[],
            state_operations=[

                migrations.DeleteModel(
                    name='BookmakerLeagueMap',
                ),
                migrations.RemoveField(
                    model_name='statpalfixturesnapshot',
                    name='fixture',
                ),
                migrations.DeleteModel(
                    name='FixtureLineup',
                ),
                migrations.RemoveField(
                    model_name='teamstrength',
                    name='model',
                ),
                migrations.DeleteModel(
                    name='PlayerAvailability',
                ),
                migrations.RemoveField(
                    model_name='statpalfixturesnapshot',
                    name='provider_fixture',
                ),
                migrations.DeleteModel(
                    name='ProviderPlayerMap',
                ),
                migrations.DeleteModel(
                    name='ProviderTeamMap',
                ),
                migrations.DeleteModel(
                    name='SlipReviewMarketCache',
                ),
                migrations.DeleteModel(
                    name='TeamAliasMap',
                ),
                migrations.DeleteModel(
                    name='TeamRateProfile',
                ),
                migrations.DeleteModel(
                    name='StatPalFixtureCoverage',
                ),
                migrations.DeleteModel(
                    name='FixtureCache',
                ),
                migrations.DeleteModel(
                    name='LeagueScoreModel',
                ),
                migrations.DeleteModel(
                    name='TeamStrength',
                ),
                migrations.DeleteModel(
                    name='ProviderFixtureMap',
                ),
                migrations.DeleteModel(
                    name='StatPalFixtureSnapshot',
                ),
            ],
        ),
    ]
