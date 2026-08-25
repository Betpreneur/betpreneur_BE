"""
Same-fixture correlation adjustment (ADR-002).

Two legs on one match are not independent, so multiplying their marginals misstates the
ticket. Rather than replace the calibrated probabilities — which would throw away the
calibration built from settled outcomes — we compute how far the *model* thinks the pair
departs from independence and apply that as a factor:

    factor = P_model(A and B) / (P_model(A) * P_model(B))

The calibrated marginals still carry the empirical correction; the factor carries the
dependence structure. A factor above 1 means the legs reinforce each other, which is the
usual case for `Home Win` + `Over 2.5`.

**Cross-fixture correlation is deliberately out of scope.** Two matches in the same
league on the same day are weakly related; modelling that is a much larger exercise and
the assumption is recorded rather than quietly made.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dixon_coles import ScoreMatrix
from .predicates import predicate_for

# Guard rails. A factor outside this band means the pair is near-degenerate (mutually
# exclusive, or one implying the other), where the ratio is numerically unstable.
MIN_FACTOR = 0.2
MAX_FACTOR = 5.0
_EPSILON = 1e-9


@dataclass(frozen=True)
class CorrelationResult:
    factor: float
    correlated_groups: int
    adjusted_legs: int
    skipped_legs: int

    @property
    def applied(self) -> bool:
        return self.correlated_groups > 0

    def to_dict(self):
        return {
            "applied": self.applied,
            "factor": round(self.factor, 6),
            "correlated_groups": self.correlated_groups,
            "adjusted_legs": self.adjusted_legs,
            "legs_assumed_independent": self.skipped_legs,
            "cross_fixture_correlation": "not_modelled",
        }


def group_factor(matrix: ScoreMatrix, legs) -> tuple[float, int]:
    """
    Dependence factor for the representable legs of one fixture.

    `legs` are `(family, side, line, team)` tuples. Returns the factor and how many legs
    it covers; legs that cannot be expressed as a predicate are excluded and reported.
    """
    predicates = []
    for family, side, line, team in legs:
        predicate = predicate_for(family, side, line=line, team=team)
        if predicate is not None:
            predicates.append(predicate)

    if len(predicates) < 2:
        return 1.0, 0

    joint = matrix.sum_where(lambda home, away: all(test(home, away) for test in predicates))

    independent = 1.0
    for predicate in predicates:
        independent *= matrix.sum_where(predicate)

    if independent <= _EPSILON:
        return 1.0, 0
    factor = joint / independent
    return min(MAX_FACTOR, max(MIN_FACTOR, factor)), len(predicates)


def combine(groups) -> CorrelationResult:
    """
    Fold per-fixture factors into one ticket-level adjustment.

    `groups` are `(matrix, legs)` pairs, one per fixture carrying more than one leg.
    """
    total_factor = 1.0
    correlated = adjusted = skipped = 0
    for matrix, legs in groups:
        factor, covered = group_factor(matrix, legs)
        if covered >= 2:
            total_factor *= factor
            correlated += 1
            adjusted += covered
            skipped += len(legs) - covered
        else:
            skipped += len(legs)
    return CorrelationResult(
        factor=min(MAX_FACTOR, max(MIN_FACTOR, total_factor)),
        correlated_groups=correlated,
        adjusted_legs=adjusted,
        skipped_legs=skipped,
    )
