"""The daily product: picks, games, top picks and backing.

Extracted from the 11k-line apps/algo/views.py.
"""

# Settlement sits above picks and beside analytics, so neither may import its
# tasks. Dispatching by name keeps the layer order intact — Celery resolves
# the task on the worker.
import csv
import logging

from celery import current_app
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from betpreneur.modules.picks.interface.serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    BackedGamesResponseSerializer,
    BackedPicksQuerySerializer,
    BulkGameBackRequestSerializer,
    BulkGameBackResponseSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    GameAnalysisQuerySerializer,
    GameBackResponseSerializer,
    GameDetailResponseSerializer,
    GameListResponseSerializer,
    PickDetailResponseSerializer,
    PickSerializer,
    ResultsUpdateSerializer,
    SingleGameBackRequestSerializer,
    TaskQueuedSerializer,
    TopPickResponseSerializer,
)
from betpreneur.modules.picks.models import (
    AlgoFixture,
    AlgoRun,
    GameBack,
    MarketPrediction,
    Pick,
)
from betpreneur.modules.picks.services.performance import (
    add_pick,
    confidence_band,
    empty_stats,
    finalize_stats,
    latest_audited_picks,
    odds_band,
)
from betpreneur.modules.picks.services.presentation import (
    EXCLUDED_MARKETS,
    _apply_council_recommendation_gate,
    _back_count,
    _effective_pick_tier,
    _game_market_rank,
    _latest_successful_run,
    _market_verdict_for_game,
    _public_reasoning_text,
    _recent_form_payload,
    _top_pick_sort_key,
    decimal_or_none,
    game_detail_payload,
    game_summary_from_fixture,
    market_prediction_payload,
    normalise_council_review,
    picks_by_match_for_run,
)
from betpreneur.modules.picks.tasks import generate_daily_picks
from betpreneur.modules.pricing.api import (
    market_display_score,
    market_publicly_paused,
    market_sort_value,
)
from betpreneur.modules.pricing.api import tier_for_confidence as _tier_for_confidence
from betpreneur.platform.cache.http import private_cached_response
from betpreneur.platform.config import env_int as _env_int

SETTLE_PICKS_TASK = "betpreneur.modules.settlement.tasks.settle_daily_results"
AUDITOR_TASK = "betpreneur.modules.analytics.tasks.run_monthly_auditor"
SETTLE_SLIPS_TASK = "betpreneur.modules.settlement.tasks.settle_slip_selections"

log = logging.getLogger(__name__)


log = logging.getLogger(__name__)


SLIP_REVIEW_PARALLEL_LEG_THRESHOLD = _env_int("SLIP_REVIEW_PARALLEL_LEG_THRESHOLD", 4)


SLIP_REVIEW_ANALYSIS_WORKERS = max(1, _env_int("SLIP_REVIEW_ANALYSIS_WORKERS", 4))


SLIP_REVIEW_LEG_CACHE_TTL_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_TTL_SECONDS", 15 * 60)


SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS", 5 * 60)


SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS", 45)


PICK_DETAIL_HISTORY_DAYS = 90




















def _pick_identity_key(pick):
    return (
        pick.match_date or pick.run.target_date,
        str(pick.match_id or "").strip(),
        pick.fixture,
        pick.market,
    )


def _fixture_summary_for_pick(pick):
    fixture = AlgoFixture.objects.filter(run=pick.run, match_id=str(pick.match_id or "")).first()
    if fixture:
        markets = [
            market_prediction_payload(prediction)
            for prediction in MarketPrediction.objects.filter(run=pick.run, match_id=str(pick.match_id or ""))
            .select_related("selected_pick")
            .order_by("-confidence", "-ev", "market")
            if prediction.market not in EXCLUDED_MARKETS
        ]
        return {
            "fixture": fixture.fixture,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "home_logo": fixture.home_logo,
            "away_logo": fixture.away_logo,
            "league": fixture.league,
            "league_logo": fixture.league_logo,
            "country": fixture.country,
            "country_flag": fixture.country_flag,
            "round": fixture.round,
            "league_type": fixture.league_type,
            "kickoff": fixture.kickoff,
            "match_id": str(fixture.match_id or ""),
            "home_recent_form": fixture.home_recent_form or {},
            "away_recent_form": fixture.away_recent_form or {},
            "fixture_context": fixture.fixture_context or {},
            "team_news": fixture.team_news or {},
            "corner_profile": fixture.corner_profile or {},
            "insights": fixture.insights or {},
            "source_payload": fixture.source_payload or {},
            "market_count": fixture.market_count,
            "markets_70_plus": fixture.markets_70_plus,
            "markets_65_plus": fixture.markets_65_plus,
            "markets": markets,
        }
    for item in (pick.run.result or {}).get("fixture_summaries", []):
        if str(item.get("match_id")) == str(pick.match_id):
            return item
    return {}




def _fixture_group_sort_key(item):
    return (
        (item.get("country") or "World").lower(),
        (item.get("league") or item.get("competition") or "").lower(),
        item.get("kickoff") or "",
        (item.get("fixture") or "").lower(),
    )


def _group_by_country_and_league(items, item_label):
    countries = {}
    for item in items:
        country = item.get("country") or "World"
        league = item.get("league") or "Other"
        country_bucket = countries.setdefault(country, {})
        country_bucket.setdefault(league, []).append(item)

    grouped = []
    for country in sorted(countries):
        leagues = []
        for league in sorted(countries[country]):
            league_items = sorted(countries[country][league], key=_fixture_group_sort_key)
            leagues.append({
                "league": league,
                "competition": league,
                "count": len(league_items),
                item_label: league_items,
            })
        grouped.append({
            "country": country,
            "count": sum(league["count"] for league in leagues),
            "leagues": leagues,
        })
    return grouped
























