from datetime import datetime, timedelta
import csv
import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation

from celery.result import AsyncResult
from django.conf import settings
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

from .models import AlgoFixture, AlgoRun, GameBack, MarketPrediction, Pick, SlipReview, SlipSelection
from .market_taxonomy import canonical_market_name, describe_market, market_matches, market_options
from .recommendation_policy import assess_recommendation
from .services import BetanoBetslipImporter, FixtureSearchService, SportyBetShareImporter, algo_runner_service
from .statpal_advisory import statpal_market_advisory
from .statpal_snapshots import statpal_snapshot_service
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
    SlipReviewListResponseSerializer,
    SlipReviewOptionsResponseSerializer,
    SportyBetSlipImportRequestSerializer,
    StatPalFixtureContextQuerySerializer,
    StatPalFixtureContextResponseSerializer,
    StatPalFixtureRefreshRequestSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
    TopPickResponseSerializer,
)
from .tasks import generate_daily_picks, import_slip_review, run_monthly_auditor, settle_daily_results


log = logging.getLogger(__name__)
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
        "insights": prediction.insights or {},
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


def _replacement_market_for_slip(game, selected_market=None):
    markets = [
        _with_match_checker_advisory(market)
        for market in (game.get("markets") or [])
        if market.get("market") not in EXCLUDED_MARKETS
    ]
    markets = [market for market in markets if market]
    if selected_market:
        selected_name = selected_market.get("market")
        markets = [market for market in markets if not _market_matches(selected_name, market.get("market"))]
    candidates = [market for market in markets if (market.get("advisory_score") or 0) >= 55]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda market: (
            market.get("advisory_score") or 0,
            market.get("final_confidence") or market.get("confidence") or 0,
            _float_or_none(market.get("ev")) or -1,
        ),
        reverse=True,
    )[0]


def _market_is_better_for_slip(selected_market, replacement_market):
    if not replacement_market:
        return False
    if _market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False
    selected_score = _float_or_none(selected_market.get("advisory_score")) or float(selected_market.get("display_score") or 0)
    replacement_score = _float_or_none(replacement_market.get("advisory_score")) or float(replacement_market.get("display_score") or 0)
    return replacement_score >= selected_score + 6 and replacement_score >= 58


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
    }
    return _game_summary_from_fixture(
        fixture_summary,
        _picks_by_match(algo_run),
        request=request,
        include_markets=True,
    )


def _manual_verdict(selected_market, replacement_market):
    status_value = selected_market.get("recommendation_status") or "no_edge"
    has_better_market = _market_is_better_for_slip(selected_market, replacement_market)
    advisory_score = _float_or_none(selected_market.get("advisory_score")) or 0
    advisory_status = selected_market.get("advisory_status") or _match_checker_status(advisory_score)

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
        verdict = "replace" if has_better_market else "remove"
        message = (
            "The selected market does not show enough edge; consider the stronger match-specific alternative."
            if has_better_market
            else "The selected market does not show enough edge from the current analysis."
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
    score = _float_or_none(score) or 0
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Good"
    if score >= 45:
        return "Risky"
    if score >= 20:
        return "Poor"
    return "Very Poor"


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
    }.get(str(verdict or "").lower(), "Review")


def _public_verdict_message(verdict, submitted_market=None):
    market = submitted_market or "This pick"
    return {
        "keep": f"{market} is playable from the current analysis.",
        "caution": f"{market} is playable, but it carries extra risk.",
        "replace": f"{market} is too risky compared with the suggested alternative.",
        "remove": f"{market} is too risky to trust from the current analysis.",
        "expired": "This event has already started or ended.",
        "unmatched": "We could not confidently match this fixture.",
        "unmatched_market": "We matched the fixture, but not this market.",
        "pending_analysis": "This fixture is still being analysed.",
    }.get(str(verdict or "").lower(), "This pick needs review.")


