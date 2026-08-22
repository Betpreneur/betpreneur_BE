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
from functools import lru_cache

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


@lru_cache(maxsize=None)
def _lead_hit_probability(home_goals: int, away_goals: int, *, team: str, lead: int) -> float:
    """
    P(team reaches `lead` at some point), conditional on the final score.

    With only a pre-match score matrix we do not know the actual goal timeline. The
    least-inventive approximation is to treat all orderings of the final home/away
    goals as equally likely, then count the sequences where the selected side ever
    opens the requested lead.
    """
    if lead <= 0:
        return 1.0
    if team not in {"home", "away"}:
        return 0.0
    if home_goals < 0 or away_goals < 0:
        return 0.0

    @lru_cache(maxsize=None)
    def walk(home_remaining: int, away_remaining: int, current_diff: int) -> tuple[int, int]:
        if (team == "home" and current_diff >= lead) or (team == "away" and -current_diff >= lead):
            return 1, 1
        if home_remaining == 0 and away_remaining == 0:
            return 0, 1

        hit = total = 0
        if home_remaining:
            child_hit, child_total = walk(home_remaining - 1, away_remaining, current_diff + 1)
            hit += child_hit
            total += child_total
        if away_remaining:
            child_hit, child_total = walk(home_remaining, away_remaining - 1, current_diff - 1)
            hit += child_hit
            total += child_total
        return hit, total

    hit, total = walk(int(home_goals), int(away_goals), 0)
    return round(hit / total, 6) if total else 0.0


def result_early_payout(matrix: ScoreMatrix, side: str, lead: int = 1) -> float:
    """Result market with 1UP/2UP early-payout protection."""
    side = str(side or "").lower()
    if side == "draw":
        return draw(matrix)
    if side not in {"home", "away"}:
        return 0.0

    total = 0.0
    for home_goals, row in enumerate(matrix.grid):
        for away_goals, mass in enumerate(row):
            final_wins = home_goals > away_goals if side == "home" else away_goals > home_goals
            if final_wins:
                total += mass
            else:
                total += mass * _lead_hit_probability(home_goals, away_goals, team=side, lead=int(lead or 1))
    return min(1.0, max(0.0, total))


def double_chance_early_payout(matrix: ScoreMatrix, side: str, lead: int = 1) -> float:
    """Double-chance market with SportyBet-style 1UP early-payout protection."""
    side = str(side or "").lower()

    def final_matches(home: int, away: int) -> bool:
        if side in {"home_or_draw", "1x"}:
            return home >= away
        if side in {"draw_or_away", "x2"}:
            return home <= away
        if side in {"home_or_away", "12"}:
            return home != away
        return False

    protected_teams = {
        "home_or_draw": ("home",),
        "1x": ("home",),
        "draw_or_away": ("away",),
        "x2": ("away",),
        "home_or_away": ("home", "away"),
        "12": ("home", "away"),
    }.get(side, ())

    total = 0.0
    for home_goals, row in enumerate(matrix.grid):
        for away_goals, mass in enumerate(row):
            if final_matches(home_goals, away_goals):
                total += mass
                continue
            no_hit_probability = 1.0
            for team in protected_teams:
                no_hit_probability *= 1.0 - _lead_hit_probability(home_goals, away_goals, team=team, lead=int(lead or 1))
            total += mass * (1.0 - no_hit_probability)
    return min(1.0, max(0.0, total))


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


def result_total_goals(matrix: ScoreMatrix, line: float, side: str) -> MarketOutcome:
    """Combined 1X2 and total-goals market, e.g. Home & Over 2.5."""
    result_side, _, total_side = str(side or "").partition("_")
    line = float(line)

    def result_matches(home: int, away: int) -> bool:
        if result_side == "home":
            return home > away
        if result_side == "draw":
            return home == away
        if result_side == "away":
            return home < away
        return False

    def total_matches(home: int, away: int) -> bool:
        total = home + away
        if total_side == "over":
            return total > line
        if total_side == "under":
            return total < line
        return False

    push = matrix.sum_where(lambda home, away: home + away == line) if _is_whole(line) else 0.0
    win = matrix.sum_where(lambda home, away: result_matches(home, away) and total_matches(home, away))
    return MarketOutcome(win=win, push=push)


def result_btts(matrix: ScoreMatrix, side: str) -> float:
    """Combined 1X2 and BTTS market, e.g. Home & Yes."""
    result_side, _, btts_side = str(side or "").partition("_")

    def result_matches(home: int, away: int) -> bool:
        if result_side == "home":
            return home > away
        if result_side == "draw":
            return home == away
        if result_side == "away":
            return home < away
        return False

    def btts_matches(home: int, away: int) -> bool:
        both_score = home > 0 and away > 0
        return both_score if btts_side == "yes" else not both_score if btts_side == "no" else False

    return matrix.sum_where(lambda home, away: result_matches(home, away) and btts_matches(home, away))


