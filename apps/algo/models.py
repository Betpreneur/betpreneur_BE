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


class StatPalFixtureCoverage(FixtureCache):
    class Meta:
        proxy = True
        verbose_name = "StatPal Fixture Coverage"
        verbose_name_plural = "StatPal Fixture Coverage"


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


class ProviderTeamMap(models.Model):
    provider = models.CharField(max_length=30)
    provider_team_id = models.CharField(max_length=120)
    provider_team_name = models.CharField(max_length=255)
    provider_team_normalized = models.CharField(max_length=255, blank=True)
    internal_team_id = models.CharField(max_length=120, blank=True)
    internal_team_name = models.CharField(max_length=255, blank=True)
    internal_team_normalized = models.CharField(max_length=255, blank=True)
    api_team_id = models.PositiveIntegerField(null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    resolution_method = models.CharField(max_length=80, default="provider_id")
    active = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "provider_team_name"]
        indexes = [
            models.Index(fields=["provider", "provider_team_id"]),
            models.Index(fields=["provider", "provider_team_normalized"]),
            models.Index(fields=["internal_team_id"]),
            models.Index(fields=["api_team_id"]),
            models.Index(fields=["active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_team_id"],
                name="unique_provider_team_map_provider_team",
            )
        ]

    def __str__(self):
        target = self.internal_team_name or self.api_team_id or self.internal_team_id or "unmapped"
        return f"{self.provider}: {self.provider_team_name} -> {target}"


class ProviderPlayerMap(models.Model):
    provider = models.CharField(max_length=30)
    provider_player_id = models.CharField(max_length=120)
    provider_player_name = models.CharField(max_length=255)
    provider_player_normalized = models.CharField(max_length=255, blank=True)
    internal_player_id = models.CharField(max_length=120, blank=True)
    internal_player_name = models.CharField(max_length=255, blank=True)
    internal_player_normalized = models.CharField(max_length=255, blank=True)
    provider_team_id = models.CharField(max_length=120, blank=True)
    provider_team_name = models.CharField(max_length=255, blank=True)
    internal_team_id = models.CharField(max_length=120, blank=True)
    internal_team_name = models.CharField(max_length=255, blank=True)
    position = models.CharField(max_length=80, blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    resolution_method = models.CharField(max_length=80, default="provider_id")
    active = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "provider_player_name"]
        indexes = [
            models.Index(fields=["provider", "provider_player_id"]),
            models.Index(fields=["provider", "provider_player_normalized"]),
            models.Index(fields=["internal_player_id"]),
            models.Index(fields=["provider_team_id"]),
            models.Index(fields=["active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_player_id"],
                name="unique_provider_player_map_provider_player",
            )
        ]

    def __str__(self):
        target = self.internal_player_name or self.internal_player_id or "unmapped"
        return f"{self.provider}: {self.provider_player_name} -> {target}"


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


class StatPalFixtureSnapshot(models.Model):
    class SnapshotType(models.TextChoices):
        INJURIES_SUSPENSIONS = "injuries_suspensions", "Injuries & Suspensions"
        TEAM_STATS = "team_stats", "Team Stats"
        PREMATCH_ODDS = "prematch_odds", "Pre-Match Odds"
        LIVE_ODDS = "live_odds", "Live Odds"
        LINEUPS = "lineups", "Lineups"
        PREDICTIONS = "predictions", "Predictions"
        DETAILED_STATS = "detailed_stats", "Detailed Stats"
        HEAD_TO_HEAD = "head_to_head", "Head to Head"
        LEAGUE_STANDINGS = "league_standings", "League Standings"
        LEAGUE_STATS = "league_stats", "League Stats"
        WEATHER_FORECAST = "weather_forecast", "Weather Forecast"
        PLAYER_STATS = "player_stats", "Player Stats"
        COACH = "coach", "Coach"
        IMAGES = "images", "Images"
        LIVE_STORYLINES = "live_storylines", "Live Storylines"
        RAW = "raw", "Raw"

    provider_fixture = models.ForeignKey(
        ProviderFixtureMap,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="statpal_snapshots",
    )
    fixture = models.ForeignKey(
        FixtureCache,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="statpal_snapshots",
    )
    match_id = models.CharField(max_length=120, blank=True)
    provider_match_id = models.CharField(max_length=120, blank=True)
    provider_competition_id = models.CharField(max_length=100, blank=True)
    snapshot_type = models.CharField(max_length=40, choices=SnapshotType.choices)
    source_endpoint = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=30, default="available")
    payload = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fetched_at", "-updated_at"]
        indexes = [
            models.Index(fields=["match_id", "snapshot_type"]),
            models.Index(fields=["provider_match_id", "snapshot_type"]),
            models.Index(fields=["provider_competition_id", "snapshot_type"]),
            models.Index(fields=["snapshot_type", "status"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["match_id", "provider_match_id", "snapshot_type"],
                name="unique_statpal_snapshot_fixture_type",
            )
        ]

    def __str__(self):
        target = self.match_id or self.provider_match_id or "unmapped"
        return f"StatPal {self.snapshot_type} for {target}"


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
        UNANALYSED = "unanalysed", "Unanalysed"
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
    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        VOID = "void", "Void"
        UNSETTLEABLE = "unsettleable", "Unsettleable"

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
    settlement_market = models.CharField(max_length=120, blank=True)
    odds = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    # Pre-kickoff advisory score, denormalised so calibration is an aggregate query
    # rather than a scan over analysis_payload JSON.
    advisory_score = models.FloatField(null=True, blank=True)
    flagged_risky = models.BooleanField(default=False)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.PENDING)
    score = models.CharField(max_length=20, blank=True)
    result = models.CharField(max_length=120, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["review", "order"]),
            models.Index(fields=["match_id"]),
            models.Index(fields=["status", "verdict"]),
            models.Index(fields=["outcome", "match_date"]),
            models.Index(fields=["review", "outcome"]),
        ]

    def __str__(self):
        return f"{self.submitted_match} - {self.submitted_market}"

    @property
    def market(self):
        """Canonical market name, so settlement can reuse ``AlgoRunnerService._check_market``."""
        return self.settlement_market


