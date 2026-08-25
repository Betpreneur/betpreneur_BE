"""Public record, market health and ops payloads."""
from rest_framework import serializers


class TaskStatusSerializer(serializers.Serializer):
    task_id = serializers.CharField()
    status = serializers.CharField()
    result = serializers.JSONField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_blank=True)


class PublicSummarySerializer(serializers.Serializer):
    hit_rate = serializers.FloatField()
    roi_flat = serializers.FloatField()
    picks_logged = serializers.IntegerField()
    wins = serializers.IntegerField()
    losses = serializers.IntegerField()
    voids = serializers.IntegerField()
    pending = serializers.IntegerField()
    window_days = serializers.IntegerField()


class RecordQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(required=False, min_value=1, max_value=365, default=90)


class MarketHealthQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(required=False, min_value=1, max_value=365, default=90)
    market = serializers.CharField(required=False, allow_blank=True)
    scope = serializers.ChoiceField(
        choices=("all", "published", "internal"),
        required=False,
        default="all",
    )


class MarketHealthResponseSerializer(serializers.Serializer):
    days = serializers.IntegerField()
    scope = serializers.CharField()
    count = serializers.IntegerField()
    markets = serializers.JSONField()


class PublicRecordPickSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    posted_at = serializers.DateTimeField()
    match_date = serializers.DateField()
    fixture = serializers.CharField()
    home_team = serializers.CharField(allow_blank=True)
    away_team = serializers.CharField(allow_blank=True)
    league = serializers.CharField(allow_blank=True)
    kickoff = serializers.CharField(allow_blank=True)
    tier = serializers.CharField()
    market = serializers.CharField()
    pick = serializers.CharField()
    confidence = serializers.IntegerField()
    odds = serializers.DecimalField(max_digits=8, decimal_places=2)
    stake = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    status = serializers.CharField()
    score = serializers.CharField(allow_blank=True)
    pnl = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    settled_at = serializers.DateTimeField(allow_null=True)


class RecordResponseSerializer(serializers.Serializer):
    summary = PublicSummarySerializer()
    records = PublicRecordPickSerializer(many=True)


class MaintenanceRunRequestSerializer(serializers.Serializer):
    jobs = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=(
            "Jobs to queue. Omit to run every job. Valid names: statpal_daily_cache, "
            "fixture_horizon, score_models, player_availability, lineups, settle_slips, "
            "recover_slip_reviews."
        ),
    )
    days = serializers.IntegerField(
        required=False, min_value=0, max_value=7, default=3,
        help_text="Build window in days. Used by statpal_daily_cache and fixture_horizon.",
    )

    def validate(self, attrs):
        """
        Reject unrecognised fields.

        Omitting `jobs` means "run everything", so a mistyped key would otherwise be
        ignored and quietly launch every job — roughly two thousand provider calls
        instead of the one that was asked for. A typo should fail, not escalate.
        """
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({
                "unknown_fields": unknown,
                "detail": (
                    f"Unrecognised field(s): {', '.join(unknown)}. "
                    f"Expected 'jobs' (list) and optionally 'days'."
                ),
            })
        return attrs


class MaintenanceRunResponseSerializer(serializers.Serializer):
    queued = serializers.JSONField()
    poll = serializers.CharField()
