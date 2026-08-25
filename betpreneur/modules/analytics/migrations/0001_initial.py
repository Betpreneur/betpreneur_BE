"""Bankroll snapshots and reports, adopted from retired apps.

The original bankroll/reports app labels are gone, but their production tables
remain. This migration therefore creates the tables on a fresh database and
adopts them when they already exist.
"""

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


def create_missing_analytics_tables(apps, schema_editor):
    from betpreneur.modules.analytics.models import BankrollSnapshot, Report

    existing_tables = set(schema_editor.connection.introspection.table_names())
    for model in (Report, BankrollSnapshot):
        if model._meta.db_table not in existing_tables:
            schema_editor.create_model(model)
            existing_tables.add(model._meta.db_table)


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    report_model = migrations.CreateModel(
        name='Report',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('target_date', models.DateField()),
            ('title', models.CharField(max_length=255)),
            ('drive_file_id', models.CharField(blank=True, max_length=255)),
            ('local_path', models.CharField(blank=True, max_length=500)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
        ],
        options={
            'db_table': 'reports_report',
            'ordering': ['-target_date', '-created_at'],
        },
    )
    bankroll_snapshot_model = migrations.CreateModel(
        name='BankrollSnapshot',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
            ('source', models.CharField(default='manual', max_length=50)),
            ('recorded_at', models.DateTimeField(auto_now_add=True)),
            ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bankroll_snapshots', to=settings.AUTH_USER_MODEL)),
        ],
        options={
            'db_table': 'bankroll_bankrollsnapshot',
            'ordering': ['-recorded_at'],
        },
    )

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(create_missing_analytics_tables, migrations.RunPython.noop),
            ],
            state_operations=[
                report_model,
                bankroll_snapshot_model,
            ],
        ),
    ]
