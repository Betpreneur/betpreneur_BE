"""Game, fixture search, and backed-game API views."""

from decimal import Decimal, InvalidOperation

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.algo.market_data.api import FixtureSearchService
from apps.algo.models import AlgoFixture, GameBack, MarketPrediction
from apps.algo.serializers import (
    BackedGamesResponseSerializer,
    BackedPicksQuerySerializer,
    BulkGameBackRequestSerializer,
    BulkGameBackResponseSerializer,
    FixtureSearchQuerySerializer,
    FixtureSearchResponseSerializer,
    GameAnalysisQuerySerializer,
    GameBackResponseSerializer,
    GameDetailResponseSerializer,
    GameListResponseSerializer,
    SingleGameBackRequestSerializer,
)
from apps.algo.views import (
    _all_games_payload,
    _apply_council_recommendation_gate,
    _compact_games_payload,
    _fixture_summaries_for_run,
    _game_detail_payload,
    _game_summary_from_fixture,
    _market_verdict_for_game,
    _normalise_council_review,
    _picks_by_match,
    _private_cached_response,
    _public_reasoning_text,
    _tier_for_confidence,
)


class GamesView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameListResponseSerializer

    @extend_schema(
        summary="All covered games",
        description="Authenticated user endpoint. Returns every fixture scored for the covered leagues on a matchday, including each game's best available market and any official published pick.",
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        responses={200: GameListResponseSerializer},
    )
    def get(self, request):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        if query.validated_data.get("view") == "compact":
            return _private_cached_response(_compact_games_payload(target_date, request), request=request)
        return _private_cached_response(_all_games_payload(target_date, request), request=request)


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


class GameDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameDetailResponseSerializer

    @extend_schema(
        summary="Game analysis detail",
        description="Authenticated user endpoint. Returns full model context for one scored fixture, including all markets, fixture context, forms, team news, insights, and official picks if published.",
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        responses={200: GameDetailResponseSerializer},
    )
    def get(self, request, match_id):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        payload = _game_detail_payload(target_date, match_id, request)
        if payload["game"] is None:
            return Response(payload, status=status.HTTP_404_NOT_FOUND)
        return _private_cached_response(payload, request=request)


def _latest_fixture_for_match(match_id, target_date=None):
    fixtures = AlgoFixture.objects.select_related("run").filter(match_id=str(match_id))
    if target_date:
        fixtures = fixtures.filter(match_date=target_date)
    return fixtures.order_by("-match_date", "-created_at").first()


def _latest_prediction_for_back(back):
    predictions = MarketPrediction.objects.select_related("run", "selected_pick").filter(match_id=str(back.match_id))
    if back.match_date:
        predictions = predictions.filter(match_date=back.match_date)
    if back.market:
        market_prediction = predictions.filter(market__iexact=back.market).order_by("-created_at").first()
        if market_prediction:
            return market_prediction
    return predictions.order_by("-published", "-eligible", "-confidence", "-ev", "-created_at").first()


