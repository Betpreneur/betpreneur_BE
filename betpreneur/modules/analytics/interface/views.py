"""Public record, market health, and the cross-cutting ops endpoints."""

# Settlement sits above picks and beside analytics, so neither may import its
# tasks. Dispatching by name keeps the layer order intact — Celery resolves
# the task on the worker.
import logging
from datetime import timedelta

from celery import current_app
from celery.result import AsyncResult
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from betpreneur.modules.analytics.interface.serializers import (
    MaintenanceRunRequestSerializer,
    MaintenanceRunResponseSerializer,
    MarketHealthQuerySerializer,
    MarketHealthResponseSerializer,
    ModelHealthResponseSerializer,
    PublicDatasetResponseSerializer,
    PublicSummarySerializer,
    RecordQuerySerializer,
    RecordResponseSerializer,
    TaskStatusSerializer,
)
from betpreneur.modules.analytics.services.model_health import (
    DEFAULT_WINDOW_DAYS,
    model_health_service,
)
from betpreneur.modules.analytics.services.public_dataset import (
    DEFAULT_WINDOW_DAYS as DATASET_WINDOW_DAYS,
)
from betpreneur.modules.analytics.services.public_dataset import (
    MAX_WINDOW_DAYS as DATASET_MAX_WINDOW_DAYS,
)
from betpreneur.modules.analytics.services.public_dataset import (
    public_dataset_service,
)
from betpreneur.modules.catalog.api import api_response_payload
from betpreneur.modules.picks.api import MarketPrediction, Pick
from betpreneur.platform.cache.http import public_cached_response
from betpreneur.platform.config import env_int as _env_int

SETTLE_PICKS_TASK = "betpreneur.modules.settlement.tasks.settle_daily_results"
SETTLE_SLIPS_TASK = "betpreneur.modules.settlement.tasks.settle_slip_selections"

log = logging.getLogger(__name__)


SLIP_REVIEW_STALE_AFTER_SECONDS = _env_int("SLIP_REVIEW_STALE_AFTER_SECONDS", 20 * 60)


SETTLED_PICK_STATUSES = [Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID]


def _performance_summary(queryset, window_days):
    if not hasattr(queryset, "aggregate"):
        return _performance_summary_from_picks(queryset, window_days)

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


def _performance_summary_from_picks(picks, window_days):
    wins = sum(1 for pick in picks if pick.status == Pick.Status.WIN)
    losses = sum(1 for pick in picks if pick.status == Pick.Status.LOSS)
    voids = sum(1 for pick in picks if pick.status == Pick.Status.VOID)
    pending = sum(1 for pick in picks if pick.status == Pick.Status.PENDING)
    settled = wins + losses
    stake = sum(
        float(pick.stake or 0)
        for pick in picks
        if pick.status in [Pick.Status.WIN, Pick.Status.LOSS]
    )
    pnl = sum(
        float(pick.pnl or 0) for pick in picks if pick.status in [Pick.Status.WIN, Pick.Status.LOSS]
    )
    return {
        "hit_rate": round((wins / settled) * 100, 1) if settled else 0.0,
        "roi_flat": round((pnl / stake) * 100, 1) if stake else 0.0,
        "picks_logged": len(picks),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "pending": pending,
        "window_days": window_days,
    }


def _dedupe_latest_public_picks(picks):
    latest = {}
    for pick in picks:
        key = (
            pick.match_date or pick.run.target_date,
            str(pick.match_id or "").strip(),
            pick.fixture,
            pick.market,
        )
        if key not in latest:
            latest[key] = pick
    return list(latest.values())


def _public_record_pick_payload(pick):
    return {
        "id": pick.id,
        "posted_at": pick.created_at,
        "match_date": pick.match_date or pick.run.target_date,
        "fixture": pick.fixture,
        "home_team": pick.home_team,
        "away_team": pick.away_team,
        "league": pick.league,
        "kickoff": pick.kickoff,
        "tier": pick.tier,
        "market": pick.market,
        "pick": pick.meaning or pick.market,
        "confidence": pick.confidence,
        "odds": pick.odds,
        "stake": pick.stake,
        "status": pick.status,
        "score": pick.score,
        "pnl": pick.pnl,
        "settled_at": pick.settled_at,
    }


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
        picks = (
            Pick.objects.filter(
                status__in=SETTLED_PICK_STATUSES,
            )
            .filter(
                Q(match_date__gte=since, match_date__lte=today)
                | Q(
                    match_date__isnull=True,
                    run__target_date__gte=since,
                    run__target_date__lte=today,
                )
            )
            .select_related("run")
            .order_by(
                "-match_date",
                "-run__target_date",
                "-created_at",
                "-id",
            )
        )
        return public_cached_response(
            _performance_summary(_dedupe_latest_public_picks(picks), window_days),
            request=request,
        )


