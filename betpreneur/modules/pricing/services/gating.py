"""Publication gating and the scores that depend on it.

Whether a market may be shown publicly is an operational switch, not a pricing
judgement — so it reads settings, and every score that consults it lands here
rather than in domain/. Keeping that boundary honest is what lets domain/ be
tested with no Django at all.
"""
from __future__ import annotations

from django.conf import settings

from betpreneur.modules.markets.api import describe_market

from ..domain.market_scoring import (
    _bounded_ev_score,
    _market_reviewer_score,
    _match_checker_evidence,
    _match_checker_risk_penalty,
    _match_checker_warnings,
    float_or_none,
    match_checker_status,
)


def setting_bool(name, default=False):
    value = (getattr(settings, "GRIND_ALGO", {}) or {}).get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def market_publicly_paused(market_name):
    if market_name == "DC: 12":
        return not setting_bool("ALGO_PUBLISH_DC12", False)
    return False


def market_display_score(market):
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
        if market_publicly_paused("DC: 12"):
            score -= 80.0
        score -= 8.0
        if "draw_boundary_risk" in risk_flags or "Draw pressure" in avoid_reason:
            score -= 8.0
    if "thin_edge" in risk_flags:
        score -= 3.0
    if "goal_line_boundary" in risk_flags:
        score -= 24.0
    if "german_under_goals_market_blocked" in risk_flags:
        score -= 70.0
    if "under25_goal_volatility" in risk_flags:
        score -= 20.0
    if "under35_blowout_risk" in risk_flags:
        score -= 28.0
    if "under45_high_goal_volatility" in risk_flags:
        score -= 18.0
    if "corner_line_boundary" in risk_flags:
        score -= 18.0
    if "corner_under_pressure_risk" in risk_flags:
        score -= 12.0
    if "corner_over_margin_risk" in risk_flags:
        score -= 10.0
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


def _match_checker_advisory_score(market):
    review = market.get("council_review") or {}
    final_confidence = float_or_none(review.get("final_confidence") or market.get("final_confidence") or market.get("confidence")) or 0
    consensus = float_or_none(review.get("consensus_score")) or final_confidence
    disagreement = float_or_none(review.get("disagreement_score")) or 0
    market_fit = _market_reviewer_score(market, "market_fit") or consensus
    scoreline_fit = _market_reviewer_score(market, "scoreline_pattern") or consensus
    value_score = _market_reviewer_score(market, "value") or consensus
    ev_score = _bounded_ev_score(market.get("ev"))
    decision = str(review.get("decision") or "")
    odds = float_or_none(market.get("odds")) or 0

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
        if market_publicly_paused("DC: 12"):
            score -= 7.0
    if odds and odds <= 1.06:
        score -= 5.0
    elif odds >= 10:
        score -= 15.0
    elif odds >= 6:
        score -= 8.0

    score -= _match_checker_risk_penalty(market.get("risk_flags") or [])
    return round(max(0, min(100, score)), 1)


def with_match_checker_advisory(market):
    if not market:
        return None
    payload = dict(market)
    payload["market_taxonomy"] = payload.get("market_taxonomy") or describe_market(payload.get("market")).to_dict()
    score = _match_checker_advisory_score(payload)
    payload["advisory_score"] = score
    payload["advisory_status"] = match_checker_status(score)
    payload["advisory_warnings"] = _match_checker_warnings(payload)
    payload["advisory_evidence"] = _match_checker_evidence(payload)
    payload["advisory_basis"] = "match_specific_analysis"
    return payload
