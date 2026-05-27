from django.conf import settings
from django.db import models


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
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.target_date} - {self.status}"


class Pick(models.Model):
    class Tier(models.TextChoices):
        BANKER = "banker", "Banker"
        VALUE_GEM = "value_gem", "Value Gem"
        WILD_CARD = "wild_card", "Wild Card"

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
        unique_together = ("pick", "user")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} backed {self.pick}"


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
