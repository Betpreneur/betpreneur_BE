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
LEAGUE_SHOTS_ON_TARGET_PER_TEAM = 4.2

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


def expected_shots_on_target(home_profile, away_profile) -> CountForecast:
    """Total match shots on target: home side's home rate plus away side's away rate."""
    sources = []
    home_rate = away_rate = None
    home_matches = away_matches = 0
    if home_profile is not None:
        home_rate = getattr(home_profile, "shots_on_target_home", None)
        home_matches = home_profile.matches
        if home_rate is not None:
            sources.append("home_team_profile")
    if away_profile is not None:
        away_rate = getattr(away_profile, "shots_on_target_away", None)
        away_matches = away_profile.matches
        if away_rate is not None:
            sources.append("away_team_profile")

    expected = _shrink(home_rate, home_matches, LEAGUE_SHOTS_ON_TARGET_PER_TEAM) + _shrink(
        away_rate, away_matches, LEAGUE_SHOTS_ON_TARGET_PER_TEAM
    )
    return CountForecast(
        expected=round(max(0.5, expected), 3),
        sources=tuple(sources),
        matches=int(min(home_matches or 0, away_matches or 0)),
    )


def expected_team_shots_on_target(profile, *, side: str) -> CountForecast:
    if profile is None:
        return CountForecast(expected=LEAGUE_SHOTS_ON_TARGET_PER_TEAM, sources=(), matches=0)
    rate = getattr(profile, "shots_on_target_home", None) if side == "home" else getattr(profile, "shots_on_target_away", None)
    return CountForecast(
        expected=round(max(0.2, _shrink(rate, profile.matches, LEAGUE_SHOTS_ON_TARGET_PER_TEAM)), 3),
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


def poisson_range(expected: float, bucket: str) -> tuple[float | None, tuple[int | None, int | None]]:
    """
    Probability that a count lands in an inclusive bucket.

    SportyBet range markets arrive as outcome labels such as ``0-8``, ``9-11`` or
    ``12+``. Unlike over/under lines there is no push mass: a settled count either
    belongs to the bucket or it does not.
    """
    expected = max(1e-6, float(expected))
    raw = str(bucket or "").strip().replace(" ", "")
    lower: int | None
    upper: int | None
    if raw.endswith("+"):
        try:
            lower = int(raw[:-1])
        except ValueError:
            return None, (None, None)
        upper = None
    elif "-" in raw:
        start, _, end = raw.partition("-")
        try:
            lower, upper = int(start), int(end)
        except ValueError:
            return None, (None, None)
    else:
        try:
            lower = upper = int(raw)
        except ValueError:
            return None, (None, None)

    if lower is None or lower < 0 or (upper is not None and upper < lower):
        return None, (None, None)

    ceiling = max(30, int(expected * 4) + 12, (upper if upper is not None else lower) + 20)

    def pmf(count: int) -> float:
        return math.exp(-expected) * expected**count / math.factorial(count)

    total = sum(pmf(count) for count in range(0, ceiling + 1))
    if not total:
        return 0.0, (lower, upper)

    if upper is None:
        mass = sum(pmf(count) for count in range(lower, ceiling + 1))
    else:
        mass = sum(pmf(count) for count in range(lower, upper + 1))
    return min(max(mass / total, 0.0), 1.0), (lower, upper)


def poisson_three_way(home_expected: float, away_expected: float) -> dict[str, float]:
    """Independent count 1X2: home count wins, equal count, or away count wins."""
    home_expected = max(1e-6, float(home_expected))
    away_expected = max(1e-6, float(away_expected))
    ceiling = max(30, int(max(home_expected, away_expected) * 4) + 12)

    def pmf(rate: float, count: int) -> float:
        return math.exp(-rate) * rate**count / math.factorial(count)

    home_pmf = [pmf(home_expected, count) for count in range(0, ceiling + 1)]
    away_pmf = [pmf(away_expected, count) for count in range(0, ceiling + 1)]
    total = sum(home_pmf) * sum(away_pmf)
    if not total:
        return {"home": 0.0, "draw": 0.0, "away": 0.0}

    home = draw = away = 0.0
    for home_count, home_prob in enumerate(home_pmf):
        for away_count, away_prob in enumerate(away_pmf):
            mass = home_prob * away_prob
            if home_count > away_count:
                home += mass
            elif home_count == away_count:
                draw += mass
            else:
                away += mass

    return {
        "home": min(max(home / total, 0.0), 1.0),
        "draw": min(max(draw / total, 0.0), 1.0),
        "away": min(max(away / total, 0.0), 1.0),
    }


def poisson_handicap(home_expected: float, away_expected: float, line: float) -> dict[str, float]:
    """
    Two-way count handicap from the home side's perspective.

    ``line=-1.5`` means home corners minus 1.5 versus away corners. The returned
    push mass is non-zero only for whole-number lines.
    """
    home_expected = max(1e-6, float(home_expected))
    away_expected = max(1e-6, float(away_expected))
    line = float(line)
    ceiling = max(30, int(max(home_expected, away_expected) * 4) + 12)

    def pmf(rate: float, count: int) -> float:
        return math.exp(-rate) * rate**count / math.factorial(count)

    home_pmf = [pmf(home_expected, count) for count in range(0, ceiling + 1)]
    away_pmf = [pmf(away_expected, count) for count in range(0, ceiling + 1)]
    total = sum(home_pmf) * sum(away_pmf)
    if not total:
        return {"home": 0.0, "away": 0.0, "push": 0.0}

    home = away = push = 0.0
    for home_count, home_prob in enumerate(home_pmf):
        for away_count, away_prob in enumerate(away_pmf):
            mass = home_prob * away_prob
            adjusted = home_count + line - away_count
            if abs(adjusted) < 1e-12:
                push += mass
            elif adjusted > 0:
                home += mass
            else:
                away += mass

    return {
        "home": min(max(home / total, 0.0), 1.0),
        "away": min(max(away / total, 0.0), 1.0),
        "push": min(max(push / total, 0.0), 1.0),
    }
