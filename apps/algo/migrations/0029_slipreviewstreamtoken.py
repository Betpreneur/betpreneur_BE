from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("algo", "0028_slipleganalysiscache"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SlipReviewStreamToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "review",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stream_tokens", to="algo.slipreview"),
                ),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slip_review_stream_tokens", to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["token_hash"], name="algo_slipre_token_h_1d9e88_idx"),
                    models.Index(fields=["review", "user"], name="algo_slipre_review__75fc80_idx"),
                    models.Index(fields=["expires_at"], name="algo_slipre_expires_57f904_idx"),
                ],
            },
        ),
    ]
