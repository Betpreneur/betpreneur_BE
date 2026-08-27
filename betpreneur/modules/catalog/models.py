"""Fixtures, provider identity maps and cached market evaluations.

Table names are pinned to their original algo_* values — this refactor moves
Python packages, never data.
"""
from django.db import models


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
        db_table = "algo_fixturecache"
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


class TeamProfile(models.Model):
    """Provider-neutral team identity for the Team Intelligence Store."""

    canonical_name = models.CharField(max_length=255)
    canonical_normalized = models.CharField(max_length=255, unique=True)
    country = models.CharField(max_length=100, blank=True)
    primary_league_key = models.CharField(max_length=120, blank=True)
    primary_league_name = models.CharField(max_length=255, blank=True)
    provider_ids = models.JSONField(default=dict, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_teamprofile"
        ordering = ["canonical_name"]
        indexes = [
            models.Index(fields=["country", "canonical_name"]),
            models.Index(fields=["primary_league_key"]),
            models.Index(fields=["active"]),
        ]

    def __str__(self):
        return self.canonical_name


class TeamSeasonProfile(models.Model):
    """Season-level team facts used as the stable baseline for match analysis."""

    class DataQuality(models.TextChoices):
        STRONG = "strong", "Strong"
        MEDIUM = "medium", "Medium"
        LIMITED = "limited", "Limited"
        POOR = "poor", "Poor"
        MISSING = "missing", "Missing"

    team = models.ForeignKey(TeamProfile, on_delete=models.CASCADE, related_name="season_profiles")
    league_key = models.CharField(max_length=120)
    league_name = models.CharField(max_length=255)
    country = models.CharField(max_length=100, blank=True)
    season = models.CharField(max_length=32)
    provider_ids = models.JSONField(default=dict, blank=True)
    matches_played = models.PositiveIntegerField(default=0)
    home_matches = models.PositiveIntegerField(default=0)
    away_matches = models.PositiveIntegerField(default=0)
    goals_for = models.FloatField(null=True, blank=True)
    goals_against = models.FloatField(null=True, blank=True)
    home_goals_for = models.FloatField(null=True, blank=True)
    home_goals_against = models.FloatField(null=True, blank=True)
    away_goals_for = models.FloatField(null=True, blank=True)
    away_goals_against = models.FloatField(null=True, blank=True)
    xg_for = models.FloatField(null=True, blank=True)
    xg_against = models.FloatField(null=True, blank=True)
    corners_for = models.FloatField(null=True, blank=True)
    corners_against = models.FloatField(null=True, blank=True)
    cards_for = models.FloatField(null=True, blank=True)
    cards_against = models.FloatField(null=True, blank=True)
    shots_for = models.FloatField(null=True, blank=True)
    shots_against = models.FloatField(null=True, blank=True)
    shots_on_target_for = models.FloatField(null=True, blank=True)
    shots_on_target_against = models.FloatField(null=True, blank=True)
    clean_sheet_rate = models.FloatField(null=True, blank=True)
    btts_rate = models.FloatField(null=True, blank=True)
    over_15_rate = models.FloatField(null=True, blank=True)
    over_25_rate = models.FloatField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    data_quality = models.CharField(max_length=20, choices=DataQuality.choices, default=DataQuality.MISSING)
    source = models.CharField(max_length=30, default="derived")
    fetched_at = models.DateTimeField(null=True, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_teamseasonprofile"
        ordering = ["league_name", "season", "team__canonical_name"]
        indexes = [
            models.Index(fields=["league_key", "season"]),
            models.Index(fields=["country", "league_name"]),
            models.Index(fields=["data_quality"]),
            models.Index(fields=["updated_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "league_key", "season"],
                name="unique_team_season_profile",
            )
        ]

    def __str__(self):
        return f"{self.team} {self.league_name} {self.season}"


class TeamRecentFormProfile(models.Model):
    """Rolling team form windows, split by all/home/away scopes."""

    class Scope(models.TextChoices):
        ALL = "all", "All"
        HOME = "home", "Home"
        AWAY = "away", "Away"

    team = models.ForeignKey(TeamProfile, on_delete=models.CASCADE, related_name="recent_form_profiles")
    league_key = models.CharField(max_length=120, blank=True)
    league_name = models.CharField(max_length=255, blank=True)
    season = models.CharField(max_length=32, blank=True)
    window = models.PositiveSmallIntegerField(default=5)
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.ALL)
    matches = models.PositiveSmallIntegerField(default=0)
    wins = models.PositiveSmallIntegerField(default=0)
    draws = models.PositiveSmallIntegerField(default=0)
    losses = models.PositiveSmallIntegerField(default=0)
    goals_for = models.FloatField(null=True, blank=True)
    goals_against = models.FloatField(null=True, blank=True)
    xg_for = models.FloatField(null=True, blank=True)
    xg_against = models.FloatField(null=True, blank=True)
    corners_for = models.FloatField(null=True, blank=True)
    corners_against = models.FloatField(null=True, blank=True)
    cards_for = models.FloatField(null=True, blank=True)
    cards_against = models.FloatField(null=True, blank=True)
    shots_on_target_for = models.FloatField(null=True, blank=True)
    shots_on_target_against = models.FloatField(null=True, blank=True)
    form = models.JSONField(default=list, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=30, default="derived")
    computed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_teamrecentformprofile"
        ordering = ["team__canonical_name", "window", "scope"]
        indexes = [
            models.Index(fields=["team", "window", "scope"]),
            models.Index(fields=["league_key", "season"]),
            models.Index(fields=["computed_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "league_key", "season", "window", "scope"],
                name="unique_team_recent_form_profile",
            )
        ]

    def __str__(self):
        return f"{self.team} last {self.window} ({self.scope})"


class TeamMarketProfile(models.Model):
    """Team behaviour for a market or market family in a season."""

    class Scope(models.TextChoices):
        ALL = "all", "All"
        HOME = "home", "Home"
        AWAY = "away", "Away"

    team = models.ForeignKey(TeamProfile, on_delete=models.CASCADE, related_name="market_profiles")
    league_key = models.CharField(max_length=120)
    league_name = models.CharField(max_length=255)
    season = models.CharField(max_length=32)
    market_family = models.CharField(max_length=80)
    market = models.CharField(max_length=120)
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.ALL)
    side = models.CharField(max_length=20, blank=True)
    line = models.FloatField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    voids = models.PositiveIntegerField(default=0)
    hit_rate = models.FloatField(null=True, blank=True)
    avg_odds = models.FloatField(null=True, blank=True)
    roi_flat = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    data_quality = models.CharField(max_length=20, default="missing")
    stats = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=30, default="derived")
    computed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_teammarketprofile"
        ordering = ["league_name", "season", "team__canonical_name", "market"]
        indexes = [
            models.Index(fields=["team", "market_family"]),
            models.Index(fields=["league_key", "season", "market_family"]),
            models.Index(fields=["market", "scope"]),
            models.Index(fields=["data_quality"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "league_key", "season", "market", "scope"],
                name="unique_team_market_profile",
            )
        ]

    def __str__(self):
        return f"{self.team} {self.market} ({self.scope})"


class LeagueMarketProfile(models.Model):
    """League-wide market reliability for Team Intelligence."""

    league_key = models.CharField(max_length=120)
    league_name = models.CharField(max_length=255)
    country = models.CharField(max_length=100, blank=True)
    season = models.CharField(max_length=32)
    provider_ids = models.JSONField(default=dict, blank=True)
    market_family = models.CharField(max_length=80)
    market = models.CharField(max_length=120)
    side = models.CharField(max_length=20, blank=True)
    line = models.FloatField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    voids = models.PositiveIntegerField(default=0)
    hit_rate = models.FloatField(null=True, blank=True)
    avg_odds = models.FloatField(null=True, blank=True)
    roi_flat = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    fairness_score = models.FloatField(null=True, blank=True)
    volatility = models.FloatField(null=True, blank=True)
    data_quality = models.CharField(max_length=20, default="missing")
    stats = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=30, default="derived")
    computed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_leaguemarketprofile"
        ordering = ["league_name", "season", "market"]
        indexes = [
            models.Index(fields=["league_key", "season"]),
            models.Index(fields=["market_family", "market"]),
            models.Index(fields=["data_quality"]),
            models.Index(fields=["fairness_score"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["league_key", "season", "market"],
                name="unique_league_market_profile",
            )
        ]

    def __str__(self):
        return f"{self.league_name} {self.market} {self.season}"


class DataCoverage(models.Model):
    """Freshness and missing-data tracker for intelligence hydration."""

    class SubjectType(models.TextChoices):
        TEAM = "team", "Team"
        LEAGUE = "league", "League"
        MARKET = "market", "Market"
        FIXTURE = "fixture", "Fixture"

    class Status(models.TextChoices):
        FRESH = "fresh", "Fresh"
        STALE = "stale", "Stale"
        PARTIAL = "partial", "Partial"
        MISSING = "missing", "Missing"
        FAILED = "failed", "Failed"

    subject_type = models.CharField(max_length=20, choices=SubjectType.choices)
    subject_key = models.CharField(max_length=255)
    team = models.ForeignKey(
        TeamProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="coverage_rows",
    )
    league_key = models.CharField(max_length=120, blank=True)
    league_name = models.CharField(max_length=255, blank=True)
    season = models.CharField(max_length=32, blank=True)
    provider = models.CharField(max_length=30)
    coverage_key = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.MISSING)
    freshness_seconds = models.PositiveIntegerField(null=True, blank=True)
    available_requirements = models.JSONField(default=list, blank=True)
    missing_requirements = models.JSONField(default=list, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_datacoverage"
        ordering = ["status", "subject_type", "subject_key", "coverage_key"]
        indexes = [
            models.Index(fields=["subject_type", "subject_key"]),
            models.Index(fields=["league_key", "season"]),
            models.Index(fields=["provider", "coverage_key"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["team", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["subject_type", "subject_key", "provider", "coverage_key"],
                name="unique_data_coverage_subject_provider_key",
            )
        ]

    def __str__(self):
        return f"{self.subject_type}:{self.subject_key} {self.coverage_key} ({self.status})"


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
        db_table = "algo_bookmakerleaguemap"
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
        db_table = "algo_teamaliasmap"
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
        db_table = "algo_providerteammap"
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
        db_table = "algo_providerplayermap"
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
        db_table = "algo_providerfixturemap"
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
        db_table = "algo_statpalfixturesnapshot"
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
        db_table = "algo_slipreviewmarketcache"
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
