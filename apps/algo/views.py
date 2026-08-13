from datetime import datetime, timedelta
import csv
import hashlib
import json
import dataclasses
import logging
import math
import os
import time
from decimal import Decimal, InvalidOperation

from celery.result import AsyncResult
from django.conf import settings
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AlgoFixture,
    AlgoRun,
    GameBack,
    MarketPrediction,
    Pick,
    SlipLegAnalysisCache,
    SlipRepair,
    SlipReviewEvent,
    SlipReview,
    SlipSelection,
)
from .market_taxonomy import (
    MarketDescriptor,
    canonical_market_name,
    describe_market,
    market_matches,
    market_options,
    normalize_market_text,
)
from .market_capabilities import market_capability_service
from .recommendation_policy import assess_recommendation
from .services import BetanoBetslipImporter, FixtureSearchService, SportyBetShareImporter, algo_runner_service
from .provider_mapping import provider_mapping_service
from .data.planner import (
    FixtureHydrator,
    capability_for_descriptor,
    model_backed_capability,
    plan_slip_hydration,
)
from .evaluators.registry import COUNT_MODEL_ENGINE, SCORE_MATRIX_ENGINE, evaluator_for
from .explain import service as explanation_service
from .leg_state import assess_leg
from .repair import plan_repair
from .scoring.service import score_model_service
from .statpal_advisory import statpal_market_advisory
from .statpal_snapshots import statpal_snapshot_service
from .ticket_risk import risk_level_for, ticket_risk_service
from .performance import (
    add_pick,
    confidence_band,
    empty_stats,
    finalize_stats,
    latest_audited_picks,
    odds_band,
)
from .serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    BackedPicksQuerySerializer,
    BackedGamesResponseSerializer,
    BetanoSlipImportRequestSerializer,
    BulkGameBackRequestSerializer,
    BulkGameBackResponseSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    GameAnalysisQuerySerializer,
    GameBackResponseSerializer,
    GameDetailResponseSerializer,
    GameListResponseSerializer,
    FixtureSearchQuerySerializer,
    FixtureSearchResponseSerializer,
    ManualSlipReviewRequestSerializer,
    ManualSlipReviewResponseSerializer,
    MarketHealthQuerySerializer,
    MarketHealthResponseSerializer,
    PickSerializer,
    PickDetailResponseSerializer,
    PublicSummarySerializer,
    RecordResponseSerializer,
    RecordQuerySerializer,
    ResultsUpdateSerializer,
    SingleGameBackRequestSerializer,
    SlipReviewDetailResponseSerializer,
    SlipReviewEventsQuerySerializer,
    SlipReviewEventsResponseSerializer,
    SlipReviewListResponseSerializer,
    SlipReviewOptionsResponseSerializer,
    MaintenanceRunRequestSerializer,
    MaintenanceRunResponseSerializer,
    SlipRepairRequestSerializer,
    SlipRepairResponseSerializer,
    SlipReviewRecapQuerySerializer,
    SlipReviewRecapResponseSerializer,
    SportyBetSlipImportRequestSerializer,
    StatPalFixtureContextQuerySerializer,
    StatPalFixtureContextResponseSerializer,
    StatPalFixtureRefreshRequestSerializer,
    StatPalReadinessQuerySerializer,
    StatPalReadinessResponseSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
    TopPickResponseSerializer,
)
from .tasks import generate_daily_picks, import_slip_review, run_monthly_auditor, settle_daily_results


log = logging.getLogger(__name__)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


SLIP_REVIEW_PARALLEL_LEG_THRESHOLD = _env_int("SLIP_REVIEW_PARALLEL_LEG_THRESHOLD", 4)
SLIP_REVIEW_ANALYSIS_WORKERS = max(1, _env_int("SLIP_REVIEW_ANALYSIS_WORKERS", 4))
SLIP_REVIEW_DEEPSEEK_MAX_GAMES = _env_int("SLIP_REVIEW_DEEPSEEK_MAX_GAMES", 5)
SLIP_REVIEW_LEG_CACHE_TTL_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_TTL_SECONDS", 15 * 60)
SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS", 5 * 60)
SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS", 45)
SLIP_REVIEW_STALE_AFTER_SECONDS = _env_int("SLIP_REVIEW_STALE_AFTER_SECONDS", 20 * 60)
SETTLED_PICK_STATUSES = [Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID]
PICK_DETAIL_HISTORY_DAYS = 90
PICK_TIER_RANK = {
    Pick.Tier.BANKER: 3,
    Pick.Tier.VALUE_GEM: 2,
    Pick.Tier.WILD_CARD: 1,
}
EXCLUDED_MARKETS = {"DC: 1X", "DC: X2"}
MATCH_CHECKER_MEMORY_FLAGS = {
    "market_suppressed",
    "strategy_suppressed",
    "market_cooling",
    "strategy_cooling",
    "market_loss_streak",
    "market_recent_losses",
    "limited_market_history",
}
MATCH_CHECKER_SERIOUS_FLAGS = {
    "best_price_far_above_consensus",
    "wide_odds_market",
    "goal_line_boundary",
    "under35_blowout_risk",
    "nordic_under_volatility",
    "draw_boundary_risk",
}
SLIP_REVIEW_MARKET_OPTIONS = market_options()
SLIP_REVIEW_VERDICT_OPTIONS = [
    {"value": "keep", "label": "Keep", "description": "Selection is strong enough to stay on the slip."},
    {"value": "caution", "label": "Caution", "description": "Selection has some support but carries warnings."},
    {"value": "replace", "label": "Replace", "description": "A stronger market exists for the same game."},
    {"value": "remove", "label": "Remove", "description": "Selection does not show enough edge."},
    {"value": "unmatched", "label": "Unmatched", "description": "Fixture could not be confidently matched."},
    {"value": "pending_analysis", "label": "Pending Analysis", "description": "Fixture matched but has not been scored yet."},
]


def _payload_etag(payload):
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f'"{hashlib.sha256(raw).hexdigest()}"'


def _cached_response(
    payload,
    *,
    request=None,
    seconds=None,
    status_code=status.HTTP_200_OK,
    private=True,
):
    ttl = int(seconds if seconds is not None else getattr(settings, "ALGO_READ_CACHE_SECONDS", 300))
    etag = _payload_etag(payload)
    if request is not None and request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = Response(payload, status=status_code)
    response["ETag"] = etag
    visibility = "private" if private else "public"
    response["Cache-Control"] = (
        f"{visibility}, max-age={ttl}, stale-while-revalidate={ttl}, stale-if-error=86400"
    )
    if private:
        response["Vary"] = "Authorization, Cookie"
    return response


def _private_cached_response(payload, *, request=None, seconds=None, status_code=status.HTTP_200_OK):
    return _cached_response(
        payload,
        request=request,
        seconds=seconds,
        status_code=status_code,
        private=True,
    )


def _public_cached_response(payload, *, request=None, seconds=None, status_code=status.HTTP_200_OK):
    return _cached_response(
        payload,
        request=request,
        seconds=seconds,
        status_code=status_code,
        private=False,
    )


def _effective_pick_tier(pick):
    council_tier = (((pick.insights or {}).get("council_review") or {}).get("tier") or "")
    if council_tier in {Pick.Tier.BANKER, Pick.Tier.VALUE_GEM, Pick.Tier.WILD_CARD}:
        return council_tier
    return pick.tier


def _pick_final_confidence(pick):
    return (((pick.insights or {}).get("council_review") or {}).get("final_confidence") or pick.confidence or 0)


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
    stake = sum(float(pick.stake or 0) for pick in picks if pick.status in [Pick.Status.WIN, Pick.Status.LOSS])
    pnl = sum(float(pick.pnl or 0) for pick in picks if pick.status in [Pick.Status.WIN, Pick.Status.LOSS])
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


def _latest_successful_run(target_date):
    return (
        AlgoRun.objects.filter(target_date=target_date, status=AlgoRun.Status.SUCCESS)
        .prefetch_related("picks", "fixtures", "market_predictions")
        .order_by("-created_at")
        .first()
    )


def _top_pick_sort_key(pick):
    return (
        PICK_TIER_RANK.get(_effective_pick_tier(pick), 0),
        _pick_final_confidence(pick),
        float(pick.ev or 0),
        float(pick.odds or 0),
    )


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
            _market_prediction_payload(prediction)
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


def _market_sort_value(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("eligible") else 0,
        market.get("confidence") or 0,
        market.get("ev") if market.get("ev") is not None else -999,
        market.get("odds") or 0,
    )


def _market_reviewer_score(market, reviewer_name):
    review = market.get("council_review") or {}
    for item in review.get("reviewers") or []:
        if item.get("reviewer") == reviewer_name:
            try:
                return float(item.get("score") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _bounded_ev_score(ev):
    if ev is None:
        return -12.0
    try:
        return max(-12.0, min(14.0, float(ev) * 35.0))
    except (TypeError, ValueError):
        return -12.0


def _market_decision_rank(market):
    decision = str((market.get("council_review") or {}).get("decision") or "")
    if market.get("recommended"):
        return 4
    return {
        "approve": 3,
        "caution": 2,
        "not_reviewed": 1,
        "reject": 0,
    }.get(decision, 1)


def _market_display_score(market):
    review = market.get("council_review") or {}
    decision = str(review.get("decision") or "")
    final_confidence = float(review.get("final_confidence") or market.get("final_confidence") or market.get("confidence") or 0)
    raw_confidence = float(market.get("confidence") or 0)
    consensus = float(review.get("consensus_score") or final_confidence)
    disagreement = float(review.get("disagreement_score") or 0)
    market_fit = _market_reviewer_score(market, "market_fit") or consensus
    scoreline_fit = _market_reviewer_score(market, "scoreline_pattern") or consensus
    value_score = _market_reviewer_score(market, "value") or consensus
    ev = market.get("ev")
    ev_score = _bounded_ev_score(ev)
    odds = float(market.get("odds") or 0)
    score = (
        consensus * 0.34
        + market_fit * 0.22
        + scoreline_fit * 0.14
        + final_confidence * 0.18
        + value_score * 0.10
        + ev_score
        - disagreement * 0.45
    )
    risk_flags = set(market.get("risk_flags") or [])
    insights = market.get("insights") or {}
    avoid_reason = str(insights.get("avoid_reason") or "")

    if decision == "approve":
        score += 10.0
    elif decision == "caution":
        score += 3.0
    elif decision == "reject":
        score -= 70.0

    if market.get("market") == "DC: 12":
        if _market_publicly_paused("DC: 12"):
            score -= 80.0
        score -= 8.0
        if "draw_boundary_risk" in risk_flags or "Draw pressure" in avoid_reason:
            score -= 8.0
    if "thin_edge" in risk_flags:
        score -= 3.0
    if "goal_line_boundary" in risk_flags:
        score -= 24.0
    if "under35_blowout_risk" in risk_flags:
        score -= 28.0
    if "nordic_under_volatility" in risk_flags:
        score -= 16.0
    if "market_suppressed" in risk_flags or "strategy_suppressed" in risk_flags:
        score -= 35.0
    if "market_loss_streak" in risk_flags or "market_recent_losses" in risk_flags:
        score -= 22.0
    if "best_price_far_above_consensus" in risk_flags:
        score -= 18.0
    if "wide_odds_market" in risk_flags:
        score -= 12.0
    if "market_cooling" in risk_flags or "strategy_cooling" in risk_flags:
        score -= 3.0
    if "market_recovered" in risk_flags or "strategy_promoted" in risk_flags:
        score += 3.0
    if not market.get("eligible"):
        score -= 12.0
    return score, final_confidence, consensus, market_fit, scoreline_fit, ev_score, raw_confidence, -odds


def _game_market_rank(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("recommended") else 0,
        0 if market.get("publicly_paused") else 1,
        _market_decision_rank(market),
        *_market_display_score(market),
    )


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


def _tier_for_confidence(confidence):
    confidence = confidence or 0
    if confidence >= 80:
        return Pick.Tier.BANKER
    if 70 <= confidence < 80:
        return Pick.Tier.VALUE_GEM
    if 60 <= confidence < 70:
        return Pick.Tier.WILD_CARD
    return "watchlist"


def _normalise_council_review(insights, fallback_confidence=None, fallback_tier=""):
    review = ((insights or {}).get("council_review") or {}).copy()
    if not review:
        return {
            "decision": "not_reviewed",
            "tier": fallback_tier,
            "raw_confidence": fallback_confidence,
            "final_confidence": fallback_confidence,
            "consensus_score": None,
            "disagreement_score": None,
            "reasons": [],
            "reviewers": [],
        }
    return {
        "decision": review.get("decision", ""),
        "tier": review.get("tier", fallback_tier),
        "raw_confidence": review.get("raw_confidence", fallback_confidence),
        "final_confidence": review.get("final_confidence", fallback_confidence),
        "consensus_score": review.get("consensus_score"),
        "disagreement_score": review.get("disagreement_score"),
        "reasons": review.get("reasons", []),
        "reviewers": review.get("reviewers", []),
    }


def _setting_bool(name, default=False):
    value = (getattr(settings, "GRIND_ALGO", {}) or {}).get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _market_publicly_paused(market_name):
    if market_name == "DC: 12":
        return not _setting_bool("ALGO_PUBLISH_DC12", False)
    return False


def _apply_council_recommendation_gate(payload):
    assessment = assess_recommendation(payload)
    review = payload.get("council_review") or {}
    decision = review.get("decision")
    council_tier = review.get("tier") or ""
    council_reasons = [f"council:{reason}" for reason in review.get("reasons") or []]

    if decision == "not_reviewed":
        return assessment

    reasons = list(assessment.get("recommendation_reasons") or [])
    if decision == "reject" or not council_tier:
        reasons.append(f"council_{decision or 'no_tier'}")
        reasons.extend(council_reasons)
        return {
            **assessment,
            "recommended": False,
            "recommendation_status": "watchlist" if assessment.get("recommended") else assessment.get("recommendation_status", "no_edge"),
            "recommendation_reasons": list(dict.fromkeys(reasons)),
        }

    if council_tier == Pick.Tier.WILD_CARD and not _setting_bool("ALGO_PUBLISH_WILD_CARDS", False):
        reasons.append("council_wild_card_disabled")
        reasons.extend(council_reasons)
        return {
            **assessment,
            "recommended": False,
            "recommendation_status": "watchlist",
            "recommendation_reasons": list(dict.fromkeys(reasons)),
        }

    if not assessment.get("recommended"):
        return {
            **assessment,
            "recommendation_reasons": list(dict.fromkeys([*reasons, *council_reasons])),
        }

    status = "strong" if council_tier == Pick.Tier.BANKER else "recommended"
    if decision == "caution":
        status = "watchlist" if council_tier == Pick.Tier.WILD_CARD else "recommended"
    return {
        **assessment,
        "recommended": True,
        "recommendation_status": status,
        "recommendation_reasons": list(dict.fromkeys([*reasons, *council_reasons])),
    }


def _format_game_form_line(label, form):
    form = _recent_form_payload(form)
    games = int(form.get("games") or 0)
    return (
        f"{label}: {form.get('wins', 0)}W-{form.get('draws', 0)}D-{form.get('losses', 0)}L"
        f" in {games}, {form.get('avg_scored', 0)} scored and {form.get('avg_conceded', 0)} conceded per match"
    )


def _market_evidence_for_game(market, item):
    market_name = market.get("market", "")
    home_form = item.get("home_recent_form") or {}
    away_form = item.get("away_recent_form") or {}
    corner_profile = item.get("corner_profile") or {}
    fixture_context = item.get("fixture_context") or {}
    goal_model = fixture_context.get("goal_model") or {}
    expected_total = goal_model.get("expected_total")
    draw_confidence = goal_model.get("draw_confidence")

    if market_name.startswith("Corners "):
        home_corners = corner_profile.get("home") or {}
        away_corners = corner_profile.get("away") or {}
        return (
            f"The corner model projects about {corner_profile.get('expected_total', 'unknown')} total corners. "
            f"{item.get('home_team', 'Home')} average {home_corners.get('avg_for', 'unknown')} corners for and "
            f"{home_corners.get('avg_against', 'unknown')} against; "
            f"{item.get('away_team', 'Away')} average {away_corners.get('avg_for', 'unknown')} for and "
            f"{away_corners.get('avg_against', 'unknown')} against."
        )
    if market_name.startswith("Under"):
        expected_note = f" Expected goals sit around {expected_total}." if expected_total is not None else ""
        return (
            f"The goal profile leans controlled. {_format_game_form_line('Home', home_form)}. "
            f"{_format_game_form_line('Away', away_form)}.{expected_note}"
        )
    if market_name.startswith("Over") or "BTTS" in market_name or market_name.startswith("GG"):
        expected_note = f" Expected goals sit around {expected_total}." if expected_total is not None else ""
        return (
            f"The attacking profile supports goals. {_format_game_form_line('Home', home_form)}. "
            f"{_format_game_form_line('Away', away_form)}.{expected_note}"
        )
    if market_name == "DC: 12":
        draw_note = f" Draw-risk confidence is {draw_confidence}%." if draw_confidence is not None else ""
        return (
            f"This result market needs either team to win, so draw risk is the key threat. "
            f"{_format_game_form_line('Home', home_form)}. {_format_game_form_line('Away', away_form)}.{draw_note}"
        )
    if market_name.endswith("Win") or market_name.startswith("AH ") or market_name.startswith("DNB"):
        return (
            f"The result market is based on recent team balance. {_format_game_form_line('Home', home_form)}. "
            f"{_format_game_form_line('Away', away_form)}."
        )
    return (
        f"Recent team context: {_format_game_form_line('Home', home_form)}. "
        f"{_format_game_form_line('Away', away_form)}."
    )


def _market_reasoning_for_game(market, item):
    ev = market.get("ev")
    ev_text = f"{ev:+.3f} expected value" if ev is not None else "no priced EV"
    odds_source = market.get("odds_source") or "unknown"
    final_confidence = market.get("final_confidence") or market.get("confidence")
    raw_confidence = market.get("confidence")
    confidence_text = (
        f"{final_confidence}% final confidence after council review"
        if final_confidence != raw_confidence
        else f"{raw_confidence}% confidence"
    )
    return (
        f"{market.get('market')} rates at {confidence_text} with "
        f"{market.get('odds')} odds and {ev_text}. "
        f"{_market_evidence_for_game(market, item)} "
        f"Pricing is based on {odds_source} odds."
    )


def _market_verdict_for_game(market):
    risk_flags = set(market.get("risk_flags") or [])
    recommendation_status = market.get("recommendation_status")
    confidence = market.get("final_confidence") or market.get("confidence", 0)
    if recommendation_status == "no_edge":
        return "No bet; this market does not clear the accuracy-first recommendation gate."
    if recommendation_status == "watchlist":
        return "Watchlist only; useful for internal tracking, but not strong enough to recommend."
    if market.get("market") == "DC: 12":
        return "Playable only when the draw risk stays controlled; avoid overusing this market."
    if "thin_edge" in risk_flags or "goal_line_boundary" in risk_flags:
        return "Playable, but the edge is narrow and should be treated cautiously."
    if confidence >= 80:
        return "Strong model candidate from this fixture."
    if confidence >= 70:
        return "Solid model candidate with enough confidence to monitor closely."
    return "Lower-confidence candidate; useful for analysis but not a headline pick."


def _normalise_fixture_markets(item, picks_by_match, request=None):
    markets = []
    match_id = str(item.get("match_id") or "")
    match_picks = picks_by_match.get(str(item.get("match_id") or ""), [])
    pick_by_market = {pick.market: pick for pick in match_picks}
    user_backed_markets = set()
    if request and request.user.is_authenticated and match_id:
        user_backed_markets = set(
            GameBack.objects.filter(user=request.user, match_id=match_id)
            .exclude(market="")
            .values_list("market", flat=True)
        )
    for market in item.get("markets") or []:
        if market.get("market") in EXCLUDED_MARKETS:
            continue
        payload = dict(market)
        payload["council_review"] = _normalise_council_review(
            payload.get("insights"),
            fallback_confidence=payload.get("confidence"),
        )
        payload["final_confidence"] = payload["council_review"].get("final_confidence")
        payload["suggested_tier"] = (
            payload["council_review"].get("tier")
            or _tier_for_confidence(payload.get("confidence"))
        )
        selected_pick = pick_by_market.get(payload.get("market"))
        payload["publicly_paused"] = _market_publicly_paused(payload.get("market"))
        if selected_pick:
            payload["selected"] = True
            payload["selected_pick_id"] = selected_pick.id
            payload["selected_tier"] = _effective_pick_tier(selected_pick)
        else:
            payload.setdefault("selected", False)
            payload.setdefault("selected_pick_id", None)
            payload.setdefault("selected_tier", "")
        payload["market_backed_count"] = _back_count(match_id, payload.get("market")) if match_id else 0
        payload["backed_by_me"] = payload.get("market") in user_backed_markets
        payload.update(_apply_council_recommendation_gate(payload))
        payload["reasoning"] = _market_reasoning_for_game(payload, item)
        payload["model_verdict"] = _market_verdict_for_game(payload)
        payload["display_score"] = round(_market_display_score(payload)[0], 3)
        markets.append(payload)
    return sorted(markets, key=_game_market_rank, reverse=True)


def _game_summary_from_fixture(item, picks_by_match, request=None, include_markets=False):
    match_id = str(item.get("match_id") or "")
    markets = _normalise_fixture_markets(item, picks_by_match, request=request)
    match_picks = sorted(picks_by_match.get(match_id, []), key=_top_pick_sort_key, reverse=True)
    pick_data = PickSerializer(match_picks, many=True, context={"request": request}).data
    top_market = next((market for market in markets if not market.get("publicly_paused")), None)
    if top_market is None:
        top_market = markets[0] if markets else None
    recommended_market = next((market for market in markets if market.get("recommended")), None)
    official_pick = pick_data[0] if pick_data else None
    backed_count = GameBack.objects.filter(match_id=match_id).count() if match_id else 0
    backed_by_me = False
    if request and request.user.is_authenticated and match_id:
        backed_by_me = GameBack.objects.filter(user=request.user, match_id=match_id).exists()

    payload = {
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
        "competition": item.get("league", ""),
        "competition_info": {
            "name": item.get("league", ""),
            "logo": item.get("league_logo", ""),
            "country": item.get("country", ""),
            "country_flag": item.get("country_flag", ""),
        },
        "round": item.get("round", ""),
        "league_type": item.get("league_type", ""),
        "kickoff": item.get("kickoff", ""),
        "match_id": match_id,
        "published": bool(match_picks),
        "official_pick_count": len(match_picks),
        "official_pick": official_pick,
        "official_picks": pick_data,
        "backed_count": backed_count,
        "backed_by_me": backed_by_me,
        "top_market": top_market,
        "best_market": top_market,
        "recommended_market": recommended_market,
        "recommendation_status": (
            recommended_market.get("recommendation_status")
            if recommended_market
            else (top_market or {}).get("recommendation_status", "no_edge")
        ),
        "market_count": item.get("market_count", len(markets)),
        "eligible_market_count": sum(1 for market in markets if market.get("eligible")),
        "markets_70_plus": item.get("markets_70_plus", 0),
        "markets_65_plus": item.get("markets_65_plus", 0),
        "home_recent_form": _recent_form_payload(item.get("home_recent_form", {})),
        "away_recent_form": _recent_form_payload(item.get("away_recent_form", {})),
        "fixture_context": item.get("fixture_context", {}),
        "team_news": item.get("team_news", {}),
        "corner_profile": item.get("corner_profile", {}),
        "insights": item.get("insights", {}),
        "provider_merge": (
            item.get("provider_merge")
            or ((item.get("source_payload") or {}).get("provider_merge") if isinstance(item.get("source_payload"), dict) else {})
            or {}
        ),
    }
    if include_markets:
        payload["markets"] = markets
        payload["model_summary"] = {
            "pre_match_strategy": (item.get("insights") or {}).get("pre_match_strategy", ""),
            "key_signals": (item.get("insights") or {}).get("key_signals", []),
            "risk_warnings": (item.get("insights") or {}).get("risk_warnings", []),
            "top_market": top_market,
            "best_market": top_market,
            "recommended_market": recommended_market,
            "reasoning": (recommended_market or top_market or {}).get("reasoning", ""),
            "model_verdict": (recommended_market or top_market or {}).get("model_verdict", ""),
        }
    return payload


def _picks_by_match(algo_run):
    grouped = {}
    for pick in sorted(
        [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
        key=_top_pick_sort_key,
        reverse=True,
    ):
        grouped.setdefault(str(pick.match_id or ""), []).append(pick)
    return grouped


def _market_prediction_payload(prediction):
    council_review = _normalise_council_review(
        prediction.insights,
        fallback_confidence=prediction.confidence,
        fallback_tier=prediction.selected_pick.tier if prediction.selected_pick_id else "",
    )
    insights = prediction.insights or {}
    return {
        "market": prediction.market,
        "meaning": prediction.meaning,
        "raw_confidence": prediction.raw_confidence,
        "confidence": prediction.confidence,
        "final_confidence": council_review.get("final_confidence"),
        "council_review": council_review,
        "odds": float(prediction.odds or 0),
        "odds_meta": prediction.odds_meta or {},
        "ev": float(prediction.ev) if prediction.ev is not None else None,
        "odds_source": prediction.odds_source,
        "proven": False,
        "eligible": prediction.eligible,
        "risk_flags": prediction.risk_flags or [],
        "bettor_view": insights.get("bettor_view") or {},
        "analysis_summary": insights.get("summary", ""),
        "analysis_conclusion": insights.get("conclusion", ""),
        "positive_evidence": insights.get("positive_evidence") or [],
        "risk_evidence": insights.get("risk_evidence") or [],
        "insights": insights,
        "selected": prediction.published,
        "selected_pick_id": prediction.selected_pick_id,
        "selected_tier": _effective_pick_tier(prediction.selected_pick) if prediction.selected_pick_id else "",
    }


def _fixture_summaries_for_run(algo_run):
    fixtures = list(
        AlgoFixture.objects.filter(run=algo_run)
        .order_by("country", "league", "kickoff", "fixture")
    )
    if not fixtures:
        return (algo_run.result or {}).get("fixture_summaries", [])

    markets_by_match = {}
    predictions = (
        MarketPrediction.objects.filter(run=algo_run)
        .select_related("selected_pick")
        .order_by("match_id", "-confidence", "-ev", "market")
    )
    for prediction in predictions:
        if prediction.market in EXCLUDED_MARKETS:
            continue
        markets_by_match.setdefault(str(prediction.match_id or ""), []).append(
            _market_prediction_payload(prediction)
        )

    summaries = []
    for fixture in fixtures:
        match_id = str(fixture.match_id or "")
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
            "market_count": fixture.market_count,
            "markets_70_plus": fixture.markets_70_plus,
            "markets_65_plus": fixture.markets_65_plus,
            "markets": markets_by_match.get(match_id, []),
        })
    return summaries


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

    picks_by_match = _picks_by_match(algo_run)
    fixture_summaries = _fixture_summaries_for_run(algo_run)
    games = [
        _game_summary_from_fixture(item, picks_by_match, request=request)
        for item in fixture_summaries
    ]

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
            "market_count": (algo_run.result or {}).get("market_count", sum(game.get("market_count", 0) for game in games)),
            "eligible_market_count": sum(game.get("eligible_market_count", 0) for game in games),
            "top_pick_count": sum(len(items) for items in picks_by_match.values()),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "games": games,
        "grouped_games": _group_by_country_and_league(games, "games"),
    }


