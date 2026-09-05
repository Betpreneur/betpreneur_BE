"""Product-specific policy over shared prediction outputs."""

from __future__ import annotations

from typing import Any

from betpreneur.modules.prediction.api import (
    MarketProbability,
    RecommendationScore,
    ValueAssessment,
)

from ..contracts import (
    AllGamesPolicyAssessment,
    SlipReviewAlternative,
    SlipReviewPolicyAssessment,
    TopPicksPolicyAssessment,
)
from ..domain.tiers import Tier

TOP_PICK_MIN_SCORE = 70.0
TOP_PICK_MIN_EDGE = 0.02
TOP_PICK_MIN_EV = 0.01
BANKER_MIN_PROBABILITY = 0.72
BANKER_MIN_SCORE = 82.0
BANKER_MIN_EDGE = 0.03
BANKER_MAX_UNCERTAINTY = 4.0
BANKER_MAX_CORRELATION = 2.0
BANKER_MAX_VOLATILITY = 3.0
VALUE_GEM_MIN_PROBABILITY = 0.58
VALUE_GEM_MIN_SCORE = 74.0
VALUE_GEM_MIN_EDGE = 0.02
VALUE_GEM_MAX_SAMPLE_PENALTY = 8.0
VALUE_GEM_MIN_MARKET_FIT = 65.0
WILD_CARD_MIN_PROBABILITY = 0.55
WILD_CARD_MIN_SCORE = 66.0
WILD_CARD_MIN_EV = 0.04
WILD_CARD_MIN_VALUE_SCORE = 10.0
WILD_CARD_STAKE_WARNING = "Higher-variance pick: use reduced stake sizing."
SLIP_SUPPORTED_SCORE = 70.0
SLIP_ALTERNATIVE_MIN_DELTA = 3.0


def assess_all_games_policy(market_probability: MarketProbability) -> AllGamesPolicyAssessment:
    """All Games explains model coverage without aggressive profit claims."""
    return AllGamesPolicyAssessment(
        fixture_id=market_probability.fixture_id,
        market=market_probability.market,
        raw_probability=market_probability.raw_probability,
        calibrated_probability=market_probability.calibrated_probability,
        data_confidence=market_probability.confidence_score,
        data_quality=market_probability.data_quality,
        explanation_facts=market_probability.explanation_facts,
        warnings=market_probability.warnings,
    )


def assess_top_picks_policy(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment,
    recommendation_score: RecommendationScore,
) -> TopPicksPolicyAssessment:
    """Top Picks decides exposure using balanced score plus real-price value."""
    reasons = []
    score = recommendation_score.recommendation_score
    has_real_odds = bool(
        value_assessment.available_odds
        and not value_assessment.diagnostics.metadata.get("estimated_odds")
    )
    if score is None or score < TOP_PICK_MIN_SCORE:
        reasons.append("below_exposure_score")
    if not has_real_odds:
        reasons.append("real_odds_required")
    if value_assessment.edge is None or value_assessment.edge < TOP_PICK_MIN_EDGE:
        reasons.append("insufficient_edge")
    if value_assessment.ev is None or value_assessment.ev < TOP_PICK_MIN_EV:
        reasons.append("insufficient_ev")
    if recommendation_score.total_penalty >= 18:
        reasons.append("too_much_uncertainty")

    tier, tier_reasons, stake_warning = _technical_tier(
        market_probability,
        value_assessment,
        recommendation_score,
    )
    if tier == Tier.WATCHLIST:
        reasons.append("tier_watchlist")

    publishable = not reasons
    return TopPicksPolicyAssessment(
        fixture_id=market_probability.fixture_id,
        market=market_probability.market,
        exposure_score=score,
        tier=tier,
        publishable=publishable,
        calibrated_probability=market_probability.effective_probability,
        edge=value_assessment.edge,
        ev=value_assessment.ev,
        reasons=tuple(dict.fromkeys(reasons)),
        tier_reasons=tier_reasons,
        stake_warning=stake_warning,
        warnings=tuple(
            dict.fromkeys([*value_assessment.pricing_warnings, *recommendation_score.warnings])
        ),
    )