def _market_health_state(
    loss_streak, recent_5_losses, recent_10_hit_rate, recent_10_count, roi_flat
):
    if (
        loss_streak >= 3
        or recent_5_losses >= 4
        or (recent_10_count >= 5 and recent_10_hit_rate < 35)
    ):
        return "suppressed"
    if (
        loss_streak >= 2
        or recent_5_losses >= 3
        or (recent_10_count >= 5 and recent_10_hit_rate < 45)
    ):
        return "cooling"
    if recent_10_count >= 5 and recent_10_hit_rate >= 60 and roi_flat >= 0:
        return "recovered"
    return "active"


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
            recent_5_losses = sum(
                1 for status_value in recent[:5] if status_value == MarketPrediction.Status.LOSS
            )
            recent_10 = recent[:10]
            recent_10_wins = sum(
                1 for status_value in recent_10 if status_value == MarketPrediction.Status.WIN
            )
            hit_rate = round((item["wins"] / item["count"]) * 100, 1) if item["count"] else 0.0
            roi_flat = round((item["pnl"] / item["stake"]) * 100, 1) if item["stake"] else 0.0
            recent_10_hit_rate = (
                round((recent_10_wins / len(recent_10)) * 100, 1) if recent_10 else 0.0
            )
            item.update(
                {
                    "hit_rate": hit_rate,
                    "roi_flat": roi_flat,
                    "avg_confidence": round(item["confidence_total"] / item["count"], 1)
                    if item["count"]
                    else 0.0,
                    "loss_streak": loss_streak,
                    "recent_5_losses": recent_5_losses,
                    "recent_10_hit_rate": recent_10_hit_rate,
                    "state": _market_health_state(
                        loss_streak, recent_5_losses, recent_10_hit_rate, len(recent_10), roi_flat
                    ),
                }
            )
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
        return Response(
            {
                "days": days,
                "scope": scope,
                "count": len(markets),
                "markets": markets,
            }
        )


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
        picks_queryset = (
            Pick.objects.filter(
                status__in=SETTLED_PICK_STATUSES,
            )
            .filter(
                Q(match_date__gte=since, match_date__lte=today)
                | Q(
                    match_date__isnull=True,
                    run__target_date__gte=since,
                    run__target_date__lte=today,
                )
            )
            .select_related("run")
            .order_by(
                "-match_date",
                "-run__target_date",
                "-created_at",
                "-id",
            )
        )
        picks = _dedupe_latest_public_picks(picks_queryset)
        return public_cached_response(
            {
                "summary": _performance_summary(picks, window_days),
                "records": [_public_record_pick_payload(pick) for pick in picks],
            },
            request=request,
        )


def _maintenance_jobs():
    """
    Data jobs the Match Checker depends on, queued rather than run inline.

    Each is expensive — the fixture sweep and the model fit are roughly a thousand
    provider calls apiece — so this returns task ids to poll rather than holding the
    request open.
    """

    # Every job here belongs to a module analytics sits above or beside, and a
    # facade that exported tasks would drag celery into every one of its
    # consumers. Resolving by name needs no import at all.
    def task(path):
        return current_app.signature(path)

    build_statpal_daily_cache = task("betpreneur.modules.catalog.tasks.build_statpal_daily_cache")
    sync_fixture_horizon = task("betpreneur.modules.catalog.tasks.sync_fixture_horizon")
    hydrate_team_intelligence_history = task(
        "betpreneur.modules.catalog.tasks.hydrate_team_intelligence_history"
    )
    build_team_recent_form = task("betpreneur.modules.catalog.tasks.build_team_recent_form")
    build_team_market_profiles = task("betpreneur.modules.catalog.tasks.build_team_market_profiles")
    refresh_team_data_coverage = task("betpreneur.modules.catalog.tasks.refresh_team_data_coverage")
    backfill_team_intelligence = task("betpreneur.modules.catalog.tasks.backfill_team_intelligence")
    refresh_team_intelligence_nightly = task(
        "betpreneur.modules.analytics.tasks.refresh_team_intelligence_nightly"
    )
    evaluate_strategy_memory = task("betpreneur.modules.analytics.tasks.evaluate_strategy_memory")
    build_slip_review_market_cache = task(
        "betpreneur.modules.picks.tasks.build_slip_review_market_cache"
    )
    cleanup_slip_review_market_cache = task(
        "betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache"
    )
    fit_score_models = task("betpreneur.modules.scoring.tasks.fit_score_models")
    refresh_imminent_lineups = task("betpreneur.modules.scoring.tasks.refresh_imminent_lineups")
    refresh_player_availability = task(
        "betpreneur.modules.scoring.tasks.refresh_player_availability"
    )
    recover_stale_slip_reviews = task("betpreneur.modules.slips.tasks.recover_stale_slip_reviews")
    settle_slip_selections = current_app.signature(SETTLE_SLIPS_TASK)

    return {
        # Ordered so a full run populates fixtures before anything that reads them.
        "statpal_daily_cache": (
            build_statpal_daily_cache,
            "Build StatPal 3-day fixtures and analysis snapshots",
        ),
        "fixture_horizon": (sync_fixture_horizon, "Cache every fixture in the 3-day window"),
        "team_intelligence_history": (
            hydrate_team_intelligence_history,
            "Hydrate current/previous season team baselines for top leagues",
        ),
        "team_recent_form": (
            build_team_recent_form,
            "Build last-5/10/15 all/home/away form profiles",
        ),
        "team_market_profiles": (
            build_team_market_profiles,
            "Build historical market behaviour for teams and leagues",
        ),
        "team_data_coverage": (
            refresh_team_data_coverage,
            "Refresh Team Intelligence freshness, gaps and source-quality coverage",
        ),
        "team_intelligence_backfill": (
            backfill_team_intelligence,
            "Run one-time current/previous season backfill for top Team Intelligence leagues",
        ),
        "team_intelligence_nightly": (
            refresh_team_intelligence_nightly,
            "Queue the ordered nightly Team Intelligence refresh chain",
        ),
        "strategy_memory": (
            evaluate_strategy_memory,
            "Evaluate strategy promotion/cooling/suppression against next-period results",
        ),
        "slip_review_market_cache": (
            build_slip_review_market_cache,
            "Pre-score private markets for slip review",
        ),
        "slip_review_market_cache_cleanup": (
            cleanup_slip_review_market_cache,
            "Delete expired private slip-review market rows",
        ),
        "score_models": (fit_score_models, "Refit per-league goal models"),
        "player_availability": (refresh_player_availability, "Reload injuries and suspensions"),
        "lineups": (refresh_imminent_lineups, "Pull team sheets for imminent fixtures"),
        "settle_slips": (settle_slip_selections, "Settle yesterday's slip selections"),
        "recover_slip_reviews": (recover_stale_slip_reviews, "Finalize or fail stale slip reviews"),
    }


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
        available = _maintenance_jobs()
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
            log.info("Maintenance job queued by %s: %s -> %s", request.user, name, async_result.id)

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


