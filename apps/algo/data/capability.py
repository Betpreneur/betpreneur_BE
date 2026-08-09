"""
One canonical vocabulary for "what analytical data do we have".

Before this, two incompatible vocabularies were compared directly: descriptor
requirements (`team_stats`, `league_stats`, `h2h`, `odds`) against StatPal snapshot
names (`detailed_stats`, `prematch_odds`, `lineups`, …). Those strings can never match,
so the fallback path computed 0% coverage and reported every market as poorly covered.

Capabilities describe *analytical* facts, not provider endpoints. Providers declare what
they supply; evaluators declare what they need. Coverage is then a set operation, and
adding a second provider is a mapping change rather than a rewrite.
"""

from __future__ import annotations

from enum import StrEnum


class DataCapability(StrEnum):
    TEAM_GOALS_FOR = "team_goals_for"
    TEAM_GOALS_AGAINST = "team_goals_against"
    TEAM_SHOTS = "team_shots"
    TEAM_POSSESSION = "team_possession"
    TEAM_CORNERS = "team_corners"
    TEAM_CARDS = "team_cards"
    TEAM_FOULS = "team_fouls"
    TEAM_CLEAN_SHEET = "team_clean_sheet"
    GOAL_MINUTE_DIST = "goal_minute_dist"
    CARD_MINUTE_DIST = "card_minute_dist"
    PLAYER_SEASON_STATS = "player_season_stats"
    LINEUP_PROJECTED = "lineup_projected"
    LINEUP_CONFIRMED = "lineup_confirmed"
    INJURIES = "injuries"
    REFEREE = "referee"
    H2H = "h2h"
    MARKET_ODDS = "market_odds"


# What each StatPal snapshot yields. The league match-stats endpoint now fills the
# detailed-stats snapshot with fixture xG, corners, fouls, cards/events, referee and
# lineup/player-stat context when available.
STATPAL_SNAPSHOT_CAPABILITIES: dict[str, tuple[DataCapability, ...]] = {
    "team_stats": (
        DataCapability.TEAM_GOALS_FOR,
        DataCapability.TEAM_GOALS_AGAINST,
        DataCapability.TEAM_SHOTS,
        DataCapability.TEAM_POSSESSION,
        DataCapability.TEAM_CORNERS,
        DataCapability.TEAM_CARDS,
        DataCapability.TEAM_FOULS,
        DataCapability.TEAM_CLEAN_SHEET,
        DataCapability.GOAL_MINUTE_DIST,
        DataCapability.CARD_MINUTE_DIST,
    ),
    "detailed_stats": (
        DataCapability.REFEREE,
        DataCapability.CARD_MINUTE_DIST,
        DataCapability.GOAL_MINUTE_DIST,
    ),
    "predictions": (),
    "prematch_odds": (DataCapability.MARKET_ODDS,),
    "lineups": (DataCapability.LINEUP_PROJECTED, DataCapability.LINEUP_CONFIRMED),
    "injuries_suspensions": (DataCapability.INJURIES,),
}


def capabilities_from_snapshots(snapshot_types) -> set[DataCapability]:
    """Translate the snapshot types we hold into the capabilities they provide."""
    available: set[DataCapability] = set()
    for snapshot_type in snapshot_types or ():
        available.update(STATPAL_SNAPSHOT_CAPABILITIES.get(str(snapshot_type), ()))
    return available


def snapshots_for_capabilities(capabilities) -> list[str]:
    """The minimal set of snapshot types that together provide these capabilities."""
    wanted = {DataCapability(item) for item in capabilities or ()}
    needed: list[str] = []
    for snapshot_type, provided in STATPAL_SNAPSHOT_CAPABILITIES.items():
        if wanted & set(provided):
            needed.append(snapshot_type)
    return needed


def coverage(required, available) -> float:
    """Percentage of required capabilities we actually hold."""
    required = {DataCapability(item) for item in required or ()}
    if not required:
        return 100.0
    available = {DataCapability(item) for item in available or ()}
    return round(len(required & available) / len(required) * 100, 1)


def missing(required, available) -> list[DataCapability]:
    required = {DataCapability(item) for item in required or ()}
    available = {DataCapability(item) for item in available or ()}
    return sorted(required - available, key=str)