def _game_detail_payload(target_date, match_id, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "game": None,
        }

    target_match_id = str(match_id)
    picks_by_match = _picks_by_match(algo_run)
    fixture_summary = next(
        (
            item
            for item in _fixture_summaries_for_run(algo_run)
            if str(item.get("match_id") or "") == target_match_id
        ),
        None,
    )
    if not fixture_summary:
        match_picks = picks_by_match.get(target_match_id, [])
        if not match_picks:
            return {
                "date": target_date,
                "published": False,
                "run_id": algo_run.id,
                "posted_at": algo_run.created_at,
                "game": None,
            }
        pick = match_picks[0]
        fixture_summary = {
            "fixture": pick.fixture,
            "home_team": pick.home_team,
            "away_team": pick.away_team,
            "home_logo": "",
            "away_logo": "",
            "teams": {
                "home": {
                    "name": pick.home_team,
                    "logo": "",
                },
                "away": {
                    "name": pick.away_team,
                    "logo": "",
                },
            },
            "league": pick.league,
            "kickoff": pick.kickoff,
            "match_id": pick.match_id,
            "home_recent_form": pick.home_recent_form,
            "away_recent_form": pick.away_recent_form,
            "markets": [],
        }

    game = _game_summary_from_fixture(
        fixture_summary,
        picks_by_match,
        request=request,
        include_markets=True,
    )
    return {
        "date": target_date,
        "published": game.get("published", False),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "game": game,
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
            "final_confidence": _normalise_council_review(
                pick.insights,
                fallback_confidence=pick.confidence,
                fallback_tier=_effective_pick_tier(pick),
            ).get("final_confidence"),
            "council_review": _normalise_council_review(
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

    ranked = sorted(markets, key=_market_sort_value, reverse=True)
    alternatives = [market for market in ranked if not market.get("selected")][:10]
    return selected_market, alternatives


def _stats_for_picks(picks):
    stats = empty_stats()
    for pick in picks:
        add_pick(stats, pick)
    return finalize_stats(stats)


def _form_has_games(form):
    return int((form or {}).get("games") or 0) > 0


def _recent_form_payload(form):
    form = dict(form or {})
    wins = int(form.get("wins") or 0)
    draws = int(form.get("draws") or 0)
    games = int(form.get("games") or 0)
    if "losses" not in form:
        form["losses"] = max(0, games - wins - draws)
    form.setdefault("draws", draws)
    form.setdefault("scope", "overall")
    form.setdefault("form", "")
    return form


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
            from .grindalgo.algo_runner import (
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
            "reasoning": pick.reasoning,
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
                "backed_count": GameBack.objects.filter(match_id=str(pick.match_id or "")).count() if pick.match_id else 0,
                "backed_by_me": (
                    GameBack.objects.filter(user=request.user, match_id=str(pick.match_id or "")).exists()
                    if request and request.user.is_authenticated and pick.match_id
                    else False
                ),
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
        data = PickSerializer(pick, context={"request": request}).data
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
        algo_run = _latest_successful_run(target_date)
        picks = []
        if algo_run:
            picks = sorted(
                [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
                key=_top_pick_sort_key,
                reverse=True,
            )
        picks_data = PickSerializer(picks, many=True, context={"request": request}).data
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


def _normalise_market_name(value):
    return " ".join(str(value or "").strip().lower().split())


def _canonical_market_name(value):
    return canonical_market_name(value)


def _market_matches(requested, actual):
    return market_matches(requested, actual)


def _match_checker_risk_penalty(risk_flags):
    flags = set(risk_flags or [])
    penalty = 0.0
    penalty += len(flags & MATCH_CHECKER_MEMORY_FLAGS) * 3.0
    penalty += len(flags & MATCH_CHECKER_SERIOUS_FLAGS) * 6.0
    penalty += max(0, len(flags) - 8) * 1.25
    return min(penalty, 34.0)


def _match_checker_advisory_score(market):
    review = market.get("council_review") or {}
    final_confidence = _float_or_none(review.get("final_confidence") or market.get("final_confidence") or market.get("confidence")) or 0
    consensus = _float_or_none(review.get("consensus_score")) or final_confidence
    disagreement = _float_or_none(review.get("disagreement_score")) or 0
    market_fit = _market_reviewer_score(market, "market_fit") or consensus
    scoreline_fit = _market_reviewer_score(market, "scoreline_pattern") or consensus
    value_score = _market_reviewer_score(market, "value") or consensus
    ev_score = _bounded_ev_score(market.get("ev"))
    decision = str(review.get("decision") or "")
    odds = _float_or_none(market.get("odds")) or 0

    score = (
        final_confidence * 0.34
        + consensus * 0.20
        + market_fit * 0.17
        + scoreline_fit * 0.17
        + value_score * 0.08
        + ev_score
        - disagreement * 0.22
    )
    if decision == "approve":
        score += 6.0
    elif decision == "caution":
        score += 2.0
    elif decision == "reject":
        score -= 10.0

    status_value = market.get("recommendation_status")
    if market.get("recommended") or status_value in {"strong", "recommended", "playable"}:
        score += 4.0
    elif status_value == "watchlist":
        score += 1.0
    elif status_value == "no_edge":
        score -= 4.0

    if market.get("market") == "DC: 12":
        score -= 5.0
        if _market_publicly_paused("DC: 12"):
            score -= 7.0
    if odds and odds <= 1.06:
        score -= 5.0
    elif odds >= 10:
        score -= 15.0
    elif odds >= 6:
        score -= 8.0

    score -= _match_checker_risk_penalty(market.get("risk_flags") or [])
    return round(max(0, min(100, score)), 1)


def _match_checker_status(score):
    if score is None:
        return "unknown"
    if score >= 78:
        return "strong"
    if score >= 66:
        return "playable"
    if score >= 55:
        return "caution"
    return "avoid"


def _match_checker_warnings(market):
    flags = list(market.get("risk_flags") or [])
    warnings = []
    for flag in flags:
        if flag in MATCH_CHECKER_MEMORY_FLAGS or flag in MATCH_CHECKER_SERIOUS_FLAGS:
            warnings.append(flag)
    for flag in flags:
        if flag not in warnings:
            warnings.append(flag)
        if len(warnings) >= 6:
            break
    return warnings[:6]


def _match_checker_evidence(market):
    insights = market.get("insights") or {}
    league_trust = insights.get("league_trust") or {}
    calibration_trust = insights.get("calibration_trust") or {}
    review = market.get("council_review") or {}
    reviewers = review.get("reviewers") or []
    market_fit = _market_reviewer_score(market, "market_fit")
    scoreline_fit = _market_reviewer_score(market, "scoreline_pattern")
    sample_size = (
        league_trust.get("market_sample")
        or calibration_trust.get("sample")
        or league_trust.get("league_sample")
        or 0
    )
    historical_accuracy = (
        league_trust.get("market_hit_rate")
        or calibration_trust.get("hit_rate")
        or league_trust.get("league_hit_rate")
    )
    similar_market_roi = (
        league_trust.get("market_roi")
        if league_trust.get("market_roi") is not None
        else calibration_trust.get("roi")
    )
    return {
        "historical_accuracy": _float_or_none(historical_accuracy),
        "similar_market_roi": _float_or_none(similar_market_roi),
        "sample_size": int(sample_size or 0),
        "league_trust": league_trust.get("status", ""),
        "confidence_calibration": calibration_trust.get("status", ""),
        "market_fit_score": round(float(market_fit), 1) if market_fit is not None else None,
        "scoreline_fit_score": round(float(scoreline_fit), 1) if scoreline_fit is not None else None,
        "reviewer_count": len(reviewers),
    }


def _match_checker_alternative_reason(submitted_market, alternative):
    alt_market = alternative.get("market") or "the suggested market"
    submitted = submitted_market or "the submitted market"
    evidence = alternative.get("advisory_evidence") or {}
    scoreline_fit = evidence.get("scoreline_fit_score")
    market_fit = evidence.get("market_fit_score")
    confidence = alternative.get("final_confidence") or alternative.get("confidence")
    scope = alternative.get("replacement_scope")
    if scope == "comparable_market":
        return f"{alt_market} is the stronger comparable market for this same selection type."
    if scope == "broad_fallback":
        return f"{alt_market} is a broader fallback because {submitted} is weak or lacks reliable scoring data."
    if scoreline_fit and scoreline_fit >= 70:
        return f"{alt_market} fits the match scoreline pattern better than {submitted}."
    if market_fit and market_fit >= 70:
        return f"{alt_market} has stronger market fit for this fixture than {submitted}."
    if confidence:
        return f"{alt_market} carries stronger match-specific confidence than {submitted}."
    return f"{alt_market} is the safer alternative from this match analysis."


def _with_match_checker_advisory(market):
    if not market:
        return None
    payload = dict(market)
    payload["market_taxonomy"] = payload.get("market_taxonomy") or describe_market(payload.get("market")).to_dict()
    score = _match_checker_advisory_score(payload)
    payload["advisory_score"] = score
    payload["advisory_status"] = _match_checker_status(score)
    payload["advisory_warnings"] = _match_checker_warnings(payload)
    payload["advisory_evidence"] = _match_checker_evidence(payload)
    payload["advisory_basis"] = "match_specific_analysis"
    return payload


def _with_statpal_advisory(market, statpal_advisory):
    if not market:
        return None
    if not statpal_advisory or not statpal_advisory.get("available"):
        return market
    score = _float_or_none(statpal_advisory.get("score"))
    if score is None:
        return market
    payload = dict(market)
    current_score = _float_or_none(payload.get("advisory_score")) or 0
    adjustment = max(-6.0, min(6.0, (score - 55.0) * 0.20))
    adjusted_score = round(max(0, min(100, current_score + adjustment)), 1)
    payload["advisory_score"] = adjusted_score
    payload["advisory_status"] = _match_checker_status(adjusted_score)
    payload["statpal_advisory"] = statpal_advisory
    payload["advisory_basis"] = f"{payload.get('advisory_basis') or 'match_specific_analysis'}+statpal_context"
    warnings = list(payload.get("advisory_warnings") or [])
    warnings.extend(statpal_advisory.get("warnings") or [])
    payload["advisory_warnings"] = list(dict.fromkeys(warnings))[:8]
    evidence = dict(payload.get("advisory_evidence") or {})
    evidence["statpal_score"] = score
    evidence["statpal_adjustment"] = adjustment
    evidence["statpal_basis"] = statpal_advisory.get("basis")
    evidence["statpal"] = statpal_advisory.get("evidence") or {}
    payload["advisory_evidence"] = evidence
    return payload


def _cap_advisory_score(score, market_capability):
    parsed = _float_or_none(score)
    if parsed is None:
        return None
    cap = _float_or_none((market_capability or {}).get("confidence_cap"))
    if cap is None or cap <= 0:
        return round(max(0, min(100, parsed)), 1)
    return round(max(0, min(cap, parsed)), 1)


def _statpal_advisory_scored(statpal_advisory):
    return bool((statpal_advisory or {}).get("available")) and _float_or_none((statpal_advisory or {}).get("score")) is not None


def _effective_market_capability(market_capability, statpal_advisory):
    capability = dict(market_capability or {})
    if not _statpal_advisory_scored(statpal_advisory):
        return capability

    quality = str(capability.get("data_quality") or "").lower()
    cap = _float_or_none(capability.get("confidence_cap"))
    if quality not in {"poor", "unsupported"} and (cap is None or cap > 0):
        return capability

    warnings = [
        warning
        for warning in (capability.get("warnings") or [])
        if warning not in {"no_expected_goals_available", "data_quality_poor", "data_quality_unsupported"}
    ]
    return {
        **capability,
        "support_level": capability.get("support_level") or "medium",
        "data_quality": "medium",
        "confidence_cap": max(cap or 0, 75),
        "scoreable": True,
        "coverage_percent": max(_float_or_none(capability.get("coverage_percent")) or 0, 60.0),
        "warnings": list(dict.fromkeys(warnings)),
        "reason": "Scored by StatPal fallback context after the fitted model lacked enough fixture-specific inputs.",
    }


def _with_market_capability(market, market_capability):
    if not market:
        return None
    payload = dict(market)
    market_capability = _effective_market_capability(market_capability, payload.get("statpal_advisory"))
    payload["market_capability"] = market_capability or {}
    capped_score = _cap_advisory_score(payload.get("advisory_score"), market_capability)
    if capped_score is not None:
        original_score = _float_or_none(payload.get("advisory_score"))
        payload["advisory_score"] = capped_score
        payload["advisory_status"] = _match_checker_status(capped_score)
        evidence = dict(payload.get("advisory_evidence") or {})
        evidence["market_capability"] = market_capability or {}
        if original_score is not None and capped_score < original_score:
            evidence["uncapped_advisory_score"] = original_score
            evidence["cap_applied"] = True
        payload["advisory_evidence"] = evidence
    warnings = list(payload.get("advisory_warnings") or [])
    warnings.extend((market_capability or {}).get("warnings") or [])
    data_quality = (market_capability or {}).get("data_quality")
    if data_quality in {"limited", "poor", "unsupported"}:
        warnings.append(f"data_quality_{data_quality}")
    payload["advisory_warnings"] = list(dict.fromkeys(warnings))[:10]
    return payload


def _submitted_market_payload(
    *,
    requested_market,
    market_taxonomy,
    statpal_advisory,
    market_capability,
):
    market_capability = _effective_market_capability(market_capability, statpal_advisory)
    advisory_score = _cap_advisory_score((statpal_advisory or {}).get("score"), market_capability)
    if advisory_score is None:
        advisory_status = "needs_data"
    else:
        advisory_status = _match_checker_status(advisory_score)
    warnings = list((statpal_advisory or {}).get("warnings") or [])
    warnings.extend((market_capability or {}).get("warnings") or [])
    return {
        "market": requested_market,
        "market_taxonomy": market_taxonomy,
        "market_capability": market_capability or {},
        "confidence": None,
        "final_confidence": None,
        "advisory_score": advisory_score,
        "advisory_status": advisory_status,
        "advisory_basis": (statpal_advisory or {}).get("basis") or "submitted_market_advisory",
        "advisory_warnings": list(dict.fromkeys(warnings))[:10],
        "advisory_evidence": {
            **((statpal_advisory or {}).get("evidence") or {}),
            "market_capability": market_capability or {},
        },
        "statpal_advisory": statpal_advisory or {},
    }


def _generated_market_names_for_family(descriptor):
    family = descriptor.family
    raw_subject = descriptor.subject or descriptor.player or descriptor.raw
    if family in {"corners_total", "team_corners", "corners"}:
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Corners" if descriptor.team == "home" else "Away Team Corners"
            return [f"{prefix} {side.title()} {line}" for line in ("2.5", "3.5", "4.5", "5.5", "6.5") for side in ("over", "under")]
        return [f"Corners {side.title()} {line}" for line in ("7.5", "8.5", "9.5", "10.5", "11.5") for side in ("over", "under")]
    if family in {"cards_total", "team_cards", "cards"}:
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Cards" if descriptor.team == "home" else "Away Team Cards"
            return [f"{prefix} {side.title()} {line}" for line in ("1.5", "2.5", "3.5") for side in ("over", "under")]
        return [f"Cards {side.title()} {line}" for line in ("2.5", "3.5", "4.5", "5.5") for side in ("over", "under")]
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Shots On Target" if descriptor.team == "home" else "Away Team Shots On Target"
            return [f"{prefix} {side.title()} {line}" for line in ("2.5", "3.5", "4.5", "5.5") for side in ("over", "under")]
        return [f"Shots On Target {side.title()} {line}" for line in ("6.5", "7.5", "8.5", "9.5", "10.5", "11.5") for side in ("over", "under")]
    if family == "booking_points":
        return [f"Booking Points {side.title()} {line}" for line in ("35.5", "45.5", "55.5", "65.5") for side in ("over", "under")]
    if family in {"total_goals", "team_total_goals"}:
        if family == "team_total_goals" and descriptor.team in {"home", "away"}:
            prefix = "Home Team" if descriptor.team == "home" else "Away Team"
            return [f"{prefix} {side.title()} {line}" for line in ("1.5", "2.5") for side in ("over", "under")]
        return [f"{side.title()} {line}" for line in ("1.5", "2.5", "3.5", "4.5") for side in ("over", "under")]
    if family in {"result_total_goals", "double_chance_total_goals"}:
        return [
            "Home Win",
            "Draw",
            "Away Win",
            "DC: 1X",
            "DC: X2",
            "DC: 12",
            "Over 1.5",
            "Over 2.5",
            "Under 2.5",
            "Under 3.5",
        ]
    if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
        return [
            "Home Win",
            "Draw",
            "Away Win",
            "DC: 1X",
            "DC: X2",
            "DC: 12",
            "DNB Home",
            "DNB Away",
            "AH Home +0.5",
            "AH Away +0.5",
        ]
    if family == "btts":
        return ["GG / BTTS Yes", "BTTS No"]
    if family.startswith("player_") and raw_subject:
        subject = str(raw_subject)
        for suffix in (" to score", " player to score", " shots", " shot on target", " shots on target", " to be booked", " assist", " saves"):
            normalized = normalize_market_text(subject)
            if normalized.endswith(normalize_market_text(suffix)):
                subject = subject[: -len(suffix)].strip()
                break
        if subject:
            return [
                f"{subject} To Score",
                f"{subject} Shots Over 1.5",
                f"{subject} Shots On Target Over 1.5",
                f"{subject} To Be Booked",
                f"{subject} Assist",
                f"{subject} Saves Over 2.5",
            ]
    return []


def _generated_match_checker_markets(
    selected_descriptor,
    *,
    game,
    statpal_context,
    provider_payload=None,
    statpal_payload=None,
):
    generated = []
    seen = set()
    fixture = {**(game or {}), "statpal_context": statpal_context or {}}
    for market_name in _generated_market_names_for_family(selected_descriptor):
        descriptor = describe_market(market_name)
        if not descriptor.recognized:
            continue
        key = normalize_market_text(descriptor.canonical or market_name)
        if key in seen:
            continue
        seen.add(key)
        capability = capability_for_descriptor(
            descriptor, fixture=fixture, statpal_context=statpal_context
        )
        advisory = statpal_market_advisory.evaluate_market(
            descriptor,
            fixture=fixture,
            provider_payload=provider_payload or {},
            statpal_payload=statpal_payload,
        )
        if not advisory.get("available") or _float_or_none(advisory.get("score")) is None:
            continue
        market = _submitted_market_payload(
            requested_market=descriptor.canonical or market_name,
            market_taxonomy=descriptor.to_dict(),
            statpal_advisory=advisory,
            market_capability=capability,
        )
        market.update(
            {
                "market": descriptor.canonical or market_name,
                "meaning": _public_market_meaning(descriptor.canonical or market_name),
                "confidence": None,
                "final_confidence": None,
                "odds": None,
                "odds_source": "estimated",
                "generated": True,
                "generated_source": "statpal_market_family",
            }
        )
        generated.append(market)
    return generated


def _market_family_group(market):
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    family = taxonomy.get("family") or ""
    if family in {
        "total_goals",
        "team_total_goals",
        "btts",
        "clean_sheet",
        "first_to_score",
        "last_to_score",
        "result_total_goals",
        "double_chance_total_goals",
        "total_btts",
        "result_btts",
        "both_halves_total_goals",
    }:
        return "goals"
    if family in {"corners_total", "team_corners", "corners"}:
        return "corners"
    if family in {"cards_total", "team_cards", "booking_points", "cards"}:
        return "cards"
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        return "shots_on_target"
    if str(family).startswith("player_"):
        return "player"
    if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
        return "result"
    return family or "unknown"


def _replacement_scope(selected_market, candidate):
    selected_group = _market_family_group(selected_market)
    candidate_group = _market_family_group(candidate)
    if selected_group == candidate_group:
        return "comparable_market"
    return "broad_fallback"


def _allows_broad_replacement(selected_market):
    group = _market_family_group(selected_market)
    return group not in {"unknown"}


def _rank_replacement_candidates(candidates):
    return sorted(
        candidates,
        key=lambda market: (
            market.get("advisory_score") or 0,
            market.get("final_confidence") or market.get("confidence") or 0,
            _float_or_none(market.get("ev")) or -1,
        ),
        reverse=True,
    )


def _blocked_slip_recommendation_market(market):
    market_name = (market or {}).get("market") if isinstance(market, dict) else market
    descriptor = describe_market(market_name)
    if not descriptor.recognized:
        return False
    if descriptor.family in {"asian_handicap", "handicap"}:
        return False
    line = _float_or_none(descriptor.line)
    return descriptor.side == "over" and line is not None and abs(line - 0.5) < 0.001


def _replacement_is_meaningfully_better(selected_market, replacement_market):
    if not replacement_market or not selected_market:
        return bool(replacement_market)
    if _market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False

    selected_score = _float_or_none(selected_market.get("advisory_score")) or float(selected_market.get("display_score") or 0)
    replacement_score = _float_or_none(replacement_market.get("advisory_score")) or float(replacement_market.get("display_score") or 0)
    scope = replacement_market.get("replacement_scope") or _replacement_scope(selected_market, replacement_market)
    minimum_score = 58 if scope == "comparable_market" else 60
    minimum_lift = 4 if scope == "comparable_market" else 6
    return replacement_score >= minimum_score and replacement_score >= selected_score + minimum_lift


def _replacement_market_for_slip(
    game,
    selected_market=None,
    generated_markets=None,
    *,
    allow_safer_fallback=False,
    blocked_markets_out=None,
):
    markets = [
        _with_match_checker_advisory(market)
        for market in (game.get("markets") or [])
        if market.get("market") not in EXCLUDED_MARKETS
    ]
    markets.extend(generated_markets or [])
    allowed_markets = []
    blocked_markets = []
    for market in markets:
        if not market:
            continue
        if _blocked_slip_recommendation_market(market):
            blocked_markets.append(market.get("market"))
            continue
        allowed_markets.append(market)
    if blocked_markets_out is not None:
        blocked_markets_out.extend(name for name in dict.fromkeys(blocked_markets) if name)
    markets = allowed_markets
    if selected_market:
        selected_name = selected_market.get("market")
        markets = [market for market in markets if not _market_matches(selected_name, market.get("market"))]
    candidates = [market for market in markets if (market.get("advisory_score") or 0) >= 55]
    if not candidates:
        return None
    if selected_market:
        allowed = []
        for market in candidates:
            scope = _replacement_scope(selected_market, market)
            if scope == "broad_fallback" and (not allow_safer_fallback or not _allows_broad_replacement(selected_market)):
                continue
            market["replacement_scope"] = scope
            if _replacement_is_meaningfully_better(selected_market, market):
                allowed.append(market)
        if not allowed:
            return None
        replacement = _rank_replacement_candidates(allowed)[0]
        if replacement.get("replacement_scope") == "broad_fallback":
            replacement["recommendation_strength"] = "safer_alternative"
        return replacement
    replacement = _rank_replacement_candidates(candidates)[0]
    if selected_market:
        replacement["replacement_scope"] = _replacement_scope(selected_market, replacement)
    return replacement


def _market_is_better_for_slip(selected_market, replacement_market):
    if not replacement_market:
        return False
    if _market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False
    scope = replacement_market.get("replacement_scope") or _replacement_scope(selected_market, replacement_market)
    if scope == "broad_fallback" and not _allows_broad_replacement(selected_market):
        return False
    return _replacement_is_meaningfully_better(selected_market, replacement_market)


def _alternative_is_allowed_for_slip(selected_market, replacement_market):
    if not replacement_market or not _market_was_assessed(replacement_market):
        return False
    scope = replacement_market.get("replacement_scope") or _replacement_scope(selected_market, replacement_market)
    if scope != "broad_fallback":
        return True
    return _allows_broad_replacement(selected_market)


def _reverse_oriented_market(market):
    normalized = _normalise_market_name(market)
    reversed_markets = {
        "home win": "Away Win",
        "away win": "Home Win",
        "dnb home": "DNB Away",
        "dnb away": "DNB Home",
        "ah home +0.5": "AH Away +0.5",
        "ah away +0.5": "AH Home +0.5",
        "home cs": "Away CS",
        "away cs": "Home CS",
        "first to score h": "First to Score A",
        "first to score a": "First to Score H",
    }
    return reversed_markets.get(normalized, market)


def _market_for_fixture_orientation(market, candidate):
    if (candidate or {}).get("match_orientation") == "reversed":
        return _reverse_oriented_market(market)
    return market


def _manual_fixture_game(match_id, match_date, request=None):
    payload = _game_detail_payload(match_date, match_id, request=request)
    game = payload.get("game")
    if game and game.get("markets"):
        return game

    prediction = (
        MarketPrediction.objects.select_related("run", "selected_pick")
        .filter(match_id=str(match_id))
        .order_by("-run__created_at", "-created_at")
        .first()
    )
    if not prediction:
        return None

    algo_run = prediction.run
    predictions = (
        MarketPrediction.objects.filter(run=algo_run, match_id=str(match_id))
        .select_related("selected_pick")
        .order_by("-confidence", "-ev", "market")
    )
    source_payload = (
        AlgoFixture.objects.filter(run=algo_run, match_id=str(match_id))
        .values_list("source_payload", flat=True)
        .first()
        or {}
    )
    markets = [
        _market_prediction_payload(item)
        for item in predictions
        if item.market not in EXCLUDED_MARKETS
    ]
    fixture_summary = {
        "fixture": prediction.fixture,
        "home_team": prediction.home_team,
        "away_team": prediction.away_team,
        "league": prediction.league,
        "kickoff": prediction.kickoff,
        "match_id": prediction.match_id,
        "home_recent_form": prediction.home_recent_form,
        "away_recent_form": prediction.away_recent_form,
        "fixture_context": prediction.fixture_context,
        "team_news": prediction.team_news,
        "markets": markets,
        "source_payload": source_payload,
    }
    return _game_summary_from_fixture(
        fixture_summary,
        _picks_by_match(algo_run),
        request=request,
        include_markets=True,
    )


def _minimal_game_from_candidate(candidate):
    fixture_name = candidate.get("fixture") or " vs ".join(
        item for item in [candidate.get("home_team"), candidate.get("away_team")] if item
    )
    return {
        "fixture": fixture_name,
        "home_team": candidate.get("home_team", ""),
        "away_team": candidate.get("away_team", ""),
        "home_logo": candidate.get("home_logo", ""),
        "away_logo": candidate.get("away_logo", ""),
        "league": candidate.get("league", ""),
        "league_logo": candidate.get("league_logo", ""),
        "country": candidate.get("country", ""),
        "country_flag": candidate.get("country_flag", ""),
        "round": candidate.get("round", ""),
        "kickoff": candidate.get("kickoff", ""),
        "match_id": str(candidate.get("match_id") or ""),
        "match_date": candidate.get("match_date"),
        "statpal_home_team_id": candidate.get("statpal_home_team_id") or "",
        "statpal_away_team_id": candidate.get("statpal_away_team_id") or "",
        "code": candidate.get("code") or candidate.get("league_id") or "",
        "league_id": candidate.get("league_id") or candidate.get("code") or "",
        "hname": candidate.get("hname") or candidate.get("home_team", ""),
        "aname": candidate.get("aname") or candidate.get("away_team", ""),
        "hid": candidate.get("hid") or "",
        "aid": candidate.get("aid") or "",
        "markets": [],
        "market_count": 0,
        "recommendation_status": "no_edge",
        "fixture_context": candidate.get("fixture_context") or {},
        "team_news": candidate.get("team_news") or {},
        "corner_profile": candidate.get("corner_profile") or {},
        "insights": candidate.get("insights") or {},
        "provider_merge": candidate.get("provider_merge") or {},
    }


def _matched_fixture_with_statpal(candidate, game=None, statpal_candidate=None, *, provider_match_id="", provider_competition_id="", home_team_id="", away_team_id=""):
    candidate = candidate or {}
    game = game or {}
    statpal_candidate = statpal_candidate or {}
    return {
        **candidate,
        "match_id": game.get("match_id") or candidate.get("match_id"),
        "fixture": game.get("fixture") or candidate.get("fixture"),
        "home_team": game.get("home_team") or candidate.get("home_team"),
        "away_team": game.get("away_team") or candidate.get("away_team"),
        "league": game.get("league") or candidate.get("league"),
        "country": game.get("country") or candidate.get("country"),
        "kickoff": game.get("kickoff") or candidate.get("kickoff"),
        "statpal_match_id": statpal_candidate.get("match_id") or "",
        "statpal_provider_match_id": provider_match_id or statpal_candidate.get("provider_match_id") or "",
        "statpal_provider_competition_id": provider_competition_id or statpal_candidate.get("provider_competition_id") or "",
        "statpal_home_team_id": home_team_id or statpal_candidate.get("home_team_id") or "",
        "statpal_away_team_id": away_team_id or statpal_candidate.get("away_team_id") or "",
        "statpal_home_team": statpal_candidate.get("home_team") or "",
        "statpal_away_team": statpal_candidate.get("away_team") or "",
        "provider_merge": game.get("provider_merge") or candidate.get("provider_merge") or {},
    }


def _resolved_taxonomy(selection):
    """
    The descriptor the importer already resolved from the bookmaker's market ids.

    Bookmaker imports nest the analysed item under `provider_payload`; the manual path
    puts it at the top level.
    """
    for candidate in (
        selection.get("market_taxonomy"),
        (selection.get("provider_payload") or {}).get("market_taxonomy"),
    ):
        if isinstance(candidate, dict) and candidate.get("family") and candidate.get("recognized"):
            return candidate
    return {}


def _resolved_canonical_market(selection):
    """
    The market identity the importer resolved, carried into the analysis result.

    Without this the public payload reports `resolution: "unresolved"` on every leg,
    because the result is a fresh dict that never inherits what the importer worked out.
    """
    for candidate in (
        selection.get("canonical_market"),
        (selection.get("provider_payload") or {}).get("canonical_market"),
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _descriptor_from_taxonomy(taxonomy):
    """Rebuild a MarketDescriptor from its stored form, tolerating JSON round-tripping."""
    fields = {field.name for field in dataclasses.fields(MarketDescriptor)}
    payload = {key: value for key, value in taxonomy.items() if key in fields}
    payload["data_requirements"] = tuple(payload.get("data_requirements") or ())
    for key in ("raw", "canonical", "code", "family", "category"):
        payload.setdefault(key, "")
    return MarketDescriptor(**payload)


def _selection_market_descriptor(selection, requested_market):
    """
    Use the identity resolved at import time; only parse text when there is none.

    Re-deriving the descriptor from the canonical string is how period markets were
    being lost: the importer resolves market 60 to `match_result / first_half` and
    writes `1H Home Win`, which text parsing then cannot read back. Trusting the stored
    identity removes the re-derivation rather than teaching the parser one more string
    form.
    """
    taxonomy = _resolved_taxonomy(selection)
    if taxonomy:
        try:
            return _descriptor_from_taxonomy(taxonomy)
        except (TypeError, ValueError) as exc:
            log.info("Falling back to text parsing for %r: %s", requested_market, str(exc)[:200])
    return describe_market(
        requested_market,
        market_name=(taxonomy.get("raw") or (selection.get("market_taxonomy") or {}).get("raw") or ""),
    )


def _market_can_skip_core_on_demand(descriptor):
    if descriptor.family in {"team_shots_on_target"}:
        return True
    spec = evaluator_for(descriptor.family)
    if not spec:
        return False
    return spec.engine in {SCORE_MATRIX_ENGINE, COUNT_MODEL_ENGINE} or descriptor.family.startswith("player_")


def _has_statpal_hydration_identity(candidate=None, statpal_candidate=None, provider_metadata=None):
    candidate = candidate or {}
    statpal_candidate = statpal_candidate or {}
    provider_metadata = provider_metadata or {}
    if str(provider_metadata.get("provider") or "").lower() == "statpal":
        if provider_metadata.get("provider_event_id"):
            return True
    if isinstance(statpal_candidate, dict) and (
        statpal_candidate.get("provider_match_id")
        or statpal_candidate.get("statpal_provider_match_id")
        or str(statpal_candidate.get("match_id") or "").startswith("statpal:")
        or statpal_candidate.get("home_team_id")
        or statpal_candidate.get("away_team_id")
        or statpal_candidate.get("statpal_home_team_id")
        or statpal_candidate.get("statpal_away_team_id")
    ):
        return True
    if isinstance(candidate, dict) and (
        candidate.get("provider_match_id")
        or candidate.get("statpal_provider_match_id")
        or str(candidate.get("match_id") or "").startswith("statpal:")
        or candidate.get("statpal_home_team_id")
        or candidate.get("statpal_away_team_id")
    ):
        return True
    return False


def _should_skip_core_on_demand(descriptor, *, game=None, candidate=None, statpal_candidate=None, provider_metadata=None):
    if not _market_can_skip_core_on_demand(descriptor):
        return False
    if game:
        return True
    return _has_statpal_hydration_identity(candidate, statpal_candidate, provider_metadata)


def _consume_review_force_fresh(review_scoring_context):
    if review_scoring_context is None:
        return True
    if review_scoring_context.get("fixture_universe_synced"):
        return False
    review_scoring_context["fixture_universe_synced"] = True
    return True


# A market can be scored by either path: the StatPal/model advisory writes
# `advisory_score`, while the core algo writes `display_score` / confidence. Checking
# only one of them would report every leg from the other path as unassessed.
_ASSESSMENT_SCORE_KEYS = ("advisory_score", "display_score", "final_confidence", "confidence")


def _market_was_assessed(selected_market) -> bool:
    """
    Whether we actually produced a judgement about this market.

    Matters because the verdict branches below default to `no_edge -> remove`, so a
    market nobody evaluated would come out as "avoid" — a judgement we never made.
    """
    market = selected_market or {}
    return any(_float_or_none(market.get(key)) is not None for key in _ASSESSMENT_SCORE_KEYS)


def _manual_verdict(selected_market, replacement_market):
    status_value = selected_market.get("recommendation_status") or "no_edge"
    has_better_market = _market_is_better_for_slip(selected_market, replacement_market)
    has_stat_backed_alternative = _alternative_is_allowed_for_slip(selected_market, replacement_market)
    advisory_score = _float_or_none(selected_market.get("advisory_score")) or 0
    advisory_status = selected_market.get("advisory_status") or _match_checker_status(advisory_score)

    if not _market_was_assessed(selected_market):
        # Absence of evidence is not evidence of a bad pick. Saying "avoid" here is the
        # same failure as scoring an un-analysed leg zero: it reads as a judgement the
        # user could disagree with, when in fact we simply did not evaluate it.
        return {
            "verdict": "not_assessed",
            "message": "We could not assess this selection, so it has not been judged either way.",
            "better_market_available": False,
            "advisory_score": None,
            "advisory_status": "unknown",
        }

    if (selected_market.get("recommended") or selected_market.get("selected")) and not has_better_market:
        verdict = "keep"
        message = "This selection is strong enough to keep."
    elif advisory_status in {"strong", "playable"}:
        verdict = "replace" if has_better_market else ("keep" if advisory_status == "strong" else "caution")
        message = (
            "A stronger market fits this match better."
            if has_better_market
            else "This selection has enough match-specific support, even if it is not a headline pick."
        )
    elif advisory_status == "caution":
        verdict = "replace" if has_better_market else "caution"
        message = (
            "This selection is fragile; the alternative market has better match-specific support."
            if has_better_market
            else "This selection has some support, but the match and league signals require caution."
        )
    elif status_value == "watchlist":
        verdict = "replace" if has_better_market else "caution"
        message = (
            "A stronger market fits this match better."
            if has_better_market
            else "This selection is playable for tracking, but it is not strong enough as a recommended pick."
        )
    elif status_value == "no_edge":
        verdict = "replace" if replacement_market else "caution"
        message = (
            "The selected market does not show enough edge; consider the stronger match-specific alternative."
            if has_better_market
            else "The selected market is high risk; use the statistically backed alternative instead."
            if has_stat_backed_alternative
            else "This selection is high risk, but no stronger backed replacement was found for this game."
        )
    else:
        verdict = "caution"
        message = "This selection has some support, but there are enough warnings to treat it carefully."

    return {
        "verdict": verdict,
        "message": message,
        "better_market_available": has_better_market,
        "advisory_score": advisory_score,
        "advisory_status": advisory_status,
    }


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


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


def _api_response_payload(value):
    return _strip_api_usage(_json_safe(value))


def _empty_api_usage():
    return {
        "provider": "statpal",
        "attempted_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "skipped_by_cache": 0,
        "skipped_without_call": 0,
        "snapshot_types_attempted": [],
        "snapshot_types_refreshed": [],
        "snapshot_types_failed": [],
    }


def _merge_api_usage(*usages):
    total = _empty_api_usage()
    for usage in usages:
        usage = usage or {}
        total["attempted_calls"] += int(usage.get("attempted_calls") or 0)
        total["successful_calls"] += int(usage.get("successful_calls") or 0)
        total["failed_calls"] += int(usage.get("failed_calls") or 0)
        total["skipped_by_cache"] += int(usage.get("skipped_by_cache") or 0)
        total["skipped_without_call"] += int(usage.get("skipped_without_call") or 0)
        for key in ("snapshot_types_attempted", "snapshot_types_refreshed", "snapshot_types_failed"):
            total[key].extend(str(value) for value in usage.get(key) or [] if value)
    for key in ("snapshot_types_attempted", "snapshot_types_refreshed", "snapshot_types_failed"):
        total[key] = list(dict.fromkeys(total[key]))
    return total


def _selection_api_usage(item):
    refresh = item.get("statpal_refresh") or {}
    return refresh.get("api_usage") or _empty_api_usage()


def _slip_api_usage(items):
    usage = _merge_api_usage(*(_selection_api_usage(item) for item in items))
    usage["call_budget_note"] = (
        "Counts only StatPal snapshot refresh calls made during this review. "
        "Cache hits and existing mapped fixtures do not spend StatPal calls."
    )
    return usage


def _float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_percent(value):
    parsed = _float_or_none(value)
    return round(parsed * 100, 1) if parsed is not None else None


def _fair_odds(probability):
    parsed = _float_or_none(probability)
    if parsed is None or parsed <= 0:
        return None
    return round(1 / parsed, 2)


def _implied_probability_from_odds(odds):
    parsed = _float_or_none(odds)
    if parsed is None or parsed <= 1:
        return None
    return 1 / parsed


def _probability_gap(model_probability, market_probability):
    if model_probability is None or market_probability is None:
        return None
    return round((model_probability - market_probability) * 100, 1)


def _gap_level(gap_points):
    gap = abs(_float_or_none(gap_points) or 0)
    if gap >= 15:
        return "high"
    if gap >= 8:
        return "medium"
    return "low"


def _value_rating(model_probability, offered_odds):
    market_probability = _implied_probability_from_odds(offered_odds)
    gap = _probability_gap(model_probability, market_probability)
    if gap is None:
        return "unknown"
    if gap >= 5:
        return "positive_value"
    if gap <= -5:
        return "poor_value"
    return "near_fair"


def _selection_original_odds(item):
    provider_payload = item.get("provider_payload") or {}
    odds = provider_payload.get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("selection") or {}).get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("leg") or {}).get("odds")
    if odds is None:
        odds = (item.get("selected_market") or {}).get("odds")
    return _float_or_none(odds)


def _selection_suggested_odds(item):
    if item.get("verdict") == "replace":
        return _float_or_none((item.get("replacement_market") or {}).get("odds"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return _selection_original_odds(item) or _float_or_none((item.get("selected_market") or {}).get("odds"))


def _combined_odds(values):
    odds = [value for value in values if value and value > 1]
    if not odds:
        return None
    total = 1.0
    for value in odds:
        total *= value
    return round(total, 2)


def _combined_probability(scores):
    probabilities = [
        max(1.0, min(95.0, float(score))) / 100.0
        for score in scores
        if score is not None
    ]
    if not probabilities:
        return None
    total = 1.0
    for probability in probabilities:
        total *= probability
    return round(total * 100, 1)


def _optimized_leg_score(item):
    if item.get("verdict") == "replace":
        return _float_or_none((item.get("replacement_market") or {}).get("advisory_score"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return _float_or_none(item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score"))


def _ticket_health_summary(score, risk_level, remove_count, replace_count, caution_count, unverified_count):
    weak_count = remove_count + replace_count
    if risk_level == "unknown":
        return (
            f"None of these {unverified_count} {_plural(unverified_count, 'pick')} could be analysed yet, "
            "so this ticket has not been assessed."
        )
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {_plural(replace_count, 'pick')} should be replaced")
    if remove_count:
        parts.append(f"{remove_count} {_plural(remove_count, 'pick')} should be avoided")
    if caution_count:
        parts.append(f"{caution_count} {_plural(caution_count, 'pick')} need caution")
    if unverified_count:
        parts.append(f"{unverified_count} {_plural(unverified_count, 'pick')} need review")
    if parts:
        return "This ticket is risky. " + ", ".join(parts) + "."
    if risk_level == "high":
        return f"This ticket is risky. {weak_count or caution_count} pick(s) need attention."
    if risk_level == "medium":
        return f"This ticket is playable, but {replace_count + caution_count} leg(s) need attention."
    return "This ticket looks healthy from the current Match Checker analysis."


def _plural(value, singular, plural=None):
    return singular if int(value or 0) == 1 else (plural or f"{singular}s")


def _ticket_health_label(score):
    score = _float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Good"
    if score >= 45:
        return "Risky"
    if score >= 20:
        return "Poor"
    return "Very Poor"


def _pick_confidence_label(score):
    score = _float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 90:
        return "Exceptional"
    if score >= 80:
        return "Very Strong"
    if score >= 70:
        return "Strong"
    if score >= 60:
        return "Moderate"
    if score >= 50:
        return "Borderline"
    if score >= 40:
        return "Low"
    return "Very Low"


def _risk_level_from_confidence(score):
    score = _float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 70:
        return "medium"
    return "low"


def _bettor_verdict_from_confidence(score):
    score = _float_or_none(score)
    if score is None:
        return "needs_review"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "playable"
    return "high_risk"


def _bettor_verdict_label(code):
    return {
        "strong": "Strong pick",
        "playable": "Playable",
        "high_risk": "High risk",
        "needs_review": "Needs review",
    }.get(str(code or ""), "Needs review")


def _bettor_pick_message(verdict_code, *, market="", action=""):
    market = market or "This pick"
    if action == "replace":
        return f"The available statistics support a stronger option than {market}."
    if verdict_code == "strong":
        return "The available statistics strongly support this selection."
    if verdict_code == "playable":
        return "The available statistics give this selection some support, but it still carries risk."
    if verdict_code == "high_risk":
        return f"The available statistics do not strongly support {market}."
    return "There is not enough reliable data to judge this selection confidently."


def _ticket_issue_text(replace_count=0, remove_count=0, caution_count=0, unverified_count=0):
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {_plural(replace_count, 'pick')} to replace")
    if remove_count:
        parts.append(f"{remove_count} {_plural(remove_count, 'pick')} to avoid")
    if caution_count:
        parts.append(f"{caution_count} {_plural(caution_count, 'pick')} to treat carefully")
    if unverified_count:
        parts.append(f"{unverified_count} {_plural(unverified_count, 'pick')} needing review")
    return ", ".join(parts)


def _public_risk_label(value):
    return {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "unknown": "Unknown",
    }.get(str(value or "").lower(), "Unknown")


def _public_action_label(verdict):
    return {
        "keep": "Play",
        "caution": "Consider",
        "replace": "Replace",
        "remove": "Avoid",
        "expired": "Expired",
        "unmatched": "Needs review",
        "unmatched_market": "Needs review",
        "pending_analysis": "Analysing",
        "not_assessed": "Not assessed",
    }.get(str(verdict or "").lower(), "Review")


def _public_verdict_message(verdict, submitted_market=None, pick_status=None):
    market = submitted_market or "This pick"
    if str(verdict or "").lower() == "caution" and str(pick_status or "").lower() == "avoid":
        return f"{market} has low model support; treat it as high risk unless you accept the downside."
    return {
        "keep": f"{market} is playable from the current analysis.",
        "caution": f"{market} is playable, but it carries extra risk.",
        "replace": f"{market} is too risky compared with the suggested alternative.",
        "remove": f"{market} is too risky compared with safer options for this game.",
        "expired": "This event has already started or ended.",
        "unmatched": "We could not confidently match this fixture.",
        "unmatched_market": "We matched the fixture, but not this market.",
        "pending_analysis": "This fixture is still being analysed.",
        "not_assessed": f"We could not assess {market}, so it has not been judged either way.",
    }.get(str(verdict or "").lower(), "This pick needs review.")


def _public_verdict_object(verdict, submitted_market=None, pick_status=None):
    code = str(verdict or "review").lower()
    return {
        "code": code,
        "label": _public_action_label(code),
        "message": _public_verdict_message(code, submitted_market=submitted_market, pick_status=pick_status),
    }


def _public_market_meaning(market_name):
    descriptor = describe_market(market_name)
    for option in SLIP_REVIEW_MARKET_OPTIONS:
        if _market_matches(market_name, option.get("value")):
            return option.get("meaning") or descriptor.canonical
    if descriptor.family == "unknown":
        return ""
    return descriptor.canonical


def _public_market_pick(market, *, fallback_market="", fallback_odds=None):
    if not market and not fallback_market:
        return None
    odds_source = (market or {}).get("odds_source", "")
    odds_status = "estimated" if str(odds_source).lower() == "estimated" else "verified" if market else ""
    score = _float_or_none((market or {}).get("advisory_score"))
    market_name = (market or {}).get("market") or fallback_market
    return {
        "market": market_name,
        "label": market_name,
        "meaning": (market or {}).get("meaning") or _public_market_meaning(market_name),
        "confidence": (market or {}).get("final_confidence") or (market or {}).get("confidence"),
        "odds": _float_or_none((market or {}).get("odds")) if market else fallback_odds,
        "score": score,
        "decision_score": score,
        "status": _match_checker_status(score),
        "odds_status": odds_status,
    }


def _public_recommendation_strength(pick):
    if not pick:
        return "no_recommendation"
    score = _float_or_none(pick.get("score")) or 0
    if score >= 78:
        return "strong_recommendation"
    if score >= 66:
        return "playable"
    if score >= 55:
        return "safer_alternative"
    if score > 0:
        return "caution"
    return "no_recommendation"


def _public_price_check_from_card(card):
    evidence = card.get("evidence") or {}
    statpal_evidence = evidence.get("statpal") or {}
    odds_value = evidence.get("odds_value") or statpal_evidence.get("odds_value") or {}
    if not odds_value:
        return {
            "available": False,
            "status": "unknown",
            "message": "No StatPal reference price was available for this selection.",
        }

    edge = _float_or_none(odds_value.get("value_edge_pct"))
    offered = _float_or_none(odds_value.get("offered_odds"))
    reference = _float_or_none(odds_value.get("statpal_reference_odds"))
    reference_min = _float_or_none(odds_value.get("statpal_reference_min_odds"))
    reference_max = _float_or_none(odds_value.get("statpal_reference_max_odds"))
    reference_spread = _float_or_none(odds_value.get("statpal_reference_spread_pct"))
    bookmaker_count = _float_or_none(odds_value.get("statpal_reference_bookmaker_count"))
    reliability = odds_value.get("reference_reliability") or ""
    market = odds_value.get("matched_market") or ""
    outcome = odds_value.get("matched_outcome") or ""
    bookmaker = odds_value.get("bookmaker") or ""
    reliability_note = ""
    if reliability == "thin":
        reliability_note = " The reference is based on one bookmaker, so treat it as a light signal."
    elif reliability == "wide":
        reliability_note = " Bookmaker prices disagree, so treat the edge cautiously."
    elif reliability == "volatile":
        reliability_note = " Bookmaker prices disagree sharply, so the edge is unreliable."
    if edge is None:
        status = "matched"
        message = "A StatPal reference price was matched for this selection."
    elif edge >= 5:
        status = "positive_edge"
        message = f"Your price is about {round(edge, 1)}% better than the StatPal reference."
    elif edge <= -5:
        status = "short_price"
        message = f"Your price is about {abs(round(edge, 1))}% shorter than the StatPal reference."
    else:
        status = "near_reference"
        message = "Your price is close to the StatPal reference."
    message = f"{message}{reliability_note}"
    return {
        "available": True,
        "status": status,
        "message": message,
        "offered_odds": offered,
        "reference_odds": reference,
        "reference_min_odds": reference_min,
        "reference_max_odds": reference_max,
        "reference_spread_percent": reference_spread,
        "reference_bookmaker_count": int(bookmaker_count) if bookmaker_count is not None else None,
        "reference_method": odds_value.get("reference_method") or "",
        "reference_reliability": reliability,
        "edge_percent": round(edge, 1) if edge is not None else None,
        "matched_market": market,
        "matched_outcome": outcome,
        "bookmaker": bookmaker,
    }


def _public_why_from_card(card):
    why = []
    codes = []
    evidence = card.get("evidence") or {}
    alternative = card.get("alternative") or {}
    alt_evidence = alternative.get("evidence") or {}
    historical_accuracy = alt_evidence.get("historical_accuracy") or evidence.get("historical_accuracy")
    sample_size = alt_evidence.get("sample_size") or evidence.get("sample_size")
    roi = alt_evidence.get("similar_market_roi") if alt_evidence.get("similar_market_roi") is not None else evidence.get("similar_market_roi")
    league_trust = alt_evidence.get("league_trust") or evidence.get("league_trust")
    if historical_accuracy is not None:
        sample_text = f" across {int(sample_size)} tracked results" if sample_size else ""
        why.append(f"Similar selections won {round(float(historical_accuracy), 1)}%{sample_text}.")
        codes.append("historical_accuracy")
    if sample_size:
        codes.append("historical_sample")
    if roi is not None:
        why.append(f"Similar markets have returned {round(float(roi), 1)}% ROI.")
        codes.append("market_roi")
    if league_trust == "trusted":
        why.append("This market has reliable history in similar league conditions.")
        codes.append("trusted_league_market")
    elif league_trust in {"probation", "restricted"}:
        why.append("There is limited competition-specific history, so some caution remains.")
        codes.append("limited_league_sample")
    price_check = _public_price_check_from_card(card)
    if price_check.get("available"):
        if price_check.get("status") == "positive_edge":
            codes.append("price_edge")
        elif price_check.get("status") == "near_reference":
            codes.append("price_near_reference")
        elif price_check.get("status") == "short_price":
            codes.append("price_short")
        else:
            codes.append("price_reference")
    if alternative.get("reason"):
        why.append(alternative["reason"])
        codes.append("better_alternative")
    statpal_message = (card.get("statpal_advisory") or {}).get("message")
    if statpal_message:
        why.append(statpal_message)
        codes.append("statpal_advisory")
    if not why and card.get("message"):
        why.append(card["message"])
        codes.append("model_message")
    return why[:4], list(dict.fromkeys(codes))[:6]


def _public_selection_risk(verdict, pick):
    score = _float_or_none((pick or {}).get("score"))
    status_value = str((pick or {}).get("status") or "").lower()
    if verdict in {"replace", "remove"}:
        return "high"
    if verdict in {"unmatched", "unmatched_market", "pending_analysis", "expired", "not_assessed"}:
        return "unknown"
    if status_value == "avoid" or (score is not None and score < 55):
        return "high"
    if verdict == "caution" or status_value == "caution" or (score is not None and score < 66):
        return "medium"
    if score is None:
        # No score means no opinion. Reporting "low" here would imply safety we never
        # established, which is a worse error than implying danger.
        return "unknown"
    return "low"


def _selection_has_analysis(item):
    if item.get("status") == "analysed":
        return True
    if item.get("status") == "market_not_found":
        selected_market = item.get("selected_market") or {}
        return bool(item.get("replacement_market")) or _float_or_none(selected_market.get("advisory_score")) is not None
    return False


def _selection_is_unmatched(item):
    return item.get("status") in {"unmatched", "ambiguous_match"}


def _selection_strength_score(item):
    if not _selection_has_analysis(item):
        return None
    market = item.get("selected_market") or {}
    advisory_score = _float_or_none(item.get("advisory_score") or market.get("advisory_score"))
    final_confidence = _float_or_none(market.get("final_confidence") or market.get("confidence")) or 0
    display_score = _float_or_none(market.get("display_score")) or final_confidence
    verdict_bonus = {
        "keep": 12,
        "caution": -4,
        "replace": -18,
        "remove": -35,
    }.get(item.get("verdict"), -20)
    risk_penalty = min(len(market.get("risk_flags") or []) * 2.5, 18)
    base_score = advisory_score if advisory_score is not None else (final_confidence * 0.6 + display_score * 0.25)
    score = base_score + verdict_bonus - risk_penalty
    return round(max(0, min(100, score)), 1)


def _selection_card(item):
    matched = item.get("matched_fixture") or {}
    selected_market = item.get("selected_market") or {}
    replacement_market = item.get("replacement_market") or {}
    action = item.get("verdict")
    leg_score = item.get("selection_score")
    if leg_score is None:
        risk_level = "unknown"
    elif leg_score < 45 or action == "remove":
        risk_level = "high"
    elif leg_score < 65 or action in {"replace", "caution"}:
        risk_level = "medium"
    else:
        risk_level = "low"
    alternative = None
    if replacement_market:
        alternative = {
            "market": replacement_market.get("market"),
            "confidence": replacement_market.get("final_confidence") or replacement_market.get("confidence"),
            "advisory_score": replacement_market.get("advisory_score"),
            "risk_level": (
                "low"
                if (replacement_market.get("advisory_score") or 0) >= 78
                else "medium"
                if (replacement_market.get("advisory_score") or 0) >= 55
                else "high"
            ),
            "odds": _float_or_none(replacement_market.get("odds")),
            "ev": _float_or_none(replacement_market.get("ev")),
            "reason": _match_checker_alternative_reason(item.get("submitted_market"), replacement_market),
            "replacement_scope": replacement_market.get("replacement_scope") or _replacement_scope(selected_market, replacement_market),
            "evidence": replacement_market.get("advisory_evidence") or {},
            "warnings": replacement_market.get("advisory_warnings") or [],
        }
    return {
        "match": item.get("match"),
        "fixture": matched.get("fixture") or item.get("match"),
        "match_id": matched.get("match_id", ""),
        "submitted_market": item.get("submitted_market"),
        "verdict": item.get("verdict"),
        "recommended_action": action,
        "status": item.get("status"),
        "score": item.get("selection_score"),
        "submitted_pick_score": item.get("selection_score"),
        "leg_score": leg_score,
        "risk_level": risk_level,
        "advisory_score": item.get("advisory_score") or selected_market.get("advisory_score"),
        "advisory_status": item.get("advisory_status") or selected_market.get("advisory_status"),
        "advisory_basis": selected_market.get("advisory_basis"),
        "evidence": selected_market.get("advisory_evidence") or {},
        "match_resolution_score": (matched.get("match_score") if matched else None),
        "confidence": selected_market.get("final_confidence") or selected_market.get("confidence"),
        "odds": _selection_original_odds(item),
        "suggested_market": replacement_market.get("market") if item.get("verdict") == "replace" else item.get("submitted_market"),
        "suggested_odds": _selection_suggested_odds(item),
        "suggested_advisory_score": replacement_market.get("advisory_score") if replacement_market else None,
        "suggested_advisory_status": replacement_market.get("advisory_status") if replacement_market else "",
        "alternative": alternative,
        "message": item.get("message", ""),
        "why_risky": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:4],
        "warnings": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:6],
        "statpal_advisory": item.get("statpal_advisory") or selected_market.get("statpal_advisory") or {},
        "statpal_context": item.get("statpal_context") or {},
    }


def _without_remove_recommendation(item):
    if item.get("verdict") != "remove":
        return item
    copy = dict(item)
    selected_market = copy.get("selected_market") or {}
    replacement_market = copy.get("replacement_market") or {}
    if replacement_market and _replacement_is_meaningfully_better(selected_market, replacement_market):
        copy["verdict"] = "replace"
        copy["message"] = (
            copy.get("message")
            or "This selection is high risk; use the statistically backed alternative instead."
        )
    else:
        copy["verdict"] = "caution"
        copy["message"] = (
            copy.get("message")
            or "This selection is high risk, but no stronger backed replacement was found for this game."
        )
    return copy


def _without_blocked_replacement_recommendation(item):
    replacement_market = (item or {}).get("replacement_market") or {}
    if not replacement_market or not _blocked_slip_recommendation_market(replacement_market):
        return item
    copy = dict(item)
    blocked = list(copy.get("blocked_recommendation_markets") or [])
    if replacement_market.get("market"):
        blocked.append(replacement_market.get("market"))
    copy["blocked_recommendation_markets"] = list(dict.fromkeys(blocked))
    copy["replacement_market"] = None
    if copy.get("verdict") == "replace":
        copy["verdict"] = "caution"
        copy["better_market_available"] = False
        copy["message"] = (
            "This selection is risky, but no stronger backed replacement was found for this game."
        )
    return copy


def _public_selection_card(item):
    card = _selection_card(item)
    selected_market = item.get("selected_market") or {}
    replacement_market = item.get("replacement_market") or {}
    if replacement_market and _blocked_slip_recommendation_market(replacement_market):
        replacement_market = {}
    verdict = item.get("verdict")
    ai_pick = None
    if verdict == "replace" and replacement_market:
        ai_pick = _public_market_pick(replacement_market)
    elif verdict in {"keep", "caution"}:
        selected_score = _float_or_none(selected_market.get("advisory_score"))
        if selected_score is not None and selected_score >= 55:
            ai_pick = _public_market_pick(selected_market, fallback_market=item.get("submitted_market"), fallback_odds=_selection_original_odds(item))
    if ai_pick:
        ai_pick["recommendation_strength"] = _public_recommendation_strength(ai_pick)
        if verdict == "replace" and replacement_market:
            ai_pick["replacement_scope"] = replacement_market.get("replacement_scope") or _replacement_scope(selected_market, replacement_market)
    if verdict != "replace":
        card = {**card, "alternative": None}
    why, reason_codes = _public_why_from_card(card)
    price_check = _public_price_check_from_card(card)
    your_pick = {
        "market": item.get("submitted_market"),
        "label": item.get("submitted_market"),
        "meaning": _public_market_meaning(item.get("submitted_market")),
        "confidence": card.get("confidence"),
        "odds": card.get("odds"),
        "score": card.get("advisory_score"),
        "decision_score": card.get("advisory_score"),
        "status": card.get("advisory_status") or _match_checker_status(_float_or_none(card.get("advisory_score"))),
    }
    capability = item.get("market_capability") or selected_market.get("market_capability") or {}
    if capability:
        your_pick["support_level"] = capability.get("support_level")
        your_pick["data_quality"] = capability.get("data_quality")
        your_pick["confidence_cap"] = capability.get("confidence_cap")
    risk_level = _public_selection_risk(verdict, your_pick)
    statpal_context = item.get("statpal_context") or card.get("statpal_context") or {}
    statpal_coverage = statpal_context.get("market_snapshot_coverage") or {}
    statpal_plan = statpal_context.get("market_snapshot_plan") or {}
    statpal_snapshot_types = sorted((statpal_context.get("snapshots") or {}).keys())
    statpal_hydration_source = statpal_context.get("hydration_source") or ("statpal_context" if statpal_snapshot_types else "")
    statpal_snapshot_cache_status = statpal_context.get("snapshot_cache_status") or ("hit" if statpal_snapshot_types else "")
    technical_ref = {
        "status": item.get("status"),
        "match_resolution_score": card.get("match_resolution_score"),
        "kickoff": (item.get("matched_fixture") or {}).get("kickoff_utc")
        or (item.get("matched_fixture") or {}).get("kickoff")
        or "",
        "market_recognized": (item.get("market_taxonomy") or {}).get("recognized"),
        "market_core_supported": (item.get("market_taxonomy") or {}).get("core_supported"),
        "market_support_level": capability.get("support_level") if capability else "",
        "market_data_quality": capability.get("data_quality") if capability else "",
        "market_confidence_cap": capability.get("confidence_cap") if capability else None,
        "market_capability_warnings": capability.get("warnings") or [],
        "statpal_snapshot_types": statpal_snapshot_types,
        "statpal_hydration_source": statpal_hydration_source,
        "statpal_snapshot_cache_status": statpal_snapshot_cache_status,
        "statpal_required_snapshot_types": statpal_coverage.get("required") or statpal_plan.get("snapshot_types") or [],
        "statpal_missing_snapshot_types": statpal_coverage.get("missing") or statpal_plan.get("missing_snapshot_types") or [],
        "statpal_stale_snapshot_types": statpal_plan.get("stale_snapshot_types") or [],
        "statpal_snapshot_coverage_percent": statpal_coverage.get("coverage_percent") if statpal_coverage else statpal_plan.get("coverage_percent"),
        "provider_merge": (item.get("matched_fixture") or {}).get("provider_merge") or item.get("provider_merge") or {},
        "blocked_recommendation_markets": item.get("blocked_recommendation_markets") or [],
        "has_technical_details": True,
    }
    leg_assessment = assess_leg(item)
    canonical_market = item.get("canonical_market") or {}
    if card.get("match_id"):
        technical_ref["match_id"] = card.get("match_id")
    if item.get("status") == "matched_unscored":
        on_demand = item.get("on_demand_analysis") or {}
        technical_ref["analysis_status"] = on_demand.get("status") or "not_started"
        technical_ref["analysis_error"] = on_demand.get("error") or ""
        technical_ref["analysis_run_id"] = on_demand.get("run_id")
    return {
        "id": card.get("match_id") or item.get("match"),
        "match": card.get("fixture") or card.get("match"),
        "match_id": card.get("match_id", ""),
        "your_pick": your_pick,
        "verdict": _public_verdict_object(verdict, submitted_market=item.get("submitted_market"), pick_status=your_pick.get("status")),
        "risk_level": risk_level,
        "risk": _public_risk_label(risk_level),
        "ai_pick": ai_pick,
        "price_check": price_check,
        "why": why,
        "reason_codes": reason_codes,
        "home_recent_form": item.get("home_recent_form") or {},
        "away_recent_form": item.get("away_recent_form") or {},
        "corner_profile": item.get("corner_profile") or {},
        "fixture_context": item.get("fixture_context") or {},
        "evidence_payload": selected_market.get("advisory_evidence") or {},
        "state": str(leg_assessment.state),
        "assessment": {
            "type": leg_assessment.assessment_type,
            "may_publish_probability": leg_assessment.may_publish_probability,
            "market_family": leg_assessment.family,
            "message": leg_assessment.message,
        },
        "market_identity": {
            "resolution": canonical_market.get("resolution") or "unresolved",
            "provider_market_text": item.get("provider_market_text") or item.get("submitted_market") or "",
            "period": canonical_market.get("period") or "",
            "subject": canonical_market.get("subject") or "",
        },
        "technical_ref": technical_ref,
    }


def _with_explanation(card):
    """Attach a plain-language explanation built only from values the model produced."""
    card["explanation"] = explanation_service.explain_leg(card).to_dict()
    return card


def _with_bettor_view(card):
    user_pick = card.get("user_pick") or {}
    ai_pick = card.get("ai_pick") or {}
    action = "replace" if ai_pick.get("available") and (card.get("verdict") or {}).get("code") == "replace" else (
        "keep" if (card.get("verdict") or {}).get("code") in {"keep", "caution"} else "review"
    )
    user_verdict = _bettor_verdict_from_confidence(user_pick.get("confidence_score"))
    user_pick.update(
        {
            "verdict": "replace" if action == "replace" else user_verdict,
            "verdict_label": _bettor_verdict_label(user_verdict),
            "message": _bettor_pick_message(user_verdict, market=user_pick.get("market"), action=action),
        }
    )
    card["user_pick"] = user_pick

    evidence = list(dict.fromkeys(card.get("why") or []))[:5]
    card["evidence"] = evidence
    card["our_view"] = user_pick["message"]

    if action == "replace":
        recommendation_why = []
        if ai_pick.get("market"):
            recommendation_why.append(
                f"{ai_pick.get('market')} has stronger statistical support than the original selection."
            )
        if card.get("comparison", {}).get("confidence_gain") is not None:
            recommendation_why.append(
                f"It improves this leg's confidence by {card['comparison']['confidence_gain']} points."
            )
        recommendation_why.extend(evidence[:2])
        card["recommendation"] = {
            "action": "replace",
            "market": ai_pick.get("market"),
            "confidence": ai_pick.get("confidence_score"),
            "confidence_label": ai_pick.get("confidence_label"),
            "risk_level": ai_pick.get("risk_level"),
            "message": "Use the stronger backed alternative for this fixture.",
            "why": list(dict.fromkeys(recommendation_why))[:4],
        }
    elif action == "keep":
        card["recommendation"] = {
            "action": "keep",
            "market": user_pick.get("market"),
            "confidence": user_pick.get("confidence_score"),
            "confidence_label": user_pick.get("confidence_label"),
            "risk_level": user_pick.get("risk_level"),
            "message": "Keep this selection, but respect the stated risk level.",
            "why": evidence[:4],
        }
    else:
        card["recommendation"] = {
            "action": "review",
            "market": user_pick.get("market"),
            "confidence": user_pick.get("confidence_score"),
            "confidence_label": user_pick.get("confidence_label"),
            "risk_level": user_pick.get("risk_level"),
            "message": "Do not treat this as supported until more reliable match data is available.",
            "why": evidence[:4],
        }
    return card


def _leg_state_counts(items):
    """
    Where every leg stopped, and how it was assessed.

    `heuristic` legs are deliberately excluded from the ticket probability: their score
    is a constant plus context nudges, not a modelled probability. Reporting the split
    is what stops that exclusion looking like a silent gap.
    """
    states = {}
    assessments = {}
    for item in items:
        assessment = assess_leg(item)
        states[str(assessment.state)] = states.get(str(assessment.state), 0) + 1
        assessments[assessment.assessment_type] = assessments.get(assessment.assessment_type, 0) + 1
    return {"by_state": states, "by_assessment_type": assessments}


def _with_leg_risk(card, leg):
    """Attach the calibrated risk view of a leg to its public card."""
    tier_label = "High risk" if leg.tier == "avoid" else leg.tier_label
    probability_percent = _round_percent(leg.probability)
    repair_probability_percent = _round_percent(leg.repair_probability)
    selection_lift = (
        round(repair_probability_percent - probability_percent, 1)
        if repair_probability_percent is not None and probability_percent is not None
        else None
    )
    card["risk_tier"] = {
        "code": leg.tier,
        "label": tier_label,
        "estimated_success_percent": probability_percent,
        "risk_share_percent": leg.risk_share_percent,
        "capped_by_data_quality": leg.capped_by_data_quality,
    }
    card["repair"] = {
        "available": leg.repair_probability is not None,
        "estimated_success_percent": repair_probability_percent,
        "selection_lift_points": selection_lift,
        "ticket_lift_points": leg.repair_lift_points,
        "drop_lift_points": leg.drop_lift_points,
    }
    your_pick = card.get("your_pick") or {}
    data_confidence_score = _float_or_none(your_pick.get("confidence_cap") or your_pick.get("confidence"))
    offered_probability = _implied_probability_from_odds(your_pick.get("odds"))
    price_check = card.get("price_check") or {}
    reference_probability = _implied_probability_from_odds(price_check.get("reference_odds"))
    disagreement_gap = _probability_gap(leg.probability, reference_probability)
    pick_confidence_score = probability_percent
    your_pick.update(
        {
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "fair_odds": _fair_odds(leg.probability),
            "confidence_score": pick_confidence_score,
            "confidence_label": _pick_confidence_label(pick_confidence_score),
            "data_confidence_score": data_confidence_score,
            "decision_score": your_pick.get("decision_score", your_pick.get("score")),
            "risk_score": round((1 - leg.probability) * 100, 1) if leg.probability is not None else None,
            "risk_level": _risk_level_from_confidence(pick_confidence_score),
            "market_implied_probability": offered_probability,
            "market_implied_probability_percent": _round_percent(offered_probability),
            "value_rating": _value_rating(leg.probability, your_pick.get("odds")),
        }
    )
    card["your_pick"] = your_pick
    ai_same_as_user = bool(card.get("ai_pick")) and leg.repair_probability is None
    card["user_pick"] = {
        "market": your_pick.get("market"),
        "odds": your_pick.get("odds"),
        "confidence_score": pick_confidence_score,
        "confidence_label": _pick_confidence_label(pick_confidence_score),
        "risk_level": _risk_level_from_confidence(pick_confidence_score),
        "model_probability_percent": probability_percent,
        "data_confidence_score": data_confidence_score,
        "verdict": (card.get("verdict") or {}).get("code"),
    }
    if card.get("ai_pick"):
        ai_data_confidence_score = _float_or_none(
            card["ai_pick"].get("confidence") or data_confidence_score
        )
        ai_probability = leg.repair_probability if leg.repair_probability is not None else leg.probability
        ai_confidence_score = repair_probability_percent if repair_probability_percent is not None else probability_percent
        card["ai_pick"].update(
            {
                "model_probability": ai_probability,
                "model_probability_percent": ai_confidence_score,
                "fair_odds": _fair_odds(ai_probability),
                "available": True,
                "confidence_score": ai_confidence_score,
                "confidence_label": _pick_confidence_label(ai_confidence_score),
                "data_confidence_score": ai_data_confidence_score,
                "decision_score": card["ai_pick"].get("decision_score", card["ai_pick"].get("score")),
                "risk_level": _risk_level_from_confidence(ai_confidence_score),
                "selection_lift_points": selection_lift,
            }
        )
    else:
        card["ai_pick"] = {"available": False}
    card["comparison"] = {
        "confidence_gain": 0.0 if ai_same_as_user and selection_lift is None else selection_lift,
        "selection_probability_lift": 0.0 if ai_same_as_user and selection_lift is None else selection_lift,
        "ticket_success_lift": leg.repair_lift_points,
    }
    if reference_probability is not None:
        card["market_consensus"] = {
            "reference_odds": price_check.get("reference_odds"),
            "implied_probability": reference_probability,
            "implied_probability_percent": _round_percent(reference_probability),
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "probability_gap_points": disagreement_gap,
            "disagreement_level": _gap_level(disagreement_gap),
        }
        if abs(disagreement_gap or 0) >= 15:
            card.setdefault("reason_codes", [])
            if "model_market_disagreement" not in card["reason_codes"]:
                card["reason_codes"].append("model_market_disagreement")
            card.setdefault("why", [])
            card["why"].append(
                "The model and market consensus disagree strongly, so treat this verdict with extra caution."
            )
    return card


def _public_ticket_killers(ticket_risk):
    selections = []
    for killer in ticket_risk.killers:
        copy = dict(killer)
        if copy.get("tier") == "avoid":
            copy["tier_label"] = "High risk"
        selections.append(copy)
    return selections


def _ticket_risk_level_from_score(score):
    score = _float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 65:
        return "medium"
    return "low"


def _repaired_ticket_confidence_score(ticket_risk):
    probabilities = []
    for leg in ticket_risk.legs:
        probability = leg.repair_probability if leg.repair_probability is not None else leg.probability
        if probability is not None:
            probabilities.append(probability)
    if not probabilities:
        return None
    return round(math.exp(sum(math.log(probability) for probability in probabilities) / len(probabilities)) * 100, 1)


def _bettor_pick_breakdown(selections):
    breakdown = {"strong": 0, "playable": 0, "high_risk": 0, "needs_review": 0}
    for selection in selections or []:
        verdict = _simple_pick_verdict(selection)
        confidence = _float_or_none((selection.get("user_pick") or {}).get("confidence_score"))
        if verdict == "review":
            code = "needs_review"
        elif verdict == "risky":
            code = "high_risk"
        elif confidence is not None and confidence >= 70:
            code = "strong"
        else:
            code = "playable"
        breakdown[code] = breakdown.get(code, 0) + 1
    return breakdown


def _public_score(value):
    value = _float_or_none(value)
    return int(round(value)) if value is not None else None


def _public_confidence_label(score):
    return _pick_confidence_label(score)


def _public_ticket_label(score):
    score = _float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 75:
        return "Strong"
    if score >= 65:
        return "Good"
    if score >= 55:
        return "Playable"
    if score >= 40:
        return "Risky"
    return "Poor"


def _simple_pick_verdict(selection):
    verdict = ((selection or {}).get("verdict") or {}).get("code") or ""
    confidence = _float_or_none(((selection or {}).get("user_pick") or {}).get("confidence_score"))
    if verdict == "replace":
        return "risky"
    if verdict == "keep":
        return "keep"
    if verdict == "caution":
        return "risky" if confidence is not None and confidence < 55 else "caution"
    if verdict in {"expired", "not_assessed", "unmatched", "unmatched_market", "pending_analysis"}:
        return "review"
    return "risky" if confidence is not None and confidence < 55 else "caution"


def _evidence_is_risk(text):
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "risk",
            "weak",
            "only",
            "limited",
            "not enough",
            "disagree",
            "shorter",
            "thin",
            "close to",
            "caution",
            "unsupported",
            "poor",
        )
    )


def _public_market_context_line(selection, market_payload=None):
    market_payload = market_payload or {}
    market_name = market_payload.get("market") or ((selection or {}).get("user_pick") or {}).get("market") or ""
    confidence = _public_score(
        market_payload.get("model_probability_percent")
        or market_payload.get("confidence_score")
        or ((selection or {}).get("user_pick") or {}).get("confidence_score")
    )
    odds = market_payload.get("odds") or ((selection or {}).get("user_pick") or {}).get("odds")
    ev = market_payload.get("ev")
    parts = []
    if confidence is not None:
        parts.append(f"{confidence}% confidence")
    if odds is not None:
        parts.append(f"{odds} odds")
    if ev is not None:
        parts.append(f"{float(ev):+.3f} expected value")
    if not parts:
        return ""
    sentence = f"{market_name} rates at {parts[0]}"
    if len(parts) > 1:
        sentence += f" with {', '.join(parts[1:])}"
    return sentence + "."


def _stat_line_from_form(label, form):
    if not isinstance(form, dict) or not int(form.get("games") or 0):
        return ""
    return _format_game_form_line(label, form) + "."


def _goal_model_line_from_evidence(evidence):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    candidates = [evidence, statpal]
    for payload in candidates:
        home_xg = _float_or_none(
            payload.get("home_expected_goals")
            or payload.get("home_xg")
            or payload.get("expected_home_goals")
            or payload.get("first_half_expected_home_goals")
        )
        away_xg = _float_or_none(
            payload.get("away_expected_goals")
            or payload.get("away_xg")
            or payload.get("expected_away_goals")
            or payload.get("first_half_expected_away_goals")
        )
        total_xg = _float_or_none(
            payload.get("expected_goals")
            or payload.get("expected_total")
            or payload.get("total_expected_goals")
        )
        if home_xg is not None and away_xg is not None:
            return f"Expected goals: home {round(home_xg, 2)}, away {round(away_xg, 2)}."
        if total_xg is not None:
            return f"Expected goals sit around {round(total_xg, 2)}."
    return ""


def _period_or_family_line(selection, market_payload=None):
    market_payload = market_payload or {}
    user_pick = (selection or {}).get("user_pick") or {}
    market = str(market_payload.get("market") or user_pick.get("market") or "")
    assessment = (selection or {}).get("assessment") or {}
    family = str(assessment.get("market_family") or "")
    technical = (selection or {}).get("technical_ref") or {}
    snapshots = technical.get("statpal_snapshot_types") or []
    if "1H" in market or "First Half" in market or "first_half" in str((selection or {}).get("market_identity") or {}):
        return "This is a first-half market, so the pick depends on early match control rather than full-time strength."
    if "2H" in market or "Second Half" in market:
        return "This is a second-half market, so match state and second-half scoring profile matter most."
    if "corner" in family or "Corner" in market:
        return "This corner market should be judged from team corner volume and corner concessions, not win/loss form."
    if "card" in family or "Card" in market:
        return "This card market should be judged from fouls, cards, referee tendency and match intensity."
    if "shots_on_target" in family or "Shots On Target" in market:
        return "This shots-on-target market should be judged from attacking shot volume and defensive shot allowance."
    if snapshots:
        return f"StatPal context available: {', '.join(str(item) for item in snapshots[:4])}."
    return ""


def _stats_backed_evidence(selection, *, market_payload=None, include_context=True):
    market_payload = market_payload or {}
    evidence = []
    context_line = _public_market_context_line(selection, market_payload)
    if include_context and context_line:
        evidence.append(context_line)

    for label, form in (
        ("Home", (selection or {}).get("home_recent_form")),
        ("Away", (selection or {}).get("away_recent_form")),
    ):
        line = _stat_line_from_form(label, form)
        if line:
            evidence.append(line)

    selected_evidence = (
        market_payload.get("advisory_evidence")
        or (selection or {}).get("evidence_payload")
        or {}
    )
    goal_line = _goal_model_line_from_evidence(selected_evidence)
    if goal_line:
        evidence.append(goal_line)

    raw_evidence = list((selection or {}).get("evidence") or (selection or {}).get("why") or [])
    for item in raw_evidence:
        text = str(item or "").strip()
        lowered = text.lower()
        if not text:
            continue
        if "statpal reference" in lowered or "your price is" in lowered or "reference price" in lowered:
            continue
        evidence.append(text)

    period_line = _period_or_family_line(selection, market_payload)
    if period_line:
        evidence.append(period_line)

    return list(dict.fromkeys(evidence))[:5]


def _clean_bettor_evidence_items(items, *, limit=4):
    cleaned = []
    for item in items or []:
        text = str(item or "").strip()
        lowered = text.lower()
        if not text:
            continue
        if "statpal reference" in lowered or "reference price" in lowered or "your price is" in lowered:
            continue
        cleaned.append(text[:240])
    return list(dict.fromkeys(cleaned))[:limit]


def _text_mentions_blocked_slip_recommendation_market(text):
    lowered = normalize_market_text(text)
    blocked_markets = (
        "over 0.5",
        "1h over 0.5",
        "2h over 0.5",
        "home team over 0.5",
        "away team over 0.5",
        "shots over 0.5",
        "shots on target over 0.5",
    )
    return any(market in lowered for market in blocked_markets)


def _clean_deepseek_recommendation_why(game, items):
    user_market = ((game or {}).get("user_pick") or {}).get("market")
    recommendation = (game or {}).get("recommendation") or {}
    recommendation_market = (recommendation.get("pick") or {}).get("market")
    user_submitted_blocked_market = _blocked_slip_recommendation_market({"market": user_market})
    recommended_market_is_user_market = _market_matches(user_market, recommendation_market)
    allow_blocked_text = user_submitted_blocked_market and recommended_market_is_user_market
    cleaned = []
    for item in _clean_bettor_evidence_items(items):
        if not allow_blocked_text and _text_mentions_blocked_slip_recommendation_market(item):
            continue
        cleaned.append(item)
    return cleaned


def _split_bettor_evidence(selection):
    raw = _stats_backed_evidence(selection, market_payload=(selection or {}).get("your_pick") or {})
    verdict = _simple_pick_verdict(selection)
    positive = [item for item in raw if not _evidence_is_risk(item)]
    risky = [item for item in raw if _evidence_is_risk(item)]
    user_pick = (selection or {}).get("user_pick") or {}
    probability = user_pick.get("confidence_score")
    market = user_pick.get("market") or "this selection"
    if probability is not None:
        support_line = f"The model gives {market} about {_public_score(probability)}% support."
        if verdict in {"risky", "review"}:
            risky.append(support_line)
        else:
            positive.append(support_line)
    if verdict == "risky" and not risky:
        risky = raw[:3] or [f"The available statistics do not strongly support {market}."]
    if verdict in {"keep", "caution"} and not positive:
        positive = raw[:3] or [f"The available statistics give {market} some support."]
    return list(dict.fromkeys(positive))[:4], list(dict.fromkeys(risky))[:4]


def _bettor_game_summary(selection):
    user_pick = (selection or {}).get("user_pick") or {}
    market = user_pick.get("market") or "this selection"
    verdict = _simple_pick_verdict(selection)
    if verdict == "keep":
        return f"The statistics support keeping {market}."
    if verdict == "caution":
        return f"{market} is playable, but it is not one of the safest legs on this ticket."
    if verdict == "risky":
        return f"The statistics do not strongly support {market}."
    return f"We need stronger match data before judging {market}."


def _bettor_conclusion(selection):
    user_pick = (selection or {}).get("user_pick") or {}
    market = user_pick.get("market") or "this selection"
    verdict = _simple_pick_verdict(selection)
    if verdict == "keep":
        return f"The available evidence supports keeping {market}."
    if verdict == "caution":
        return f"{market} is playable, but it carries enough risk to treat carefully."
    if verdict == "risky":
        return f"{market} carries too much risk based on the available match evidence."
    return f"{market} has not been backed by enough reliable match evidence yet."


def _bettor_recommendation(selection):
    recommendation = (selection or {}).get("recommendation") or {}
    user_pick = (selection or {}).get("user_pick") or {}
    ai_pick = (selection or {}).get("ai_pick") or {}
    action = recommendation.get("action") or "review"
    if action == "replace" and ai_pick.get("available"):
        pick = {
            "market": ai_pick.get("market"),
            "confidence_score": _public_score(ai_pick.get("confidence_score")),
            "confidence_label": _public_confidence_label(ai_pick.get("confidence_score")),
        }
    elif action in {"keep", "caution"} or _simple_pick_verdict(selection) in {"keep", "caution"}:
        action = "keep" if _simple_pick_verdict(selection) == "keep" else "caution"
        pick = {
            "market": user_pick.get("market"),
            "confidence_score": _public_score(user_pick.get("confidence_score")),
            "confidence_label": _public_confidence_label(user_pick.get("confidence_score")),
        }
    else:
        pick = None
    why = _stats_backed_evidence(
        selection,
        market_payload=(selection or {}).get("ai_pick") or user_pick,
        include_context=True,
    )
    if not why:
        why = list(dict.fromkeys(recommendation.get("why") or []))[:4]
    if not why:
        if action == "replace":
            why = ["This alternative has stronger statistical support than the original selection."]
        elif action == "keep":
            why = ["Your original selection already fits the statistical profile of the match."]
        elif action == "caution":
            why = ["There is not enough evidence for a stronger replacement to be recommended confidently."]
        else:
            why = ["No confident recommendation is available from the current match data."]
    return {"action": action, "pick": pick, "why": why}


def _build_bettor_public_payload(review, technical_public, *, enhance=False):
    technical_public = technical_public or {}
    ticket_summary = technical_public.get("ticket_summary") or {}
    user_ticket = ticket_summary.get("user_ticket") or {}
    ai_ticket = ticket_summary.get("ai_ticket") or {}
    improvement = ticket_summary.get("improvement") or {}
    breakdown = ticket_summary.get("pick_breakdown") or {}
    games = []
    recommended_picks = []
    for selection in technical_public.get("selections") or []:
        user_pick = selection.get("user_pick") or selection.get("your_pick") or {}
        recommendation = _bettor_recommendation(selection)
        positive_evidence, risk_evidence = _split_bettor_evidence(selection)
        match = selection.get("match") or ""
        selected_pick = recommendation.get("pick") or {
            "market": user_pick.get("market"),
            "confidence_score": _public_score(user_pick.get("confidence_score")),
            "confidence_label": _public_confidence_label(user_pick.get("confidence_score")),
        }
        changed = recommendation.get("action") == "replace"
        games.append(
            {
                "id": selection.get("id"),
                "match": match,
                "kickoff": (selection.get("technical_ref") or {}).get("kickoff")
                or (selection.get("matched_fixture") or {}).get("kickoff_utc")
                or "",
                "user_pick": {
                    "market": user_pick.get("market"),
                    "odds": user_pick.get("odds"),
                    "confidence_score": _public_score(user_pick.get("confidence_score")),
                    "confidence_label": _public_confidence_label(user_pick.get("confidence_score")),
                    "verdict": _simple_pick_verdict(selection),
                    "summary": _bettor_game_summary(selection),
                },
                "analysis": {
                    "positive_evidence": positive_evidence,
                    "risk_evidence": risk_evidence,
                    "conclusion": _bettor_conclusion(selection),
                },
                "recommendation": recommendation,
            }
        )
        recommended_picks.append(
            {
                "match": match,
                "market": (selected_pick or {}).get("market") or user_pick.get("market"),
                "confidence_score": (selected_pick or {}).get("confidence_score")
                or _public_score(user_pick.get("confidence_score")),
                "confidence_label": (selected_pick or {}).get("confidence_label")
                or _public_confidence_label(user_pick.get("confidence_score")),
                "action": recommendation.get("action"),
                "included_in_estimate": user_pick.get("confidence_score") is not None,
                "changed": changed,
            }
        )

    changes = int(improvement.get("picks_changed") or 0)
    high_risk_count = int(breakdown.get("high_risk") or 0)
    review_count = int(breakdown.get("needs_review") or 0)
    risky_count = high_risk_count
    needs_word = "needs" if changes == 1 else "need"
    verdict_title = f"{changes} {_plural(changes, 'pick')} {needs_word} changing" if changes else "No forced changes"
    if changes:
        extra = f" {review_count} {_plural(review_count, 'pick')} still needs review." if review_count == 1 else (
            f" {review_count} {_plural(review_count, 'pick')} still need review." if review_count else ""
        )
        verdict_message = (
            f"{changes} {_plural(changes, 'selection')} "
            f"{'has' if changes == 1 else 'have'} weak statistical support. "
            f"We found {'a stronger alternative' if changes == 1 else 'stronger alternatives'} for "
            f"{'it' if changes == 1 else 'them'}.{extra}"
        )
    elif risky_count or review_count:
        total_attention = risky_count + review_count
        verdict_message = (
            f"{total_attention} {_plural(total_attention, 'selection')} need caution or review, but no stronger replacement "
            "was found with enough statistical support."
        )
    else:
        verdict_message = "Your selections are supported by the available match data."

    payload = {
        "id": review.id,
        "source": review.source,
        "status": review.status,
        "ticket": {
            "total_games": ticket_summary.get("total_legs") or len(games),
            "original_odds": user_ticket.get("combined_odds"),
            "user_picks": {
                "confidence_score": _public_score(user_ticket.get("overall_confidence_score")),
                "label": _public_ticket_label(user_ticket.get("overall_confidence_score")),
                "estimated_success_percent": user_ticket.get("estimated_success_percent"),
                "summary": {
                    "strong": int(breakdown.get("strong") or 0),
                    "playable": int(breakdown.get("playable") or 0),
                    "risky": risky_count,
                    "review": review_count,
                },
            },
            "recommended_picks": {
                "confidence_score": _public_score(ai_ticket.get("overall_confidence_score")),
                "label": _public_ticket_label(ai_ticket.get("overall_confidence_score")),
                "estimated_success_percent": ai_ticket.get("estimated_success_percent"),
                "estimated_odds": ai_ticket.get("combined_odds"),
                "changes": changes,
            },
            "verdict": {"title": verdict_title, "message": verdict_message},
        },
        "games": games,
        "recommended_ticket": {
            "confidence_score": _public_score(ai_ticket.get("overall_confidence_score")),
            "confidence_label": _public_ticket_label(ai_ticket.get("overall_confidence_score")),
            "estimated_success_percent": ai_ticket.get("estimated_success_percent"),
            "estimated_odds": ai_ticket.get("combined_odds"),
            "picks": recommended_picks,
        },
        "disclaimer": (
            "Confidence scores are statistical estimates based on available match data and do not guarantee an outcome."
        ),
    }
    if enhance and review.source == SlipReview.Source.SPORTYBET:
        game_count = len(games)
        if game_count <= SLIP_REVIEW_DEEPSEEK_MAX_GAMES:
            payload = _enhance_bettor_public_with_deepseek(payload)
        else:
            log.info(
                "DeepSeek SportyBet public analysis skipped review=%s games=%s max_games=%s reason=large_slip_llm_limit",
                review.id,
                game_count,
                SLIP_REVIEW_DEEPSEEK_MAX_GAMES,
            )
    return payload


def _streamed_slip_review_game_payload(review, index, result):
    try:
        summary = _manual_review_summary([result or {}])
        public_payload = _build_bettor_public_payload(
            review,
            summary.get("public") or {},
            enhance=False,
        )
        game = (public_payload.get("games") or [None])[0]
        recommended_pick = (public_payload.get("recommended_ticket") or {}).get("picks") or []
        return _json_safe(
            {
                "index": index,
                "order": index + 1,
                "game": game,
                "recommended_pick": recommended_pick[0] if recommended_pick else None,
            }
        )
    except Exception:
        log.exception(
            "Slip review streamed game payload failed review=%s leg=%s",
            getattr(review, "id", None),
            index + 1,
        )
        return {
            "index": index,
            "order": index + 1,
            "game": None,
            "recommended_pick": None,
        }


def _enhance_bettor_public_with_deepseek(payload):
    try:
        from .grindalgo import algo_runner

        if not algo_runner.llm_reasoning_enabled():
            return payload
        games = payload.get("games") or []
        compact_games = [
            {
                "index": index,
                "match": game.get("match"),
                "user_pick": game.get("user_pick"),
                "analysis": game.get("analysis"),
                "recommendation": game.get("recommendation"),
            }
            for index, game in enumerate(games)
        ]
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        deepseek_payload = {
            "model": model,
            "temperature": 0.15,
            "top_p": 0.85,
            "max_tokens": max(1800, min(7000, 850 * len(compact_games))),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write simple football betslip analysis for bettors. Use only the supplied facts. "
                        "Do not promise a win. Do not invent team form, injuries, odds, xG, lineups, or H2H. "
                        "Keep the user's markets, scores, verdicts, and recommendation actions unchanged. "
                        "Do not introduce Over 0.5, 1H Over 0.5, 2H Over 0.5, team Over 0.5, "
                        "or player shots/SOT Over 0.5 as replacement recommendations unless it is the user's submitted pick. "
                        "Do not use StatPal reference-price wording as evidence. Bettors need football stats, "
                        "such as expected goals, recent form, first-half/second-half profile, corners, cards, "
                        "shots, or the market-specific confidence already supplied. "
                        "Return strict valid JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Rewrite each game into bettor-facing analysis. For each game return: index, "
                        "user_pick_summary, positive_evidence, risk_evidence, conclusion, recommendation_why. "
                        "Evidence arrays must only rephrase supplied football/statistical evidence and must be short bullet strings. "
                        "Each evidence item should include an actual stat when supplied, such as confidence %, expected goals, "
                        "recent W-D-L, goals scored/conceded, corner totals, card totals, shot volume, or period-specific context. "
                        "Never write bullets like 'Your price is close to the StatPal reference'. "
                        "Shape: {\"games\":[{\"index\":0,\"user_pick_summary\":\"...\","
                        "\"positive_evidence\":[\"...\"],\"risk_evidence\":[\"...\"],"
                        "\"conclusion\":\"...\",\"recommendation_why\":[\"...\"]}]}.\n"
                        f"Data:\n{json.dumps(compact_games, ensure_ascii=True)}"
                    ),
                },
            ],
        }
        content = algo_runner._deepseek_chat_completion(deepseek_payload, retries=1) or ""
        parsed = algo_runner._parse_llm_json(content)
        items = parsed.get("games") if isinstance(parsed, dict) else []
        updates = {int(item.get("index")): item for item in items or [] if isinstance(item, dict) and str(item.get("index", "")).isdigit()}
        for index, game in enumerate(games):
            update = updates.get(index)
            if not update:
                continue
            if update.get("user_pick_summary"):
                game["user_pick"]["summary"] = str(update["user_pick_summary"]).strip()[:320]
            if isinstance(update.get("positive_evidence"), list):
                cleaned = _clean_bettor_evidence_items(update["positive_evidence"])
                if cleaned:
                    game["analysis"]["positive_evidence"] = cleaned
            if isinstance(update.get("risk_evidence"), list):
                game["analysis"]["risk_evidence"] = _clean_bettor_evidence_items(update["risk_evidence"])
            if update.get("conclusion"):
                game["analysis"]["conclusion"] = str(update["conclusion"]).strip()[:360]
            if isinstance(update.get("recommendation_why"), list):
                cleaned = _clean_deepseek_recommendation_why(game, update["recommendation_why"])
                if cleaned:
                    game["recommendation"]["why"] = cleaned
        log.info("DeepSeek SportyBet public analysis enhanced %s/%s games", len(updates), len(games))
    except Exception as exc:
        log.warning("DeepSeek SportyBet public analysis skipped: %s", exc)
    return payload


def _ticket_killers_message(ticket_risk):
    killers = ticket_risk.killers
    if not killers:
        if not ticket_risk.assessed_legs:
            return "No selection could be assessed, so no risk ranking is available."
        return "No single selection dominates this ticket's risk."
    share = round(sum(killer["risk_share_percent"] for killer in killers), 1)
    count = len(killers)
    lift = sum(killer["drop_lift_points"] or 0 for killer in killers)
    message = (
        f"{count} {_plural(count, 'selection')} {'carries' if count == 1 else 'carry'} "
        f"{share}% of this ticket's risk."
    )
    if lift > 0:
        message += f" Changing {'it' if count == 1 else 'them'} to safer backed alternatives would raise the estimated success rate by about {round(lift, 2)} percentage points."
    return message


def _slip_intelligence(results):
    enriched = []
    for item in results:
        copy = _without_remove_recommendation(dict(item))
        copy = _without_blocked_replacement_recommendation(copy)
        copy["selection_score"] = _selection_strength_score(copy)
        enriched.append(copy)

    analysed = [item for item in enriched if _selection_has_analysis(item)]
    # Ticket health is the geometric mean of the calibrated leg probabilities, so it
    # measures leg quality independently of leg count. See apps.algo.ticket_risk.
    ticket_risk = ticket_risk_service.assess(enriched)
    overall_score = ticket_risk.health_percent

    remove_items = [item for item in enriched if item.get("verdict") == "remove"]
    replace_items = [item for item in enriched if item.get("verdict") == "replace"]
    caution_items = [item for item in enriched if item.get("verdict") == "caution"]
    keep_items = [item for item in enriched if item.get("verdict") == "keep"]
    expired_items = [item for item in enriched if item.get("status") == "expired"]
    pending_items = [item for item in enriched if item.get("status") == "matched_unscored"]
    not_assessed_items = [item for item in enriched if item.get("verdict") == "not_assessed"]
    unverified_items = [
        item for item in enriched
        if (not _selection_has_analysis(item) or item.get("verdict") == "not_assessed")
        and item.get("status") != "expired"
    ]

    risk_level = risk_level_for(ticket_risk)

    strongest = sorted(
        [item for item in analysed if item.get("verdict") in {"keep", "caution"}],
        key=lambda item: item.get("selection_score") or 0,
        reverse=True,
    )[:3]
    weakest = sorted(
        [item for item in analysed if item.get("verdict") in {"remove", "replace"}],
        key=lambda item: item.get("selection_score") or 0,
    )[:3]
    original_combined = _combined_odds(_selection_original_odds(item) for item in enriched)
    suggested_combined = _combined_odds(_selection_suggested_odds(item) for item in enriched)
    original_success = ticket_risk.success_percent
    optimized_success = ticket_risk.repaired_success_percent
    optimized_leg_count = ticket_risk.assessed_legs
    improvement = (
        round(optimized_success - original_success, 2)
        if optimized_success is not None and original_success is not None
        else None
    )
    api_usage = _slip_api_usage(enriched)

    if remove_items:
        verdict = f"Remove {len(remove_items)} leg(s) before trusting this slip."
    elif replace_items:
        verdict = f"Replace {len(replace_items)} leg(s) with stronger markets."
    elif caution_items:
        verdict = "Playable, but treat the caution legs carefully."
    elif keep_items and len(keep_items) == len(analysed) and not unverified_items:
        verdict = "This slip is clean from the current model view."
    else:
        verdict = "Some selections still need verification before this slip is reliable."

    ticket_health = {
        "score": overall_score,
        "max_score": 100,
        "label": _ticket_health_label(overall_score),
        "risk_level": risk_level,
        "summary": _ticket_health_summary(
            overall_score,
            risk_level,
            len(remove_items),
            len(replace_items),
            len(caution_items),
            len(unverified_items),
        ),
    }
    original_ticket = {
        "legs": len(analysed),
        "estimated_success": original_success,
        "combined_odds": original_combined,
        "fair_odds": _fair_odds((original_success or 0) / 100) if original_success is not None else None,
    }
    optimized_ticket = {
        "legs": optimized_leg_count,
        "estimated_success": optimized_success,
        "combined_odds": suggested_combined,
        "fair_odds": _fair_odds((optimized_success or 0) / 100) if optimized_success is not None else None,
    }
    repaired_confidence_score = _repaired_ticket_confidence_score(ticket_risk)
    confidence_change = (
        round(repaired_confidence_score - overall_score, 1)
        if repaired_confidence_score is not None and overall_score is not None
        else None
    )
    improvement_text = f"+{improvement} percentage points" if improvement is not None and improvement > 0 else (
        f"{improvement} percentage points" if improvement is not None else ""
    )
    # A leg is only genuinely tracked when the settler can find it after kickoff: it
    # needs a resolved fixture date and a market this engine can settle. Anything else
    # must not be reported as tracked.
    trackable_items = [
        item for item in enriched
        if _settlement_market_for(item) and (item.get("matched_fixture") or {}).get("match_date")
    ]
    untracked_items = []
    for item in enriched:
        reasons = []
        if not (item.get("matched_fixture") or {}).get("match_date"):
            reasons.append("missing_fixture_date")
        if not _settlement_market_for(item):
            reasons.append("unsupported_settlement_market")
        if reasons:
            untracked_items.append(
                {
                    "id": (item.get("matched_fixture") or {}).get("match_id") or item.get("match"),
                    "match": item.get("match"),
                    "market": item.get("submitted_market"),
                    "reasons": reasons,
                }
            )
    flagged_risky_items = [item for item in enriched if _selection_flagged_risky(item)]
    learning_tracking = {
        "status": "tracking" if trackable_items else "not_tracked",
        "tracked_selections": len(trackable_items),
        "untracked_selections": len(enriched) - len(trackable_items),
        "flagged_risky_selections": len(flagged_risky_items),
        "outcome_tracking": "pending_settlement" if trackable_items else "unavailable",
        "reason": (
            ""
            if trackable_items
            else "No leg has both a resolved fixture date and a market the settlement engine supports."
        ),
    }

    public_selections = [
        _with_explanation(_with_bettor_view(_with_leg_risk(_public_selection_card(item), leg)))
        for item, leg in zip(enriched, ticket_risk.legs)
    ]
    bettor_breakdown = _bettor_pick_breakdown(public_selections)
    public_ticket_killers = _public_ticket_killers(ticket_risk)
    recommended_change_ids = [
        selection.get("id")
        for selection in public_selections
        if (selection.get("verdict") or {}).get("code") in {"replace", "remove"}
    ]
    ticket_impact = {
        "message": (
            # Only claim an improvement when one was actually measured; a null
            # increase alongside "improves the success rate" is not a claim we can make.
            f"Changing {len(replace_items) + len(remove_items)} risky {_plural(len(replace_items) + len(remove_items), 'pick')} improves the estimated ticket success rate."
            if (remove_items or replace_items) and improvement is not None
            else f"{len(replace_items) + len(remove_items)} {_plural(len(replace_items) + len(remove_items), 'pick')} could be changed, but the effect on this ticket could not be estimated."
            if remove_items or replace_items
            else f"None of these {len(enriched)} {_plural(len(enriched), 'selection')} could be analysed, so no risk assessment was possible."
            if not analysed
            else "No major risky picks were found in the analysed selections."
        ),
        "picks_changed": len(replace_items) + len(remove_items),
        "estimated_success_increase_points": improvement,
        "original_odds": original_combined,
        "optimized_odds": suggested_combined,
    }
    verdict_code = "review"
    verdict_label = "Review ticket"
    verdict_message = verdict
    issue_text = _ticket_issue_text(
        replace_count=len(replace_items),
        remove_count=len(remove_items),
        caution_count=len(caution_items),
        unverified_count=len(unverified_items),
    )
    if remove_items and replace_items:
        verdict_code = "replace_or_remove"
        change_count = len(remove_items) + len(replace_items)
        verdict_label = f"Change {change_count} {_plural(change_count, 'pick')}"
        verdict_message = f"This ticket has {issue_text}."
    elif remove_items:
        verdict_code = "avoid_risky_picks"
        verdict_label = f"Avoid {len(remove_items)} {_plural(len(remove_items), 'pick')}"
        verdict_message = f"This ticket has {issue_text}."
    elif replace_items:
        verdict_code = "replace_picks" if len(replace_items) != len(analysed) else "replace_all"
        verdict_label = (
            f"Replace {len(replace_items)} {_plural(len(replace_items), 'pick')}"
            if len(replace_items) != len(analysed)
            else f"Replace all {len(replace_items)} {_plural(len(replace_items), 'pick')}"
        )
        verdict_message = (
            "Every submitted pick has a safer or stronger alternative."
            if len(replace_items) == len(analysed)
            else f"This ticket has {issue_text}."
        )
    elif caution_items:
        verdict_code = "play_with_caution"
        verdict_label = "Play with caution"
        verdict_message = f"This ticket has {issue_text}."
    elif keep_items and len(keep_items) == len(analysed) and not unverified_items:
        verdict_code = "playable"
        verdict_label = "Playable"
        verdict_message = "This ticket looks clean from the current analysis."

    public_review = {
        "contract_version": "match_checker_public_v2",
        "response_mode": "public",
        "ticket": {
            "title": "Slip Review",
            "total_legs": len(enriched),
            "analysed_legs": len(analysed),
            "pending_analysis_legs": len(pending_items),
            "unmatched_legs": len([item for item in enriched if _selection_is_unmatched(item)]),
            "expired_legs": len(expired_items),
            "estimated_success_percent": ticket_risk.success_percent,
            "risk_tiers": ticket_risk.tier_counts,
            "assessed_legs_in_estimate": ticket_risk.assessed_legs,
            "legs_excluded_from_estimate": ticket_risk.unassessed_legs,
        },
        "correlation": ticket_risk.correlation,
        "explanation": {},
        "leg_states": _leg_state_counts(enriched),
        "ticket_health": ticket_health,
        "ticket_summary": {
            "total_legs": len(enriched),
            "pick_breakdown": bettor_breakdown,
            "user_ticket": {
                "overall_confidence_score": overall_score,
                "estimated_success_percent": original_success,
                "risk_level": risk_level,
                "label": _ticket_health_label(overall_score),
                "combined_odds": original_combined,
                "model_fair_odds": original_ticket["fair_odds"],
            },
            "ai_ticket": {
                "overall_confidence_score": repaired_confidence_score,
                "estimated_success_percent": optimized_success,
                "risk_level": _ticket_risk_level_from_score(repaired_confidence_score),
                "label": "Improved" if confidence_change is not None and confidence_change > 0 else _ticket_health_label(repaired_confidence_score),
                "combined_odds": suggested_combined,
                "model_fair_odds": optimized_ticket["fair_odds"],
            },
            "improvement": {
                "confidence_score_change": confidence_change,
                "success_probability_change": improvement,
                "picks_changed": len(replace_items) + len(remove_items),
            },
        },
        "ticket_killers": {
            "selections": public_ticket_killers,
            "message": _ticket_killers_message(ticket_risk),
            "combined_risk_share_percent": round(
                sum(killer["risk_share_percent"] for killer in public_ticket_killers), 1
            ) if public_ticket_killers else None,
        },
        "calibration": {
            **ticket_risk.calibration.to_dict(),
            "disclaimer": (
                "Estimated success rates are model estimates, not guarantees. "
                + (
                    "They are calibrated against selections that have already settled."
                    if ticket_risk.calibration.basis != "prior"
                    else "Not enough selections have settled yet to validate these estimates, "
                         "so a deliberately conservative prior is used."
                )
            ),
        },
        "verdict": {
            "code": verdict_code,
            "label": verdict_label,
            "message": verdict_message,
        },
        "comparison": {
            "original": {
                "legs": original_ticket["legs"],
                "combined_odds": original_ticket["combined_odds"],
                "model_fair_odds": original_ticket["fair_odds"],
                "model_estimated_success_percent": original_ticket["estimated_success"],
            },
            "repaired": {
                "legs": optimized_ticket["legs"],
                "combined_odds": optimized_ticket["combined_odds"],
                "model_fair_odds": optimized_ticket["fair_odds"],
                "model_estimated_success_percent": optimized_ticket["estimated_success"],
            },
            "optimized": {
                "legs": optimized_ticket["legs"],
                "combined_odds": optimized_ticket["combined_odds"],
                "model_fair_odds": optimized_ticket["fair_odds"],
                "model_estimated_success_percent": optimized_ticket["estimated_success"],
            },
            "success_increase_percentage_points": improvement,
            "picks_changed": len(replace_items) + len(remove_items),
        },
        "improvement": {
            "original_success_percent": original_success,
            "repaired_success_percent": optimized_success,
            "optimized_success_percent": optimized_success,
            "increase_percentage_points": improvement,
            "label": improvement_text,
        },
        "ticket_impact": ticket_impact,
        "recommended_change_ids": recommended_change_ids,
        "counts": {
            "keep": len(keep_items),
            "caution": len(caution_items),
            "replace": len(replace_items),
            "remove": len(remove_items),
            "pending_analysis": len(pending_items),
            "not_assessed": len(not_assessed_items),
            "unmatched": len([item for item in enriched if _selection_is_unmatched(item)]),
            "expired": len(expired_items),
        },
        "selections": public_selections,
        "tracking": {
            "enabled": bool(trackable_items),
            "status": learning_tracking["outcome_tracking"],
            "tracked_selections": len(trackable_items),
            "untracked_selections": len(untracked_items),
            "untracked": untracked_items,
            "flagged_risky_selections": len(flagged_risky_items),
        },
    }
    # Built last, so it can summarise the finished payload rather than a partial one.
    public_review["explanation"] = explanation_service.explain_ticket(public_review).to_dict()

    return enriched, {
        "overall_score": overall_score,
        "health_score": overall_score,
        "risk_level": risk_level,
        "verdict": verdict,
        "summary": ticket_health["summary"],
        "public": public_review,
        "ticket_health": ticket_health,
        "original_ticket": original_ticket,
        "optimized_ticket": optimized_ticket,
        "improvement": improvement_text,
        "improvement_percent": improvement,
        "learning_tracking": learning_tracking,
        "api_usage": api_usage,
        "original_combined_odds": original_combined,
        "suggested_combined_odds": suggested_combined,
        "strongest_legs": [_selection_card(item) for item in strongest],
        "weakest_legs": [_selection_card(item) for item in weakest],
        "legs_to_keep": [_selection_card(item) for item in keep_items],
        "legs_to_caution": [_selection_card(item) for item in caution_items],
        "legs_to_replace": [_selection_card(item) for item in replace_items],
        "legs_to_remove": [_selection_card(item) for item in remove_items],
        "expired_legs": [_selection_card(item) for item in expired_items],
        "unverified_legs": [_selection_card(item) for item in unverified_items],
    }


def _manual_review_summary(results):
    enriched, intelligence = _slip_intelligence(results)
    return {
        "count": len(enriched),
        "analysed_count": sum(1 for item in enriched if _selection_has_analysis(item)),
        "keep_count": sum(1 for item in enriched if item.get("verdict") == "keep"),
        "caution_count": sum(1 for item in enriched if item.get("verdict") == "caution"),
        "replace_count": sum(1 for item in enriched if item.get("verdict") == "replace"),
        "remove_count": sum(1 for item in enriched if item.get("verdict") == "remove"),
        "expired_count": sum(1 for item in enriched if item.get("status") == "expired"),
        "unmatched_count": sum(1 for item in enriched if _selection_is_unmatched(item)),
        "pending_analysis_count": sum(1 for item in enriched if item.get("status") == "matched_unscored"),
        "not_assessed_count": sum(1 for item in enriched if item.get("verdict") == "not_assessed"),
        "health_score": intelligence.get("health_score", 0),
        "risk_level": intelligence.get("risk_level", ""),
        "ticket_health": intelligence.get("ticket_health", {}),
        "original_ticket": intelligence.get("original_ticket", {}),
        "optimized_ticket": intelligence.get("optimized_ticket", {}),
        "improvement": intelligence.get("improvement", ""),
        "improvement_percent": intelligence.get("improvement_percent"),
        "learning_tracking": intelligence.get("learning_tracking", {}),
        "api_usage": intelligence.get("api_usage", _empty_api_usage()),
        "public": intelligence.get("public", {}),
        "intelligence": intelligence,
    }


def _review_status_from_summary(summary):
    count = int(summary.get("count") or 0)
    analysed_count = int(summary.get("analysed_count") or 0)
    pending_count = int(summary.get("pending_analysis_count") or 0)
    not_assessed_count = int(summary.get("not_assessed_count") or 0)
    reviewable_count = max(0, count - int(summary.get("expired_count") or 0))
    if count and analysed_count == reviewable_count:
        return SlipReview.Status.COMPLETED
    if analysed_count:
        return SlipReview.Status.PARTIAL
    if pending_count:
        return SlipReview.Status.UNANALYSED
    if not_assessed_count:
        # The review ran to completion and concluded it could not assess anything.
        # That is a finding, not a crash, and must not be reported as a failure.
        return SlipReview.Status.UNANALYSED
    return SlipReview.Status.FAILED


def _settlement_market_for(item):
    """
    Canonical, orientation-corrected market used to settle this leg after kickoff.

    Returns "" when the market cannot be resolved from a finished fixture, which the
    settler records as ``unsettleable`` rather than a void.
    """
    market = item.get("analysis_market")
    if not market:
        canonical = (item.get("market_taxonomy") or {}).get("canonical") or ""
        if canonical:
            market = _market_for_fixture_orientation(canonical, item.get("matched_fixture") or {})
    market = str(market or "").strip()
    return market if algo_runner_service.can_settle_market(market) else ""


def _selection_flagged_risky(item):
    """Whether this leg was called out pre-kickoff, frozen at analysis time."""
    return item.get("verdict") in {"remove", "replace", "caution"}


def _log_slip_review_debug(review, summary):
    public = (summary or {}).get("public") or {}
    ticket_summary = public.get("ticket_summary") or {}
    user_ticket = ticket_summary.get("user_ticket") or {}
    ai_ticket = ticket_summary.get("ai_ticket") or {}
    improvement = ticket_summary.get("improvement") or {}
    explanation = public.get("explanation") or {}
    tracking = public.get("tracking") or {}
    correlation = public.get("correlation") or {}
    counts = public.get("counts") or {}
    verdict = public.get("verdict") or {}
    log.info(
        (
            "Slip review public summary review=%s status=%s source=%s total_legs=%s analysed=%s "
            "user_conf=%s user_success=%s user_odds=%s ai_conf=%s ai_success=%s ai_odds=%s "
            "confidence_delta=%s success_delta=%s picks_changed=%s verdict=%s counts=%s "
            "correlation=%s tracking=%s explanation_ok=%s explanation_reasons=%s"
        ),
        review.id,
        review.status,
        review.source,
        ticket_summary.get("total_legs"),
        (summary or {}).get("analysed_count"),
        user_ticket.get("overall_confidence_score"),
        user_ticket.get("estimated_success_percent"),
        user_ticket.get("combined_odds"),
        ai_ticket.get("overall_confidence_score"),
        ai_ticket.get("estimated_success_percent"),
        ai_ticket.get("combined_odds"),
        improvement.get("confidence_score_change"),
        improvement.get("success_probability_change"),
        improvement.get("picks_changed"),
        verdict.get("code"),
        counts,
        correlation,
        {
            "status": tracking.get("status"),
            "tracked": tracking.get("tracked_selections"),
            "untracked": tracking.get("untracked_selections"),
            "flagged_risky": tracking.get("flagged_risky_selections"),
        },
        (explanation.get("validation") or {}).get("ok"),
        (explanation.get("validation") or {}).get("reasons") or [],
    )

    untracked_by_id = {
        str(item.get("id") or ""): item.get("reasons") or []
        for item in tracking.get("untracked") or []
    }
    for index, selection in enumerate(public.get("selections") or [], start=1):
        user_pick = selection.get("user_pick") or selection.get("your_pick") or {}
        ai_pick = selection.get("ai_pick") or {}
        comparison = selection.get("comparison") or {}
        technical = selection.get("technical_ref") or {}
        market_consensus = selection.get("market_consensus") or {}
        assessment = selection.get("assessment") or {}
        selection_id = str(selection.get("id") or "")
        log.info(
            (
                "Slip review leg debug review=%s leg=%s id=%s match=%r market=%r state=%s family=%s "
                "verdict=%s risk=%s user_conf=%s user_label=%s user_prob=%s data_conf=%s "
                "ai_available=%s ai_market=%r ai_conf=%s ai_label=%s ai_prob=%s "
                "confidence_gain=%s ticket_lift=%s value=%s market_gap=%s disagreement=%s "
                "price_status=%s statpal_source=%s statpal_cache=%s statpal_coverage=%s "
                "statpal_required=%s statpal_missing=%s statpal_stale=%s statpal_snapshots=%s "
                "provider_merge=%s "
                "blocked_recommendations=%s warnings=%s tracking_reasons=%s bettor_verdict=%s recommendation=%s "
                "evidence_count=%s reason_codes=%s"
            ),
            review.id,
            index,
            selection_id,
            selection.get("match"),
            user_pick.get("market"),
            selection.get("state"),
            assessment.get("market_family"),
            (selection.get("verdict") or {}).get("code"),
            selection.get("risk_level"),
            user_pick.get("confidence_score"),
            user_pick.get("confidence_label"),
            user_pick.get("model_probability_percent"),
            user_pick.get("data_confidence_score"),
            ai_pick.get("available"),
            ai_pick.get("market"),
            ai_pick.get("confidence_score"),
            ai_pick.get("confidence_label"),
            ai_pick.get("model_probability_percent"),
            comparison.get("confidence_gain"),
            comparison.get("ticket_success_lift"),
            user_pick.get("value_rating"),
            market_consensus.get("probability_gap_points"),
            market_consensus.get("disagreement_level"),
            (selection.get("price_check") or {}).get("status"),
            technical.get("statpal_hydration_source"),
            technical.get("statpal_snapshot_cache_status"),
            technical.get("statpal_snapshot_coverage_percent"),
            technical.get("statpal_required_snapshot_types") or [],
            technical.get("statpal_missing_snapshot_types") or [],
            technical.get("statpal_stale_snapshot_types") or [],
            technical.get("statpal_snapshot_types") or [],
            technical.get("provider_merge") or {},
            technical.get("blocked_recommendation_markets") or [],
            technical.get("market_capability_warnings") or [],
            untracked_by_id.get(selection_id, []),
            user_pick.get("verdict"),
            (selection.get("recommendation") or {}).get("action"),
            len(selection.get("evidence") or []),
            selection.get("reason_codes") or [],
        )


def _populate_slip_review(review, results):
    safe_results = _json_safe(results)
    safe_results, _ = _slip_intelligence(safe_results)
    summary = _manual_review_summary(safe_results)
    review.status = _review_status_from_summary(summary)
    summary["bettor_public"] = _build_bettor_public_payload(
        review,
        (summary.get("public") or {}),
        enhance=True,
    )
    review.summary = summary
    review.save(update_fields=["status", "summary", "updated_at"])
    _log_slip_review_debug(review, summary)
    review.selections.all().delete()
    rows = []
    for index, item in enumerate(safe_results, start=1):
        matched = item.get("matched_fixture") or {}
        rows.append(
            SlipSelection(
                review=review,
                order=index,
                submitted_match=item.get("match", ""),
                submitted_market=item.get("submitted_market", ""),
                status=item.get("status", ""),
                verdict=item.get("verdict", ""),
                message=item.get("message", ""),
                match_id=matched.get("match_id") or "",
                match_date=matched.get("match_date") or None,
                fixture=matched.get("fixture") or "",
                home_team=matched.get("home_team") or "",
                away_team=matched.get("away_team") or "",
                league=matched.get("league") or "",
                country=matched.get("country") or "",
                kickoff=matched.get("kickoff") or "",
                selected_market=item.get("selected_market") or {},
                best_market=item.get("best_market") or {},
                recommended_market=item.get("recommended_market") or {},
                possible_matches=item.get("possible_matches") or [],
                analysis_payload=item,
                settlement_market=_settlement_market_for(item),
                odds=_decimal_or_none(_selection_original_odds(item)),
                flagged_risky=_selection_flagged_risky(item),
                advisory_score=_float_or_none(
                    item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score")
                ),
            )
        )
    SlipSelection.objects.bulk_create(rows, batch_size=100)
    return summary, safe_results


def _slip_selection_defaults_from_analysis(item):
    item = _json_safe(item or {})
    matched = item.get("matched_fixture") or {}
    return {
        "submitted_match": item.get("match", ""),
        "submitted_market": item.get("submitted_market") or item.get("market", ""),
        "status": item.get("status", ""),
        "verdict": item.get("verdict", ""),
        "message": item.get("message", ""),
        "match_id": matched.get("match_id") or "",
        "match_date": matched.get("match_date") or None,
        "fixture": matched.get("fixture") or "",
        "home_team": matched.get("home_team") or "",
        "away_team": matched.get("away_team") or "",
        "league": matched.get("league") or "",
        "country": matched.get("country") or "",
        "kickoff": matched.get("kickoff") or "",
        "selected_market": item.get("selected_market") or {},
        "best_market": item.get("best_market") or {},
        "recommended_market": item.get("recommended_market") or {},
        "possible_matches": item.get("possible_matches") or [],
        "analysis_payload": item,
        "settlement_market": _settlement_market_for(item),
        "odds": _decimal_or_none(_selection_original_odds(item)),
        "flagged_risky": _selection_flagged_risky(item),
        "advisory_score": _float_or_none(
            item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score")
        ),
    }


def _initial_slip_selection_payload(selection):
    provider_payload = _json_safe(selection.get("provider_payload") or {})
    market = selection.get("market", "")
    return {
        "match": selection.get("match", ""),
        "market": market,
        "submitted_market": market,
        "status": "queued",
        "verdict": "",
        "message": "Waiting for analysis.",
        "provider": selection.get("provider", ""),
        "provider_payload": provider_payload,
    }


def _initialize_slip_selection_progress_rows(review, selections):
    review.selections.all().delete()
    rows = []
    for index, selection in enumerate(selections, start=1):
        payload = _initial_slip_selection_payload(selection)
        defaults = _slip_selection_defaults_from_analysis(payload)
        rows.append(SlipSelection(review=review, order=index, **defaults))
    if rows:
        SlipSelection.objects.bulk_create(rows, batch_size=100)


def _persist_slip_selection_progress_result(review, index, result):
    defaults = _slip_selection_defaults_from_analysis(result)
    updated = SlipSelection.objects.filter(review=review, order=index + 1).update(**defaults)
    if not updated:
        SlipSelection.objects.create(review=review, order=index + 1, **defaults)


def _create_slip_review(user, *, source, submitted_payload, results):
    review = SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.ANALYSING,
        title=f"{source.title()} review",
        submitted_payload=_json_safe(submitted_payload),
        summary=_empty_slip_summary("Slip analysis started."),
    )
    summary, safe_results = _populate_slip_review(review, results)
    return review, summary, safe_results


