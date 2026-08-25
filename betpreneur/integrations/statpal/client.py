import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)


class StatPalError(RuntimeError):
    pass


class StatPalConfigurationError(StatPalError):
    pass


@dataclass(frozen=True)
class StatPalConfig:
    access_key: str
    base_url: str
    usage_base_url: str
    timeout: int
    enabled: bool
    #: Optional per-endpoint path overrides, keyed STATPAL_ENDPOINT_<NAME>.
    endpoints: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, config: dict) -> "StatPalConfig":
        """Build from a plain mapping. The caller reads settings, not us."""
        config = config or {}
        return cls(
            access_key=str(config.get("STATPAL_ACCESS_KEY") or "").strip(),
            base_url=str(config.get("STATPAL_BASE_URL") or "https://statpal.io/api/v2").rstrip("/"),
            usage_base_url=str(config.get("STATPAL_USAGE_BASE_URL") or "https://statpal.io/api").rstrip("/"),
            timeout=int(config.get("STATPAL_TIMEOUT") or 20),
            enabled=str(config.get("STATPAL_ENABLED") or "False").strip().lower()
            in {"1", "true", "yes", "on"},
            endpoints={
                str(k): str(v)
                for k, v in config.items()
                if str(k).startswith(("STATPAL_ENDPOINT_", "STATPAL_ENDSOINT_"))
            },
        )


