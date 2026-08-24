"""Market-data and provider-readiness API views."""
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.algo.market_data.api import StatPalDailyBuildService, statpal_snapshot_service
from apps.algo.models import MarketPrediction
from apps.algo.serializers import (
    MarketHealthQuerySerializer,
    MarketHealthResponseSerializer,
    StatPalFixtureContextQuerySerializer,
    StatPalFixtureContextResponseSerializer,
    StatPalFixtureRefreshRequestSerializer,
    StatPalReadinessQuerySerializer,
    StatPalReadinessResponseSerializer,
)
from .response_utils import api_response_payload as _api_response_payload


def _market_health_state(loss_streak, recent_5_losses, recent_10_hit_rate, recent_10_count, roi_flat):
    if loss_streak >= 3 or recent_5_losses >= 4 or (recent_10_count >= 5 and recent_10_hit_rate < 35):
        return "suppressed"
    if loss_streak >= 2 or recent_5_losses >= 3 or (recent_10_count >= 5 and recent_10_hit_rate < 45):
        return "cooling"
    if recent_10_count >= 5 and recent_10_hit_rate >= 60 and roi_flat >= 0:
        return "recovered"
    return "active"


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
        return Response(_api_response_payload(payload))


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
            _api_response_payload(
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

        result = StatPalDailyBuildService().readiness_report(
            start_date=data.get("start_date"),
            days=data.get("days", 3),
            include_optional=bool(data.get("include_optional")),
            minimum_average_coverage=float(data.get("min_coverage") or 70.0),
        )
        return Response(_api_response_payload(result))


class MarketHealthView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = MarketHealthResponseSerializer

    @extend_schema(
        summary="Internal market health",
        description="Staff endpoint. Shows market performance used to suppress, watch, or restore markets.",
        tags=["Admin Algo"],
        parameters=[MarketHealthQuerySerializer],
        responses={200: MarketHealthResponseSerializer},
    )
    def get(self, request):
        query = MarketHealthQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data.get("days", 90)
        scope = query.validated_data.get("scope", "all")
        market_filter = query.validated_data.get("market", "")
        since = timezone.localdate() - timedelta(days=days)

        qs = MarketPrediction.objects.filter(
            match_date__gte=since,
            status__in=[MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS],
        ).order_by("-match_date", "-created_at", "-id")
        if market_filter:
            qs = qs.filter(market__iexact=market_filter)
        if scope == "published":
            qs = qs.filter(published=True)
        elif scope == "internal":
            qs = qs.filter(published=False)

        latest = {}
        for prediction in qs:
            key = (
                prediction.match_date,
                str(prediction.match_id or "").strip(),
                prediction.fixture,
                prediction.market,
            )
            if key not in latest:
                latest[key] = prediction

        grouped = {}
        for prediction in latest.values():
            item = grouped.setdefault(
                prediction.market,
                {
                    "market": prediction.market,
                    "count": 0,
                    "wins": 0,
                    "losses": 0,
                    "published_count": 0,
                    "internal_count": 0,
                    "stake": 0.0,
                    "pnl": 0.0,
                    "confidence_total": 0.0,
                    "recent_statuses": [],
                },
            )
            item["count"] += 1
            if prediction.status == MarketPrediction.Status.WIN:
                item["wins"] += 1
            else:
                item["losses"] += 1
            if prediction.published:
                item["published_count"] += 1
            else:
                item["internal_count"] += 1
            item["stake"] += 1000.0
            item["pnl"] += float(prediction.pnl_simulated or 0)
            item["confidence_total"] += float(prediction.confidence or 0)
            if len(item["recent_statuses"]) < 10:
                item["recent_statuses"].append(prediction.status)

        markets = []
        for item in grouped.values():
            recent = item.pop("recent_statuses")
            loss_streak = 0
            for status_value in recent:
                if status_value != MarketPrediction.Status.LOSS:
                    break
                loss_streak += 1
            recent_5_losses = sum(1 for status_value in recent[:5] if status_value == MarketPrediction.Status.LOSS)
            recent_10 = recent[:10]
            recent_10_wins = sum(1 for status_value in recent_10 if status_value == MarketPrediction.Status.WIN)
            hit_rate = round((item["wins"] / item["count"]) * 100, 1) if item["count"] else 0.0
            roi_flat = round((item["pnl"] / item["stake"]) * 100, 1) if item["stake"] else 0.0
            recent_10_hit_rate = round((recent_10_wins / len(recent_10)) * 100, 1) if recent_10 else 0.0
            item.update({
                "hit_rate": hit_rate,
                "roi_flat": roi_flat,
                "avg_confidence": round(item["confidence_total"] / item["count"], 1) if item["count"] else 0.0,
                "loss_streak": loss_streak,
                "recent_5_losses": recent_5_losses,
                "recent_10_hit_rate": recent_10_hit_rate,
                "state": _market_health_state(loss_streak, recent_5_losses, recent_10_hit_rate, len(recent_10), roi_flat),
            })
            item.pop("stake", None)
            item.pop("confidence_total", None)
            markets.append(item)

        state_rank = {"suppressed": 3, "cooling": 2, "active": 1, "recovered": 0}
        markets.sort(
            key=lambda item: (
                state_rank.get(item["state"], 0),
                item["loss_streak"],
                item["recent_5_losses"],
                -item["hit_rate"],
            ),
            reverse=True,
        )
        return Response({
            "days": days,
            "scope": scope,
            "count": len(markets),
            "markets": markets,
        })


__all__ = [
    "MarketHealthView",
    "StatPalFixtureContextView",
    "StatPalFixtureRefreshView",
    "StatPalReadinessView",
]