class ModelHealthView(APIView):
    """Stage 20 model-health dashboard.

    Staff-only: it exposes calibration gaps and per-market ROI, which is
    internal diagnostic information, not something a bettor should read as a
    tip sheet.
    """

    permission_classes = [IsAdminUser]
    serializer_class = ModelHealthResponseSerializer

    @extend_schema(
        operation_id="algo_model_health_retrieve",
        summary="Model health metrics",
        description=(
            "Internal staff endpoint. Daily model-health metrics for a trailing window: "
            "calibration gap, ROI by market/odds band/tier, odds quality, pipeline hygiene "
            "and strategy-action effectiveness. Every metric reports its own availability, "
            "so a metric with no data reads as no_data rather than zero."
        ),
        tags=["Analytics"],
        responses={200: ModelHealthResponseSerializer},
    )
    def get(self, request):
        try:
            window = int(request.query_params.get("days", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            window = DEFAULT_WINDOW_DAYS
        window = max(1, min(window, 365))
        report = model_health_service.report(window_days=window)
        return Response(report.to_dict())


def _dataset_window(request):
    try:
        window = int(request.query_params.get("days", DATASET_WINDOW_DAYS))
    except (TypeError, ValueError):
        window = DATASET_WINDOW_DAYS
    return max(1, min(window, DATASET_MAX_WINDOW_DAYS))


class PublicDatasetView(APIView):
    """Stage 21 public reporting dataset.

    The single source for anything published outwardly. Readable by anyone,
    because the whole point is that the record can be audited without asking
    us for a spreadsheet.

    Aggregates only. The row-level export lives on its own staff endpoint
    rather than behind a flag here: this response is served with
    ``Cache-Control: public``, so a shared cache that stored a staff copy
    would hand the rows to the next anonymous caller.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = PublicDatasetResponseSerializer

    @extend_schema(
        operation_id="algo_public_dataset_retrieve",
        summary="Public reporting dataset",
        description=(
            "Frozen, deduplicated, settled-only performance dataset for the transparency "
            "report. Headline ROI is computed from real bookmaker odds only; estimated-odds "
            "returns are reported separately and marked non-comparable. Voids leave the ROI "
            "denominator and are declared. Every build carries a freeze timestamp and a "
            "content-derived dataset_id."
        ),
        tags=["Public Record"],
        responses={200: PublicDatasetResponseSerializer},
    )
    def get(self, request):
        dataset = public_dataset_service.build(
            window_days=_dataset_window(request)
        )
        return public_cached_response(
            dataset.as_dict(include_records=False), request=request
        )


class PublicDatasetExportView(APIView):
    """Row-level export of the published dataset.

    Staff only and never publicly cached. The aggregates are the public claim;
    the settled-pick rows behind them are the pick history the product sells,
    so they are an operational export rather than part of the public record.
    """

    permission_classes = [IsAdminUser]
    serializer_class = PublicDatasetResponseSerializer

    @extend_schema(
        operation_id="algo_public_dataset_export_retrieve",
        summary="Public reporting dataset export",
        description=(
            "Internal staff endpoint. The same frozen dataset as the public reporting "
            "endpoint, with the underlying settled-pick rows included, each carrying its "
            "odds provenance. Intended to replace hand-built transparency spreadsheets."
        ),
        tags=["Analytics"],
        responses={200: PublicDatasetResponseSerializer},
    )
    def get(self, request):
        dataset = public_dataset_service.build(
            window_days=_dataset_window(request)
        )
        return Response(dataset.as_dict(include_records=True))