def _empty_slip_summary(verdict, *, task_id="", error=""):
    summary = {
        "count": 0,
        "analysed_count": 0,
        "keep_count": 0,
        "caution_count": 0,
        "replace_count": 0,
        "remove_count": 0,
        "expired_count": 0,
        "unmatched_count": 0,
        "pending_analysis_count": 0,
        "api_usage": _empty_api_usage(),
        "intelligence": {
            "overall_score": 0,
            "risk_level": "medium" if not error else "high",
            "verdict": verdict,
            "api_usage": _empty_api_usage(),
            "original_combined_odds": None,
            "suggested_combined_odds": None,
            "strongest_legs": [],
            "weakest_legs": [],
            "legs_to_keep": [],
            "legs_to_caution": [],
            "legs_to_replace": [],
            "legs_to_remove": [],
            "expired_legs": [],
            "unverified_legs": [],
        },
    }
    if task_id:
        summary["task_id"] = task_id
    if error:
        summary["error"] = str(error)
    return summary


def _slip_review_progress(*, phase, total=0, completed=0, message="", **extra):
    total = max(0, int(total or 0))
    completed = max(0, min(int(completed or 0), total)) if total else max(0, int(completed or 0))
    percent = round((completed / total) * 100, 1) if total else (100.0 if phase in {"completed", "failed"} else 0.0)
    progress = {
        "phase": str(phase or ""),
        "total": total,
        "completed": completed,
        "percent": percent,
        "message": str(message or ""),
        "updated_at": timezone.now().isoformat(),
    }
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            progress[key] = _json_safe(value)
    return progress