class SlipLegAnalysisCache(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    cache_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    source = models.CharField(max_length=30, blank=True)
    provider_event_id = models.CharField(max_length=120, blank=True)
    match_text = models.CharField(max_length=255, blank=True)
    market_text = models.CharField(max_length=160, blank=True)
    match_id = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["cache_key"], name="algo_sliple_cache_k_b5c8db_idx"),
            models.Index(fields=["source", "provider_event_id"], name="algo_sliple_source_4f3934_idx"),
            models.Index(fields=["match_id"], name="algo_sliple_match_i_cbbd1c_idx"),
            models.Index(fields=["status", "lock_expires_at"], name="algo_sliple_status_72ca48_idx"),
            models.Index(fields=["expires_at"], name="algo_sliple_expires_1ef28c_idx"),
        ]

    def __str__(self):
        return f"{self.match_text} - {self.market_text}"


class SlipReviewEvent(models.Model):
    review = models.ForeignKey(SlipReview, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["review", "created_at"], name="algo_slipre_review__47a5be_idx"),
            models.Index(fields=["review", "event_type"], name="algo_slipre_review__4f4768_idx"),
        ]

    def __str__(self):
        return f"{self.review_id} {self.event_type}"


class SlipReviewStreamToken(models.Model):
    review = models.ForeignKey(SlipReview, on_delete=models.CASCADE, related_name="stream_tokens")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="slip_review_stream_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["token_hash"], name="algo_slipre_token_h_1d9e88_idx"),
            models.Index(fields=["review", "user"], name="algo_slipre_review__75fc80_idx"),
            models.Index(fields=["expires_at"], name="algo_slipre_expires_57f904_idx"),
        ]

    def __str__(self):
        return f"Stream token for review #{self.review_id}"


class TokenWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_wallet",
    )
    free_tokens = models.PositiveIntegerField(default=0)
    paid_tokens = models.PositiveIntegerField(default=0)
    last_free_refill_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"], name="algo_tokenw_user_id_a075d4_idx"),
            models.Index(fields=["last_free_refill_date"], name="algo_tokenw_last_fr_8b467c_idx"),
        ]

    @property
    def total_tokens(self):
        return int(self.free_tokens or 0) + int(self.paid_tokens or 0)

    def __str__(self):
        return f"Token wallet for {self.user}"


