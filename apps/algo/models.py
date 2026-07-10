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


class FixtureCache(models.Model):
    match_date = models.DateField()
    fixture = models.CharField(max_length=255)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    home_team_normalized = models.CharField(max_length=255, blank=True)
    away_team_normalized = models.CharField(max_length=255, blank=True)
    fixture_normalized = models.CharField(max_length=520, blank=True)
    home_logo = models.URLField(blank=True)
    away_logo = models.URLField(blank=True)
    league = models.CharField(max_length=255, blank=True)
    league_logo = models.URLField(blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_flag = models.URLField(blank=True)
    round = models.CharField(max_length=255, blank=True)
    league_type = models.CharField(max_length=50, blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    kickoff_utc = models.DateTimeField(null=True, blank=True)
    match_id = models.CharField(max_length=100, unique=True)
    api_payload = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=30, default="api_football")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match_date", "country", "league", "kickoff", "fixture"]
        indexes = [
            models.Index(fields=["match_date"]),
            models.Index(fields=["home_team_normalized"]),
            models.Index(fields=["away_team_normalized"]),
            models.Index(fields=["fixture_normalized"]),
            models.Index(fields=["country", "league"]),
        ]

    def __str__(self):
        return self.fixture


class BookmakerLeagueMap(models.Model):
    provider = models.CharField(max_length=30)
    provider_competition_id = models.CharField(max_length=100, blank=True)
    provider_competition_name = models.CharField(max_length=255)
    provider_competition_normalized = models.CharField(max_length=255, blank=True)
    api_league_id = models.PositiveIntegerField()
    api_league_name = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=100, blank=True)
    current_api_season = models.PositiveIntegerField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=50, default="auto")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "provider_competition_name"]
        indexes = [
            models.Index(fields=["provider", "provider_competition_id"]),
            models.Index(fields=["provider", "provider_competition_normalized"]),
            models.Index(fields=["api_league_id"]),
            models.Index(fields=["active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_competition_id", "provider_competition_normalized"],
                name="unique_bookmaker_league_map_provider_competition",
            )
        ]

    def __str__(self):
        return f"{self.provider}: {self.provider_competition_name} -> {self.api_league_name or self.api_league_id}"


class TeamAliasMap(models.Model):
    provider = models.CharField(max_length=30, blank=True)
    api_team_id = models.PositiveIntegerField(null=True, blank=True)
    canonical_name = models.CharField(max_length=255)
    canonical_normalized = models.CharField(max_length=255, blank=True)
    alias = models.CharField(max_length=255)
    alias_normalized = models.CharField(max_length=255)
    country = models.CharField(max_length=100, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=50, default="auto")
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["alias"]
        indexes = [
            models.Index(fields=["provider", "alias_normalized"]),
            models.Index(fields=["api_team_id"]),
            models.Index(fields=["canonical_normalized"]),
            models.Index(fields=["active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "alias_normalized", "canonical_normalized"],
                name="unique_team_alias_provider_alias_canonical",
            )
        ]

    def __str__(self):
        provider = f"{self.provider}: " if self.provider else ""
        return f"{provider}{self.alias} -> {self.canonical_name}"


class ProviderFixtureMap(models.Model):
    provider = models.CharField(max_length=30)
    provider_event_id = models.CharField(max_length=120)
    provider_competition_id = models.CharField(max_length=100, blank=True)
    provider_competition_name = models.CharField(max_length=255, blank=True)
    api_fixture_id = models.CharField(max_length=100)
    api_league_id = models.PositiveIntegerField(null=True, blank=True)
    api_league_name = models.CharField(max_length=255, blank=True)
    provider_home_team = models.CharField(max_length=255, blank=True)
    provider_away_team = models.CharField(max_length=255, blank=True)
    api_home_team = models.CharField(max_length=255, blank=True)
    api_away_team = models.CharField(max_length=255, blank=True)
    kickoff_at = models.DateTimeField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    resolution_method = models.CharField(max_length=80, default="team_date_league")
    active = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "provider_event_id"]
        indexes = [
            models.Index(fields=["provider", "provider_event_id"]),
            models.Index(fields=["api_fixture_id"]),
            models.Index(fields=["provider_competition_id"]),
            models.Index(fields=["active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                name="unique_provider_fixture_map_event",
            )
        ]

    def __str__(self):
        return f"{self.provider}: {self.provider_event_id} -> {self.api_fixture_id}"


class SlipReview(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        SPORTYBET = "sportybet", "SportyBet"
        BETANO = "betano", "Betano"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        IMPORTING = "importing", "Importing"
        ANALYSING = "analysing", "Analysing"
        COMPLETED = "completed", "Completed"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="slip_reviews",
    )
    source = models.CharField(max_length=30, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.COMPLETED)
    title = models.CharField(max_length=255, blank=True)
    submitted_payload = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["source", "status"]),
        ]

    def __str__(self):
        return f"{self.user} {self.source} review #{self.id}"


class SlipSelection(models.Model):
    review = models.ForeignKey(SlipReview, on_delete=models.CASCADE, related_name="selections")
    order = models.PositiveIntegerField(default=0)
    submitted_match = models.CharField(max_length=255)
    submitted_market = models.CharField(max_length=120)
    status = models.CharField(max_length=40, blank=True)
    verdict = models.CharField(max_length=40, blank=True)
    message = models.TextField(blank=True)
    match_id = models.CharField(max_length=100, blank=True)
    match_date = models.DateField(null=True, blank=True)
    fixture = models.CharField(max_length=255, blank=True)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    league = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=100, blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    selected_market = models.JSONField(default=dict, blank=True)
    best_market = models.JSONField(default=dict, blank=True)
    recommended_market = models.JSONField(default=dict, blank=True)
    possible_matches = models.JSONField(default=list, blank=True)
    analysis_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["review", "order"]),
            models.Index(fields=["match_id"]),
            models.Index(fields=["status", "verdict"]),
        ]

    def __str__(self):
        return f"{self.submitted_match} - {self.submitted_market}"


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
        ordering = ["-target_date"]
        indexes = [
            models.Index(fields=["target_date"]),
        ]

    def __str__(self):
        return f"Strategy review {self.target_date}"