def _publish_slip_review_event(review, event_type, payload=None):
    payload = _json_safe(payload or {})
    try:
        event = SlipReviewEvent.objects.create(
            review=review,
            event_type=str(event_type or ""),
            payload=payload,
        )
        log.info(
            "Slip review event review=%s event=%s event_id=%s payload=%s",
            review.id,
            event_type,
            event.id,
            payload,
        )
        if getattr(settings, "ENABLE_WEBSOCKETS", False):
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer

                channel_layer = get_channel_layer()
                if channel_layer:
                    async_to_sync(channel_layer.group_send)(
                        f"slip_review_{review.id}",
                        {
                            "type": "slip_review.event",
                            "payload": {
                                "type": "slip_review.event",
                                "id": event.id,
                                "review_id": review.id,
                                "event_type": event.event_type,
                                "payload": event.payload or {},
                                "created_at": event.created_at.isoformat() if event.created_at else "",
                            },
                        },
                    )
            except Exception:
                log.exception(
                    "Slip review websocket publish failed review=%s event=%s event_id=%s",
                    review.id,
                    event_type,
                    event.id,
                )
        return event
    except Exception:
        log.exception("Slip review event publish failed review=%s event=%s", getattr(review, "id", None), event_type)
        return None


def _set_slip_review_progress(review, *, phase, total=0, completed=0, message="", status=None, save=True, **extra):
    summary = dict(review.summary or {})
    summary["progress"] = _slip_review_progress(
        phase=phase,
        total=total,
        completed=completed,
        message=message,
        **extra,
    )
    review.summary = summary
    if status:
        review.status = status
    if save:
        fields = ["summary", "updated_at"]
        if status:
            fields.insert(0, "status")
        review.save(update_fields=fields)
        _publish_slip_review_event(
            review,
            "review.progress",
            {
                "status": review.status,
                "progress": summary["progress"],
            },
        )
    return summary["progress"]