def _fixture_summaries_for_run(algo_run):
    fixtures = list(
        AlgoFixture.objects.filter(run=algo_run)
        .order_by("country", "league", "kickoff", "fixture")
    )
    if not fixtures:
        return (algo_run.result or {}).get("fixture_summaries", [])

    markets_by_match = {}
    predictions = (
        MarketPrediction.objects.filter(run=algo_run, eligible=True)
        .select_related("selected_pick")
        .order_by("match_id", "-confidence", "-ev", "market")
    )
    for prediction in predictions:
        if prediction.market in EXCLUDED_MARKETS:
            continue
        markets_by_match.setdefault(str(prediction.match_id or ""), []).append(
            market_prediction_payload(prediction)
        )

    summaries = []
    for fixture in fixtures:
        match_id = str(fixture.match_id or "")
        markets = markets_by_match.get(match_id, [])
        summaries.append({
            "fixture": fixture.fixture,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "home_logo": fixture.home_logo,
            "away_logo": fixture.away_logo,
            "league": fixture.league,
            "league_logo": fixture.league_logo,
            "country": fixture.country,
            "country_flag": fixture.country_flag,
            "round": fixture.round,
            "league_type": fixture.league_type,
            "kickoff": fixture.kickoff,
            "match_id": match_id,
            "home_recent_form": fixture.home_recent_form or {},
            "away_recent_form": fixture.away_recent_form or {},
            "fixture_context": fixture.fixture_context or {},
            "team_news": fixture.team_news or {},
            "corner_profile": fixture.corner_profile or {},
            "insights": fixture.insights or {},
            "source_payload": fixture.source_payload or {},
            "market_count": len(markets),
            "markets_70_plus": sum(1 for market in markets if (market.get("confidence") or 0) >= 70),
            "markets_65_plus": sum(1 for market in markets if (market.get("confidence") or 0) >= 65),
            "markets": markets,
        })
    return summaries




def _bulk_game_back_context(match_ids, request=None):
    match_ids = [str(match_id or "") for match_id in match_ids if str(match_id or "")]
    if not match_ids:
        return {}, set(), {}

    backed_game_counts = dict(
        GameBack.objects.filter(match_id__in=match_ids)
        .values("match_id")
        .annotate(total=Count("id"))
        .values_list("match_id", "total")
    )
    user_backed_game_ids = set()
    user_backed_markets_by_match = {}
    if request and request.user.is_authenticated:
        rows = list(
            GameBack.objects.filter(user=request.user, match_id__in=match_ids)
            .values_list("match_id", "market")
        )
        user_backed_game_ids = {str(match_id or "") for match_id, _market in rows}
        for match_id, market in rows:
            if market:
                user_backed_markets_by_match.setdefault(str(match_id or ""), set()).add(market)
    return backed_game_counts, user_backed_game_ids, user_backed_markets_by_match


def _compact_pick_payload(pick, *, backed_game_counts=None, backed_game_ids=None):
    insights = pick.insights or {}
    council_review = normalise_council_review(
        insights,
        fallback_confidence=pick.confidence,
        fallback_tier=pick.tier,
    )
    match_id = str(pick.match_id or "")
    return {
        "id": pick.id,
        "match_date": pick.match_date,
        "fixture": pick.fixture,
        "home_team": pick.home_team,
        "away_team": pick.away_team,
        "league": pick.league,
        "kickoff": pick.kickoff,
        "match_id": match_id,
        "tier": _effective_pick_tier(pick),
        "market": pick.market,
        "meaning": pick.meaning,
        "confidence": pick.confidence,
        "final_confidence": council_review.get("final_confidence"),
        "odds": float(pick.odds),
        "ev": float(pick.ev),
        "status": pick.status,
        "bettor_view": insights.get("bettor_view") or {},
        "analysis_summary": insights.get("summary", ""),
        "analysis_conclusion": insights.get("conclusion", ""),
        "positive_evidence": insights.get("positive_evidence") or [],
        "risk_evidence": insights.get("risk_evidence") or [],
        "backed_count": int((backed_game_counts or {}).get(match_id, 0) or 0),
        "backed_by_me": match_id in (backed_game_ids or set()),
    }


def _compact_market_payload(market):
    if not market:
        return None
    insights = market.get("insights") or {}
    bettor_view = insights.get("bettor_view") or market.get("bettor_view") or {}
    return {
        "market": market.get("market", ""),
        "meaning": market.get("meaning", ""),
        "confidence": market.get("confidence"),
        "final_confidence": market.get("final_confidence"),
        "odds": market.get("odds"),
        "ev": market.get("ev"),
        "odds_source": market.get("odds_source", ""),
        "eligible": bool(market.get("eligible")),
        "analysis_available": bool(market.get("analysis_available", market.get("eligible"))),
        "data_status": market.get("data_status", "modelled" if market.get("eligible") else "insufficient_data"),
        "recommended": bool(market.get("recommended")),
        "recommendation_status": market.get("recommendation_status", ""),
        "risk_flags": market.get("risk_flags") or [],
        "bettor_view": bettor_view,
        "analysis_summary": market.get("analysis_summary") or bettor_view.get("summary", ""),
        "analysis_conclusion": market.get("analysis_conclusion") or bettor_view.get("conclusion", ""),
        "positive_evidence": market.get("positive_evidence") or bettor_view.get("positive_evidence") or [],
        "risk_evidence": market.get("risk_evidence") or bettor_view.get("risk_evidence") or [],
    }