def _market_snapshot_from_prediction_local(prediction):
    if not prediction:
        return {}
    payload = {
        "market": prediction.market,
        "meaning": prediction.meaning,
        "raw_confidence": prediction.raw_confidence,
        "confidence": prediction.confidence,
        "odds": float(prediction.odds or 0),
        "ev": float(prediction.ev) if prediction.ev is not None else None,
        "odds_source": prediction.odds_source,
        "odds_meta": prediction.odds_meta or {},
        "eligible": prediction.eligible,
        "risk_flags": prediction.risk_flags or [],
        "insights": prediction.insights or {},
        "selected": bool(prediction.selected_pick_id),
        "selected_pick_id": prediction.selected_pick_id,
        "selected_tier": prediction.selected_pick.tier if prediction.selected_pick else "",
    }
    payload["council_review"] = _normalise_council_review(
        payload.get("insights"),
        fallback_confidence=payload.get("confidence"),
    )
    payload["final_confidence"] = payload["council_review"].get("final_confidence")
    payload["suggested_tier"] = payload["council_review"].get("tier") or _tier_for_confidence(payload.get("confidence"))
    payload.update(_apply_council_recommendation_gate(payload))
    payload["model_verdict"] = _market_verdict_for_game(payload)
    return payload


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _market_snapshot_for_back(fixture, market_name=""):
    if not fixture:
        return None
    summaries = _fixture_summaries_for_run(fixture.run)
    item = next(
        (summary for summary in summaries if str(summary.get("match_id") or "") == str(fixture.match_id)),
        None,
    )
    if not item:
        return None
    game = _game_summary_from_fixture(item, _picks_by_match(fixture.run), request=None, include_markets=True)
    markets = game.get("markets") or []
    requested = str(market_name or "").strip()
    if requested:
        return next(
            (market for market in markets if str(market.get("market") or "").strip().lower() == requested.lower()),
            None,
        )
    return game.get("recommended_market") or game.get("best_market") or game.get("top_market")


def _back_count(match_id, market=""):
    queryset = GameBack.objects.filter(match_id=str(match_id))
    market = str(market or "").strip()
    if market:
        queryset = queryset.filter(market=market)
    return queryset.count()


def _back_game_for_user(user, match_id, target_date=None, market_name=""):
    match_id = str(match_id).strip()
    fixture = _latest_fixture_for_match(match_id, target_date)
    market_snapshot = _market_snapshot_for_back(fixture, market_name)
    if str(market_name or "").strip() and not market_snapshot:
        raise ValueError(f"Market '{market_name}' was not found for match_id {match_id}.")
    market = str((market_snapshot or {}).get("market") or market_name or "").strip()
    backed, created = GameBack.objects.get_or_create(
        user=user,
        match_id=match_id,
        market=market,
        defaults={
            "match_date": fixture.match_date if fixture else target_date,
            "fixture": fixture,
            "meaning": (market_snapshot or {}).get("meaning", ""),
            "odds": _decimal_or_none((market_snapshot or {}).get("odds")),
            "confidence": _int_or_none((market_snapshot or {}).get("confidence")),
            "final_confidence": _int_or_none((market_snapshot or {}).get("final_confidence")),
            "ev": _decimal_or_none((market_snapshot or {}).get("ev")),
            "market_snapshot": market_snapshot or {},
        },
    )
    update_fields = []
    if fixture and (backed.fixture_id != fixture.id or backed.match_date != fixture.match_date):
        backed.fixture = fixture
        backed.match_date = fixture.match_date
        update_fields.extend(["fixture", "match_date"])
    if market_snapshot:
        backed.meaning = market_snapshot.get("meaning", "")
        backed.odds = _decimal_or_none(market_snapshot.get("odds"))
        backed.confidence = _int_or_none(market_snapshot.get("confidence"))
        backed.final_confidence = _int_or_none(market_snapshot.get("final_confidence"))
        backed.ev = _decimal_or_none(market_snapshot.get("ev"))
        backed.market_snapshot = market_snapshot
        update_fields.extend(["meaning", "odds", "confidence", "final_confidence", "ev", "market_snapshot"])
    if update_fields:
        backed.save(update_fields=list(dict.fromkeys(update_fields)))
    return backed, created


