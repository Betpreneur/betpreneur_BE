from rest_framework import serializers

from .models import AlgoRun, Pick, PickBack


class PickSerializer(serializers.ModelSerializer):
    tier = serializers.SerializerMethodField()
    model_verdict = serializers.SerializerMethodField()
    backed_count = serializers.SerializerMethodField()
    backed_by_me = serializers.SerializerMethodField()
    selection_profile = serializers.SerializerMethodField()
    risk_level = serializers.SerializerMethodField()

    class Meta:
        model = Pick
        fields = (
            "id",
            "match_date",
            "fixture",
            "home_team",
            "away_team",
            "league",
            "kickoff",
            "match_id",
            "tier",
            "market",
            "meaning",
            "reasoning",
            "model_verdict",
            "home_recent_form",
            "away_recent_form",
            "risk_flags",
            "selection_profile",
            "risk_level",
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
            "backed_count",
            "backed_by_me",
        )

    def get_backed_count(self, obj) -> int:
        return obj.backs.count()

    def get_backed_by_me(self, obj) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return obj.backs.filter(user=request.user).exists()

    def get_tier(self, obj) -> str:
        if obj.tier != Pick.Tier.WILD_CARD:
            return obj.tier
        try:
            odds = float(obj.odds or 0)
        except (TypeError, ValueError):
            odds = 0
        confidence = obj.confidence or 0
        if 60 <= confidence < 70 and odds > 2.0:
            return obj.tier
        return Pick.Tier.VALUE_GEM

    def get_selection_profile(self, obj) -> str:
        if obj.tier == Pick.Tier.WILD_CARD and self.get_tier(obj) != Pick.Tier.WILD_CARD:
            return "mispriced_value"
        for flag in obj.risk_flags or []:
            if str(flag).startswith("profile:"):
                return str(flag).split(":", 1)[1]
        return ""

    def get_model_verdict(self, obj) -> str:
        if obj.tier == Pick.Tier.WILD_CARD and self.get_tier(obj) != Pick.Tier.WILD_CARD:
            return "Value Gem selected for positive value and confidence."
        return obj.model_verdict

    def get_risk_level(self, obj) -> str:
        profile = self.get_selection_profile(obj)
        tier = self.get_tier(obj)
        if tier == Pick.Tier.BANKER:
            return "low"
        if tier == Pick.Tier.VALUE_GEM:
            return "medium"
        if profile == "high_upside":
            return "high"
        return "medium"


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


class DailyPicksSummarySerializer(serializers.Serializer):
    fixture_count = serializers.IntegerField()
    market_count = serializers.IntegerField()
    selected_pick_count = serializers.IntegerField()
    top_pick_id = serializers.IntegerField(required=False, allow_null=True)
    banker_count = serializers.IntegerField(required=False)
    value_gem_count = serializers.IntegerField(required=False)
    wild_card_count = serializers.IntegerField(required=False)
    picks_70_plus = serializers.IntegerField()
    picks_65_plus = serializers.IntegerField()
    markets_70_plus = serializers.IntegerField()
    markets_65_plus = serializers.IntegerField()


class FixtureMarketSerializer(serializers.Serializer):
    market = serializers.CharField()
    meaning = serializers.CharField(allow_blank=True)
    raw_confidence = serializers.IntegerField(required=False)
    confidence = serializers.IntegerField()
    odds = serializers.FloatField()
    ev = serializers.FloatField()
    odds_source = serializers.CharField(required=False)
    proven = serializers.BooleanField()
    eligible = serializers.BooleanField()
    risk_flags = serializers.ListField(child=serializers.CharField(), required=False)
    selected = serializers.BooleanField(required=False)
    selected_pick_id = serializers.IntegerField(required=False, allow_null=True)
    selected_tier = serializers.CharField(required=False, allow_blank=True)


class FixturePickGroupSerializer(serializers.Serializer):
    fixture = serializers.CharField()
    home_team = serializers.CharField(allow_blank=True)
    away_team = serializers.CharField(allow_blank=True)
    league = serializers.CharField(allow_blank=True)
    kickoff = serializers.CharField(allow_blank=True)
    match_id = serializers.CharField(allow_blank=True)
    market_count = serializers.IntegerField()
    markets_70_plus = serializers.IntegerField()
    markets_65_plus = serializers.IntegerField()
    corner_profile = serializers.JSONField(required=False)
    markets = FixtureMarketSerializer(many=True)
    picks = PickSerializer(many=True)


class DailyPicksResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    no_bet = serializers.BooleanField(required=False)
    message = serializers.CharField(required=False, allow_blank=True)
    run_id = serializers.IntegerField(allow_null=True)
    posted_at = serializers.DateTimeField(allow_null=True)
    summary = DailyPicksSummarySerializer()
    fixtures = FixturePickGroupSerializer(many=True)


class TopPickResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    pick = PickSerializer(allow_null=True)


class PickDetailResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    run_id = serializers.IntegerField(allow_null=True)
    posted_at = serializers.DateTimeField(allow_null=True)
    pick = PickSerializer()
    fixture = serializers.JSONField()
    market = serializers.JSONField()
    selection = serializers.JSONField()
    model_summary = serializers.JSONField()
    performance = serializers.JSONField()


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


class PickBackSerializer(serializers.ModelSerializer):
    class Meta:
        model = PickBack
        fields = ("id", "pick", "created_at")
        read_only_fields = fields


class PickBackResponseSerializer(serializers.Serializer):
    pick_id = serializers.IntegerField()
    backed = serializers.BooleanField()
    created = serializers.BooleanField()
    backed_count = serializers.IntegerField()