class TokenTransaction(models.Model):
    class TokenBucket(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        MIXED = "mixed", "Mixed"

    class Reason(models.TextChoices):
        SIGNUP_GRANT = "signup_grant", "Signup Grant"
        DAILY_FREE_REFILL = "daily_free_refill", "Daily Free Refill"
        TOKEN_PURCHASE_CREDIT = "token_purchase_credit", "Token Purchase Credit"
        SLIP_REVIEW_RESERVE = "slip_review_reserve", "Slip Review Reserve"
        SLIP_REVIEW_CONSUME = "slip_review_consume", "Slip Review Consume"
        SLIP_REVIEW_RELEASE = "slip_review_release", "Slip Review Release"
        TOKEN_RESERVATION_EXPIRE = "token_reservation_expire", "Token Reservation Expire"
        SMART_RANDOMIZE_CHARGE = "smart_randomize_charge", "Smart Randomize Charge"
        ADMIN_ADJUSTMENT = "admin_adjustment", "Admin Adjustment"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_transactions",
    )
    wallet = models.ForeignKey(TokenWallet, on_delete=models.CASCADE, related_name="transactions")
    amount = models.IntegerField(help_text="Positive credits tokens; negative debits tokens.")
    free_tokens_delta = models.IntegerField(default=0)
    paid_tokens_delta = models.IntegerField(default=0)
    token_bucket = models.CharField(max_length=20, choices=TokenBucket.choices, default=TokenBucket.MIXED)
    reason = models.CharField(max_length=40, choices=Reason.choices)
    reference_type = models.CharField(max_length=80, blank=True)
    reference_id = models.CharField(max_length=120, blank=True)
    balance_after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="algo_tokent_user_id_f2f456_idx"),
            models.Index(fields=["reference_type", "reference_id"], name="algo_tokent_referen_844759_idx"),
            models.Index(fields=["reason", "created_at"], name="algo_tokent_reason_0318f1_idx"),
        ]

    def __str__(self):
        sign = "+" if self.amount > 0 else ""
        return f"{sign}{self.amount} tokens for {self.user} ({self.reason})"


class TokenReservation(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_reservations",
    )
    wallet = models.ForeignKey(TokenWallet, on_delete=models.CASCADE, related_name="reservations")
    amount = models.PositiveIntegerField()
    free_tokens_reserved = models.PositiveIntegerField(default=0)
    paid_tokens_reserved = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RESERVED)
    reference_type = models.CharField(max_length=80, blank=True)
    reference_id = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"], name="algo_tokenr_user_id_4605d0_idx"),
            models.Index(fields=["reference_type", "reference_id"], name="algo_tokenr_referen_32a438_idx"),
            models.Index(fields=["status", "expires_at"], name="algo_tokenr_status__c5782f_idx"),
        ]

    def __str__(self):
        return f"{self.amount} tokens reserved for {self.user} ({self.status})"


class TokenPurchase(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_purchases",
    )
    package_id = models.CharField(max_length=120)
    tokens = models.PositiveIntegerField()
    amount = models.PositiveIntegerField(help_text="Major currency unit, e.g. naira.")
    amount_kobo = models.PositiveIntegerField()
    currency = models.CharField(max_length=10, default="NGN")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=40, blank=True)
    provider_reference = models.CharField(max_length=160, blank=True)
    credited_transaction = models.ForeignKey(
        TokenTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credited_token_purchases",
    )
    metadata = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"], name="algo_tokenp_user_id_80d2d6_idx"),
            models.Index(fields=["package_id", "status"], name="algo_tokenp_package_3e1f55_idx"),
            models.Index(fields=["provider", "provider_reference"], name="algo_tokenp_prov_4f3a41_idx"),
            models.Index(fields=["status", "created_at"], name="algo_tokenp_status_3916fd_idx"),
        ]

    def __str__(self):
        return f"{self.tokens} tokens for {self.user} ({self.status})"


class SlipRepair(models.Model):
    """
    A revised version of a ticket, persisted so the user can return to it and so the
    settlement engine can later compare how repaired tickets actually fared.
    """

    class Mode(models.TextChoices):
        RECOMMENDED = "recommended", "Recommended"
        CUSTOM = "custom", "Custom"

    review = models.ForeignKey(SlipReview, on_delete=models.CASCADE, related_name="repairs")
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.RECOMMENDED)
    original_legs = models.PositiveIntegerField(default=0)
    original_combined_odds = models.FloatField(null=True, blank=True)
    original_success_percent = models.FloatField(null=True, blank=True)
    revised_legs = models.PositiveIntegerField(default=0)
    revised_combined_odds = models.FloatField(null=True, blank=True)
    revised_success_percent = models.FloatField(null=True, blank=True)
    changes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["review", "created_at"])]

    def __str__(self):
        return f"Repair of review #{self.review_id}"


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


