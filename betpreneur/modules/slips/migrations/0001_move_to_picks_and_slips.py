"""Adopt the slip-review tables from algo.

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
                    name='SlipLegAnalysisCache',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('cache_key', models.CharField(max_length=64, unique=True)),
                        ('status', models.CharField(choices=[('processing', 'Processing'), ('ready', 'Ready'), ('failed', 'Failed')], default='ready', max_length=20)),
                        ('source', models.CharField(blank=True, max_length=30)),
                        ('provider_event_id', models.CharField(blank=True, max_length=120)),
                        ('match_text', models.CharField(blank=True, max_length=255)),
                        ('market_text', models.CharField(blank=True, max_length=160)),
                        ('match_id', models.CharField(blank=True, max_length=100)),
                        ('payload', models.JSONField(blank=True, default=dict)),
                        ('expires_at', models.DateTimeField()),
                        ('lock_expires_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'db_table': 'algo_slipleganalysiscache',
                        'indexes': [models.Index(fields=['cache_key'], name='algo_sliple_cache_k_b5c8db_idx'), models.Index(fields=['source', 'provider_event_id'], name='algo_sliple_source_4f3934_idx'), models.Index(fields=['match_id'], name='algo_sliple_match_i_cbbd1c_idx'), models.Index(fields=['status', 'lock_expires_at'], name='algo_sliple_status_72ca48_idx'), models.Index(fields=['expires_at'], name='algo_sliple_expires_1ef28c_idx')],
                    },
                ),
                migrations.CreateModel(
                    name='SlipReview',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('source', models.CharField(choices=[('manual', 'Manual'), ('sportybet', 'SportyBet'), ('betano', 'Betano')], default='manual', max_length=30)),
                        ('status', models.CharField(choices=[('queued', 'Queued'), ('importing', 'Importing'), ('analysing', 'Analysing'), ('completed', 'Completed'), ('partial', 'Partial'), ('unanalysed', 'Unanalysed'), ('failed', 'Failed')], default='completed', max_length=30)),
                        ('title', models.CharField(blank=True, max_length=255)),
                        ('submitted_payload', models.JSONField(blank=True, default=dict)),
                        ('summary', models.JSONField(blank=True, default=dict)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slip_reviews', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_slipreview',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='SlipRepair',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('mode', models.CharField(choices=[('recommended', 'Recommended'), ('custom', 'Custom')], default='recommended', max_length=20)),
                        ('original_legs', models.PositiveIntegerField(default=0)),
                        ('original_combined_odds', models.FloatField(blank=True, null=True)),
                        ('original_success_percent', models.FloatField(blank=True, null=True)),
                        ('revised_legs', models.PositiveIntegerField(default=0)),
                        ('revised_combined_odds', models.FloatField(blank=True, null=True)),
                        ('revised_success_percent', models.FloatField(blank=True, null=True)),
                        ('changes', models.JSONField(blank=True, default=list)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('review', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='repairs', to='slips.slipreview')),
                    ],
                    options={
                        'db_table': 'algo_sliprepair',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='SlipReviewEvent',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('event_type', models.CharField(max_length=80)),
                        ('payload', models.JSONField(blank=True, default=dict)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('review', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='slips.slipreview')),
                    ],
                    options={
                        'db_table': 'algo_slipreviewevent',
                        'ordering': ['created_at', 'id'],
                    },
                ),
                migrations.CreateModel(
                    name='SlipReviewStreamToken',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('token_hash', models.CharField(max_length=64, unique=True)),
                        ('expires_at', models.DateTimeField()),
                        ('last_used_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('review', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stream_tokens', to='slips.slipreview')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slip_review_stream_tokens', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_slipreviewstreamtoken',
                    },
                ),
                migrations.CreateModel(
                    name='SlipSelection',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('order', models.PositiveIntegerField(default=0)),
                        ('submitted_match', models.CharField(max_length=255)),
                        ('submitted_market', models.CharField(max_length=120)),
                        ('status', models.CharField(blank=True, max_length=40)),
                        ('verdict', models.CharField(blank=True, max_length=40)),
                        ('message', models.TextField(blank=True)),
                        ('match_id', models.CharField(blank=True, max_length=100)),
                        ('match_date', models.DateField(blank=True, null=True)),
                        ('fixture', models.CharField(blank=True, max_length=255)),
                        ('home_team', models.CharField(blank=True, max_length=255)),
                        ('away_team', models.CharField(blank=True, max_length=255)),
                        ('league', models.CharField(blank=True, max_length=255)),
                        ('country', models.CharField(blank=True, max_length=100)),
                        ('kickoff', models.CharField(blank=True, max_length=50)),
                        ('selected_market', models.JSONField(blank=True, default=dict)),
                        ('best_market', models.JSONField(blank=True, default=dict)),
                        ('recommended_market', models.JSONField(blank=True, default=dict)),
                        ('possible_matches', models.JSONField(blank=True, default=list)),
                        ('analysis_payload', models.JSONField(blank=True, default=dict)),
                        ('settlement_market', models.CharField(blank=True, max_length=120)),
                        ('odds', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                        ('advisory_score', models.FloatField(blank=True, null=True)),
                        ('flagged_risky', models.BooleanField(default=False)),
                        ('outcome', models.CharField(choices=[('pending', 'Pending'), ('win', 'Win'), ('loss', 'Loss'), ('void', 'Void'), ('unsettleable', 'Unsettleable')], default='pending', max_length=20)),
                        ('score', models.CharField(blank=True, max_length=20)),
                        ('result', models.CharField(blank=True, max_length=120)),
                        ('settled_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('review', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='selections', to='slips.slipreview')),
                    ],
                    options={
                        'db_table': 'algo_slipselection',
                        'ordering': ['order', 'id'],
                    },
                ),
                migrations.AddIndex(
                    model_name='slipreview',
                    index=models.Index(fields=['user', 'created_at'], name='algo_slipre_user_id_b978ba_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreview',
                    index=models.Index(fields=['source', 'status'], name='algo_slipre_source_b40ee6_idx'),
                ),
                migrations.AddIndex(
                    model_name='sliprepair',
                    index=models.Index(fields=['review', 'created_at'], name='algo_slipre_review__57e760_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreviewevent',
                    index=models.Index(fields=['review', 'created_at'], name='algo_slipre_review__47a5be_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreviewevent',
                    index=models.Index(fields=['review', 'event_type'], name='algo_slipre_review__4f4768_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreviewstreamtoken',
                    index=models.Index(fields=['token_hash'], name='algo_slipre_token_h_1d9e88_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreviewstreamtoken',
                    index=models.Index(fields=['review', 'user'], name='algo_slipre_review__75fc80_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipreviewstreamtoken',
                    index=models.Index(fields=['expires_at'], name='algo_slipre_expires_57f904_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipselection',
                    index=models.Index(fields=['review', 'order'], name='algo_slipse_review__071346_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipselection',
                    index=models.Index(fields=['match_id'], name='algo_slipse_match_i_29f60d_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipselection',
                    index=models.Index(fields=['status', 'verdict'], name='algo_slipse_status_39798d_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipselection',
                    index=models.Index(fields=['outcome', 'match_date'], name='algo_slipse_outcome_d3f4fe_idx'),
                ),
                migrations.AddIndex(
                    model_name='slipselection',
                    index=models.Index(fields=['review', 'outcome'], name='algo_slipse_review__af2256_idx'),
                ),
            ],
        ),
    ]