def _create_queued_slip_review(user, *, source, submitted_payload):
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.QUEUED,
        title=f"{source.title()} review",
        submitted_payload=_json_safe(submitted_payload),
        summary={
            **_empty_slip_summary("Slip import queued."),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        },
    )


def _create_failed_slip_review(user, *, source, submitted_payload, error):
    summary = _empty_slip_summary("Slip import failed.", error=error)
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.FAILED,
        title=f"{source.title()} review",
        submitted_payload=_json_safe(submitted_payload),
        summary=summary,
    )


def _slip_selection_payload(selection):
    payload = dict(selection.analysis_payload or {})
    payload.setdefault("match", selection.submitted_match)
    payload.setdefault("submitted_market", selection.submitted_market)
    payload.setdefault("status", selection.status)
    payload.setdefault("verdict", selection.verdict)
    payload.setdefault("message", selection.message)
    return payload


def _slip_review_payload(review, *, include_selections=True, public_only=False):
    summary = review.summary or {}
    public_payload = summary.get("public") or (summary.get("intelligence") or {}).get("public", {})
    latest_event_id = (
        review.events.order_by("-id").values_list("id", flat=True).first()
        if hasattr(review, "events")
        else None
    )
    if public_only:
        if review.status in {
            SlipReview.Status.QUEUED,
            SlipReview.Status.IMPORTING,
            SlipReview.Status.ANALYSING,
        }:
            progress = (summary or {}).get("progress") or _slip_review_progress(
                phase=review.status,
                message=f"Slip review is {review.status}.",
            )
            return _api_response_payload(
                {
                    "id": review.id,
                    "source": review.source,
                    "status": review.status,
                    "created_at": review.created_at,
                    "updated_at": review.updated_at,
                    "progress": progress,
                    "latest_event_id": latest_event_id,
                }
            )
        bettor_payload = summary.get("bettor_public") or _build_bettor_public_payload(
            review,
            public_payload,
            enhance=False,
        )
        if bettor_payload.get("status") != review.status:
            bettor_payload = _build_bettor_public_payload(
                review,
                public_payload,
                enhance=False,
            )
        return _api_response_payload(bettor_payload)
    payload = {
        "id": review.id,
        "source": review.source,
        "status": review.status,
        "title": review.title,
        "summary": summary,
        "public": public_payload,
        "intelligence": summary.get("intelligence", {}),
        "created_at": review.created_at,
        "updated_at": review.updated_at,
        "latest_event_id": latest_event_id,
    }
    if include_selections:
        payload["selections"] = [
            _slip_selection_payload(selection)
            for selection in review.selections.all().order_by("order", "id")
        ]
    return _api_response_payload(payload)


