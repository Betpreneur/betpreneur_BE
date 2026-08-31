from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .evaluation import assessment_type_for, evaluator_for
from .taxonomy import describe_market


@dataclass(frozen=True)
class DailyMarketCatalogEntry:
    market: str
    meaning: str
    group: str
    enabled: bool = True
    publish_enabled: bool = True
    proven: bool = False
    odds_key: str = ""
    generation: str = "fixed"
    descriptor_override: dict = field(default_factory=dict)

    @property
    def family_payload(self) -> dict:
        return daily_market_family_payload(self.market, self.descriptor_override)


def daily_evaluation_route(market: str) -> dict:
    payload = daily_market_family_payload(market)
    return {
        "family": payload["market_family"],
        "assessment_type": payload["assessment_type"],
        "engine": payload["evaluation_engine"],
        "publishes_probability": payload["publishes_probability"],
        "required_capabilities": payload["required_capabilities"],
        "optional_capabilities": payload["optional_capabilities"],
    }


DAILY_MARKET_CATALOG: tuple[DailyMarketCatalogEntry, ...] = (
    DailyMarketCatalogEntry(
        "Home Win",
        "Home team to win",
        "result",
        odds_key="hw",
    ),
    DailyMarketCatalogEntry("Away Win", "Away team to win", "result", odds_key="aw"),
    DailyMarketCatalogEntry("Draw", "Match ends in a draw", "result", odds_key="d"),
    DailyMarketCatalogEntry("Over 1.5", "2 or more total goals", "goals", proven=True, odds_key="o15"),
    DailyMarketCatalogEntry("Under 1.5", "1 or 0 total goals", "goals", odds_key="u15"),
    DailyMarketCatalogEntry("Over 2.5", "3 or more total goals", "goals", odds_key="o25"),
    DailyMarketCatalogEntry("Under 2.5", "2 or fewer total goals", "goals", odds_key="u25"),
    DailyMarketCatalogEntry("Over 3.5", "4 or more total goals", "goals", odds_key="o35"),
    DailyMarketCatalogEntry("Under 3.5", "3 or fewer total goals", "goals", proven=True, odds_key="u35"),
    DailyMarketCatalogEntry("Over 4.5", "5 or more total goals", "goals", odds_key="o45"),
    DailyMarketCatalogEntry("Under 4.5", "4 or fewer total goals", "goals", odds_key="u45"),
    DailyMarketCatalogEntry("GG / BTTS Yes", "Both teams to score", "goals", proven=True, odds_key="btts_yes"),
    DailyMarketCatalogEntry("BTTS No", "At least one team not to score", "goals", odds_key="btts_no"),
    DailyMarketCatalogEntry(
        "GG + Over 2.5",
        "Both score & 3+ goals",
        "goals",
        descriptor_override={
            "canonical": "GG + Over 2.5",
            "code": "btts_over_2.5",
            "family": "total_btts",
            "category": "Goals",
            "side": "over",
            "selection": "yes_over",
            "line": "2.5",
            "support_level": "full",
            "recognized": True,
            "core_supported": True,
        },
    ),
    DailyMarketCatalogEntry("DC: 12", "Home or Away win", "result", odds_key="12"),
    DailyMarketCatalogEntry("DC: 1X", "Home win or draw", "result", enabled=False, publish_enabled=False),
    DailyMarketCatalogEntry("DC: X2", "Away win or draw", "result", enabled=False, publish_enabled=False),
    DailyMarketCatalogEntry("DNB Home", "Home win (Draw = refund)", "result"),
    DailyMarketCatalogEntry("DNB Away", "Away win (Draw = refund)", "result"),
    DailyMarketCatalogEntry(
        "Home CS",
        "Home team keeps clean sheet",
        "clean_sheet",
        descriptor_override={
            "canonical": "Home CS",
            "code": "clean_sheet_home",
            "family": "clean_sheet",
            "category": "Clean Sheet",
            "side": "home",
            "selection": "yes",
            "team": "home",
            "support_level": "full",
            "recognized": True,
            "core_supported": True,
        },
    ),
    DailyMarketCatalogEntry(
        "Away CS",
        "Away team keeps clean sheet",
        "clean_sheet",
        descriptor_override={
            "canonical": "Away CS",
            "code": "clean_sheet_away",
            "family": "clean_sheet",
            "category": "Clean Sheet",
            "side": "away",
            "selection": "yes",
            "team": "away",
            "support_level": "full",
            "recognized": True,
            "core_supported": True,
        },
    ),
    DailyMarketCatalogEntry(
        "AH Home +0.5",
        "Home win or draw (+0.5)",
        "handicap",
        enabled=False,
        publish_enabled=False,
    ),
    DailyMarketCatalogEntry(
        "AH Away +0.5",
        "Away win or draw (+0.5)",
        "handicap",
        enabled=False,
        publish_enabled=False,
    ),
    DailyMarketCatalogEntry("First to Score H", "Home team scores first", "scoring", proven=True),
    DailyMarketCatalogEntry("First to Score A", "Away team scores first", "scoring"),
)