def _compact_fixture_card(
    fixture,
    *,
    top_markets_by_match,
    recommended_markets_by_match,
    picks_by_match,
    eligible_counts=None,
    backed_game_counts=None,
    backed_game_ids=None,
):
    match_id = str(fixture.match_id or "")
    top_market = top_markets_by_match.get(match_id)
    recommended_market = recommended_markets_by_match.get(match_id)
    match_picks = sorted(picks_by_match.get(match_id, []), key=_top_pick_sort_key, reverse=True)
    pick_data = [
        _compact_pick_payload(
            pick,
            backed_game_counts=backed_game_counts,
            backed_game_ids=backed_game_ids,
        )
        for pick in match_picks
    ]
    return {
        "fixture": fixture.fixture,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "home_logo": fixture.home_logo,
        "away_logo": fixture.away_logo,
        "teams": {
            "home": {"name": fixture.home_team, "logo": fixture.home_logo},
            "away": {"name": fixture.away_team, "logo": fixture.away_logo},
        },
        "league": fixture.league,
        "league_logo": fixture.league_logo,
        "competition_logo": fixture.league_logo,
        "country": fixture.country,
        "country_flag": fixture.country_flag,
        "competition": fixture.league,
        "competition_info": {
            "name": fixture.league,
            "logo": fixture.league_logo,
            "country": fixture.country,
            "country_flag": fixture.country_flag,
        },
        "round": fixture.round,
        "league_type": fixture.league_type,
        "kickoff": fixture.kickoff,
        "match_id": match_id,
        "published": bool(match_picks),
        "official_pick_count": len(match_picks),
        "official_pick": pick_data[0] if pick_data else None,
        "official_picks": pick_data,
        "backed_count": int((backed_game_counts or {}).get(match_id, 0) or 0),
        "backed_by_me": match_id in (backed_game_ids or set()),
        "top_market": _compact_market_payload(top_market),
        "best_market": _compact_market_payload(top_market),
        "recommended_market": _compact_market_payload(recommended_market),
        "recommendation_status": (
            recommended_market.get("recommendation_status")
            if recommended_market
            else (top_market or {}).get("recommendation_status", "no_edge")
        ),
        "market_count": fixture.market_count,
        "eligible_market_count": int((eligible_counts or {}).get(match_id, 0) or 0),
        "markets_70_plus": fixture.markets_70_plus,
        "markets_65_plus": fixture.markets_65_plus,
    }


def _compact_strategy_payload(strategy):
    strategy = strategy or {}
    league_warnings = strategy.get("league_warnings") or []
    suppressed = []
    promoted = []
    cooling = []
    for market, payload in (strategy.get("markets") or {}).items():
        action = (payload or {}).get("action") or (payload or {}).get("state")
        if action == "suppress":
            suppressed.append(market)
        elif action == "promote":
            promoted.append(market)
        elif action == "cool":
            cooling.append(market)
    return {
        "date": strategy.get("date"),
        "daily_policy": strategy.get("daily_policy"),
        "reason": strategy.get("reason", ""),
        "suppressed_market_count": len(suppressed),
        "promoted_market_count": len(promoted),
        "cooling_market_count": len(cooling),
        "league_warning_count": len(league_warnings),
        "suppressed_markets": suppressed[:12],
        "promoted_markets": promoted[:12],
        "cooling_markets": cooling[:12],
        "league_warnings": league_warnings[:12],
        "confidence_bands": strategy.get("confidence_bands") or {},
    }


