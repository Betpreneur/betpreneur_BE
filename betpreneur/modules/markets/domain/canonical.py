"""
Canonical selection vocabulary.

A bet is identified by the bookmaker's structured market identity, never by its
display text. `Over 2.5` alone is genuinely ambiguous -- on SportyBet it has been
observed meaning match goals, home-team goals, bookings, corners and shots on target,
distinguished only by ``marketId``. Resolving from text is therefore not a bug to fix
but an approach to abandon; text parsing survives only as a flagged last resort.

Everything downstream (evaluator dispatch, data planning, settlement) keys off
``CanonicalMarket``, which is resolved exactly once at import time and then carried,
never re-derived.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Period(StrEnum):
    FULL_MATCH = "full_match"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"


class Subject(StrEnum):
    """Whose statistic the market is about."""
    MATCH = "match"
    HOME = "home"
    AWAY = "away"
    EITHER = "either"
    PLAYER = "player"


class Settlement(StrEnum):
    """How the line settles -- drives push/void handling at settlement time."""
    WIN_LOSE = "win_lose"          # half line: 0.5, 1.5, 2.5
    WIN_LOSE_VOID = "win_lose_void"  # whole line: 1, 2, 3 -- can push
    ASIAN_QUARTER = "asian_quarter"  # 0.25, 0.75 -- half stake returned
    THREE_WAY = "three_way"
    EARLY_PAYOUT = "early_payout"    # settles early on a trigger; not a plain O/U


class Resolution(StrEnum):
    """How confident we are in this identification."""
    MAPPED = "mapped"        # bookmaker market id found in the mapping table
    TEXT_FALLBACK = "text"   # guessed from display text -- treat with suspicion
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class CanonicalMarket:
    family: str
    period: Period = Period.FULL_MATCH
    subject: Subject = Subject.MATCH
    side: str = ""                 # over/under/home/draw/away/yes/no/...
    line: float | None = None      # signed: Asian -1.5 stays negative
    settlement: Settlement = Settlement.WIN_LOSE
    resolution: Resolution = Resolution.MAPPED
    subject_player_id: str = ""    # Sportradar player id, when the market names a player
    goal_number: int | None = None  # goalnr= specifier
    label: str = ""                # bookmaker's own description, for display only
    warnings: list[str] = field(default_factory=list)

    @property
    def assessable(self) -> bool:
        """A text-fallback identification is not trustworthy enough to assess."""
        return self.resolution == Resolution.MAPPED

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["period"] = str(self.period)
        payload["subject"] = str(self.subject)
        payload["settlement"] = str(self.settlement)
        payload["resolution"] = str(self.resolution)
        return payload


def settlement_for_line(line: float | None) -> Settlement:
    """Half lines win/lose; whole lines can push; quarter lines split the stake."""
    if line is None:
        return Settlement.WIN_LOSE
    fraction = abs(line) % 1
    if fraction in (0.25, 0.75):
        return Settlement.ASIAN_QUARTER
    if fraction == 0.5:
        return Settlement.WIN_LOSE
    return Settlement.WIN_LOSE_VOID
