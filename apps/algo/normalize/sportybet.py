"""
SportyBet market identity -> canonical market.

SportyBet is a Sportradar UOF feed (``sourceType`` BET_RADAR, ``eventId`` ``sr:match:…``),
so ``marketId`` is a stable vocabulary rather than free text. Standard UOF ids sit in the
low range; SportyBet vendor extensions occupy 60xxx / 800xxx / 900xxx.

Two facts drive the design, both established from real slips:

* **Outcome ids are scoped per market, not global.** ``12``/``13`` mean Over/Under on
  market 18 but market 900300 uses ``30``/``31``, and 143 uses ``730``/``732`` for
  bucket outcomes. So the key is ``(market_id, outcome_id)``.
* **Period lives in the market id, not the specifier.** 166 is full-match corners and
  177 is first-half corners with identical specifiers. Period is therefore unrecoverable
  from text, which is why every half market was previously read as a full match.

Anything not in the table resolves to ``UNRESOLVED`` and is reported as such. It is
never silently guessed from the display string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .canonical import (
    CanonicalMarket,
    Period,
    Resolution,
    Settlement,
    Subject,
    settlement_for_line,
)


# --- shared outcome vocabularies -------------------------------------------------
OU = {"12": "over", "13": "under"}
OU_ALT = {"30": "over", "31": "under"}          # team corners use a different pair
ONE_X_TWO = {"1": "home", "2": "draw", "3": "away"}
DOUBLE_CHANCE = {"9": "home_or_draw", "10": "home_or_away", "11": "draw_or_away"}
YES_NO = {"74": "yes", "76": "no"}
NEXT_GOAL = {"6": "home", "7": "none", "8": "away"}
HCP_3WAY = {"1711": "home", "1712": "draw", "1713": "away"}
HCP_2WAY = {"1714": "home", "1715": "away"}
DNB = {"4": "home", "5": "away"}
ODD_EVEN = {"32": "even", "33": "odd"}
SCORING_HALF = {"436": "first_half", "438": "second_half", "440": "equal"}
EXACT_BOOKINGS = {"730": "0-1", "732": "2"}
COMBO_1H_RESULT_BTTS = {"78": "home_and_yes", "80": "home_and_no"}
COMBO_DC_1H_BTTS = {"1718": "home_or_draw_and_yes", "1719": "home_or_draw_and_no"}

# Player markets encode the player in the outcome id itself, two different ways.
PLAYER_OUTCOME = "player"


@dataclass(frozen=True)
class MarketSpec:
    family: str
    period: Period = Period.FULL_MATCH
    subject: Subject = Subject.MATCH
    outcomes: dict[str, str] = field(default_factory=dict)
    settlement: Settlement | None = None   # None -> derive from the line
    warnings: tuple[str, ...] = ()


_FULL, _1H, _2H = Period.FULL_MATCH, Period.FIRST_HALF, Period.SECOND_HALF
_MATCH, _HOME, _AWAY, _EITHER, _PLAYER = (
    Subject.MATCH, Subject.HOME, Subject.AWAY, Subject.EITHER, Subject.PLAYER,
)


SPORTYBET_MARKETS: dict[str, MarketSpec] = {
    # --- match result family ---
    "1":  MarketSpec("match_result", _FULL, _MATCH, ONE_X_TWO, Settlement.THREE_WAY),
    "10": MarketSpec("double_chance", _FULL, _MATCH, DOUBLE_CHANCE, Settlement.WIN_LOSE),
    "60": MarketSpec("match_result", _1H, _MATCH, ONE_X_TWO, Settlement.THREE_WAY),
    "63": MarketSpec("double_chance", _1H, _MATCH, DOUBLE_CHANCE, Settlement.WIN_LOSE),
    "64": MarketSpec("draw_no_bet", _1H, _MATCH, DNB, Settlement.WIN_LOSE_VOID),
    "83": MarketSpec("match_result", _2H, _MATCH, ONE_X_TWO, Settlement.THREE_WAY),
    "85": MarketSpec("double_chance", _2H, _MATCH, DOUBLE_CHANCE, Settlement.WIN_LOSE),

    # --- handicaps: 14/65/87 are European (3-way, goal start), 16/66/88 Asian ---
    "14": MarketSpec("handicap", _FULL, _MATCH, HCP_3WAY, Settlement.THREE_WAY),
    "65": MarketSpec("handicap", _1H, _MATCH, HCP_3WAY, Settlement.THREE_WAY),
    "87": MarketSpec("handicap", _2H, _MATCH, HCP_3WAY, Settlement.THREE_WAY),
    "16": MarketSpec("asian_handicap", _FULL, _MATCH, HCP_2WAY),
    "66": MarketSpec("asian_handicap", _1H, _MATCH, HCP_2WAY),
    "88": MarketSpec("asian_handicap", _2H, _MATCH, HCP_2WAY),

    # --- goals ---
    "18": MarketSpec("total_goals", _FULL, _MATCH, OU),
    "68": MarketSpec("total_goals", _1H, _MATCH, OU),
    "90": MarketSpec("total_goals", _2H, _MATCH, OU),
    "19": MarketSpec("team_total_goals", _FULL, _HOME, OU),
    "20": MarketSpec("team_total_goals", _FULL, _AWAY, OU),
    "29": MarketSpec("btts", _FULL, _MATCH, YES_NO, Settlement.WIN_LOSE),
    "900032": MarketSpec("team_goals_odd_even", _1H, _HOME, ODD_EVEN, Settlement.WIN_LOSE),

    # --- goal sequencing ---
    "8":  MarketSpec("nth_goal", _FULL, _MATCH, NEXT_GOAL, Settlement.THREE_WAY),
    "62": MarketSpec("nth_goal", _1H, _MATCH, NEXT_GOAL, Settlement.THREE_WAY),
    "52": MarketSpec("highest_scoring_half", _FULL, _MATCH, SCORING_HALF, Settlement.THREE_WAY),

    # --- goalscorers: outcome id is "sr:player:<id>" ---
    "38": MarketSpec("goalscorer_first", _FULL, _PLAYER, {}, Settlement.WIN_LOSE),
    "39": MarketSpec("goalscorer_last", _FULL, _PLAYER, {}, Settlement.WIN_LOSE),
    "40": MarketSpec("goalscorer_anytime", _FULL, _PLAYER, {}, Settlement.WIN_LOSE),

    # --- cards / bookings ---
    "136": MarketSpec("cards_result", _FULL, _MATCH, ONE_X_TWO, Settlement.THREE_WAY),
    "139": MarketSpec("cards_total", _FULL, _MATCH, OU),
    "138": MarketSpec("booking_points", _FULL, _MATCH, OU),
    "143": MarketSpec("exact_cards", _FULL, _HOME, EXACT_BOOKINGS, Settlement.WIN_LOSE),
    "144": MarketSpec("exact_cards", _FULL, _AWAY, EXACT_BOOKINGS, Settlement.WIN_LOSE),
    "800060": MarketSpec("team_cards", _FULL, _MATCH, {}, Settlement.WIN_LOSE,
                         ("team_resolved_from_outcome_text",)),
    "1191": MarketSpec("player_card", _FULL, _PLAYER, {}, Settlement.WIN_LOSE),
    "800117": MarketSpec("player_card", _FULL, _PLAYER, {}, Settlement.WIN_LOSE),

    # --- corners ---
    "166": MarketSpec("corners_total", _FULL, _MATCH, OU),
    "177": MarketSpec("corners_total", _1H, _MATCH, OU),
    "900300": MarketSpec("team_corners", _FULL, _HOME, OU_ALT),
    "900301": MarketSpec("team_corners", _FULL, _AWAY, OU_ALT),
    "900302": MarketSpec("team_corners", _1H, _HOME, OU_ALT),
    "900303": MarketSpec("team_corners", _1H, _AWAY, OU_ALT),

    # --- shots on target: TEAM markets, not player props ---
    "900546": MarketSpec("team_shots_on_target", _FULL, _HOME, OU),
    "900547": MarketSpec("team_shots_on_target", _FULL, _AWAY, OU),

    # --- vendor extensions: team scores N+ in a row / leads by N ---
    "60000": MarketSpec("btts_n_plus", _FULL, _MATCH, YES_NO, Settlement.WIN_LOSE),
    "60010": MarketSpec("team_scores_n_plus", _FULL, _EITHER, YES_NO, Settlement.WIN_LOSE),
    "60011": MarketSpec("team_scores_n_plus", _FULL, _HOME, YES_NO, Settlement.WIN_LOSE),
    "60012": MarketSpec("team_scores_n_plus", _FULL, _AWAY, YES_NO, Settlement.WIN_LOSE),
    "60020": MarketSpec("team_scores_n_plus", _FULL, _EITHER, YES_NO, Settlement.WIN_LOSE),
    "60021": MarketSpec("team_scores_n_plus", _FULL, _HOME, YES_NO, Settlement.WIN_LOSE),
    "60300": MarketSpec("team_leads_by_n", _FULL, _EITHER, YES_NO, Settlement.WIN_LOSE),
    "60301": MarketSpec("team_leads_by_n", _FULL, _EITHER, YES_NO, Settlement.WIN_LOSE),
    "60303": MarketSpec("team_leads_by_n", _FULL, _HOME, YES_NO, Settlement.WIN_LOSE),

    # --- early payout: settles on a trigger, NOT a plain over/under ---
    "60180": MarketSpec("total_goals", _FULL, _MATCH, OU, Settlement.EARLY_PAYOUT,
                        ("early_payout_market",)),

    # --- combos: two conditions on one leg ---
    "78":  MarketSpec("combo", _1H, _MATCH, COMBO_1H_RESULT_BTTS, Settlement.WIN_LOSE,
                      ("combined_market",)),
    "540": MarketSpec("combo", _FULL, _MATCH, COMBO_DC_1H_BTTS, Settlement.WIN_LOSE,
                      ("combined_market",)),
}


# --- specifier grammar -----------------------------------------------------------
_PLAYER_VARIANT = re.compile(r"pre:playerprops:(?P<match>\d+):(?P<player>\d+)")
_SR_PLAYER = re.compile(r"sr:player:(?P<player>\d+)")


def parse_specifier(specifier: str) -> dict[str, str]:
    """`total=2.5`, `hcp=0:1`, `minsnr=10|total=1.5` -> a flat dict."""
    parsed: dict[str, str] = {}
    for part in str(specifier or "").split("|"):
        if "=" in part:
            key, _, value = part.partition("=")
            parsed[key.strip()] = value.strip()
    return parsed


def _line_from(params: dict[str, str]) -> float | None:
    """
    Signed line, always expressed from the home team's perspective.

    Asian handicaps already use that convention (`hcp=-1.5` means home -1.5). European
    handicaps state a goal start as `home:away`, so `hcp=0:3` gives the away side a
    three-goal start, which is home -3. Both must land on the same scale or a handicap
    read one way settles as its opposite.
    """
    if "total" in params:
        try:
            return float(params["total"])
        except ValueError:
            return None
    if "hcp" in params:
        raw = params["hcp"]
        if ":" in raw:
            home, _, away = raw.partition(":")
            try:
                return float(home) - float(away)
            except ValueError:
                return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _player_id_from(specifier: str, outcome_id: str) -> str:
    for candidate in (specifier, outcome_id):
        match = _PLAYER_VARIANT.search(str(candidate or ""))
        if match:
            return match.group("player")
        match = _SR_PLAYER.search(str(candidate or ""))
        if match:
            return match.group("player")
    return ""


def resolve(
    *,
    market_id,
    outcome_id,
    specifier: str = "",
    market_label: str = "",
    outcome_label: str = "",
) -> CanonicalMarket:
    """Resolve a SportyBet selection to a canonical market. Never guesses from text."""
    market_id = str(market_id or "").strip()
    outcome_id = str(outcome_id or "").strip()
    spec = SPORTYBET_MARKETS.get(market_id)
    params = parse_specifier(specifier)
    line = _line_from(params)
    goal_number = int(params["goalnr"]) if params.get("goalnr", "").isdigit() else None

    if spec is None:
        return CanonicalMarket(
            family="unknown",
            resolution=Resolution.UNRESOLVED,
            line=line,
            goal_number=goal_number,
            label=market_label or outcome_label,
            warnings=[f"unmapped_bookmaker_market:{market_id}"],
        )

    warnings = list(spec.warnings)
    player_id = _player_id_from(specifier, outcome_id) if spec.subject == _PLAYER else ""

    if spec.subject == _PLAYER:
        side = PLAYER_OUTCOME
        if not player_id:
            warnings.append("player_id_not_resolved")
    else:
        side = spec.outcomes.get(outcome_id, "")
        if not side:
            warnings.append(f"unmapped_outcome:{market_id}:{outcome_id}")

    settlement = spec.settlement or settlement_for_line(line)

    return CanonicalMarket(
        family=spec.family,
        period=spec.period,
        subject=spec.subject,
        side=side,
        line=line,
        settlement=settlement,
        resolution=Resolution.MAPPED,
        subject_player_id=player_id,
        goal_number=goal_number,
        label=market_label or outcome_label,
        warnings=warnings,
    )


def resolve_selection(selection: dict, market: dict, outcome: dict) -> CanonicalMarket:
    """Convenience wrapper over a raw SportyBet ``selection``/``market``/``outcome``."""
    return resolve(
        market_id=selection.get("marketId") or market.get("id"),
        outcome_id=selection.get("outcomeId") or outcome.get("id"),
        specifier=selection.get("specifier") or market.get("specifier") or "",
        market_label=market.get("name") or market.get("desc") or "",
        outcome_label=outcome.get("desc") or "",
    )