def _compact_games_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date, prefetch=False)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "summary": {
                "game_count": 0,
                "published_game_count": 0,
                "recommended_game_count": 0,
                "market_count": 0,
                "eligible_market_count": 0,
                "top_pick_count": 0,
            },
            "games": [],
        }

    fixtures = list(
        AlgoFixture.objects.filter(run=algo_run)
        .only(
            "id",
            "run",
            "fixture",
            "home_team",
            "away_team",
            "home_logo",
            "away_logo",
            "league",
            "league_logo",
            "country",
            "country_flag",
            "round",
            "league_type",
            "kickoff",
            "match_id",
            "market_count",
            "markets_70_plus",
            "markets_65_plus",
        )
        .order_by("country", "league", "kickoff", "fixture")
    )
    match_ids = [str(fixture.match_id or "") for fixture in fixtures if str(fixture.match_id or "")]
    backed_game_counts, backed_game_ids, _user_markets = _bulk_game_back_context(match_ids, request)
    picks_by_match = picks_by_match_for_run(algo_run)

    base_predictions = MarketPrediction.objects.filter(run=algo_run, eligible=True).exclude(market__in=EXCLUDED_MARKETS)
    eligible_counts = {
        str(item["match_id"] or ""): item["eligible_count"]
        for item in base_predictions.values("match_id").annotate(eligible_count=Count("id"))
    }
    top_markets_by_match = {}
    top_market_ranks = {}
    recommended_markets_by_match = {}
    recommended_market_ranks = {}
    predictions = base_predictions.select_related("selected_pick").order_by(
        "match_id",
        "-published",
        "-confidence",
        "-ev",
        "market",
    )
    for prediction in predictions.iterator(chunk_size=200):
        prediction_match_id = str(prediction.match_id or "")
        payload = market_prediction_payload(prediction)
        payload["publicly_paused"] = market_publicly_paused(payload.get("market"))
        payload.update(_apply_council_recommendation_gate(payload))
        payload["display_score"] = round(market_display_score(payload)[0], 3)
        if not payload.get("publicly_paused"):
            rank = _game_market_rank(payload)
            if rank > top_market_ranks.get(prediction_match_id, ()):
                top_market_ranks[prediction_match_id] = rank
                top_markets_by_match[prediction_match_id] = payload
        if payload.get("recommended"):
            rank = _game_market_rank(payload)
            if rank > recommended_market_ranks.get(prediction_match_id, ()):
                recommended_market_ranks[prediction_match_id] = rank
                recommended_markets_by_match[prediction_match_id] = payload

    games = [
        _compact_fixture_card(
            fixture,
            top_markets_by_match=top_markets_by_match,
            recommended_markets_by_match=recommended_markets_by_match,
            picks_by_match=picks_by_match,
            eligible_counts=eligible_counts,
            backed_game_counts=backed_game_counts,
            backed_game_ids=backed_game_ids,
        )
        for fixture in fixtures
    ]
    games = [game for game in games if int(game.get("eligible_market_count") or 0) > 0]
    games.sort(
        key=lambda game: (
            (game.get("country") or "World").lower(),
            (game.get("league") or "").lower(),
            game.get("kickoff") or "",
            0 if game.get("published") else 1,
            -((game.get("top_market") or {}).get("final_confidence") or (game.get("top_market") or {}).get("confidence") or 0),
            game.get("fixture") or "",
        ),
    )

    return {
        "date": target_date,
        "published": bool(games),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "game_count": len(games),
            "published_game_count": sum(1 for game in games if game.get("published")),
            "recommended_game_count": sum(1 for game in games if game.get("recommended_market")),
            "market_count": sum(game.get("market_count", 0) for game in games),
            "eligible_market_count": sum(game.get("eligible_market_count", 0) for game in games),
            "top_pick_count": sum(len(items) for items in picks_by_match.values()),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": _compact_strategy_payload((algo_run.result or {}).get("strategy_profile", {})),
        "games": games,
        "grouped_games": _group_by_country_and_league(games, "games"),
    }


def _all_games_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "summary": {
                "game_count": 0,
                "published_game_count": 0,
                "recommended_game_count": 0,
                "market_count": 0,
                "eligible_market_count": 0,
                "top_pick_count": 0,
            },
            "games": [],
        }

    picks_by_match = picks_by_match_for_run(algo_run)
    fixture_summaries = _fixture_summaries_for_run(algo_run)
    match_ids = [str(item.get("match_id") or "") for item in fixture_summaries if str(item.get("match_id") or "")]
    backed_game_counts, backed_game_ids, backed_markets_by_match = _bulk_game_back_context(
        match_ids,
        request,
    )
    games = [
        game_summary_from_fixture(
            item,
            picks_by_match,
            request=request,
            backed_game_counts=backed_game_counts,
            user_backed_game_ids=backed_game_ids,
            user_backed_markets_by_match=backed_markets_by_match,
        )
        for item in fixture_summaries
    ]
    games = [game for game in games if int(game.get("eligible_market_count") or 0) > 0]

    games.sort(
        key=lambda game: (
            (game.get("country") or "World").lower(),
            (game.get("league") or "").lower(),
            game.get("kickoff") or "",
            0 if game.get("published") else 1,
            -((game.get("top_market") or {}).get("final_confidence") or (game.get("top_market") or {}).get("confidence") or 0),
            game.get("fixture") or "",
        ),
    )

    return {
        "date": target_date,
        "published": bool(games),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "game_count": len(games),
            "published_game_count": sum(1 for game in games if game.get("published")),
            "recommended_game_count": sum(1 for game in games if game.get("recommended_market")),
            "market_count": sum(game.get("market_count", 0) for game in games),
            "eligible_market_count": sum(game.get("eligible_market_count", 0) for game in games),
            "top_pick_count": sum(len(items) for items in picks_by_match.values()),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "games": games,
        "grouped_games": _group_by_country_and_league(games, "games"),
    }




def _markets_for_pick_detail(pick, fixture_summary):
    markets = []
    selected_market = None
    for market in fixture_summary.get("markets") or []:
        if market.get("market") in EXCLUDED_MARKETS:
            continue
        payload = dict(market)
        is_selected = payload.get("market") == pick.market
        payload["selected"] = is_selected
        payload["selected_pick_id"] = pick.id if is_selected else payload.get("selected_pick_id")
        payload["selected_tier"] = pick.tier if is_selected else payload.get("selected_tier", "")
        markets.append(payload)
        if is_selected:
            selected_market = payload

    if selected_market is None:
        selected_market = {
            "market": pick.market,
            "meaning": pick.meaning,
            "confidence": pick.confidence,
            "raw_confidence": pick.confidence,
            "final_confidence": normalise_council_review(
                pick.insights,
                fallback_confidence=pick.confidence,
                fallback_tier=_effective_pick_tier(pick),
            ).get("final_confidence"),
            "council_review": normalise_council_review(
                pick.insights,
                fallback_confidence=pick.confidence,
                fallback_tier=_effective_pick_tier(pick),
            ),
            "odds": float(pick.odds),
            "ev": float(pick.ev),
            "odds_source": pick.source,
            "proven": False,
            "eligible": True,
            "risk_flags": pick.risk_flags,
            "selected": True,
            "selected_pick_id": pick.id,
            "selected_tier": pick.tier,
        }
        markets.append(selected_market)

    ranked = sorted(markets, key=market_sort_value, reverse=True)
    alternatives = [market for market in ranked if not market.get("selected")][:10]
    return selected_market, alternatives


