"""
Corner and card markets.

Same shape as the goal model — an expected count, then a Poisson tail for the line —
but these cannot come from the score matrix: corners and cards are separate generating
processes with no relationship to the scoreline grid.

The rates come from `soccer/teams/{id}`, which reports `avg_corners`, `avg_yellowcards`,
`avg_redcards` and `fouls` split home and away. The previous evaluators read these from
the match-stats endpoint, which does not carry them at all, so they never fired and
published a constant instead.

Bookings follow the bookmaker's own scale: a yellow is one booking, a red is two.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Fallbacks when a team has no profile. Broad league averages, used only to blend a
# thin sample toward something sane -- never as a substitute for having no data at all.
LEAGUE_CORNERS_PER_TEAM = 5.1
LEAGUE_CARDS_PER_TEAM = 1.9

# Pseudo-matches of prior weight, mirroring the goal model's shrinkage.
SHRINKAGE_MATCHES = 5

RED_CARD_BOOKINGS = 2


@dataclass(frozen=True)
class CountForecast:
    expected: float
    sources: tuple[str, ...]
    matches: int

    @property
    def thin(self) -> bool:
        return self.matches < SHRINKAGE_MATCHES


def _shrink(observed: float | None, matches: float, prior: float) -> float:
    if observed is None:
        return prior
    matches = max(0.0, matches)
    return (matches * observed + SHRINKAGE_MATCHES * prior) / (matches + SHRINKAGE_MATCHES)


def expected_corners(home_profile, away_profile) -> CountForecast:
    """Total match corners: the home side's home rate plus the away side's away rate."""
    sources = []
    home_rate = away_rate = None
    home_matches = away_matches = 0
    if home_profile is not None:
        home_rate = home_profile.corners_home
        home_matches = home_profile.matches
        if home_rate is not None:
            sources.append("home_team_profile")
    if away_profile is not None:
        away_rate = away_profile.corners_away
        away_matches = away_profile.matches
        if away_rate is not None:
            sources.append("away_team_profile")

    expected = _shrink(home_rate, home_matches, LEAGUE_CORNERS_PER_TEAM) + _shrink(
        away_rate, away_matches, LEAGUE_CORNERS_PER_TEAM
    )
    return CountForecast(
        expected=round(max(0.5, expected), 3),
        sources=tuple(sources),
        matches=int(min(home_matches or 0, away_matches or 0)),
    )


def expected_team_corners(profile, *, side: str) -> CountForecast:
    if profile is None:
        return CountForecast(expected=LEAGUE_CORNERS_PER_TEAM, sources=(), matches=0)
    rate = profile.corners_home if side == "home" else profile.corners_away
    return CountForecast(
        expected=round(max(0.2, _shrink(rate, profile.matches, LEAGUE_CORNERS_PER_TEAM)), 3),
        sources=("team_profile",) if rate is not None else (),
        matches=profile.matches,
    )


def expected_cards(home_profile, away_profile) -> CountForecast:
    """Total match bookings across both sides."""
    sources = []
    home_rate = away_rate = None
    home_matches = away_matches = 0
    if home_profile is not None:
        home_rate = home_profile.cards_home
        home_matches = home_profile.matches
        if home_rate is not None:
            sources.append("home_team_profile")
    if away_profile is not None:
        away_rate = away_profile.cards_away
        away_matches = away_profile.matches
        if away_rate is not None:
            sources.append("away_team_profile")

    expected = _shrink(home_rate, home_matches, LEAGUE_CARDS_PER_TEAM) + _shrink(
        away_rate, away_matches, LEAGUE_CARDS_PER_TEAM
    )
    return CountForecast(
        expected=round(max(0.5, expected), 3),
        sources=tuple(sources),
        matches=int(min(home_matches or 0, away_matches or 0)),
    )


def expected_team_cards(profile, *, side: str) -> CountForecast:
    if profile is None:
        return CountForecast(expected=LEAGUE_CARDS_PER_TEAM, sources=(), matches=0)
    rate = profile.cards_home if side == "home" else profile.cards_away
    return CountForecast(
        expected=round(max(0.1, _shrink(rate, profile.matches, LEAGUE_CARDS_PER_TEAM)), 3),
        sources=("team_profile",) if rate is not None else (),
        matches=profile.matches,
    )


def poisson_over_under(expected: float, line: float, side: str = "over") -> tuple[float, float]:
    """
    Probability the count beats the line, plus the push mass on a whole line.

    Returns `(win, push)` so a whole-line market is never settled as a loss when it
    lands exactly on the number.
    """
    expected = max(1e-6, float(expected))
    line = float(line)
    ceiling = max(30, int(expected * 4) + 12)

    def pmf(count: int) -> float:
        return math.exp(-expected) * expected**count / math.factorial(count)

    at_or_below = sum(pmf(count) for count in range(0, int(math.floor(line)) + 1))
    push = pmf(int(line)) if float(line).is_integer() else 0.0

    total = sum(pmf(count) for count in range(0, ceiling + 1))
    at_or_below = min(at_or_below / total, 1.0) if total else 0.0
    push = min(push / total, 1.0) if total else 0.0

    if side == "under":
        win = max(0.0, at_or_below - push)
    else:
        win = max(0.0, 1.0 - at_or_below)
    return win, push
