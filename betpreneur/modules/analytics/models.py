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


class StrategyActionOutcome(models.Model):
    class Action(models.TextChoices):
        PROMOTE = "promote", "Promote"
        COOL = "cool", "Cool"
        SUPPRESS = "suppress", "Suppress"

    class Scope(models.TextChoices):
        MARKET = "market", "Market"
        LEAGUE_MARKET = "league_market", "League Market"
        CONFIDENCE_BAND = "confidence_band", "Confidence Band"

    decision_date = models.DateField()
    evaluated_from = models.DateField()
    evaluated_to = models.DateField()
    scope = models.CharField(max_length=40, choices=Scope.choices)
    action = models.CharField(max_length=20, choices=Action.choices)
    key = models.CharField(max_length=240)
    market = models.CharField(max_length=160, blank=True)
    league = models.CharField(max_length=160, blank=True)
    sample_size = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    voids = models.PositiveIntegerField(default=0)
    hit_rate = models.FloatField(null=True, blank=True)
    roi = models.FloatField(null=True, blank=True)
    baseline_roi = models.FloatField(null=True, blank=True)
    roi_delta = models.FloatField(null=True, blank=True)
    authority_multiplier = models.FloatField(default=1.0)
    verdict = models.CharField(max_length=40, default="insufficient_sample")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_strategyactionoutcome"
        ordering = ["-decision_date", "scope", "key"]
        indexes = [
            models.Index(fields=["decision_date", "scope"], name="strat_outcome_date_scope_idx"),
            models.Index(fields=["action", "verdict"], name="strategy_outcome_action_idx"),
            models.Index(fields=["market"], name="strategy_outcome_market_idx"),
            models.Index(fields=["league", "market"], name="strat_outcome_league_mkt_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["decision_date", "scope", "action", "key"],
                name="unique_strategy_action_outcome",
            )
        ]

    def __str__(self):
        return f"{self.decision_date} {self.action} {self.scope}:{self.key}"