def _stats_for_picks(picks):
    stats = empty_stats()
    for pick in picks:
        add_pick(stats, pick)
    return finalize_stats(stats)


def _form_has_games(form):
    return int((form or {}).get("games") or 0) > 0




def _fresh_recent_forms_for_pick(pick, fixture_summary=None):
    fixture_summary = fixture_summary or {}
    home_form = pick.home_recent_form or fixture_summary.get("home_recent_form") or {}
    away_form = pick.away_recent_form or fixture_summary.get("away_recent_form") or {}

    if not _form_has_games(home_form):
        home_form = fixture_summary.get("home_recent_form") or home_form
    if not _form_has_games(away_form):
        away_form = fixture_summary.get("away_recent_form") or away_form

    needs_home = not _form_has_games(home_form)
    needs_away = not _form_has_games(away_form)
    if (needs_home or needs_away) and pick.match_id:
        try:
            from betpreneur.modules.catalog.api import (
                aps_get,
                fetch_team_recent_form,
                recent_form_summary,
            )

            matches = aps_get("/fixtures", {"id": pick.match_id}, timeout=12)
            match = matches[0] if matches else {}
            teams = match.get("teams", {}) or {}
            home_id = (teams.get("home") or {}).get("id")
            away_id = (teams.get("away") or {}).get("id")
            if needs_home and home_id:
                fresh = fetch_team_recent_form(home_id)
                if fresh:
                    home_form = recent_form_summary(fresh)
            if needs_away and away_id:
                fresh = fetch_team_recent_form(away_id)
                if fresh:
                    away_form = recent_form_summary(fresh)
        except Exception as exc:
            log.warning("Could not refresh recent form for pick %s: %s", pick.id, exc)

    update_fields = []
    if _form_has_games(home_form) and home_form != pick.home_recent_form:
        pick.home_recent_form = home_form
        update_fields.append("home_recent_form")
    if _form_has_games(away_form) and away_form != pick.away_recent_form:
        pick.away_recent_form = away_form
        update_fields.append("away_recent_form")
    if update_fields:
        Pick.objects.filter(id=pick.id).update(
            **{field: getattr(pick, field) for field in update_fields}
        )
    return _recent_form_payload(home_form), _recent_form_payload(away_form)


def _pick_performance_slices(pick, days=PICK_DETAIL_HISTORY_DAYS):
    history = [
        item
        for item in latest_audited_picks(days=days)
        if _pick_identity_key(item) != _pick_identity_key(pick)
    ]
    pick_confidence_band = confidence_band(pick.confidence)
    pick_odds_band = odds_band(pick.odds)
    league_market = [
        item for item in history
        if item.league == pick.league and item.market == pick.market
    ]
    return {
        "days": days,
        "overall": _stats_for_picks(history),
        "same_market": _stats_for_picks([item for item in history if item.market == pick.market]),
        "same_league": _stats_for_picks([item for item in history if item.league == pick.league]),
        "same_league_market": _stats_for_picks(league_market),
        "same_tier": _stats_for_picks([item for item in history if item.tier == pick.tier]),
        "same_confidence_band": {
            "label": pick_confidence_band,
            **_stats_for_picks([item for item in history if confidence_band(item.confidence) == pick_confidence_band]),
        },
        "same_odds_band": {
            "label": pick_odds_band,
            **_stats_for_picks([item for item in history if odds_band(item.odds) == pick_odds_band]),
        },
    }


