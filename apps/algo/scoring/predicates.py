"""
Scoreline predicates, so two legs on one fixture can be evaluated jointly.

`P(ticket)` assumes independence, which is wrong whenever a slip carries two markets on
the same match — and SportyBet users do that constantly. `Home Win` and `Over 2.5` are
positively dependent: the scorelines that win one overlap heavily with those that win
the other, so multiplying their marginals understates the ticket.

Expressing each leg as a predicate over the shared score matrix makes the joint exact:
sum the cells where *both* legs win. Only markets with a clean win/lose boundary are
representable here; anything that can push is left to the independence fallback, since
a pushed stake is not an outcome the grid can express as a single condition.
"""

from __future__ import annotations

from typing import Callable


def _line(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_half_line(line: float | None) -> bool:
    return line is not None and abs(line) % 1 == 0.5


def predicate_for(family: str, side: str, *, line=None, team: str = "") -> Callable | None:
    """
    A `(home_goals, away_goals) -> bool` test for this leg, or None if not representable.

    Returning None is the honest answer for push-capable and unmodelled markets: the
    caller then falls back to independence and says so.
    """
    side = (side or "").lower()
    line = _line(line)
    team = team or ("home" if side == "home" else "away" if side == "away" else "")

    if family == "match_result":
        if side == "home":
            return lambda home, away: home > away
        if side == "draw":
            return lambda home, away: home == away
        if side == "away":
            return lambda home, away: home < away
        return None

    if family == "double_chance":
        if side in {"home_or_draw", "1X"}:
            return lambda home, away: home >= away
        if side in {"draw_or_away", "X2"}:
            return lambda home, away: home <= away
        if side in {"home_or_away", "12"}:
            return lambda home, away: home != away
        return None

    if family == "btts":
        if side == "no":
            return lambda home, away: home == 0 or away == 0
        return lambda home, away: home > 0 and away > 0

    if family == "clean_sheet":
        if team == "home":
            return lambda home, away: away == 0
        return lambda home, away: home == 0

    if family == "odd_even":
        remainder = 1 if side == "odd" else 0
        return lambda home, away: (home + away) % 2 == remainder

    # Lines below here can push on whole numbers, so only half lines are representable.
    if family == "total_goals" and _is_half_line(line):
        if side == "under":
            return lambda home, away: home + away < line
        return lambda home, away: home + away > line

    if family == "team_total_goals" and _is_half_line(line):
        if team == "home":
            getter = lambda home, away: home  # noqa: E731
        else:
            getter = lambda home, away: away  # noqa: E731
        if side == "under":
            return lambda home, away: getter(home, away) < line
        return lambda home, away: getter(home, away) > line

    if family == "asian_handicap" and _is_half_line(line):
        if team == "away":
            return lambda home, away: (away - home) - line > 0
        return lambda home, away: (home - away) + line > 0

    return None
