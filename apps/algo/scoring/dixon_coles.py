"""
Per-fixture score distribution (Dixon-Coles).

One model, from which every goals-and-result market is derived by summation. Building
fifteen unrelated evaluators is what let `P(Home Win)` and `P(1X)` come from different
heuristics and contradict each other; from a shared matrix they are consistent by
construction.

Goals are modelled as two Poisson processes with the Dixon-Coles low-score correction.
Plain independent Poisson materially under-predicts draws, and draws are exactly where
Double Chance lives — the market we most want for repairs — so the correction is not
optional here.

Nothing in this module touches the database or the network. Team strengths are fitted
elsewhere (nightly, per league); at request time this is arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_GOALS = 8

# Dixon-Coles dependence parameter. Negative values lift 0-0 and 1-1 while damping
# 1-0 and 0-1, which is the empirically observed low-score behaviour.
DEFAULT_RHO = -0.13

# Keep every tau factor strictly positive, otherwise the matrix can carry negative mass.
_MIN_TAU = 1e-6


def _poisson_pmf(k: int, rate: float) -> float:
    if rate <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-rate) * rate**k / math.factorial(k)


def tau(home_goals: int, away_goals: int, home_rate: float, away_rate: float, rho: float) -> float:
    """Dixon-Coles correction, applied only to the four lowest scorelines."""
    if home_goals == 0 and away_goals == 0:
        value = 1.0 - home_rate * away_rate * rho
    elif home_goals == 0 and away_goals == 1:
        value = 1.0 + home_rate * rho
    elif home_goals == 1 and away_goals == 0:
        value = 1.0 + away_rate * rho
    elif home_goals == 1 and away_goals == 1:
        value = 1.0 - rho
    else:
        value = 1.0
    return max(value, _MIN_TAU)


@dataclass(frozen=True)
class ScoreMatrix:
    """P(home_goals=h, away_goals=a) over a truncated, renormalised grid."""

    grid: tuple[tuple[float, ...], ...]
    home_rate: float
    away_rate: float
    rho: float

    @property
    def max_goals(self) -> int:
        return len(self.grid) - 1

    def probability(self, home_goals: int, away_goals: int) -> float:
        if not (0 <= home_goals <= self.max_goals and 0 <= away_goals <= self.max_goals):
            return 0.0
        return self.grid[home_goals][away_goals]

    def sum_where(self, predicate) -> float:
        """Total probability mass over cells satisfying `predicate(home, away)`."""
        total = 0.0
        for home_goals, row in enumerate(self.grid):
            for away_goals, cell in enumerate(row):
                if predicate(home_goals, away_goals):
                    total += cell
        return min(1.0, max(0.0, total))

    def home_goal_distribution(self) -> tuple[float, ...]:
        return tuple(sum(row) for row in self.grid)

    def away_goal_distribution(self) -> tuple[float, ...]:
        return tuple(sum(row[index] for row in self.grid) for index in range(self.max_goals + 1))

    def expected_goals(self) -> tuple[float, float]:
        home = sum(goals * mass for goals, mass in enumerate(self.home_goal_distribution()))
        away = sum(goals * mass for goals, mass in enumerate(self.away_goal_distribution()))
        return round(home, 4), round(away, 4)


def build_score_matrix(
    home_rate: float,
    away_rate: float,
    *,
    rho: float = DEFAULT_RHO,
    max_goals: int = MAX_GOALS,
) -> ScoreMatrix:
    """
    Build the joint scoreline distribution for one fixture.

    `home_rate` / `away_rate` are expected goals for each side. The grid is truncated at
    `max_goals` and renormalised, so the tail beyond it is redistributed rather than
    silently lost — the probabilities must sum to one for every derived market to be
    trustworthy.
    """
    home_rate = max(0.0, float(home_rate))
    away_rate = max(0.0, float(away_rate))

    home_pmf = [_poisson_pmf(goals, home_rate) for goals in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(goals, away_rate) for goals in range(max_goals + 1)]

    raw = [
        [
            home_pmf[home_goals]
            * away_pmf[away_goals]
            * tau(home_goals, away_goals, home_rate, away_rate, rho)
            for away_goals in range(max_goals + 1)
        ]
        for home_goals in range(max_goals + 1)
    ]

    total = sum(sum(row) for row in raw)
    if total <= 0:
        uniform = 1.0 / ((max_goals + 1) ** 2)
        grid = tuple(tuple(uniform for _ in range(max_goals + 1)) for _ in range(max_goals + 1))
    else:
        grid = tuple(tuple(cell / total for cell in row) for row in raw)

    return ScoreMatrix(grid=grid, home_rate=home_rate, away_rate=away_rate, rho=rho)