def _pick_detail_payload(pick, request=None):
    fixture_summary = _fixture_summary_for_pick(pick)
    selected_market, alternatives = _markets_for_pick_detail(pick, fixture_summary)
    home_form, away_form = _fresh_recent_forms_for_pick(pick, fixture_summary)
    run_picks = sorted(list(pick.run.picks.all()), key=_top_pick_sort_key, reverse=True)
    rank_on_day = next((index + 1 for index, item in enumerate(run_picks) if item.id == pick.id), None)
    pick_data = PickSerializer(pick, context={"request": request}).data
    effective_tier = _effective_pick_tier(pick)
    selected_market["selected_tier"] = effective_tier

    return {
        "date": pick.match_date or pick.run.target_date,
        "published": True,
        "run_id": pick.run_id,
        "posted_at": pick.created_at,
        "pick": pick_data,
        "fixture": {
            "fixture": pick.fixture,
            "home_team": pick.home_team,
            "away_team": pick.away_team,
            "home_logo": fixture_summary.get("home_logo", ""),
            "away_logo": fixture_summary.get("away_logo", ""),
            "teams": {
                "home": {
                    "name": pick.home_team,
                    "logo": fixture_summary.get("home_logo", ""),
                },
                "away": {
                    "name": pick.away_team,
                    "logo": fixture_summary.get("away_logo", ""),
                },
            },
            "league": pick.league,
            "league_logo": fixture_summary.get("league_logo", ""),
            "competition_logo": fixture_summary.get("league_logo", ""),
            "country": fixture_summary.get("country", ""),
            "country_flag": fixture_summary.get("country_flag", ""),
            "competition": pick.league,
            "competition_info": {
                "name": pick.league,
                "logo": fixture_summary.get("league_logo", ""),
                "country": fixture_summary.get("country", ""),
                "country_flag": fixture_summary.get("country_flag", ""),
            },
            "kickoff": pick.kickoff,
            "match_id": pick.match_id,
            "market_count": fixture_summary.get("market_count", len(fixture_summary.get("markets") or [])),
            "markets_70_plus": fixture_summary.get("markets_70_plus", 0),
            "markets_65_plus": fixture_summary.get("markets_65_plus", 0),
            "home_recent_form": home_form,
            "away_recent_form": away_form,
            "fixture_context": fixture_summary.get("fixture_context", {}),
            "team_news": fixture_summary.get("team_news", {}),
            "insights": fixture_summary.get("insights", {}),
            "corner_profile": fixture_summary.get("corner_profile", {}),
        },
        "market": {
            "selected": selected_market,
            "alternatives": alternatives,
            "eligible_count": sum(1 for market in fixture_summary.get("markets") or [] if market.get("eligible")),
        },
        "selection": {
            "rank_on_day": rank_on_day,
            "total_picks_on_day": len(run_picks),
            "is_top_pick": rank_on_day == 1,
            "tier": effective_tier,
            "selection_profile": pick_data.get("selection_profile", ""),
            "risk_level": pick_data.get("risk_level", ""),
            "confidence_band": confidence_band(pick.confidence),
            "odds_band": odds_band(pick.odds),
            "confidence": pick.confidence,
            "final_confidence": pick_data.get("final_confidence", pick.confidence),
            "council_review": pick_data.get("council_review", {}),
            "odds": float(pick.odds),
            "ev": float(pick.ev),
            "stake": float(pick.stake) if pick.stake is not None else None,
            "status": pick.status,
        },
        "model_summary": {
            "meaning": pick.meaning,
            "reasoning": _public_reasoning_text(pick.reasoning),
            "model_verdict": pick_data.get("model_verdict", pick.model_verdict),
            "risk_flags": pick.risk_flags,
            "insights": pick_data.get("insights", {}),
            "home_recent_form": home_form,
            "away_recent_form": away_form,
        },
        "performance": _pick_performance_slices(pick),
    }


