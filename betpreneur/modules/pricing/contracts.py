"""Types crossing the pricing boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .domain.leg_state import LegState
from .services.calibration_source import SettledLeg
from .services.ticket_risk import Calibration


def _as_payload(value) -> dict[str, Any]:
    return asdict(value)


@dataclass(frozen=True, slots=True)
class AllGamesPolicyAssessment:
    product: str = "all_games"
    fixture_id: str = ""
    market: str = ""
    raw_probability: float | None = None
    calibrated_probability: float | None = None
    data_confidence: float | None = None
    data_quality: str = "unknown"
    explanation_facts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    selection_bias: str = "coverage"
    aggressive_profit_claims: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class TopPicksPolicyAssessment:
    product: str = "top_picks"
    fixture_id: str = ""
    market: str = ""
    exposure_score: float | None = None
    tier: str = "watchlist"
    publishable: bool = False
    calibrated_probability: float | None = None
    edge: float | None = None
    ev: float | None = None
    real_odds_required: bool = True
    reasons: tuple[str, ...] = ()
    tier_reasons: tuple[str, ...] = ()
    stake_warning: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class SlipReviewAlternative:
    market: str
    confidence_score: float | None
    confidence_delta: float | None
    thesis_preserved: bool
    family_distance: int = 0
    market_fit_score: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class SlipReviewPolicyAssessment:
    product: str = "slip_review"
    fixture_id: str = ""
    market: str = ""
    supported: bool = False
    verdict: str = "review"
    user_confidence_score: float | None = None
    suggested_alternative: SlipReviewAlternative | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


__all__ = [
    "AllGamesPolicyAssessment",
    "Calibration",
    "LegState",
    "SettledLeg",
    "SlipReviewAlternative",
    "SlipReviewPolicyAssessment",
    "TopPicksPolicyAssessment",
]
