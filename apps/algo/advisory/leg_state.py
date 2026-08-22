"""
Leg lifecycle state.

Every leg walks one pipeline, and each stage has an explicit terminal. The API reports
**where a leg stopped** rather than coercing it into a verdict — the same principle that
stopped un-analysed picks rendering as "avoid" and fabricated tracking claiming legs were
being followed.

    PARSED ─► RECOGNIZED ─► FIXTURE_RESOLVED ─► DATA_AVAILABLE ─► MODEL_AVAILABLE ─► ASSESSED
                  │                │                   │                  │
                  ▼                ▼                   ▼                  ▼
            UNKNOWN_MARKET     UNMATCHED        INSUFFICIENT_DATA      NO_MODEL
                               AMBIGUOUS_FIXTURE
                               EXPIRED

`assessment_type` travels alongside the state and carries the invariant that keeps the
product honest: a probability may only ever be published for a `quantitative_model`
assessment. A heuristic score is a signal, not a probability, and must never be
presented as one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from ..scoring.api import HEURISTIC, NONE, QUANTITATIVE, assessment_type_for


class LegState(StrEnum):
    ASSESSED = "assessed"
    UNKNOWN_MARKET = "unknown_market"
    UNMATCHED = "unmatched"
    AMBIGUOUS_FIXTURE = "ambiguous_fixture"
    EXPIRED = "expired"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_MODEL = "no_model"


TERMINAL_MESSAGES = {
    LegState.UNKNOWN_MARKET: "This market could not be identified from the bookmaker's data.",
    LegState.UNMATCHED: "The fixture for this selection could not be found.",
    LegState.AMBIGUOUS_FIXTURE: "More than one fixture matched this selection.",
    LegState.EXPIRED: "This fixture has already started.",
    LegState.INSUFFICIENT_DATA: "There is not enough data on this market to assess it.",
    LegState.NO_MODEL: "This market is recognised but not modelled yet.",
    LegState.ASSESSED: "",
}

UNASSESSABLE_DATA_QUALITY = {"poor", "unsupported"}


@dataclass(frozen=True)
class LegAssessment:
    state: LegState
    assessment_type: str
    family: str = ""
    message: str = ""

    @property
    def may_publish_probability(self) -> bool:
        return self.state == LegState.ASSESSED and self.assessment_type == QUANTITATIVE

    def to_dict(self):
        payload = asdict(self)
        payload["state"] = str(self.state)
        payload["may_publish_probability"] = self.may_publish_probability
        return payload


def _selected_market(item):
    return item.get("selected_market") or {}


def _family(item):
    taxonomy = item.get("market_taxonomy") or _selected_market(item).get("market_taxonomy") or {}
    return taxonomy.get("family") or ""


def _capability(item):
    return item.get("market_capability") or _selected_market(item).get("market_capability") or {}


def _advisory(item):
    return _selected_market(item).get("statpal_advisory") or {}


def _has_scored_quantitative_advisory(item, assessment_type: str) -> bool:
    advisory = _advisory(item)
    if assessment_type != QUANTITATIVE:
        return False
    if not advisory.get("available"):
        return False
    return (
        advisory.get("score") is not None
        or _selected_market(item).get("advisory_score") is not None
        or item.get("advisory_score") is not None
    )


def assess_leg(item) -> LegAssessment:
    """Derive the lifecycle state of a single analysed selection."""
    family = _family(item)
    canonical = item.get("canonical_market") or {}
    taxonomy = item.get("market_taxonomy") or {}
    status = item.get("status") or ""

    if status == "expired":
        return LegAssessment(LegState.EXPIRED, NONE, family, TERMINAL_MESSAGES[LegState.EXPIRED])
    if status == "ambiguous_match":
        return LegAssessment(
            LegState.AMBIGUOUS_FIXTURE, NONE, family, TERMINAL_MESSAGES[LegState.AMBIGUOUS_FIXTURE]
        )
    if status == "unmatched":
        return LegAssessment(LegState.UNMATCHED, NONE, family, TERMINAL_MESSAGES[LegState.UNMATCHED])

    # An identity we had to guess from display text is not a confident identification.
    unresolved_identity = canonical.get("resolution") == "unresolved"
    if unresolved_identity or (taxonomy and not taxonomy.get("recognized", True)) or not family:
        return LegAssessment(
            LegState.UNKNOWN_MARKET, NONE, family, TERMINAL_MESSAGES[LegState.UNKNOWN_MARKET]
        )

    assessment_type = _advisory(item).get("assessment_type") or assessment_type_for(family)
    if assessment_type == NONE:
        return LegAssessment(LegState.NO_MODEL, NONE, family, TERMINAL_MESSAGES[LegState.NO_MODEL])

    data_quality = str(_capability(item).get("data_quality") or "").lower()
    scored = _selected_market(item).get("advisory_score") is not None or item.get("advisory_score") is not None
    advisory_scored = _has_scored_quantitative_advisory(item, assessment_type)
    if (data_quality in UNASSESSABLE_DATA_QUALITY and not advisory_scored) or not scored:
        return LegAssessment(
            LegState.INSUFFICIENT_DATA,
            assessment_type,
            family,
            TERMINAL_MESSAGES[LegState.INSUFFICIENT_DATA],
        )

    return LegAssessment(LegState.ASSESSED, assessment_type, family)


def may_publish_probability(item) -> bool:
    """Whether this leg's score is a real probability estimate, not a heuristic signal."""
    return assess_leg(item).may_publish_probability


__all__ = [
    "HEURISTIC",
    "NONE",
    "QUANTITATIVE",
    "LegAssessment",
    "LegState",
    "assess_leg",
    "may_publish_probability",
]
