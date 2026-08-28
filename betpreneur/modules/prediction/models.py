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
