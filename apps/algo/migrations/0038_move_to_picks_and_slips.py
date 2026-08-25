"""Hand the daily-run and slip-review tables to picks and slips.

State only — the tables are untouched.
"""

from django.db import migrations


class Migration(migrations.Migration):

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

                migrations.RemoveField(
                    model_name='algofixture',
                    name='run',
                ),
                migrations.RemoveField(
                    model_name='gameback',
                    name='fixture',
                ),
                migrations.RemoveField(
                    model_name='algorun',
                    name='triggered_by',
                ),
                migrations.RemoveField(
                    model_name='pick',
                    name='run',
                ),
                migrations.RemoveField(
                    model_name='marketprediction',
                    name='run',
                ),
                migrations.RemoveField(
                    model_name='gameback',
                    name='user',
                ),
                migrations.RemoveField(
                    model_name='marketprediction',
                    name='selected_pick',
                ),
                migrations.RemoveField(
                    model_name='pickback',
                    name='pick',
                ),
                migrations.AlterUniqueTogether(
                    name='pickback',
                    unique_together=None,
                ),
                migrations.RemoveField(
                    model_name='pickback',
                    name='user',
                ),
                migrations.DeleteModel(
                    name='SlipLegAnalysisCache',
                ),
                migrations.RemoveField(
                    model_name='sliprepair',
                    name='review',
                ),
                migrations.RemoveField(
                    model_name='slipreview',
                    name='user',
                ),
                migrations.RemoveField(
                    model_name='slipreviewevent',
                    name='review',
                ),
                migrations.RemoveField(
                    model_name='slipreviewstreamtoken',
                    name='review',
                ),
                migrations.RemoveField(
                    model_name='slipselection',
                    name='review',
                ),
                migrations.RemoveField(
                    model_name='slipreviewstreamtoken',
                    name='user',
                ),
                migrations.DeleteModel(
                    name='AlgoFixture',
                ),
                migrations.DeleteModel(
                    name='AlgoRun',
                ),
                migrations.DeleteModel(
                    name='GameBack',
                ),
                migrations.DeleteModel(
                    name='MarketPrediction',
                ),
                migrations.DeleteModel(
                    name='Pick',
                ),
                migrations.DeleteModel(
                    name='PickBack',
                ),
                migrations.DeleteModel(
                    name='SlipRepair',
                ),
                migrations.DeleteModel(
                    name='SlipReviewEvent',
                ),
                migrations.DeleteModel(
                    name='SlipReview',
                ),
                migrations.DeleteModel(
                    name='SlipSelection',
                ),
                migrations.DeleteModel(
                    name='SlipReviewStreamToken',
                ),
            ],
        ),
    ]
