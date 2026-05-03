from rest_framework import serializers

from .models import AlgoRun, Pick


class PickSerializer(serializers.ModelSerializer):
    class Meta:
        model = Pick
        fields = (
            "id",
            "fixture",
            "league",
            "kickoff",
            "tier",
            "market",
            "meaning",
            "confidence",
            "odds",
            "ev",
            "source",
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
