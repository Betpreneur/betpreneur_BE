"""Bankroll snapshots and reports, adopted from the retired bankroll/reports apps.

Unlike the algo model moves, those apps are deleted outright — their migration
history goes with them — so this migration really does create the tables on a
fresh database. In production the tables already exist, so it is applied with:

    python manage.py migrate analytics 0001 --fake

db_table is pinned to the original names, so nothing is renamed either way.
"""

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
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
        ),
        migrations.CreateModel(
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
        ),
    ]