def _public_verdict_object(verdict, submitted_market=None):
    code = str(verdict or "review").lower()
    return {
        "code": code,
        "label": _public_action_label(code),
        "message": _public_verdict_message(code, submitted_market=submitted_market),
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
        "status": _match_checker_status(score or 0) if score is not None else "",
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
    if verdict in {"unmatched", "unmatched_market", "pending_analysis", "expired"}:
        return "unknown"
    if status_value == "avoid" or (score is not None and score < 55):
        return "high"
    if verdict == "caution" or status_value == "caution" or (score is not None and score < 66):
        return "medium"
    return "low"


def _selection_strength_score(item):
    if item.get("status") != "analysed":
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


def _public_selection_card(item):
    card = _selection_card(item)
    selected_market = item.get("selected_market") or {}
    replacement_market = item.get("replacement_market") or {}
    verdict = item.get("verdict")
    ai_pick = None
    if verdict == "replace" and replacement_market:
        ai_pick = _public_market_pick(replacement_market)
    elif verdict in {"keep", "caution"}:
        ai_pick = _public_market_pick(selected_market, fallback_market=item.get("submitted_market"), fallback_odds=_selection_original_odds(item))
    if ai_pick:
        ai_pick["recommendation_strength"] = _public_recommendation_strength(ai_pick)
    if verdict != "replace":
        card = {**card, "alternative": None}
    why, reason_codes = _public_why_from_card(card)
    your_pick = {
        "market": item.get("submitted_market"),
        "label": item.get("submitted_market"),
        "meaning": _public_market_meaning(item.get("submitted_market")),
        "confidence": card.get("confidence"),
        "odds": card.get("odds"),
        "score": card.get("advisory_score"),
        "status": card.get("advisory_status") or _match_checker_status(card.get("advisory_score") or 0),
    }
    risk_level = _public_selection_risk(verdict, your_pick)
    technical_ref = {
        "status": item.get("status"),
        "match_resolution_score": card.get("match_resolution_score"),
        "market_recognized": (item.get("market_taxonomy") or {}).get("recognized"),
        "market_core_supported": (item.get("market_taxonomy") or {}).get("core_supported"),
        "statpal_snapshot_types": sorted(((card.get("statpal_context") or {}).get("snapshots") or {}).keys()),
        "has_technical_details": True,
    }
    if card.get("match_id"):
        technical_ref["match_id"] = card.get("match_id")
    return {
        "id": card.get("match_id") or item.get("match"),
        "match": card.get("fixture") or card.get("match"),
        "match_id": card.get("match_id", ""),
        "your_pick": your_pick,
        "verdict": _public_verdict_object(verdict, submitted_market=item.get("submitted_market")),
        "risk_level": risk_level,
        "risk": _public_risk_label(risk_level),
        "ai_pick": ai_pick,
        "why": why,
        "reason_codes": reason_codes,
        "technical_ref": technical_ref,
    }


def _slip_intelligence(results):
    enriched = []
    for item in results:
        copy = dict(item)
        copy["selection_score"] = _selection_strength_score(copy)
        enriched.append(copy)

    analysed = [item for item in enriched if item.get("status") == "analysed"]
    if analysed:
        overall_score = round(sum(item["selection_score"] for item in analysed) / len(analysed), 1)
    else:
        overall_score = 0.0

    remove_items = [item for item in enriched if item.get("verdict") == "remove"]
    replace_items = [item for item in enriched if item.get("verdict") == "replace"]
    caution_items = [item for item in enriched if item.get("verdict") == "caution"]
    keep_items = [item for item in enriched if item.get("verdict") == "keep"]
    expired_items = [item for item in enriched if item.get("status") == "expired"]
    unverified_items = [item for item in enriched if item.get("status") not in {"analysed", "expired"}]

    if remove_items or len(replace_items) >= 3 or overall_score < 45:
        risk_level = "high"
    elif replace_items or len(caution_items) >= 2 or overall_score < 65:
        risk_level = "medium"
    else:
        risk_level = "low"

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
    original_success = _combined_probability(
        (item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score"))
        for item in analysed
    )
    optimized_scores = [_optimized_leg_score(item) for item in analysed]
    optimized_success = _combined_probability(optimized_scores)
    optimized_leg_count = sum(1 for score in optimized_scores if score is not None)
    improvement = (
        round(optimized_success - original_success, 1)
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
    }
    optimized_ticket = {
        "legs": optimized_leg_count,
        "estimated_success": optimized_success,
        "combined_odds": suggested_combined,
    }
    improvement_text = f"+{improvement} percentage points" if improvement is not None and improvement > 0 else (
        f"{improvement} percentage points" if improvement is not None else ""
    )
    learning_tracking = {
        "status": "captured",
        "tracked_items": len(analysed),
        "tracks_submitted_market": True,
        "tracks_suggested_alternative": True,
        "outcome_tracking": "pending_settlement",
    }

    public_selections = [_public_selection_card(item) for item in enriched]
    recommended_change_ids = [
        selection.get("id")
        for selection in public_selections
        if (selection.get("verdict") or {}).get("code") in {"replace", "remove"}
    ]
    ticket_impact = {
        "message": (
            f"Changing {len(replace_items) + len(remove_items)} risky {_plural(len(replace_items) + len(remove_items), 'pick')} improves the estimated ticket success rate."
            if remove_items or replace_items
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
        "contract_version": "match_checker_public_v1",
        "response_mode": "public",
        "ticket": {
            "title": "Slip Review",
            "total_legs": len(enriched),
            "analysed_legs": len(analysed),
            "unmatched_legs": len(unverified_items),
            "expired_legs": len(expired_items),
        },
        "ticket_health": ticket_health,
        "verdict": {
            "code": verdict_code,
            "label": verdict_label,
            "message": verdict_message,
        },
        "comparison": {
            "original": {
                "legs": original_ticket["legs"],
                "combined_odds": original_ticket["combined_odds"],
                "model_estimated_success_percent": original_ticket["estimated_success"],
            },
            "optimized": {
                "legs": optimized_ticket["legs"],
                "combined_odds": optimized_ticket["combined_odds"],
                "model_estimated_success_percent": optimized_ticket["estimated_success"],
            },
            "success_increase_percentage_points": improvement,
            "picks_changed": len(replace_items) + len(remove_items),
        },
        "improvement": {
            "original_success_percent": original_success,
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
            "unmatched": len(unverified_items),
            "expired": len(expired_items),
        },
        "selections": public_selections,
        "tracking": {
            "enabled": True,
            "status": "pending_settlement",
            "tracked_selections": len(analysed),
        },
    }

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
        "analysed_count": sum(1 for item in enriched if item.get("status") == "analysed"),
        "keep_count": sum(1 for item in enriched if item.get("verdict") == "keep"),
        "caution_count": sum(1 for item in enriched if item.get("verdict") == "caution"),
        "replace_count": sum(1 for item in enriched if item.get("verdict") == "replace"),
        "remove_count": sum(1 for item in enriched if item.get("verdict") == "remove"),
        "expired_count": sum(1 for item in enriched if item.get("status") == "expired"),
        "unmatched_count": sum(1 for item in enriched if item.get("status") in {"unmatched", "ambiguous_match", "market_not_found"}),
        "pending_analysis_count": sum(1 for item in enriched if item.get("status") == "matched_unscored"),
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
    reviewable_count = max(0, int(summary.get("count") or 0) - int(summary.get("expired_count") or 0))
    if summary.get("count") and summary.get("analysed_count") == reviewable_count:
        return SlipReview.Status.COMPLETED
    if summary.get("analysed_count") or summary.get("pending_analysis_count"):
        return SlipReview.Status.PARTIAL
    return SlipReview.Status.FAILED


def _populate_slip_review(review, results):
    safe_results = _json_safe(results)
    safe_results, _ = _slip_intelligence(safe_results)
    summary = _manual_review_summary(safe_results)
    review.status = _review_status_from_summary(summary)
    review.summary = summary
    review.save(update_fields=["status", "summary", "updated_at"])
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
            )
        )
    SlipSelection.objects.bulk_create(rows, batch_size=100)
    return summary, safe_results


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


def _create_queued_slip_review(user, *, source, submitted_payload):
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.QUEUED,
        title=f"{source.title()} review",
        submitted_payload=_json_safe(submitted_payload),
        summary=_empty_slip_summary("Slip import queued."),
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
    if public_only:
        return _api_response_payload({
            "id": review.id,
            "source": review.source,
            "status": review.status,
            "title": review.title,
            "created_at": review.created_at,
            "updated_at": review.updated_at,
        } | public_payload)
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


def _analyse_manual_selection(selection, *, days, request=None, force_fresh=False):
    match_text = selection.get("match", "")
    requested_market = selection.get("market", "")
    market_descriptor = describe_market(
        requested_market,
        market_name=((selection.get("market_taxonomy") or {}).get("raw") or ""),
    )
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
    provider_fixture = search_service.get_provider_fixture(
        provider=provider_metadata.get("provider"),
        provider_event_id=provider_metadata.get("provider_event_id"),
    )
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
    search_service.learn_resolution(
        provider_metadata=provider_metadata,
        candidate=candidate,
        confidence=candidate.get("match_score"),
        method="provider_fixture_map" if provider_fixture else "team_date_league",
    )

    on_demand = None
    if force_fresh:
        on_demand = algo_runner_service.score_cached_fixture_on_demand(
            candidate["match_id"],
            match_date=candidate.get("match_date"),
            reason="slip_review",
            force=True,
        )
        game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
    else:
        game = _manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
        if not game:
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
    statpal_provider_match_id = (
        provider_metadata.get("provider_event_id")
        if str(provider_metadata.get("provider") or "").lower() == "statpal"
        else ""
    )
    statpal_refresh = statpal_snapshot_service.refresh_fixture_snapshots(
        match_id=candidate.get("match_id"),
        provider_match_id=statpal_provider_match_id,
        provider_competition_id=provider_metadata.get("provider_competition_id") or "",
    )
    statpal_context = statpal_snapshot_service.fixture_context(
        match_id=candidate.get("match_id"),
        provider_match_id=statpal_provider_match_id,
    )
    statpal_advisory = statpal_market_advisory.evaluate_market(
        market_descriptor,
        fixture={**game, "statpal_context": statpal_context},
        provider_payload=selection.get("provider_payload") or {},
        statpal_payload=selection.get("statpal_payload"),
    )
    canonical_requested_market = market_descriptor.canonical
    analysis_market = _market_for_fixture_orientation(canonical_requested_market, candidate)
    selected_market = next((market for market in markets if _market_matches(analysis_market, market.get("market"))), None)
    if not selected_market:
        replacement_market = _replacement_market_for_slip(game)
        advisory_score = statpal_advisory.get("score")
        advisory_status = statpal_advisory.get("status") if advisory_score is not None else "needs_data"
        advisory_warnings = list(statpal_advisory.get("warnings") or [])
        if market_descriptor.recognized and advisory_score is not None:
            advisory_basis = statpal_advisory.get("basis") or "statpal_advisory"
        else:
            advisory_basis = "unsupported_market"
            advisory_warnings = advisory_warnings or ["unsupported_market"]
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "analysis_market": analysis_market,
            "fixture_orientation": candidate.get("match_orientation", ""),
            "status": "market_not_found",
            "verdict": "replace" if replacement_market else "unmatched_market",
            "message": (
                "We recognized this market, but it is not scored directly yet, so we selected the best available alternative for this match."
                if market_descriptor.recognized
                else "This market is not recognized yet, so we selected the best available alternative for this match."
            )
            if replacement_market
            else (
                "We recognized this market, but it is not available in the scored markets for this game yet."
                if market_descriptor.recognized
                else "Fixture matched, but that market is not available in the scored markets for this game."
            ),
            "matched_fixture": candidate,
            "available_markets": [market.get("market") for market in markets],
            "selected_market": {
                "market": requested_market,
                "market_taxonomy": market_taxonomy,
                "confidence": None,
                "final_confidence": None,
                "advisory_score": advisory_score,
                "advisory_status": advisory_status,
                "advisory_basis": advisory_basis,
                "advisory_warnings": advisory_warnings or ["market_recognized_not_scored"],
                "advisory_evidence": statpal_advisory.get("evidence") or {},
                "statpal_advisory": statpal_advisory,
            },
            "best_market": game.get("best_market"),
            "recommended_market": game.get("recommended_market"),
            "replacement_market": replacement_market,
            "fixture_resolution": {
                "status": "market_not_found",
                "attempts": resolver_trace,
            },
            "statpal_refresh": statpal_refresh,
            "statpal_context": statpal_context,
            "statpal_advisory": statpal_advisory,
        }

    best_market = game.get("best_market") or game.get("top_market")
    recommended_market = game.get("recommended_market")
    selected_market = _with_match_checker_advisory(selected_market)
    if selected_market:
        selected_market["market_taxonomy"] = market_taxonomy
        selected_market = _with_statpal_advisory(selected_market, statpal_advisory)
    best_market = _with_match_checker_advisory(best_market)
    recommended_market = _with_match_checker_advisory(recommended_market)
    replacement_market = _replacement_market_for_slip(game, selected_market=selected_market)
    verdict = _manual_verdict(selected_market, replacement_market)
    return {
        "match": match_text,
        "submitted_market": requested_market,
        "market_taxonomy": market_taxonomy,
        "analysis_market": analysis_market,
        "fixture_orientation": candidate.get("match_orientation", ""),
        "status": "analysed",
        **verdict,
        "matched_fixture": {
            "match_id": game.get("match_id"),
            "match_date": candidate.get("match_date"),
            "fixture": game.get("fixture"),
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "league": game.get("league"),
            "country": game.get("country"),
            "kickoff": game.get("kickoff"),
            "match_score": candidate.get("match_score"),
            "match_orientation": candidate.get("match_orientation", ""),
        },
        "selected_market": selected_market,
        "best_market": best_market,
        "recommended_market": recommended_market,
        "replacement_market": replacement_market,
        "statpal_refresh": statpal_refresh,
        "statpal_context": statpal_context,
        "statpal_advisory": statpal_advisory,
        "possible_matches": candidates,
        "on_demand_analysis": on_demand,
        "fixture_resolution": {
            "status": "matched",
            "attempts": resolver_trace,
        },
    }


