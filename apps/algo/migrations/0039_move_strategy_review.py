"""Hand StrategyReview to picks.

It records the daily run's own operating profile — written by the runner and
read by admin — so it belongs with picks rather than analytics. State only.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('algo', '0038_move_to_picks_and_slips'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. These tables already exist in
            # production, and on a fresh build algo's historical migrations
            # create them, so this only moves ownership in migration state.
            database_operations=[],
            state_operations=[

                migrations.DeleteModel(
                    name='StrategyReview',
                ),
            ],
        ),
    ]
