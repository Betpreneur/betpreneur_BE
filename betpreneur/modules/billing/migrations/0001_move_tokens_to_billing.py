"""Adopt the four token tables from algo.

State only. db_table stays algo_* so no data moves and every index keeps
its existing name.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('algo', '0036_move_tokens_to_billing'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing reaches the database. The tables already exist in
            # production and are created from algo's historical migrations
            # on a fresh build, so this only moves ownership in Django's
            # migration state.
            database_operations=[],
            state_operations=[

                migrations.CreateModel(
                    name='TokenWallet',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('free_tokens', models.PositiveIntegerField(default=0)),
                        ('paid_tokens', models.PositiveIntegerField(default=0)),
                        ('last_free_refill_date', models.DateField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='token_wallet', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'algo_tokenwallet',
                    },
                ),
                migrations.CreateModel(
                    name='TokenTransaction',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('amount', models.IntegerField(help_text='Positive credits tokens; negative debits tokens.')),
                        ('free_tokens_delta', models.IntegerField(default=0)),
                        ('paid_tokens_delta', models.IntegerField(default=0)),
                        ('token_bucket', models.CharField(choices=[('free', 'Free'), ('paid', 'Paid'), ('mixed', 'Mixed')], default='mixed', max_length=20)),
                        ('reason', models.CharField(choices=[('signup_grant', 'Signup Grant'), ('daily_free_refill', 'Daily Free Refill'), ('token_purchase_credit', 'Token Purchase Credit'), ('slip_review_reserve', 'Slip Review Reserve'), ('slip_review_consume', 'Slip Review Consume'), ('slip_review_release', 'Slip Review Release'), ('token_reservation_expire', 'Token Reservation Expire'), ('smart_randomize_charge', 'Smart Randomize Charge'), ('admin_adjustment', 'Admin Adjustment')], max_length=40)),
                        ('reference_type', models.CharField(blank=True, max_length=80)),
                        ('reference_id', models.CharField(blank=True, max_length=120)),
                        ('balance_after', models.JSONField(blank=True, default=dict)),
                        ('metadata', models.JSONField(blank=True, default=dict)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='token_transactions', to=settings.AUTH_USER_MODEL)),
                        ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='transactions', to='billing.tokenwallet')),
                    ],
                    options={
                        'db_table': 'algo_tokentransaction',
                        'ordering': ['-created_at', '-id'],
                    },
                ),
                migrations.CreateModel(
                    name='TokenReservation',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('amount', models.PositiveIntegerField()),
                        ('free_tokens_reserved', models.PositiveIntegerField(default=0)),
                        ('paid_tokens_reserved', models.PositiveIntegerField(default=0)),
                        ('status', models.CharField(choices=[('reserved', 'Reserved'), ('consumed', 'Consumed'), ('released', 'Released'), ('expired', 'Expired')], default='reserved', max_length=20)),
                        ('reference_type', models.CharField(blank=True, max_length=80)),
                        ('reference_id', models.CharField(blank=True, max_length=120)),
                        ('metadata', models.JSONField(blank=True, default=dict)),
                        ('expires_at', models.DateTimeField()),
                        ('consumed_at', models.DateTimeField(blank=True, null=True)),
                        ('released_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='token_reservations', to=settings.AUTH_USER_MODEL)),
                        ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='billing.tokenwallet')),
                    ],
                    options={
                        'db_table': 'algo_tokenreservation',
                        'ordering': ['-created_at', '-id'],
                    },
                ),
                migrations.CreateModel(
                    name='TokenPurchase',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('package_id', models.CharField(max_length=120)),
                        ('tokens', models.PositiveIntegerField()),
                        ('amount', models.PositiveIntegerField(help_text='Major currency unit, e.g. naira.')),
                        ('amount_kobo', models.PositiveIntegerField()),
                        ('currency', models.CharField(default='NGN', max_length=10)),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('paid', 'Paid'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                        ('provider', models.CharField(blank=True, max_length=40)),
                        ('provider_reference', models.CharField(blank=True, max_length=160)),
                        ('metadata', models.JSONField(blank=True, default=dict)),
                        ('paid_at', models.DateTimeField(blank=True, null=True)),
                        ('failed_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='token_purchases', to=settings.AUTH_USER_MODEL)),
                        ('credited_transaction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='credited_token_purchases', to='billing.tokentransaction')),
                    ],
                    options={
                        'db_table': 'algo_tokenpurchase',
                        'ordering': ['-created_at', '-id'],
                        'indexes': [models.Index(fields=['user', 'status'], name='algo_tokenp_user_id_80d2d6_idx'), models.Index(fields=['package_id', 'status'], name='algo_tokenp_package_3e1f55_idx'), models.Index(fields=['provider', 'provider_reference'], name='algo_tokenp_prov_4f3a41_idx'), models.Index(fields=['status', 'created_at'], name='algo_tokenp_status_3916fd_idx')],
                    },
                ),
                migrations.AddIndex(
                    model_name='tokenwallet',
                    index=models.Index(fields=['user'], name='algo_tokenw_user_id_a075d4_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokenwallet',
                    index=models.Index(fields=['last_free_refill_date'], name='algo_tokenw_last_fr_8b467c_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokentransaction',
                    index=models.Index(fields=['user', 'created_at'], name='algo_tokent_user_id_f2f456_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokentransaction',
                    index=models.Index(fields=['reference_type', 'reference_id'], name='algo_tokent_referen_844759_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokentransaction',
                    index=models.Index(fields=['reason', 'created_at'], name='algo_tokent_reason_0318f1_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokenreservation',
                    index=models.Index(fields=['user', 'status'], name='algo_tokenr_user_id_4605d0_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokenreservation',
                    index=models.Index(fields=['reference_type', 'reference_id'], name='algo_tokenr_referen_32a438_idx'),
                ),
                migrations.AddIndex(
                    model_name='tokenreservation',
                    index=models.Index(fields=['status', 'expires_at'], name='algo_tokenr_status__c5782f_idx'),
                ),
            ],
        ),
    ]
