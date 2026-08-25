"""Bankroll snapshots and generated reports.

Table names stay as they were — this refactor moves packages, not data.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class BankrollSnapshot(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bankroll_snapshots",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    source = models.CharField(max_length=50, default="manual")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bankroll_bankrollsnapshot"
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.amount} ({self.source})"


class Report(models.Model):
    target_date = models.DateField()
    title = models.CharField(max_length=255)
    drive_file_id = models.CharField(max_length=255, blank=True)
    local_path = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reports_report"
        ordering = ["-target_date", "-created_at"]

    def __str__(self):
        return self.title
