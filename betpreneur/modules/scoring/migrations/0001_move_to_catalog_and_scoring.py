"""Adopt the fitted-model tables from algo.

State only. db_table stays algo_*.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('algo', '0037_move_to_catalog_and_scoring'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. These tables already exist in
            # production, and on a fresh build algo's historical migrations
            # create them, so this only moves ownership in migration state.
            database_operations=[],
            state_operations=[

                migrations.CreateModel(
                    name='FixtureLineup',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('provider', models.CharField(default='statpal', max_length=30)),
                        ('match_id', models.CharField(max_length=64)),
                        ('side', models.CharField(max_length=10)),
                        ('team_id', models.CharField(blank=True, max_length=64)),
                        ('team_name', models.CharField(blank=True, max_length=255)),
                        ('team_name_normalized', models.CharField(blank=True, db_index=True, max_length=255)),
                        ('formation', models.CharField(blank=True, max_length=32)),
                        ('confidence', models.PositiveIntegerField(default=0)),
                        ('starting_xi', models.JSONField(blank=True, default=list)),
                        ('bench', models.JSONField(blank=True, default=list)),
                        ('fetched_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_fixturelineup',
                        'indexes': [models.Index(fields=['match_id', 'side'], name='algo_fixtur_match_i_44ab7e_idx')],
                        'constraints': [models.UniqueConstraint(fields=('provider', 'match_id', 'side'), name='unique_fixture_lineup')],
                    },
                ),
                migrations.CreateModel(
                    name='LeagueScoreModel',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('provider', models.CharField(default='statpal', max_length=30)),
                        ('league_id', models.CharField(max_length=64)),
                        ('league_name', models.CharField(blank=True, max_length=255)),
                        ('season', models.CharField(blank=True, max_length=32)),
                        ('model_version', models.CharField(max_length=32)),
                        ('home_goal_baseline', models.FloatField(default=1.35)),
                        ('away_goal_baseline', models.FloatField(default=1.1)),
                        ('rho', models.FloatField(default=-0.13)),
                        ('data_quality', models.CharField(choices=[('strong', 'Strong'), ('medium', 'Medium'), ('limited', 'Limited'), ('poor', 'Poor')], default='poor', max_length=20)),
                        ('teams_fitted', models.PositiveIntegerField(default=0)),
                        ('matches_observed', models.PositiveIntegerField(default=0)),
                        ('prior_season', models.CharField(blank=True, max_length=32)),
                        ('diagnostics', models.JSONField(blank=True, default=dict)),
                        ('fitted_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_leaguescoremodel',
                        'ordering': ['-fitted_at'],
                        'indexes': [models.Index(fields=['provider', 'league_id'], name='algo_league_provide_54ffb4_idx')],
                        'constraints': [models.UniqueConstraint(fields=('provider', 'league_id', 'model_version'), name='unique_league_score_model')],
                    },
                ),
                migrations.CreateModel(
                    name='PlayerAvailability',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('provider', models.CharField(default='statpal', max_length=30)),
                        ('player_id', models.CharField(blank=True, max_length=64)),
                        ('player_name', models.CharField(max_length=255)),
                        ('player_name_normalized', models.CharField(db_index=True, max_length=255)),
                        ('team_id', models.CharField(blank=True, max_length=64)),
                        ('team_name', models.CharField(blank=True, max_length=255)),
                        ('team_name_normalized', models.CharField(blank=True, db_index=True, max_length=255)),
                        ('match_id', models.CharField(blank=True, max_length=64)),
                        ('match_date', models.DateField(blank=True, null=True)),
                        ('status', models.CharField(choices=[('out', 'Out'), ('doubtful', 'Doubtful')], default='out', max_length=20)),
                        ('reason', models.CharField(blank=True, max_length=120)),
                        ('fetched_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_playeravailability',
                        'indexes': [models.Index(fields=['player_name_normalized', 'match_id'], name='algo_player_player__d6ee57_idx'), models.Index(fields=['team_name_normalized', 'match_date'], name='algo_player_team_na_82f23b_idx')],
                    },
                ),
                migrations.CreateModel(
                    name='TeamRateProfile',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('provider', models.CharField(default='statpal', max_length=30)),
                        ('team_id', models.CharField(max_length=64)),
                        ('team_name', models.CharField(blank=True, max_length=255)),
                        ('team_name_normalized', models.CharField(blank=True, db_index=True, max_length=255)),
                        ('league_id', models.CharField(blank=True, max_length=64)),
                        ('corners_home', models.FloatField(blank=True, null=True)),
                        ('corners_away', models.FloatField(blank=True, null=True)),
                        ('cards_home', models.FloatField(blank=True, null=True)),
                        ('cards_away', models.FloatField(blank=True, null=True)),
                        ('shots_on_target_home', models.FloatField(blank=True, null=True)),
                        ('shots_on_target_away', models.FloatField(blank=True, null=True)),
                        ('fouls_per_game', models.FloatField(blank=True, null=True)),
                        ('matches', models.PositiveIntegerField(default=0)),
                        ('payload', models.JSONField(blank=True, default=dict)),
                        ('fetched_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_teamrateprofile',
                        'indexes': [models.Index(fields=['provider', 'team_name_normalized'], name='algo_teamra_provide_e1fb43_idx'), models.Index(fields=['league_id'], name='algo_teamra_league__661842_idx')],
                        'constraints': [models.UniqueConstraint(fields=('provider', 'team_id'), name='unique_team_rate_profile')],
                    },
                ),
                migrations.CreateModel(
                    name='TeamStrength',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('team_id', models.CharField(blank=True, max_length=64)),
                        ('team_name', models.CharField(max_length=255)),
                        ('team_name_normalized', models.CharField(db_index=True, max_length=255)),
                        ('home_attack', models.FloatField(default=1.0)),
                        ('home_defence', models.FloatField(default=1.0)),
                        ('away_attack', models.FloatField(default=1.0)),
                        ('away_defence', models.FloatField(default=1.0)),
                        ('matches', models.PositiveIntegerField(default=0)),
                        ('prior_matches', models.PositiveIntegerField(default=0)),
                        ('prior_season', models.CharField(blank=True, max_length=32)),
                        ('shots_per_game', models.FloatField(blank=True, null=True)),
                        ('model', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='teams', to='scoring.leaguescoremodel')),
                    ],
                    options={
                        'db_table': 'algo_teamstrength',
                        'indexes': [models.Index(fields=['model', 'team_name_normalized'], name='algo_teamst_model_i_1eaefa_idx'), models.Index(fields=['model', 'team_id'], name='algo_teamst_model_i_9e8882_idx')],
                    },
                ),
            ],
        ),
    ]
