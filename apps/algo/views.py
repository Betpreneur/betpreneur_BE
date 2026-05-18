from datetime import timedelta

from celery.result import AsyncResult
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AlgoRun, Pick
from .serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    PickSerializer,
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


class PublicDailyPicksView(APIView):
    permission_classes = [AllowAny]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Public daily picks",
        description="Returns the published picks for a matchday. Defaults to today in WAT.",
        tags=["Algo"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: DailyPicksResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date)
        picks = algo_run.picks.all() if algo_run else Pick.objects.none()
        return Response(
            {
                "date": target_date,
                "published": bool(algo_run),
                "run_id": algo_run.id if algo_run else None,
                "posted_at": algo_run.created_at if algo_run else None,
                "picks": PickSerializer(picks, many=True).data,
            }
        )


class PublicTopPickView(APIView):
    permission_classes = [AllowAny]
    serializer_class = TopPickResponseSerializer

    @extend_schema(
        summary="Public top pick of the day",
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
        return Response(
            {
                "date": target_date,
                "published": bool(pick),
                "pick": PickSerializer(pick).data if pick else None,
            }
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