def total_btts(matrix: ScoreMatrix, line: float, side: str) -> MarketOutcome:
    """Combined total-goals and BTTS market, e.g. Over 2.5 & Yes."""
    total_side, _, btts_side = str(side or "").partition("_")
    line = float(line)

    def total_matches(home: int, away: int) -> bool:
        total = home + away
        if total_side == "over":
            return total > line
        if total_side == "under":
            return total < line
        return False

    def btts_matches(home: int, away: int) -> bool:
        both_score = home > 0 and away > 0
        return both_score if btts_side == "yes" else not both_score if btts_side == "no" else False

    push = matrix.sum_where(lambda home, away: home + away == line) if _is_whole(line) else 0.0
    win = matrix.sum_where(lambda home, away: total_matches(home, away) and btts_matches(home, away))
    return MarketOutcome(win=win, push=push)


def double_chance_btts(matrix: ScoreMatrix, side: str) -> float:
    """Combined double-chance and BTTS market, e.g. Home/Draw & Yes."""
    dc_side, _, btts_side = str(side or "").rpartition("_")

    def dc_matches(home: int, away: int) -> bool:
        if dc_side == "home_or_draw":
            return home >= away
        if dc_side == "home_or_away":
            return home != away
        if dc_side == "draw_or_away":
            return home <= away
        return False

    def btts_matches(home: int, away: int) -> bool:
        both_score = home > 0 and away > 0
        return both_score if btts_side == "yes" else not both_score if btts_side == "no" else False

    return matrix.sum_where(lambda home, away: dc_matches(home, away) and btts_matches(home, away))


def double_chance_total_goals(matrix: ScoreMatrix, line: float, side: str) -> MarketOutcome:
    """Combined double-chance and total-goals market, e.g. Home/Draw & Over 2.5."""
    dc_side, _, total_side = str(side or "").rpartition("_")
    line = float(line)

    def dc_matches(home: int, away: int) -> bool:
        if dc_side == "home_or_draw":
            return home >= away
        if dc_side == "home_or_away":
            return home != away
        if dc_side == "draw_or_away":
            return home <= away
        return False

    def total_matches(home: int, away: int) -> bool:
        total = home + away
        if total_side == "over":
            return total > line
        if total_side == "under":
            return total < line
        return False

    push = matrix.sum_where(lambda home, away: home + away == line) if _is_whole(line) else 0.0
    win = matrix.sum_where(lambda home, away: dc_matches(home, away) and total_matches(home, away))
    return MarketOutcome(win=win, push=push)


def _result_matches(result_side: str, home: int, away: int) -> bool:
    if result_side == "home":
        return home > away
    if result_side == "draw":
        return home == away
    if result_side == "away":
        return home < away
    return False


def result_or_total_goals(matrix: ScoreMatrix, line: float, side: str) -> MarketOutcome:
    """Either selected 1X2 result happens or the selected total-goals side happens."""
    combo_side, _, answer = str(side or "").rpartition("_")
    result_side, _, total_side = combo_side.partition("_")
    line = float(line)

    def total_matches(home: int, away: int) -> bool:
        total = home + away
        if total_side == "over":
            return total > line
        if total_side == "under":
            return total < line
        return False

    def union_matches(home: int, away: int) -> bool:
        return _result_matches(result_side, home, away) or total_matches(home, away)

    push = matrix.sum_where(lambda home, away: home + away == line) if _is_whole(line) else 0.0
    if answer == "yes":
        win = matrix.sum_where(lambda home, away: union_matches(home, away))
    else:
        win = matrix.sum_where(lambda home, away: not union_matches(home, away) and home + away != line)
    return MarketOutcome(win=win, push=push)


def result_or_btts(matrix: ScoreMatrix, side: str) -> float:
    """Either selected 1X2 result happens or both teams score."""
    combo_side, _, answer = str(side or "").rpartition("_")
    result_side, _, _btts = combo_side.partition("_")

    def union_matches(home: int, away: int) -> bool:
        return _result_matches(result_side, home, away) or (home > 0 and away > 0)

    if answer == "yes":
        return matrix.sum_where(union_matches)
    return matrix.sum_where(lambda home, away: not union_matches(home, away))


def result_or_clean_sheet(matrix: ScoreMatrix, side: str) -> float:
    """Either selected 1X2 result happens or at least one team keeps a clean sheet."""
    combo_side, _, answer = str(side or "").rpartition("_")
    result_side, _, _clean_sheet = combo_side.partition("_")

    def union_matches(home: int, away: int) -> bool:
        any_clean_sheet = home == 0 or away == 0
        return _result_matches(result_side, home, away) or any_clean_sheet

    if answer == "yes":
        return matrix.sum_where(union_matches)
    return matrix.sum_where(lambda home, away: not union_matches(home, away))


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
    "result_total_goals",
    "result_btts",
    "total_btts",
    "double_chance_btts",
    "double_chance_total_goals",
    "result_or_total_goals",
    "result_or_btts",
    "result_or_clean_sheet",
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
