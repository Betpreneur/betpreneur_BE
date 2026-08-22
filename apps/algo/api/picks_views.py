"""Daily picks and public record API views."""

import csv
from datetime import timedelta

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.algo.models import Pick
from apps.algo.serializers import (
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    PickDetailResponseSerializer,
    PickSerializer,
    PublicSummarySerializer,
    RecordQuerySerializer,
    RecordResponseSerializer,
    TopPickResponseSerializer,
)
from apps.algo.views import (
    EXCLUDED_MARKETS,
    SETTLED_PICK_STATUSES,
    _bulk_game_back_context,
    _compact_daily_picks_payload,
    _compact_pick_payload,
    _daily_picks_payload,
    _dedupe_latest_public_picks,
    _latest_successful_run,
    _performance_summary,
    _pick_detail_payload,
    _private_cached_response,
    _public_cached_response,
    _public_record_pick_payload,
    _top_pick_sort_key,
)


class PublicSummaryView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = PublicSummarySerializer

    @extend_schema(
        summary="Public audited performance summary",
        description="Returns headline stats for the public proof/landing page.",
        tags=["Public Record"],
        responses={200: PublicSummarySerializer},
    )
    def get(self, request):
        window_days = 90
        since = timezone.localdate() - timedelta(days=window_days)
        today = timezone.localdate()
        picks = Pick.objects.filter(
            status__in=SETTLED_PICK_STATUSES,
        ).filter(
            Q(match_date__gte=since, match_date__lte=today)
            | Q(match_date__isnull=True, run__target_date__gte=since, run__target_date__lte=today)
        ).select_related("run").order_by(
            "-match_date",
            "-run__target_date",
            "-created_at",
            "-id",
        )
        return _public_cached_response(
            _performance_summary(_dedupe_latest_public_picks(picks), window_days),
            request=request,
        )


class DailyPicksView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Daily picks",
        description="Authenticated user endpoint. Returns the published picks for a matchday. Defaults to today in WAT.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: DailyPicksResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        if query.validated_data.get("view") == "compact":
            return _private_cached_response(_compact_daily_picks_payload(target_date, request), request=request)
        return _private_cached_response(_daily_picks_payload(target_date, request), request=request)


class TopPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TopPickResponseSerializer

    @extend_schema(
        summary="Top picks of the day",
        description="Authenticated user endpoint. Returns the high-value published picks for the requested matchday, ranked by tier, confidence, EV, and odds.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: TopPickResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date, prefetch=False)
        picks = []
        if algo_run:
            picks = sorted(
                [
                    pick
                    for pick in Pick.objects.filter(run=algo_run).exclude(market__in=EXCLUDED_MARKETS)
                ],
                key=_top_pick_sort_key,
                reverse=True,
            )
        match_ids = [str(pick.match_id or "") for pick in picks if str(pick.match_id or "")]
        backed_game_counts, backed_game_ids, _backed_markets = _bulk_game_back_context(match_ids, request)
        if query.validated_data.get("view") == "compact":
            picks_data = [
                _compact_pick_payload(
                    pick,
                    backed_game_counts=backed_game_counts,
                    backed_game_ids=backed_game_ids,
                )
                for pick in picks
            ]
        else:
            picks_data = PickSerializer(
                picks,
                many=True,
                context={
                    "request": request,
                    "backed_game_counts": backed_game_counts,
                    "backed_game_ids": backed_game_ids,
                },
            ).data
        top_pick = picks_data[0] if picks_data else None
        return _private_cached_response(
            {
                "date": target_date,
                "published": bool(picks_data),
                "count": len(picks_data),
                "pick": top_pick,
                "top_pick": top_pick,
                "picks": picks_data,
            },
            request=request,
        )


class PickDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PickDetailResponseSerializer

    @extend_schema(
        summary="Pick detail",
        description="Authenticated user endpoint. Returns one published pick with fixture context, market context, model summary, and historical performance slices.",
        tags=["Picks"],
        responses={200: PickDetailResponseSerializer},
    )
    def get(self, request, pick_id):
        pick = get_object_or_404(
            Pick.objects.select_related("run").prefetch_related("backs", "run__picks"),
            id=pick_id,
        )
        return Response(_pick_detail_payload(pick, request))


class DailyPicksDownloadView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Download daily picks",
        description="Authenticated user endpoint. Downloads the daily picks as CSV.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date)
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="betpreneur_picks_{target_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(["date", "fixture", "league", "kickoff", "tier", "market", "confidence", "odds", "ev", "status"])
        if algo_run:
            for pick in algo_run.picks.all().order_by("kickoff", "-confidence"):
                if pick.market in EXCLUDED_MARKETS:
                    continue
                writer.writerow([
                    pick.match_date,
                    pick.fixture,
                    pick.league,
                    pick.kickoff,
                    pick.tier,
                    pick.market,
                    pick.confidence,
                    pick.odds,
                    pick.ev,
                    pick.status,
                ])
        return response


class PublicRecordView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = RecordResponseSerializer

    @extend_schema(
        summary="Public audited pick record",
        description="Returns a deduplicated public audit table for the requested window. Each record is the latest posted copy for a date, fixture and market.",
        tags=["Public Record"],
        parameters=[RecordQuerySerializer],
        responses={200: RecordResponseSerializer},
    )
    def get(self, request):
        query = RecordQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        window_days = query.validated_data["days"]
        since = timezone.localdate() - timedelta(days=window_days)
        today = timezone.localdate()
        picks_queryset = Pick.objects.filter(
            status__in=SETTLED_PICK_STATUSES,
        ).filter(
            Q(match_date__gte=since, match_date__lte=today)
            | Q(match_date__isnull=True, run__target_date__gte=since, run__target_date__lte=today)
        ).select_related("run").order_by(
            "-match_date",
            "-run__target_date",
            "-created_at",
            "-id",
        )
        picks = _dedupe_latest_public_picks(picks_queryset)
        return _public_cached_response(
            {
                "summary": _performance_summary(picks, window_days),
                "records": [_public_record_pick_payload(pick) for pick in picks],
            },
            request=request,
        )


__all__ = [
    "DailyPicksDownloadView",
    "DailyPicksView",
    "PickDetailView",
    "PublicRecordView",
    "PublicSummaryView",
    "TopPickView",
]
