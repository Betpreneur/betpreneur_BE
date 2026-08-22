"""
Ticket repair (ADR-004).

A repair is an evidence-based alternative, **not a promise of better returns**. The
revised ticket almost always carries lower odds, and the payload says so plainly rather
than presenting the swap as free money.

Alternatives are ranked by their effect on the ticket's estimated success, subject to
hard constraints. Ranking by probability alone would recommend `Under 8.5 Goals` on
every leg — extremely likely and completely useless.

Constraints, each of which rejects rather than merely down-ranks:

* **Assessment floor** — the alternative must come from a quantitative model. Swapping a
  modelled pick for a heuristic one is not a repair.
* **Intent preservation** — same fixture, and never the opposite side of the user's own
  thesis. Someone who backed the home team is not helped by being offered the away team.
* **Odds floor** — nothing below a minimum price, which is what stops the engine
  collapsing every ticket into near-certainties.
* **Correlation guard** — no alternative that duplicates another leg already on the
  ticket, which would add price without adding a real second condition.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MIN_ALTERNATIVE_ODDS = 1.10
MIN_LIFT_POINTS = 0.5

KEEP, REPLACE, DROP = "keep", "replace", "drop"

# Sides that back opposing outcomes on the same fixture.
_OPPOSING = [
    {"home", "away"},
    {"home", "draw_or_away"},
    {"away", "home_or_draw"},
    {"over", "under"},
    {"yes", "no"},
]


def _float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _side(payload) -> str:
    taxonomy = (payload or {}).get("market_taxonomy") or {}
    return str(taxonomy.get("side") or taxonomy.get("selection") or "").lower()


def contradicts(original, alternative) -> bool:
    """Whether the alternative backs the opposite of what the user chose."""
    first, second = _side(original), _side(alternative)
    if not first or not second:
        return False
    pair = {first, second}
    return any(pair == opposing for opposing in _OPPOSING)


@dataclass
class RepairDecision:
    index: int
    action: str
    match: str = ""
    original_market: str = ""
    original_odds: float | None = None
    revised_market: str = ""
    revised_odds: float | None = None
    lift_points: float | None = None
    reason: str = ""
    rejected: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class RepairPlan:
    decisions: list[RepairDecision]
    original_legs: int
    original_combined_odds: float | None
    original_success_percent: float | None
    revised_legs: int
    revised_combined_odds: float | None
    revised_success_percent: float | None
    disclosure: str

    @property
    def changes(self) -> int:
        return sum(1 for decision in self.decisions if decision.action != KEEP)

    def to_dict(self):
        payload = asdict(self)
        payload["decisions"] = [decision.to_dict() for decision in self.decisions]
        payload["changes"] = self.changes
        return payload


def _combined_odds(values) -> float | None:
    odds = [value for value in values if value and value > 1]
    if not odds:
        return None
    total = 1.0
    for value in odds:
        total *= value
    return round(total, 2)


def _leg_odds(item) -> float | None:
    provider = item.get("provider_payload") or {}
    return _float(provider.get("odds")) or _float((item.get("selected_market") or {}).get("odds"))


def _alternative(item):
    return item.get("replacement_market") or item.get("recommended_market") or {}


def _reject_reasons(item, alternative, leg_risk, existing_markets) -> list[str]:
    reasons = []
    if not alternative:
        return ["no_alternative_available"]
    if leg_risk is None or leg_risk.probability is None:
        reasons.append("original_not_assessed")
    if leg_risk is not None and leg_risk.repair_probability is None:
        reasons.append("alternative_not_better")
    alt_odds = _float(alternative.get("odds"))
    if alt_odds is not None and alt_odds < MIN_ALTERNATIVE_ODDS:
        reasons.append("below_minimum_odds")
    if contradicts(item, alternative):
        reasons.append("contradicts_original_thesis")
    market_name = str(alternative.get("market") or "")
    if market_name and market_name in existing_markets:
        reasons.append("duplicates_another_leg")
    if (alternative.get("statpal_advisory") or {}).get("assessment_type") not in (
        None, "quantitative_model",
    ):
        reasons.append("alternative_not_modelled")
    return reasons


def plan_repair(items, ticket_risk, *, decisions=None) -> RepairPlan:
    """
    Build a revised ticket.

    `decisions` maps a leg index to an explicit action, for the accept/reject flow. When
    omitted, every leg takes the recommended action.
    """
    decisions = {int(key): str(value) for key, value in (decisions or {}).items()}
    legs_by_index = {leg.index: leg for leg in ticket_risk.legs}
    existing_markets = {
        str((item.get("selected_market") or {}).get("market") or item.get("submitted_market") or "")
        for item in items
    }

    planned: list[RepairDecision] = []
    kept_odds: list[float | None] = []

    for index, item in enumerate(items):
        leg_risk = legs_by_index.get(index)
        alternative = _alternative(item)
        original_odds = _leg_odds(item)
        original_market = str(
            (item.get("selected_market") or {}).get("market") or item.get("submitted_market") or ""
        )
        rejected = _reject_reasons(item, alternative, leg_risk, existing_markets - {original_market})
        lift = leg_risk.repair_lift_points if leg_risk else None

        recommended = KEEP
        reason = "This selection is fine as it is."
        if not rejected and lift is not None and lift >= MIN_LIFT_POINTS:
            recommended = REPLACE
            reason = "A better-supported market is available for this fixture."
        elif leg_risk is not None and leg_risk.tier == "avoid" and rejected:
            recommended = DROP
            reason = "No defensible alternative exists for this selection."

        action = decisions.get(index, recommended)
        decision = RepairDecision(
            index=index,
            action=action,
            match=str(item.get("match") or ""),
            original_market=original_market,
            original_odds=original_odds,
            reason=reason,
            rejected=rejected,
            lift_points=lift,
        )

        if action == REPLACE and not rejected:
            decision.revised_market = str(alternative.get("market") or "")
            decision.revised_odds = _float(alternative.get("odds"))
            kept_odds.append(decision.revised_odds)
        elif action == DROP:
            pass  # leg leaves the ticket entirely
        else:
            decision.action = KEEP if action != DROP else DROP
            kept_odds.append(original_odds)

        planned.append(decision)

    original_odds_all = [_leg_odds(item) for item in items]
    revised_legs = sum(1 for decision in planned if decision.action != DROP)
    original_combined = _combined_odds(original_odds_all)
    revised_combined = _combined_odds(kept_odds)

    return RepairPlan(
        decisions=planned,
        original_legs=len(items),
        original_combined_odds=original_combined,
        original_success_percent=ticket_risk.success_percent,
        revised_legs=revised_legs,
        revised_combined_odds=revised_combined,
        revised_success_percent=ticket_risk.repaired_success_percent,
        disclosure=(
            "A repaired ticket is an evidence-based alternative, not a guarantee. "
            "Removing or replacing legs usually lowers the potential return."
        ),
    )