def _provider_match_date(selection):
    provider_payload = selection.get("provider_payload") or {}
    kickoff_ms = provider_payload.get("kickoff_ms")
    if kickoff_ms in (None, ""):
        nested = provider_payload.get("provider_payload") or {}
        kickoff_ms = ((nested.get("outcome") or {}).get("estimateStartTime"))
        if kickoff_ms in (None, ""):
            kickoff_ms = ((nested.get("leg") or {}).get("eventStartTime"))
    try:
        if kickoff_ms in (None, ""):
            return None
        return datetime.fromtimestamp(float(kickoff_ms) / 1000, tz=timezone.get_current_timezone()).date()
    except (TypeError, ValueError, OSError):
        return None


def _provider_kickoff_datetime(selection):
    provider_payload = selection.get("provider_payload") or {}
    kickoff_ms = provider_payload.get("kickoff_ms")
    if kickoff_ms in (None, ""):
        nested = provider_payload.get("provider_payload") or {}
        kickoff_ms = ((nested.get("outcome") or {}).get("estimateStartTime"))
        if kickoff_ms in (None, ""):
            kickoff_ms = ((nested.get("leg") or {}).get("eventStartTime"))
    try:
        if kickoff_ms in (None, ""):
            return None
        return datetime.fromtimestamp(float(kickoff_ms) / 1000, tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OSError):
        return None


