"""The daily run and everything it produces.

Table names stay algo_* — this refactor moves Python packages, never data.
"""
from django.conf import settings
from django.db import models

from betpreneur.modules.pricing.api import Tier as PricingTier


class AlgoRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        REST_DAY = "rest_day", "Rest Day"
        NO_DATA = "no_data", "No Data"

    target_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="algo_runs",
    )
    fd_fixtures = models.PositiveIntegerField(default=0)
    aps_fixtures = models.PositiveIntegerField(default=0)
    total_scored = models.PositiveIntegerField(default=0)
    picks_count = models.PositiveIntegerField(default=0)
    bankers = models.PositiveIntegerField(default=0)
    value_gems = models.PositiveIntegerField(default=0)
    wild_cards = models.PositiveIntegerField(default=0)
    bankroll = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_algorun"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.target_date} - {self.status}"


class AlgoFixture(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SCORED = "scored", "Scored"
        FAILED = "failed", "Failed"

    run = models.ForeignKey(AlgoRun, on_delete=models.CASCADE, related_name="fixtures")
    match_date = models.DateField()
    fixture = models.CharField(max_length=255)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    home_logo = models.URLField(blank=True)
    away_logo = models.URLField(blank=True)
    league = models.CharField(max_length=255, blank=True)
    league_logo = models.URLField(blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_flag = models.URLField(blank=True)
    round = models.CharField(max_length=255, blank=True)
    league_type = models.CharField(max_length=50, blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    match_id = models.CharField(max_length=100)
    market_count = models.PositiveIntegerField(default=0)
    markets_70_plus = models.PositiveIntegerField(default=0)
    markets_65_plus = models.PositiveIntegerField(default=0)
    home_recent_form = models.JSONField(default=dict, blank=True)
    away_recent_form = models.JSONField(default=dict, blank=True)
    fixture_context = models.JSONField(default=dict, blank=True)
    team_news = models.JSONField(default=dict, blank=True)
    corner_profile = models.JSONField(default=dict, blank=True)
    insights = models.JSONField(default=dict, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_algofixture"
        ordering = ["match_date", "country", "league", "kickoff", "fixture"]
        indexes = [
            models.Index(fields=["match_date", "status"]),
            models.Index(fields=["run", "match_id"]),
            models.Index(fields=["country", "league"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "match_id"],
                name="unique_algo_fixture_per_run_match",
            )
        ]

    def __str__(self):
        return self.fixture


class Pick(models.Model):
    class Tier(models.TextChoices):
        # Values come from pricing, which decides what a tier means; picks only
        # stores the answer. Keeping them derived stops the two drifting.
        BANKER = PricingTier.BANKER.value, "Banker"
        VALUE_GEM = PricingTier.VALUE_GEM.value, "Value Gem"
        WILD_CARD = PricingTier.WILD_CARD.value, "Wild Card"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        VOID = "void", "Void"

    run = models.ForeignKey(AlgoRun, on_delete=models.CASCADE, related_name="picks")
    match_date = models.DateField(null=True, blank=True)
    fixture = models.CharField(max_length=255)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    league = models.CharField(max_length=255, blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    match_id = models.CharField(max_length=100, blank=True)
    tier = models.CharField(max_length=20, choices=Tier.choices)
    market = models.CharField(max_length=100)
    meaning = models.CharField(max_length=255, blank=True)
    reasoning = models.TextField(blank=True)
    model_verdict = models.TextField(blank=True)
    home_recent_form = models.JSONField(default=dict, blank=True)
    away_recent_form = models.JSONField(default=dict, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    insights = models.JSONField(default=dict, blank=True)
    confidence = models.PositiveIntegerField()
    odds = models.DecimalField(max_digits=8, decimal_places=2)
    ev = models.DecimalField(max_digits=8, decimal_places=3)
    stake = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    score = models.CharField(max_length=20, blank=True)
    result = models.CharField(max_length=255, blank=True)
    pnl = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=20, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "algo_pick"
        ordering = ["match_date", "tier", "-confidence", "-ev"]

    def __str__(self):
        return f"{self.fixture} - {self.market}"


class PickBack(models.Model):
    pick = models.ForeignKey(Pick, on_delete=models.CASCADE, related_name="backs")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="backed_picks",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "algo_pickback"
        unique_together = ("pick", "user")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} backed {self.pick}"


class GameBack(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="backed_games",
    )
    match_id = models.CharField(max_length=100)
    match_date = models.DateField(null=True, blank=True)
    market = models.CharField(max_length=120, blank=True)
    meaning = models.CharField(max_length=255, blank=True)
    odds = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    final_confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    ev = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    market_snapshot = models.JSONField(default=dict, blank=True)
    fixture = models.ForeignKey(
        AlgoFixture,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="backs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "algo_gameback"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "match_date"]),
            models.Index(fields=["match_id"]),
            models.Index(fields=["match_id", "market"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "match_id", "market"],
                name="unique_game_back_user_match_market",
            )
        ]

    def __str__(self):
        market = f" ({self.market})" if self.market else ""
        return f"{self.user} backed game {self.match_id}{market}"


class MarketPrediction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        VOID = "void", "Void"

    run = models.ForeignKey(AlgoRun, on_delete=models.CASCADE, related_name="market_predictions")
    selected_pick = models.ForeignKey(
        Pick,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="internal_predictions",
    )
    match_date = models.DateField()
    fixture = models.CharField(max_length=255)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    league = models.CharField(max_length=255, blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    match_id = models.CharField(max_length=100, blank=True)
    market = models.CharField(max_length=100)
    meaning = models.CharField(max_length=255, blank=True)
    raw_confidence = models.PositiveIntegerField(default=0)
    confidence = models.PositiveIntegerField(default=0)
    odds = models.DecimalField(max_digits=8, decimal_places=2)
    ev = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    odds_source = models.CharField(max_length=30, blank=True)
    odds_meta = models.JSONField(default=dict, blank=True)
    eligible = models.BooleanField(default=False)
    published = models.BooleanField(default=False)
    rejection_reason = models.CharField(max_length=255, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    insights = models.JSONField(default=dict, blank=True)
    home_recent_form = models.JSONField(default=dict, blank=True)
    away_recent_form = models.JSONField(default=dict, blank=True)
    fixture_context = models.JSONField(default=dict, blank=True)
    team_news = models.JSONField(default=dict, blank=True)
    score = models.CharField(max_length=20, blank=True)
    result = models.CharField(max_length=255, blank=True)
    pnl_simulated = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "algo_marketprediction"
        ordering = ["match_date", "fixture", "-confidence", "market"]
        indexes = [
            models.Index(fields=["match_date", "status"]),
            models.Index(fields=["market", "status"]),
            models.Index(fields=["league", "market", "status"]),
            models.Index(fields=["published", "status"]),
            models.Index(fields=["match_id", "market"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "match_id", "fixture", "market"],
                name="unique_market_prediction_per_run_fixture_market",
            )
        ]

    def __str__(self):
        label = "published" if self.published else "internal"
        return f"{self.fixture} - {self.market} ({label})"



class StrategyReview(models.Model):
    target_date = models.DateField(unique=True)
    profile = models.JSONField(default=dict, blank=True)
    markets_suppressed = models.JSONField(default=list, blank=True)
    markets_cooling = models.JSONField(default=list, blank=True)
    markets_promoted = models.JSONField(default=list, blank=True)
    league_market_actions = models.JSONField(default=dict, blank=True)
    league_warnings = models.JSONField(default=list, blank=True)
    daily_policy = models.CharField(max_length=100, default="adaptive_market_memory")
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_strategyreview"
        ordering = ["-target_date"]
        indexes = [
            models.Index(fields=["target_date"]),
        ]

    def __str__(self):
        return f"Strategy review {self.target_date}"