def process_slip_review_import(review_id):
    review = SlipReview.objects.get(id=review_id)
    payload = review.submitted_payload or {}
    review.status = SlipReview.Status.IMPORTING
    review.summary = {
        **(review.summary or {}),
        **_empty_slip_summary("Importing slip selections.", task_id=(review.summary or {}).get("task_id", "")),
    }
    review.save(update_fields=["status", "summary", "updated_at"])

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

        review.status = SlipReview.Status.ANALYSING
        review.summary = {
            **(review.summary or {}),
            **_empty_slip_summary("Analysing imported selections.", task_id=(review.summary or {}).get("task_id", "")),
        }
        review.save(update_fields=["status", "summary", "updated_at"])

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
        results = []
        for selection in selections:
            result = _analyse_manual_selection(
                selection,
                days=payload.get("days", 3),
                request=None,
                force_fresh=True,
            )
            result["provider"] = review.source
            result["provider_payload"] = _json_safe(selection.get("provider_payload") or {})
            results.append(result)

        if not results:
            raise ValueError("No supported football selections were found in this slip.")

        summary, safe_results = _populate_slip_review(review, results)
        review.submitted_payload = _json_safe(
            {
                **payload,
                "provider_code": imported.get("share_code") or imported.get("booking_code") or "",
                "selection_count": imported.get("selection_count", 0),
            }
        )
        review.save(update_fields=["submitted_payload", "updated_at"])
        return _api_response_payload({"review_id": review.id, "status": review.status, **summary})
    except Exception as exc:
        review.status = SlipReview.Status.FAILED
        review.summary = _empty_slip_summary("Slip import failed.", task_id=(review.summary or {}).get("task_id", ""), error=exc)
        review.save(update_fields=["status", "summary", "updated_at"])
        raise


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
        results = [
            _analyse_manual_selection(selection, days=days, request=request, force_fresh=True)
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
        review.summary = _empty_slip_summary("Slip import queued.", task_id=task.id)
        review.save(update_fields=["summary", "updated_at"])
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
        review.summary = _empty_slip_summary("Slip import queued.", task_id=task.id)
        review.save(update_fields=["summary", "updated_at"])
        return Response(_slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)


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