def _official_pick_from_back(back, fixture=None, prediction=None):
    snapshot = dict(back.market_snapshot or {}) or _market_snapshot_from_prediction_local(prediction)
    market = back.market or snapshot.get("market", "")
    if not snapshot and not market:
        return None
    return {
        "id": None,
        "match_date": back.match_date or (fixture.match_date if fixture else prediction.match_date if prediction else None),
        "fixture": fixture.fixture if fixture else prediction.fixture if prediction else "",
        "home_team": fixture.home_team if fixture else prediction.home_team if prediction else "",
        "away_team": fixture.away_team if fixture else prediction.away_team if prediction else "",
        "league": fixture.league if fixture else prediction.league if prediction else "",
        "kickoff": fixture.kickoff if fixture else prediction.kickoff if prediction else "",
        "match_id": back.match_id,
        "tier": snapshot.get("selected_tier") or snapshot.get("suggested_tier") or "",
        "market": market,
        "meaning": back.meaning or snapshot.get("meaning", ""),
        "reasoning": _public_reasoning_text(snapshot.get("reasoning", "")),
        "model_verdict": snapshot.get("model_verdict", ""),
        "risk_flags": snapshot.get("risk_flags") or [],
        "confidence": back.confidence if back.confidence is not None else snapshot.get("confidence"),
        "final_confidence": back.final_confidence if back.final_confidence is not None else snapshot.get("final_confidence"),
        "council_review": snapshot.get("council_review") or {},
        "odds": str(back.odds) if back.odds is not None else snapshot.get("odds"),
        "ev": str(back.ev) if back.ev is not None else snapshot.get("ev"),
        "status": snapshot.get("status", ""),
        "backed_by_me": True,
        "backed_count": _back_count(back.match_id, market),
        "source": "backed_market",
    }


def _backed_market_payload(back, fixture=None, prediction=None):
    backed_pick = _official_pick_from_back(back, fixture, prediction) or {}
    snapshot = dict(back.market_snapshot or {}) or _market_snapshot_from_prediction_local(prediction)
    market = back.market or snapshot.get("market", "")
    return {
        **backed_pick,
        "back_id": back.id,
        "match_id": back.match_id,
        "match_date": back.match_date or (fixture.match_date if fixture else prediction.match_date if prediction else None),
        "fixture": fixture.fixture if fixture else prediction.fixture if prediction else backed_pick.get("fixture", ""),
        "home_team": fixture.home_team if fixture else prediction.home_team if prediction else backed_pick.get("home_team", ""),
        "away_team": fixture.away_team if fixture else prediction.away_team if prediction else backed_pick.get("away_team", ""),
        "home_logo": fixture.home_logo if fixture else "",
        "away_logo": fixture.away_logo if fixture else "",
        "league": fixture.league if fixture else prediction.league if prediction else backed_pick.get("league", ""),
        "league_logo": fixture.league_logo if fixture else "",
        "country": fixture.country if fixture else "",
        "country_flag": fixture.country_flag if fixture else "",
        "kickoff": fixture.kickoff if fixture else prediction.kickoff if prediction else backed_pick.get("kickoff", ""),
        "market": market,
        "meaning": back.meaning or backed_pick.get("meaning", ""),
        "odds": str(back.odds) if back.odds is not None else backed_pick.get("odds"),
        "ev": str(back.ev) if back.ev is not None else backed_pick.get("ev"),
        "confidence": back.confidence if back.confidence is not None else backed_pick.get("confidence"),
        "final_confidence": back.final_confidence if back.final_confidence is not None else backed_pick.get("final_confidence"),
        "risk_flags": snapshot.get("risk_flags") or backed_pick.get("risk_flags") or [],
        "reasoning": _public_reasoning_text(snapshot.get("reasoning") or backed_pick.get("reasoning", "")),
        "model_verdict": snapshot.get("model_verdict") or backed_pick.get("model_verdict", ""),
        "council_review": snapshot.get("council_review") or backed_pick.get("council_review") or {},
        "recommendation_status": snapshot.get("recommendation_status", ""),
        "backed": True,
        "backed_by_me": True,
        "backed_market": market,
        "backed_selection": snapshot,
        "backed_count": _back_count(back.match_id, market),
        "market_backed_count": _back_count(back.match_id, market),
        "created_at": back.created_at,
    }


def _backed_games_payload(request, target_date=None):
    backs = GameBack.objects.select_related("fixture", "fixture__run").filter(user=request.user)
    if target_date:
        backs = backs.filter(match_date=target_date)
    backs = backs.order_by("-match_date", "-created_at")

    games = []
    for back in backs:
        fixture = back.fixture or _latest_fixture_for_match(back.match_id, back.match_date)
        prediction = _latest_prediction_for_back(back)
        if not fixture:
            games.append(_backed_market_payload(back, prediction=prediction))
            continue
        games.append(_backed_market_payload(back, fixture, prediction))
    return games


