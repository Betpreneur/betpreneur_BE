import re
import unicodedata
from dataclasses import asdict, dataclass


CORE_MARKETS = {
    "Home Win",
    "Away Win",
    "Draw",
    "DC: 12",
    "DNB Home",
    "DNB Away",
    "AH Home +0.5",
    "AH Away +0.5",
    "Over 1.5",
    "Over 2.5",
    "Over 3.5",
    "Under 1.5",
    "Under 2.5",
    "Under 3.5",
    "GG / BTTS Yes",
    "GG + Over 2.5",
    "Home CS",
    "Away CS",
    "First to Score H",
    "First to Score A",
}


@dataclass(frozen=True)
class MarketDescriptor:
    raw: str
    canonical: str
    code: str
    family: str
    category: str
    market_type: str = ""
    selection: str = ""
    side: str = ""
    line: str = ""
    team: str = ""
    player: str = ""
    subject: str = ""
    period: str = "match"
    support_level: str = "unsupported"
    data_requirements: tuple = ()
    recognized: bool = True
    core_supported: bool = False
    requires_player_stats: bool = False
    requires_card_stats: bool = False
    requires_corner_stats: bool = False
    requires_team_goal_stats: bool = False

    def to_dict(self):
        return asdict(self)


def normalize_market_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9.+-]+", " ", text).strip()


