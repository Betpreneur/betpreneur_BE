"""Fixture search and provider-context endpoints.

Extracted from the 11k-line apps/algo/views.py.
"""
import logging

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from betpreneur.modules.catalog.interface.serializers import (
    FixtureSearchQuerySerializer,
    FixtureSearchResponseSerializer,
    StatPalFixtureContextQuerySerializer,
    StatPalFixtureContextResponseSerializer,
    StatPalFixtureRefreshRequestSerializer,
    StatPalReadinessQuerySerializer,
    StatPalReadinessResponseSerializer,
)
from betpreneur.modules.catalog.services.search import FixtureSearchService
from betpreneur.modules.catalog.services.snapshots import statpal_snapshot_service
from betpreneur.platform.db.json import json_safe

log = logging.getLogger(__name__)


class FixtureSearchView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = FixtureSearchResponseSerializer

    @extend_schema(
        summary="Search upcoming fixtures",
        description=(
            "Authenticated user endpoint. Searches the local upcoming-fixture cache first using a typed match name "
            "such as 'France vs Morocco'. If no local match exists, the backend refreshes today plus the requested "
            "future-day window from API-Football and searches again."
        ),
        tags=["Games"],
        parameters=[FixtureSearchQuerySerializer],
        responses={200: FixtureSearchResponseSerializer},
    )
    def get(self, request):
        query = FixtureSearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        search_text = query.validated_data["q"]
        days = query.validated_data.get("days", 3)
        limit = query.validated_data.get("limit", 10)
        refresh = query.validated_data.get("refresh", False)
        start_date = timezone.localdate()
        search = FixtureSearchService().search(
            search_text,
            start_date=start_date,
            days=days,
            limit=limit,
            refresh=refresh,
        )
        return Response(
            {
                "query": search_text,
                "start_date": start_date,
                "days": days,
                "count": len(search["results"]),
                "refreshed": search["refreshed"],
                "refresh_errors": search.get("refresh_errors", []),
                "results": search["results"],
            }
        )




def _strip_api_usage(value):
    if isinstance(value, dict):
        return {
            key: _strip_api_usage(child)
            for key, child in value.items()
            if key != "api_usage"
        }
    if isinstance(value, list):
        return [_strip_api_usage(item) for item in value]
    return value


def api_response_payload(value):
    return _strip_api_usage(json_safe(value))


class StatPalFixtureContextView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = StatPalFixtureContextResponseSerializer

    @extend_schema(
        summary="StatPal fixture context",
        description=(
            "Authenticated endpoint for Match Checker screens. Returns compact StatPal snapshot summaries "
            "for a fixture, with an optional non-forced refresh before reading the context."
        ),
        tags=["Slip Reviews"],
        parameters=[StatPalFixtureContextQuerySerializer],
        responses={200: StatPalFixtureContextResponseSerializer},
    )
    def get(self, request):
        query = StatPalFixtureContextQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        match_id = str(data.get("match_id") or "")
        provider_match_id = str(data.get("provider_match_id") or "")
        refreshed = None

        if data.get("refresh"):
            refreshed = statpal_snapshot_service.refresh_fixture_snapshots(
                match_id=match_id,
                provider_match_id=provider_match_id,
                force=False,
            )

        context = statpal_snapshot_service.fixture_context(
            match_id=match_id,
            provider_match_id=provider_match_id,
        )
        payload = {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "context": context,
        }
        if refreshed is not None:
            payload["refreshed"] = refreshed
        return Response(api_response_payload(payload))


class StatPalFixtureRefreshView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = StatPalFixtureContextResponseSerializer

    @extend_schema(
        summary="Refresh StatPal fixture context",
        description=(
            "Admin-only endpoint. Refreshes selected StatPal fixture snapshots and returns the compact "
            "context that Match Checker will use. Raw provider payloads remain internal."
        ),
        tags=["Slip Reviews"],
        request=StatPalFixtureRefreshRequestSerializer,
        responses={200: StatPalFixtureContextResponseSerializer},
        examples=[
            OpenApiExample(
                "Refresh one fixture",
                value={
                    "match_id": "1581037",
                    "provider_match_id": "statpal-match-1",
                    "provider_competition_id": "3037",
                    "snapshot_types": ["lineups", "predictions", "prematch_odds"],
                    "force": True,
                },
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = StatPalFixtureRefreshRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        match_id = str(data.get("match_id") or "")
        provider_match_id = str(data.get("provider_match_id") or "")
        refreshed = statpal_snapshot_service.refresh_fixture_snapshots(
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=str(data.get("provider_competition_id") or ""),
            force=bool(data.get("force")),
            snapshot_types=data.get("snapshot_types"),
        )
        context = statpal_snapshot_service.fixture_context(
            match_id=match_id,
            provider_match_id=provider_match_id,
        )
        return Response(
            api_response_payload(
                {
                    "match_id": match_id,
                    "provider_match_id": provider_match_id,
                    "refreshed": refreshed,
                    "context": context,
                }
            )
        )


class StatPalReadinessView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = StatPalReadinessResponseSerializer

    @extend_schema(
        summary="StatPal cache readiness",
        description=(
            "Admin-only endpoint. Inspects cached StatPal data for the requested window without making provider calls, "
            "then returns fixture coverage and a readiness verdict for Match Checker analysis."
        ),
        tags=["Slip Reviews"],
        parameters=[StatPalReadinessQuerySerializer],
        responses={200: StatPalReadinessResponseSerializer},
    )
    def get(self, request):
        query = StatPalReadinessQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data

        from betpreneur.modules.catalog.services.daily_build import StatPalDailyBuildService

        result = StatPalDailyBuildService().readiness_report(
            start_date=data.get("start_date"),
            days=data.get("days", 3),
            include_optional=bool(data.get("include_optional")),
            minimum_average_coverage=float(data.get("min_coverage") or 70.0),
        )
        return Response(api_response_payload(result))
