"""Adopt StrategyReview from algo. State only; db_table stays algo_strategyreview."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('algo', '0039_move_strategy_review'),
        ('picks', '0002_content_types'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. These tables already exist in
            # production, and on a fresh build algo's historical migrations
            # create them, so this only moves ownership in migration state.
            database_operations=[],
            state_operations=[

                migrations.CreateModel(
                    name='StrategyReview',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('target_date', models.DateField(unique=True)),
                        ('profile', models.JSONField(blank=True, default=dict)),
                        ('markets_suppressed', models.JSONField(blank=True, default=list)),
                        ('markets_cooling', models.JSONField(blank=True, default=list)),
                        ('markets_promoted', models.JSONField(blank=True, default=list)),
                        ('league_market_actions', models.JSONField(blank=True, default=dict)),
                        ('league_warnings', models.JSONField(blank=True, default=list)),
                        ('daily_policy', models.CharField(default='adaptive_market_memory', max_length=100)),
                        ('reason', models.TextField(blank=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_strategyreview',
                        'ordering': ['-target_date'],
                        'indexes': [models.Index(fields=['target_date'], name='algo_strate_target__f06c48_idx')],
                    },
                ),
            ],
        ),
    ]
