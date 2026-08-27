from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean
from typing import Any

from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.domain.league_registry import (
    IntelligenceLeague,
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.models import (
    DataCoverage,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.platform.db.json import json_safe

REQUIRED_RECENT_FORM_WINDOWS = (5, 10, 15)
REQUIRED_RECENT_FORM_SCOPES = (
    TeamRecentFormProfile.Scope.ALL,
    TeamRecentFormProfile.Scope.HOME,
    TeamRecentFormProfile.Scope.AWAY,
)
REQUIRED_MARKET_FAMILIES = (
    "match_result",
    "double_chance",
    "draw_no_bet",
    "total_goals",
    "both_teams_to_score",
    "team_total_goals",
    "corners_total",
    "cards_total",
    "shots_on_target_total",
)


@dataclass(frozen=True)
class DataCoverageScope:
    league: IntelligenceLeague
    season: str


class DataCoverageTracker:
    """Summarise team-intelligence freshness, gaps and trust into coverage rows."""

    TEAM_COVERAGE_KEY = "team_intelligence_readiness"
    LEAGUE_COVERAGE_KEY = "league_intelligence_readiness"
    PROVIDER = "derived"

    def refresh(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        seasons: list[str] | tuple[str, ...] | None = None,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        scopes = self._scopes(league_keys=league_keys, seasons=seasons)
        results = [self.refresh_scope(scope, ttl_hours=ttl_hours) for scope in scopes]
        return {
            "provider": self.PROVIDER,
            "coverage_key": self.TEAM_COVERAGE_KEY,
            "scopes": len(results),
            "leagues": len({item["league_key"] for item in results}),
            "seasons": sorted({item["season"] for item in results}),
            "teams_checked": sum(item.get("teams_checked", 0) for item in results),
            "fresh": sum(item.get("fresh", 0) for item in results),
            "partial": sum(item.get("partial", 0) for item in results),
            "missing": sum(item.get("missing", 0) for item in results),
            "stale": sum(item.get("stale", 0) for item in results),
            "skipped": sum(1 for item in results if item.get("status") == "skipped"),
            "results": results,
        }

    def refresh_scope(self, scope: DataCoverageScope, *, ttl_hours: int = 24) -> dict[str, Any]:
        teams = list(self._teams_for_scope(scope))
        ttl = timedelta(hours=max(int(ttl_hours or 24), 1))
        rows = [self.refresh_team(team, scope, ttl=ttl) for team in teams]
        league_row = self.refresh_league(scope, rows, ttl=ttl)
        counts = self._status_counts(rows)
        return {
            "league_key": scope.league.key,
            "league_name": scope.league.name,
            "season": scope.season,
            "status": league_row.status,
            "teams_checked": len(rows),
            "fresh": counts.get(DataCoverage.Status.FRESH, 0),
            "partial": counts.get(DataCoverage.Status.PARTIAL, 0),
            "missing": counts.get(DataCoverage.Status.MISSING, 0),
            "stale": counts.get(DataCoverage.Status.STALE, 0),
            "average_confidence": league_row.metadata.get("average_confidence"),
        }

    def refresh_team(self, team: TeamProfile, scope: DataCoverageScope, *, ttl: timedelta) -> DataCoverage:
        now = timezone.now()
        available: list[str] = []
        missing: list[str] = []

        identity_score = self._identity_score(team, scope, available, missing)
        season_profile = self._season_profile(team, scope)
        season_score = self._season_score(season_profile, available, missing)
        recent_profiles = list(self._recent_profiles(team, scope))
        recent_score = self._recent_score(recent_profiles, available, missing)
        market_profiles = list(self._market_profiles(team, scope))
        market_score = self._market_score(market_profiles, available, missing)

        latest_at = self._latest_timestamp(
            [team.updated_at],
            [season_profile] if season_profile else [],
            recent_profiles,
            market_profiles,
        )
        freshness_seconds = self._freshness_seconds(latest_at, now)
        confidence = round(mean([identity_score, season_score, recent_score, market_score]), 1)
        source_quality = self._quality_label(confidence, missing)
        status = self._status(available, missing, latest_at, now, ttl)

        coverage, _ = DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key=self._team_subject_key(team, scope),
            provider=self.PROVIDER,
            coverage_key=self.TEAM_COVERAGE_KEY,
            defaults={
                "team": team,
                "league_key": scope.league.key,
                "league_name": scope.league.name,
                "season": scope.season,
                "status": status,
                "freshness_seconds": freshness_seconds,
                "available_requirements": available,
                "missing_requirements": missing,
                "last_attempted_at": now,
                "last_success_at": latest_at,
                "expires_at": now + ttl if latest_at else None,
                "error": "",
                "metadata": json_safe(
                    {
                        "confidence": confidence,
                        "source_quality": source_quality,
                        "component_scores": {
                            "identity": identity_score,
                            "season_profile": season_score,
                            "recent_form": recent_score,
                            "market_profiles": market_score,
                        },
                        "profile_ids": {
                            "season_profile": season_profile.pk if season_profile else None,
                            "recent_form": [profile.pk for profile in recent_profiles],
                            "market_profiles": [profile.pk for profile in market_profiles],
                        },
                        "market_families": sorted({profile.market_family for profile in market_profiles}),
                        "latest_profile_at": latest_at.isoformat() if latest_at else None,
                    }
                ),
            },
        )
        return coverage

    def refresh_league(
        self,
        scope: DataCoverageScope,
        team_rows: list[DataCoverage],
        *,
        ttl: timedelta,
    ) -> DataCoverage:
        now = timezone.now()
        counts = self._status_counts(team_rows)
        confidences = [
            float(row.metadata.get("confidence"))
            for row in team_rows
            if isinstance(row.metadata, dict) and row.metadata.get("confidence") is not None
        ]
        league_markets = list(self._league_market_profiles(scope))
        market_families = sorted({profile.market_family for profile in league_markets})
        missing = []
        available = []
        if team_rows:
            available.append("teams")
        else:
            missing.append("teams")
        if market_families:
            available.append("league_market_profiles")
        else:
            missing.append("league_market_profiles")

        latest_at = self._latest_timestamp([], [], [], league_markets + team_rows)
        freshness_seconds = self._freshness_seconds(latest_at, now)
        status = self._league_status(team_rows, missing, latest_at, now, ttl)
        coverage, _ = DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.LEAGUE,
            subject_key=f"{scope.league.key}:{scope.season}",
            provider=self.PROVIDER,
            coverage_key=self.LEAGUE_COVERAGE_KEY,
            defaults={
                "team": None,
                "league_key": scope.league.key,
                "league_name": scope.league.name,
                "season": scope.season,
                "status": status,
                "freshness_seconds": freshness_seconds,
                "available_requirements": available,
                "missing_requirements": missing,
                "last_attempted_at": now,
                "last_success_at": latest_at,
                "expires_at": now + ttl if latest_at else None,
                "error": "",
                "metadata": json_safe(
                    {
                        "teams_checked": len(team_rows),
                        "status_counts": counts,
                        "average_confidence": round(mean(confidences), 1) if confidences else None,
                        "market_families": market_families,
                    }
                ),
            },
        )
        return coverage

    def _scopes(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None,
        seasons: list[str] | tuple[str, ...] | None,
    ) -> list[DataCoverageScope]:
        wanted_keys = {str(key) for key in league_keys or []}
        scopes = []
        for league in team_intelligence_leagues():
            if wanted_keys and league.key not in wanted_keys:
                continue
            scope_seasons = tuple(seasons or (league.current_season,))
            for season in scope_seasons:
                scopes.append(DataCoverageScope(league=league, season=str(season)))
        return scopes

    @staticmethod
    def _teams_for_scope(scope: DataCoverageScope):
        return (
            TeamProfile.objects.filter(active=True)
            .filter(
                Q(primary_league_key=scope.league.key)
                | Q(season_profiles__league_key=scope.league.key, season_profiles__season=scope.season)
            )
            .distinct()
            .order_by("canonical_name")
        )

    @staticmethod
    def _season_profile(team: TeamProfile, scope: DataCoverageScope) -> TeamSeasonProfile | None:
        return (
            TeamSeasonProfile.objects.filter(team=team, league_key=scope.league.key, season=scope.season)
            .order_by("-updated_at")
            .first()
        )

    @staticmethod
    def _recent_profiles(team: TeamProfile, scope: DataCoverageScope):
        return TeamRecentFormProfile.objects.filter(
            team=team,
            league_key=scope.league.key,
            season=scope.season,
            window__in=REQUIRED_RECENT_FORM_WINDOWS,
            scope__in=REQUIRED_RECENT_FORM_SCOPES,
        )

    @staticmethod
    def _market_profiles(team: TeamProfile, scope: DataCoverageScope):
        return TeamMarketProfile.objects.filter(
            team=team,
            league_key=scope.league.key,
            season=scope.season,
            attempts__gt=0,
        )

    @staticmethod
    def _league_market_profiles(scope: DataCoverageScope):
        return LeagueMarketProfile.objects.filter(
            league_key=scope.league.key,
            season=scope.season,
            attempts__gt=0,
        )

    @staticmethod
    def _identity_score(team: TeamProfile, scope: DataCoverageScope, available: list[str], missing: list[str]) -> float:
        provider_ids = team.provider_ids if isinstance(team.provider_ids, dict) else {}
        statpal_ids = provider_ids.get("statpal") if isinstance(provider_ids.get("statpal"), dict) else {}
        if team.canonical_normalized:
            available.append("team_identity")
        else:
            missing.append("team_identity")
        if statpal_ids.get("team_id") or provider_ids.get("statpal_team_id"):
            available.append("statpal_team_id")
        else:
            missing.append("statpal_team_id")
        if team.primary_league_key == scope.league.key:
            available.append("primary_league")
        else:
            missing.append("primary_league")
        return round((3 - len([item for item in missing if item in {"team_identity", "statpal_team_id", "primary_league"}])) / 3 * 100, 1)

    @staticmethod
    def _season_score(
        profile: TeamSeasonProfile | None,
        available: list[str],
        missing: list[str],
    ) -> float:
        if not profile:
            missing.append("season_profile")
            return 0.0
        available.append("season_profile")
        quality_scores = {
            TeamSeasonProfile.DataQuality.STRONG: 95.0,
            TeamSeasonProfile.DataQuality.MEDIUM: 78.0,
            TeamSeasonProfile.DataQuality.LIMITED: 58.0,
            TeamSeasonProfile.DataQuality.POOR: 35.0,
            TeamSeasonProfile.DataQuality.MISSING: 0.0,
        }
        if profile.matches_played <= 0:
            missing.append("season_matches")
        else:
            available.append("season_matches")
        return quality_scores.get(profile.data_quality, 35.0)

    @staticmethod
    def _recent_score(
        profiles: list[TeamRecentFormProfile],
        available: list[str],
        missing: list[str],
    ) -> float:
        present = {(profile.window, profile.scope) for profile in profiles if profile.matches > 0}
        required = {(window, scope) for window in REQUIRED_RECENT_FORM_WINDOWS for scope in REQUIRED_RECENT_FORM_SCOPES}
        for window, scope in sorted(required):
            key = f"recent_form_{scope}_{window}"
            if (window, scope) in present:
                available.append(key)
            else:
                missing.append(key)
        return round((len(present & required) / len(required)) * 100, 1)

    @staticmethod
    def _market_score(
        profiles: list[TeamMarketProfile],
        available: list[str],
        missing: list[str],
    ) -> float:
        families = {profile.market_family for profile in profiles if profile.attempts > 0}
        for family in REQUIRED_MARKET_FAMILIES:
            key = f"market_profile_{family}"
            if family in families:
                available.append(key)
            else:
                missing.append(key)
        return round((len(families & set(REQUIRED_MARKET_FAMILIES)) / len(REQUIRED_MARKET_FAMILIES)) * 100, 1)

    @staticmethod
    def _latest_timestamp(
        timestamps: list[Any],
        season_profiles: list[TeamSeasonProfile],
        recent_profiles: list[TeamRecentFormProfile],
        market_profiles: list[Any],
    ):
        values = [value for value in timestamps if value]
        for profile in season_profiles:
            values.extend([profile.computed_at, profile.fetched_at, profile.updated_at])
        for profile in recent_profiles:
            values.extend([profile.computed_at, profile.updated_at])
        for profile in market_profiles:
            values.extend([getattr(profile, "computed_at", None), getattr(profile, "updated_at", None), getattr(profile, "last_success_at", None)])
        values = [value for value in values if value]
        return max(values) if values else None

    @staticmethod
    def _freshness_seconds(latest_at, now) -> int | None:
        if not latest_at:
            return None
        return max(0, int((now - latest_at).total_seconds()))

    @staticmethod
    def _status(available: list[str], missing: list[str], latest_at, now, ttl: timedelta) -> str:
        if not available:
            return DataCoverage.Status.MISSING
        if latest_at and now - latest_at > ttl and not missing:
            return DataCoverage.Status.STALE
        if missing:
            return DataCoverage.Status.PARTIAL
        return DataCoverage.Status.FRESH

    @staticmethod
    def _league_status(team_rows: list[DataCoverage], missing: list[str], latest_at, now, ttl: timedelta) -> str:
        if not team_rows:
            return DataCoverage.Status.MISSING
        statuses = {row.status for row in team_rows}
        if statuses == {DataCoverage.Status.FRESH} and not missing:
            return DataCoverage.Status.STALE if latest_at and now - latest_at > ttl else DataCoverage.Status.FRESH
        if DataCoverage.Status.STALE in statuses and statuses <= {DataCoverage.Status.FRESH, DataCoverage.Status.STALE}:
            return DataCoverage.Status.STALE
        return DataCoverage.Status.PARTIAL

    @staticmethod
    def _quality_label(confidence: float, missing: list[str]) -> str:
        if confidence >= 80 and not missing:
            return "strong"
        if confidence >= 65:
            return "medium"
        if confidence >= 45:
            return "limited"
        if confidence > 0:
            return "poor"
        return "missing"

    @staticmethod
    def _status_counts(rows: list[DataCoverage]) -> dict[str, int]:
        counts = {status: 0 for status, _label in DataCoverage.Status.choices}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    @staticmethod
    def _team_subject_key(team: TeamProfile, scope: DataCoverageScope) -> str:
        return f"{team.canonical_normalized}:{scope.league.key}:{scope.season}"
