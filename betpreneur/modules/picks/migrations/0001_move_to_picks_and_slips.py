"""Adopt the daily-run tables from algo.

State only. db_table stays algo_*.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('algo', '0038_move_to_picks_and_slips'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. These tables already exist in
            # production, and on a fresh build algo's historical migrations
            # create them, so this only moves ownership in migration state.
            database_operations=[],
            state_operations=[

                migrations.CreateModel(
                    name='AlgoRun',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('target_date', models.DateField()),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('success', 'Success'), ('failed', 'Failed'), ('rest_day', 'Rest Day'), ('no_data', 'No Data')], default='pending', max_length=20)),
                        ('fd_fixtures', models.PositiveIntegerField(default=0)),
                        ('aps_fixtures', models.PositiveIntegerField(default=0)),
                        ('total_scored', models.PositiveIntegerField(default=0)),
                        ('picks_count', models.PositiveIntegerField(default=0)),
                        ('bankers', models.PositiveIntegerField(default=0)),
                        ('value_gems', models.PositiveIntegerField(default=0)),
                        ('wild_cards', models.PositiveIntegerField(default=0)),
                        ('bankroll', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                        ('result', models.JSONField(blank=True, default=dict)),
                        ('error', models.TextField(blank=True)),
                        ('started_at', models.DateTimeField(blank=True, null=True)),
                        ('finished_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('triggered_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='algo_runs', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_algorun',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='AlgoFixture',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('match_date', models.DateField()),
                        ('fixture', models.CharField(max_length=255)),
                        ('home_team', models.CharField(blank=True, max_length=255)),
                        ('away_team', models.CharField(blank=True, max_length=255)),
                        ('home_logo', models.URLField(blank=True)),
                        ('away_logo', models.URLField(blank=True)),
                        ('league', models.CharField(blank=True, max_length=255)),
                        ('league_logo', models.URLField(blank=True)),
                        ('country', models.CharField(blank=True, max_length=100)),
                        ('country_flag', models.URLField(blank=True)),
                        ('round', models.CharField(blank=True, max_length=255)),
                        ('league_type', models.CharField(blank=True, max_length=50)),
                        ('kickoff', models.CharField(blank=True, max_length=50)),
                        ('match_id', models.CharField(max_length=100)),
                        ('market_count', models.PositiveIntegerField(default=0)),
                        ('markets_70_plus', models.PositiveIntegerField(default=0)),
                        ('markets_65_plus', models.PositiveIntegerField(default=0)),
                        ('home_recent_form', models.JSONField(blank=True, default=dict)),
                        ('away_recent_form', models.JSONField(blank=True, default=dict)),
                        ('fixture_context', models.JSONField(blank=True, default=dict)),
                        ('team_news', models.JSONField(blank=True, default=dict)),
                        ('corner_profile', models.JSONField(blank=True, default=dict)),
                        ('insights', models.JSONField(blank=True, default=dict)),
                        ('source_payload', models.JSONField(blank=True, default=dict)),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('scored', 'Scored'), ('failed', 'Failed')], default='pending', max_length=20)),
                        ('error', models.TextField(blank=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fixtures', to='picks.algorun')),
                    ],
                    options={
                        'db_table': 'algo_algofixture',
                        'ordering': ['match_date', 'country', 'league', 'kickoff', 'fixture'],
                    },
                ),
                migrations.CreateModel(
                    name='GameBack',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('match_id', models.CharField(max_length=100)),
                        ('match_date', models.DateField(blank=True, null=True)),
                        ('market', models.CharField(blank=True, max_length=120)),
                        ('meaning', models.CharField(blank=True, max_length=255)),
                        ('odds', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                        ('confidence', models.PositiveSmallIntegerField(blank=True, null=True)),
                        ('final_confidence', models.PositiveSmallIntegerField(blank=True, null=True)),
                        ('ev', models.DecimalField(blank=True, decimal_places=3, max_digits=8, null=True)),
                        ('market_snapshot', models.JSONField(blank=True, default=dict)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('fixture', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backs', to='picks.algofixture')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='backed_games', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_gameback',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='Pick',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('match_date', models.DateField(blank=True, null=True)),
                        ('fixture', models.CharField(max_length=255)),
                        ('home_team', models.CharField(blank=True, max_length=255)),
                        ('away_team', models.CharField(blank=True, max_length=255)),
                        ('league', models.CharField(blank=True, max_length=255)),
                        ('kickoff', models.CharField(blank=True, max_length=50)),
                        ('match_id', models.CharField(blank=True, max_length=100)),
                        ('tier', models.CharField(choices=[('banker', 'Banker'), ('value_gem', 'Value Gem'), ('wild_card', 'Wild Card')], max_length=20)),
                        ('market', models.CharField(max_length=100)),
                        ('meaning', models.CharField(blank=True, max_length=255)),
                        ('reasoning', models.TextField(blank=True)),
                        ('model_verdict', models.TextField(blank=True)),
                        ('home_recent_form', models.JSONField(blank=True, default=dict)),
                        ('away_recent_form', models.JSONField(blank=True, default=dict)),
                        ('risk_flags', models.JSONField(blank=True, default=list)),
                        ('insights', models.JSONField(blank=True, default=dict)),
                        ('confidence', models.PositiveIntegerField()),
                        ('odds', models.DecimalField(decimal_places=2, max_digits=8)),
                        ('ev', models.DecimalField(decimal_places=3, max_digits=8)),
                        ('stake', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                        ('score', models.CharField(blank=True, max_length=20)),
                        ('result', models.CharField(blank=True, max_length=255)),
                        ('pnl', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('win', 'Win'), ('loss', 'Loss'), ('void', 'Void')], default='pending', max_length=20)),
                        ('source', models.CharField(blank=True, max_length=20)),
                        ('settled_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='picks', to='picks.algorun')),
                    ],
                    options={
                        'db_table': 'algo_pick',
                        'ordering': ['match_date', 'tier', '-confidence', '-ev'],
                    },
                ),
                migrations.CreateModel(
                    name='MarketPrediction',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('match_date', models.DateField()),
                        ('fixture', models.CharField(max_length=255)),
                        ('home_team', models.CharField(blank=True, max_length=255)),
                        ('away_team', models.CharField(blank=True, max_length=255)),
                        ('league', models.CharField(blank=True, max_length=255)),
                        ('kickoff', models.CharField(blank=True, max_length=50)),
                        ('match_id', models.CharField(blank=True, max_length=100)),
                        ('market', models.CharField(max_length=100)),
                        ('meaning', models.CharField(blank=True, max_length=255)),
                        ('raw_confidence', models.PositiveIntegerField(default=0)),
                        ('confidence', models.PositiveIntegerField(default=0)),
                        ('odds', models.DecimalField(decimal_places=2, max_digits=8)),
                        ('ev', models.DecimalField(blank=True, decimal_places=3, max_digits=8, null=True)),
                        ('odds_source', models.CharField(blank=True, max_length=30)),
                        ('odds_meta', models.JSONField(blank=True, default=dict)),
                        ('eligible', models.BooleanField(default=False)),
                        ('published', models.BooleanField(default=False)),
                        ('rejection_reason', models.CharField(blank=True, max_length=255)),
                        ('risk_flags', models.JSONField(blank=True, default=list)),
                        ('insights', models.JSONField(blank=True, default=dict)),
                        ('home_recent_form', models.JSONField(blank=True, default=dict)),
                        ('away_recent_form', models.JSONField(blank=True, default=dict)),
                        ('fixture_context', models.JSONField(blank=True, default=dict)),
                        ('team_news', models.JSONField(blank=True, default=dict)),
                        ('score', models.CharField(blank=True, max_length=20)),
                        ('result', models.CharField(blank=True, max_length=255)),
                        ('pnl_simulated', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('win', 'Win'), ('loss', 'Loss'), ('void', 'Void')], default='pending', max_length=20)),
                        ('settled_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='market_predictions', to='picks.algorun')),
                        ('selected_pick', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='internal_predictions', to='picks.pick')),
                    ],
                    options={
                        'db_table': 'algo_marketprediction',
                        'ordering': ['match_date', 'fixture', '-confidence', 'market'],
                    },
                ),
                migrations.CreateModel(
                    name='PickBack',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('pick', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='backs', to='picks.pick')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='backed_picks', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_pickback',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.AddIndex(
                    model_name='algofixture',
                    index=models.Index(fields=['match_date', 'status'], name='algo_algofi_match_d_0120aa_idx'),
                ),
                migrations.AddIndex(
                    model_name='algofixture',
                    index=models.Index(fields=['run', 'match_id'], name='algo_algofi_run_id_3960f5_idx'),
                ),
                migrations.AddIndex(
                    model_name='algofixture',
                    index=models.Index(fields=['country', 'league'], name='algo_algofi_country_0d5888_idx'),
                ),
                migrations.AddConstraint(
                    model_name='algofixture',
                    constraint=models.UniqueConstraint(fields=('run', 'match_id'), name='unique_algo_fixture_per_run_match'),
                ),
                migrations.AddIndex(
                    model_name='gameback',
                    index=models.Index(fields=['user', 'match_date'], name='algo_gameba_user_id_9eff12_idx'),
                ),
                migrations.AddIndex(
                    model_name='gameback',
                    index=models.Index(fields=['match_id'], name='algo_gameba_match_i_119779_idx'),
                ),
                migrations.AddIndex(
                    model_name='gameback',
                    index=models.Index(fields=['match_id', 'market'], name='algo_gameba_match_i_cc6cb4_idx'),
                ),
                migrations.AddConstraint(
                    model_name='gameback',
                    constraint=models.UniqueConstraint(fields=('user', 'match_id', 'market'), name='unique_game_back_user_match_market'),
                ),
                migrations.AddIndex(
                    model_name='marketprediction',
                    index=models.Index(fields=['match_date', 'status'], name='algo_market_match_d_395881_idx'),
                ),
                migrations.AddIndex(
                    model_name='marketprediction',
                    index=models.Index(fields=['market', 'status'], name='algo_market_market_ff1b97_idx'),
                ),
                migrations.AddIndex(
                    model_name='marketprediction',
                    index=models.Index(fields=['league', 'market', 'status'], name='algo_market_league_42426c_idx'),
                ),
                migrations.AddIndex(
                    model_name='marketprediction',
                    index=models.Index(fields=['published', 'status'], name='algo_market_publish_8bd4b5_idx'),
                ),
                migrations.AddIndex(
                    model_name='marketprediction',
                    index=models.Index(fields=['match_id', 'market'], name='algo_market_match_i_8a1f8f_idx'),
                ),
                migrations.AddConstraint(
                    model_name='marketprediction',
                    constraint=models.UniqueConstraint(fields=('run', 'match_id', 'fixture', 'market'), name='unique_market_prediction_per_run_fixture_market'),
                ),
                migrations.AlterUniqueTogether(
                    name='pickback',
                    unique_together={('pick', 'user')},
                ),
            ],
        ),
    ]