def assess_slip_review_policy(
    user_pick: MarketProbability,
    alternatives: tuple[MarketProbability | dict[str, Any], ...] = (),
    *,
    min_supported_score: float = SLIP_SUPPORTED_SCORE,
    min_alternative_delta: float = SLIP_ALTERNATIVE_MIN_DELTA,
) -> SlipReviewPolicyAssessment:
    """Slip Review evaluates the user thesis and nearby alternatives."""
    user_score = user_pick.confidence_score
    supported = user_score is not None and user_score >= min_supported_score
    alternative = (
        None
        if supported
        else _best_slip_alternative(user_pick, alternatives, min_delta=min_alternative_delta)
    )
    reasons = []
    if user_score is None:
        reasons.append("user_pick_unmodelled")
    elif supported:
        reasons.append("user_pick_supported")
    else:
        reasons.append("user_pick_below_support_threshold")
    if alternative is not None:
        reasons.append("closer_supported_alternative_found")
    elif not supported:
        reasons.append("no_close_supported_alternative")

    return SlipReviewPolicyAssessment(
        fixture_id=user_pick.fixture_id,
        market=user_pick.market,
        supported=supported,
        verdict="supported" if supported else "replace" if alternative else "review",
        user_confidence_score=user_score,
        suggested_alternative=alternative,
        reasons=tuple(reasons),
        warnings=user_pick.warnings,
    )


def _best_slip_alternative(
    user_pick: MarketProbability,
    alternatives: tuple[MarketProbability | dict[str, Any], ...],
    *,
    min_delta: float,
) -> SlipReviewAlternative | None:
    candidates = [_alternative_payload(user_pick, item) for item in alternatives]
    candidates = [
        item
        for item in candidates
        if item.confidence_delta is not None and item.confidence_delta >= min_delta
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item.thesis_preserved,
            -(item.family_distance or 0),
            item.confidence_delta or 0,
            item.market_fit_score or 50,
        ),
        reverse=True,
    )
    return candidates[0]


def _alternative_payload(
    user_pick: MarketProbability, value: MarketProbability | dict[str, Any]
) -> SlipReviewAlternative:
    if isinstance(value, MarketProbability):
        probability = value
        thesis_preserved = _same_family(user_pick, probability)
        family_distance = 0 if thesis_preserved else 1
        market_fit_score = None
        reason = ""
    else:
        probability = value["probability"]
        thesis_preserved = bool(value.get("thesis_preserved", _same_family(user_pick, probability)))
        family_distance = int(value.get("family_distance", 0 if thesis_preserved else 1))
        market_fit_score = _float_or_none(value.get("market_fit_score"))
        reason = str(value.get("reason") or "")
    user_score = user_pick.confidence_score or 0
    confidence = probability.confidence_score
    delta = None if confidence is None else round(confidence - user_score, 2)
    return SlipReviewAlternative(
        market=probability.market,
        confidence_score=confidence,
        confidence_delta=delta,
        thesis_preserved=thesis_preserved,
        family_distance=family_distance,
        market_fit_score=market_fit_score,
        reason=reason,
    )


def _same_family(left: MarketProbability, right: MarketProbability) -> bool:
    left_family = left.diagnostics.metadata.get("market_family")
    right_family = right.diagnostics.metadata.get("market_family")
    return bool(left_family and right_family and left_family == right_family)


