"""Stored prediction samples for model calibration.

This table is deliberately product-neutral. Picks, slips, all-games, and
settlement can write training rows through prediction.api without prediction
importing any product module.
"""
from django.db import models


class PredictionTrainingSample(models.Model):
    class SettlementResult(models.TextChoices):
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        VOID = "void", "Void"
        PUSH = "push", "Push"

    fixture_id = models.CharField(max_length=120)
    canonical_market = models.CharField(max_length=160)
    line = models.CharField(max_length=32, blank=True)
    side = models.CharField(max_length=40, blank=True)
    first_prediction_score = models.FloatField(null=True, blank=True)
    last_prediction_score = models.FloatField(null=True, blank=True)
    selected_status = models.CharField(max_length=40, blank=True)
    published_status = models.CharField(max_length=40, blank=True)
    odds_source = models.CharField(max_length=60, blank=True)
    real_odds = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    estimated_odds = models.BooleanField(default=False)
    settlement_result = models.CharField(max_length=20, choices=SettlementResult.choices)
    market_family = models.CharField(max_length=80, blank=True)
    league_key = models.CharField(max_length=120, blank=True)
    season = models.CharField(max_length=32, blank=True)
    kickoff = models.DateTimeField(null=True, blank=True)
    prediction_created_at = models.DateTimeField()
    last_prediction_created_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=40, blank=True)
    source_reference = models.CharField(max_length=160, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prediction_training_sample"
        ordering = ["-prediction_created_at", "fixture_id", "canonical_market"]
        indexes = [
            models.Index(fields=["league_key", "season"], name="prediction_league_season_idx"),
            models.Index(fields=["canonical_market", "settlement_result"], name="prediction_market_result_idx"),
            models.Index(fields=["market_family"], name="prediction_market_family_idx"),
            models.Index(fields=["odds_source", "estimated_odds"], name="prediction_odds_quality_idx"),
            models.Index(fields=["kickoff"], name="prediction_kickoff_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["fixture_id", "canonical_market", "line", "side"],
                name="unique_prediction_training_sample",
            )
        ]

    @property
    def is_void(self) -> bool:
        return self.settlement_result in {self.SettlementResult.VOID, self.SettlementResult.PUSH}

    @property
    def has_real_odds(self) -> bool:
        return self.real_odds is not None and not self.estimated_odds

    def __str__(self):
        return f"{self.fixture_id} - {self.canonical_market}"


class PredictionTeamMatchFeedback(models.Model):
    class Side(models.TextChoices):
        HOME = "home", "Home"
        AWAY = "away", "Away"

    class Result(models.TextChoices):
        WIN = "win", "Win"
        DRAW = "draw", "Draw"
        LOSS = "loss", "Loss"

    fixture_id = models.CharField(max_length=120)
    provider_match_id = models.CharField(max_length=120, blank=True)
    fixture_name = models.CharField(max_length=255, blank=True)
    match_date = models.DateField(null=True, blank=True)
    league_key = models.CharField(max_length=120, blank=True)
    season = models.CharField(max_length=32, blank=True)
    team_id = models.CharField(max_length=120, blank=True)
    team_name = models.CharField(max_length=255)
    opponent_id = models.CharField(max_length=120, blank=True)
    opponent_name = models.CharField(max_length=255, blank=True)
    side = models.CharField(max_length=10, choices=Side.choices)
    actual_result = models.CharField(max_length=10, choices=Result.choices)
    goals_for = models.PositiveSmallIntegerField(null=True, blank=True)
    goals_against = models.PositiveSmallIntegerField(null=True, blank=True)
    corners_for = models.FloatField(null=True, blank=True)
    corners_against = models.FloatField(null=True, blank=True)
    cards_for = models.FloatField(null=True, blank=True)
    cards_against = models.FloatField(null=True, blank=True)
    shots_on_target_for = models.FloatField(null=True, blank=True)
    shots_on_target_against = models.FloatField(null=True, blank=True)
    referee_name = models.CharField(max_length=160, blank=True)
    source = models.CharField(max_length=40, blank=True)
    prediction_snapshot = models.JSONField(default=dict, blank=True)
    actual_stats = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prediction_team_match_feedback"
        ordering = ["-match_date", "team_name", "fixture_id"]
        indexes = [
            models.Index(fields=["team_name", "match_date"], name="pred_feedback_team_date_idx"),
            models.Index(fields=["opponent_name", "match_date"], name="pred_feedback_opp_date_idx"),
            models.Index(fields=["league_key", "match_date"], name="pred_feedback_league_idx"),
            models.Index(fields=["fixture_id"], name="pred_feedback_fixture_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["fixture_id", "team_name", "side"],
                name="unique_prediction_team_feedback",
            )
        ]

    def __str__(self):
        return f"{self.team_name} feedback - {self.fixture_id}"
