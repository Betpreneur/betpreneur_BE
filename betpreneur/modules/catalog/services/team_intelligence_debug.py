from __future__ import annotations

from typing import Any

from django.db.models import Max, Q

from betpreneur.modules.catalog.domain.text import normalize_fixture_text
from betpreneur.modules.catalog.models import (
    DataCoverage,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.platform.db.json import json_safe


class TeamIntelligenceDebugService:
    """Compact internal view of stored intelligence freshness for a team."""

    def inspect_team(self, *, team_id: int | None = None, query: str = "", limit: int = 10) -> dict[str, Any]:
        teams = self._teams(team_id=team_id, query=query, limit=limit)
        return json_safe(
            {
                "query": query,
                "team_id": team_id,
                "count": len(teams),
                "teams": [self.team_summary(team) for team in teams],
            }
        )

    def team_summary(self, team: TeamProfile) -> dict[str, Any]:
        coverage_rows = list(DataCoverage.objects.filter(team=team).order_by("league_key", "season", "coverage_key"))
        season_profiles = list(team.season_profiles.order_by("-season", "league_name")[:8])
        recent_profiles = list(team.recent_form_profiles.order_by("-computed_at", "-updated_at")[:12])
        market_profiles = list(team.market_profiles.order_by("-computed_at", "-updated_at")[:20])
        latest_refresh = self._latest_refresh(team, coverage_rows, season_profiles, recent_profiles, market_profiles)
        league_priors = self._league_priors_for_team(team, season_profiles)

        return {
            "id": team.pk,
            "canonical_name": team.canonical_name,
            "canonical_normalized": team.canonical_normalized,
            "country": team.country,
            "primary_league_key": team.primary_league_key,
            "primary_league_name": team.primary_league_name,
            "active": team.active,
            "provider_ids": team.provider_ids,
            "aliases": team.aliases,
            "coverage": [self._coverage_payload(row) for row in coverage_rows],
            "coverage_summary": self._coverage_summary(coverage_rows),
            "profiles": {
                "season": [self._season_payload(profile) for profile in season_profiles],
                "recent_form": [self._recent_payload(profile) for profile in recent_profiles],
                "markets": [self._market_payload(profile) for profile in market_profiles],
                "league_priors": [self._league_market_payload(profile) for profile in league_priors],
            },
            "profile_counts": {
                "season": team.season_profiles.count(),
                "recent_form": team.recent_form_profiles.count(),
                "markets": team.market_profiles.count(),
                "league_priors": len(league_priors),
            },
            "last_refresh": latest_refresh,
            "updated_at": team.updated_at,
        }

    @staticmethod
    def _teams(*, team_id: int | None, query: str, limit: int):
        qs = TeamProfile.objects.filter(active=True)
        if team_id is not None:
            return list(qs.filter(pk=team_id)[:1])
        query = query.strip()
        if query:
            normalized = normalize_fixture_text(query)
            qs = qs.filter(
                Q(canonical_name__icontains=query)
                | Q(canonical_normalized__icontains=normalized)
                | Q(aliases__contains=[query])
            )
        return list(qs.order_by("canonical_name")[:limit])

    @staticmethod
    def _latest_refresh(team, coverage_rows, season_profiles, recent_profiles, market_profiles):
        timestamps = [team.updated_at]
        timestamps.extend(row.last_success_at or row.updated_at for row in coverage_rows)
        timestamps.extend(profile.fetched_at or profile.computed_at or profile.updated_at for profile in season_profiles)
        timestamps.extend(profile.computed_at or profile.updated_at for profile in recent_profiles)
        timestamps.extend(profile.computed_at or profile.updated_at for profile in market_profiles)
        timestamps = [value for value in timestamps if value]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _coverage_summary(rows):
        counts = {status: 0 for status, _label in DataCoverage.Status.choices}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        if counts.get(DataCoverage.Status.FAILED):
            status = DataCoverage.Status.FAILED
        elif counts.get(DataCoverage.Status.STALE):
            status = DataCoverage.Status.STALE
        elif counts.get(DataCoverage.Status.MISSING):
            status = DataCoverage.Status.MISSING
        elif counts.get(DataCoverage.Status.PARTIAL):
            status = DataCoverage.Status.PARTIAL
        elif rows:
            status = DataCoverage.Status.FRESH
        else:
            status = DataCoverage.Status.MISSING
        return {
            "status": status,
            "counts": counts,
            "last_success_at": max((row.last_success_at for row in rows if row.last_success_at), default=None),
            "expires_at": min((row.expires_at for row in rows if row.expires_at), default=None),
        }

    @staticmethod
    def _coverage_payload(row: DataCoverage) -> dict[str, Any]:
        return {
            "subject_type": row.subject_type,
            "subject_key": row.subject_key,
            "provider": row.provider,
            "coverage_key": row.coverage_key,
            "league_key": row.league_key,
            "league_name": row.league_name,
            "season": row.season,
            "status": row.status,
            "confidence": row.metadata.get("confidence") if isinstance(row.metadata, dict) else None,
            "freshness_seconds": row.freshness_seconds,
            "available_requirements": row.available_requirements,
            "missing_requirements": row.missing_requirements,
            "last_success_at": row.last_success_at,
            "expires_at": row.expires_at,
            "error": row.error,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _season_payload(profile: TeamSeasonProfile) -> dict[str, Any]:
        return {
            "league_key": profile.league_key,
            "league_name": profile.league_name,
            "season": profile.season,
            "matches_played": profile.matches_played,
            "home_matches": profile.home_matches,
            "away_matches": profile.away_matches,
            "goals_for": profile.goals_for,
            "goals_against": profile.goals_against,
            "xg_for": profile.xg_for,
            "xg_against": profile.xg_against,
            "corners_for": profile.corners_for,
            "corners_against": profile.corners_against,
            "cards_for": profile.cards_for,
            "cards_against": profile.cards_against,
            "shots_on_target_for": profile.shots_on_target_for,
            "shots_on_target_against": profile.shots_on_target_against,
            "data_quality": profile.data_quality,
            "source": profile.source,
            "fetched_at": profile.fetched_at,
            "computed_at": profile.computed_at,
            "updated_at": profile.updated_at,
        }

    @staticmethod
    def _recent_payload(profile: TeamRecentFormProfile) -> dict[str, Any]:
        return {
            "league_key": profile.league_key,
            "league_name": profile.league_name,
            "season": profile.season,
            "window": profile.window,
            "scope": profile.scope,
            "matches": profile.matches,
            "wins": profile.wins,
            "draws": profile.draws,
            "losses": profile.losses,
            "goals_for": profile.goals_for,
            "goals_against": profile.goals_against,
            "xg_for": profile.xg_for,
            "xg_against": profile.xg_against,
            "computed_at": profile.computed_at,
            "updated_at": profile.updated_at,
        }

    @staticmethod
    def _market_payload(profile: TeamMarketProfile) -> dict[str, Any]:
        return {
            "league_key": profile.league_key,
            "league_name": profile.league_name,
            "season": profile.season,
            "market_family": profile.market_family,
            "market": profile.market,
            "scope": profile.scope,
            "side": profile.side,
            "line": profile.line,
            "attempts": profile.attempts,
            "hit_rate": profile.hit_rate,
            "confidence": profile.confidence,
            "roi_flat": profile.roi_flat,
            "data_quality": profile.data_quality,
            "computed_at": profile.computed_at,
            "updated_at": profile.updated_at,
        }

    @staticmethod
    def _league_market_payload(profile: LeagueMarketProfile) -> dict[str, Any]:
        return {
            "league_key": profile.league_key,
            "league_name": profile.league_name,
            "season": profile.season,
            "market_family": profile.market_family,
            "market": profile.market,
            "line": profile.line,
            "attempts": profile.attempts,
            "hit_rate": profile.hit_rate,
            "confidence": profile.confidence,
            "fairness_score": profile.fairness_score,
            "volatility": profile.volatility,
            "data_quality": profile.data_quality,
            "computed_at": profile.computed_at,
            "updated_at": profile.updated_at,
        }

    @staticmethod
    def _league_priors_for_team(team: TeamProfile, season_profiles: list[TeamSeasonProfile]):
        pairs = {(profile.league_key, profile.season) for profile in season_profiles}
        if team.primary_league_key:
            latest_season = (
                TeamSeasonProfile.objects.filter(team=team, league_key=team.primary_league_key)
                .aggregate(value=Max("season"))
                .get("value")
            )
            if latest_season:
                pairs.add((team.primary_league_key, latest_season))
        if not pairs:
            return []
        query = Q()
        for league_key, season in pairs:
            query |= Q(league_key=league_key, season=season)
        return list(LeagueMarketProfile.objects.filter(query).order_by("-computed_at", "-attempts")[:20])


team_intelligence_debug_service = TeamIntelligenceDebugService()
