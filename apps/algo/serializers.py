from rest_framework import serializers

from .models import AlgoRun, GameBack, Pick, StatPalFixtureSnapshot, TokenPurchase, TokenTransaction


class TokenTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TokenTransaction
        fields = (
            "id",
            "amount",
            "free_tokens_delta",
            "paid_tokens_delta",
            "token_bucket",
            "reason",
            "reference_type",
            "reference_id",
            "balance_after",
            "metadata",
            "created_at",
        )


class TokenWalletResponseSerializer(serializers.Serializer):
    wallet = serializers.DictField()
    pricing = serializers.DictField()
    refill_policy = serializers.DictField()
    recent_transactions = TokenTransactionSerializer(many=True)


class TokenPackageSerializer(serializers.Serializer):
    id = serializers.CharField()
    tokens = serializers.IntegerField()
    amount = serializers.IntegerField()
    amount_kobo = serializers.IntegerField()
    currency = serializers.CharField()
    label = serializers.CharField()


class TokenPackageListResponseSerializer(serializers.Serializer):
    currency = serializers.CharField()
    packages = TokenPackageSerializer(many=True)


class TokenPurchaseSerializer(serializers.ModelSerializer):
    payment = serializers.SerializerMethodField()

    class Meta:
        model = TokenPurchase
        fields = (
            "id",
            "package_id",
            "tokens",
            "amount",
            "amount_kobo",
            "currency",
            "status",
            "provider",
            "provider_reference",
            "paid_at",
            "failed_at",
            "created_at",
            "updated_at",
            "payment",
        )

    def get_payment(self, obj):
        if obj.provider != "payfonte":
            return None
        from .payfonte import payfonte_payment_payload

        return payfonte_payment_payload(obj)


class TokenPurchaseCreateRequestSerializer(serializers.Serializer):
    package_id = serializers.CharField(max_length=120)
    metadata = serializers.DictField(required=False)


class TokenPurchaseCreateResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    package = TokenPackageSerializer()


class TokenPurchaseListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    purchases = TokenPurchaseSerializer(many=True)


class TokenPurchaseAdminCompleteRequestSerializer(serializers.Serializer):
    provider = serializers.CharField(required=False, allow_blank=True, max_length=40)
    provider_reference = serializers.CharField(required=False, allow_blank=True, max_length=160)
    metadata = serializers.DictField(required=False)


class TokenPurchaseAdminCompleteResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    wallet = serializers.DictField()
    transaction = TokenTransactionSerializer(required=False, allow_null=True)
    idempotent = serializers.BooleanField()


class TokenPurchaseAdminFailRequestSerializer(serializers.Serializer):
    provider = serializers.CharField(required=False, allow_blank=True, max_length=40)
    provider_reference = serializers.CharField(required=False, allow_blank=True, max_length=160)
    metadata = serializers.DictField(required=False)


class TokenPurchaseAdminFailResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()


class TokenPurchaseVerifyResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    wallet = serializers.DictField(required=False)
    transaction = TokenTransactionSerializer(required=False, allow_null=True)
    idempotent = serializers.BooleanField(default=False)
    payfonte_status = serializers.CharField()


class TokenTransactionListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    transactions = TokenTransactionSerializer(many=True)


class TokenAdminAdjustmentRequestSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False)
    email = serializers.EmailField(required=False)
    free_tokens_delta = serializers.IntegerField(default=0, min_value=-100000, max_value=100000)
    paid_tokens_delta = serializers.IntegerField(default=0, min_value=-100000, max_value=100000)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
    reference_id = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate(self, attrs):
        if not attrs.get("user_id") and not attrs.get("email"):
            raise serializers.ValidationError("Provide user_id or email.")
        if int(attrs.get("free_tokens_delta") or 0) == 0 and int(attrs.get("paid_tokens_delta") or 0) == 0:
            raise serializers.ValidationError("Provide a non-zero token delta.")
        return attrs


class TokenAdminAdjustmentResponseSerializer(serializers.Serializer):
    user = serializers.DictField()
    wallet = serializers.DictField()
    transaction = TokenTransactionSerializer()


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


class FixtureSearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(
        min_length=2,
        help_text="Typed fixture name, for example 'France vs Morocco'.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=14,
        default=3,
        help_text="Search from today through this many future days. Defaults to 3.",
    )
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=25,
        default=10,
        help_text="Maximum number of candidate fixtures to return.",
    )
    refresh = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Force refresh from API-Football before searching.",
    )


class FixtureSearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    start_date = serializers.DateField()
    days = serializers.IntegerField()
    count = serializers.IntegerField()
    refreshed = serializers.BooleanField()
    refresh_errors = serializers.JSONField(required=False)
    results = serializers.JSONField()


class ManualSlipSelectionSerializer(serializers.Serializer):
    match = serializers.CharField(
        min_length=2,
        max_length=255,
        help_text="Typed fixture name, for example 'France vs Morocco'.",
    )
    market = serializers.CharField(
        min_length=2,
        max_length=120,
        help_text="Selected market from the frontend dropdown, for example 'Over 1.5'.",
    )


class ManualSlipReviewRequestSerializer(serializers.Serializer):
    selections = ManualSlipSelectionSerializer(
        many=True,
        min_length=1,
        max_length=30,
        help_text="Manual match and market selections to review.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=14,
        default=3,
        help_text="Search from today through this many future days. Defaults to 3.",
    )


class ManualSlipReviewResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    source = serializers.CharField()
    status = serializers.CharField()
    count = serializers.IntegerField()
    analysed_count = serializers.IntegerField()
    keep_count = serializers.IntegerField()
    caution_count = serializers.IntegerField()
    replace_count = serializers.IntegerField()
    remove_count = serializers.IntegerField()
    unmatched_count = serializers.IntegerField()
    pending_analysis_count = serializers.IntegerField()
    health_score = serializers.FloatField(required=False, allow_null=True)
    risk_level = serializers.CharField(required=False)
    ticket_health = serializers.JSONField(required=False)
    original_ticket = serializers.JSONField(required=False)
    optimized_ticket = serializers.JSONField(required=False)
    improvement = serializers.CharField(required=False, allow_blank=True)
    improvement_percent = serializers.FloatField(required=False, allow_null=True)
    learning_tracking = serializers.JSONField(required=False)
    public = serializers.JSONField(required=False)
    intelligence = serializers.JSONField()
    selections = serializers.JSONField()


class SlipReviewListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    reviews = serializers.JSONField()


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


class StatPalReadinessQuerySerializer(serializers.Serializer):
    start_date = serializers.DateField(
        required=False,
        help_text="Start date in YYYY-MM-DD format. Defaults to today.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=7,
        default=3,
        help_text="Number of days to inspect.",
    )
    include_optional = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Include optional player/coach coverage in the report.",
    )
    min_coverage = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        default=70.0,
        help_text="Minimum average coverage percentage required for readiness.",
    )


class StatPalReadinessResponseSerializer(serializers.Serializer):
    window = serializers.ListField(child=serializers.CharField())
    coverage = serializers.JSONField()
    readiness = serializers.JSONField()


class SlipRepairDecisionSerializer(serializers.Serializer):
    index = serializers.IntegerField(min_value=0)
    action = serializers.ChoiceField(choices=["keep", "replace", "drop"])


class SlipRepairRequestSerializer(serializers.Serializer):
    decisions = SlipRepairDecisionSerializer(many=True, required=False)


class SlipRepairResponseSerializer(serializers.Serializer):
    repair_id = serializers.IntegerField()
    review_id = serializers.IntegerField()
    mode = serializers.CharField()
    original = serializers.JSONField()
    revised = serializers.JSONField()
    changes = serializers.IntegerField()
    decisions = serializers.JSONField()
    disclosure = serializers.CharField()


class SlipReviewRandomizeRequestSerializer(serializers.Serializer):
    games = serializers.IntegerField(
        min_value=2,
        max_value=100,
        help_text="Number of strongest analysed games to build into the generated ticket.",
    )


class SlipReviewRandomizeResponseSerializer(serializers.Serializer):
    review_id = serializers.IntegerField()
    requested_games = serializers.IntegerField()
    available_options = serializers.JSONField()
    ticket = serializers.JSONField()
    picks = serializers.JSONField()
    excluded = serializers.JSONField(required=False)
    disclaimer = serializers.CharField()


class SlipReviewRecapQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(required=False, min_value=1, max_value=90, default=1)


class SlipReviewRecapResponseSerializer(serializers.Serializer):
    contract_version = serializers.CharField()
    window = serializers.JSONField()
    tickets = serializers.IntegerField()
    selections = serializers.JSONField()
    flagged = serializers.JSONField()
    message = serializers.CharField()


class SlipReviewDetailResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    source = serializers.CharField()
    status = serializers.CharField()
    title = serializers.CharField()
    summary = serializers.JSONField()
    public = serializers.JSONField(required=False)
    intelligence = serializers.JSONField(required=False)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    selections = serializers.JSONField()


class SlipReviewEventsQuerySerializer(serializers.Serializer):
    after_id = serializers.IntegerField(required=False, min_value=0, default=0)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=200, default=100)


class SlipReviewEventsResponseSerializer(serializers.Serializer):
    review_id = serializers.IntegerField()
    status = serializers.CharField()
    progress = serializers.JSONField()
    latest_event_id = serializers.IntegerField(required=False, allow_null=True)
    events = serializers.JSONField()


class SlipReviewStreamTokenResponseSerializer(serializers.Serializer):
    ticket = serializers.CharField()
    expires_in = serializers.IntegerField()
    expires_at = serializers.DateTimeField()
    ws_path = serializers.CharField()
    ws_url = serializers.CharField()


class SlipReviewOptionsResponseSerializer(serializers.Serializer):
    markets = serializers.JSONField()
    verdicts = serializers.JSONField()
    sources = serializers.JSONField()
    limits = serializers.JSONField()


class StatPalFixtureContextQuerySerializer(serializers.Serializer):
    match_id = serializers.CharField(required=False, allow_blank=True, max_length=120)
    provider_match_id = serializers.CharField(required=False, allow_blank=True, max_length=120)
    refresh = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if not attrs.get("match_id") and not attrs.get("provider_match_id"):
            raise serializers.ValidationError("Provide match_id or provider_match_id.")
        return attrs


class StatPalFixtureRefreshRequestSerializer(serializers.Serializer):
    match_id = serializers.CharField(required=False, allow_blank=True, max_length=120)
    provider_match_id = serializers.CharField(required=False, allow_blank=True, max_length=120)
    provider_competition_id = serializers.CharField(required=False, allow_blank=True, max_length=100)
    force = serializers.BooleanField(required=False, default=False)
    snapshot_types = serializers.ListField(
        child=serializers.ChoiceField(choices=StatPalFixtureSnapshot.SnapshotType.choices),
        required=False,
        allow_empty=False,
        help_text="Optional subset of StatPal snapshot types to refresh.",
    )

    def validate(self, attrs):
        if not attrs.get("match_id") and not attrs.get("provider_match_id"):
            raise serializers.ValidationError("Provide match_id or provider_match_id.")
        return attrs


class StatPalFixtureContextResponseSerializer(serializers.Serializer):
    match_id = serializers.CharField(required=False, allow_blank=True)
    provider_match_id = serializers.CharField(required=False, allow_blank=True)
    refreshed = serializers.JSONField(required=False)
    context = serializers.JSONField()


class SportyBetSlipImportRequestSerializer(serializers.Serializer):
    url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="SportyBet share URL, for example http://www.sportybet.com/ng/?shareCode=V41T5X.",
    )
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=32,
        help_text="SportyBet share code, for example V41T5X.",
    )
    payload = serializers.JSONField(
        required=False,
        help_text="Optional raw SportyBet share JSON payload. Useful if the provider blocks server-side HTTP import.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=14,
        default=3,
        help_text="Search from today through this many future days. Defaults to 3.",
    )

    def validate(self, attrs):
        if not attrs.get("url") and not attrs.get("code") and attrs.get("payload") is None:
            raise serializers.ValidationError("Provide url, code, or payload.")
        return attrs


class BetanoSlipImportRequestSerializer(serializers.Serializer):
    url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="Betano booking URL, for example https://www.betano.ng/bookingcode/65R4NAGB.",
    )
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
        help_text="Betano booking code, for example 65R4NAGB.",
    )
    payload = serializers.JSONField(
        required=False,
        help_text="Optional raw Betano getbetslip JSON response or request payload containing data.legs or betslip.legs. If omitted, the backend opens the booking link with a browser importer.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=14,
        default=3,
        help_text="Search from today through this many future days. Defaults to 3.",
    )

    def validate(self, attrs):
        if not attrs.get("url") and not attrs.get("code") and attrs.get("payload") is None:
            raise serializers.ValidationError("Provide url, code, or payload.")
        return attrs
