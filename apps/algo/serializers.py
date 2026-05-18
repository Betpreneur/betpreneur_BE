from rest_framework import serializers

from .models import AlgoRun, Pick


class PickSerializer(serializers.ModelSerializer):
    class Meta:
        model = Pick
        fields = (
            "id",
            "match_date",
            "fixture",
            "league",
            "kickoff",
            "match_id",
            "tier",
            "market",
            "meaning",
            "reasoning",
            "risk_flags",
            "confidence",
            "odds",
            "ev",
            "stake",
            "score",
            "result",
            "pnl",
            "status",
            "source",
            "settled_at",
            "created_at",
        )


class AlgoRunSerializer(serializers.ModelSerializer):
    picks = PickSerializer(many=True, read_only=True)

    class Meta:
        model = AlgoRun
        fields = (
            "id",
            "target_date",
            "status",
            "fd_fixtures",
            "aps_fixtures",
            "total_scored",
            "picks_count",
            "bankers",
            "value_gems",
            "wild_cards",
            "bankroll",
            "result",
            "error",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
            "picks",
        )


class AlgoRunCreateSerializer(serializers.Serializer):
    target_date = serializers.DateField(required=False)


class ResultsUpdateSerializer(serializers.Serializer):
    target_date = serializers.DateField(required=False)


class AuditorRunSerializer(serializers.Serializer):
    from_date = serializers.DateField(required=False)
    to_date = serializers.DateField(required=False)


class TaskQueuedSerializer(serializers.Serializer):
    task_id = serializers.CharField()
    status = serializers.CharField()
    message = serializers.CharField()


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


class DailyPicksQuerySerializer(serializers.Serializer):
    date = serializers.DateField(required=False)


class RecordQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(required=False, min_value=1, max_value=365, default=90)


class DailyPicksResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    run_id = serializers.IntegerField(allow_null=True)
    posted_at = serializers.DateTimeField(allow_null=True)
    picks = PickSerializer(many=True)


class TopPickResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    pick = PickSerializer(allow_null=True)


class RecordResponseSerializer(serializers.Serializer):
    summary = PublicSummarySerializer()
    picks = PickSerializer(many=True)