def _daily_picks_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "no_bet": False,
            "message": "Picks have not been published for this date.",
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

    picks = sorted(
        [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
        key=_top_pick_sort_key,
        reverse=True,
    )
    fixture_summaries = {
        str(item.get("match_id")): item
        for item in _fixture_summaries_for_run(algo_run)
    }
    fixture_match_ids = list(dict.fromkeys(
        [match_id for match_id in fixture_summaries if match_id]
        + [str(pick.match_id or "") for pick in picks if str(pick.match_id or "")]
    ))
    backed_game_counts = dict(
        GameBack.objects.filter(match_id__in=fixture_match_ids)
        .values("match_id")
        .annotate(total=Count("id"))
        .values_list("match_id", "total")
    )
    backed_game_ids = set()
    if request and request.user.is_authenticated and fixture_match_ids:
        backed_game_ids = set(
            GameBack.objects.filter(user=request.user, match_id__in=fixture_match_ids)
            .values_list("match_id", flat=True)
        )

    fixtures = {}
    for item in fixture_summaries.values():
        match_id = str(item.get("match_id") or "")
        markets = [
            market
            for market in item.get("markets") or []
            if market.get("market") not in EXCLUDED_MARKETS
        ]
        fixtures[match_id] = {
            "fixture": item.get("fixture", ""),
            "home_team": item.get("home_team", ""),
            "away_team": item.get("away_team", ""),
            "home_logo": item.get("home_logo", ""),
            "away_logo": item.get("away_logo", ""),
            "teams": {
                "home": {
                    "name": item.get("home_team", ""),
                    "logo": item.get("home_logo", ""),
                },
                "away": {
                    "name": item.get("away_team", ""),
                    "logo": item.get("away_logo", ""),
                },
            },
            "league": item.get("league", ""),
            "league_logo": item.get("league_logo", ""),
            "competition_logo": item.get("league_logo", ""),
            "country": item.get("country", ""),
            "country_flag": item.get("country_flag", ""),
            "round": item.get("round", ""),
            "league_type": item.get("league_type", ""),
            "competition": item.get("league", ""),
            "competition_info": {
                "name": item.get("league", ""),
                "logo": item.get("league_logo", ""),
                "country": item.get("country", ""),
                "country_flag": item.get("country_flag", ""),
            },
            "kickoff": item.get("kickoff", ""),
            "match_id": match_id,
            "backed_count": backed_game_counts.get(match_id, 0),
            "backed_by_me": match_id in backed_game_ids,
            "market_count": item.get("market_count", 0),
            "markets_70_plus": item.get("markets_70_plus", 0),
            "markets_65_plus": item.get("markets_65_plus", 0),
            "home_recent_form": _recent_form_payload(item.get("home_recent_form", {})),
            "away_recent_form": _recent_form_payload(item.get("away_recent_form", {})),
            "fixture_context": item.get("fixture_context", {}),
            "team_news": item.get("team_news", {}),
            "insights": item.get("insights", {}),
            "corner_profile": item.get("corner_profile", {}),
            "markets": markets,
            "picks": [],
        }

    for pick in picks:
        key = str(pick.match_id)
        if key not in fixtures:
            fixtures[key] = {
                "fixture": pick.fixture,
                "home_team": pick.home_team,
                "away_team": pick.away_team,
                "home_logo": "",
                "away_logo": "",
                "teams": {
                    "home": {"name": pick.home_team, "logo": ""},
                    "away": {"name": pick.away_team, "logo": ""},
                },
                "league": pick.league,
                "league_logo": "",
                "competition_logo": "",
                "country": "",
                "country_flag": "",
                "round": "",
                "league_type": "",
                "competition": pick.league,
                "competition_info": {
                    "name": pick.league,
                    "logo": "",
                    "country": "",
                    "country_flag": "",
                },
                "kickoff": pick.kickoff,
                "match_id": pick.match_id,
                "backed_count": backed_game_counts.get(str(pick.match_id or ""), 0),
                "backed_by_me": str(pick.match_id or "") in backed_game_ids,
                "market_count": 0,
                "markets_70_plus": 0,
                "markets_65_plus": 0,
                "home_recent_form": _recent_form_payload(pick.home_recent_form),
                "away_recent_form": _recent_form_payload(pick.away_recent_form),
                "fixture_context": {},
                "team_news": {},
                "insights": {},
                "corner_profile": {},
                "markets": [],
                "picks": [],
            }
        fixtures[key]["home_recent_form"] = _recent_form_payload(
            fixtures[key].get("home_recent_form") or pick.home_recent_form
        )
        fixtures[key]["away_recent_form"] = _recent_form_payload(
            fixtures[key].get("away_recent_form") or pick.away_recent_form
        )
        data = PickSerializer(
            pick,
            context={
                "request": request,
                "backed_game_counts": backed_game_counts,
                "backed_game_ids": backed_game_ids,
            },
        ).data
        pick_match_id = str(pick.match_id or "")
        data["backed_by_me"] = pick_match_id in backed_game_ids
        data["backed_count"] = backed_game_counts.get(pick_match_id, 0)
        fixtures[key]["picks"].append(data)
        for market in fixtures[key]["markets"]:
            if market.get("market") == pick.market:
                market["selected"] = True
                market["selected_pick_id"] = pick.id
                market["selected_tier"] = data["tier"]

    for fixture in fixtures.values():
        for market in fixture["markets"]:
            market.setdefault("selected", False)
            market.setdefault("selected_pick_id", None)
            market.setdefault("selected_tier", "")

    published_fixtures = sorted(
        [fixture for fixture in fixtures.values() if fixture["picks"]],
        key=lambda fixture: (
            (fixture.get("country") or "World").lower(),
            (fixture.get("league") or "").lower(),
            fixture.get("kickoff") or "",
            -_top_pick_sort_key(
                next(pick for pick in picks if str(pick.match_id) == str(fixture["match_id"]))
            )[1],
        ),
    )
    top_pick = picks[0] if picks else None

    return {
        "date": target_date,
        "published": bool(picks),
        "no_bet": not bool(picks),
        "message": (
            "Published picks are available."
            if picks
            else "No bet today. The model scored the fixtures but did not find an edge strong enough to publish."
        ),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "fixture_count": len(published_fixtures),
            "market_count": (algo_run.result or {}).get("market_count", 0),
            "selected_pick_count": len(picks),
            "top_pick_id": top_pick.id if top_pick else None,
            "banker_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.BANKER),
            "value_gem_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.VALUE_GEM),
            "wild_card_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.WILD_CARD),
            "picks_70_plus": sum(1 for pick in picks if pick.confidence >= 70),
            "picks_65_plus": sum(1 for pick in picks if pick.confidence >= 65),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "fixtures": published_fixtures,
        "grouped_fixtures": _group_by_country_and_league(published_fixtures, "fixtures"),
    }


def _compact_daily_picks_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date, prefetch=False)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "no_bet": False,
            "message": "Picks have not been published for this date.",
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
            "grouped_fixtures": {},
        }

    picks = sorted(
        list(Pick.objects.filter(run=algo_run).exclude(market__in=EXCLUDED_MARKETS)),
        key=_top_pick_sort_key,
        reverse=True,
    )
    match_ids = [str(pick.match_id or "") for pick in picks if str(pick.match_id or "")]
    backed_game_counts, backed_game_ids, _backed_markets = _bulk_game_back_context(match_ids, request)
    fixture_by_match = {
        str(fixture.match_id or ""): fixture
        for fixture in AlgoFixture.objects.filter(run=algo_run, match_id__in=match_ids).only(
            "fixture",
            "home_team",
            "away_team",
            "home_logo",
            "away_logo",
            "league",
            "league_logo",
            "country",
            "country_flag",
            "round",
            "league_type",
            "kickoff",
            "match_id",
        )
    }

    fixtures = []
    for pick in picks:
        match_id = str(pick.match_id or "")
        fixture = fixture_by_match.get(match_id)
        home_team = fixture.home_team if fixture else pick.home_team
        away_team = fixture.away_team if fixture else pick.away_team
        home_logo = fixture.home_logo if fixture else ""
        away_logo = fixture.away_logo if fixture else ""
        pick_payload = _compact_pick_payload(
            pick,
            backed_game_counts=backed_game_counts,
            backed_game_ids=backed_game_ids,
        )
        fixtures.append({
            "fixture": fixture.fixture if fixture else pick.fixture,
            "home_team": home_team,
            "away_team": away_team,
            "home_logo": home_logo,
            "away_logo": away_logo,
            "teams": {
                "home": {"name": home_team, "logo": home_logo},
                "away": {"name": away_team, "logo": away_logo},
            },
            "league": fixture.league if fixture else pick.league,
            "league_logo": fixture.league_logo if fixture else "",
            "competition_logo": fixture.league_logo if fixture else "",
            "country": fixture.country if fixture else "",
            "country_flag": fixture.country_flag if fixture else "",
            "round": fixture.round if fixture else "",
            "league_type": fixture.league_type if fixture else "",
            "competition": fixture.league if fixture else pick.league,
            "competition_info": {
                "name": fixture.league if fixture else pick.league,
                "logo": fixture.league_logo if fixture else "",
                "country": fixture.country if fixture else "",
                "country_flag": fixture.country_flag if fixture else "",
            },
            "kickoff": fixture.kickoff if fixture else pick.kickoff,
            "match_id": match_id,
            "backed_count": int(backed_game_counts.get(match_id, 0) or 0),
            "backed_by_me": match_id in backed_game_ids,
            "picks": [pick_payload],
        })

    top_pick = picks[0] if picks else None
    return {
        "date": target_date,
        "published": bool(picks),
        "no_bet": not bool(picks),
        "message": (
            "Published picks are available."
            if picks
            else "No bet today. The model scored the fixtures but did not find an edge strong enough to publish."
        ),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "fixture_count": len(fixtures),
            "market_count": (algo_run.result or {}).get("market_count", 0),
            "selected_pick_count": len(picks),
            "top_pick_id": top_pick.id if top_pick else None,
            "banker_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.BANKER),
            "value_gem_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.VALUE_GEM),
            "wild_card_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.WILD_CARD),
            "picks_70_plus": sum(1 for pick in picks if pick.confidence >= 70),
            "picks_65_plus": sum(1 for pick in picks if pick.confidence >= 65),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "fixtures": fixtures,
        "grouped_fixtures": _group_by_country_and_league(fixtures, "fixtures"),
    }