def _provider_event_status(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    status = str(outcome.get("status") if outcome.get("status") is not None else "").strip()
    match_status = str(outcome.get("matchStatus") or "").strip().lower()
    return status, match_status


def _provider_metadata(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    sport = outcome.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    provider_competition_id = str(tournament.get("id") or "")
    return {
        "provider": selection.get("provider") or provider_payload.get("provider") or "",
        "provider_event_id": provider_payload.get("provider_event_id") or outcome.get("eventId") or "",
        "provider_competition_id": provider_competition_id,
        "competition": provider_payload.get("competition") or tournament.get("name") or "",
        "home_team": provider_payload.get("home_team") or outcome.get("homeTeamName") or "",
        "away_team": provider_payload.get("away_team") or outcome.get("awayTeamName") or "",
    }


def _sportybet_statpal_event(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    event = dict(outcome) if isinstance(outcome, dict) else {}
    event.setdefault("eventId", provider_payload.get("provider_event_id") or "")
    event.setdefault("homeTeamName", provider_payload.get("home_team") or "")
    event.setdefault("awayTeamName", provider_payload.get("away_team") or "")
    event.setdefault("estimateStartTime", provider_payload.get("kickoff_ms") or "")
    if provider_payload.get("competition") and not event.get("sport"):
        event["sport"] = {"category": {"tournament": {"name": provider_payload.get("competition")}}}
    return event


def _try_sportybet_statpal_mapping(selection, *, provider_date, resolver_trace):
    provider_payload = selection.get("provider_payload") or {}
    provider_event_id = str(provider_payload.get("provider_event_id") or "").strip()
    if str(selection.get("provider") or "").lower() != "sportybet" or not provider_event_id:
        return None

    search_service = FixtureSearchService()
    sync_result = {}
    if provider_date:
        try:
            sync_result = search_service.sync_statpal_daily(provider_date)
        except Exception as exc:
            sync_result = {"synced": 0, "errors": [str(exc)]}

    try:
        result = provider_mapping_service.match_sportybet_to_statpal(_sportybet_statpal_event(selection))
    except Exception as exc:
        result = {"matched": False, "reason": "sportybet_statpal_mapping_error", "error": str(exc)}

    resolver_trace.append(
        {
            "strategy": "sportybet_statpal_mapping",
            "synced": sync_result.get("synced", 0),
            "sync_errors": sync_result.get("errors", []),
            "matched": bool(result.get("matched")),
            "reason": result.get("reason", ""),
            "candidate_match_id": ((result.get("candidate") or {}).get("match_id") if isinstance(result.get("candidate"), dict) else ""),
            "candidate_score": ((result.get("candidate") or {}).get("match_score") if isinstance(result.get("candidate"), dict) else None),
        }
    )
    return result


def _selection_expiry(selection):
    status, match_status = _provider_event_status(selection)
    terminal_statuses = {"ended", "finished", "cancelled", "canceled", "postponed", "abandoned"}
    if status in {"3", "4", "5"} or match_status in terminal_statuses:
        return {
            "expired": True,
            "reason": "provider_event_not_reviewable",
            "message": "This event has already ended or is not available for pre-match review.",
        }
    kickoff_at = _provider_kickoff_datetime(selection)
    if kickoff_at and kickoff_at <= timezone.now():
        return {
            "expired": True,
            "reason": "kickoff_already_passed",
            "message": "This event has already started and cannot be reviewed as a pre-match selection.",
        }
    return {"expired": False}


def _analyse_manual_selection(
    selection,
    *,
    days,
    request=None,
    force_fresh=False,
    hydration_cache=None,
    review_scoring_context=None,
    allow_on_demand_scoring=True,
):
    match_text = selection.get("match", "")
    requested_market = selection.get("market", "")
    market_descriptor = _selection_market_descriptor(selection, requested_market)
    market_taxonomy = market_descriptor.to_dict()
    provider_date = _provider_match_date(selection)
    provider_kickoff = _provider_kickoff_datetime(selection)
    provider_metadata = _provider_metadata(selection)
    expiry = _selection_expiry(selection)
    resolver_trace = [
        {
            "strategy": "provider_metadata",
            "provider_date": provider_date.isoformat() if provider_date else "",
            "provider_kickoff": provider_kickoff.isoformat() if provider_kickoff else "",
            "competition": provider_metadata.get("competition") or "",
            "provider_event_id": provider_metadata.get("provider_event_id") or "",
            "provider_competition_id": provider_metadata.get("provider_competition_id") or "",
            "expired": expiry.get("expired", False),
            "expiry_reason": expiry.get("reason", ""),
        }
    ]
    if expiry.get("expired"):
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "expired",
            "verdict": "expired",
            "message": expiry.get("message"),
            "fixture_resolution": {
                "status": "expired",
                "attempts": resolver_trace,
            },
            "possible_matches": [],
        }

    search_service = FixtureSearchService()
    statpal_mapping_result = _try_sportybet_statpal_mapping(selection, provider_date=provider_date, resolver_trace=resolver_trace)
    statpal_candidate = (statpal_mapping_result or {}).get("candidate") if isinstance(statpal_mapping_result, dict) else {}
    provider_fixture = search_service.get_provider_fixture(
        provider=provider_metadata.get("provider"),
        provider_event_id=provider_metadata.get("provider_event_id"),
    )
    if provider_fixture and (provider_fixture.get("fixture") or {}).get("source") == "statpal":
        statpal_candidate = statpal_candidate or provider_fixture.get("fixture") or {}
        resolver_trace.append(
            {
                "strategy": "provider_fixture_map_statpal_context",
                "mapping_id": provider_fixture.get("mapping_id"),
                "candidate_match_ids": [provider_fixture["fixture"].get("match_id")],
            }
        )
        provider_fixture = None
    if provider_fixture:
        candidates = [provider_fixture["fixture"]]
        resolver_trace.append(
            {
                "strategy": "provider_fixture_map",
                "mapping_id": provider_fixture.get("mapping_id"),
                "candidate_count": 1,
                "candidate_match_ids": [provider_fixture["fixture"].get("match_id")],
            }
        )
    else:
        search = search_service.search(match_text, days=days, limit=5)
        candidates = search.get("results") or []
        resolver_trace.append(
            {
                "strategy": "local_or_default_window",
                "candidate_count": len(candidates),
                "refreshed": search.get("refreshed", False),
                "refresh_errors": search.get("refresh_errors", []),
                "candidate_match_ids": [candidate.get("match_id") for candidate in candidates],
            }
        )
    best_score = float((candidates[0] if candidates else {}).get("match_score") or 0)
    if not provider_fixture and provider_date and best_score < 70:
        search = search_service.search(
            match_text,
            start_date=max(provider_date - timedelta(days=2), timezone.localdate()),
            days=4,
            limit=5,
            refresh=True,
            unrestricted=True,
        )
        candidates = search.get("results") or candidates
        resolver_trace.append(
            {
                "strategy": "provider_date_unrestricted_refresh",
                "candidate_count": len(search.get("results") or []),
                "refreshed": search.get("refreshed", False),
                "refresh_errors": search.get("refresh_errors", []),
                "candidate_match_ids": [candidate.get("match_id") for candidate in (search.get("results") or [])],
            }
        )
        best_score = float((candidates[0] if candidates else {}).get("match_score") or 0)
    if not provider_fixture and provider_date and best_score < 70:
        provider_search = search_service.search_provider_fixture(
            match_text,
            provider_date=provider_date,
            competition=provider_metadata.get("competition") or "",
            provider=provider_metadata.get("provider") or "",
            provider_competition_id=provider_metadata.get("provider_competition_id") or "",
            limit=5,
        )
        candidates = provider_search.get("results") or candidates
        resolver_trace.extend(provider_search.get("trace") or [])
    if not candidates:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "unmatched",
            "verdict": "unmatched",
            "message": "We could not find this fixture in the upcoming fixture cache or API-Football search window.",
            "fixture_resolution": {
                "status": "unmatched",
                "attempts": resolver_trace,
            },
            "possible_matches": [],
        }

    candidate = candidates[0]
    if float(candidate.get("match_score") or 0) < 70:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "ambiguous_match",
            "verdict": "unmatched",
            "message": "We found possible fixtures, but none were clear enough to analyse automatically.",
            "fixture_resolution": {
                "status": "ambiguous_match",
                "attempts": resolver_trace,
            },
            "possible_matches": candidates,
        }
    if not statpal_candidate:
        statpal_candidate = search_service.find_statpal_fixture_context(candidate)
        if statpal_candidate:
            resolver_trace.append(
                {
                    "strategy": "statpal_context_from_resolved_fixture",
                    "candidate_match_id": statpal_candidate.get("match_id"),
                    "provider_match_id": statpal_candidate.get("provider_match_id") or statpal_candidate.get("statpal_provider_match_id"),
                    "candidate_score": statpal_candidate.get("match_score"),
                }
            )
    if str(provider_metadata.get("provider") or "").lower() != "sportybet" or provider_fixture:
        search_service.learn_resolution(
            provider_metadata=provider_metadata,
            candidate=candidate,
            confidence=candidate.get("match_score"),
            method="provider_fixture_map" if provider_fixture else "team_date_league",
        )

    on_demand = None
    skip_core_on_demand = _market_can_skip_core_on_demand(market_descriptor)
    if skip_core_on_demand:
        game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
        if not game:
            if _should_skip_core_on_demand(
                market_descriptor,
                game=game,
                candidate=candidate,
                statpal_candidate=statpal_candidate,
                provider_metadata=provider_metadata,
            ):
                game = _minimal_game_from_candidate(candidate)
                on_demand = {
                    "status": "skipped",
                    "reason": "market_served_by_match_checker_advisory",
                    "market_family": market_descriptor.family,
                }
            else:
                effective_force_fresh = _consume_review_force_fresh(review_scoring_context)
                on_demand = algo_runner_service.score_cached_fixture_on_demand(
                    candidate["match_id"],
                    match_date=candidate.get("match_date"),
                    reason="slip_review_market_context",
                    force=effective_force_fresh,
                )
                game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
                if not game:
                    game = _minimal_game_from_candidate(candidate)
    elif force_fresh:
        effective_force_fresh = _consume_review_force_fresh(review_scoring_context)
        on_demand = algo_runner_service.score_cached_fixture_on_demand(
            candidate["match_id"],
            match_date=candidate.get("match_date"),
            reason="slip_review",
            force=effective_force_fresh,
        )
        game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
    else:
        game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
        if not game and allow_on_demand_scoring:
            on_demand = algo_runner_service.score_cached_fixture_on_demand(
                candidate["match_id"],
                match_date=candidate.get("match_date"),
                reason="slip_review",
            )
            game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)

    if not game:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "matched_unscored",
            "verdict": "pending_analysis",
            "message": "Fixture matched, but on-demand analysis could not produce market predictions yet.",
            "matched_fixture": candidate,
            "possible_matches": candidates,
            "on_demand_analysis": on_demand,
            "fixture_resolution": {
                "status": "matched_unscored",
                "attempts": resolver_trace,
            },
        }

    markets = game.get("markets") or []
    statpal_provider_match_id = ""
    statpal_provider_competition_id = provider_metadata.get("provider_competition_id") or ""
    if str(provider_metadata.get("provider") or "").lower() == "statpal":
        statpal_provider_match_id = provider_metadata.get("provider_event_id") or ""
    elif isinstance(statpal_candidate, dict):
        statpal_provider_match_id = statpal_candidate.get("provider_match_id") or str(statpal_candidate.get("match_id") or "").replace("statpal:", "", 1)
        statpal_provider_competition_id = statpal_candidate.get("provider_competition_id") or statpal_provider_competition_id
    statpal_home_team_id = (statpal_candidate.get("home_team_id") if isinstance(statpal_candidate, dict) else "") or game.get("statpal_home_team_id") or ""
    statpal_away_team_id = (statpal_candidate.get("away_team_id") if isinstance(statpal_candidate, dict) else "") or game.get("statpal_away_team_id") or ""
    scoring_game = {
        **game,
        "statpal_provider_match_id": statpal_provider_match_id,
        "statpal_provider_competition_id": statpal_provider_competition_id,
        "statpal_home_team_id": statpal_home_team_id,
        "statpal_away_team_id": statpal_away_team_id,
        "provider_merge": game.get("provider_merge") or {},
    }
    matched_fixture_payload = _matched_fixture_with_statpal(
        candidate,
        scoring_game,
        statpal_candidate,
        provider_match_id=statpal_provider_match_id,
        provider_competition_id=statpal_provider_competition_id,
        home_team_id=statpal_home_team_id,
        away_team_id=statpal_away_team_id,
    )
    hydrator = hydration_cache or FixtureHydrator()
    statpal_bundle = hydrator.bundle_for(
        market_descriptor,
        match_id=(statpal_candidate.get("match_id") if isinstance(statpal_candidate, dict) and statpal_candidate.get("match_id") else candidate.get("match_id")),
        provider_match_id=statpal_provider_match_id,
        provider_competition_id=statpal_provider_competition_id,
        home_team_id=statpal_home_team_id,
        away_team_id=statpal_away_team_id,
    )
    statpal_refresh = statpal_bundle.get("refreshed") or {}
    statpal_context = statpal_bundle.get("context") or {}

    # Snapshot coverage is only the right yardstick for the StatPal advisory path;
    # matrix- and count-model markets are judged on the data that actually serves them.
    market_capability = capability_for_descriptor(
        market_descriptor, fixture=scoring_game, statpal_context=statpal_context
    )
    statpal_advisory = statpal_market_advisory.evaluate_market(
        market_descriptor,
        fixture={**scoring_game, "statpal_context": statpal_context},
        provider_payload=selection.get("provider_payload") or {},
        statpal_payload=selection.get("statpal_payload"),
    )
    market_capability = _effective_market_capability(market_capability, statpal_advisory)
    generated_markets = _generated_match_checker_markets(
        market_descriptor,
        game=scoring_game,
        statpal_context=statpal_context,
        provider_payload=selection.get("provider_payload") or {},
        statpal_payload=selection.get("statpal_payload"),
    )
    canonical_requested_market = market_descriptor.canonical
    analysis_market = _market_for_fixture_orientation(canonical_requested_market, candidate)
    selected_market = next((market for market in markets if _market_matches(analysis_market, market.get("market"))), None)
    blocked_recommendation_markets = []
    if not selected_market:
        submitted_market = _submitted_market_payload(
            requested_market=requested_market,
            market_taxonomy=market_taxonomy,
            statpal_advisory=statpal_advisory,
            market_capability=market_capability,
        )
        replacement_market = _replacement_market_for_slip(
            game,
            selected_market=submitted_market,
            generated_markets=generated_markets,
            allow_safer_fallback=True,
            blocked_markets_out=blocked_recommendation_markets,
        )
        verdict = _manual_verdict(submitted_market, replacement_market)
        # A market priced by a fitted model is not "not found" merely because the core
        # algo did not enumerate it. Since most families are now served by the score
        # matrix or the count models, that list is often empty by design — reporting
        # those legs as unmatched hid perfectly good assessments.
        model_served = _market_was_assessed(submitted_market)
        resolution_status = "analysed" if model_served else "market_not_found"
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "provider_market_text": selection.get("provider_market_text") or requested_market,
            "canonical_market": _resolved_canonical_market(selection),
            "market_taxonomy": market_taxonomy,
            "analysis_market": analysis_market,
            "fixture_orientation": candidate.get("match_orientation", ""),
            "status": resolution_status,
            **verdict,
            "matched_fixture": matched_fixture_payload,
            "provider_merge": matched_fixture_payload.get("provider_merge") or {},
            "available_markets": [market.get("market") for market in markets],
            "selected_market": submitted_market,
            "best_market": game.get("best_market"),
            "recommended_market": game.get("recommended_market"),
            "replacement_market": replacement_market,
            "blocked_recommendation_markets": blocked_recommendation_markets,
            "generated_markets": generated_markets,
            "fixture_resolution": {
                "status": resolution_status,
                "attempts": resolver_trace,
            },
            "statpal_refresh": statpal_refresh,
            "statpal_context": statpal_context,
            "statpal_advisory": statpal_advisory,
            "market_capability": market_capability,
        }

    best_market = game.get("best_market") or game.get("top_market")
    recommended_market = game.get("recommended_market")
    selected_market = _with_match_checker_advisory(selected_market)
    if selected_market:
        selected_market["market_taxonomy"] = market_taxonomy
        selected_market = _with_statpal_advisory(selected_market, statpal_advisory)
        selected_market = _with_market_capability(selected_market, market_capability)
    best_market = _with_match_checker_advisory(best_market)
    recommended_market = _with_match_checker_advisory(recommended_market)
    replacement_market = _replacement_market_for_slip(
        game,
        selected_market=selected_market,
        generated_markets=generated_markets,
        allow_safer_fallback=True,
        blocked_markets_out=blocked_recommendation_markets,
    )
    verdict = _manual_verdict(selected_market, replacement_market)
    return {
        "match": match_text,
        "submitted_market": requested_market,
        "provider_market_text": selection.get("provider_market_text") or requested_market,
        "canonical_market": _resolved_canonical_market(selection),
        "market_taxonomy": market_taxonomy,
        "analysis_market": analysis_market,
        "fixture_orientation": candidate.get("match_orientation", ""),
        "status": "analysed",
        **verdict,
        "matched_fixture": matched_fixture_payload,
        "provider_merge": matched_fixture_payload.get("provider_merge") or {},
        "selected_market": selected_market,
        "best_market": best_market,
        "recommended_market": recommended_market,
        "replacement_market": replacement_market,
        "blocked_recommendation_markets": blocked_recommendation_markets,
        "generated_markets": generated_markets,
        "statpal_refresh": statpal_refresh,
        "statpal_context": statpal_context,
        "statpal_advisory": statpal_advisory,
        "market_capability": market_capability,
        "possible_matches": candidates,
        "on_demand_analysis": on_demand,
        "fixture_resolution": {
            "status": "matched",
            "attempts": resolver_trace,
        },
    }


def _slip_review_completed_leg_count(review):
    return review.selections.exclude(status__in=["queued", "analysing", ""]).count()


def _slip_review_leg_failure_result(index, selection, message, *, error_code="analysis_failed"):
    provider_payload = _json_safe((selection or {}).get("provider_payload") or {})
    return {
        "match": (selection or {}).get("match", ""),
        "submitted_market": (selection or {}).get("market", ""),
        "market_taxonomy": _selection_market_descriptor(selection or {}, (selection or {}).get("market", "")).to_dict(),
        "status": "analysis_failed",
        "verdict": "not_assessed",
        "message": str(message or "Slip leg analysis failed."),
        "provider": (selection or {}).get("provider", ""),
        "provider_payload": provider_payload,
        "fixture_resolution": {
            "status": "analysis_failed",
            "attempts": [
                {
                    "strategy": "celery_leg_task",
                    "error_code": error_code,
                    "index": index,
                }
            ],
        },
        "possible_matches": [],
    }


def _slip_leg_analysis_cache_key(selection):
    selection = selection or {}
    provider_payload = selection.get("provider_payload") or {}
    provider_metadata = _provider_metadata(selection)
    descriptor = _selection_market_descriptor(selection, selection.get("market", ""))
    market_key = (
        getattr(descriptor, "code", "")
        or getattr(descriptor, "canonical", "")
        or selection.get("market")
        or ""
    )
    raw_key = {
        "provider": str(selection.get("provider") or provider_metadata.get("provider") or "").lower(),
        "provider_event_id": provider_metadata.get("provider_event_id") or "",
        "provider_competition_id": provider_metadata.get("provider_competition_id") or "",
        "provider_date": _provider_match_date(selection).isoformat() if _provider_match_date(selection) else "",
        "match": normalize_market_text(selection.get("match") or ""),
        "market": normalize_market_text(market_key),
        "odds": str(provider_payload.get("odds") or provider_payload.get("displayOdds") or ""),
        "market_id": str(provider_payload.get("marketId") or provider_payload.get("market_id") or ""),
        "outcome_id": str(provider_payload.get("outcomeId") or provider_payload.get("outcome_id") or ""),
        "specifier": str(provider_payload.get("specifier") or ""),
    }
    encoded = json.dumps(raw_key, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), raw_key


def _cached_slip_leg_payload(cached, cache_key, *, status="hit"):
    payload = dict(cached.payload or {})
    payload["analysis_cache"] = {
        "status": status,
        "cache_key": cache_key,
        "updated_at": cached.updated_at.isoformat() if cached.updated_at else "",
        "expires_at": cached.expires_at.isoformat() if cached.expires_at else "",
    }
    return payload


def _get_or_lock_slip_leg_analysis_cache(selection):
    cache_key, raw_key = _slip_leg_analysis_cache_key(selection)
    now = timezone.now()
    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if (
        cached
        and cached.status == SlipLegAnalysisCache.Status.READY
        and cached.expires_at > now
        and cached.payload
    ):
        return _cached_slip_leg_payload(cached, cache_key), cache_key, raw_key, False

    lock_until = now + timedelta(seconds=max(30, SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS))
    expires_at = now + timedelta(seconds=max(60, SLIP_REVIEW_LEG_CACHE_TTL_SECONDS))
    if not cached:
        try:
            SlipLegAnalysisCache.objects.create(
                cache_key=cache_key,
                status=SlipLegAnalysisCache.Status.PROCESSING,
                source=raw_key.get("provider") or "",
                provider_event_id=raw_key.get("provider_event_id") or "",
                match_text=(selection or {}).get("match") or "",
                market_text=(selection or {}).get("market") or "",
                payload={},
                expires_at=expires_at,
                lock_expires_at=lock_until,
            )
            return None, cache_key, raw_key, True
        except IntegrityError:
            cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()

    now = timezone.now()
    if cached and cached.status == SlipLegAnalysisCache.Status.PROCESSING and cached.lock_expires_at and cached.lock_expires_at > now:
        deadline = time.monotonic() + max(0, SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS)
        while time.monotonic() < deadline:
            time.sleep(1)
            cached.refresh_from_db()
            if cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
                return _cached_slip_leg_payload(cached, cache_key, status="wait_hit"), cache_key, raw_key, False

    updated = SlipLegAnalysisCache.objects.filter(
        cache_key=cache_key,
    ).filter(
        Q(lock_expires_at__lte=timezone.now()) | Q(lock_expires_at__isnull=True) | Q(status__in=[
            SlipLegAnalysisCache.Status.READY,
            SlipLegAnalysisCache.Status.FAILED,
        ])
    ).update(
        status=SlipLegAnalysisCache.Status.PROCESSING,
        lock_expires_at=lock_until,
        expires_at=expires_at,
    )
    if updated:
        return None, cache_key, raw_key, True

    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if cached and cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
        return _cached_slip_leg_payload(cached, cache_key, status="late_hit"), cache_key, raw_key, False
    return None, cache_key, raw_key, True