class SlipReviewMarketCache(models.Model):
    """
    Private pre-scored market cache used by Match Checker slip reviews.

    Unlike MarketPrediction, these rows are not public top-pick candidates. They can
    cover broad provider fixture universes while all-games/top-picks remain restricted
    to the curated public league list.
    """

    class Scope(models.TextChoices):
        SLIP_REVIEW = "slip_review", "Slip Review"

    class Source(models.TextChoices):
        STATPAL = "statpal", "StatPal"
        API_FOOTBALL = "api_football", "API-Football"
        MERGED = "merged", "Merged"
        ON_DEMAND = "on_demand", "On Demand"

    cache_scope = models.CharField(max_length=30, choices=Scope.choices, default=Scope.SLIP_REVIEW)
    source = models.CharField(max_length=30, choices=Source.choices, default=Source.MERGED)
    match_date = models.DateField()
    fixture = models.CharField(max_length=255)
    home_team = models.CharField(max_length=255, blank=True)
    away_team = models.CharField(max_length=255, blank=True)
    home_logo = models.URLField(blank=True)
    away_logo = models.URLField(blank=True)
    league = models.CharField(max_length=255, blank=True)
    league_id = models.CharField(max_length=100, blank=True)
    league_logo = models.URLField(blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_flag = models.URLField(blank=True)
    kickoff = models.CharField(max_length=50, blank=True)
    match_id = models.CharField(max_length=100)
    provider_match_id = models.CharField(max_length=120, blank=True)
    provider_competition_id = models.CharField(max_length=120, blank=True)
    home_team_id = models.CharField(max_length=120, blank=True)
    away_team_id = models.CharField(max_length=120, blank=True)
    market = models.CharField(max_length=120)
    market_family = models.CharField(max_length=80, blank=True)
    meaning = models.CharField(max_length=255, blank=True)
    raw_confidence = models.PositiveIntegerField(default=0)
    confidence = models.PositiveIntegerField(default=0)
    final_confidence = models.FloatField(null=True, blank=True)
    odds = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    ev = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    odds_source = models.CharField(max_length=30, blank=True)
    odds_meta = models.JSONField(default=dict, blank=True)
    eligible = models.BooleanField(default=False)
    risk_flags = models.JSONField(default=list, blank=True)
    insights = models.JSONField(default=dict, blank=True)
    market_payload = models.JSONField(default=dict, blank=True)
    fixture_payload = models.JSONField(default=dict, blank=True)
    provider_merge = models.JSONField(default=dict, blank=True)
    data_quality = models.CharField(max_length=30, blank=True)
    cache_version = models.CharField(max_length=40, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match_date", "fixture", "-confidence", "market"]
        indexes = [
            models.Index(fields=["cache_scope", "match_date"]),
            models.Index(fields=["cache_scope", "match_id", "market"]),
            models.Index(fields=["cache_scope", "provider_match_id", "market"]),
            models.Index(fields=["cache_scope", "league_id", "match_date"]),
            models.Index(fields=["cache_scope", "market_family", "confidence"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["cache_scope", "match_id", "market"],
                name="unique_slip_review_market_cache_match_market",
            )
        ]

    def __str__(self):
        return f"{self.fixture} - {self.market} ({self.cache_scope})"


class LeagueScoreModel(models.Model):
    """
    Fitted goal-scoring parameters for one league, refreshed nightly.

    Request-time work is a lookup plus a matrix summation, so fitting happens here and
    never on the critical path of a slip review.
    """

    class DataQuality(models.TextChoices):
        STRONG = "strong", "Strong"        # goal splits plus shot volume
        MEDIUM = "medium", "Medium"        # home/away goal splits only
        LIMITED = "limited", "Limited"     # overall goals only
        POOR = "poor", "Poor"              # league baseline only

    provider = models.CharField(max_length=30, default="statpal")
    league_id = models.CharField(max_length=64)
    league_name = models.CharField(max_length=255, blank=True)
    season = models.CharField(max_length=32, blank=True)
    model_version = models.CharField(max_length=32)
    home_goal_baseline = models.FloatField(default=1.35)
    away_goal_baseline = models.FloatField(default=1.10)
    rho = models.FloatField(default=-0.13)
    data_quality = models.CharField(max_length=20, choices=DataQuality.choices, default=DataQuality.POOR)
    teams_fitted = models.PositiveIntegerField(default=0)
    matches_observed = models.PositiveIntegerField(default=0)
    prior_season = models.CharField(max_length=32, blank=True)
    diagnostics = models.JSONField(default=dict, blank=True)
    fitted_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "league_id", "model_version"],
                name="unique_league_score_model",
            )
        ]
        indexes = [models.Index(fields=["provider", "league_id"])]

    def __str__(self):
        return f"{self.league_name or self.league_id} ({self.model_version})"