class StatPalClient:
    """
    Small provider client for StatPal.

    Stage 1 intentionally keeps this independent from the prediction engine.
    Use `get()` for any documented endpoint while we confirm exact endpoint
    shapes and coverage.
    """

    def __init__(self, config: StatPalConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def _require_configured(self) -> None:
        if not self.config.enabled:
            raise StatPalConfigurationError("StatPal is disabled. Set STATPAL_ENABLED=True to use this provider.")
        if not self.config.access_key:
            raise StatPalConfigurationError("Missing STATPAL_ACCESS_KEY.")

    def _url(self, path: str, *, usage_api: bool = False) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base_url = self.config.usage_base_url if usage_api else self.config.base_url
        return f"{base_url}/{path.lstrip('/')}"

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
        usage_api: bool = False,
    ) -> dict[str, Any]:
        self._require_configured()
        request_params = {**(params or {})}
        request_params.setdefault("access_key", self.config.access_key)
        url = self._url(path, usage_api=usage_api)
        try:
            response = self.session.get(url, params=request_params, timeout=timeout or self.config.timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            snippet = self._response_snippet(exc.response)
            raise StatPalError(
                f"StatPal request failed status={exc.response.status_code if exc.response else 'unknown'} "
                f"path={path!r} response={snippet!r}"
            ) from exc
        except requests.RequestException as exc:
            raise StatPalError(f"StatPal request failed path={path!r}: {exc}") from exc
        except json.JSONDecodeError as exc:
            snippet = self._response_snippet(response)
            raise StatPalError(f"StatPal did not return JSON path={path!r} response={snippet!r}") from exc

    def usage_count(self) -> dict[str, Any]:
        return self.get("user-request-count", usage_api=True)

    def soccer(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.get(f"soccer/{path.lstrip('/')}", params=params)

    def soccer_live_matches(self) -> dict[str, Any]:
        return self.soccer("matches/live")

    def soccer_leagues(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUES", params=params)

    def soccer_league_seasons(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUE_SEASONS", params=params)

    def soccer_daily_matches(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_MATCHES_DAILY", params=params)

    def soccer_league_matches(self, league_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUE_MATCHES", params=params, league_id=league_id)

    def soccer_league_match_stats(self, league_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUE_MATCH_STATS", params=params, league_id=league_id)

    def soccer_league_standings(self, league_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUE_STANDINGS", params=params, league_id=league_id)

    def soccer_league_stats(self, league_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LEAGUE_STATS", params=params, league_id=league_id)

    def soccer_head_to_head(self, team1_id: str | int, team2_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = {**(params or {}), "team1_id": team1_id, "team2_id": team2_id}
        return self.soccer_endpoint("SOCCER_HEAD_TO_HEAD", params=request_params)

    def soccer_injuries_suspensions(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_INJURIES_SUSPENSIONS", params=params)

    def soccer_team(self, team_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_TEAM", params=params, team_id=team_id)

    def soccer_player(self, player_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_PLAYER", params=params, player_id=player_id)

    def soccer_coach(self, coach_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_COACH", params=params, coach_id=coach_id)

    def soccer_team_lineups(self, match_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = {**(params or {}), "match_id": match_id}
        return self.soccer_endpoint("SOCCER_TEAM_LINEUPS", params=request_params)

    def soccer_predictions(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_PREDICTIONS", params=params)

    def soccer_prematch_odds(self, league_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_PREMATCH_ODDS", params=params, league_id=league_id)

    def soccer_weather_forecast(self, match_id: str | int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = {**(params or {}), "match_id": match_id}
        return self.soccer_endpoint("SOCCER_WEATHER_FORECAST", params=request_params)

    def soccer_images(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_IMAGES", params=params)

    def soccer_live_storylines(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LIVE_STORYLINES", params=params)

    def soccer_live_odds(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LIVE_ODDS", params=params)

    def soccer_live_odds_markets(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LIVE_ODDS_MARKETS", params=params)

    def soccer_live_odds_match_states(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.soccer_endpoint("SOCCER_LIVE_ODDS_MATCH_STATES", params=params)

    def soccer_endpoint(self, endpoint_name: str, params: dict[str, Any] | None = None, **path_params: Any) -> dict[str, Any]:
        path = self._configured_endpoint(endpoint_name)
        if path_params:
            path = path.replace("{league-id}", "{league_id}")
            path = path.format(**path_params)
        return self.get(path, params=params)

    def _configured_endpoint(self, endpoint_name: str) -> str:
        config = self.config.endpoints
        key = f"STATPAL_ENDPOINT_{endpoint_name.upper()}"
        path = str(config.get(key) or "").strip()
        if not path and endpoint_name.upper() == "SOCCER_HEAD_TO_HEAD":
            # Historical typo in the settings key, kept so existing deploys work.
            path = str(config.get("STATPAL_ENDPOINT_SOCCER_HEAD_TO_HEAD") or "").strip()
        if path:
            return path
        defaults = {
            "SOCCER_LEAGUES": "soccer/leagues",
            "SOCCER_LIVE_MATCHES": "soccer/matches/live",
            "SOCCER_LEAGUE_SEASONS": "soccer/leagues/seasons",
            "SOCCER_MATCHES_DAILY": "soccer/matches/daily",
            "SOCCER_LEAGUE_MATCHES": "soccer/leagues/{league_id}/matches",
            "SOCCER_LEAGUE_MATCH_STATS": "soccer/leagues/{league_id}/matches/stats",
            "SOCCER_LEAGUE_STANDINGS": "soccer/leagues/{league_id}/standings",
            "SOCCER_LEAGUE_STATS": "soccer/leagues/{league_id}/stats",
            "SOCCER_HEAD_TO_HEAD": "soccer/head-to-head",
            "SOCCER_INJURIES_SUSPENSIONS": "soccer/injuries-suspensions",
            "SOCCER_TEAM": "soccer/teams/{team_id}",
            "SOCCER_PLAYER": "soccer/players/{player_id}",
            "SOCCER_COACH": "soccer/coaches/{coach_id}",
            "SOCCER_IMAGES": "soccer/images",
            "SOCCER_LIVE_STORYLINES": "soccer/live-storylines",
            "SOCCER_TEAM_LINEUPS": "soccer/team-lineups",
            "SOCCER_WEATHER_FORECAST": "soccer/weather-forecast",
            "SOCCER_PREDICTIONS": "soccer/predictions",
            "SOCCER_PREMATCH_ODDS": "soccer/leagues/{league_id}/odds/prematch",
            "SOCCER_LIVE_ODDS": "soccer/odds/live",
            "SOCCER_LIVE_ODDS_MARKETS": "soccer/odds/live/markets",
            "SOCCER_LIVE_ODDS_MATCH_STATES": "soccer/odds/live/match-states",
            # Backward-compatible aliases used by earlier StatPal integration stages.
            "SOCCER_PLAYER_STATS": "soccer/players/{player_id}",
            "SOCCER_TEAM_STATS": "soccer/teams/{team_id}",
            "SOCCER_LINEUPS": "soccer/team-lineups",
            "SOCCER_DETAILED_STATS": "soccer/leagues/{league_id}/matches/stats",
        }
        try:
            return defaults[endpoint_name.upper()]
        except KeyError as exc:
            raise StatPalConfigurationError(f"No StatPal endpoint configured for {endpoint_name!r}.") from exc

    @staticmethod
    def _response_snippet(response) -> str:
        if response is None:
            return ""
        return (response.text or "")[:300].replace("\n", " ")
