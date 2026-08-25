"""Slip review payloads."""
from rest_framework import serializers


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