DAILY_MARKET_LOOKUP = {entry.market: entry for entry in DAILY_MARKET_CATALOG}
DAILY_MARKET_MEANINGS = {entry.market: entry.meaning for entry in DAILY_MARKET_CATALOG}
DAILY_MARKET_FAMILY_OVERRIDES = {
    entry.market: entry.descriptor_override
    for entry in DAILY_MARKET_CATALOG
    if entry.descriptor_override
}
PROVEN_DAILY_MARKETS = {entry.market for entry in DAILY_MARKET_CATALOG if entry.proven}
EXCLUDED_DAILY_MARKETS = {
    entry.market
    for entry in DAILY_MARKET_CATALOG
    if not entry.enabled or not entry.publish_enabled
}


def _decimal_line(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _standard_half_goal_line(line: Decimal | None) -> bool:
    if line is None:
        return False
    return line % Decimal("1") == Decimal("0.5")


def _daily_line_allowed(market: str) -> bool:
    descriptor = describe_market(market)
    line = _decimal_line(descriptor.line)
    side = (descriptor.side or descriptor.selection or "").lower()
    if descriptor.family == "total_goals" and line is not None:
        return _standard_half_goal_line(line) and Decimal("1.5") <= line <= Decimal("4.5")
    if descriptor.family == "team_corners" and side == "over" and line is not None:
        return line >= Decimal("2.5")
    return True


def _daily_group_for_family(family: str) -> str:
    if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
        return "result"
    if family in {"total_goals", "team_total_goals", "btts", "total_btts"}:
        return "goals"
    if family in {"corners_total", "team_corners"}:
        return "corners"
    if family in {"cards_total", "team_cards", "booking_points"}:
        return "cards"
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        return "shots"
    if family == "clean_sheet":
        return "clean_sheet"
    if family == "first_to_score":
        return "scoring"
    return "other"


def _dynamic_market_meaning(market: str) -> str:
    descriptor = describe_market(market)
    side = (descriptor.side or descriptor.selection or "").lower()
    line = descriptor.line
    team_prefix = ""
    if descriptor.team == "home":
        team_prefix = "Home team "
    elif descriptor.team == "away":
        team_prefix = "Away team "

    if descriptor.family == "total_goals" and line:
        return f"Match to finish with {'more than' if side == 'over' else 'fewer than'} {line} total goals"
    if descriptor.family == "team_total_goals" and line:
        return f"{team_prefix or 'Selected team '}to score {'more than' if side == 'over' else 'fewer than'} {line} goals"
    if descriptor.family == "btts":
        return "Both teams to score" if side == "yes" else "At least one team not to score"
    if descriptor.family in {"corners_total", "team_corners"} and line:
        subject = team_prefix or "Match "
        return f"{subject}to finish with {'more than' if side == 'over' else 'fewer than'} {line} corners"
    if descriptor.family in {"cards_total", "team_cards"} and line:
        subject = team_prefix or "Match "
        return f"{subject}to finish with {'more than' if side == 'over' else 'fewer than'} {line} cards"
    if descriptor.family == "booking_points" and line:
        return f"Match to finish with {'more than' if side == 'over' else 'fewer than'} {line} booking points"
    if descriptor.family in {"shots_on_target_total", "team_shots_on_target"} and line:
        subject = team_prefix or "Match "
        return f"{subject}to finish with {'more than' if side == 'over' else 'fewer than'} {line} shots on target"
    return descriptor.canonical or market


def daily_catalog_entry(market: str) -> DailyMarketCatalogEntry | None:
    market = str(market or "").strip()
    if not _daily_line_allowed(market):
        return None
    if market in DAILY_MARKET_LOOKUP:
        return DAILY_MARKET_LOOKUP[market]
    if market.startswith("Corners Over "):
        line = market.rsplit(" ", 1)[-1]
        return DailyMarketCatalogEntry(
            market,
            f"Match to finish with more than {line} total corners",
            "corners",
            generation="odds_line",
        )
    if market.startswith("Corners Under "):
        line = market.rsplit(" ", 1)[-1]
        return DailyMarketCatalogEntry(
            market,
            f"Match to finish with fewer than {line} total corners",
            "corners",
            generation="odds_line",
        )
    descriptor = describe_market(market)
    if (
        descriptor.recognized
        and descriptor.support_level != "unsupported"
        and evaluator_for(descriptor.family)
    ):
        return DailyMarketCatalogEntry(
            descriptor.canonical or market,
            _dynamic_market_meaning(market),
            _daily_group_for_family(descriptor.family),
            generation="discovered",
        )
    return None


def daily_market_names(*, include_excluded=False) -> list[str]:
    return [
        entry.market
        for entry in DAILY_MARKET_CATALOG
        if include_excluded or (entry.enabled and entry.publish_enabled)
    ]


def daily_scoring_market_names() -> list[str]:
    return [
        entry.market
        for entry in DAILY_MARKET_CATALOG
        if entry.enabled and entry.generation == "fixed"
    ]


def _line_markets(prefix: str, lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix} Over {line}" for line in lines) + tuple(
        f"{prefix} Under {line}" for line in lines
    )


