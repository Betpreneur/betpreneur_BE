from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.domain.league_registry import (
    IntelligenceLeague,
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.models import (
    DataCoverage,
    FixtureCache,
    TeamProfile,
    TeamRecentFormProfile,
)
from betpreneur.modules.catalog.services.legacy_runner import aps_get
from betpreneur.modules.catalog.services.provider_client import (
    StatPalClient,
    StatPalConfigurationError,
    StatPalError,
    statpal_client,
)
from betpreneur.modules.catalog.services.search import FixtureSearchService
from betpreneur.modules.catalog.services.statpal_normalize import normalize_daily_matches
from betpreneur.platform.db.json import json_safe

DEFAULT_RECENT_FORM_WINDOWS = (5, 10, 15)


@dataclass(frozen=True)
class RecentFormScope:
    league: IntelligenceLeague
    season: str


class RecentFormBuilder:
    """Build rolling recent-form profiles from cached historical fixtures."""

    COVERAGE_KEY = "recent_form"
    FINAL_STATUSES = {"finished", "ft", "after extra time", "after penalties", "ended", "complete", "completed"}

    def __init__(
        self,
        *,
        client: StatPalClient | None = None,
        fixture_service: FixtureSearchService | None = None,
    ):
        self.client = client or statpal_client()
        self.fixture_service = fixture_service or FixtureSearchService()

    def build(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        seasons: list[str] | tuple[str, ...] | None = None,
        windows: list[int] | tuple[int, ...] = DEFAULT_RECENT_FORM_WINDOWS,
        sync_matches: bool = True,
        max_matches: int | None = None,
    ) -> dict[str, Any]:
        scopes = self._scopes(league_keys=league_keys, seasons=seasons)
        results = [
            self.build_scope(
                scope,
                windows=windows,
                sync_matches=sync_matches,
                max_matches=max_matches,
            )
            for scope in scopes
        ]
        return {
            "provider": "statpal",
            "scopes": len(results),
            "leagues": len({item["league_key"] for item in results}),
            "seasons": sorted({item["season"] for item in results}),
            "matches_synced": sum(item.get("matches_synced", 0) for item in results),
            "teams_considered": sum(item.get("teams_considered", 0) for item in results),
            "profiles_saved": sum(item.get("profiles_saved", 0) for item in results),
            "coverage_fresh": sum(item.get("coverage_fresh", 0) for item in results),
            "coverage_partial": sum(item.get("coverage_partial", 0) for item in results),
            "skipped": sum(1 for item in results if item.get("status") == "skipped"),
            "results": results,
        }

    def build_scope(
        self,
        scope: RecentFormScope,
        *,
        windows: list[int] | tuple[int, ...] = DEFAULT_RECENT_FORM_WINDOWS,
        sync_matches: bool = True,
        max_matches: int | None = None,
    ) -> dict[str, Any]:
        league = scope.league
        provider_league_id = self._provider_league_id(league)
        if not provider_league_id:
            return self._skipped(scope, "missing_provider_league_id")

        errors = []
        synced = 0
        if sync_matches:
            try:
                synced = self.sync_league_matches(scope, max_matches=max_matches)
            except (StatPalConfigurationError, StatPalError) as exc:
                errors.append({"phase": "sync_matches", "error": str(exc)[:300]})

        fixtures = list(self._completed_fixtures(scope))
        teams = list(self._teams_for_scope(scope))
        profiles_saved = 0
        coverage_fresh = 0
        coverage_partial = 0
        for team in teams:
            team_rows = self._team_rows(team, fixtures)
            for window in self._windows(windows):
                for form_scope in (
                    TeamRecentFormProfile.Scope.ALL,
                    TeamRecentFormProfile.Scope.HOME,
                    TeamRecentFormProfile.Scope.AWAY,
                ):
                    profile = self.save_profile(
                        team=team,
                        league=league,
                        season=scope.season,
                        window=window,
                        scope=form_scope,
                        rows=team_rows[form_scope],
                    )
                    profiles_saved += 1
                    if profile.matches >= min(window, 3):
                        coverage_fresh += 1
                    else:
                        coverage_partial += 1

        return {
            "status": "partial" if errors else "complete",
            "league_key": league.key,
            "league_name": league.name,
            "season": scope.season,
            "provider_league_id": provider_league_id,
            "matches_synced": synced,
            "completed_matches": len(fixtures),
            "teams_considered": len(teams),
            "profiles_saved": profiles_saved,
            "coverage_fresh": coverage_fresh,
            "coverage_partial": coverage_partial,
            "errors": errors,
        }

    def sync_league_matches(self, scope: RecentFormScope, *, max_matches: int | None = None) -> int:
        if scope.league.statpal_league_id:
            payload = self.client.soccer_league_matches(
                scope.league.statpal_league_id,
                params=self._statpal_season_params(scope),
            )
            fixtures = normalize_daily_matches(payload, target_date=timezone.localdate())
            provider_league_id = str(scope.league.statpal_league_id)
            filtered = [
                fixture
                for fixture in fixtures
                if str(fixture.get("provider_competition_id") or fixture.get("code") or "") == provider_league_id
            ]
        else:
            filtered = self._api_football_fixture_rows(scope)
        if max_matches is not None:
            filtered = filtered[: max(0, int(max_matches))]

        synced = 0
        by_date: dict[Any, list[dict[str, Any]]] = {}
        for fixture in filtered:
            by_date.setdefault(fixture.get("date") or timezone.localdate(), []).append(fixture)
        for target_date, rows in by_date.items():
            synced += self.fixture_service._upsert_fixtures(rows, target_date)
        return synced

    def save_profile(
        self,
        *,
        team: TeamProfile,
        league: IntelligenceLeague,
        season: str,
        window: int,
        scope: str,
        rows: list[dict[str, Any]],
    ) -> TeamRecentFormProfile:
        selected = rows[:window]
        summary = self._summary(selected)
        profile, _ = TeamRecentFormProfile.objects.update_or_create(
            team=team,
            league_key=league.key,
            season=season,
            window=window,
            scope=scope,
            defaults={
                "league_name": league.name,
                "matches": summary["matches"],
                "wins": summary["wins"],
                "draws": summary["draws"],
                "losses": summary["losses"],
                "goals_for": summary["goals_for"],
                "goals_against": summary["goals_against"],
                "xg_for": summary["xg_for"],
                "xg_against": summary["xg_against"],
                "corners_for": summary["corners_for"],
                "corners_against": summary["corners_against"],
                "cards_for": summary["cards_for"],
                "cards_against": summary["cards_against"],
                "shots_on_target_for": summary["shots_on_target_for"],
                "shots_on_target_against": summary["shots_on_target_against"],
                "form": summary["form"],
                "stats": json_safe({"window": window, "scope": scope, "fixtures": selected}),
                "source": "statpal",
                "computed_at": timezone.now(),
            },
        )
        self._save_coverage(profile)
        return profile

    def _completed_fixtures(self, scope: RecentFormScope):
        provider_ids = self._provider_league_ids(scope.league)
        payload_filter = Q()
        for provider_id in provider_ids:
            payload_filter |= Q(api_payload__provider_competition_id=str(provider_id))
            payload_filter |= Q(api_payload__code=str(provider_id))
            payload_filter |= Q(api_payload__league_id=str(provider_id))
        return (
            FixtureCache.objects.filter(source__in=["statpal", "api_football", "aps_provider_lookup"])
            .filter(payload_filter)
            .order_by("-match_date", "-kickoff_utc", "-updated_at")
        )

    @staticmethod
    def _teams_for_scope(scope: RecentFormScope):
        return TeamProfile.objects.filter(
            active=True,
            primary_league_key=scope.league.key,
            season_profiles__league_key=scope.league.key,
            season_profiles__season=scope.season,
        ).distinct()

    def _team_rows(self, team: TeamProfile, fixtures: list[FixtureCache]) -> dict[str, list[dict[str, Any]]]:
        provider_ids = self._team_provider_ids(team)
        statpal_identity = (team.provider_ids or {}).get("statpal") or {}
        statpal_id = str(
            (statpal_identity.get("team_id") if isinstance(statpal_identity, dict) else statpal_identity)
            or ""
        )
        names = {team.canonical_normalized, *(self._normalized_aliases(team))}
        rows = {
            TeamRecentFormProfile.Scope.ALL: [],
            TeamRecentFormProfile.Scope.HOME: [],
            TeamRecentFormProfile.Scope.AWAY: [],
        }
        for fixture in fixtures:
            occurrence = self._fixture_occurrence(fixture, team_ids=provider_ids | ({statpal_id} if statpal_id else set()), names=names)
            if not occurrence:
                continue
            rows[TeamRecentFormProfile.Scope.ALL].append(occurrence)
            rows[occurrence["scope"]].append(occurrence)
        return rows

    @staticmethod
    def _fixture_occurrence(fixture: FixtureCache, *, team_ids: set[str], names: set[str]) -> dict[str, Any] | None:
        payload = fixture.api_payload or {}
        home_id = str(payload.get("provider_home_team_id") or payload.get("hid") or "")
        away_id = str(payload.get("provider_away_team_id") or payload.get("aid") or "")
        home_match = bool(home_id and home_id in team_ids) or fixture.home_team_normalized in names
        away_match = bool(away_id and away_id in team_ids) or fixture.away_team_normalized in names
        if not home_match and not away_match:
            return None
        if not RecentFormBuilder._is_final(payload):
            return None
        home_goals = RecentFormBuilder._score(payload.get("home_goals"), payload.get("ft_home_goals"))
        away_goals = RecentFormBuilder._score(payload.get("away_goals"), payload.get("ft_away_goals"))
        if home_goals is None or away_goals is None:
            return None

        is_home = home_match
        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals
        result = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
        side = "home" if is_home else "away"
        opponent = fixture.away_team if is_home else fixture.home_team
        return {
            "match_id": fixture.match_id,
            "match_date": fixture.match_date.isoformat() if fixture.match_date else "",
            "fixture": fixture.fixture,
            "scope": side,
            "opponent": opponent,
            "result": result,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "xg_for": RecentFormBuilder._side_metric(payload, side, "expected_goals"),
            "xg_against": RecentFormBuilder._side_metric(payload, "away" if is_home else "home", "expected_goals"),
            "corners_for": RecentFormBuilder._side_metric(payload, side, "corners"),
            "corners_against": RecentFormBuilder._side_metric(payload, "away" if is_home else "home", "corners"),
            "cards_for": RecentFormBuilder._side_metric(payload, side, "yellowcards"),
            "cards_against": RecentFormBuilder._side_metric(payload, "away" if is_home else "home", "yellowcards"),
            "shots_on_target_for": RecentFormBuilder._side_metric(payload, side, "shots_on_goal"),
            "shots_on_target_against": RecentFormBuilder._side_metric(payload, "away" if is_home else "home", "shots_on_goal"),
        }

    @classmethod
    def _summary(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        matches = len(rows)
        return {
            "matches": matches,
            "wins": sum(1 for row in rows if row["result"] == "W"),
            "draws": sum(1 for row in rows if row["result"] == "D"),
            "losses": sum(1 for row in rows if row["result"] == "L"),
            "goals_for": cls._avg(rows, "goals_for"),
            "goals_against": cls._avg(rows, "goals_against"),
            "xg_for": cls._avg(rows, "xg_for"),
            "xg_against": cls._avg(rows, "xg_against"),
            "corners_for": cls._avg(rows, "corners_for"),
            "corners_against": cls._avg(rows, "corners_against"),
            "cards_for": cls._avg(rows, "cards_for"),
            "cards_against": cls._avg(rows, "cards_against"),
            "shots_on_target_for": cls._avg(rows, "shots_on_target_for"),
            "shots_on_target_against": cls._avg(rows, "shots_on_target_against"),
            "form": [row["result"] for row in rows],
        }

    @staticmethod
    def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
        values = [float(row[key]) for row in rows if row.get(key) not in (None, "")]
        if not values:
            return None
        return round(sum(values) / len(values), 3)

    @staticmethod
    def _is_final(payload: dict[str, Any]) -> bool:
        status = str(payload.get("status") or "").strip().lower()
        return status in RecentFormBuilder.FINAL_STATUSES or (
            payload.get("home_goals") not in (None, "") and payload.get("away_goals") not in (None, "")
        )

    @staticmethod
    def _score(*values) -> int | None:
        for value in values:
            try:
                if value in (None, ""):
                    continue
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _side_metric(payload: dict[str, Any], side: str, metric: str) -> float | None:
        team_stats = payload.get("team_stats") if isinstance(payload.get("team_stats"), dict) else {}
        value = None
        side_stats = team_stats.get(side) if isinstance(team_stats.get(side), dict) else {}
        metric_stats = side_stats.get(metric) if isinstance(side_stats.get(metric), dict) else {}
        if metric_stats:
            value = metric_stats.get("total")
        if value in (None, ""):
            value = payload.get(f"{side}_{metric}") or payload.get(f"{metric}_{side}")
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalized_aliases(team: TeamProfile) -> set[str]:
        from betpreneur.modules.catalog.domain.text import normalize_fixture_text

        return {normalize_fixture_text(alias) for alias in (team.aliases or []) if normalize_fixture_text(alias)}

    @staticmethod
    def _team_provider_ids(team: TeamProfile) -> set[str]:
        values = set()
        for identity in (team.provider_ids or {}).values():
            if isinstance(identity, dict):
                values.update(str(value).strip() for key, value in identity.items() if key.endswith("team_id") or key == "team_id")
            else:
                values.add(str(identity).strip())
        return values - {""}

    @staticmethod
    def _provider_league_id(league: IntelligenceLeague) -> str:
        return str(league.statpal_league_id or league.api_football_league_id or "").strip()

    @staticmethod
    def _provider_league_ids(league: IntelligenceLeague) -> set[str]:
        return {str(value).strip() for value in (league.statpal_league_id, league.api_football_league_id) if str(value or "").strip()}

    def _api_football_fixture_rows(self, scope: RecentFormScope) -> list[dict[str, Any]]:
        response = aps_get(
            "/fixtures",
            {
                "league": scope.league.api_football_league_id,
                "season": self._api_football_season(scope.season),
            },
            timeout=30,
        )
        rows = []
        for item in response or []:
            if not isinstance(item, dict):
                continue
            fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
            league = item.get("league") if isinstance(item.get("league"), dict) else {}
            teams = item.get("teams") if isinstance(item.get("teams"), dict) else {}
            goals = item.get("goals") if isinstance(item.get("goals"), dict) else {}
            home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
            away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
            fixture_id = fixture.get("id")
            if not fixture_id or not home.get("name") or not away.get("name"):
                continue
            kickoff = fixture.get("date") or ""
            rows.append(
                {
                    "fixture": f"{home.get('name')} vs {away.get('name')}",
                    "hname": home.get("name") or "",
                    "aname": away.get("name") or "",
                    "home_logo": home.get("logo") or "",
                    "away_logo": away.get("logo") or "",
                    "hid": home.get("id"),
                    "aid": away.get("id"),
                    "league": league.get("name") or scope.league.name,
                    "league_logo": league.get("logo") or "",
                    "country": league.get("country") or scope.league.country,
                    "country_flag": league.get("flag") or "",
                    "round": league.get("round") or "",
                    "league_type": league.get("type") or "",
                    "code": str(league.get("id") or scope.league.api_football_league_id),
                    "kickoff": "",
                    "kickoff_utc": kickoff,
                    "match_id": fixture_id,
                    "source": "api_football",
                    "aps_id": fixture_id,
                    "date": (kickoff or "")[:10] or timezone.localdate().isoformat(),
                    "season": league.get("season") or self._api_football_season(scope.season),
                    "status": (fixture.get("status") or {}).get("short") or "",
                    "home_goals": goals.get("home"),
                    "away_goals": goals.get("away"),
                }
            )
        return rows

    @staticmethod
    def _statpal_season_params(scope: RecentFormScope) -> dict[str, str] | None:
        if str(scope.season or "") == str(scope.league.current_season or ""):
            return None
        return {"season": str(scope.season)}

    @staticmethod
    def _api_football_season(season: str) -> str:
        return str(season or "").replace("/", "-").split("-", 1)[0]

    @staticmethod
    def _windows(windows: list[int] | tuple[int, ...]) -> tuple[int, ...]:
        return tuple(sorted({max(1, int(window)) for window in (windows or DEFAULT_RECENT_FORM_WINDOWS)}))

    def _scopes(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None,
        seasons: list[str] | tuple[str, ...] | None,
    ) -> list[RecentFormScope]:
        selected = set(league_keys or [])
        scopes = []
        for league in team_intelligence_leagues(active_only=True):
            if selected and league.key not in selected:
                continue
            league_seasons = seasons or (league.current_season, league.previous_season)
            for season in league_seasons:
                scopes.append(RecentFormScope(league=league, season=str(season)))
        return scopes

    @staticmethod
    def _save_coverage(profile: TeamRecentFormProfile) -> None:
        now = timezone.now()
        status = DataCoverage.Status.FRESH if profile.matches >= min(profile.window, 3) else DataCoverage.Status.PARTIAL
        DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key=(
                f"{profile.team.canonical_normalized}:"
                f"{profile.league_key}:{profile.season}:"
                f"{profile.window}:{profile.scope}"
            ),
            provider="statpal",
            coverage_key=RecentFormBuilder.COVERAGE_KEY,
            defaults={
                "team": profile.team,
                "league_key": profile.league_key,
                "league_name": profile.league_name,
                "season": profile.season,
                "status": status,
                "available_requirements": ["completed_matches"] if profile.matches else [],
                "missing_requirements": [] if profile.matches else ["completed_matches"],
                "last_attempted_at": now,
                "last_success_at": now if profile.matches else None,
                "metadata": {
                    "team_recent_form_profile_id": profile.pk,
                    "window": profile.window,
                    "scope": profile.scope,
                    "matches": profile.matches,
                },
            },
        )

    @staticmethod
    def _skipped(scope: RecentFormScope, reason: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": reason,
            "league_key": scope.league.key,
            "league_name": scope.league.name,
            "season": scope.season,
            "matches_synced": 0,
            "completed_matches": 0,
            "teams_considered": 0,
            "profiles_saved": 0,
            "coverage_fresh": 0,
            "coverage_partial": 0,
            "errors": [],
        }