class TeamStrength(models.Model):
    """Multiplicative attack/defence strengths, split home and away."""

    model = models.ForeignKey(LeagueScoreModel, on_delete=models.CASCADE, related_name="teams")
    team_id = models.CharField(max_length=64, blank=True)
    team_name = models.CharField(max_length=255)
    team_name_normalized = models.CharField(max_length=255, db_index=True)
    home_attack = models.FloatField(default=1.0)
    home_defence = models.FloatField(default=1.0)
    away_attack = models.FloatField(default=1.0)
    away_defence = models.FloatField(default=1.0)
    matches = models.PositiveIntegerField(default=0)
    # Evidence carried over from the previous season's fit. `matches` alone is a
    # current-season count, which is zero for every team in August.
    prior_matches = models.PositiveIntegerField(default=0)
    prior_season = models.CharField(max_length=32, blank=True)
    shots_per_game = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["model", "team_name_normalized"]),
            models.Index(fields=["model", "team_id"]),
        ]

    def __str__(self):
        return f"{self.team_name} ({self.model_id})"


class FixtureLineup(models.Model):
    """
    Projected or confirmed team sheet for one side of a fixture.

    `confidence` distinguishes the two: StatPal reports 100 once a lineup is confirmed.
    Only a confirmed sheet justifies refusing to price a player prop — a projected sheet
    that omits someone is a signal, not a fact.
    """

    provider = models.CharField(max_length=30, default="statpal")
    match_id = models.CharField(max_length=64)
    side = models.CharField(max_length=10)  # home | away
    team_id = models.CharField(max_length=64, blank=True)
    team_name = models.CharField(max_length=255, blank=True)
    team_name_normalized = models.CharField(max_length=255, blank=True, db_index=True)
    formation = models.CharField(max_length=32, blank=True)
    confidence = models.PositiveIntegerField(default=0)
    starting_xi = models.JSONField(default=list, blank=True)
    bench = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "match_id", "side"], name="unique_fixture_lineup"
            )
        ]
        indexes = [models.Index(fields=["match_id", "side"])]

    @property
    def confirmed(self) -> bool:
        return self.confidence >= 100

    def __str__(self):
        return f"{self.team_name or self.side} lineup for {self.match_id}"


class PlayerAvailability(models.Model):
    """
    Who is injured or suspended, from StatPal's injuries/suspensions feed.

    A player prop on someone who will not take the field is not a risky bet — it is a
    dead one, and must be reported as unavailable rather than scored down. Identity is
    matched on name and team because SportyBet's Sportradar player ids and StatPal's own
    ids are different id spaces with no overlap.
    """

    class Status(models.TextChoices):
        OUT = "out", "Out"                     # to_miss: injured or suspended
        DOUBTFUL = "doubtful", "Doubtful"      # questionable

    provider = models.CharField(max_length=30, default="statpal")
    player_id = models.CharField(max_length=64, blank=True)
    player_name = models.CharField(max_length=255)
    player_name_normalized = models.CharField(max_length=255, db_index=True)
    team_id = models.CharField(max_length=64, blank=True)
    team_name = models.CharField(max_length=255, blank=True)
    team_name_normalized = models.CharField(max_length=255, blank=True, db_index=True)
    match_id = models.CharField(max_length=64, blank=True)
    match_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OUT)
    reason = models.CharField(max_length=120, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["player_name_normalized", "match_id"]),
            models.Index(fields=["team_name_normalized", "match_date"]),
        ]

    def __str__(self):
        return f"{self.player_name} ({self.status})"


class TeamRateProfile(models.Model):
    """
    Per-game corner, card and shots-on-target rates for one team, cached from
    `soccer/teams/{id}`.

    These are not available on the match-stats endpoint, which is why the earlier
    corners and cards evaluators never fired and fell back to a constant.
    """

    provider = models.CharField(max_length=30, default="statpal")
    team_id = models.CharField(max_length=64)
    team_name = models.CharField(max_length=255, blank=True)
    team_name_normalized = models.CharField(max_length=255, db_index=True, blank=True)
    league_id = models.CharField(max_length=64, blank=True)
    corners_home = models.FloatField(null=True, blank=True)
    corners_away = models.FloatField(null=True, blank=True)
    cards_home = models.FloatField(null=True, blank=True)
    cards_away = models.FloatField(null=True, blank=True)
    shots_on_target_home = models.FloatField(null=True, blank=True)
    shots_on_target_away = models.FloatField(null=True, blank=True)
    fouls_per_game = models.FloatField(null=True, blank=True)
    matches = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "team_id"], name="unique_team_rate_profile")
        ]
        indexes = [
            models.Index(fields=["provider", "team_name_normalized"]),
            models.Index(fields=["league_id"]),
        ]

    def __str__(self):
        return f"{self.team_name or self.team_id} rates"


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
