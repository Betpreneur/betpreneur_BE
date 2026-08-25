"""Slip reviews: the imported ticket, its legs, and the analysis of each.

Table names stay algo_* — this refactor moves Python packages, never data.
"""
from django.conf import settings
from django.db import models


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
        db_table = "algo_slipreview"
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
        db_table = "algo_slipselection"
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
        db_table = "algo_slipleganalysiscache"
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
        db_table = "algo_slipreviewevent"
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
        db_table = "algo_slipreviewstreamtoken"
        indexes = [
            models.Index(fields=["token_hash"], name="algo_slipre_token_h_1d9e88_idx"),
            models.Index(fields=["review", "user"], name="algo_slipre_review__75fc80_idx"),
            models.Index(fields=["expires_at"], name="algo_slipre_expires_57f904_idx"),
        ]

    def __str__(self):
        return f"Stream token for review #{self.review_id}"


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
        db_table = "algo_sliprepair"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["review", "created_at"])]

    def __str__(self):
        return f"Repair of review #{self.review_id}"