@extend_schema_view(
    list=extend_schema(
        summary="List algo runs",
        description="Internal staff endpoint. Lists algorithm execution records.",
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
        task = current_app.send_task(SETTLE_PICKS_TASK, args=[target_date.isoformat() if target_date else None])
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
        task = current_app.send_task(
            AUDITOR_TASK,
            args=[
                from_date.isoformat() if from_date else None,
                to_date.isoformat() if to_date else None,
            ],
        )
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Auditor run queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class DailyPicksView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        operation_id="algo_picks_list",
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
            return private_cached_response(_compact_daily_picks_payload(target_date, request), request=request)
        return private_cached_response(_daily_picks_payload(target_date, request), request=request)


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
        return private_cached_response(
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


class GamesView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameListResponseSerializer

    @extend_schema(
        operation_id="algo_games_list",
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
            return private_cached_response(_compact_games_payload(target_date, request), request=request)
        return private_cached_response(_all_games_payload(target_date, request), request=request)


class GameDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameDetailResponseSerializer

    @extend_schema(
        operation_id="algo_games_retrieve",
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
        payload = game_detail_payload(target_date, match_id, request)
        if payload["game"] is None:
            return Response(payload, status=status.HTTP_404_NOT_FOUND)
        return private_cached_response(payload, request=request)


class PickDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PickDetailResponseSerializer

    @extend_schema(
        operation_id="algo_picks_retrieve",
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


def _market_snapshot_from_prediction(prediction):
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
    payload["council_review"] = normalise_council_review(
        payload.get("insights"),
        fallback_confidence=payload.get("confidence"),
    )
    payload["final_confidence"] = payload["council_review"].get("final_confidence")
    payload["suggested_tier"] = payload["council_review"].get("tier") or _tier_for_confidence(payload.get("confidence"))
    payload.update(_apply_council_recommendation_gate(payload))
    payload["model_verdict"] = _market_verdict_for_game(payload)
    return payload




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
    game = game_summary_from_fixture(item, picks_by_match_for_run(fixture.run), request=None, include_markets=True)
    markets = game.get("markets") or []
    requested = str(market_name or "").strip()
    if requested:
        return next(
            (market for market in markets if str(market.get("market") or "").strip().lower() == requested.lower()),
            None,
        )
    return game.get("recommended_market") or game.get("best_market") or game.get("top_market")




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
            "odds": decimal_or_none((market_snapshot or {}).get("odds")),
            "confidence": _int_or_none((market_snapshot or {}).get("confidence")),
            "final_confidence": _int_or_none((market_snapshot or {}).get("final_confidence")),
            "ev": decimal_or_none((market_snapshot or {}).get("ev")),
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
        backed.odds = decimal_or_none(market_snapshot.get("odds"))
        backed.confidence = _int_or_none(market_snapshot.get("confidence"))
        backed.final_confidence = _int_or_none(market_snapshot.get("final_confidence"))
        backed.ev = decimal_or_none(market_snapshot.get("ev"))
        backed.market_snapshot = market_snapshot
        update_fields.extend(["meaning", "odds", "confidence", "final_confidence", "ev", "market_snapshot"])
    if update_fields:
        backed.save(update_fields=list(dict.fromkeys(update_fields)))
    return backed, created


def _official_pick_from_back(back, fixture=None, prediction=None):
    snapshot = dict(back.market_snapshot or {}) or _market_snapshot_from_prediction(prediction)
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
    snapshot = dict(back.market_snapshot or {}) or _market_snapshot_from_prediction(prediction)
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
