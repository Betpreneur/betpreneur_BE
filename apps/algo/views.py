from datetime import timedelta
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

from .models import AlgoFixture, AlgoRun, GameBack, MarketPrediction, Pick
from .recommendation_policy import assess_recommendation
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
    BulkGameBackRequestSerializer,
    BulkGameBackResponseSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    GameAnalysisQuerySerializer,
    GameBackResponseSerializer,
    GameDetailResponseSerializer,
    GameListResponseSerializer,
    MarketHealthQuerySerializer,
    MarketHealthResponseSerializer,
    PickSerializer,
    PickDetailResponseSerializer,
    PublicSummarySerializer,
    RecordResponseSerializer,
    RecordQuerySerializer,
    ResultsUpdateSerializer,
    SingleGameBackRequestSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
    TopPickResponseSerializer,
)
from .tasks import generate_daily_picks, run_monthly_auditor, settle_daily_results


log = logging.getLogger(__name__)
SETTLED_PICK_STATUSES = [Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID]
PICK_DETAIL_HISTORY_DAYS = 90
PICK_TIER_RANK = {
    Pick.Tier.BANKER: 3,
    Pick.Tier.VALUE_GEM: 2,
    Pick.Tier.WILD_CARD: 1,
}
EXCLUDED_MARKETS = {"DC: 1X", "DC: X2"}


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


def _official_pick_from_back(back, fixture=None):
    snapshot = dict(back.market_snapshot or {})
    if not snapshot and not back.market:
        return None
    return {
        "id": None,
        "match_date": back.match_date or (fixture.match_date if fixture else None),
        "fixture": fixture.fixture if fixture else "",
        "home_team": fixture.home_team if fixture else "",
        "away_team": fixture.away_team if fixture else "",
        "league": fixture.league if fixture else "",
        "kickoff": fixture.kickoff if fixture else "",
        "match_id": back.match_id,
        "tier": snapshot.get("selected_tier") or snapshot.get("suggested_tier") or "",
        "market": back.market or snapshot.get("market", ""),
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
        "backed_count": _back_count(back.match_id, back.market),
        "source": "backed_market",
    }


def _backed_games_payload(request, target_date=None):
    backs = GameBack.objects.select_related("fixture", "fixture__run").filter(user=request.user)
    if target_date:
        backs = backs.filter(match_date=target_date)
    backs = backs.order_by("-match_date", "-created_at")

    games = []
    for back in backs:
        fixture = back.fixture or _latest_fixture_for_match(back.match_id, back.match_date)
        if not fixture:
            backed_pick = _official_pick_from_back(back)
            games.append({
                "match_id": back.match_id,
                "match_date": back.match_date,
                "backed_market": back.market,
                "backed_selection": back.market_snapshot or {},
                "official_pick": backed_pick,
                "official_picks": [backed_pick] if backed_pick else [],
                "official_pick_count": 1 if backed_pick else 0,
                "backed_official_pick": backed_pick,
                "backed": True,
                "backed_by_me": True,
                "backed_count": _back_count(back.match_id),
                "market_backed_count": _back_count(back.match_id, back.market),
            })
            continue
        summaries = _fixture_summaries_for_run(fixture.run)
        item = next(
            (summary for summary in summaries if str(summary.get("match_id") or "") == str(back.match_id)),
            None,
        )
        if item:
            summary = _game_summary_from_fixture(item, _picks_by_match(fixture.run), request=request, include_markets=True)
            backed_pick = _official_pick_from_back(back, fixture)
            summary["backed_market"] = back.market
            summary["backed_selection"] = back.market_snapshot or {}
            summary["market_backed_count"] = _back_count(back.match_id, back.market)
            if backed_pick:
                summary["official_pick"] = backed_pick
                summary["official_picks"] = [backed_pick]
                summary["official_pick_count"] = 1
                summary["backed_official_pick"] = backed_pick
            games.append(summary)
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
            "Authenticated user endpoint. Returns games/markets backed by the current user, with optional match date filtering. "
            "For this endpoint, official_pick is intentionally set to the user's backed market selection so existing frontend pick cards can render it directly. "
            "The same object is also available as backed_official_pick and the raw market snapshot is available as backed_selection."
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
            payload["result"] = task.result
        elif task.failed():
            payload["error"] = str(task.result)
        return Response(payload, status=status.HTTP_200_OK)
