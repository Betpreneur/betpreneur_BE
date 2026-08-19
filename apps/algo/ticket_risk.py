"""
Ticket-level risk model for the Match Checker.

An accumulator pays only if every leg lands, so ticket probability is multiplicative::

    P(ticket) = product of p_i over all legs

Everything this module reports -- ticket health, which legs carry the risk, and what
repairing a leg is actually worth -- is derived from that single identity rather than
from an average of per-leg scores. Averaging is what made a 15-leg ticket unable to
say which three legs were killing it.

Leg probabilities come from calibrating the 0-100 advisory score against legs that
have actually settled (``SlipSelection``). Until enough legs have settled, the
calibration shrinks toward a deliberately conservative prior and reports
``basis="prior"``, so the API never presents an unvalidated number as if it had been
measured. This is the honest half of the feature: the percentage is only as good as
the evidence behind it, and the payload says which it is.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .leg_state import may_publish_probability


# Band edges align with the status thresholds users already see (avoid / caution /
# playable / strong), so a calibration table can be read against the same labels.
SCORE_BANDS = (
    ("avoid", 0.0, 55.0),
    ("caution", 55.0, 66.0),
    ("playable", 66.0, 78.0),
    ("strong", 78.0, 100.01),
)

# How much of an uncalibrated score we are willing to believe. A raw score of 85 does
# not mean an 85% chance of landing; it means the model liked it. Until settled data
# says otherwise we shrink toward a coin flip rather than flatter the ticket.
PRIOR_TRUST = 0.6

# Beta-style shrinkage weight, in pseudo-observations. A band needs roughly this many
# settled legs before its empirical rate outweighs the prior.
PRIOR_WEIGHT = 25

# Below this many settled legs in total, the calibration is reported as prior-based.
MIN_EMPIRICAL_SAMPLE = 40

PROBABILITY_FLOOR = 0.05
PROBABILITY_CEILING = 0.92

VERY_STRONG = "very_strong"
STRONG = "strong"
BORDERLINE = "borderline"
RISKY = "risky"
AVOID = "avoid"
UNKNOWN = "unknown"

TIER_THRESHOLDS = (
    (0.75, VERY_STRONG),
    (0.65, STRONG),
    (0.55, BORDERLINE),
    (0.45, RISKY),
)

TIER_LABELS = {
    VERY_STRONG: "Very Strong",
    STRONG: "Strong",
    BORDERLINE: "Borderline",
    RISKY: "Risky",
    AVOID: "Avoid",
    UNKNOWN: "Not assessed",
}

# Data quality governs whether we are entitled to an opinion at all.
#
# Low confidence is not the same as low probability: a market we cannot see well is
# unknown, not doomed. So the worst coverage bands make a leg *unassessed* -- excluded
# from the ticket estimate and reported as such -- rather than being scored down into
# "avoid", which would assert something the evidence does not support. Merely thin
# coverage still yields an estimate, but cannot reach a headline tier.
UNASSESSABLE_QUALITY = {"poor", "unsupported"}
THIN_DATA_QUALITY = {"limited"}
THIN_DATA_TIER_CAP = BORDERLINE
TIER_ORDER = [AVOID, RISKY, BORDERLINE, STRONG, VERY_STRONG]

NO_SCORE = "no_advisory_score"
INSUFFICIENT_DATA = "insufficient_market_data"
HEURISTIC_ONLY = "heuristic_assessment_only"


def _float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _band_for(score: float) -> str:
    for name, low, high in SCORE_BANDS:
        if low <= score < high:
            return name
    return "strong" if score >= 78 else "avoid"


def _prior_probability(score: float) -> float:
    """Conservative prior: half-trust the raw score, shrunk toward a coin flip."""
    naive = max(PROBABILITY_FLOOR, min(PROBABILITY_CEILING, score / 100.0))
    return 0.5 + (naive - 0.5) * PRIOR_TRUST


def _ticket_success_percent(probability: float) -> float:
    percent = max(0.0, min(1.0, probability)) * 100
    if percent == 0:
        return 0.0
    if percent >= 0.01:
        return round(percent, 2)
    # Long accumulators can be genuinely below 0.01%; keep significant digits so
    # the API does not report a non-zero probability as exactly 0.0.
    return float(f"{percent:.4g}")


@dataclass(frozen=True)
class Calibration:
    basis: str
    sample_size: int
    bands: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def probability(self, score: float) -> float:
        prior = _prior_probability(score)
        band = self.bands.get(_band_for(score)) or {}
        wins = int(band.get("wins") or 0)
        settled = int(band.get("settled") or 0)
        if not settled:
            return prior
        blended = (wins + PRIOR_WEIGHT * prior) / (settled + PRIOR_WEIGHT)
        return max(PROBABILITY_FLOOR, min(PROBABILITY_CEILING, blended))


@dataclass(frozen=True)
class LegRisk:
    index: int
    probability: float | None
    tier: str
    tier_label: str
    risk_share_percent: float | None
    repair_probability: float | None
    repair_lift_points: float | None
    drop_lift_points: float | None
    capped_by_data_quality: bool
    unassessed_reason: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TicketRisk:
    success_percent: float | None
    # Geometric mean of the leg probabilities: "how good are these legs", independent
    # of how many there are. Raw success_percent is not a health score -- a 15-leg
    # ticket of excellent legs still has a tiny product.
    health_percent: float | None
    assessed_legs: int
    unassessed_legs: int
    tier_counts: dict[str, int]
    legs: list[LegRisk]
    killers: list[dict[str, Any]]
    calibration: Calibration
    repaired_success_percent: float | None
    # How far same-fixture legs depart from independence (ADR-002). An empty dict or a
    # factor of 1.0 means every leg was on its own match, or the dependence between
    # them was not representable on the score grid.
    correlation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        payload = asdict(self)
        payload["legs"] = [leg.to_dict() for leg in self.legs]
        payload["calibration"] = self.calibration.to_dict()
        return payload


class TicketRiskService:
    def calibration(self) -> Calibration:
        """Fit score band -> hit rate from legs that have actually settled."""
        from .models import SlipSelection

        rows = (
            SlipSelection.objects.filter(
                outcome__in=[SlipSelection.Outcome.WIN, SlipSelection.Outcome.LOSS],
                advisory_score__isnull=False,
            )
            .values_list("advisory_score", "outcome")
        )

        bands: dict[str, dict[str, int]] = {name: {"wins": 0, "settled": 0} for name, _, _ in SCORE_BANDS}
        total = 0
        for score, outcome in rows.iterator(chunk_size=1000):
            band = bands[_band_for(float(score))]
            band["settled"] += 1
            total += 1
            if outcome == SlipSelection.Outcome.WIN:
                band["wins"] += 1

        for name, stats in bands.items():
            settled = stats["settled"]
            stats["hit_rate_percent"] = round((stats["wins"] / settled) * 100, 1) if settled else None

        if total >= MIN_EMPIRICAL_SAMPLE:
            basis = "blended"
        else:
            basis = "prior"
            # Not enough evidence to move off the prior at all.
            bands = {name: {"wins": 0, "settled": 0, "hit_rate_percent": None} for name, _, _ in SCORE_BANDS}

        return Calibration(basis=basis, sample_size=total, bands=bands)

    def assess(self, items: Iterable[dict[str, Any]], *, calibration: Calibration | None = None) -> TicketRisk:
        items = list(items)
        calibration = calibration or self.calibration()

        legs: list[LegRisk] = []
        probabilities: list[float] = []
        for index, item in enumerate(items):
            score = _leg_score(item)
            reason = ""
            if score is None:
                reason = NO_SCORE
            elif _data_quality(item) in UNASSESSABLE_QUALITY and not _has_scored_quantitative_advisory(item):
                # We can see this market too poorly to claim anything either way.
                reason = INSUFFICIENT_DATA
            elif not may_publish_probability(item):
                # A heuristic score is a constant plus context nudges. Multiplying it
                # into a ticket probability would dress a guess up as arithmetic.
                reason = HEURISTIC_ONLY
            if reason:
                legs.append(
                    LegRisk(
                        index=index,
                        probability=None,
                        tier=UNKNOWN,
                        tier_label=TIER_LABELS[UNKNOWN],
                        risk_share_percent=None,
                        repair_probability=None,
                        repair_lift_points=None,
                        drop_lift_points=None,
                        capped_by_data_quality=False,
                        unassessed_reason=reason,
                    )
                )
                continue

            probability = calibration.probability(score)
            probability, capped = _apply_capability_cap(probability, item)
            probabilities.append(probability)

            repair_score = _repair_score(item)
            repair_probability = None
            if repair_score is not None:
                repaired, _ = _apply_capability_cap(calibration.probability(repair_score), item)
                # Only call it a repair when it genuinely improves the leg.
                repair_probability = repaired if repaired > probability else None

            tier = _tier_for(probability, item)
            legs.append(
                LegRisk(
                    index=index,
                    probability=round(probability, 4),
                    tier=tier,
                    tier_label=TIER_LABELS[tier],
                    risk_share_percent=None,
                    repair_probability=round(repair_probability, 4) if repair_probability else None,
                    repair_lift_points=None,
                    drop_lift_points=None,
                    capped_by_data_quality=capped,
                )
            )

        if not probabilities:
            return TicketRisk(
                success_percent=None,
                health_percent=None,
                assessed_legs=0,
                unassessed_legs=len(items),
                tier_counts=_tier_counts(legs),
                legs=legs,
                killers=[],
                calibration=calibration,
                repaired_success_percent=None,
            )

        ticket_probability = 1.0
        for probability in probabilities:
            ticket_probability *= probability

        # Legs sharing a fixture are not independent; adjust the product rather than
        # replace the calibrated marginals (ADR-002).
        correlation = _correlation_for(items, legs)
        ticket_probability = min(1.0, ticket_probability * correlation.factor)

        # Each leg's share of the ticket's total improbability. Using -ln(p) makes the
        # shares additive, which is what lets us say "these three carry 68% of the risk".
        total_improbability = sum(-math.log(p) for p in probabilities) or 1.0

        resolved: list[LegRisk] = []
        for leg in legs:
            if leg.probability is None:
                resolved.append(leg)
                continue
            improbability = -math.log(leg.probability)
            drop_lift = (ticket_probability / leg.probability) - ticket_probability
            repair_lift = None
            if leg.repair_probability:
                repair_lift = ticket_probability * (leg.repair_probability / leg.probability) - ticket_probability
            resolved.append(
                LegRisk(
                    index=leg.index,
                    probability=leg.probability,
                    tier=leg.tier,
                    tier_label=leg.tier_label,
                    risk_share_percent=round((improbability / total_improbability) * 100, 1),
                    repair_probability=leg.repair_probability,
                    repair_lift_points=round(repair_lift * 100, 2) if repair_lift is not None else None,
                    drop_lift_points=round(drop_lift * 100, 2),
                    capped_by_data_quality=leg.capped_by_data_quality,
                )
            )

        repaired_probability = ticket_probability
        for leg in resolved:
            if leg.probability and leg.repair_probability:
                repaired_probability *= leg.repair_probability / leg.probability

        geometric_mean = math.exp(sum(math.log(p) for p in probabilities) / len(probabilities))

        return TicketRisk(
            success_percent=_ticket_success_percent(ticket_probability),
            health_percent=round(geometric_mean * 100, 1),
            assessed_legs=len(probabilities),
            unassessed_legs=len(items) - len(probabilities),
            tier_counts=_tier_counts(resolved),
            legs=resolved,
            killers=_killers(resolved, items),
            calibration=calibration,
            repaired_success_percent=_ticket_success_percent(repaired_probability * correlation.factor),
            correlation=correlation.to_dict(),
        )


def _fixture_key(item: dict[str, Any]) -> str:
    matched = item.get("matched_fixture") or {}
    return str(matched.get("match_id") or item.get("match_id") or "")


def _correlation_for(items: list[dict[str, Any]], legs: list[LegRisk]):
    """
    Build the same-fixture dependence adjustment.

    Only legs that carry a probability participate; an unassessed leg contributes no
    marginal to adjust. Fixtures with a single leg are skipped entirely.
    """
    from .scoring.correlation import CorrelationResult, combine
    from .scoring.service import score_model_service

    assessed = {leg.index for leg in legs if leg.probability is not None}
    grouped: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if index not in assessed:
            continue
        key = _fixture_key(item)
        if key:
            grouped.setdefault(key, []).append(index)

    multi = {key: indexes for key, indexes in grouped.items() if len(indexes) > 1}
    assessed_count = len(assessed)
    if not multi:
        return CorrelationResult(factor=1.0, correlated_groups=0, adjusted_legs=0, skipped_legs=assessed_count)

    groups = []
    for indexes in multi.values():
        first = items[indexes[0]]
        matched = first.get("matched_fixture") or {}
        rates = score_model_service.rates_for_fixture(
            league_id=matched.get("league_id") or matched.get("code") or "",
            home_team_name=matched.get("home_team") or "",
            away_team_name=matched.get("away_team") or "",
        )
        if not rates.usable:
            continue
        legs_spec = []
        for index in indexes:
            taxonomy = items[index].get("market_taxonomy") or {}
            legs_spec.append(
                (
                    taxonomy.get("family") or "",
                    taxonomy.get("side") or taxonomy.get("selection") or "",
                    taxonomy.get("line"),
                    taxonomy.get("team") or "",
                )
            )
        groups.append((rates.matrix(), legs_spec))

    if not groups:
        return CorrelationResult(factor=1.0, correlated_groups=0, adjusted_legs=0, skipped_legs=assessed_count)
    result = combine(groups)
    return CorrelationResult(
        factor=result.factor,
        correlated_groups=result.correlated_groups,
        adjusted_legs=result.adjusted_legs,
        skipped_legs=max(0, assessed_count - result.adjusted_legs),
    )


def _tier_counts(legs: list[LegRisk]) -> dict[str, int]:
    counts = {tier: 0 for tier in TIER_LABELS}
    for leg in legs:
        counts[leg.tier] = counts.get(leg.tier, 0) + 1
    return counts


def risk_level_for(ticket: TicketRisk) -> str:
    """Ticket-level risk band, driven by leg tiers rather than verdict counts."""
    if not ticket.assessed_legs or ticket.health_percent is None:
        return "unknown"
    counts = ticket.tier_counts
    if counts.get(AVOID) or counts.get(RISKY, 0) >= 3 or ticket.health_percent < 55:
        return "high"
    if counts.get(RISKY) or counts.get(BORDERLINE, 0) >= 2 or ticket.health_percent < 65:
        return "medium"
    return "low"


def _killers(legs: list[LegRisk], items: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    """The legs carrying the most risk, worst first."""
    ranked = sorted(
        [leg for leg in legs if leg.risk_share_percent is not None and leg.tier in {RISKY, AVOID, BORDERLINE}],
        key=lambda leg: leg.risk_share_percent,
        reverse=True,
    )[:limit]
    killers = []
    for leg in ranked:
        item = items[leg.index] if leg.index < len(items) else {}
        killers.append({
            "index": leg.index,
            "match": item.get("match", ""),
            "submitted_market": item.get("submitted_market", ""),
            "tier": leg.tier,
            "tier_label": leg.tier_label,
            "probability_percent": round((leg.probability or 0) * 100, 1),
            "risk_share_percent": leg.risk_share_percent,
            "repair_lift_points": leg.repair_lift_points,
            "drop_lift_points": leg.drop_lift_points,
        })
    return killers


def _leg_score(item: dict[str, Any]) -> float | None:
    market = item.get("selected_market") or {}
    return _float_or_none(item.get("advisory_score") or market.get("advisory_score"))


def _repair_score(item: dict[str, Any]) -> float | None:
    replacement = item.get("replacement_market") or item.get("recommended_market") or {}
    return _float_or_none(replacement.get("advisory_score"))


def _capability(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("market_capability") or (item.get("selected_market") or {}).get("market_capability") or {}


def _data_quality(item: dict[str, Any]) -> str:
    return str(_capability(item).get("data_quality") or "").lower()


def _has_scored_quantitative_advisory(item: dict[str, Any]) -> bool:
    from .evaluators.registry import QUANTITATIVE, assessment_type_for

    market = item.get("selected_market") or {}
    advisory = market.get("statpal_advisory") or item.get("statpal_advisory") or {}
    taxonomy = item.get("market_taxonomy") or market.get("market_taxonomy") or {}
    family = taxonomy.get("family") or ""
    assessment_type = advisory.get("assessment_type") or assessment_type_for(family)
    return (
        assessment_type == QUANTITATIVE
        and bool(advisory.get("available"))
        and (advisory.get("score") is not None or _leg_score(item) is not None)
    )


def _apply_capability_cap(probability: float, item: dict[str, Any]) -> tuple[float, bool]:
    """Thin StatPal coverage caps how confident the model is allowed to sound."""
    cap = _float_or_none(_capability(item).get("confidence_cap"))
    if cap is None or cap <= 0:
        return probability, False
    ceiling = max(PROBABILITY_FLOOR, min(PROBABILITY_CEILING, cap / 100.0))
    if probability > ceiling:
        return ceiling, True
    return probability, False


def _tier_for(probability: float, item: dict[str, Any]) -> str:
    tier = AVOID
    for threshold, name in TIER_THRESHOLDS:
        if probability >= threshold:
            tier = name
            break
    if _data_quality(item) in THIN_DATA_QUALITY and TIER_ORDER.index(tier) > TIER_ORDER.index(THIN_DATA_TIER_CAP):
        return THIN_DATA_TIER_CAP
    return tier


ticket_risk_service = TicketRiskService()
