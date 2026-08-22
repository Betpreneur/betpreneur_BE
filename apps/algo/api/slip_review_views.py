import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.algo import views as legacy_views
from apps.algo.models import SlipRepair, SlipReview, SlipReviewEvent, SlipReviewStreamToken, SlipSelection, TokenTransaction
from apps.algo.serializers import (
    BetanoSlipImportRequestSerializer,
    ManualSlipReviewRequestSerializer,
    ManualSlipReviewResponseSerializer,
    SlipRepairRequestSerializer,
    SlipRepairResponseSerializer,
    SlipReviewDetailResponseSerializer,
    SlipReviewEventsQuerySerializer,
    SlipReviewEventsResponseSerializer,
    SlipReviewListResponseSerializer,
    SlipReviewOptionsResponseSerializer,
    SlipReviewRandomizeRequestSerializer,
    SlipReviewRandomizeResponseSerializer,
    SlipReviewRecapQuerySerializer,
    SlipReviewRecapResponseSerializer,
    SlipReviewStreamTokenResponseSerializer,
    SportyBetSlipImportRequestSerializer,
)
from apps.algo.slip_review import api as slip_review_redis
from apps.algo.slip_review.api import (
    consume_slip_review_token_reservation,
    create_queued_slip_review,
    empty_slip_summary,
    insufficient_feature_tokens_payload,
    insufficient_tokens_payload,
    plan_repair,
    public_slip_review_progress,
    public_slip_review_status,
    public_slip_review_stream_event,
    publish_slip_review_event,
    repair_payload,
    release_slip_review_token_reservation,
    reserve_slip_review_tokens,
    set_slip_review_progress,
    slip_recap_payload,
    slip_review_event_payload,
    slip_review_progress,
    stream_ticket_hash,
    ticket_risk_service,
)
from apps.algo.tasks import import_slip_review
from apps.algo.wallet.api import InsufficientTokens, token_wallet_service


class ManualSlipReviewView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ManualSlipReviewResponseSerializer

    @extend_schema(
        summary="Review manual match predictions",
        description=(
            "Authenticated user endpoint. Accepts manually typed matches and selected markets, matches each fixture "
            "against the upcoming fixture cache/API-Football fallback, and reviews the selected market using existing "
            "scored market analysis when available."
        ),
        tags=["Slip Reviews"],
        request=ManualSlipReviewRequestSerializer,
        responses={200: ManualSlipReviewResponseSerializer},
    )
    def post(self, request):
        serializer = ManualSlipReviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data.get("days", 3)
        selections = serializer.validated_data["selections"]
        review = create_queued_slip_review(
            request.user,
            source=SlipReview.Source.MANUAL,
            submitted_payload=serializer.validated_data,
        )
        try:
            reserve_slip_review_tokens(review, len(selections))
            set_slip_review_progress(
                review,
                phase="analysing_legs",
                total=len(selections),
                completed=0,
                message=f"Analysing {len(selections)} selections.",
                status=SlipReview.Status.ANALYSING,
            )
            review_scoring_context = {"fixture_universe_synced": False}
            results = [
                legacy_views._analyse_manual_selection(
                    selection,
                    days=days,
                    request=request,
                    force_fresh=True,
                    review_scoring_context=review_scoring_context,
                )
                for selection in selections
            ]
            summary, safe_results = legacy_views._populate_slip_review(review, results)
            consume_slip_review_token_reservation(review)
            review.save(update_fields=["summary", "submitted_payload", "updated_at"])
        except InsufficientTokens as exc:
            error_payload = insufficient_tokens_payload(
                exc,
                review_id=review.id,
                selection_count=len(selections),
            )
            fail_payload = legacy_views.fail_slip_review_import(
                review.id,
                error_payload["message"],
                error_code="insufficient_tokens",
                error_payload=error_payload,
                release_tokens=False,
            )
            return Response(fail_payload, status=status.HTTP_402_PAYMENT_REQUIRED)
        except Exception:
            release_slip_review_token_reservation(review)
            raise
        return Response(
            legacy_views._api_response_payload(
                {
                    "id": review.id,
                    "source": review.source,
                    "status": public_slip_review_status(review.status),
                    "public": summary.get("public", {}),
                    **summary,
                    "selections": safe_results,
                }
            )
        )


class SportyBetSlipImportView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        summary="Import SportyBet slip",
        description=(
            "Authenticated user endpoint. Accepts a SportyBet share URL/code or raw share payload, imports the booked "
            "football selections asynchronously, matches them against cached fixtures, analyses each selected market, "
            "and saves the review. Returns a queued review immediately; poll the review detail endpoint until the "
            "status becomes completed or failed."
        ),
        tags=["Slip Reviews"],
        request=SportyBetSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = SportyBetSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _queue_bookmaker_review(
            request,
            source=SlipReview.Source.SPORTYBET,
            submitted_payload=serializer.validated_data,
        )


