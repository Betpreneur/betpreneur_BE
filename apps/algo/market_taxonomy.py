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
    side: str = ""
    line: str = ""
    subject: str = ""
    period: str = "match"
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


def _mk(
    *,
    raw,
    canonical,
    code,
    family,
    category,
    side="",
    line="",
    subject="",
    period="match",
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
        side=side,
        line=str(line or ""),
        subject=subject,
        period=period,
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
            return _mk(raw=raw or canonical, canonical=canonical, code="result_home", family="match_result", category="Result", side="home")
        if canonical == "Away Win":
            return _mk(raw=raw or canonical, canonical=canonical, code="result_away", family="match_result", category="Result", side="away")
        if canonical == "Draw":
            return _mk(raw=raw or canonical, canonical=canonical, code="result_draw", family="match_result", category="Result", side="draw")
        if canonical == "DC: 12":
            return _mk(raw=raw or canonical, canonical=canonical, code="double_chance_12", family="double_chance", category="Result", side="12")
        if canonical == "GG / BTTS Yes":
            return _mk(raw=raw or canonical, canonical=canonical, code="btts_yes", family="btts", category="Goals", side="yes")

    if re.fullmatch(r"(home win|away win|draw)", text):
        side = _side_from_text(text)
        canonical = {"home": "Home Win", "away": "Away Win", "draw": "Draw"}[side]
        return _mk(raw=raw or canonical, canonical=canonical, code=f"result_{side}", family="match_result", category="Result", side=side)

    if "match result" in text or "1x2" in text or market_text in {"match result superodds"}:
        side = _side_from_text(outcome or raw)
        if side:
            canonical = {"home": "Home Win", "away": "Away Win", "draw": "Draw"}[side]
            return _mk(raw=raw or canonical, canonical=canonical, code=f"result_{side}", family="match_result", category="Result", side=side)

    if "double chance" in text or outcome_text in {"1x", "x2", "12"}:
        side = outcome_text.replace(" ", "") or text.replace("double chance", "").strip()
        label = {"1x": "DC: 1X", "x2": "DC: X2", "12": "DC: 12"}.get(side, f"Double Chance {side.upper()}")
        return _mk(raw=raw or label, canonical=label, code=f"double_chance_{side}", family="double_chance", category="Result", side=side)

    if "draw no bet" in text or "dnb" in text:
        side = _side_from_text(outcome or raw)
        if side in {"home", "away"}:
            canonical = "DNB Home" if side == "home" else "DNB Away"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"dnb_{side}", family="draw_no_bet", category="Result", side=side)

    if "both teams" in text or "btts" in text:
        yes_no = "no" if re.search(r"\b(no)\b", outcome_text or text) else "yes"
        canonical = "GG / BTTS Yes" if yes_no == "yes" else "BTTS No"
        return _mk(raw=raw or canonical, canonical=canonical, code=f"btts_{yes_no}", family="btts", category="Goals", side=yes_no)

    if "clean sheet" in text:
        side = _side_from_text(text)
        if side in {"home", "away"}:
            canonical = "Home CS" if side == "home" else "Away CS"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"clean_sheet_{side}", family="clean_sheet", category="Clean Sheet", side=side)

    if "first to score" in text or "first team to score" in text:
        side = _side_from_text(outcome or raw)
        if side in {"home", "away"}:
            canonical = "First to Score H" if side == "home" else "First to Score A"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"first_score_{side}", family="first_to_score", category="Scoring", side=side)

    if "handicap" in text or re.search(r"\bah\b", text):
        side = _side_from_text(outcome or raw or market)
        handicap = _line_from_text(spec, outcome, raw)
        if side and handicap:
            prefix = "AH Home" if side == "home" else "AH Away" if side == "away" else "AH"
            canonical = f"{prefix} {handicap}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"asian_handicap_{side}_{handicap}", family="asian_handicap", category="Asian Handicap", side=side, line=handicap)

    if "corner" in text:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            canonical = f"Corners {side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"corners_total_{side}_{line}", family="corners_total", category="Corners", side=side, line=line, requires_corner_stats=True)
        return _mk(raw=raw or combined, canonical=raw or combined, code="corners_market", family="corners", category="Corners", requires_corner_stats=True)

    if "card" in text or "booking" in text or "yellow" in text or "red card" in text:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            canonical = f"Cards {side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"cards_total_{side}_{line}", family="cards_total", category="Cards", side=side, line=line, requires_card_stats=True)
        return _mk(raw=raw or combined, canonical=raw or combined, code="cards_market", family="cards", category="Cards", requires_card_stats=True)

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
        elif "card" in text or "booking" in text:
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
            requires_player_stats=True,
            requires_card_stats=family == "player_card",
        )

    total_context = "over under" in text or "total goals" in text or re.search(r"\b(over|under)\b", text)
    if total_context:
        side = _over_under_side(outcome or raw or text)
        if side and line:
            if "team total" in text or "home total" in text or "away total" in text:
                team_side = "home" if "home" in text else "away" if "away" in text else ""
                canonical = f"{team_side.title()} Team {side.title()} {line}".strip()
                return _mk(raw=raw or canonical, canonical=canonical, code=f"team_total_goals_{team_side}_{side}_{line}", family="team_total_goals", category="Team Goals", side=team_side or side, line=line, requires_team_goal_stats=True)
            canonical = f"{side.title()} {line}"
            return _mk(raw=raw or canonical, canonical=canonical, code=f"total_goals_{side}_{line}", family="total_goals", category="Goals", side=side, line=line)

    if "gg over" in text or ("btts" in text and "over" in text):
        line = line or "2.5"
        canonical = f"GG + Over {line}"
        return _mk(raw=raw or canonical, canonical=canonical, code=f"btts_over_{line}", family="combo", category="Goals", side="over", line=line)

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
