"""Canonical league registry for Team Intelligence.

Stage 1 is deliberately static. Later stages can hydrate teams, seasons and
market profiles from this list without rediscovering what leagues matter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntelligenceLeague:
    key: str
    name: str
    country: str
    priority: int
    current_season: str
    previous_season: str
    api_football_league_id: str
    statpal_league_id: str = ""
    active: bool = True

    @property
    def provider_ids(self) -> dict[str, str]:
        return {
            "api_football": self.api_football_league_id,
            "statpal": self.statpal_league_id,
        }

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["provider_ids"] = self.provider_ids
        return payload


CURRENT_EUROPEAN_SEASON = "2026-2027"
PREVIOUS_EUROPEAN_SEASON = "2025-2026"


TOP_EUROPEAN_INTELLIGENCE_LEAGUES: tuple[IntelligenceLeague, ...] = (
    IntelligenceLeague(
        key="england-premier-league",
        name="English Premier League",
        country="England",
        priority=1,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="39",
        statpal_league_id="3037",
    ),
    IntelligenceLeague(
        key="spain-la-liga",
        name="Spanish La Liga",
        country="Spain",
        priority=2,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="140",
        statpal_league_id="3232",
    ),
    IntelligenceLeague(
        key="italy-serie-a",
        name="Italian Serie A",
        country="Italy",
        priority=3,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="135",
        statpal_league_id="3102",
    ),
    IntelligenceLeague(
        key="germany-bundesliga",
        name="German Bundesliga",
        country="Germany",
        priority=4,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="78",
        statpal_league_id="3062",
    ),
    IntelligenceLeague(
        key="france-ligue-1",
        name="French Ligue 1",
        country="France",
        priority=5,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="61",
        statpal_league_id="3054",
    ),
    IntelligenceLeague(
        key="england-championship",
        name="English Championship",
        country="England",
        priority=6,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="40",
        statpal_league_id="3038",
    ),
    IntelligenceLeague(
        key="netherlands-eredivisie",
        name="Dutch Eredivisie",
        country="Netherlands",
        priority=7,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="88",
        statpal_league_id="3155",
    ),
    IntelligenceLeague(
        key="portugal-primeira-liga",
        name="Portuguese Primeira Liga",
        country="Portugal",
        priority=8,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="94",
        statpal_league_id="3185",
    ),
    IntelligenceLeague(
        key="belgium-pro-league",
        name="Belgian Pro League",
        country="Belgium",
        priority=9,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="144",
        statpal_league_id="2935",
    ),
    IntelligenceLeague(
        key="scotland-premiership",
        name="Scottish Premiership",
        country="Scotland",
        priority=10,
        current_season=CURRENT_EUROPEAN_SEASON,
        previous_season=PREVIOUS_EUROPEAN_SEASON,
        api_football_league_id="179",
        statpal_league_id="3203",
    ),
)


def team_intelligence_leagues(*, active_only: bool = True) -> tuple[IntelligenceLeague, ...]:
    leagues = TOP_EUROPEAN_INTELLIGENCE_LEAGUES
    if active_only:
        leagues = tuple(league for league in leagues if league.active)
    return tuple(sorted(leagues, key=lambda league: league.priority))


def team_intelligence_league_ids(provider: str, *, active_only: bool = True) -> set[str]:
    provider = str(provider or "").strip().lower().replace("-", "_")
    ids = set()
    for league in team_intelligence_leagues(active_only=active_only):
        value = league.provider_ids.get(provider, "")
        if value:
            ids.add(str(value))
    return ids


def team_intelligence_registry_payload(*, active_only: bool = True) -> list[dict[str, object]]:
    return [league.to_dict() for league in team_intelligence_leagues(active_only=active_only)]
