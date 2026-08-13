from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("algo", "0027_expand_statpal_snapshot_types"),
    ]

    operations = [
        migrations.CreateModel(
            name="SlipLegAnalysisCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cache_key", models.CharField(max_length=64, unique=True)),
                ("status", models.CharField(choices=[("processing", "Processing"), ("ready", "Ready"), ("failed", "Failed")], default="ready", max_length=20)),
                ("source", models.CharField(blank=True, max_length=30)),
                ("provider_event_id", models.CharField(blank=True, max_length=120)),
                ("match_text", models.CharField(blank=True, max_length=255)),
                ("market_text", models.CharField(blank=True, max_length=160)),
                ("match_id", models.CharField(blank=True, max_length=100)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField()),
                ("lock_expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["cache_key"], name="algo_sliple_cache_k_b5c8db_idx"),
                    models.Index(fields=["source", "provider_event_id"], name="algo_sliple_source_4f3934_idx"),
                    models.Index(fields=["match_id"], name="algo_sliple_match_i_cbbd1c_idx"),
                    models.Index(fields=["status", "lock_expires_at"], name="algo_sliple_status_72ca48_idx"),
                    models.Index(fields=["expires_at"], name="algo_sliple_expires_1ef28c_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SlipReviewEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=80)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "review",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="events", to="algo.slipreview"),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(fields=["review", "created_at"], name="algo_slipre_review__47a5be_idx"),
                    models.Index(fields=["review", "event_type"], name="algo_slipre_review__4f4768_idx"),
                ],
            },
        ),
    ]
