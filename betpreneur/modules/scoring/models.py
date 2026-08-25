"""Fitted models: league score models, team strengths and rate profiles,
plus the lineup and availability facts that adjust them.

Table names stay algo_* — this refactor moves packages, not data.
"""
from django.db import models


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
        db_table = "algo_leaguescoremodel"
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
        db_table = "algo_teamstrength"
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
        db_table = "algo_fixturelineup"
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
        db_table = "algo_playeravailability"
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
        db_table = "algo_teamrateprofile"
        constraints = [
            models.UniqueConstraint(fields=["provider", "team_id"], name="unique_team_rate_profile")
        ]
        indexes = [
            models.Index(fields=["provider", "team_name_normalized"]),
            models.Index(fields=["league_id"]),
        ]

    def __str__(self):
        return f"{self.team_name or self.team_id} rates"