def _line_from_text(*values):
    joined = " ".join(str(value or "") for value in values)
    patterns = [
        r"(?:total|line|goals?|corners?|cards?|bookings?|shots?|saves?|assists?)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)",
        r"\b(?:over|under|o|u)\s*([0-9]+(?:\.[0-9]+)?)\b",
        r"\b([+-]?[0-9]+(?:\.[0-9]+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, joined, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _side_from_text(value):
    normalized = normalize_market_text(value)
    if normalized in {"1", "home", "home win", "home team", "home wins"}:
        return "home"
    if normalized in {"2", "away", "away win", "away team", "away wins"}:
        return "away"
    if normalized in {"x", "draw"}:
        return "draw"
    if "home" in normalized:
        return "home"
    if "away" in normalized:
        return "away"
    if "draw" in normalized:
        return "draw"
    return ""


def _over_under_side(value):
    normalized = normalize_market_text(value)
    if re.search(r"\b(over|yes|o)\b", normalized):
        return "over"
    if re.search(r"\b(under|no|u)\b", normalized):
        return "under"
    return ""


def _period_from_text(value):
    normalized = normalize_market_text(value)
    window = re.search(r"\bfirst\s+(5|10|15|20|25|30|35|40|50|55|60|65|70|75|80|85)\s+minutes?\b", normalized)
    if window:
        return f"first_{window.group(1)}m"
    if re.search(r"\b(first|1st|1 h|1h|half time|halftime)\b", normalized):
        return "first_half"
    if re.search(r"\b(second|2nd|2 h|2h)\b", normalized):
        return "second_half"
    return "match"


def _support_level(family, *, period="match"):
    if family in {
        "match_result",
        "double_chance",
        "draw_no_bet",
        "asian_handicap",
        "total_goals",
        "team_total_goals",
        "btts",
        "clean_sheet",
    }:
        return "full" if period == "match" else "medium"
    if family in {
        "corners_total",
        "team_corners",
        "cards_total",
        "booking_points",
        "team_cards",
        "first_to_score",
        "last_to_score",
        "goal_range",
        "exact_goals",
        "multigoals",
        "odd_even",
        "winning_margin",
        "half_market",
    }:
        return "medium"
    if family.startswith("player_") or family in {"correct_score", "time_window", "score_combo"}:
        return "weak"
    return "unsupported"


def _data_requirements(family):
    requirements = {
        "match_result": ("team_stats", "league_stats", "odds"),
        "double_chance": ("team_stats", "league_stats", "odds"),
        "draw_no_bet": ("team_stats", "league_stats", "odds"),
        "asian_handicap": ("team_stats", "league_stats", "odds"),
        "total_goals": ("team_stats", "league_stats", "h2h", "odds"),
        "team_total_goals": ("team_stats", "league_stats", "odds"),
        "btts": ("team_stats", "league_stats", "h2h", "odds"),
        "clean_sheet": ("team_stats", "league_stats", "odds"),
        "corners_total": ("corner_stats", "league_stats", "odds"),
        "team_corners": ("corner_stats", "team_stats", "odds"),
        "cards_total": ("card_stats", "league_stats", "referee_stats", "odds"),
        "team_cards": ("card_stats", "team_stats", "referee_stats", "odds"),
        "booking_points": ("card_stats", "league_stats", "referee_stats", "odds"),
        "player_goal": ("player_stats", "lineups", "team_stats", "odds"),
        "player_shots": ("player_stats", "lineups", "team_stats", "odds"),
        "player_shots_on_target": ("player_stats", "lineups", "team_stats", "odds"),
        "player_card": ("player_stats", "card_stats", "lineups", "referee_stats", "odds"),
        "player_assist": ("player_stats", "lineups", "team_stats", "odds"),
    }
    return requirements.get(family, ("team_stats", "league_stats", "odds"))


def _mk(
    *,
    raw,
    canonical,
    code,
    family,
    category,
    market_type="",
    selection="",
    side="",
    line="",
    team="",
    player="",
    subject="",
    period="match",
    support_level="",
    data_requirements=(),
    requires_player_stats=False,
    requires_card_stats=False,
    requires_corner_stats=False,
    requires_team_goal_stats=False,
):
    return MarketDescriptor(
        raw=str(raw or "").strip(),
        canonical=canonical,
        code=code,
        family=family,
        category=category,
        market_type=market_type or family,
        selection=selection or side,
        side=side,
        line=str(line or ""),
        team=team,
        player=player,
        subject=subject,
        period=period,
        support_level=support_level or _support_level(family, period=period),
        data_requirements=tuple(data_requirements or _data_requirements(family)),
        core_supported=canonical in CORE_MARKETS,
        requires_player_stats=requires_player_stats,
        requires_card_stats=requires_card_stats,
        requires_corner_stats=requires_corner_stats,
        requires_team_goal_stats=requires_team_goal_stats,
    )


def describe_market(value, *, market_name="", outcome_name="", specifier=""):
    raw = str(value or "").strip()
    market = str(market_name or "").strip()
    outcome = str(outcome_name or "").strip()
    spec = str(specifier or "").strip()
    combined = " ".join(item for item in [raw, market, outcome, spec] if item)
    text = normalize_market_text(combined)
    market_text = normalize_market_text(market)
    outcome_text = normalize_market_text(outcome or raw)
    line = _line_from_text(spec, outcome, market, raw)
    period = _period_from_text(combined)

    aliases = {
        "1": "Home Win",
        "home": "Home Win",
        "home win": "Home Win",
        "home team": "Home Win",
        "2": "Away Win",
        "away": "Away Win",
        "away win": "Away Win",
        "away team": "Away Win",
        "x": "Draw",
        "draw": "Draw",
        "12": "DC: 12",
        "home or away": "DC: 12",
        "home away": "DC: 12",
        "home/away": "DC: 12",
        "dc 12": "DC: 12",
        "dc: 12": "DC: 12",
        "double chance 12": "DC: 12",
        "both teams to score": "GG / BTTS Yes",
        "btts": "GG / BTTS Yes",
        "btts yes": "GG / BTTS Yes",
        "gg": "GG / BTTS Yes",
        "gg / btts yes": "GG / BTTS Yes",
    }
    normalized_raw = normalize_market_text(raw)
    if normalized_raw in aliases:
        canonical = aliases[normalized_raw]
        if canonical == "Home Win":
            return _mk(raw=raw or canonical, canonical=canonical, code="result_home", family="match_result", category="Result", side="home", selection="home", period=period)
        if canonical == "Away Win":
            return _mk(raw=raw or canonical, canonical=canonical, code="result_away", family="match_result", category="Result", side="away", selection="away", period=period)
        if canonical == "Draw":
            return _mk(raw=raw or canonical, canonical=canonical, code="result_draw", family="match_result", category="Result", side="draw", selection="draw", period=period)
        if canonical == "DC: 12":
            return _mk(raw=raw or canonical, canonical=canonical, code="double_chance_12", family="double_chance", category="Result", side="12", selection="12", period=period)
        if canonical == "GG / BTTS Yes":
            return _mk(raw=raw or canonical, canonical=canonical, code="btts_yes", family="btts", category="Goals", side="yes", selection="yes", period=period)

    if re.fullmatch(r"(home win|away win|draw)", text):
        side = _side_from_text(text)
        canonical = {"home": "Home Win", "away": "Away Win", "draw": "Draw"}[side]
        return _mk(raw=raw or canonical, canonical=canonical, code=f"result_{side}", family="match_result", category="Result", side=side, selection=side, period=period)

    if "match result" in text or "1x2" in text or market_text in {"match result superodds"}:
        side = _side_from_text(outcome or raw)
        if side:
            canonical = {"home": "Home Win", "away": "Away Win", "draw": "Draw"}[side]
            return _mk(raw=raw or canonical, canonical=canonical, code=f"result_{side}", family="match_result", category="Result", side=side, selection=side, period=period)

    if "double chance" in text or outcome_text in {"1x", "x2", "12"}:
        side = outcome_text.replace(" ", "") or text.replace("double chance", "").strip()
        label = {"1x": "DC: 1X", "x2": "DC: X2", "12": "DC: 12"}.get(side, f"Double Chance {side.upper()}")
        return _mk(raw=raw or label, canonical=label, code=f"double_chance_{side}", family="double_chance", category="Result", side=side, selection=side, period=period)

    if "draw no bet" in text or "dnb" in text:
        side = _side_from_text(outcome or raw)
        if side in {"home", "away"}:
            canonical = "DNB Home" if side == "home" else "DNB Away"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"dnb_{side}", family="draw_no_bet", category="Result", side=side, selection=side, period=period)

    if "both teams" in text or "btts" in text:
        yes_no = "no" if re.search(r"\b(no)\b", outcome_text or text) else "yes"
        canonical = "GG / BTTS Yes" if yes_no == "yes" else "BTTS No"
        return _mk(raw=raw or canonical, canonical=canonical, code=f"btts_{yes_no}", family="btts", category="Goals", side=yes_no, selection=yes_no, period=period)

    if "clean sheet" in text:
        side = _side_from_text(text)
        if side in {"home", "away"}:
            canonical = "Home CS" if side == "home" else "Away CS"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"clean_sheet_{side}", family="clean_sheet", category="Clean Sheet", side=side, team=side, selection="yes", period=period)

    if "first to score" in text or "first team to score" in text:
        side = _side_from_text(outcome or raw)
        if side in {"home", "away"}:
            canonical = "First to Score H" if side == "home" else "First to Score A"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"first_score_{side}", family="first_to_score", category="Scoring", side=side, selection=side, period=period)

    if "last goal" in text or "last to score" in text:
        side = _side_from_text(outcome or raw)
        if side in {"home", "away"}:
            canonical = "Last to Score H" if side == "home" else "Last to Score A"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"last_score_{side}", family="last_to_score", category="Scoring", side=side, selection=side, period=period)

    if "handicap" in text or re.search(r"\bah\b", text):
        side = _side_from_text(outcome or raw or market)
        handicap = _line_from_text(spec, outcome, raw)
        if side and handicap:
            prefix = "AH Home" if side == "home" else "AH Away" if side == "away" else "AH"
            canonical = f"{prefix} {handicap}"
            family = "asian_handicap" if "asian" in text or re.search(r"\bah\b", text) else "handicap"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"{family}_{side}_{handicap}", family=family, category="Asian Handicap", side=side, selection=side, line=handicap, period=period)

    if "corner" in text:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            team_side = "home" if "home" in text else "away" if "away" in text else ""
            family = "team_corners" if team_side else "corners_total"
            canonical = f"{team_side.title() + ' Team ' if team_side else ''}Corners {side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"{family}_{team_side or 'total'}_{side}_{line}", family=family, category="Corners", side=side, team=team_side, selection=side, line=line, period=period, requires_corner_stats=True)
        return _mk(raw=raw or combined, canonical=raw or combined, code="corners_market", family="corners", category="Corners", period=period, requires_corner_stats=True)

    player_card_phrase = re.search(r"\b(to be booked|to get booked|to receive a card|carded|booked yes)\b", text)
    if ("player" in text and ("card" in text or "booking" in text or "booked" in text)) or player_card_phrase:
        subject = outcome or raw
        return _mk(
            raw=raw or combined,
            canonical=raw or outcome or combined,
            code="player_card",
            family="player_card",
            category="Player Cards",
            line=line,
            player=subject,
            subject=subject,
            period=period,
            requires_player_stats=True,
            requires_card_stats=True,
        )

    if "card" in text or "booking" in text or "booked" in text or "yellow" in text or "red card" in text:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            team_side = "home" if "home" in text else "away" if "away" in text else ""
            family = "team_cards" if team_side else "cards_total"
            category = "Booking Points" if "booking point" in text else "Cards"
            if "booking point" in text:
                family = "booking_points"
            canonical = f"{team_side.title() + ' Team ' if team_side else ''}{category} {side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"{family}_{team_side or 'total'}_{side}_{line}", family=family, category=category, side=side, team=team_side, selection=side, line=line, period=period, requires_card_stats=True)
        return _mk(raw=raw or combined, canonical=raw or combined, code="cards_market", family="cards", category="Cards", period=period, requires_card_stats=True)

    if "player" in text or any(keyword in text for keyword in ["to score", "shots", "shot on target", "assist", "saves", "tackles"]):
        subject = outcome or raw
        if "shot on target" in text or "shots on target" in text:
            family = "player_shots_on_target"
            category = "Player Shots"
        elif "shot" in text:
            family = "player_shots"
            category = "Player Shots"
        elif "assist" in text:
            family = "player_assist"
            category = "Player Assists"
        elif "card" in text or "booking" in text or "booked" in text:
            family = "player_card"
            category = "Player Cards"
        elif "save" in text:
            family = "player_saves"
            category = "Player Saves"
        else:
            family = "player_goal"
            category = "Player Goals"
        return _mk(
            raw=raw or combined,
            canonical=raw or outcome or combined,
            code=family,
            family=family,
            category=category,
            line=line,
            subject=subject,
            player=subject,
            period=period,
            requires_player_stats=True,
            requires_card_stats=family == "player_card",
        )

    if "correct score" in text:
        return _mk(raw=raw or combined, canonical=raw or outcome or combined, code="correct_score", family="correct_score", category="Scoreline", selection=outcome or raw, period=period)

    if "odd even" in text or re.search(r"\b(odd|even)\b", outcome_text):
        team_side = "home" if "home" in text else "away" if "away" in text else ""
        selection = "odd" if "odd" in outcome_text or "odd" in text else "even" if "even" in outcome_text or "even" in text else ""
        canonical = f"{team_side.title() + ' Team ' if team_side else ''}Odd/Even {selection.title()}".strip()
        return _mk(raw=raw or canonical, canonical=canonical, code=f"odd_even_{team_side or 'total'}_{selection}", family="odd_even", category="Goals", team=team_side, selection=selection, period=period)

    if any(key in text for key in ["goal bounds", "goal range", "exact goals", "multigoals", "excluded number of goals"]):
        if "exact goals" in text or "excluded number" in text:
            family = "exact_goals"
        elif "multigoals" in text:
            family = "multigoals"
        else:
            family = "goal_range"
        team_side = "home" if "home" in text else "away" if "away" in text else ""
        selection = outcome or raw
        return _mk(raw=raw or combined, canonical=selection or combined, code=f"{family}_{team_side or 'total'}", family=family, category="Goal Ranges", team=team_side, selection=selection, period=period)

    if re.search(r"\b[0-9]+\s*\+(?=\s|$)", text):
        team_side = "home" if "home" in text else "away" if "away" in text else ""
        goal_match = re.search(r"\b([0-9]+)\s*\+(?=\s|$)", text)
        if goal_match:
            goal_line = str(max(int(goal_match.group(1)) - 0.5, 0.5))
            canonical = f"{team_side.title() + ' Team ' if team_side else ''}Over {goal_line}".strip()
            return _mk(raw=raw or canonical, canonical=canonical, code=f"team_total_goals_{team_side or 'unknown'}_over_{goal_line}", family="team_total_goals", category="Team Goals", side="over", team=team_side, selection="over", line=goal_line, period=period, requires_team_goal_stats=True)

    if "winning margin" in text or "lead by" in text:
        team_side = "home" if "home" in text else "away" if "away" in text else ""
        return _mk(raw=raw or combined, canonical=raw or outcome or combined, code=f"winning_margin_{team_side or 'any'}", family="winning_margin", category="Result", team=team_side, selection=outcome or raw, period=period)

    team_goals_match = re.search(r"\b(home|away|team)\b.*\b([0-9]+)\s*\+(?=\s|$)", text)
    if team_goals_match:
        team_side = "home" if "home" in text else "away" if "away" in text else ""
        goal_line = str(max(int(team_goals_match.group(2)) - 0.5, 0.5))
        canonical = f"{team_side.title() + ' Team ' if team_side else 'Team '}Over {goal_line}"
        return _mk(raw=raw or canonical, canonical=canonical, code=f"team_total_goals_{team_side or 'team'}_over_{goal_line}", family="team_total_goals", category="Team Goals", side="over", team=team_side, selection="over", line=goal_line, period=period, requires_team_goal_stats=True)

    total_context = "over under" in text or "total goals" in text or re.search(r"\b(over|under)\b", text)
    if total_context:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            if (
                "team total" in text
                or "home total" in text
                or "away total" in text
                or re.search(r"\b(home|away)\s+team\s+(?:goals?\s+)?(?:over|under)\b", text)
                or re.search(r"\b(home|away)\s+over\s+under\b", text)
            ):
                team_side = "home" if "home" in text else "away" if "away" in text else ""
                canonical = f"{team_side.title()} Team {side.title()} {line}".strip()
                return _mk(raw=raw or canonical, canonical=canonical, code=f"team_total_goals_{team_side}_{side}_{line}", family="team_total_goals", category="Team Goals", side=side, team=team_side, selection=side, line=line, period=period, requires_team_goal_stats=True)
            canonical = f"{side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"total_goals_{side}_{line}", family="total_goals", category="Goals", side=side, selection=side, line=line, period=period)

    if "gg over" in text or ("btts" in text and "over" in text):
        line = line or "2.5"
        canonical = f"GG + Over {line}"
        return _mk(raw=raw or canonical, canonical=canonical, code=f"btts_over_{line}", family="combo", category="Goals", side="over", selection="yes_over", line=line, period=period)

    return MarketDescriptor(
        raw=raw or combined,
        canonical=raw or combined,
        code="unknown",
        family="unknown",
        category="Other",
        recognized=False,
        core_supported=False,
    )


