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

from ..taxonomy import CORE_MARKETS, MarketDescriptor
from .canonical import CanonicalMarket, Period, Resolution, Subject


# Canonical family -> the family name the capability layer knows. Families absent from
# this map are recognised but not modelled, and are reported as such.
FAMILY_TO_TAXONOMY = {
    "match_result": "match_result",
    # SportyBet 1UP/2UP are early-payout guards layered on top of normal result
    # markets. We keep their display labels distinct, but analyse them with the
    # underlying result/double-chance model instead of treating them as unmodelled.
    "match_result_1up": "match_result",
    "match_result_2up": "match_result",
    "double_chance": "double_chance",
    "double_chance_1up": "double_chance",
    "draw_no_bet": "draw_no_bet",
    "handicap": "handicap",
    "asian_handicap": "asian_handicap",
    "total_goals": "total_goals",
    "team_total_goals": "team_total_goals",
    "result_total_goals": "result_total_goals",
    "result_btts": "result_btts",
    "total_btts": "total_btts",
    "double_chance_btts": "double_chance_btts",
    "double_chance_total_goals": "double_chance_total_goals",
    "result_or_total_goals": "result_or_total_goals",
    "result_or_btts": "result_or_btts",
    "result_or_clean_sheet": "result_or_clean_sheet",
    "btts": "btts",
    "corners_total": "corners_total",
    "team_corners": "team_corners",
    "corners_result": "corners_result",
    "corner_handicap": "corner_handicap",
    "cards_total": "cards_total",
    "cards_result": "cards_result",
    "team_cards": "team_cards",
    "booking_points": "booking_points",
    "player_card": "player_card",
    "goalscorer_anytime": "player_goal",
    "goalscorer_first": "player_goal",
    "goalscorer_last": "player_goal",
    "player_shots": "player_shots",
    "player_shots_on_target": "player_shots_on_target",
    "both_halves_total_goals": "both_halves_total_goals",
    "team_shots_on_target": "team_shots_on_target",
    "shots_on_target_total": "shots_on_target_total",
    "nth_goal": "first_to_score",
    "team_goals_odd_even": "odd_even",
}