def _technical_tier(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment,
    recommendation_score: RecommendationScore,
) -> tuple[str, tuple[str, ...], str]:
    probability = market_probability.effective_probability or 0
    score = recommendation_score.recommendation_score or 0
    edge = value_assessment.edge or 0
    ev = value_assessment.ev or 0
    value_score = value_assessment.value_score or recommendation_score.value_score or 0
    market_fit = recommendation_score.market_fit_score
    uncertainty = recommendation_score.uncertainty_penalty + value_assessment.sample_size_penalty
    league_uncertainty = value_assessment.league_uncertainty_penalty
    correlation = max(
        recommendation_score.correlation_penalty, value_assessment.correlation_penalty
    )
    volatility = value_assessment.market_volatility_penalty
    has_real_odds = bool(
        value_assessment.available_odds
        and not value_assessment.diagnostics.metadata.get("estimated_odds")
    )
    weak_market = _has_weak_market_flag(market_probability, recommendation_score)
    stable_profile = (
        not weak_market
        and recommendation_score.stale_data_penalty <= 0
        and uncertainty <= BANKER_MAX_UNCERTAINTY
        and league_uncertainty <= BANKER_MAX_UNCERTAINTY
        and volatility <= BANKER_MAX_VOLATILITY
    )
    good_market_fit = market_fit is None or market_fit >= VALUE_GEM_MIN_MARKET_FIT
    reasonable_sample = value_assessment.sample_size_penalty <= VALUE_GEM_MAX_SAMPLE_PENALTY
    meaningful_upside = (
        ev >= WILD_CARD_MIN_EV
        or value_score >= WILD_CARD_MIN_VALUE_SCORE
        or edge >= BANKER_MIN_EDGE
    )

    if (
        probability >= BANKER_MIN_PROBABILITY
        and score >= BANKER_MIN_SCORE
        and edge >= BANKER_MIN_EDGE
        and has_real_odds
        and stable_profile
        and correlation <= BANKER_MAX_CORRELATION
    ):
        return (
            Tier.BANKER,
            (
                "high_calibrated_probability",
                "low_uncertainty",
                "real_odds_available",
                "stable_league_market_profile",
                "low_correlation",
            ),
            "",
        )

    if (
        probability >= VALUE_GEM_MIN_PROBABILITY
        and score >= VALUE_GEM_MIN_SCORE
        and edge >= VALUE_GEM_MIN_EDGE
        and has_real_odds
        and good_market_fit
        and reasonable_sample
        and not weak_market
    ):
        return (
            Tier.VALUE_GEM,
            (
                "positive_edge",
                "fair_probability",
                "real_odds_available",
                "good_market_fit",
                "reasonable_sample",
            ),
            "",
        )

    if (
        probability >= WILD_CARD_MIN_PROBABILITY
        and score >= WILD_CARD_MIN_SCORE
        and meaningful_upside
        and good_market_fit
        and recommendation_score.weak_market_penalty < 10
    ):
        return (
            Tier.WILD_CARD,
            (
                "higher_variance",
                "meaningful_upside",
                "strong_enough_model_case",
                "reduced_stake_required",
            ),
            WILD_CARD_STAKE_WARNING,
        )

    return (
        Tier.WATCHLIST,
        tuple(
            _tier_blockers(
                probability=probability,
                score=score,
                edge=edge,
                has_real_odds=has_real_odds,
                weak_market=weak_market,
                stable_profile=stable_profile,
                good_market_fit=good_market_fit,
                reasonable_sample=reasonable_sample,
            )
        ),
        "",
    )


def _has_weak_market_flag(
    market_probability: MarketProbability,
    recommendation_score: RecommendationScore,
) -> bool:
    support_level = str(
        market_probability.diagnostics.metadata.get("market_support_level") or ""
    ).lower()
    warnings = {*market_probability.warnings, *recommendation_score.warnings}
    return bool(
        support_level in {"weak", "unsupported", "thin", "missing"}
        or recommendation_score.weak_market_penalty > 0
        or "weak_market_penalty" in warnings
        or bool(warnings & {
            "goal_line_boundary",
            "german_under_goals_market_blocked",
            "under25_goal_volatility",
            "under35_blowout_risk",
            "under45_high_goal_volatility",
            "corner_line_boundary",
            "corner_under_pressure_risk",
            "corner_over_margin_risk",
        })
    )


def _tier_blockers(
    *,
    probability: float,
    score: float,
    edge: float,
    has_real_odds: bool,
    weak_market: bool,
    stable_profile: bool,
    good_market_fit: bool,
    reasonable_sample: bool,
) -> tuple[str, ...]:
    blockers = []
    if probability < WILD_CARD_MIN_PROBABILITY:
        blockers.append("low_calibrated_probability")
    if score < WILD_CARD_MIN_SCORE:
        blockers.append("low_recommendation_score")
    if edge < VALUE_GEM_MIN_EDGE:
        blockers.append("weak_or_negative_edge")
    if not has_real_odds:
        blockers.append("missing_real_odds")
    if weak_market:
        blockers.append("weak_market_profile")
    if not stable_profile:
        blockers.append("unstable_league_market_profile")
    if not good_market_fit:
        blockers.append("weak_market_fit")
    if not reasonable_sample:
        blockers.append("thin_sample")
    return tuple(dict.fromkeys(blockers))


def _float_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