class BetanoSlipImportView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        summary="Import Betano slip",
        description=(
            "Authenticated user endpoint. Accepts a Betano booking URL/code, opens it with the backend browser "
            "importer, captures the getbetslip payload, imports the booked football selections, matches them against "
            "cached fixtures, analyses each selected market, and saves the review asynchronously. A raw getbetslip "
            "payload can also be supplied as a fallback. Returns a queued review immediately; poll the review detail "
            "endpoint until the status becomes completed or failed."
        ),
        tags=["Slip Reviews"],
        request=BetanoSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = BetanoSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _queue_bookmaker_review(
            request,
            source=SlipReview.Source.BETANO,
            submitted_payload=serializer.validated_data,
        )


def _queue_bookmaker_review(request, *, source, submitted_payload):
    review = create_queued_slip_review(
        request.user,
        source=source,
        submitted_payload=submitted_payload,
    )
    task = import_slip_review.apply_async(args=[review.id], queue=settings.SLIP_REVIEW_IMPORT_QUEUE)
    review.summary = {
        **empty_slip_summary("Slip import queued.", task_id=task.id),
        "progress": slip_review_progress(
            phase="queued",
            message="Slip import queued.",
        ),
    }
    review.save(update_fields=["summary", "updated_at"])
    publish_slip_review_event(
        review,
        "review.queued",
        {
            "status": review.status,
            "task_id": task.id,
            "progress": review.summary.get("progress") or {},
        },
    )
    return Response(legacy_views._slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)


