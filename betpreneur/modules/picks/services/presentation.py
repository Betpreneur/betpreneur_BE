"""Rendering a fixture and its picks as a payload.

This is picks' shared surface: the shapes slips and analytics read back. It
sits below interface/ deliberately — a facade that re-exports from the
delivery layer makes every consumer inherit Django views and celery, which is
what R5 caught when slips.domain first tried to use it.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from betpreneur.modules.picks.interface.serializers import PickSerializer
from betpreneur.modules.picks.models import AlgoFixture, AlgoRun, GameBack, MarketPrediction, Pick
from betpreneur.modules.pricing.api import (
    assess_recommendation,
    market_decision_rank,
    market_display_score,
    market_publicly_paused,
    setting_bool,
)
from betpreneur.modules.pricing.api import tier_for_confidence as _tier_for_confidence

PICK_TIER_RANK = {
    Pick.Tier.BANKER: 3,
    Pick.Tier.VALUE_GEM: 2,
    Pick.Tier.WILD_CARD: 1,
}


EXCLUDED_MARKETS = {"DC: 1X", "DC: X2"}


def _effective_pick_tier(pick):
    council_tier = (((pick.insights or {}).get("council_review") or {}).get("tier") or "")
    if council_tier in {Pick.Tier.BANKER, Pick.Tier.VALUE_GEM, Pick.Tier.WILD_CARD}:
        return council_tier
    return pick.tier


def _pick_final_confidence(pick):
    return (((pick.insights or {}).get("council_review") or {}).get("final_confidence") or pick.confidence or 0)


def _latest_successful_run(target_date, *, prefetch=True):
    queryset = AlgoRun.objects.filter(target_date=target_date, status=AlgoRun.Status.SUCCESS)
    if prefetch:
        queryset = queryset.prefetch_related("picks", "fixtures", "market_predictions")
    return queryset.order_by("-created_at").first()


def _top_pick_sort_key(pick):
    return (
        PICK_TIER_RANK.get(_effective_pick_tier(pick), 0),
        _pick_final_confidence(pick),
        float(pick.ev or 0),
        float(pick.odds or 0),
    )


def _recommendation_status_rank(market):
    status = str(market.get("recommendation_status") or "").strip().lower()
    return {
        "strong": 5,
        "recommended": 4,
        "playable": 3,
        "watchlist": 2,
        "no_edge": 1,
    }.get(status, 0)


def _model_probability_percent(market):
    insights = market.get("insights") or {}
    probability = insights.get("calibrated_probability")
    if probability is None:
        probability = insights.get("raw_probability")
    try:
        if probability is not None:
            return float(probability) * 100.0
    except (TypeError, ValueError):
        pass
    return float(market.get("final_confidence") or market.get("confidence") or 0)


def _data_quality_rank(market):
    quality = str(
        (market.get("insights") or {}).get("data_quality") or market.get("data_quality") or ""
    ).lower()
    return {
        "calibrated": 6,
        "strong": 5,
        "fresh": 5,
        "medium": 4,
        "limited": 3,
        "partial": 3,
        "poor": 2,
        "unavailable": 1,
        "unknown": 1,
    }.get(quality, 0)


def _game_market_rank(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("recommended") else 0,
        0 if market.get("publicly_paused") else 1,
        _model_probability_percent(market),
        _data_quality_rank(market),
        -len(market.get("risk_flags") or []),
        _recommendation_status_rank(market),
        market_decision_rank(market),
        *market_display_score(market),
    )


def normalise_council_review(insights, fallback_confidence=None, fallback_tier=""):
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

    if council_tier == Pick.Tier.WILD_CARD and not setting_bool("ALGO_PUBLISH_WILD_CARDS", False):
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

    if council_tier == "watchlist":
        reasons.append("council_watchlist")
        reasons.extend(council_reasons)
        return {
            **assessment,
            "recommended": False,
            "recommendation_status": "watchlist",
            "recommendation_reasons": list(dict.fromkeys(reasons)),
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


def format_game_form_line(label, form):
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
            f"The goal profile leans controlled. {format_game_form_line('Home', home_form)}. "
            f"{format_game_form_line('Away', away_form)}.{expected_note}"
        )
    if market_name.startswith("Over") or "BTTS" in market_name or market_name.startswith("GG"):
        expected_note = f" Expected goals sit around {expected_total}." if expected_total is not None else ""
        return (
            f"The attacking profile supports goals. {format_game_form_line('Home', home_form)}. "
            f"{format_game_form_line('Away', away_form)}.{expected_note}"
        )
    if market_name == "DC: 12":
        draw_note = f" Draw-risk confidence is {draw_confidence}%." if draw_confidence is not None else ""
        return (
            f"This result market needs either team to win, so draw risk is the key threat. "
            f"{format_game_form_line('Home', home_form)}. {format_game_form_line('Away', away_form)}.{draw_note}"
        )
    if market_name.endswith("Win") or market_name.startswith("AH ") or market_name.startswith("DNB"):
        return (
            f"The result market is based on recent team balance. {format_game_form_line('Home', home_form)}. "
            f"{format_game_form_line('Away', away_form)}."
        )
    return (
        f"Recent team context: {format_game_form_line('Home', home_form)}. "
        f"{format_game_form_line('Away', away_form)}."
    )


def _public_reasoning_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?:^|\s+)Pricing is based on [^.]+ odds\.", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _prediction_reasoning_for_market(market):
    parts = []
    summary = market.get("analysis_summary") or ((market.get("bettor_view") or {}).get("summary"))
    if summary:
        parts.append(summary)
    parts.extend([str(item) for item in (market.get("positive_evidence") or []) if item][:3])
    conclusion = market.get("analysis_conclusion") or ((market.get("bettor_view") or {}).get("conclusion"))
    if conclusion and conclusion not in parts:
        parts.append(conclusion)
    if not parts:
        return ""
    return _public_reasoning_text(" ".join(parts))


def _prediction_verdict_for_market(market):
    conclusion = market.get("analysis_conclusion") or ((market.get("bettor_view") or {}).get("conclusion"))
    if conclusion:
        return _public_reasoning_text(conclusion)
    return _market_verdict_for_game(market)


def _market_reasoning_for_game(market, item):
    ev = market.get("ev")
    ev_text = f"{ev:+.3f} expected value" if ev is not None else "no priced EV"
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
        f"{_market_evidence_for_game(market, item)}"
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


def _normalise_fixture_markets(item, picks_by_match, request=None, user_backed_markets=None):
    markets = []
    match_id = str(item.get("match_id") or "")
    match_picks = picks_by_match.get(str(item.get("match_id") or ""), [])
    pick_by_market = {pick.market: pick for pick in match_picks}
    if user_backed_markets is not None:
        user_backed_markets = set(user_backed_markets)
    elif request and request.user.is_authenticated and match_id:
        user_backed_markets = set(
            GameBack.objects.filter(user=request.user, match_id=match_id)
            .exclude(market="")
            .values_list("market", flat=True)
        )
    else:
        user_backed_markets = set()
    for market in item.get("markets") or []:
        if market.get("market") in EXCLUDED_MARKETS:
            continue
        payload = dict(market)
        payload["council_review"] = normalise_council_review(
            payload.get("insights"),
            fallback_confidence=payload.get("confidence"),
        )
        payload["final_confidence"] = payload["council_review"].get("final_confidence")
        payload["suggested_tier"] = (
            payload["council_review"].get("tier")
            or _tier_for_confidence(payload.get("confidence"))
        )
        selected_pick = pick_by_market.get(payload.get("market"))
        payload["publicly_paused"] = market_publicly_paused(payload.get("market"))
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
        payload["reasoning"] = _prediction_reasoning_for_market(payload)
        payload["model_verdict"] = _prediction_verdict_for_market(payload)
        payload["display_score"] = round(market_display_score(payload)[0], 3)
        markets.append(payload)
    return sorted(markets, key=_game_market_rank, reverse=True)


def game_summary_from_fixture(
    item,
    picks_by_match,
    request=None,
    include_markets=False,
    backed_game_counts=None,
    user_backed_game_ids=None,
    user_backed_markets_by_match=None,
):
    match_id = str(item.get("match_id") or "")
    markets = _normalise_fixture_markets(
        item,
        picks_by_match,
        request=request,
        user_backed_markets=(user_backed_markets_by_match or {}).get(match_id),
    )
    match_picks = sorted(picks_by_match.get(match_id, []), key=_top_pick_sort_key, reverse=True)
    pick_data = PickSerializer(
        match_picks,
        many=True,
        context={
            "request": request,
            "backed_game_counts": backed_game_counts,
            "backed_game_ids": user_backed_game_ids,
        },
    ).data
    top_market = next((market for market in markets if not market.get("publicly_paused")), None)
    if top_market is None:
        top_market = markets[0] if markets else None
    recommended_market = next((market for market in markets if market.get("recommended")), None)
    official_pick = pick_data[0] if pick_data else None
    backed_count = (
        int((backed_game_counts or {}).get(match_id, 0) or 0)
        if backed_game_counts is not None
        else (GameBack.objects.filter(match_id=match_id).count() if match_id else 0)
    )
    backed_by_me = False
    if user_backed_game_ids is not None:
        backed_by_me = match_id in user_backed_game_ids
    elif request and request.user.is_authenticated and match_id:
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
            "reasoning": _public_reasoning_text((recommended_market or top_market or {}).get("reasoning", "")),
            "model_verdict": (recommended_market or top_market or {}).get("model_verdict", ""),
        }
    return payload


def picks_by_match_for_run(algo_run):
    grouped = {}
    for pick in sorted(
        [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
        key=_top_pick_sort_key,
        reverse=True,
    ):
        grouped.setdefault(str(pick.match_id or ""), []).append(pick)
    return grouped


def market_prediction_payload(prediction):
    council_review = normalise_council_review(
        prediction.insights,
        fallback_confidence=prediction.confidence,
        fallback_tier=prediction.selected_pick.tier if prediction.selected_pick_id else "",
    )
    insights = prediction.insights or {}
    payload = {
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
        "analysis_available": bool(insights.get("analysis_available", prediction.eligible)),
        "data_status": insights.get("data_status", "modelled" if prediction.eligible else "insufficient_data"),
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
    payload["reasoning"] = _prediction_reasoning_for_market(payload)
    payload["model_verdict"] = _prediction_verdict_for_market(payload)
    return payload


def _fixture_summary_for_match(algo_run, match_id):
    target_match_id = str(match_id or "").strip()
    fixture = (
        AlgoFixture.objects.filter(run=algo_run, match_id=target_match_id)
        .only(
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
            "home_recent_form",
            "away_recent_form",
            "fixture_context",
            "team_news",
            "corner_profile",
            "insights",
            "source_payload",
            "market_count",
            "markets_70_plus",
            "markets_65_plus",
        )
        .first()
    )
    if not fixture:
        return None

    markets = [
        market_prediction_payload(prediction)
        for prediction in MarketPrediction.objects.filter(
            run=algo_run,
            match_id=target_match_id,
            eligible=True,
        )
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


def game_detail_payload(target_date, match_id, request=None):
    algo_run = _latest_successful_run(target_date, prefetch=False)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "game": None,
        }

    target_match_id = str(match_id)
    match_picks = list(
        Pick.objects.filter(run=algo_run, match_id=target_match_id)
        .exclude(market__in=EXCLUDED_MARKETS)
        .order_by("-confidence", "-ev", "market")
    )
    picks_by_match = {target_match_id: sorted(match_picks, key=_top_pick_sort_key, reverse=True)}
    fixture_summary = _fixture_summary_for_match(algo_run, target_match_id)
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

    game = game_summary_from_fixture(
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


def decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _back_count(match_id, market=""):
    queryset = GameBack.objects.filter(match_id=str(match_id))
    market = str(market or "").strip()
    if market:
        queryset = queryset.filter(market=market)
    return queryset.count()