class BackGameView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameBackResponseSerializer

    @extend_schema(
        operation_id="algo_games_backed_single_create",
        summary="Back a game",
        description=(
            "Authenticated user endpoint. Marks that the user backed/saved a game by match_id. "
            "Send an optional market in the body to back a specific market from the game's all-markets list. "
            "If market is omitted, the current recommended/best market is backed."
        ),
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        request=SingleGameBackRequestSerializer,
        responses={200: GameBackResponseSerializer, 201: GameBackResponseSerializer},
        examples=[
            OpenApiExample(
                "Back recommended/best market",
                summary="Back default market",
                description="No body is required. The backend resolves the current recommended market first, then best market.",
                request_only=True,
                value={},
            ),
            OpenApiExample(
                "Back a specific market",
                summary="Back market from all-markets list",
                request_only=True,
                value={"market": "Over 1.5"},
            ),
            OpenApiExample(
                "Back response",
                response_only=True,
                value={
                    "match_id": "1489374",
                    "market": "Over 1.5",
                    "meaning": "2 or more total goals",
                    "backed": True,
                    "created": True,
                    "backed_count": 3,
                },
            ),
        ],
    )
    def post(self, request, match_id):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        serializer = SingleGameBackRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("date") or query.validated_data.get("date")
        market_name = serializer.validated_data.get("market", "")
        try:
            backed, created = _back_game_for_user(request.user, match_id, target_date, market_name)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "match_id": backed.match_id,
                "market": backed.market,
                "meaning": backed.meaning,
                "backed": True,
                "created": created,
                "backed_count": _back_count(backed.match_id, backed.market),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        operation_id="algo_games_backed_single_destroy",
        summary="Remove backed game",
        description="Authenticated user endpoint. Removes the current user's backed marker from one game by match_id. Pass market to remove only one backed market.",
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        request=SingleGameBackRequestSerializer,
        responses={200: GameBackResponseSerializer},
        examples=[
            OpenApiExample(
                "Delete a specific backed market",
                request_only=True,
                value={"market": "Over 1.5"},
            ),
            OpenApiExample(
                "Delete response",
                response_only=True,
                value={
                    "match_id": "1489374",
                    "market": "Over 1.5",
                    "backed": False,
                    "deleted": True,
                    "backed_count": 2,
                },
            ),
        ],
    )
    def delete(self, request, match_id):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        serializer = SingleGameBackRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        market_name = str(serializer.validated_data.get("market") or request.query_params.get("market") or "").strip()
        backs = GameBack.objects.filter(user=request.user, match_id=str(match_id))
        if market_name:
            backs = backs.filter(market=market_name)
        deleted_count, _ = backs.delete()
        return Response(
            {
                "match_id": str(match_id),
                "market": market_name,
                "backed": False,
                "deleted": bool(deleted_count),
                "backed_count": _back_count(str(match_id), market_name),
            },
            status=status.HTTP_200_OK,
        )


