"""Hand the four token tables over to the billing module.

State only: the tables are untouched. billing/0001 adopts them.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('algo', '0035_pin_db_table_names'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. The tables already exist in
            # production and are created from algo's historical migrations
            # on a fresh build, so this only moves ownership in Django's
            # migration state.
            database_operations=[],
            state_operations=[

                migrations.RemoveField(
                    model_name='tokenreservation',
                    name='user',
                ),
                migrations.RemoveField(
                    model_name='tokenreservation',
                    name='wallet',
                ),
                migrations.RemoveField(
                    model_name='tokentransaction',
                    name='user',
                ),
                migrations.RemoveField(
                    model_name='tokentransaction',
                    name='wallet',
                ),
                migrations.RemoveField(
                    model_name='tokenwallet',
                    name='user',
                ),
                migrations.DeleteModel(
                    name='TokenPurchase',
                ),
                migrations.DeleteModel(
                    name='TokenReservation',
                ),
                migrations.DeleteModel(
                    name='TokenTransaction',
                ),
                migrations.DeleteModel(
                    name='TokenWallet',
                ),
            ],
        ),
    ]
