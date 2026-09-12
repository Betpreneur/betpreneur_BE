"""Fixtures, provider identity maps and cached market evaluations.

Table names are pinned to their original algo_* values — this refactor moves
Python packages, never data.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


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


class CoachProfile(models.Model):
    """Provider identity and research state for a football coach."""

    class ResearchStatus(models.TextChoices):
        UNRESEARCHED = "unresearched", "Unresearched"
        DRAFT = "draft", "Draft"
        REVIEWED = "reviewed", "Reviewed"
        APPROVED = "approved", "Approved"
        STALE = "stale", "Stale"

    class Confidence(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    canonical_name = models.CharField(max_length=255)
    canonical_normalized = models.CharField(max_length=255, db_index=True)
    provider = models.CharField(max_length=30, default="statpal")
    provider_coach_id = models.CharField(max_length=120, blank=True)
    provider_name = models.CharField(max_length=255, blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    research_status = models.CharField(
        max_length=20,
        choices=ResearchStatus.choices,
        default=ResearchStatus.UNRESEARCHED,
    )
    research_confidence = models.CharField(
        max_length=20,
        choices=Confidence.choices,
        default=Confidence.UNKNOWN,
    )
    research_sources = models.JSONField(default=list, blank=True)
    research_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_coach_profiles",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_coachprofile"
        ordering = ["canonical_name"]
        indexes = [
            models.Index(fields=["provider", "provider_coach_id"]),
            models.Index(fields=["research_status", "active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_coach_id"],
                condition=~Q(provider_coach_id=""),
                name="unique_catalog_provider_coach",
            )
        ]

    def __str__(self):
        return self.canonical_name


class TeamCoachAssignment(models.Model):
    """Effective-dated team/coach relationship discovered from a provider."""

    class Role(models.TextChoices):
        PERMANENT = "permanent", "Permanent"
        INTERIM = "interim", "Interim"
        CARETAKER = "caretaker", "Caretaker"
        UNKNOWN = "unknown", "Unknown"

    class DatePrecision(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        ESTIMATED = "estimated", "Estimated"
        DETECTED = "detected", "First detected"

    team = models.ForeignKey(TeamProfile, on_delete=models.CASCADE, related_name="coach_assignments")
    coach = models.ForeignKey(CoachProfile, on_delete=models.PROTECT, related_name="team_assignments")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.UNKNOWN)
    started_on = models.DateField(null=True, blank=True)
    ended_on = models.DateField(null=True, blank=True)
    date_precision = models.CharField(
        max_length=20,
        choices=DatePrecision.choices,
        default=DatePrecision.DETECTED,
    )
    currently_active = models.BooleanField(default=True)
    change_reason = models.CharField(max_length=80, blank=True)
    provider = models.CharField(max_length=30, default="statpal")
    provider_team_id = models.CharField(max_length=120, blank=True)
    provider_coach_id = models.CharField(max_length=120, blank=True)
    provider_team_name = models.CharField(max_length=255, blank=True)
    provider_coach_name = models.CharField(max_length=255, blank=True)
    first_detected_at = models.DateTimeField()
    last_confirmed_at = models.DateTimeField()
    provider_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_teamcoachassignment"
        ordering = ["-currently_active", "team__canonical_name", "-first_detected_at"]
        indexes = [
            models.Index(fields=["currently_active", "last_confirmed_at"]),
            models.Index(fields=["provider", "provider_team_id"]),
            models.Index(fields=["provider", "provider_coach_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=Q(currently_active=True),
                name="unique_current_coach_per_team",
            )
        ]

    def __str__(self):
        return f"{self.team} - {self.coach}"

    def clean(self):
        errors = {}
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            errors["ended_on"] = "The end date cannot be earlier than the start date."
        if self.currently_active and self.ended_on:
            errors["ended_on"] = "A current assignment cannot have an end date."
        if errors:
            raise ValidationError(errors)


class CoachTacticalProfile(models.Model):
    """Versioned, human-reviewed tactical intelligence for a coach."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        ARCHIVED = "archived", "Archived"

    RATING_VALIDATORS = [MinValueValidator(0), MaxValueValidator(100)]
    RATING_FIELDS = (
        "possession_tendency",
        "build_up_patience",
        "passing_directness",
        "attacking_tempo",
        "attacking_width",
        "crossing_tendency",
        "counterattack_tendency",
        "attacking_risk",
        "pressing_intensity",
        "defensive_block_height",
        "defensive_line_height",
        "defensive_compactness",
        "transition_defence",
        "defensive_aggression",
        "set_piece_emphasis",
        "rotation_tendency",
        "tactical_flexibility",
        "youth_usage",
    )
    NARRATIVE_TARGETS = {
        "philosophy_summary": 160,
        "attacking_style": 40,
        "defensive_style": 40,
        "build_up_style": 40,
        "leading_approach": 80,
        "trailing_approach": 80,
        "attacking_notes": 120,
        "defensive_notes": 120,
        "match_management_notes": 120,
        "set_piece_notes": 80,
    }
    CONFIDENCE_STYLE_FIELDS = (
        "philosophy_summary",
        "attacking_style",
        "build_up_style",
        "defensive_style",
    )

    coach = models.ForeignKey(CoachProfile, on_delete=models.CASCADE, related_name="tactical_profiles")
    team = models.ForeignKey(
        TeamProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="coach_tactical_profiles",
        help_text="Leave blank for the coach's general philosophy; select a team for a club-specific implementation.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    preferred_formation = models.CharField(max_length=40, blank=True)
    alternative_formations = models.JSONField(default=list, blank=True)
    philosophy_summary = models.TextField(blank=True)
    attacking_style = models.CharField(max_length=120, blank=True)
    defensive_style = models.CharField(max_length=120, blank=True)
    build_up_style = models.CharField(max_length=120, blank=True)
    leading_approach = models.TextField(blank=True)
    trailing_approach = models.TextField(blank=True)
    attacking_notes = models.TextField(blank=True)
    defensive_notes = models.TextField(blank=True)
    match_management_notes = models.TextField(blank=True)
    set_piece_notes = models.TextField(blank=True)
    possession_tendency = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    build_up_patience = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    passing_directness = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    attacking_tempo = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    attacking_width = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    crossing_tendency = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    counterattack_tendency = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    attacking_risk = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    pressing_intensity = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    defensive_block_height = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    defensive_line_height = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    defensive_compactness = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    transition_defence = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    defensive_aggression = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    set_piece_emphasis = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    rotation_tendency = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    tactical_flexibility = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    youth_usage = models.PositiveSmallIntegerField(null=True, blank=True, validators=RATING_VALIDATORS)
    confidence = models.CharField(
        max_length=20,
        choices=CoachProfile.Confidence.choices,
        default=CoachProfile.Confidence.UNKNOWN,
        editable=False,
    )
    confidence_score = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        validators=RATING_VALIDATORS,
    )
    ai_confidence_review = models.JSONField(default=dict, blank=True)
    ai_confidence_reviewed_at = models.DateTimeField(null=True, blank=True)
    ai_confidence_model = models.CharField(max_length=120, blank=True)
    source_urls = models.JSONField(default=list, blank=True)
    research_notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_coach_tactical_profiles",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_coach_tactical_profiles",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_coachtacticalprofile"
        ordering = ["coach__canonical_name", "-version", "-updated_at"]
        indexes = [
            models.Index(fields=["status", "confidence"]),
            models.Index(fields=["coach", "team", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["coach", "version"],
                condition=Q(team__isnull=True),
                name="unique_general_coach_tactical_version",
            ),
            models.UniqueConstraint(
                fields=["coach", "team", "version"],
                condition=Q(team__isnull=False),
                name="unique_team_coach_tactical_version",
            ),
        ]

    def __str__(self):
        scope = self.team.canonical_name if self.team_id else "general"
        return f"{self.coach} - {scope} v{self.version}"

    def clean(self):
        self.recalculate_confidence()
        errors = {}
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            errors["effective_to"] = "The effective end date cannot be earlier than the start date."
        if self.status == self.Status.APPROVED:
            if not self.source_urls:
                errors["source_urls"] = "Approved tactical profiles require at least one evidence source."
            if not str(self.philosophy_summary or "").strip():
                errors["philosophy_summary"] = "Approved tactical profiles require a philosophy summary."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recalculate_confidence()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"confidence", "confidence_score"}
        super().save(*args, **kwargs)

    def recalculate_confidence(self):
        """Calculate tactical-profile usability, preferring AI context review when available."""
        ai_score = self._ai_confidence_score()
        self.confidence_score = ai_score if ai_score is not None else self._deterministic_confidence_score()
        self.confidence = self._confidence_label(self.confidence_score)
        return self.confidence_score

    def apply_ai_confidence_review(self, review: dict, *, model: str = "", reviewed_at=None):
        self.ai_confidence_review = review or {}
        self.ai_confidence_model = model or str(self.ai_confidence_review.get("model") or "")
        self.ai_confidence_reviewed_at = reviewed_at
        self.recalculate_confidence()
        return self.confidence_score

    def _ai_confidence_score(self):
        if not isinstance(self.ai_confidence_review, dict):
            return None
        for key in ("ai_confidence_score", "confidence_score"):
            value = self.ai_confidence_review.get(key)
            if value in (None, ""):
                continue
            try:
                score = round(float(value))
            except (TypeError, ValueError):
                continue
            return max(0, min(100, score))
        return None

    def _deterministic_confidence_score(self):
        ratings_completed = sum(getattr(self, field) is not None for field in self.RATING_FIELDS)
        rating_score = 65 * ratings_completed / len(self.RATING_FIELDS)

        style_completed = sum(bool(str(getattr(self, field) or "").strip()) for field in self.CONFIDENCE_STYLE_FIELDS)
        style_score = 25 * style_completed / len(self.CONFIDENCE_STYLE_FIELDS)

        formation_score = 7 if str(self.preferred_formation or "").strip() else 0
        formation_score += min(3, self._json_item_count(self.alternative_formations))

        return round(
            min(
                100,
                rating_score + style_score + formation_score,
            )
        )

    @staticmethod
    def _confidence_label(score):
        if score == 0:
            return CoachProfile.Confidence.UNKNOWN
        if score < 45:
            return CoachProfile.Confidence.LOW
        if score < 80:
            return CoachProfile.Confidence.MEDIUM
        return CoachProfile.Confidence.HIGH

    @staticmethod
    def _json_item_count(value):
        if isinstance(value, (list, tuple, set)):
            return sum(bool(str(item).strip()) for item in value)
        if isinstance(value, dict):
            return sum(item not in (None, "", [], {}) for item in value.values())
        return int(bool(str(value or "").strip()))


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
