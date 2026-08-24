from celery.result import AsyncResult
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.algo.models import AlgoRun
from apps.algo.serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    MaintenanceRunRequestSerializer,
    MaintenanceRunResponseSerializer,
    ResultsUpdateSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
)
from apps.algo.tasks import generate_daily_picks, run_monthly_auditor, settle_daily_results
from .maintenance import maintenance_jobs
from .response_utils import api_response_payload


@extend_schema_view(
    list=extend_schema(
        summary="List algo runs",
        description="Internal staff endpoint. Lists historical algorithm executions and their generated picks.",
        tags=["Admin Algo"],
    ),
    retrieve=extend_schema(
        summary="Get algo run",
        description="Internal staff endpoint. Gets a specific algorithm execution record by ID.",
        tags=["Admin Algo"],
    ),
    create=extend_schema(
        summary="Queue manual algo run",
        description="""
        Internal staff endpoint. Queues the betting algorithm for a target date.

        **Optional payload:**
        ```json
        {
          "target_date": "2026-05-04"
        }
        ```

        If no target_date is provided, runs for today.
        """,
        tags=["Admin Algo"],
        request=AlgoRunCreateSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Generate picks for a date",
                value={"target_date": "2026-05-19"},
                request_only=True,
            )
        ],
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
        description="Internal staff endpoint. Queues settlement for the target date. If omitted, settles yesterday in WAT.",
        tags=["Admin Algo"],
        request=ResultsUpdateSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Settle a date",
                value={"target_date": "2026-05-18"},
                request_only=True,
            )
        ],
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
        description="Internal staff endpoint. Queues the monthly auditor report for an optional date range.",
        tags=["Admin Algo"],
        request=AuditorRunSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Audit date range",
                value={"from_date": "2026-04-01", "to_date": "2026-04-30"},
                request_only=True,
            )
        ],
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


class MaintenanceRunView(APIView):
    permission_classes = [AllowAny]
    serializer_class = MaintenanceRunResponseSerializer

    @extend_schema(
        summary="Run Match Checker data jobs",
        description=(
            "Public endpoint. Queues the background jobs the Match Checker depends on "
            "and returns their task ids. Omit `jobs` to run all of them. These make roughly two "
            "thousand provider calls in total, so they are queued rather than executed inline; "
            "poll `/api/algo/tasks/{task_id}/` for progress."
        ),
        tags=["Algo"],
        request=MaintenanceRunRequestSerializer,
        responses={202: MaintenanceRunResponseSerializer},
    )
    def post(self, request):
        serializer = MaintenanceRunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        available = maintenance_jobs()
        requested = serializer.validated_data.get("jobs") or list(available)

        unknown = [name for name in requested if name not in available]
        if unknown:
            return Response(
                {"detail": f"Unknown jobs: {', '.join(unknown)}", "available": sorted(available)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        days = serializer.validated_data.get("days", 3)
        queued = []
        for name in requested:
            task, description = available[name]
            async_result = (
                task.delay(days=days)
                if name in {"fixture_horizon", "statpal_daily_cache", "slip_review_market_cache"}
                else task.delay()
            )
            queued.append({"job": name, "task_id": async_result.id, "description": description})

        return Response(
            {"queued": queued, "poll": "/api/algo/tasks/{task_id}/"},
            status=status.HTTP_202_ACCEPTED,
        )


class TaskStatusView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        summary="Get background task status",
        description="Internal staff endpoint. Returns Celery task status and result/error when available.",
        tags=["Admin Algo"],
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
            payload["result"] = api_response_payload(task.result)
        elif task.failed():
            payload["error"] = str(task.result)
        return Response(payload, status=status.HTTP_200_OK)


__all__ = ["AlgoRunViewSet", "MaintenanceRunView", "TaskStatusView"]
