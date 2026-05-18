from datetime import timedelta
import csv

from celery.result import AsyncResult
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AlgoRun, Pick, PickBack
from .serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    PickSerializer,
    PickBackResponseSerializer,
    PublicSummarySerializer,
    RecordResponseSerializer,
    RecordQuerySerializer,
    ResultsUpdateSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
    TopPickResponseSerializer,
)
from .tasks import generate_daily_picks, run_monthly_auditor, settle_daily_results


def _performance_summary(queryset, window_days):
    aggregate = queryset.aggregate(
        wins=Count("id", filter=Q(status=Pick.Status.WIN)),
        losses=Count("id", filter=Q(status=Pick.Status.LOSS)),
        voids=Count("id", filter=Q(status=Pick.Status.VOID)),
        pending=Count("id", filter=Q(status=Pick.Status.PENDING)),
        stake=Sum("stake", filter=Q(status__in=[Pick.Status.WIN, Pick.Status.LOSS])),
        pnl=Sum("pnl", filter=Q(status__in=[Pick.Status.WIN, Pick.Status.LOSS])),
    )
    wins = aggregate["wins"] or 0
    losses = aggregate["losses"] or 0
    settled = wins + losses
    stake = float(aggregate["stake"] or 0)
    pnl = float(aggregate["pnl"] or 0)
    return {
        "hit_rate": round((wins / settled) * 100, 1) if settled else 0.0,
        "roi_flat": round((pnl / stake) * 100, 1) if stake else 0.0,
        "picks_logged": queryset.count(),
        "wins": wins,
        "losses": losses,
        "voids": aggregate["voids"] or 0,
        "pending": aggregate["pending"] or 0,
        "window_days": window_days,
    }


def _latest_successful_run(target_date):
    return (
        AlgoRun.objects.filter(target_date=target_date, status=AlgoRun.Status.SUCCESS)
        .prefetch_related("picks")
        .order_by("-created_at")
        .first()
    )


def _daily_picks_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "summary": {
                "fixture_count": 0,
                "market_count": 0,
                "selected_pick_count": 0,
                "picks_70_plus": 0,
                "picks_65_plus": 0,
                "markets_70_plus": 0,
                "markets_65_plus": 0,
            },
            "fixtures": [],
        }

    picks = list(algo_run.picks.all().order_by("kickoff", "-confidence", "-ev"))
    backed_ids = set()
    if request and request.user.is_authenticated:
        backed_ids = set(
            PickBack.objects.filter(user=request.user, pick__in=picks)
            .values_list("pick_id", flat=True)
        )

    fixture_summaries = {
        str(item.get("match_id")): item
        for item in (algo_run.result or {}).get("fixture_summaries", [])
    }
    fixtures = {}
    for item in fixture_summaries.values():
        fixtures[item.get("match_id")] = {
            "fixture": item.get("fixture", ""),
            "home_team": item.get("home_team", ""),
            "away_team": item.get("away_team", ""),
            "league": item.get("league", ""),
            "kickoff": item.get("kickoff", ""),
            "match_id": item.get("match_id", ""),
            "market_count": item.get("market_count", 0),
            "markets_70_plus": item.get("markets_70_plus", 0),
            "markets_65_plus": item.get("markets_65_plus", 0),
            "picks": [],
        }

    for pick in picks:
        key = str(pick.match_id)
        if key not in fixtures:
            fixtures[key] = {
                "fixture": pick.fixture,
                "home_team": pick.home_team,
                "away_team": pick.away_team,
                "league": pick.league,
                "kickoff": pick.kickoff,
                "match_id": pick.match_id,
                "market_count": 0,
                "markets_70_plus": 0,
                "markets_65_plus": 0,
                "picks": [],
            }
        data = PickSerializer(pick).data
        data["backed_by_me"] = pick.id in backed_ids
        data["backed_count"] = pick.backs.count()
        fixtures[key]["picks"].append(data)

    return {
        "date": target_date,
        "published": True,
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "fixture_count": algo_run.total_scored,
            "market_count": (algo_run.result or {}).get("market_count", 0),
            "selected_pick_count": len(picks),
            "picks_70_plus": sum(1 for pick in picks if pick.confidence >= 70),
            "picks_65_plus": sum(1 for pick in picks if pick.confidence >= 65),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "fixtures": list(fixtures.values()),
    }