DAILY_MARKET_DISCOVERY_POOL: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *daily_scoring_market_names(),
            "Home Team Over 1.5",
            "Away Team Over 1.5",
            "Home Team Under 2.5",
            "Away Team Under 2.5",
            "1H Over 1.5",
            "1H Under 1.5",
            "1H Under 2.5",
            *_line_markets("Corners", ("7.5", "8.5", "9.5", "10.5", "11.5")),
            *_line_markets("Home Team Corners", ("2.5", "3.5", "4.5", "5.5", "6.5")),
            *_line_markets("Away Team Corners", ("2.5", "3.5", "4.5", "5.5", "6.5")),
            *_line_markets("Cards", ("2.5", "3.5", "4.5", "5.5")),
            *_line_markets("Home Team Cards", ("1.5", "2.5", "3.5")),
            *_line_markets("Away Team Cards", ("1.5", "2.5", "3.5")),
            *_line_markets("Booking Points", ("35.5", "45.5", "55.5", "65.5")),
            *_line_markets("Shots On Target", ("6.5", "7.5", "8.5", "9.5", "10.5", "11.5")),
            *_line_markets("Home Team Shots On Target", ("2.5", "3.5", "4.5", "5.5")),
            *_line_markets("Away Team Shots On Target", ("2.5", "3.5", "4.5", "5.5")),
        ]
    )
)


def daily_discovery_market_names(*, include_excluded=False) -> list[str]:
    markets: list[str] = []
    for market in DAILY_MARKET_DISCOVERY_POOL:
        entry = daily_catalog_entry(market)
        if not entry:
            continue
        if not include_excluded and (not entry.enabled or not entry.publish_enabled):
            continue
        if entry.market not in markets:
            markets.append(entry.market)
    return markets


def daily_odds_key_map() -> dict[str, str]:
    return {
        entry.market: entry.odds_key
        for entry in DAILY_MARKET_CATALOG
        if entry.odds_key
    }


def build_daily_market_scores(score_values: dict, dynamic_scores: dict | None = None) -> dict:
    scores = {
        market: score_values[market]
        for market in daily_scoring_market_names()
        if market in score_values
    }
    for market, value in (dynamic_scores or {}).items():
        entry = daily_catalog_entry(market)
        if entry and entry.enabled:
            scores[market] = value
    return scores


def daily_markets_by_family(*, include_excluded=False) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for entry in DAILY_MARKET_CATALOG:
        if not include_excluded and (not entry.enabled or not entry.publish_enabled):
            continue
        family = entry.family_payload["market_family"]
        grouped.setdefault(family, []).append(entry.market)
    return dict(sorted(grouped.items()))


def daily_market_family_payload(market: str, descriptor_override: dict | None = None) -> dict:
    descriptor = describe_market(market)
    identity = descriptor.to_dict()
    override = descriptor_override or DAILY_MARKET_FAMILY_OVERRIDES.get(str(market or "")) or {}
    if override:
        identity.update(override)
        identity["raw"] = str(market or "")
    family = identity.get("family") or descriptor.family
    spec = evaluator_for(family)
    return {
        "market_family": family,
        "market_category": identity.get("category") or descriptor.category,
        "market_identity": identity,
        "assessment_type": assessment_type_for(family),
        "evaluation_engine": spec.engine if spec else "",
        "market_support_level": identity.get("support_level") or descriptor.support_level,
        "market_recognized": bool(identity.get("recognized")),
        "market_core_supported": bool(identity.get("core_supported")),
        "publishes_probability": bool(spec and spec.publishes_probability),
        "required_capabilities": [cap.value for cap in spec.required] if spec else [],
        "optional_capabilities": [cap.value for cap in spec.optional] if spec else [],
    }
