"""Fixture search and provider-context payloads."""
from rest_framework import serializers

from betpreneur.modules.catalog.models import StatPalFixtureSnapshot


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