PLAYER_FAMILIES = {
    "player_card",
    "goalscorer_anytime",
    "goalscorer_first",
    "goalscorer_last",
    "player_shots",
    "player_shots_on_target",
}
CARD_FAMILIES = {"cards_total", "team_cards", "booking_points", "exact_cards", "cards_result", "player_card"}
CORNER_FAMILIES = {
    "corners_total", "team_corners", "corner_range", "team_corner_range",
    "corners_result", "corner_handicap",
}
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
_DC_COMBO_TEXT = {"home_or_draw": "Home/Draw", "home_or_away": "Home/Away", "draw_or_away": "Draw/Away"}
_DNB_TEXT = {"home": "DNB Home", "away": "DNB Away"}
_TEAMS_TO_SCORE_TEXT = {
    "none": "No Team To Score",
    "only_home": "Only Home Team To Score",
    "only_away": "Only Away Team To Score",
    "both": "Both Teams To Score",
}
_GOAL_SIDE_TEXT = {"home": "Home", "none": "No Goal", "away": "Away"}
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
    if family == "match_result_1up":
        return f"{prefix}{_RESULT_TEXT.get(side, side.title())} 1UP".strip()
    if family == "match_result_2up":
        return f"{prefix}{_RESULT_TEXT.get(side, side.title())} 2UP".strip()
    if family == "match_result_never_down":
        return f"{prefix}{_RESULT_TEXT.get(side, side.title())} Never Down".strip()
    if family == "double_chance":
        return f"{prefix}{_DC_TEXT.get(side, side)}".strip()
    if family == "double_chance_1up":
        return f"{prefix}{_DC_TEXT.get(side, side)} 1UP".strip()
    if family == "draw_no_bet":
        return f"{prefix}{_DNB_TEXT.get(side, side)}".strip()
    if family == "btts":
        return f"{prefix}GG / BTTS Yes" if side == "yes" else f"{prefix}BTTS No".strip()
    if family == "teams_to_score":
        return f"{prefix}{_TEAMS_TO_SCORE_TEXT.get(side, side.replace('_', ' ').title())}".strip()
    if family == "btts_n_plus":
        goals = market.goal_number or 2
        return f"{prefix}GG / BTTS {goals}+ Yes" if side == "yes" else f"{prefix}BTTS {goals}+ No".strip()
    if family == "team_scores_n_plus":
        goals = market.goal_number or 2
        subject_word = {
            Subject.HOME: "Home Team",
            Subject.AWAY: "Away Team",
            Subject.EITHER: "Any Team",
        }.get(market.subject, "Any Team")
        outcome = "Yes" if side == "yes" else "No" if side == "no" else side.title()
        return f"{prefix}{subject_word} To Score {goals}+ Goals in a Row - {outcome}".strip()
    if family == "both_halves_total_goals":
        direction, _, answer = side.partition("_")
        outcome = "Yes" if answer == "yes" else "No" if answer == "no" else answer.title()
        return f"{prefix}Both Halves {direction.title()} {_line_text(line)} - {outcome}".strip()
    if family == "half_btts_pair":
        first, _, second = side.partition("_")
        first_text = "Yes" if first == "yes" else "No" if first == "no" else first.title()
        second_text = "Yes" if second == "yes" else "No" if second == "no" else second.title()
        return f"{prefix}1H BTTS {first_text} / 2H BTTS {second_text}".strip()
    if family == "no_draw_btts":
        outcome = "Yes" if side == "yes" else "No" if side == "no" else side.title()
        return f"{prefix}No Draw BTTS - {outcome}".strip()
    if family == "team_scores_both_halves":
        subject_word = {Subject.HOME: "Home Team", Subject.AWAY: "Away Team"}.get(market.subject, "Team")
        outcome = "Yes" if side == "yes" else "No" if side == "no" else side.title()
        return f"{prefix}{subject_word} To Score In Both Halves - {outcome}".strip()
    if family == "team_clean_sheet":
        subject_word = {Subject.HOME: "Home Team", Subject.AWAY: "Away Team"}.get(market.subject, "Team")
        outcome = "Yes" if side == "yes" else "No" if side == "no" else side.title()
        return f"{prefix}{subject_word} Clean Sheet - {outcome}".strip()
    if family == "team_win_to_nil":
        result_word = {Subject.HOME: "Home Win", Subject.AWAY: "Away Win"}.get(market.subject, "Team Win")
        outcome = "Yes" if side == "yes" else "No" if side == "no" else side.title()
        return f"{prefix}{result_word} To Nil - {outcome}".strip()
    if family == "corners_result":
        return f"{prefix}Corners 1X2 - {_RESULT_TEXT.get(side, side.title())}".strip()
    if family == "nth_corner":
        number = market.goal_number or 1
        suffix = "st" if number == 1 else "nd" if number == 2 else "rd" if number == 3 else "th"
        outcome = "No Corner" if side == "none" else _GOAL_SIDE_TEXT.get(side, side.title())
        return f"{prefix}{number}{suffix} Corner - {outcome}".strip()
    if family == "last_corner":
        outcome = "No Corner" if side == "none" else _GOAL_SIDE_TEXT.get(side, side.title())
        return f"{prefix}Last Corner - {outcome}".strip()
    if family == "corner_handicap":
        return f"{prefix}Corner Handicap {_GOAL_SIDE_TEXT.get(side, side.title())} {_line_text(line)}".strip()
    if family == "corner_range":
        return f"{prefix}Corner Range {side}".strip()
    if family == "team_corner_range":
        subject_word = {Subject.HOME: "Home", Subject.AWAY: "Away"}.get(market.subject, "Team")
        return f"{prefix}{subject_word} Corner Range {side}".strip()
    if family == "nth_goal":
        number = market.goal_number or 1
        suffix = "st" if number == 1 else "nd" if number == 2 else "rd" if number == 3 else "th"
        return f"{prefix}{number}{suffix} Goal - {_GOAL_SIDE_TEXT.get(side, side.title())}".strip()
    if family == "total_goals":
        return f"{prefix}{_SIDE_TITLE.get(side, side.title())} {_line_text(line)}".strip()
    if family == "result_total_goals":
        result_side, _, total_side = side.partition("_")
        result_text = _RESULT_TEXT.get(result_side, result_side.title())
        total_text = _SIDE_TITLE.get(total_side, total_side.title())
        return f"{prefix}{result_text} & {total_text} {_line_text(line)}".strip()
    if family == "result_btts":
        result_side, _, btts_side = side.partition("_")
        result_text = _RESULT_TEXT.get(result_side, result_side.title())
        btts_text = "GG / BTTS Yes" if btts_side == "yes" else "BTTS No"
        return f"{prefix}{result_text} & {btts_text}".strip()
    if family == "total_btts":
        total_side, _, btts_side = side.partition("_")
        total_text = _SIDE_TITLE.get(total_side, total_side.title())
        btts_text = "GG / BTTS Yes" if btts_side == "yes" else "BTTS No"
        return f"{prefix}{total_text} {_line_text(line)} & {btts_text}".strip()
    if family == "double_chance_btts":
        dc_side, _, btts_side = side.rpartition("_")
        dc_text = _DC_COMBO_TEXT.get(dc_side, dc_side.replace("_", "/").title())
        btts_text = "GG / BTTS Yes" if btts_side == "yes" else "BTTS No"
        return f"{prefix}{dc_text} & {btts_text}".strip()
    if family == "double_chance_total_goals":
        dc_side, _, total_side = side.rpartition("_")
        dc_text = _DC_COMBO_TEXT.get(dc_side, dc_side.replace("_", "/").title())
        total_text = _SIDE_TITLE.get(total_side, total_side.title())
        return f"{prefix}{dc_text} & {total_text} {_line_text(line)}".strip()
    if family == "result_or_total_goals":
        combo_side, _, answer = side.rpartition("_")
        result_side, _, total_side = combo_side.partition("_")
        result_text = _RESULT_TEXT.get(result_side, result_side.title())
        total_text = _SIDE_TITLE.get(total_side, total_side.title())
        answer_text = "Yes" if answer == "yes" else "No" if answer == "no" else answer.title()
        return f"{prefix}{result_text} or {total_text} {_line_text(line)} - {answer_text}".strip()
    if family == "result_or_btts":
        combo_side, _, answer = side.rpartition("_")
        result_side, _, _btts = combo_side.partition("_")
        result_text = _RESULT_TEXT.get(result_side, result_side.title())
        answer_text = "Yes" if answer == "yes" else "No" if answer == "no" else answer.title()
        return f"{prefix}{result_text} or GG / BTTS Yes - {answer_text}".strip()
    if family == "result_or_clean_sheet":
        combo_side, _, answer = side.rpartition("_")
        result_side, _, _clean_sheet = combo_side.partition("_")
        result_text = _RESULT_TEXT.get(result_side, result_side.title())
        answer_text = "Yes" if answer == "yes" else "No" if answer == "no" else answer.title()
        return f"{prefix}{result_text} or Any Clean Sheet - {answer_text}".strip()
    if family in PLAYER_FAMILIES:
        player = str(market.label or "").strip()
        suffix = {
            "player_card": "To Be Booked",
            "goalscorer_first": "First Goalscorer",
            "goalscorer_last": "Last Goalscorer",
            "goalscorer_anytime": "To Score",
            "player_shots": "Shots",
            "player_shots_on_target": "Shots On Target",
        }.get(family, "")
        return " ".join(part for part in [player, suffix] if part).strip() or market.family.replace("_", " ").title()

    subject_word = {Subject.HOME: "Home", Subject.AWAY: "Away", Subject.EITHER: "Any"}.get(
        market.subject, ""
    )
    label = {
        "team_total_goals": "Team Goals",
        "result_total_goals": "Result & Total Goals",
        "result_btts": "Result & BTTS",
        "total_btts": "Total Goals & BTTS",
        "double_chance_btts": "Double Chance & BTTS",
        "double_chance_total_goals": "Double Chance & Total Goals",
        "result_or_total_goals": "Result or Total Goals",
        "result_or_btts": "Result or BTTS",
        "result_or_clean_sheet": "Result or Clean Sheet",
        "corners_total": "Corners",
        "team_corners": "Team Corners",
        "cards_total": "Cards",
        "team_cards": "Team Cards",
        "booking_points": "Booking Points",
        "team_shots_on_target": "Team Shots On Target",
        "shots_on_target_total": "Shots On Target",
        "asian_handicap": "AH",
        "handicap": "EH",
        "nth_goal": "Nth Goal",
        "exact_cards": "Exact Cards",
        "cards_result": "Cards Result",
        "highest_scoring_half": "Highest Scoring Half",
        "team_goals_odd_even": "Team Goals Odd/Even",
        "team_win_to_nil": "Win To Nil",
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
    player_label = str(market.label or "").strip() if market.family in PLAYER_FAMILIES else ""

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
        player=player_label or market.subject_player_id,
        subject=player_label or str(market.subject),
        period=PERIOD_TO_TAXONOMY[market.period],
        recognized=True,
        core_supported=canonical in CORE_MARKETS,
        requires_player_stats=market.family in PLAYER_FAMILIES,
        requires_card_stats=market.family in CARD_FAMILIES,
        requires_corner_stats=market.family in CORNER_FAMILIES,
        requires_team_goal_stats=market.family in TEAM_GOAL_FAMILIES,
    )