class BackedGamesView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BackedGamesResponseSerializer

    @extend_schema(
        operation_id="algo_games_backed_bulk_create",
        summary="Back multiple games",
        description=(
            "Authenticated user endpoint. Marks multiple games/markets as backed. "
            "Use match_ids for default recommended/best markets, or games=[{match_id, market}] for specific markets."
        ),
        tags=["Games"],
        request=BulkGameBackRequestSerializer,
        responses={200: BulkGameBackResponseSerializer, 201: BulkGameBackResponseSerializer},
        examples=[
            OpenApiExample(
                "Back default markets in bulk",
                summary="Legacy/default mode",
                request_only=True,
                value={"match_ids": ["1489374", "1489375"], "date": "2026-06-14"},
            ),
            OpenApiExample(
                "Back specific markets in bulk",
                summary="Market-specific mode",
                request_only=True,
                value={
                    "games": [
                        {"match_id": "1489374", "market": "Over 1.5"},
                        {"match_id": "1489375", "market": "Under 3.5"},
                    ],
                    "date": "2026-06-14",
                },
            ),
            OpenApiExample(
                "Bulk response",
                response_only=True,
                value={
                    "requested_count": 2,
                    "game_count": 2,
                    "created_count": 2,
                    "already_backed_count": 0,
                    "results": [
                        {
                            "match_id": "1489374",
                            "market": "Over 1.5",
                            "meaning": "2 or more total goals",
                            "backed": True,
                            "created": True,
                            "backed_count": 3,
                        }
                    ],
                    "games": [],
                },
            ),
        ],
    )
    def post(self, request):
        serializer = BulkGameBackRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match_ids = serializer.validated_data.get("match_ids") or []
        game_selections = serializer.validated_data.get("games") or []
        target_date = serializer.validated_data.get("date")
        selections = [
            {"match_id": match_id, "market": "", "date": target_date}
            for match_id in match_ids
        ]
        selections.extend(
            {
                "match_id": item["match_id"],
                "market": item.get("market", ""),
                "date": item.get("date") or target_date,
            }
            for item in game_selections
        )

        created_count = 0
        results = []
        for selection in selections:
            match_id = selection["match_id"]
            market_name = selection.get("market", "")
            try:
                backed, created = _back_game_for_user(request.user, match_id, selection.get("date"), market_name)
            except ValueError as exc:
                results.append({
                    "match_id": match_id,
                    "market": market_name,
                    "backed": False,
                    "created": False,
                    "error": str(exc),
                    "backed_count": 0,
                })
                continue
            created_count += 1 if created else 0
            results.append({
                "match_id": backed.match_id,
                "market": backed.market,
                "meaning": backed.meaning,
                "backed": True,
                "created": created,
                "backed_count": _back_count(backed.match_id, backed.market),
            })

        games = _backed_games_payload(request, target_date)
        return Response(
            {
                "requested_count": len(selections),
                "game_count": len(selections),
                "created_count": created_count,
                "already_backed_count": max(0, len([item for item in results if item.get("backed")]) - created_count),
                "results": results,
                "games": games,
            },
            status=status.HTTP_201_CREATED if created_count else status.HTTP_200_OK,
        )

    @extend_schema(
        operation_id="algo_games_backed_list",
        summary="List user backed games",
        description=(
            "Authenticated user endpoint. Returns compact backed-market items for the current user, with optional match date filtering. "
            "Each item is the exact market the user backed, including fixture metadata and the saved market snapshot. "
            "This endpoint does not return the full game analysis or all markets."
        ),
        tags=["Games"],
        parameters=[BackedPicksQuerySerializer],
        responses={200: BackedGamesResponseSerializer},
    )
    def get(self, request):
        query = BackedPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date")
        games = _backed_games_payload(request, target_date)

        return Response({"date": target_date, "count": len(games), "games": games})

    @extend_schema(
        operation_id="algo_games_backed_bulk_destroy",
        summary="Clear user backed games",
        description="Authenticated user endpoint. Deletes all backed-game markers for the current user. Pass date to clear only one matchday.",
        tags=["Games"],
        parameters=[BackedPicksQuerySerializer],
        responses={200: OpenApiTypes.OBJECT},
    )
    def delete(self, request):
        query = BackedPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date")

        backs = GameBack.objects.filter(user=request.user)
        if target_date:
            backs = backs.filter(match_date=target_date)
        deleted_count, _ = backs.delete()
        return Response(
            {
                "date": target_date,
                "deleted_count": deleted_count,
                "message": "Backed games cleared.",
            },
            status=status.HTTP_200_OK,
        )


__all__ = [
    "BackGameView",
    "BackedGamesView",
    "FixtureSearchView",
    "GameDetailView",
    "GamesView",
]
