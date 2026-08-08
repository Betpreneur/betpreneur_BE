"""
Markets derived from the shared score matrix.

Every function here is a sum over the same distribution, so the results are
arithmetically consistent: `P(1X)` is exactly `P(home) + P(draw)`, and
`P(over 2.5) + P(under 2.5)` is exactly 1. That consistency is the point of the shared
model — it cannot be achieved with independent per-market heuristics.

Lines settle three ways, matching the bookmaker's own rules:

* half line (2.5) — win or lose
* whole line (2)  — win, lose, or **push**; the stake is returned on an exact hit
* Asian quarter   — half the stake settles on each adjacent half/whole line

`MarketOutcome` therefore carries win/push/lose rather than a single probability, so a
push is never quietly counted as a loss.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dixon_coles import ScoreMatrix


@dataclass(frozen=True)
class MarketOutcome:
    win: float
    push: float = 0.0

    @property
    def lose(self) -> float:
        return max(0.0, 1.0 - self.win - self.push)

    @property
    def probability(self) -> float:
        """Win probability with pushed stake removed, i.e. the effective chance."""
        live = 1.0 - self.push
        return round(self.win / live, 6) if live > 1e-9 else 0.0

    def to_dict(self):
        return {
            "win": round(self.win, 6),
            "push": round(self.push, 6),
            "lose": round(self.lose, 6),
            "probability": self.probability,
        }


def _is_whole(line: float) -> bool:
    return float(line) % 1 == 0


def _is_quarter(line: float) -> bool:
    return abs(float(line)) % 1 in (0.25, 0.75)


# --- match result -----------------------------------------------------------------
def home_win(matrix: ScoreMatrix) -> float:
    return matrix.sum_where(lambda home, away: home > away)


def draw(matrix: ScoreMatrix) -> float:
    return matrix.sum_where(lambda home, away: home == away)


def away_win(matrix: ScoreMatrix) -> float:
    return matrix.sum_where(lambda home, away: home < away)


def double_chance(matrix: ScoreMatrix, side: str) -> float:
    if side in {"home_or_draw", "1X"}:
        return matrix.sum_where(lambda home, away: home >= away)
    if side in {"draw_or_away", "X2"}:
        return matrix.sum_where(lambda home, away: home <= away)
    if side in {"home_or_away", "12"}:
        return matrix.sum_where(lambda home, away: home != away)
    return 0.0


def draw_no_bet(matrix: ScoreMatrix, side: str) -> MarketOutcome:
    """A draw returns the stake, so it is a push rather than a loss."""
    push = draw(matrix)
    win = home_win(matrix) if side == "home" else away_win(matrix)
    return MarketOutcome(win=win, push=push)


# --- totals -----------------------------------------------------------------------
def total_goals(matrix: ScoreMatrix, line: float, side: str = "over") -> MarketOutcome:
    line = float(line)
    if _is_quarter(line):
        lower, upper = line - 0.25, line + 0.25
        first = total_goals(matrix, lower, side)
        second = total_goals(matrix, upper, side)
        return MarketOutcome(
            win=(first.win + second.win) / 2,
            push=(first.push + second.push) / 2,
        )

    push = matrix.sum_where(lambda home, away: home + away == line) if _is_whole(line) else 0.0
    if side == "over":
        win = matrix.sum_where(lambda home, away: home + away > line)
    else:
        win = matrix.sum_where(lambda home, away: home + away < line)
    return MarketOutcome(win=win, push=push)


def team_total_goals(matrix: ScoreMatrix, line: float, *, team: str, side: str = "over") -> MarketOutcome:
    line = float(line)
    distribution = matrix.home_goal_distribution() if team == "home" else matrix.away_goal_distribution()
    push = sum(mass for goals, mass in enumerate(distribution) if goals == line) if _is_whole(line) else 0.0
    if side == "over":
        win = sum(mass for goals, mass in enumerate(distribution) if goals > line)
    else:
        win = sum(mass for goals, mass in enumerate(distribution) if goals < line)
    return MarketOutcome(win=win, push=push)


# --- goals shape ------------------------------------------------------------------
def btts(matrix: ScoreMatrix, yes: bool = True) -> float:
    if yes:
        return matrix.sum_where(lambda home, away: home > 0 and away > 0)
    return matrix.sum_where(lambda home, away: home == 0 or away == 0)


def clean_sheet(matrix: ScoreMatrix, team: str) -> float:
    if team == "home":
        return matrix.sum_where(lambda home, away: away == 0)
    return matrix.sum_where(lambda home, away: home == 0)


def odd_even(matrix: ScoreMatrix, want: str = "odd") -> float:
    remainder = 1 if want == "odd" else 0
    return matrix.sum_where(lambda home, away: (home + away) % 2 == remainder)


def exact_goals(matrix: ScoreMatrix, goals: int) -> float:
    return matrix.sum_where(lambda home, away: home + away == goals)


def correct_score(matrix: ScoreMatrix, home_goals: int, away_goals: int) -> float:
    return matrix.probability(home_goals, away_goals)


def winning_margin(matrix: ScoreMatrix, margin: int, *, team: str = "home") -> float:
    if team == "home":
        return matrix.sum_where(lambda home, away: home - away == margin)
    return matrix.sum_where(lambda home, away: away - home == margin)


# --- handicaps --------------------------------------------------------------------
def asian_handicap(matrix: ScoreMatrix, line: float, *, team: str = "home") -> MarketOutcome:
    """
    `line` is always from the home team's perspective, matching the normalizer.

    A home line of -1.5 means home must win by two or more; +1.5 means home may lose by
    one. Whole lines push on an exact hit.
    """
    line = float(line)
    if _is_quarter(line):
        lower, upper = line - 0.25, line + 0.25
        first = asian_handicap(matrix, lower, team=team)
        second = asian_handicap(matrix, upper, team=team)
        return MarketOutcome(
            win=(first.win + second.win) / 2,
            push=(first.push + second.push) / 2,
        )

    if team == "home":
        margin = lambda home, away: (home - away) + line  # noqa: E731
    else:
        margin = lambda home, away: (away - home) - line  # noqa: E731

    push = matrix.sum_where(lambda home, away: margin(home, away) == 0) if _is_whole(line) else 0.0
    win = matrix.sum_where(lambda home, away: margin(home, away) > 0)
    return MarketOutcome(win=win, push=push)


def european_handicap(matrix: ScoreMatrix, line: float, side: str) -> float:
    """Three-way handicap: the adjusted result can still be a draw."""
    line = float(line)
    if side == "home":
        return matrix.sum_where(lambda home, away: (home + line) > away)
    if side == "away":
        return matrix.sum_where(lambda home, away: (home + line) < away)
    return matrix.sum_where(lambda home, away: (home + line) == away)


DERIVED_FAMILIES = (
    "match_result",
    "double_chance",
    "draw_no_bet",
    "total_goals",
    "team_total_goals",
    "btts",
    "clean_sheet",
    "odd_even",
    "exact_goals",
    "correct_score",
    "winning_margin",
    "asian_handicap",
    "handicap",
)
