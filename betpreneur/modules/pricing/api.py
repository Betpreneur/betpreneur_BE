"""Turning a distribution into a verdict — the public surface of pricing.

pricing owns the policy: whether a market is worth recommending, how much edge
a price carries, how risky a whole ticket is, and what to say about a leg that
cannot be modelled. It holds no tables and no opinion about *how* a fixture was
scored — that is scoring's job, below it.

Because the evidence for calibration lives above pricing, in slips, pricing
declares a source and the owning module registers one.
"""
from __future__ import annotations

from betpreneur.modules.markets.api import market_matches

from .contracts import (
    AllGamesPolicyAssessment,
    Calibration,
    LegState,
    SettledLeg,
    SlipReviewAlternative,
    SlipReviewPolicyAssessment,
    TopPicksPolicyAssessment,
)
from .domain.leg_state import assess_leg, may_publish_probability
from .domain.market_scoring import (
    SPECIALIST_REPLACEMENT_GROUPS,
    allows_broad_replacement,
    broad_fallback_candidate_allowed,
    count_model_line_from_evidence,
    effective_market_capability,
    float_or_none,
    goal_model_line_from_evidence,
    line_replacement_preserves_user_thesis,
    market_decision_rank,
    market_edge,
    market_family_group,
    market_owned_model_lines,
    market_profile_fit_score,
    market_similarity_score,
    market_sort_value,
    match_checker_alternative_reason,
    match_checker_status,
    normalise_market_name,
    period_or_family_line,
    replacement_scope,
    result_replacement_preserves_user_thesis,
    result_thesis_side,
    scored_claim,
    with_market_capability,
    with_statpal_advisory,
)
from .domain.tiers import Tier, tier_for_confidence
from .services.advisory import MAX_PLAUSIBLE_TEAM_EXPECTED_GOALS, statpal_market_advisory
from .services.calibration_source import (
    clear_calibration_source,
    register_calibration_source,
    settled_legs,
)
from .services.gating import (
    market_display_score,
    market_publicly_paused,
    setting_bool,
    with_match_checker_advisory,
)
from .services.product_policies import (
    assess_all_games_policy,
    assess_slip_review_policy,
    assess_top_picks_policy,
)
from .services.recommendation_policy import (
    assess_calibration_trust,
    assess_league_market_trust,
    assess_recommendation,
)
from .services.ticket_risk import (
    SCORE_BANDS,
    TicketRiskService,
    risk_level_for,
    ticket_risk_service,
)

__all__ = [
    "MAX_PLAUSIBLE_TEAM_EXPECTED_GOALS",
    "SCORE_BANDS",
    "SPECIALIST_REPLACEMENT_GROUPS",
    "AllGamesPolicyAssessment",
    "Calibration",
    "LegState",
    "SettledLeg",
    "SlipReviewAlternative",
    "SlipReviewPolicyAssessment",
    "TicketRiskService",
    "Tier",
    "TopPicksPolicyAssessment",
    "allows_broad_replacement",
    "annotations",
    "assess_all_games_policy",
    "assess_calibration_trust",
    "assess_league_market_trust",
    "assess_leg",
    "assess_recommendation",
    "assess_slip_review_policy",
    "assess_top_picks_policy",
    "broad_fallback_candidate_allowed",
    "clear_calibration_source",
    "count_model_line_from_evidence",
    "effective_market_capability",
    "float_or_none",
    "goal_model_line_from_evidence",
    "line_replacement_preserves_user_thesis",
    "market_decision_rank",
    "market_display_score",
    "market_edge",
    "market_family_group",
    "market_owned_model_lines",
    "market_profile_fit_score",
    "market_publicly_paused",
    "market_similarity_score",
    "market_sort_value",
    "match_checker_alternative_reason",
    "match_checker_status",
    "may_publish_probability",
    "normalise_market_name",
    "period_or_family_line",
    "register_calibration_source",
    "replacement_scope",
    "result_replacement_preserves_user_thesis",
    "result_thesis_side",
    "risk_level_for",
    "scored_claim",
    "setting_bool",
    "settled_legs",
    "statpal_market_advisory",
    "ticket_risk_service",
    "tier_for_confidence",
    "with_market_capability",
    "with_match_checker_advisory",
    "with_statpal_advisory",
]