def canonical_market_name(value):
    return describe_market(value).canonical or value


def market_matches(left, right):
    return normalize_market_text(canonical_market_name(left)) == normalize_market_text(canonical_market_name(right))


def market_options():
    groups = {
        "Result": [
            ("Home Win", "Home Win", "Home team to win"),
            ("Away Win", "Away Win", "Away team to win"),
            ("Draw", "Draw", "Match ends in a draw"),
            ("DC: 12", "Home or Away", "Either team wins"),
            ("DNB Home", "Draw No Bet - Home", "Home win, draw refunds"),
            ("DNB Away", "Draw No Bet - Away", "Away win, draw refunds"),
        ],
        "Asian Handicap": [
            ("AH Home +0.5", "Home +0.5", "Home win or draw"),
            ("AH Away +0.5", "Away +0.5", "Away win or draw"),
        ],
        "Goals": [
            ("Over 0.5", "Over 0.5 Goals", "1 or more total goals"),
            ("Over 1.5", "Over 1.5 Goals", "2 or more total goals"),
            ("Over 2.5", "Over 2.5 Goals", "3 or more total goals"),
            ("Over 3.5", "Over 3.5 Goals", "4 or more total goals"),
            ("Under 1.5", "Under 1.5 Goals", "1 or 0 total goals"),
            ("Under 2.5", "Under 2.5 Goals", "2 or fewer total goals"),
            ("Under 3.5", "Under 3.5 Goals", "3 or fewer total goals"),
            ("GG / BTTS Yes", "Both Teams To Score", "Both teams to score"),
            ("BTTS No", "BTTS No", "At least one team does not score"),
            ("GG + Over 2.5", "BTTS + Over 2.5", "Both score and 3+ goals"),
        ],
        "Team Goals": [
            ("Home Team Over 0.5", "Home Over 0.5 Goals", "Home team scores at least once"),
            ("Away Team Over 0.5", "Away Over 0.5 Goals", "Away team scores at least once"),
        ],
        "Corners": [
            ("Corners Over 8.5", "Corners Over 8.5", "9 or more corners"),
            ("Corners Under 10.5", "Corners Under 10.5", "10 or fewer corners"),
        ],
        "Cards": [
            ("Cards Over 3.5", "Cards Over 3.5", "4 or more cards"),
            ("Cards Under 5.5", "Cards Under 5.5", "5 or fewer cards"),
            ("Player To Be Carded", "Player To Be Carded", "Selected player receives a card"),
        ],
        "Player": [
            ("Player To Score", "Player To Score", "Selected player scores"),
            ("Player Shots Over 1.5", "Player Shots Over 1.5", "Selected player has 2+ shots"),
            ("Player Shots On Target Over 0.5", "Player SOT Over 0.5", "Selected player has 1+ shot on target"),
            ("Player Assist", "Player Assist", "Selected player assists a goal"),
        ],
        "Clean Sheet": [
            ("Home CS", "Home Clean Sheet", "Home team keeps clean sheet"),
            ("Away CS", "Away Clean Sheet", "Away team keeps clean sheet"),
        ],
        "Scoring": [
            ("First to Score H", "Home First To Score", "Home team scores first"),
            ("First to Score A", "Away First To Score", "Away team scores first"),
        ],
    }
    options = []
    for group, items in groups.items():
        for value, label, meaning in items:
            descriptor = describe_market(value)
            options.append(
                {
                    "value": value,
                    "label": label,
                    "group": group,
                    "meaning": meaning,
                    "family": descriptor.family,
                    "category": descriptor.category,
                    "core_supported": descriptor.core_supported,
                    "requires_player_stats": descriptor.requires_player_stats,
                    "requires_card_stats": descriptor.requires_card_stats,
                    "requires_corner_stats": descriptor.requires_corner_stats,
                }
            )
    return options