class SlipRepairView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipRepairResponseSerializer

    @extend_schema(
        summary="Repair a slip",
        description=(
            "Authenticated user endpoint. Builds a revised version of a reviewed slip by "
            "replacing or dropping selections the model cannot defend. Send `decisions` to "
            "accept or reject individual changes; omit it to apply every recommended change. "
            "A repaired ticket is an evidence-based alternative, not a guarantee, and it "
            "usually carries lower combined odds than the original."
        ),
        tags=["Slip Reviews"],
        request=SlipRepairRequestSerializer,
        responses={201: SlipRepairResponseSerializer},
    )
    def post(self, request, review_id):
        review = SlipReview.objects.filter(id=review_id, user=request.user).first()
        if review is None:
            return Response({"detail": "Slip review not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = SlipRepairRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submitted = serializer.validated_data.get("decisions") or []
        decisions = {item["index"]: item["action"] for item in submitted}

        items = [selection.analysis_payload or {} for selection in review.selections.all()]
        if not items:
            return Response(
                {"detail": "This review has no analysed selections to repair."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ticket_risk = ticket_risk_service.assess(items)
        plan = plan_repair(items, ticket_risk, decisions=decisions)
        repair = SlipRepair.objects.create(
            review=review,
            mode=SlipRepair.Mode.CUSTOM if decisions else SlipRepair.Mode.RECOMMENDED,
            original_legs=plan.original_legs,
            original_combined_odds=plan.original_combined_odds,
            original_success_percent=plan.original_success_percent,
            revised_legs=plan.revised_legs,
            revised_combined_odds=plan.revised_combined_odds,
            revised_success_percent=plan.revised_success_percent,
            changes=[decision.to_dict() for decision in plan.decisions],
        )
        return Response(repair_payload(review, plan, repair), status=status.HTTP_201_CREATED)


class SlipReviewRandomizeView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewRandomizeResponseSerializer

    @extend_schema(
        summary="Build a smart randomized slip ticket",
        description=(
            "Authenticated user endpoint. After a slip review has completed, send the number of games "
            "the user wants in the generated ticket. The backend deterministically returns the strongest "
            "analysed picks from that slip; there are no modes or frontend-side ranking rules."
        ),
        tags=["Slip Reviews"],
        request=SlipReviewRandomizeRequestSerializer,
        responses={200: SlipReviewRandomizeResponseSerializer},
    )
    def post(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.prefetch_related("selections"),
            id=review_id,
            user=request.user,
        )
        if review.status in {
            SlipReview.Status.QUEUED,
            SlipReview.Status.IMPORTING,
            SlipReview.Status.ANALYSING,
        }:
            return Response(
                {
                    "detail": "Slip review is still being analysed.",
                    "status": public_slip_review_status(review.status),
                    "progress": public_slip_review_progress((review.summary or {}).get("progress") or {}),
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = SlipReviewRandomizeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        public_payload = legacy_views._slip_review_payload(review, include_selections=True, public_only=True)
        ticket, error = legacy_views._smart_randomize_ticket(public_payload, serializer.validated_data["games"])
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        token_cost = int(getattr(settings, "SLIP_REVIEW_RANDOMIZE_TOKEN_COST", 5))
        try:
            charge = token_wallet_service.charge_tokens(
                request.user,
                token_cost,
                reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE,
                reference_type="slip_review_randomize",
                reference_id=str(review.id),
                metadata={
                    "review_id": review.id,
                    "requested_games": serializer.validated_data["games"],
                    "source": review.source,
                },
            )
        except InsufficientTokens as exc:
            return Response(
                insufficient_feature_tokens_payload(
                    exc,
                    feature="smart_randomize",
                    token_cost=token_cost,
                    review_id=review.id,
                ),
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        ticket["billing"] = {
            "status": "charged",
            "token_cost": token_cost,
            "transaction_id": charge.transaction.id if charge.transaction else None,
            "wallet": charge.balance_after,
        }
        return Response(legacy_views._api_response_payload(ticket))


class SlipReviewRecapView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewRecapResponseSerializer

    @extend_schema(
        summary="Slip review recap",
        description=(
            "Authenticated user endpoint. Returns settled outcomes for the current user's slip selections over a "
            "recent window, including how many failed selections had been flagged as risky before kickoff. "
            "Selections whose market the settlement engine cannot resolve are reported separately as "
            "`unsettleable` and are excluded from hit rates."
        ),
        tags=["Slip Reviews"],
        parameters=[SlipReviewRecapQuerySerializer],
        responses={200: SlipReviewRecapResponseSerializer},
    )
    def get(self, request):
        query = SlipReviewRecapQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data.get("days") or 1
        return Response(slip_recap_payload(request.user, days=days))


class SlipReviewListView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewListResponseSerializer

    @extend_schema(
        summary="List slip reviews",
        description=(
            "Authenticated user endpoint. Returns compact previous manual/bookmaker slip reviews for the current user: "
            "number of games, status, and each game's user pick vs AI pick."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewListResponseSerializer},
    )
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        view_mode = (request.query_params.get("view") or "compact").strip().lower()
        explicit_include_picks = str(
            request.query_params.get("include_picks", "")
        ).strip().lower() in {"1", "true", "yes"}
        include_picks = view_mode in {"compact", "full", "legacy"} or explicit_include_picks
        try:
            pick_limit = int(request.query_params.get("pick_limit", 2))
        except (TypeError, ValueError):
            pick_limit = 2
        pick_limit = max(0, min(pick_limit, 20))
        if include_picks and pick_limit == 0:
            include_picks = False

        selected_fields = ["id", "source", "status", "title", "submitted_payload", "created_at", "updated_at"]
        use_summary = include_picks and pick_limit is None
        if use_summary:
            selected_fields.append("summary")
        reviews_qs = (
            SlipReview.objects.filter(user=request.user)
            .annotate(selection_count=Count("selections"))
            .only(*selected_fields)
            .order_by("-created_at")
        )
        if include_picks and pick_limit is None:
            reviews_qs = reviews_qs.prefetch_related("selections")
        elif include_picks:
            reviews_qs = reviews_qs.prefetch_related(
                Prefetch(
                    "selections",
                    queryset=SlipSelection.objects.only(
                        "id",
                        "review_id",
                        "order",
                        "submitted_match",
                        "submitted_market",
                        "odds",
                        "status",
                        "verdict",
                        "advisory_score",
                        "analysis_payload",
                    ).order_by("order", "id"),
                    to_attr="preview_selections",
                )
            )
        reviews = list(reviews_qs[:limit])
        return Response(
            {
                "count": len(reviews),
                "reviews": [
                    legacy_views._compact_slip_review_list_payload(
                        review,
                        include_picks=include_picks or pick_limit > 0,
                        pick_limit=pick_limit if pick_limit > 0 else None,
                        use_summary=use_summary,
                    )
                    for review in reviews
                ],
            }
        )


class SlipReviewOptionsView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewOptionsResponseSerializer

    @extend_schema(
        summary="Slip review frontend options",
        description="Authenticated user endpoint. Returns stable market dropdown options, verdict labels, source labels, and request limits for slip review screens.",
        tags=["Slip Reviews"],
        responses={200: SlipReviewOptionsResponseSerializer},
    )
    def get(self, request):
        return Response(
            {
                "markets": legacy_views.SLIP_REVIEW_MARKET_OPTIONS,
                "verdicts": legacy_views.SLIP_REVIEW_VERDICT_OPTIONS,
                "sources": [
                    {"value": "manual", "label": "Manual"},
                    {"value": "sportybet", "label": "SportyBet"},
                    {"value": "betano", "label": "Betano"},
                ],
                "limits": {
                    "manual_max_selections": 30,
                    "search_max_days": 14,
                    "search_default_days": 3,
                    "fixture_search_limit": 25,
                },
            }
        )


class SlipReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        summary="Slip review detail",
        description=(
            "Authenticated user endpoint. Returns one previous slip review. Use `?view=public` for the "
            "frontend-ready bettor response; omit it for the full technical/internal payload."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewDetailResponseSerializer},
    )
    def get(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.prefetch_related("selections"),
            id=review_id,
            user=request.user,
        )
        public_only = str(request.query_params.get("view", "")).lower() == "public"
        return Response(legacy_views._slip_review_payload(review, include_selections=True, public_only=public_only))


class SlipReviewEventsView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewEventsResponseSerializer

    @extend_schema(
        summary="Slip review realtime events",
        description=(
            "Authenticated user endpoint. Returns only slip-review events newer than `after_id`, plus the current "
            "progress snapshot. This is the HTTP fallback/reconnect path for the websocket stream."
        ),
        tags=["Slip Reviews"],
        parameters=[SlipReviewEventsQuerySerializer],
        responses={200: SlipReviewEventsResponseSerializer},
    )
    def get(self, request, review_id):
        query = SlipReviewEventsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        after_id = query.validated_data.get("after_id") or 0
        limit = query.validated_data.get("limit") or 100
        review = get_object_or_404(
            SlipReview.objects.only("id", "status", "summary", "updated_at"),
            id=review_id,
            user=request.user,
        )
        redis_snapshot = slip_review_redis.get_snapshot(review.id) or {}
        redis_events = slip_review_redis.get_events_after(review.id, after_id=after_id, limit=limit)
        redis_knows_review = bool(redis_events) or bool(redis_snapshot)
        if redis_events is not None and redis_knows_review:
            events_payload = [public_slip_review_stream_event(event) for event in redis_events]
            latest_event_id = (redis_snapshot or {}).get("latest_event_id")
            if latest_event_id is None and events_payload:
                latest_event_id = events_payload[-1].get("id")
        else:
            events = list(
                SlipReviewEvent.objects.filter(review=review, id__gt=after_id)
                .order_by("id")[:limit]
            )
            latest_event_id = (
                SlipReviewEvent.objects.filter(review=review).order_by("-id").values_list("id", flat=True).first()
            )
            events_payload = [slip_review_event_payload(event) for event in events]
        payload = {
            "review_id": review.id,
            "status": public_slip_review_status(review.status),
            "progress": public_slip_review_progress(
                (redis_snapshot or {}).get("progress") or (review.summary or {}).get("progress") or {}
            ),
            "latest_event_id": latest_event_id,
            "events": events_payload,
        }
        response = Response(legacy_views._api_response_payload(payload))
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Authorization, Cookie"
        return response


class SlipReviewStreamTokenView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewStreamTokenResponseSerializer

    @extend_schema(
        summary="Create slip review websocket ticket",
        description=(
            "Authenticated user endpoint. Mints a short-lived, review-scoped websocket ticket so the frontend "
            "does not put the real JWT access token in the websocket URL."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewStreamTokenResponseSerializer},
    )
    def post(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.only("id", "user_id"),
            id=review_id,
            user=request.user,
        )
        now = timezone.now()
        expires_at = now + timedelta(seconds=max(60, legacy_views.SLIP_REVIEW_STREAM_TICKET_SECONDS))
        ticket = secrets.token_urlsafe(32)
        SlipReviewStreamToken.objects.filter(expires_at__lt=now).delete()
        SlipReviewStreamToken.objects.create(
            review=review,
            user=request.user,
            token_hash=stream_ticket_hash(ticket),
            expires_at=expires_at,
        )
        ws_path = f"/ws/slip-reviews/{review.id}/?ticket={ticket}"
        scheme = "wss" if request.is_secure() else "ws"
        ws_url = f"{scheme}://{request.get_host()}{ws_path}"
        return Response(
            legacy_views._api_response_payload(
                {
                    "ticket": ticket,
                    "expires_in": max(60, legacy_views.SLIP_REVIEW_STREAM_TICKET_SECONDS),
                    "expires_at": expires_at,
                    "ws_path": ws_path,
                    "ws_url": ws_url,
                }
            )
        )


__all__ = [
    "BetanoSlipImportView",
    "ManualSlipReviewView",
    "SlipRepairView",
    "SlipReviewDetailView",
    "SlipReviewEventsView",
    "SlipReviewListView",
    "SlipReviewOptionsView",
    "SlipReviewRandomizeView",
    "SlipReviewRecapView",
    "SlipReviewStreamTokenView",
    "SportyBetSlipImportView",
]
