"""Canonical daily league universe shared by StatPal and API-Football.

The daily products need one selected set of competitions, with provider ids
attached per source. Some competitions exist in one provider only; those stay
in the registry with the missing provider id blank until verified.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DailyTrackedLeague:
    key: str
    name: str
    country: str
    statpal_id: str = ""
    api_football_id: str = ""
    active: bool = True

    @property
    def provider_ids(self) -> dict[str, str]:
        return {
            "statpal": self.statpal_id,
            "api_football": self.api_football_id,
        }

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["provider_ids"] = self.provider_ids
        return payload


DAILY_TRACKED_LEAGUES: tuple[DailyTrackedLeague, ...] = (
    DailyTrackedLeague("caf-african-nations-championship", "CAF African Nations Championship", "Africa", statpal_id="3800"),
    DailyTrackedLeague("australia-a-league", "A-League", "Australia", statpal_id="2919", api_football_id="188"),
    DailyTrackedLeague("australia-new-south-wales", "New South Wales NPL", "Australia", statpal_id="2920", api_football_id="192"),
    DailyTrackedLeague("australia-western-australia", "Western Australia NPL", "Australia", statpal_id="2921", api_football_id="196"),
    DailyTrackedLeague("austria-bundesliga", "Bundesliga Austria", "Austria", statpal_id="2926", api_football_id="218"),
    DailyTrackedLeague("belgium-pro-league", "Pro League", "Belgium", statpal_id="2935", api_football_id="144"),
    DailyTrackedLeague("belgium-cup", "Belgium Cup", "Belgium", statpal_id="2936"),
    DailyTrackedLeague("croatia-hnl", "HNL", "Croatia", statpal_id="3005", api_football_id="210"),
    DailyTrackedLeague("czech-czech-liga", "Czech Liga", "Czech Republic", statpal_id="3017", api_football_id="345"),
    DailyTrackedLeague("denmark-superliga", "Super Liga", "Denmark", statpal_id="3018", api_football_id="119"),
    DailyTrackedLeague("england-fa-trophy", "FA Trophy", "England", statpal_id="3029", api_football_id="47"),
    DailyTrackedLeague("england-league-two", "League Two", "England", statpal_id="3030", api_football_id="42"),
    DailyTrackedLeague("england-fa-cup", "FA Cup", "England", statpal_id="3031", api_football_id="45"),
    DailyTrackedLeague("england-efl-cup", "EFL Cup", "England", statpal_id="3032", api_football_id="48"),
    DailyTrackedLeague("england-premier-league", "Premier League", "England", statpal_id="3037", api_football_id="39"),
    DailyTrackedLeague("england-championship", "Championship", "England", statpal_id="3038", api_football_id="40"),
    DailyTrackedLeague("england-league-one", "League One", "England", statpal_id="3039", api_football_id="41"),
    DailyTrackedLeague("estonia-esiliiga", "Esiliiga", "Estonia", statpal_id="3040", api_football_id="328"),
    DailyTrackedLeague("europe-euro", "UEFA European Championship", "Europe", statpal_id="2834"),
    DailyTrackedLeague("europe-champions-league-women", "Champions League Women", "Europe", statpal_id="2836"),
    DailyTrackedLeague("europe-champions-league", "UEFA Champions League", "Europe", statpal_id="2838", api_football_id="2"),
    DailyTrackedLeague("europe-europa-league", "UEFA Europa League", "Europe", statpal_id="2840", api_football_id="3"),
    DailyTrackedLeague("europe-euro-qualifiers", "UEFA European Championship Qualifiers", "Europe", statpal_id="3364"),
    DailyTrackedLeague("europe-super-cup", "UEFA Super Cup", "Europe", statpal_id="3436", api_football_id="531"),
    DailyTrackedLeague("europe-womens-championship", "UEFA Women's Championship", "Europe", statpal_id="3876"),
    DailyTrackedLeague("europe-nations-league", "Nations League", "Europe", statpal_id="6362"),
    DailyTrackedLeague("europe-conference-league", "UEFA Europa Conference League", "Europe", statpal_id="20686", api_football_id="848"),
    DailyTrackedLeague("finland-kakkonen", "Kakkonen", "Finland", statpal_id="3046"),
    DailyTrackedLeague("finland-ykkonen", "Ykkonen", "Finland", statpal_id="3363", api_football_id="245"),
    DailyTrackedLeague("france-ligue-1", "Ligue 1", "France", statpal_id="3054", api_football_id="61"),
    DailyTrackedLeague("france-super-cup", "Trophee des Champions", "France", statpal_id="3466", api_football_id="526"),
    DailyTrackedLeague("germany-bundesliga-2", "Bundesliga 2", "Germany", statpal_id="3058", api_football_id="79"),
    DailyTrackedLeague("germany-bundesliga", "Bundesliga", "Germany", statpal_id="3062", api_football_id="78"),
    DailyTrackedLeague("germany-super-cup", "Super Cup", "Germany", statpal_id="3453", api_football_id="529"),
    DailyTrackedLeague("international-concacaf-champions-league", "CONCACAF Champions League", "International", statpal_id="2844", api_football_id="16"),
    DailyTrackedLeague("international-afc-challenge-cup", "AFC Challenge Cup", "International", statpal_id="2854"),
    DailyTrackedLeague("international-afc-champions-league", "AFC Champions League", "International", statpal_id="2855", api_football_id="17"),
    DailyTrackedLeague("international-afc-cup", "AFC Cup", "International", statpal_id="2856"),
    DailyTrackedLeague("international-asean-cup", "ASEAN Cup", "International", statpal_id="2858"),
    DailyTrackedLeague("international-fifa-confederations-cup", "FIFA Confederations Cup", "International", statpal_id="2871"),
    DailyTrackedLeague("international-copa-america", "CONMEBOL Copa America", "International", statpal_id="2872"),
    DailyTrackedLeague("international-libertadores", "Libertadores", "International", statpal_id="2873"),
    DailyTrackedLeague("international-africa-cup-of-nations", "Cup Of Nations", "International", statpal_id="2892", api_football_id="6"),
    DailyTrackedLeague("international-caf-super-cup", "CAF Super Cup", "International", statpal_id="2896"),
    DailyTrackedLeague("international-caf-champions-league", "CAF Champions League", "International", statpal_id="3346"),
    DailyTrackedLeague("international-afc-asian-cup", "AFC Asian Cup", "International", statpal_id="3348"),
    DailyTrackedLeague("international-caf-confederations-cup", "CAF Confederations Cup", "International", statpal_id="3468"),
    DailyTrackedLeague("international-fifa-club-world-cup-play-in", "FIFA Club World Cup Play-In", "International", statpal_id="20900"),
    DailyTrackedLeague("ireland-premier-division", "Premier Division", "Ireland", statpal_id="3091", api_football_id="357"),
    DailyTrackedLeague("italy-serie-a", "Serie A", "Italy", statpal_id="3102", api_football_id="135"),
    DailyTrackedLeague("italy-super-cup", "Super Cup", "Italy", statpal_id="3459", api_football_id="547"),
    DailyTrackedLeague("netherlands-knvb-beker", "KNVB Beker", "Netherlands", statpal_id="3153", api_football_id="90"),
    DailyTrackedLeague("netherlands-eredivisie", "Eredivisie", "Netherlands", statpal_id="3155", api_football_id="88"),
    DailyTrackedLeague("netherlands-eerste-divisie", "Eerste Divisie", "Netherlands", statpal_id="3156", api_football_id="89"),
    DailyTrackedLeague("netherlands-super-cup", "Super Cup", "Netherlands", statpal_id="3469", api_football_id="543"),
    DailyTrackedLeague("nigeria-premier-league", "Premier League", "Nigeria", statpal_id="3159", api_football_id="399"),
    DailyTrackedLeague("norway-eliteserien", "Eliteserien", "Norway", statpal_id="3168", api_football_id="103"),
    DailyTrackedLeague("poland-ekstraklasa", "Ekstraklasa", "Poland", statpal_id="3177", api_football_id="106"),
    DailyTrackedLeague("portugal-primeira-liga", "Portuguese Liga", "Portugal", statpal_id="3185", api_football_id="94"),
    DailyTrackedLeague("portugal-super-cup", "Super Cup", "Portugal", statpal_id="3187", api_football_id="550"),
    DailyTrackedLeague("romania-liga-i", "Liga I", "Romania", statpal_id="3194"),
    DailyTrackedLeague("russia-premier-league", "Premier League", "Russia", statpal_id="3290", api_football_id="235"),
    DailyTrackedLeague("scotland-premiership", "Premier League", "Scotland", statpal_id="3203", api_football_id="179"),
    DailyTrackedLeague("scotland-fa-cup", "FA Cup", "Scotland", statpal_id="3204"),
    DailyTrackedLeague("spain-copa-del-rey", "Spain Cup", "Spain", statpal_id="3230", api_football_id="143"),
    DailyTrackedLeague("spain-segunda", "Segunda", "Spain", statpal_id="3231", api_football_id="141"),
    DailyTrackedLeague("spain-la-liga", "Primera", "Spain", statpal_id="3232", api_football_id="140"),
    DailyTrackedLeague("spain-super-cup", "Super Cup", "Spain", statpal_id="3457", api_football_id="556"),
    DailyTrackedLeague("spain-copa-federacion", "Copa Federacion", "Spain", statpal_id="6001"),
    DailyTrackedLeague("sweden-superettan", "Superettan", "Sweden", statpal_id="3238", api_football_id="114"),
    DailyTrackedLeague("switzerland-super-league", "Super League", "Switzerland", statpal_id="3241", api_football_id="207"),
    DailyTrackedLeague("turkey-super-lig", "Super Lig", "Turkey", statpal_id="3258", api_football_id="203"),
    DailyTrackedLeague("ukraine-premier-league", "Premier League", "Ukraine", statpal_id="3261", api_football_id="342"),
    DailyTrackedLeague("wales-premier-league", "Premier League", "Wales", statpal_id="3285", api_football_id="110"),
    DailyTrackedLeague("world-womens-world-cup", "FIFA Women's World Cup", "World", statpal_id="2875", api_football_id="8"),
    DailyTrackedLeague("world-caf-world-cup-qualifiers", "WC Qualification Africa", "World", statpal_id="2881", api_football_id="29"),
    DailyTrackedLeague("world-afc-world-cup-qualifiers", "WC Qualification Asia", "World", statpal_id="2882", api_football_id="30"),
    DailyTrackedLeague("world-concacaf-world-cup-qualifiers", "WC Qualification Concacaf", "World", statpal_id="2883", api_football_id="31"),
    DailyTrackedLeague("world-uefa-world-cup-qualifiers", "WC Qualification Europe", "World", statpal_id="2884", api_football_id="32"),
    DailyTrackedLeague("world-world-cup-intercontinental-playoffs", "WC Intercontinental Playoffs", "World", statpal_id="2885", api_football_id="37"),
    DailyTrackedLeague("world-ofc-world-cup-qualifiers", "WC Qualification Oceania", "World", statpal_id="2886", api_football_id="33"),
    DailyTrackedLeague("world-conmebol-world-cup-qualifiers", "WC Qualification South America", "World", statpal_id="2887", api_football_id="34"),
    DailyTrackedLeague("world-fifa-intercontinental-cup", "FIFA Intercontinental Cup", "World", statpal_id="2874"),
    DailyTrackedLeague("world-world-cup", "World Cup", "World", statpal_id="2889", api_football_id="1"),
    DailyTrackedLeague("world-fifa-club-world-cup", "FIFA Club World Cup", "World", statpal_id="3770", api_football_id="15"),
)


def daily_tracked_leagues(*, active_only: bool = True) -> tuple[DailyTrackedLeague, ...]:
    leagues = DAILY_TRACKED_LEAGUES
    if active_only:
        leagues = tuple(league for league in leagues if league.active)
    return leagues


def daily_tracked_league_ids(provider: str, *, active_only: bool = True) -> set[str]:
    provider = str(provider or "").strip().lower().replace("-", "_")
    ids: set[str] = set()
    for league in daily_tracked_leagues(active_only=active_only):
        value = league.provider_ids.get(provider, "")
        if value:
            ids.add(str(value))
    return ids


def daily_api_football_tracked_leagues(*, active_only: bool = True) -> dict[int, str]:
    leagues: dict[int, str] = {}
    for league in daily_tracked_leagues(active_only=active_only):
        if not league.api_football_id:
            continue
        leagues[int(league.api_football_id)] = league.name
    return leagues


def daily_tracked_registry_payload(*, active_only: bool = True) -> list[dict[str, object]]:
    return [league.to_dict() for league in daily_tracked_leagues(active_only=active_only)]
