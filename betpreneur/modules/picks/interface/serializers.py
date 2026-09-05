"""Daily picks, games and backing payloads."""
from rest_framework import serializers

from betpreneur.modules.picks.models import AlgoRun, GameBack, Pick


class PickSerializer(serializers.ModelSerializer):
    tier = serializers.SerializerMethodField()
    model_verdict = serializers.SerializerMethodField()
    council_review = serializers.SerializerMethodField()
    final_confidence = serializers.SerializerMethodField()
    home_recent_form = serializers.SerializerMethodField()
    away_recent_form = serializers.SerializerMethodField()
    backed_count = serializers.SerializerMethodField()
    backed_by_me = serializers.SerializerMethodField()
    selection_profile = serializers.SerializerMethodField()
    risk_level = serializers.SerializerMethodField()
    bettor_view = serializers.SerializerMethodField()
    analysis_summary = serializers.SerializerMethodField()
    analysis_conclusion = serializers.SerializerMethodField()
    positive_evidence = serializers.SerializerMethodField()
    risk_evidence = serializers.SerializerMethodField()

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
            "insights",
            "bettor_view",
            "analysis_summary",
            "analysis_conclusion",
            "positive_evidence",
            "risk_evidence",
            "selection_profile",
            "risk_level",
            "confidence",
            "final_confidence",
            "council_review",
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
        match_id = str(obj.match_id or "")
        if not match_id:
            return 0
        backed_game_counts = self.context.get("backed_game_counts")
        if backed_game_counts is not None:
            return int(backed_game_counts.get(match_id, 0) or 0)
        return GameBack.objects.filter(match_id=match_id).count()

    def _recent_form_payload(self, form) -> dict:
        form = dict(form or {})
        wins = int(form.get("wins") or 0)
        draws = int(form.get("draws") or 0)
        games = int(form.get("games") or 0)
        if "losses" not in form:
            form["losses"] = max(0, games - wins - draws)
        form.setdefault("draws", draws)
        form.setdefault("scope", "overall")
        form.setdefault("form", "")
        return form

    def get_home_recent_form(self, obj) -> dict:
        return self._recent_form_payload(obj.home_recent_form)

    def get_away_recent_form(self, obj) -> dict:
        return self._recent_form_payload(obj.away_recent_form)

    def get_backed_by_me(self, obj) -> bool:
        request = self.context.get("request")
        match_id = str(obj.match_id or "")
        if not request or not request.user.is_authenticated or not match_id:
            return False
        backed_game_ids = self.context.get("backed_game_ids")
        if backed_game_ids is not None:
            return match_id in backed_game_ids
        return GameBack.objects.filter(user=request.user, match_id=match_id).exists()

    def get_tier(self, obj) -> str:
        council_tier = self.get_council_review(obj).get("tier")
        if council_tier in {Pick.Tier.BANKER, Pick.Tier.VALUE_GEM, Pick.Tier.WILD_CARD}:
            return council_tier
        return obj.tier

    def get_council_review(self, obj) -> dict:
        review = ((obj.insights or {}).get("council_review") or {}).copy()
        if review:
            return {
                "decision": review.get("decision", ""),
                "tier": review.get("tier", ""),
                "raw_confidence": review.get("raw_confidence", obj.confidence),
                "final_confidence": review.get("final_confidence", obj.confidence),
                "consensus_score": review.get("consensus_score"),
                "disagreement_score": review.get("disagreement_score"),
                "reasons": review.get("reasons", []),
                "reviewers": review.get("reviewers", []),
            }
        return {
            "decision": "legacy",
            "tier": obj.tier,
            "raw_confidence": obj.confidence,
            "final_confidence": obj.confidence,
            "consensus_score": None,
            "disagreement_score": None,
            "reasons": [],
            "reviewers": [],
        }

    def get_final_confidence(self, obj):
        return self.get_council_review(obj).get("final_confidence")

    def get_selection_profile(self, obj) -> str:
        tier = self.get_tier(obj)
        if obj.tier != tier:
            if tier == Pick.Tier.BANKER:
                return "reliability"
            if tier == Pick.Tier.VALUE_GEM:
                return "mispriced_value"
            if tier == Pick.Tier.WILD_CARD:
                return "high_upside"
        if obj.tier == Pick.Tier.WILD_CARD and tier != Pick.Tier.WILD_CARD:
            return "mispriced_value"
        for flag in obj.risk_flags or []:
            if str(flag).startswith("profile:"):
                return str(flag).split(":", 1)[1]
        return ""

    def get_model_verdict(self, obj) -> str:
        tier = self.get_tier(obj)
        if obj.tier == tier:
            return obj.model_verdict
        if tier == Pick.Tier.BANKER:
            return "Banker selected for high confidence and a controlled risk profile."
        if tier == Pick.Tier.VALUE_GEM:
            return "Value Gem selected for positive value and confidence."
        if tier == Pick.Tier.WILD_CARD:
            return "Wild Card selected for playable upside at moderate confidence."
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

    def get_bettor_view(self, obj) -> dict:
        return (obj.insights or {}).get("bettor_view") or {}

    def get_analysis_summary(self, obj) -> str:
        return (obj.insights or {}).get("summary", "")

    def get_analysis_conclusion(self, obj) -> str:
        return (obj.insights or {}).get("conclusion", "")

    def get_positive_evidence(self, obj) -> list:
        return (obj.insights or {}).get("positive_evidence") or []

    def get_risk_evidence(self, obj) -> list:
        return (obj.insights or {}).get("risk_evidence") or []


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


class DailyPicksQuerySerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    view = serializers.ChoiceField(
        choices=("full", "compact"),
        required=False,
        default="full",
    )


class BackedPicksQuerySerializer(serializers.Serializer):
    date = serializers.DateField(required=False)


class GameAnalysisQuerySerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    view = serializers.ChoiceField(
        choices=("full", "compact"),
        required=False,
        default="full",
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)
    include = serializers.ChoiceField(
        choices=("public", "technical"),
        required=False,
        default="public",
    )


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
    final_confidence = serializers.FloatField(required=False, allow_null=True)
    council_review = serializers.JSONField(required=False)
    odds = serializers.FloatField()
    odds_meta = serializers.JSONField(required=False)
    ev = serializers.FloatField()
    odds_source = serializers.CharField(required=False)
    proven = serializers.BooleanField()
    eligible = serializers.BooleanField()
    risk_flags = serializers.ListField(child=serializers.CharField(), required=False)
    bettor_view = serializers.JSONField(required=False)
    analysis_summary = serializers.CharField(required=False, allow_blank=True)
    analysis_conclusion = serializers.CharField(required=False, allow_blank=True)
    positive_evidence = serializers.ListField(child=serializers.CharField(), required=False)
    risk_evidence = serializers.ListField(child=serializers.CharField(), required=False)
    insights = serializers.JSONField(required=False)
    selected = serializers.BooleanField(required=False)
    selected_pick_id = serializers.IntegerField(required=False, allow_null=True)
    selected_tier = serializers.CharField(required=False, allow_blank=True)


class FixturePickGroupSerializer(serializers.Serializer):
    fixture = serializers.CharField()
    home_team = serializers.CharField(allow_blank=True)
    away_team = serializers.CharField(allow_blank=True)
    home_logo = serializers.URLField(required=False, allow_blank=True)
    away_logo = serializers.URLField(required=False, allow_blank=True)
    teams = serializers.JSONField(required=False)
    league = serializers.CharField(allow_blank=True)
    league_logo = serializers.URLField(required=False, allow_blank=True)
    competition_logo = serializers.URLField(required=False, allow_blank=True)
    country = serializers.CharField(required=False, allow_blank=True)
    country_flag = serializers.URLField(required=False, allow_blank=True)
    round = serializers.CharField(required=False, allow_blank=True)
    league_type = serializers.CharField(required=False, allow_blank=True)
    competition = serializers.CharField(required=False, allow_blank=True)
    competition_info = serializers.JSONField(required=False)
    kickoff = serializers.CharField(allow_blank=True)
    match_id = serializers.CharField(allow_blank=True)
    market_count = serializers.IntegerField()
    markets_70_plus = serializers.IntegerField()
    markets_65_plus = serializers.IntegerField()
    home_recent_form = serializers.JSONField(required=False)
    away_recent_form = serializers.JSONField(required=False)
    corner_profile = serializers.JSONField(required=False)
    fixture_context = serializers.JSONField(required=False)
    team_news = serializers.JSONField(required=False)
    insights = serializers.JSONField(required=False)
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
    strategy = serializers.JSONField(required=False)
    fixtures = FixturePickGroupSerializer(many=True)
    grouped_fixtures = serializers.JSONField(required=False)


class TopPickResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    count = serializers.IntegerField(required=False)
    pick = PickSerializer(allow_null=True, required=False)
    top_pick = PickSerializer(allow_null=True, required=False)
    picks = PickSerializer(many=True, required=False)


class GameListResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    run_id = serializers.IntegerField(allow_null=True)
    posted_at = serializers.DateTimeField(allow_null=True)
    summary = serializers.JSONField()
    strategy = serializers.JSONField(required=False)
    games = serializers.JSONField()
    grouped_games = serializers.JSONField(required=False)


class GameDetailResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    published = serializers.BooleanField()
    run_id = serializers.IntegerField(allow_null=True)
    posted_at = serializers.DateTimeField(allow_null=True)
    game = serializers.JSONField(allow_null=True)


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


class GameBackSelectionSerializer(serializers.Serializer):
    match_id = serializers.CharField(
        allow_blank=False,
        help_text="API-Football match_id for the game.",
    )
    market = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional exact market name from the game markets list, for example 'Over 1.5'. If omitted, the recommended/best market is backed.",
    )
    date = serializers.DateField(
        required=False,
        help_text="Optional match date used to resolve the fixture when the same match_id could appear in multiple runs.",
    )


class SingleGameBackRequestSerializer(serializers.Serializer):
    market = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional exact market name from the game markets list. If omitted, backs the recommended/best market.",
    )
    date = serializers.DateField(
        required=False,
        help_text="Optional match date used to resolve a specific matchday.",
    )


class BulkGameBackRequestSerializer(serializers.Serializer):
    match_ids = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        min_length=1,
        max_length=50,
        required=False,
        help_text="Legacy/default backing mode. Each match_id backs the recommended/best market for that game.",
    )
    games = GameBackSelectionSerializer(
        many=True,
        required=False,
        help_text="Market-specific backing mode. Each item can include match_id and an optional market.",
    )
    date = serializers.DateField(
        required=False,
        help_text="Optional default date applied to all selections that do not include their own date.",
    )

    def validate(self, attrs):
        if not attrs.get("match_ids") and not attrs.get("games"):
            raise serializers.ValidationError("Provide match_ids or games.")
        return attrs

    def validate_match_ids(self, value):
        cleaned = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not cleaned:
            raise serializers.ValidationError("At least one match_id is required.")
        return cleaned


class BulkGameBackResponseSerializer(serializers.Serializer):
    requested_count = serializers.IntegerField()
    game_count = serializers.IntegerField()
    created_count = serializers.IntegerField()
    already_backed_count = serializers.IntegerField()
    results = serializers.JSONField()
    games = serializers.JSONField()


class GameBackResponseSerializer(serializers.Serializer):
    match_id = serializers.CharField()
    market = serializers.CharField(
        allow_blank=True,
        required=False,
        help_text="The backed market. Blank only for old backed-game rows created before market-specific backing existed.",
    )
    meaning = serializers.CharField(allow_blank=True, required=False)
    backed = serializers.BooleanField()
    created = serializers.BooleanField(required=False)
    deleted = serializers.BooleanField(required=False)
    backed_count = serializers.IntegerField(help_text="Number of users that backed this specific match/market when market is present.")


class BackedGamesResponseSerializer(serializers.Serializer):
    date = serializers.DateField(required=False, allow_null=True)
    count = serializers.IntegerField()
    games = serializers.JSONField()