def _store_slip_leg_analysis_cache(selection, result, *, cache_key=None, raw_key=None):
    result = _json_safe(result or {})
    if result.get("status") not in {"analysed", "market_not_found", "insufficient_data"}:
        return
    cache_key = cache_key or _slip_leg_analysis_cache_key(selection)[0]
    raw_key = raw_key or _slip_leg_analysis_cache_key(selection)[1]
    matched = result.get("matched_fixture") or {}
    expires_at = timezone.now() + timedelta(seconds=max(60, SLIP_REVIEW_LEG_CACHE_TTL_SECONDS))
    SlipLegAnalysisCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "status": SlipLegAnalysisCache.Status.READY,
            "source": raw_key.get("provider") or "",
            "provider_event_id": raw_key.get("provider_event_id") or "",
            "match_text": result.get("match") or (selection or {}).get("match") or "",
            "market_text": result.get("submitted_market") or (selection or {}).get("market") or "",
            "match_id": matched.get("match_id") or "",
            "payload": result,
            "expires_at": expires_at,
            "lock_expires_at": None,
        },
    )


def _mark_slip_leg_analysis_cache_failed(selection, *, cache_key=None):
    cache_key = cache_key or _slip_leg_analysis_cache_key(selection)[0]
    SlipLegAnalysisCache.objects.filter(cache_key=cache_key).update(
        status=SlipLegAnalysisCache.Status.FAILED,
        lock_expires_at=None,
        expires_at=timezone.now() + timedelta(seconds=60),
    )


def process_slip_review_leg_failure(review_id, index, selection, message, *, error_code="analysis_failed"):
    review = SlipReview.objects.get(id=review_id)
    _mark_slip_leg_analysis_cache_failed(selection)
    result = _slip_review_leg_failure_result(index, selection, message, error_code=error_code)
    _persist_slip_selection_progress_result(review, index, result)
    total = review.selections.count()
    completed = _slip_review_completed_leg_count(review)
    _set_slip_review_progress(
        review,
        phase="analysing_legs",
        total=total,
        completed=completed,
        message=f"Analysed {completed} of {total} selections.",
        last_completed_match=result.get("match") or (selection or {}).get("match"),
        last_error=str(message or ""),
    )
    _publish_slip_review_event(
        review,
        "leg.failed",
        {
            "index": index,
            "order": index + 1,
            "match": result.get("match") or (selection or {}).get("match"),
            "market": result.get("submitted_market") or (selection or {}).get("market"),
            "game": _streamed_slip_review_game_payload(review, index, result).get("game"),
            "error": str(message or ""),
            "error_code": error_code,
            "completed": completed,
            "total": total,
        },
    )
    log.warning(
        "Slip review leg failed review=%s leg=%s match=%r market=%r error_code=%s error=%s",
        review_id,
        index + 1,
        (selection or {}).get("match"),
        (selection or {}).get("market"),
        error_code,
        message,
    )
    return {"review_id": review_id, "index": index, "status": "failed", "result": result, "error": str(message or "")}


def process_slip_review_leg_analysis(review_id, index, selection, *, days=3):
    review = SlipReview.objects.get(id=review_id)
    SlipSelection.objects.filter(review=review, order=index + 1).update(
        status="analysing",
        message="Analysing this selection.",
        analysis_payload={
            **_initial_slip_selection_payload(selection or {}),
            "status": "analysing",
            "message": "Analysing this selection.",
        },
    )
    total = review.selections.count()
    _publish_slip_review_event(
        review,
        "leg.started",
        {
            "index": index,
            "order": index + 1,
            "match": (selection or {}).get("match"),
            "market": (selection or {}).get("market"),
            "completed": _slip_review_completed_leg_count(review),
            "total": total,
        },
    )
    cached, cache_key, raw_key, owns_cache_lock = _get_or_lock_slip_leg_analysis_cache(selection)
    if cached:
        cached["provider"] = review.source
        cached["provider_payload"] = _json_safe((selection or {}).get("provider_payload") or {})
        _persist_slip_selection_progress_result(review, index, cached)
        total = review.selections.count()
        completed = _slip_review_completed_leg_count(review)
        _set_slip_review_progress(
            review,
            phase="analysing_legs",
            total=total,
            completed=completed,
            message=f"Analysed {completed} of {total} selections.",
            last_completed_match=cached.get("match") or (selection or {}).get("match"),
            cache_status="hit",
        )
        _publish_slip_review_event(
            review,
            "leg.completed",
            {
                "index": index,
                "order": index + 1,
                "match": cached.get("match") or (selection or {}).get("match"),
                "market": cached.get("submitted_market") or (selection or {}).get("market"),
                "status": cached.get("status"),
                "verdict": cached.get("verdict"),
                "cache_status": (cached.get("analysis_cache") or {}).get("status") or "hit",
                **_streamed_slip_review_game_payload(review, index, cached),
                "completed": completed,
                "total": total,
            },
        )
        log.info(
            "Slip review leg cache hit review=%s leg=%s match=%r market=%r cache_key=%s",
            review_id,
            index + 1,
            (selection or {}).get("match"),
            (selection or {}).get("market"),
            cache_key,
        )
        return {
            "review_id": review_id,
            "index": index,
            "status": "cache_hit",
            "result": cached,
            "hydration": {
                "calls_used": 0,
                "served_from_cache": 1,
                "served_from_snapshot_cache": 0,
                "snapshot_cache_misses": 0,
                "served_by_model": 0,
                "fixtures_hydrated": 0,
                "budget_exhausted": False,
            },
        }
    hydrator = FixtureHydrator()
    try:
        result = _analyse_manual_selection(
            selection or {},
            days=days,
            request=None,
            force_fresh=True,
            hydration_cache=hydrator,
            review_scoring_context={"fixture_universe_synced": index > 0},
            allow_on_demand_scoring=True,
        )
    except Exception:
        if owns_cache_lock:
            _mark_slip_leg_analysis_cache_failed(selection, cache_key=cache_key)
        raise
    result["provider"] = review.source
    result["provider_payload"] = _json_safe((selection or {}).get("provider_payload") or {})
    result["analysis_cache"] = {"status": "miss", "cache_key": cache_key}
    if owns_cache_lock:
        _store_slip_leg_analysis_cache(selection, result, cache_key=cache_key, raw_key=raw_key)
    _persist_slip_selection_progress_result(review, index, result)
    total = review.selections.count()
    completed = _slip_review_completed_leg_count(review)
    _set_slip_review_progress(
        review,
        phase="analysing_legs",
        total=total,
        completed=completed,
        message=f"Analysed {completed} of {total} selections.",
        last_completed_match=result.get("match") or (selection or {}).get("match"),
    )
    _publish_slip_review_event(
        review,
        "leg.completed",
        {
            "index": index,
            "order": index + 1,
            "match": result.get("match") or (selection or {}).get("match"),
            "market": result.get("submitted_market") or (selection or {}).get("market"),
            "status": result.get("status"),
            "verdict": result.get("verdict"),
            "cache_status": "miss",
            **_streamed_slip_review_game_payload(review, index, result),
            "completed": completed,
            "total": total,
        },
    )
    log.info(
        "Slip review leg analysed review=%s leg=%s match=%r status=%s hydration=%s",
        review_id,
        index + 1,
        result.get("match") or (selection or {}).get("match"),
        result.get("status"),
        hydrator.stats.to_dict(),
    )
    return {
        "review_id": review_id,
        "index": index,
        "status": "analysed",
        "result": result,
        "hydration": hydrator.stats.to_dict(),
    }


def finalize_slip_review_import_results(review_id, leg_results):
    review = SlipReview.objects.get(id=review_id)
    payload = review.submitted_payload or {}
    ordered = sorted(
        [item for item in leg_results or [] if isinstance(item, dict)],
        key=lambda item: int(item.get("index") or 0),
    )
    results = [item.get("result") for item in ordered if isinstance(item.get("result"), dict)]
    hydration_totals = {
        "calls_used": 0,
        "served_from_cache": 0,
        "served_from_snapshot_cache": 0,
        "snapshot_cache_misses": 0,
        "served_by_model": 0,
        "fixtures_hydrated": 0,
        "budget_exhausted": False,
    }
    for item in ordered:
        stats = item.get("hydration") or {}
        for key in (
            "calls_used",
            "served_from_cache",
            "served_from_snapshot_cache",
            "snapshot_cache_misses",
            "served_by_model",
            "fixtures_hydrated",
        ):
            hydration_totals[key] += int(stats.get(key) or 0)
        hydration_totals["budget_exhausted"] = bool(hydration_totals["budget_exhausted"] or stats.get("budget_exhausted"))

    log.info("Slip hydration done review=%s %s", review.id, hydration_totals)
    if not results:
        raise ValueError("No supported football selections were found in this slip.")

    summary, safe_results = _populate_slip_review(review, results)
    summary["progress"] = _slip_review_progress(
        phase="completed",
        total=len(results),
        completed=len(results),
        message="Slip review completed.",
        final_status=review.status,
    )
    review.summary = summary
    final_payload = _json_safe({**payload, "fanout_analysis": True})
    review.submitted_payload = final_payload
    final_updated_at = timezone.now()
    SlipReview.objects.filter(id=review.id).update(
        status=review.status,
        summary=review.summary,
        submitted_payload=final_payload,
        updated_at=final_updated_at,
    )
    review.updated_at = final_updated_at
    log.info(
        "Slip review final persisted review=%s status=%s fanout=True selections=%s",
        review.id,
        review.status,
        len(results),
    )
    _publish_slip_review_event(
        review,
        "review.completed",
        {
            "status": review.status,
            "total": len(results),
            "completed": len(results),
            "progress": summary.get("progress") or {},
        },
    )
    return _api_response_payload({"review_id": review.id, "status": review.status, **summary})


def _leg_results_from_persisted_slip_selections(review):
    leg_results = []
    for selection in review.selections.order_by("order"):
        payload = selection.analysis_payload or {}
        if payload.get("status") in {"queued", "analysing", ""}:
            continue
        leg_results.append(
            {
                "review_id": review.id,
                "index": max(0, int(selection.order or 1) - 1),
                "status": payload.get("status") or selection.status or "",
                "result": payload,
                "hydration": {},
            }
        )
    return leg_results


def recover_stale_slip_reviews(*, stale_after_seconds=None, limit=25):
    stale_after_seconds = int(stale_after_seconds or SLIP_REVIEW_STALE_AFTER_SECONDS)
    cutoff = timezone.now() - timedelta(seconds=max(60, stale_after_seconds))
    candidates = list(
        SlipReview.objects.filter(
            status__in=[
                SlipReview.Status.QUEUED,
                SlipReview.Status.IMPORTING,
                SlipReview.Status.ANALYSING,
            ],
            updated_at__lt=cutoff,
        )
        .prefetch_related("selections")
        .order_by("updated_at")[: max(1, int(limit or 25))]
    )
    recovered = failed = skipped = 0
    results = []
    for review in candidates:
        progress = (review.summary or {}).get("progress") or {}
        total = review.selections.count() or int(progress.get("total") or 0)
        persisted_leg_results = _leg_results_from_persisted_slip_selections(review)
        completed = len(persisted_leg_results)
        try:
            if persisted_leg_results:
                finalize_slip_review_import_results(review.id, persisted_leg_results)
                recovered += 1
                outcome = "finalized_from_persisted_legs"
            else:
                fail_slip_review_import(
                    review.id,
                    "Slip review did not finish in time. Please retry the slip review.",
                    error_code="stale_review_timeout",
                )
                failed += 1
                outcome = "failed_stale_without_completed_legs"
            results.append(
                {
                    "review_id": review.id,
                    "previous_status": review.status,
                    "outcome": outcome,
                    "completed": completed,
                    "total": total,
                }
            )
            log.warning(
                "Slip review stale recovery review=%s outcome=%s completed=%s total=%s stale_after_seconds=%s",
                review.id,
                outcome,
                completed,
                total,
                stale_after_seconds,
            )
        except Exception as exc:
            skipped += 1
            results.append(
                {
                    "review_id": review.id,
                    "previous_status": review.status,
                    "outcome": "recovery_failed",
                    "error": str(exc)[:300],
                    "completed": completed,
                    "total": total,
                }
            )
            log.exception("Slip review stale recovery failed review=%s", review.id)
    return {
        "considered": len(candidates),
        "recovered": recovered,
        "failed": failed,
        "skipped": skipped,
        "stale_after_seconds": stale_after_seconds,
        "results": results,
    }


def process_slip_review_import(review_id):
    review = SlipReview.objects.get(id=review_id)
    payload = review.submitted_payload or {}
    review.summary = {
        **(review.summary or {}),
        **_empty_slip_summary("Importing slip selections.", task_id=(review.summary or {}).get("task_id", "")),
    }
    _set_slip_review_progress(
        review,
        phase="importing",
        message="Importing slip selections.",
        status=SlipReview.Status.IMPORTING,
    )

    try:
        if review.source == SlipReview.Source.SPORTYBET:
            imported = SportyBetShareImporter().import_share(
                url=payload.get("url"),
                code=payload.get("code"),
                payload=payload.get("payload"),
            )
        elif review.source == SlipReview.Source.BETANO:
            imported = BetanoBetslipImporter().import_betslip(
                url=payload.get("url"),
                code=payload.get("code"),
                payload=payload.get("payload"),
            )
        else:
            raise ValueError(f"Unsupported async slip source: {review.source}")

        review.summary = {
            **(review.summary or {}),
            **_empty_slip_summary("Analysing imported selections.", task_id=(review.summary or {}).get("task_id", "")),
        }

        selections = [
            {
                "match": item.get("match", ""),
                "market": item.get("market", ""),
                "provider": review.source,
                "provider_payload": item,
            }
            for item in imported.get("selections") or []
            if item.get("match") and item.get("market")
        ]
        _initialize_slip_selection_progress_rows(review, selections)
        _set_slip_review_progress(
            review,
            phase="analysing_legs",
            total=len(selections),
            completed=0,
            message=f"Imported {len(selections)} selections. Analysing each leg.",
            status=SlipReview.Status.ANALYSING,
        )
        plan = plan_slip_hydration(selections)
        log.info(
            "Slip hydration plan review=%s legs=%s fixtures=%s needing_snapshots=%s served_by_model=%s estimated_snapshot_calls=%s fanout=True",
            review.id,
            plan["legs"],
            plan["distinct_fixtures"],
            plan["fixtures_needing_snapshots"],
            plan["fixtures_served_by_model"],
            plan.get("estimated_snapshot_calls"),
        )
        if not selections:
            raise ValueError("No supported football selections were found in this slip.")
        final_payload = _json_safe(
            {
                **payload,
                "provider_code": imported.get("share_code") or imported.get("booking_code") or "",
                "selection_count": imported.get("selection_count", 0),
                "fanout_analysis": True,
            }
        )
        review.submitted_payload = final_payload
        review.save(update_fields=["submitted_payload", "updated_at"])

        from celery import chord as celery_chord
        from .tasks import analyse_slip_review_leg, finalize_slip_review_import

        workflow = celery_chord(
            [
                analyse_slip_review_leg.s(review.id, index, _json_safe(selection), payload.get("days", 3))
                for index, selection in enumerate(selections)
            ]
        )(finalize_slip_review_import.s(review.id))
        _publish_slip_review_event(
            review,
            "review.fanout_queued",
            {
                "total": len(selections),
                "fanout_task_id": getattr(workflow, "id", ""),
            },
        )
        log.info(
            "Slip review fanout queued review=%s legs=%s chord_task_id=%s",
            review.id,
            len(selections),
            getattr(workflow, "id", ""),
        )
        return _api_response_payload(
            {
                "review_id": review.id,
                "status": review.status,
                "fanout_task_id": getattr(workflow, "id", ""),
                **(review.summary or {}),
            }
        )
    except Exception as exc:
        review.status = SlipReview.Status.FAILED
        review.summary = _empty_slip_summary("Slip import failed.", task_id=(review.summary or {}).get("task_id", ""), error=exc)
        review.summary["progress"] = _slip_review_progress(
            phase="failed",
            message="Slip review failed.",
            error=str(exc),
        )
        review.save(update_fields=["status", "summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.failed",
            {
                "status": review.status,
                "error": str(exc),
                "progress": review.summary.get("progress") or {},
            },
        )
        raise


def fail_slip_review_import(review_id, message, *, error_code="failed"):
    review = SlipReview.objects.get(id=review_id)
    review.status = SlipReview.Status.FAILED
    review.summary = _empty_slip_summary(
        message,
        task_id=(review.summary or {}).get("task_id", ""),
        error=message,
    )
    review.summary["error_code"] = error_code
    review.summary["progress"] = _slip_review_progress(
        phase="failed",
        message=message,
        error_code=error_code,
    )
    review.save(update_fields=["status", "summary", "updated_at"])
    _publish_slip_review_event(
        review,
        "review.failed",
        {
            "status": review.status,
            "error": message,
            "error_code": error_code,
            "progress": review.summary.get("progress") or {},
        },
    )
    return _api_response_payload(
        {
            "review_id": review.id,
            "status": review.status,
            "error": message,
            "error_code": error_code,
            **(review.summary or {}),
        }
    )


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
        review_scoring_context = {"fixture_universe_synced": False}
        results = [
            _analyse_manual_selection(
                selection,
                days=days,
                request=request,
                force_fresh=True,
                review_scoring_context=review_scoring_context,
            )
            for selection in serializer.validated_data["selections"]
        ]
        review, summary, safe_results = _create_slip_review(
            request.user,
            source=SlipReview.Source.MANUAL,
            submitted_payload=serializer.validated_data,
            results=results,
        )
        return Response(
            _api_response_payload({
                "id": review.id,
                "source": review.source,
                "status": review.status,
                "public": summary.get("public", {}),
                **summary,
                "selections": safe_results,
            })
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
            "status becomes completed, partial, or failed."
        ),
        tags=["Slip Reviews"],
        request=SportyBetSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = SportyBetSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = _create_queued_slip_review(
            request.user,
            source=SlipReview.Source.SPORTYBET,
            submitted_payload=data,
        )
        task = import_slip_review.delay(review.id)
        review.summary = {
            **_empty_slip_summary("Slip import queued.", task_id=task.id),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        }
        review.save(update_fields=["summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.queued",
            {
                "status": review.status,
                "task_id": task.id,
                "progress": review.summary.get("progress") or {},
            },
        )
        return Response(_slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)


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
            "endpoint until the status becomes completed, partial, or failed."
        ),
        tags=["Slip Reviews"],
        request=BetanoSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = BetanoSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = _create_queued_slip_review(
            request.user,
            source=SlipReview.Source.BETANO,
            submitted_payload=data,
        )
        task = import_slip_review.delay(review.id)
        review.summary = {
            **_empty_slip_summary("Slip import queued.", task_id=task.id),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        }
        review.save(update_fields=["summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.queued",
            {
                "status": review.status,
                "task_id": task.id,
                "progress": review.summary.get("progress") or {},
            },
        )
        return Response(_slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)


def _hit_rate(wins, losses):
    settled = wins + losses
    return round((wins / settled) * 100, 1) if settled else None


def _slip_recap_payload(user, *, days):
    since = timezone.localdate() - timedelta(days=days)
    selections = list(
        SlipSelection.objects.filter(
            review__user=user,
            match_date__gte=since,
        ).only("outcome", "flagged_risky", "review_id")
    )

    wins = [item for item in selections if item.outcome == SlipSelection.Outcome.WIN]
    losses = [item for item in selections if item.outcome == SlipSelection.Outcome.LOSS]
    void = [item for item in selections if item.outcome == SlipSelection.Outcome.VOID]
    unsettleable = [item for item in selections if item.outcome == SlipSelection.Outcome.UNSETTLEABLE]
    pending = [item for item in selections if item.outcome == SlipSelection.Outcome.PENDING]

    flagged_wins = [item for item in wins if item.flagged_risky]
    flagged_losses = [item for item in losses if item.flagged_risky]
    unflagged_wins = [item for item in wins if not item.flagged_risky]
    unflagged_losses = [item for item in losses if not item.flagged_risky]

    ticket_count = len({item.review_id for item in selections})
    settled_count = len(wins) + len(losses)

    if not settled_count:
        message = "None of your selections in this window have been settled yet."
    else:
        message = (
            f"You submitted {ticket_count} {_plural(ticket_count, 'ticket')}. "
            f"{len(wins)} of {settled_count} settled {_plural(settled_count, 'selection')} were correct."
        )
        if losses:
            message += (
                f" {len(flagged_losses)} of the {len(losses)} that failed "
                f"{'was' if len(flagged_losses) == 1 else 'were'} flagged as risky before kickoff."
            )

    return {
        "contract_version": "match_checker_public_v2",
        "window": {"days": days, "from": since.isoformat(), "to": timezone.localdate().isoformat()},
        "tickets": ticket_count,
        "selections": {
            "total": len(selections),
            "settled": settled_count,
            "correct": len(wins),
            "failed": len(losses),
            "void": len(void),
            "unsettleable": len(unsettleable),
            "awaiting_result": len(pending),
        },
        "flagged": {
            "flagged_before_kickoff": len(flagged_wins) + len(flagged_losses),
            "failed_and_flagged": len(flagged_losses),
            "failed_and_not_flagged": len(unflagged_losses),
            "flagged_hit_rate_percent": _hit_rate(len(flagged_wins), len(flagged_losses)),
            "unflagged_hit_rate_percent": _hit_rate(len(unflagged_wins), len(unflagged_losses)),
        },
        "message": message,
    }


def _repair_payload(review, plan, repair):
    return {
        "repair_id": repair.id,
        "review_id": review.id,
        "mode": repair.mode,
        "original": {
            "legs": plan.original_legs,
            "combined_odds": plan.original_combined_odds,
            "estimated_success_percent": plan.original_success_percent,
        },
        "revised": {
            "legs": plan.revised_legs,
            "combined_odds": plan.revised_combined_odds,
            "estimated_success_percent": plan.revised_success_percent,
        },
        "changes": plan.changes,
        "decisions": [decision.to_dict() for decision in plan.decisions],
        "disclosure": plan.disclosure,
    }


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
        return Response(_repair_payload(review, plan, repair), status=status.HTTP_201_CREATED)


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
        return Response(_slip_recap_payload(request.user, days=days))


class SlipReviewListView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewListResponseSerializer

    @extend_schema(
        summary="List slip reviews",
        description="Authenticated user endpoint. Returns previous manual/bookmaker slip reviews for the current user.",
        tags=["Slip Reviews"],
        responses={200: SlipReviewListResponseSerializer},
    )
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        reviews = (
            SlipReview.objects.filter(user=request.user)
            .prefetch_related("selections")
            .order_by("-created_at")[:limit]
        )
        return Response(
            {
                "count": len(reviews),
                "reviews": [
                    _slip_review_payload(review, include_selections=False)
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
                "markets": SLIP_REVIEW_MARKET_OPTIONS,
                "verdicts": SLIP_REVIEW_VERDICT_OPTIONS,
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

        from .statpal_daily_build import StatPalDailyBuildService

        result = StatPalDailyBuildService().readiness_report(
            start_date=data.get("start_date"),
            days=data.get("days", 3),
            include_optional=bool(data.get("include_optional")),
            minimum_average_coverage=float(data.get("min_coverage") or 70.0),
        )
        return Response(_api_response_payload(result))


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
        return Response(_slip_review_payload(review, include_selections=True, public_only=public_only))


def _slip_review_event_payload(event):
    return {
        "id": event.id,
        "review_id": event.review_id,
        "event_type": event.event_type,
        "payload": event.payload or {},
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }


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
        events = list(
            SlipReviewEvent.objects.filter(review=review, id__gt=after_id)
            .order_by("id")[:limit]
        )
        latest_event_id = (
            SlipReviewEvent.objects.filter(review=review).order_by("-id").values_list("id", flat=True).first()
        )
        payload = {
            "review_id": review.id,
            "status": review.status,
            "progress": (review.summary or {}).get("progress") or {},
            "latest_event_id": latest_event_id,
            "events": [_slip_review_event_payload(event) for event in events],
        }
        response = Response(_api_response_payload(payload))
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Authorization, Cookie"
        return response


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


def _market_health_state(loss_streak, recent_5_losses, recent_10_hit_rate, recent_10_count, roi_flat):
    if loss_streak >= 3 or recent_5_losses >= 4 or (recent_10_count >= 5 and recent_10_hit_rate < 35):
        return "suppressed"
    if loss_streak >= 2 or recent_5_losses >= 3 or (recent_10_count >= 5 and recent_10_hit_rate < 45):
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
        "reasoning": snapshot.get("reasoning", ""),
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
        "reasoning": snapshot.get("reasoning") or backed_pick.get("reasoning", ""),
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


def _maintenance_jobs():
    """
    Data jobs the Match Checker depends on, queued rather than run inline.

    Each is expensive — the fixture sweep and the model fit are roughly a thousand
    provider calls apiece — so this returns task ids to poll rather than holding the
    request open.
    """
    from .tasks import (
        build_statpal_daily_cache,
        fit_score_models,
        refresh_imminent_lineups,
        refresh_player_availability,
        recover_stale_slip_reviews,
        settle_slip_selections,
        sync_fixture_horizon,
    )

    return {
        # Ordered so a full run populates fixtures before anything that reads them.
        "statpal_daily_cache": (build_statpal_daily_cache, "Build StatPal 3-day fixtures and analysis snapshots"),
        "fixture_horizon": (sync_fixture_horizon, "Cache every fixture in the 3-day window"),
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
            async_result = task.delay(days=days) if name in {"fixture_horizon", "statpal_daily_cache"} else task.delay()
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
            payload["result"] = _api_response_payload(task.result)
        elif task.failed():
            payload["error"] = str(task.result)
        return Response(payload, status=status.HTTP_200_OK)
