"""
Bridge from a resolved `CanonicalMarket` to the existing `MarketDescriptor`.

The importer resolves market identity once, from the bookmaker's ids. Everything
downstream still speaks `MarketDescriptor`, so rather than rewrite those consumers in
one step we translate — and the translation is where the old text re-parse used to
happen. Nothing here inspects display text.

Two rules matter:

* A canonical family with no capability rule maps to an **unsupported** descriptor.
  Reporting "we do not model this" is correct; inventing a nearby family is what
  produced confident answers to the wrong question.
* Half-period markets must never emit a full-match canonical string. `1st Half Over 0.5`
  and `Over 0.5` are different bets, and a shared string is exactly how the period
  used to get lost.
"""

from __future__ import annotations

from ..market_taxonomy import CORE_MARKETS, MarketDescriptor
from .canonical import CanonicalMarket, Period, Resolution, Subject


# Canonical family -> the family name the capability layer knows. Families absent from
# this map are recognised but not modelled, and are reported as such.
FAMILY_TO_TAXONOMY = {
    "match_result": "match_result",
    "double_chance": "double_chance",
    "draw_no_bet": "draw_no_bet",
    "handicap": "handicap",
    "asian_handicap": "asian_handicap",
    "total_goals": "total_goals",
    "team_total_goals": "team_total_goals",
    "btts": "btts",
    "corners_total": "corners_total",
    "team_corners": "team_corners",
    "cards_total": "cards_total",
    "team_cards": "team_cards",
    "booking_points": "booking_points",
    "player_card": "player_card",
    "goalscorer_anytime": "player_goal",
    "goalscorer_first": "player_goal",
    "goalscorer_last": "player_goal",
    "nth_goal": "first_to_score",
    "team_goals_odd_even": "odd_even",
}

PLAYER_FAMILIES = {"player_card", "goalscorer_anytime", "goalscorer_first", "goalscorer_last"}
CARD_FAMILIES = {"cards_total", "team_cards", "booking_points", "exact_cards", "cards_result", "player_card"}
CORNER_FAMILIES = {"corners_total", "team_corners"}
TEAM_GOAL_FAMILIES = {"team_total_goals", "team_goals_odd_even"}

PERIOD_TO_TAXONOMY = {
    Period.FULL_MATCH: "match",
    Period.FIRST_HALF: "1st_half",
    Period.SECOND_HALF: "2nd_half",
}

PERIOD_PREFIX = {
    Period.FULL_MATCH: "",
    Period.FIRST_HALF: "1H ",
    Period.SECOND_HALF: "2H ",
}

_RESULT_TEXT = {"home": "Home Win", "draw": "Draw", "away": "Away Win"}
_DC_TEXT = {"home_or_draw": "DC: 1X", "home_or_away": "DC: 12", "draw_or_away": "DC: X2"}
_DNB_TEXT = {"home": "DNB Home", "away": "DNB Away"}
_SIDE_TITLE = {"over": "Over", "under": "Under", "yes": "Yes", "no": "No"}


def _line_text(line: float | None) -> str:
    if line is None:
        return ""
    return str(int(line)) if float(line).is_integer() else str(line)


def canonical_text(market: CanonicalMarket) -> str:
    """
    Stable display/settlement string for a resolved market.

    Full-match core markets reproduce the strings the settlement engine already
    understands; everything else gets a distinct, period-qualified string so it can
    never be mistaken for a market we can actually settle.
    """
    prefix = PERIOD_PREFIX[market.period]
    family, side, line = market.family, market.side, market.line

    if family == "match_result":
        return f"{prefix}{_RESULT_TEXT.get(side, side.title())}".strip()
    if family == "double_chance":
        return f"{prefix}{_DC_TEXT.get(side, side)}".strip()
    if family == "draw_no_bet":
        return f"{prefix}{_DNB_TEXT.get(side, side)}".strip()
    if family == "btts":
        return f"{prefix}GG / BTTS Yes" if side == "yes" else f"{prefix}BTTS No".strip()
    if family == "total_goals":
        return f"{prefix}{_SIDE_TITLE.get(side, side.title())} {_line_text(line)}".strip()

    subject_word = {Subject.HOME: "Home", Subject.AWAY: "Away", Subject.EITHER: "Any"}.get(
        market.subject, ""
    )
    label = {
        "team_total_goals": "Team Goals",
        "corners_total": "Corners",
        "team_corners": "Team Corners",
        "cards_total": "Cards",
        "team_cards": "Team Cards",
        "booking_points": "Booking Points",
        "team_shots_on_target": "Team Shots On Target",
        "asian_handicap": "AH",
        "handicap": "EH",
        "nth_goal": "Nth Goal",
        "exact_cards": "Exact Cards",
        "cards_result": "Cards Result",
        "highest_scoring_half": "Highest Scoring Half",
        "team_goals_odd_even": "Team Goals Odd/Even",
    }.get(family, family.replace("_", " ").title())

    parts = [prefix.strip(), subject_word, label, _SIDE_TITLE.get(side, side.replace("_", " ").title()), _line_text(line)]
    return " ".join(part for part in parts if part).strip()


def descriptor_from_canonical(market: CanonicalMarket, *, raw: str = "") -> MarketDescriptor:
    """Translate a resolved market into the descriptor the rest of the app consumes."""
    if market.resolution != Resolution.MAPPED:
        return MarketDescriptor(
            raw=raw or market.label,
            canonical="",
            code="",
            family="unknown",
            category="",
            recognized=False,
            core_supported=False,
        )

    taxonomy_family = FAMILY_TO_TAXONOMY.get(market.family, "")
    canonical = canonical_text(market)
    team = ""
    if market.subject == Subject.HOME:
        team = "home"
    elif market.subject == Subject.AWAY:
        team = "away"

    return MarketDescriptor(
        raw=raw or market.label,
        canonical=canonical,
        code=f"{market.family}:{market.side}" if market.side else market.family,
        # An unmodelled family is reported honestly rather than mapped to a near neighbour.
        family=taxonomy_family or market.family,
        category=market.family,
        selection=market.side,
        side=market.side,
        line=_line_text(market.line),
        team=team,
        player=market.subject_player_id,
        subject=str(market.subject),
        period=PERIOD_TO_TAXONOMY[market.period],
        recognized=True,
        core_supported=canonical in CORE_MARKETS,
        requires_player_stats=market.family in PLAYER_FAMILIES,
        requires_card_stats=market.family in CARD_FAMILIES,
        requires_corner_stats=market.family in CORNER_FAMILIES,
        requires_team_goal_stats=market.family in TEAM_GOAL_FAMILIES,
    )
