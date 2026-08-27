from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from betpreneur.modules.catalog.domain.league_registry import (
    IntelligenceLeague,
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.models import DataCoverage, TeamSeasonProfile
from betpreneur.modules.catalog.services.legacy_runner import aps_get
from betpreneur.modules.catalog.services.provider_client import (
    StatPalClient,
    StatPalConfigurationError,
    StatPalError,
    statpal_client,
)
from betpreneur.modules.catalog.services.resolution import (
    ProviderMappingService,
    provider_mapping_service,
)
from betpreneur.modules.catalog.services.statpal_normalize import (
    normalize_league_standings,
    normalize_team,
)
from betpreneur.platform.db.json import json_safe


@dataclass(frozen=True)
class HistoricalHydrationScope:
    league: IntelligenceLeague
    season: str
    provider: str = "auto"


class HistoricalTeamHydrator:
    """Hydrate stable team-season profiles for the Team Intelligence Store."""

    COVERAGE_KEY = "historical_team_season_profile"

    def __init__(
        self,
        *,
        client: StatPalClient | None = None,
        mapping_service: ProviderMappingService | None = None,
    ):
        self.client = client or statpal_client()
        self.mapping_service = mapping_service or provider_mapping_service

    def hydrate(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        seasons: list[str] | tuple[str, ...] | None = None,
        max_teams: int | None = None,
    ) -> dict[str, Any]:
        scopes = self._scopes(league_keys=league_keys, seasons=seasons)
        results = [self.hydrate_scope(scope, max_teams=max_teams) for scope in scopes]
        return {
            "provider": "statpal",
            "scopes": len(results),
            "leagues": len({item["league_key"] for item in results}),
            "seasons": sorted({item["season"] for item in results}),
            "teams_considered": sum(item.get("teams_considered", 0) for item in results),
            "profiles_saved": sum(item.get("profiles_saved", 0) for item in results),
            "coverage_fresh": sum(item.get("coverage_fresh", 0) for item in results),
            "coverage_failed": sum(item.get("coverage_failed", 0) for item in results),
            "skipped": sum(1 for item in results if item.get("status") == "skipped"),
            "results": results,
        }

    def hydrate_scope(
        self,
        scope: HistoricalHydrationScope,
        *,
        max_teams: int | None = None,
    ) -> dict[str, Any]:
        league = scope.league
        statpal_league_id = str(league.statpal_league_id or "").strip()
        provider = "statpal"
        provider_league_id = statpal_league_id
        source_errors = []
        standings = []
        if statpal_league_id:
            try:
                standings_payload = self.client.soccer_league_standings(
                    statpal_league_id,
                    params={"season": scope.season},
                )
                standings = [
                    row
                    for row in normalize_league_standings(standings_payload)
                    if self._row_matches_scope(
                        row,
                        league=league,
                        season=scope.season,
                        provider_league_id=statpal_league_id,
                    )
                ]
            except (StatPalConfigurationError, StatPalError) as exc:
                source_errors.append({"provider": "statpal", "error": str(exc)[:300]})

        if not standings and league.api_football_league_id:
            provider = "api_football"
            provider_league_id = str(league.api_football_league_id or "").strip()
            try:
                standings = self._api_football_standings(scope)
            except Exception as exc:
                source_errors.append({"provider": "api_football", "error": str(exc)[:300]})

        if not standings:
            reason = "no_standings_rows"
            if not league.statpal_league_id and not league.api_football_league_id:
                reason = "missing_provider_league_id"
            return self._scope_failed(
                scope,
                reason,
                provider=provider,
                provider_league_id=provider_league_id,
                errors=source_errors,
            )
        if max_teams is not None:
            standings = standings[: max(0, int(max_teams))]

        saved = 0
        coverage_fresh = 0
        coverage_failed = 0
        errors = []
        for standing in standings:
            team_id = str(standing.get("team_id") or "").strip()
            team_payload = {}
            team_error = ""
            if team_id and provider == "statpal":
                try:
                    team_payload = normalize_team(self.client.soccer_team(team_id)) or {}
                except (StatPalConfigurationError, StatPalError) as exc:
                    team_error = str(exc)
                except Exception as exc:
                    team_error = str(exc)

            try:
                self.save_team_season_profile(
                    league=league,
                    season=scope.season,
                    standing=standing,
                    team_payload=team_payload,
                    provider=provider,
                    provider_league_id=provider_league_id,
                    team_error=team_error,
                )
                saved += 1
                if team_error:
                    coverage_failed += 1
                    errors.append({"team_id": team_id, "team_name": standing.get("team_name"), "error": team_error[:300]})
                else:
                    coverage_fresh += 1
            except Exception as exc:
                coverage_failed += 1
                errors.append({"team_id": team_id, "team_name": standing.get("team_name"), "error": str(exc)[:300]})

        return {
            "status": "complete" if not errors else "partial",
            "league_key": league.key,
            "league_name": league.name,
            "season": scope.season,
            "provider": provider,
            "provider_league_id": provider_league_id,
            "teams_considered": len(standings),
            "profiles_saved": saved,
            "coverage_fresh": coverage_fresh,
            "coverage_failed": coverage_failed,
            "errors": errors[:25],
        }

    def save_team_season_profile(
        self,
        *,
        league: IntelligenceLeague,
        season: str,
        standing: dict[str, Any],
        team_payload: dict[str, Any] | None,
        provider_league_id: str,
        provider: str = "statpal",
        team_error: str = "",
    ) -> TeamSeasonProfile:
        team_payload = team_payload or {}
        team_id = str(team_payload.get("provider_team_id") or standing.get("team_id") or "").strip()
        team_name = str(team_payload.get("name") or standing.get("team_name") or "").strip()
        stats_row = self._team_league_stats_row(
            team_payload,
            provider_league_id=provider_league_id,
            season=season,
        )
        fulltime = stats_row.get("fulltime") or {}
        firsthalf = stats_row.get("firsthalf") or {}
        overall = standing.get("overall") or {}
        home = standing.get("home") or {}
        away = standing.get("away") or {}
        matches_played = self._int(overall.get("games_played"))
        data_quality = self._data_quality(matches_played, bool(stats_row), bool(team_error))

        with transaction.atomic():
            team = self.mapping_service.link_provider_team_identity(
                provider=provider,
                provider_team_id=team_id,
                provider_team_name=team_name,
                canonical_name=team_name,
                country=league.country or standing.get("country") or team_payload.get("country") or "",
                league_key=league.key,
                league_name=league.name,
                provider_league_id=provider_league_id,
                season=season,
                confidence=95 if team_id else 70,
                resolution_method="historical_team_hydrator",
                payload={"standing": standing, "team": self._compact_team_payload(team_payload)},
            )
            profile, _ = TeamSeasonProfile.objects.update_or_create(
                team=team,
                league_key=league.key,
                season=season,
                defaults={
                    "league_name": league.name,
                    "country": league.country or standing.get("country") or team_payload.get("country") or "",
                    "provider_ids": json_safe(
                        {
                            "statpal": {
                                "team_id": team_id if provider == "statpal" else "",
                                "league_id": provider_league_id if provider == "statpal" else league.statpal_league_id,
                                "season": season if provider == "statpal" else "",
                            },
                            "api_football": {
                                "team_id": team_id if provider == "api_football" else "",
                                "league_id": league.api_football_league_id,
                                "season": self._api_football_season(season),
                            },
                        }
                    ),
                    "matches_played": matches_played,
                    "home_matches": self._int(home.get("games_played")),
                    "away_matches": self._int(away.get("games_played")),
                    "goals_for": self._num(overall.get("goals_scored")),
                    "goals_against": self._num(overall.get("goals_allowed")),
                    "home_goals_for": self._num(home.get("goals_scored")),
                    "home_goals_against": self._num(home.get("goals_allowed")),
                    "away_goals_for": self._num(away.get("goals_scored")),
                    "away_goals_against": self._num(away.get("goals_allowed")),
                    "corners_for": self._phase_total(fulltime, "corners", matches_played),
                    "cards_for": self._phase_total(fulltime, "yellowcards", matches_played),
                    "shots_for": self._phase_total(fulltime, "shots_total", matches_played),
                    "shots_on_target_for": self._phase_total(fulltime, "shots_on_goal", matches_played),
                    "clean_sheet_rate": self._rate(fulltime, "clean_sheet", matches_played),
                    "btts_rate": None,
                    "over_15_rate": None,
                    "over_25_rate": None,
                    "stats": json_safe(
                        {
                            "standing": standing,
                            "team": self._compact_team_payload(team_payload),
                            "fulltime": fulltime,
                            "firsthalf": firsthalf,
                            "source_error": team_error,
                        }
                    ),
                    "data_quality": data_quality,
                    "source": provider,
                    "fetched_at": timezone.now(),
                    "computed_at": timezone.now(),
                },
            )
            self._save_coverage(profile, team_error=team_error, provider=provider)
        return profile

    def _scopes(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None,
        seasons: list[str] | tuple[str, ...] | None,
    ) -> list[HistoricalHydrationScope]:
        selected = set(league_keys or [])
        scopes = []
        for league in team_intelligence_leagues(active_only=True):
            if selected and league.key not in selected:
                continue
            league_seasons = seasons or (league.current_season, league.previous_season)
            for season in league_seasons:
                scopes.append(HistoricalHydrationScope(league=league, season=str(season)))
        return scopes

    @staticmethod
    def _row_matches_scope(
        row: dict[str, Any],
        *,
        league: IntelligenceLeague,
        season: str,
        provider_league_id: str,
    ) -> bool:
        row_league_id = str(row.get("provider_competition_id") or "").strip()
        row_season = str(row.get("season") or "").strip()
        if row_league_id and row_league_id != provider_league_id:
            return False
        if row_season and row_season != str(season):
            return False
        return bool(row.get("team_id") or row.get("team_name"))

    def _api_football_standings(self, scope: HistoricalHydrationScope) -> list[dict[str, Any]]:
        response = aps_get(
            "/standings",
            {
                "league": scope.league.api_football_league_id,
                "season": self._api_football_season(scope.season),
            },
            timeout=20,
        )
        rows = []
        for item in response or []:
            league_payload = item.get("league") if isinstance(item, dict) else {}
            tables = league_payload.get("standings") if isinstance(league_payload, dict) else []
            for table in tables or []:
                for standing in table or []:
                    if not isinstance(standing, dict):
                        continue
                    team = standing.get("team") if isinstance(standing.get("team"), dict) else {}
                    team_id = str(team.get("id") or "").strip()
                    team_name = str(team.get("name") or "").strip()
                    if not team_id and not team_name:
                        continue
                    rows.append(
                        {
                            "provider": "api_football",
                            "source": "api_football",
                            "id": f"api_football:{scope.league.api_football_league_id}:{scope.season}:{team_id}",
                            "provider_competition_id": str(scope.league.api_football_league_id),
                            "league": scope.league.name,
                            "country": scope.league.country,
                            "season": scope.season,
                            "team_id": team_id,
                            "team_name": team_name,
                            "position": self._int(standing.get("rank")),
                            "position_raw": str(standing.get("rank") or "").strip(),
                            "movement_status": str(standing.get("status") or "").strip(),
                            "recent_form": str(standing.get("form") or "").strip(),
                            "overall": self._api_football_scope_stats(standing.get("all") or {}),
                            "home": self._api_football_scope_stats(standing.get("home") or {}),
                            "away": self._api_football_scope_stats(standing.get("away") or {}),
                            "goal_difference": self._int(standing.get("goalsDiff")),
                            "goal_difference_raw": str(standing.get("goalsDiff") or "").strip(),
                            "points": self._int(standing.get("points")),
                            "description": str(standing.get("description") or "").strip(),
                            "raw": standing,
                        }
                    )
        return rows

    @classmethod
    def _api_football_scope_stats(cls, stats: dict[str, Any]) -> dict[str, Any]:
        goals = stats.get("goals") if isinstance(stats.get("goals"), dict) else {}
        return {
            "games_played": cls._int(stats.get("played")),
            "wins": cls._int(stats.get("win")),
            "draws": cls._int(stats.get("draw")),
            "losses": cls._int(stats.get("lose")),
            "goals_scored": cls._api_football_goal_total(goals.get("for")),
            "goals_allowed": cls._api_football_goal_total(goals.get("against")),
        }

    @classmethod
    def _api_football_goal_total(cls, value: Any) -> float | None:
        if isinstance(value, dict):
            return cls._num(value.get("total"))
        return cls._num(value)

    @staticmethod
    def _team_league_stats_row(
        payload: dict[str, Any],
        *,
        provider_league_id: str,
        season: str,
    ) -> dict[str, Any]:
        rows = payload.get("league_stats") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("league_id") or "") == str(provider_league_id) and str(row.get("season") or "") == str(season):
                return row
        for row in rows:
            if isinstance(row, dict) and str(row.get("season") or "") == str(season):
                return row
        for row in rows:
            if isinstance(row, dict) and str(row.get("league_id") or "") == str(provider_league_id):
                return row
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    @staticmethod
    def _phase_total(phase: dict[str, Any], key: str, matches_played: int) -> float | None:
        value = ((phase.get(key) or {}).get("total") if isinstance(phase.get(key), dict) else None)
        number = HistoricalTeamHydrator._num(value)
        if number is None:
            return None
        if matches_played and number > matches_played * 2:
            return round(number / matches_played, 3)
        return number

    @staticmethod
    def _rate(phase: dict[str, Any], key: str, matches_played: int) -> float | None:
        value = ((phase.get(key) or {}).get("total") if isinstance(phase.get(key), dict) else None)
        number = HistoricalTeamHydrator._num(value)
        if number is None:
            return None
        if matches_played and number > 1:
            return round(number / matches_played, 4)
        return number

    @staticmethod
    def _data_quality(matches_played: int, has_team_stats: bool, has_error: bool) -> str:
        if has_error and not has_team_stats:
            return TeamSeasonProfile.DataQuality.LIMITED if matches_played else TeamSeasonProfile.DataQuality.MISSING
        if matches_played >= 20 and has_team_stats:
            return TeamSeasonProfile.DataQuality.STRONG
        if matches_played >= 10:
            return TeamSeasonProfile.DataQuality.MEDIUM
        if matches_played > 0:
            return TeamSeasonProfile.DataQuality.LIMITED
        return TeamSeasonProfile.DataQuality.MISSING

    @staticmethod
    def _api_football_season(season: str) -> str:
        return str(season or "").replace("/", "-").split("-", 1)[0]

    def _save_coverage(self, profile: TeamSeasonProfile, *, team_error: str = "", provider: str = "statpal") -> None:
        now = timezone.now()
        status = DataCoverage.Status.PARTIAL if team_error else DataCoverage.Status.FRESH
        DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key=f"{profile.team.canonical_normalized}:{profile.league_key}:{profile.season}",
            provider=provider,
            coverage_key=HistoricalTeamHydrator.COVERAGE_KEY,
            defaults={
                "team": profile.team,
                "league_key": profile.league_key,
                "league_name": profile.league_name,
                "season": profile.season,
                "status": status,
                "available_requirements": self._coverage_requirements(provider, team_error=team_error),
                "missing_requirements": ["team_stats"] if team_error and provider == "statpal" else [],
                "last_attempted_at": now,
                "last_success_at": now,
                "error": team_error[:1000],
                "metadata": {"team_season_profile_id": profile.pk, "provider": provider},
            },
        )

    @staticmethod
    def _coverage_requirements(provider: str, *, team_error: str) -> list[str]:
        if provider == "api_football":
            return ["standings"]
        return ["standings"] if team_error else ["standings", "team_stats"]

    def _scope_failed(
        self,
        scope: HistoricalHydrationScope,
        error: str,
        *,
        provider: str = "statpal",
        provider_league_id: str = "",
        errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.LEAGUE,
            subject_key=f"{scope.league.key}:{scope.season}",
            provider=provider,
            coverage_key=self.COVERAGE_KEY,
            defaults={
                "league_key": scope.league.key,
                "league_name": scope.league.name,
                "season": scope.season,
                "status": DataCoverage.Status.FAILED,
                "available_requirements": [],
                "missing_requirements": ["standings"],
                "last_attempted_at": timezone.now(),
                "error": error[:1000],
                "metadata": {
                    "provider_league_id": provider_league_id
                    or scope.league.statpal_league_id
                    or scope.league.api_football_league_id,
                    "provider": provider,
                },
            },
        )
        reported_errors = list(errors or [])
        reported_errors.append({"league_key": scope.league.key, "error": error[:300]})
        return {
            "status": "failed",
            "league_key": scope.league.key,
            "league_name": scope.league.name,
            "season": scope.season,
            "teams_considered": 0,
            "profiles_saved": 0,
            "coverage_fresh": 0,
            "coverage_failed": 1,
            "provider": provider,
            "provider_league_id": provider_league_id,
            "errors": reported_errors[:25],
        }

    @staticmethod
    def _compact_team_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not payload:
            return {}
        return {
            key: value
            for key, value in payload.items()
            if key not in {"raw", "squad", "transfers"}
        }

    @staticmethod
    def _int(value) -> int:
        try:
            if value in (None, ""):
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _num(value) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
