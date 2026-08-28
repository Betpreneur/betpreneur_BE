"""Market correlation and exposure detection."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations

from betpreneur.modules.markets.api import MarketDescriptor, describe_market

from .contracts import (
    CorrelationPair,
    CorrelationReport,
    MarketProbability,
    PredictionDiagnostics,
)

CONCENTRATION_WARNING_SHARE = 0.40


def analyze_ticket_correlation(selections) -> CorrelationReport:
    """Detect related markets and concentration in a ticket-like selection list."""
    markets = tuple(item for item in selections or () if isinstance(item, MarketProbability))
    if not markets:
        return CorrelationReport(
            diagnostics=PredictionDiagnostics(
                data_quality="unavailable",
                model_sources=("prediction.correlation",),
                warnings=("no_market_probabilities",),
            )
        )

    fixture_exposure = dict(Counter(item.fixture_id for item in markets))
    family_exposure = dict(Counter(_family(item) for item in markets))
    pairs = tuple(_detect_pairs(markets))
    max_fixture_share = _max_share(fixture_exposure, len(markets))
    max_family_share = _max_share(family_exposure, len(markets))
    concentration_score = round(max(max_fixture_share, max_family_share) * 100.0, 2)
    warnings = _warnings(pairs, max_fixture_share, max_family_share)
    return CorrelationReport(
        pairs=pairs,
        fixture_exposure=fixture_exposure,
        market_family_exposure=family_exposure,
        max_fixture_share=max_fixture_share,
        max_market_family_share=max_family_share,
        concentration_score=concentration_score,
        warnings=warnings,
        diagnostics=PredictionDiagnostics(
            data_quality=_combined_quality(markets),
            model_sources=("prediction.correlation",),
            warnings=warnings,
            metadata={
                "pair_count": len(pairs),
                "selection_count": len(markets),
                "policy": "do_not_treat_related_legs_as_independent",
            },
        ),
    )


def _detect_pairs(markets: tuple[MarketProbability, ...]):
    by_fixture = defaultdict(list)
    for market in markets:
        by_fixture[market.fixture_id].append(market)

    for fixture_id, fixture_markets in by_fixture.items():
        if len(fixture_markets) < 2:
            continue
        for left, right in combinations(fixture_markets, 2):
            pair = _relationship(fixture_id, left, right)
            if pair is not None:
                yield pair


def _relationship(
    fixture_id: str,
    left: MarketProbability,
    right: MarketProbability,
) -> CorrelationPair | None:
    left_descriptor = describe_market(left.market)
    right_descriptor = describe_market(right.market)
    if not left_descriptor.recognized or not right_descriptor.recognized:
        return None

    exact = _exact_duplicate(fixture_id, left_descriptor, right_descriptor)
    if exact:
        return exact

    result = _result_relationship(fixture_id, left_descriptor, right_descriptor)
    if result:
        return result

    goals = _goal_relationship(fixture_id, left_descriptor, right_descriptor)
    if goals:
        return goals

    counts = _count_relationship(fixture_id, left_descriptor, right_descriptor)
    if counts:
        return counts

    if left_descriptor.family == right_descriptor.family:
        return _pair(
            fixture_id,
            left_descriptor,
            right_descriptor,
            relationship="same_family",
            direction="overlapping",
            strength=0.45,
            reason="Same fixture and same market family.",
        )
    return None


def _exact_duplicate(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
) -> CorrelationPair | None:
    if left.canonical and left.canonical == right.canonical:
        return _pair(
            fixture_id,
            left,
            right,
            relationship="duplicate_market",
            direction="nested",
            strength=1.0,
            reason="Both legs describe the same market on the same fixture.",
        )
    return None


def _result_relationship(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
) -> CorrelationPair | None:
    families = {left.family, right.family}
    if families <= {"match_result", "draw_no_bet", "double_chance", "asian_handicap", "handicap"}:
        if _result_sides_conflict(left, right):
            return _pair(
                fixture_id,
                left,
                right,
                "result_conflict",
                "conflicting",
                0.9,
                "Result legs cannot all win together.",
            )
        if _same_result_side(left, right):
            return _pair(
                fixture_id,
                left,
                right,
                "result_protection",
                "nested",
                0.8,
                "One result leg protects or implies the other.",
            )
        return _pair(
            fixture_id,
            left,
            right,
            "result_overlap",
            "overlapping",
            0.55,
            "Same-fixture result markets share match-state risk.",
        )
    return None


def _goal_relationship(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
) -> CorrelationPair | None:
    goal_families = {"total_goals", "team_total_goals", "btts"}
    if left.family not in goal_families or right.family not in goal_families:
        return None
    if {left.family, right.family} == {"total_goals", "btts"}:
        total = left if left.family == "total_goals" else right
        btts = right if total is left else left
        if (
            btts.side == "yes"
            and total.side == "over"
            and _line(total) is not None
            and _line(total) >= 2.5
        ):
            return _pair(
                fixture_id,
                left,
                right,
                "goals_btts",
                "reinforcing",
                0.7,
                "BTTS Yes and goal overs are driven by the same scoring path.",
            )
    if left.family == right.family == "total_goals":
        return _line_relationship(fixture_id, left, right, event="goals")
    return _pair(
        fixture_id,
        left,
        right,
        "goal_family",
        "overlapping",
        0.5,
        "Goal markets share the same scoreline distribution.",
    )


def _count_relationship(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
) -> CorrelationPair | None:
    count_groups = [
        {"corners_total", "team_corners"},
        {"cards_total", "team_cards", "booking_points"},
        {"shots_on_target_total", "team_shots_on_target"},
    ]
    if not any(left.family in group and right.family in group for group in count_groups):
        return None
    if left.family == right.family and left.family in {
        "corners_total",
        "cards_total",
        "shots_on_target_total",
    }:
        return _line_relationship(fixture_id, left, right, event=left.family)
    return _pair(
        fixture_id,
        left,
        right,
        "count_family",
        "overlapping",
        0.55,
        "Count markets on one fixture share event volume risk.",
    )


def _line_relationship(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
    *,
    event: str,
) -> CorrelationPair | None:
    left_line = _line(left)
    right_line = _line(right)
    if left_line is None or right_line is None:
        return None
    if left.side == right.side:
        return _pair(
            fixture_id,
            left,
            right,
            relationship=f"{event}_line_ladder",
            direction="nested",
            strength=0.85,
            reason="Same-side lines on one fixture are laddered versions of the same event count.",
        )
    return _pair(
        fixture_id,
        left,
        right,
        relationship=f"{event}_line_conflict",
        direction="conflicting",
        strength=0.75,
        reason="Opposite-side lines on one fixture can reduce or exclude each other.",
    )


def _same_result_side(left: MarketDescriptor, right: MarketDescriptor) -> bool:
    left_sides = _covered_result_sides(left)
    right_sides = _covered_result_sides(right)
    return bool(
        left_sides and right_sides and (left_sides <= right_sides or right_sides <= left_sides)
    )


def _result_sides_conflict(left: MarketDescriptor, right: MarketDescriptor) -> bool:
    left_sides = _covered_result_sides(left)
    right_sides = _covered_result_sides(right)
    return bool(left_sides and right_sides and not left_sides.intersection(right_sides))


def _covered_result_sides(descriptor: MarketDescriptor) -> set[str]:
    if descriptor.family in {"match_result", "draw_no_bet"}:
        return {descriptor.side}
    if descriptor.family in {"asian_handicap", "handicap"}:
        return {descriptor.side or descriptor.team}
    if descriptor.family == "double_chance":
        return {
            "home_or_draw": {"home", "draw"},
            "1x": {"home", "draw"},
            "draw_or_away": {"draw", "away"},
            "x2": {"draw", "away"},
            "home_or_away": {"home", "away"},
            "12": {"home", "away"},
        }.get(str(descriptor.side).lower(), set())
    return set()


def _pair(
    fixture_id: str,
    left: MarketDescriptor,
    right: MarketDescriptor,
    relationship: str,
    direction: str,
    strength: float,
    reason: str,
) -> CorrelationPair:
    return CorrelationPair(
        fixture_id=fixture_id,
        left_market=left.canonical or left.raw,
        right_market=right.canonical or right.raw,
        relationship=relationship,
        direction=direction,
        strength=round(strength, 2),
        reason=reason,
    )


def _line(descriptor: MarketDescriptor) -> float | None:
    if descriptor.line in (None, ""):
        return None
    try:
        return float(descriptor.line)
    except (TypeError, ValueError):
        return None


def _family(selection: MarketProbability) -> str:
    return str(
        selection.diagnostics.metadata.get("market_family")
        or describe_market(selection.market).family
        or selection.model
        or "unknown"
    )


def _max_share(values: dict[str, int], total: int) -> float:
    return round((max(values.values(), default=0) / max(total, 1)), 6)


def _warnings(
    pairs: tuple[CorrelationPair, ...],
    max_fixture_share: float,
    max_family_share: float,
) -> tuple[str, ...]:
    warnings = []
    if pairs:
        warnings.append("related_markets_detected")
        warnings.append("same_fixture_correlation")
    if any(pair.direction == "conflicting" for pair in pairs):
        warnings.append("conflicting_same_fixture_markets")
    if any(pair.direction == "nested" for pair in pairs):
        warnings.append("nested_same_fixture_markets")
    if max_fixture_share >= CONCENTRATION_WARNING_SHARE:
        warnings.append("fixture_exposure_concentration")
        warnings.append("concentrated_fixture_exposure")
    if max_family_share >= CONCENTRATION_WARNING_SHARE:
        warnings.append("market_family_concentration")
        warnings.append("concentrated_market_family")
    return tuple(warnings)


def _combined_quality(markets: tuple[MarketProbability, ...]) -> str:
    ranks = {
        "calibrated": 5,
        "strong": 4,
        "fresh": 4,
        "medium": 3,
        "limited": 2,
        "partial": 2,
        "poor": 1,
        "unavailable": 0,
        "unknown": 0,
    }
    return min(
        (str(item.data_quality or "unknown").lower() for item in markets),
        key=lambda item: ranks.get(item, 0),
    )