@extend_schema_view(
    list=extend_schema(
        summary="List algo runs",
        description="Get all algorithm execution records",
        tags=["Algo"],
    ),
    retrieve=extend_schema(
        summary="Get algo run",
        description="Get a specific algo run by ID",
        tags=["Algo"],
    ),
    create=extend_schema(
        summary="Run algo",
        description="""
        Execute the betting algorithm for a target date.
        
        **Optional payload:**
        ```json
        {
          "target_date": "2026-05-04"
        }
        ```
        
        If no target_date is provided, runs for today.
        """,
        tags=["Algo"],
        request=AlgoRunCreateSerializer,
        responses={202: TaskQueuedSerializer},
    ),
)
class AlgoRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AlgoRun.objects.prefetch_related("picks").all()
    serializer_class = AlgoRunSerializer
    permission_classes = [IsAdminUser]

    def create(self, request):
        serializer = AlgoRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("target_date")
        task = generate_daily_picks.delay(target_date.isoformat() if target_date else None)
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Algo run queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Update algo results",
        description="Settle picks for the target date. If omitted, settles yesterday in WAT.",
        tags=["Algo"],
        request=ResultsUpdateSerializer,
        responses={202: TaskQueuedSerializer},
    )
    @action(detail=False, methods=["post"], url_path="update-results")
    def update_results(self, request):
        serializer = ResultsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("target_date")
        task = settle_daily_results.delay(target_date.isoformat() if target_date else None)
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Results settlement queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Run algo auditor",
        description="Generate the monthly auditor report for an optional date range.",
        tags=["Algo"],
        request=AuditorRunSerializer,
        responses={202: TaskQueuedSerializer},
    )
    @action(detail=False, methods=["post"], url_path="run-auditor")
    def run_auditor(self, request):
        serializer = AuditorRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from_date = serializer.validated_data.get("from_date")
        to_date = serializer.validated_data.get("to_date")
        task = run_monthly_auditor.delay(
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
        )
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Auditor run queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PublicSummaryView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PublicSummarySerializer

    @extend_schema(
        summary="Public audited performance summary",
        description="Returns headline stats for the public proof/landing page.",
        tags=["Algo"],
        responses={200: PublicSummarySerializer},
    )
    def get(self, request):
        window_days = 90
        since = timezone.localdate() - timedelta(days=window_days)
        queryset = Pick.objects.filter(match_date__gte=since)
        return Response(_performance_summary(queryset, window_days))


class DailyPicksView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Daily picks",
        description="Returns the published picks for a matchday. Defaults to today in WAT.",
        tags=["Algo"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: DailyPicksResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        return Response(_daily_picks_payload(target_date, request))


class TopPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TopPickResponseSerializer

    @extend_schema(
        summary="Top pick of the day",
        description="Returns the highest-confidence published pick for the requested matchday.",
        tags=["Algo"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: TopPickResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date)
        pick = None
        if algo_run:
            pick = algo_run.picks.order_by("-confidence", "-ev").first()
        pick_data = PickSerializer(pick).data if pick else None
        if pick_data:
            pick_data["backed_by_me"] = PickBack.objects.filter(pick=pick, user=request.user).exists()
            pick_data["backed_count"] = pick.backs.count()
        return Response(
            {
                "date": target_date,
                "published": bool(pick),
                "pick": pick_data,
            }
        )


class DailyPicksDownloadView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Download daily picks",
        description="Downloads the authenticated daily picks as CSV.",
        tags=["Algo"],
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


class BackPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PickBackResponseSerializer

    @extend_schema(
        summary="Back a pick",
        description="Marks that the authenticated user backed this pick.",
        tags=["Algo"],
        responses={200: PickBackResponseSerializer, 201: PickBackResponseSerializer},
    )
    def post(self, request, pick_id):
        pick = get_object_or_404(Pick, id=pick_id)
        backed, created = PickBack.objects.get_or_create(pick=pick, user=request.user)
        return Response(
            {
                "pick_id": pick.id,
                "backed": True,
                "created": created,
                "backed_count": pick.backs.count(),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PublicRecordView(APIView):
    permission_classes = [AllowAny]
    serializer_class = RecordResponseSerializer

    @extend_schema(
        summary="Public audited pick record",
        description="Returns settled and pending picks for the requested audit window.",
        tags=["Algo"],
        parameters=[RecordQuerySerializer],
        responses={200: RecordResponseSerializer},
    )
    def get(self, request):
        query = RecordQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        window_days = query.validated_data["days"]
        since = timezone.localdate() - timedelta(days=window_days)
        picks = Pick.objects.filter(match_date__gte=since).order_by("-match_date", "-confidence")
        return Response(
            {
                "summary": _performance_summary(picks, window_days),
                "picks": PickSerializer(picks, many=True).data,
            }
        )


class TaskStatusView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        summary="Get background task status",
        description="Returns Celery task status and result/error when available.",
        tags=["Algo"],
        responses={200: TaskStatusSerializer},
    )
    def get(self, request, task_id):
        task = AsyncResult(task_id)
        payload = {
            "task_id": task_id,
            "status": task.status.lower(),
            "result": None,
            "error": "",
        }
        if task.successful():
            payload["result"] = task.result
        elif task.failed():
            payload["error"] = str(task.result)
        return Response(payload, status=status.HTTP_200_OK)
