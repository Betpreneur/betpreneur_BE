from __future__ import annotations

from typing import Any

from django.db.models import Q

from betpreneur.modules.catalog.domain.league_registry import (
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.domain.text import normalize_fixture_text
from betpreneur.modules.catalog.models import (
    DataCoverage,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.services.coverage_tracker import DataCoverageTracker
from betpreneur.platform.db.json import json_safe


class TeamIntelligenceService:
    """Read stored team/league intelligence for a fixture before live provider calls."""

    def for_fixture(self, fixture: dict[str, Any] | None) -> dict[str, Any]:
        fixture = fixture or {}
        league = self._league_for_fixture(fixture)
        season = str(fixture.get("season") or (league.current_season if league else "") or "")
        league_key = league.key if league else self._league_key_from_profiles(fixture)
        home = self._team_for_side(fixture, "home", league_key=league_key, season=season)
        away = self._team_for_side(fixture, "away", league_key=league_key, season=season)
        league_payload = self._league_payload(league_key=league_key, season=season)

        if not home and not away:
            return {
                "available": False,
                "source": "stored_team_intelligence",
                "status": "missing",
                "league_key": league_key,
                "league_name": (league.name if league else fixture.get("league")) or "",
                "season": season,
                "home": None,
                "away": None,
                "league": league_payload,
                "missing": ["home_team_profile", "away_team_profile"],
            }

        payload = {
            "available": True,
            "source": "stored_team_intelligence",
            "status": "available",
            "league_key": league_key,
            "league_name": (league.name if league else fixture.get("league")) or "",
            "season": season,
            "home": self._team_payload(home, league_key=league_key, season=season) if home else None,
            "away": self._team_payload(away, league_key=league_key, season=season) if away else None,
            "league": league_payload,
            "missing": [],
        }
        if not home:
            payload["missing"].append("home_team_profile")
        if not away:
            payload["missing"].append("away_team_profile")
        if payload["missing"]:
            payload["status"] = "partial"
        return json_safe(payload)

    def _team_for_side(self, fixture: dict[str, Any], side: str, *, league_key: str, season: str) -> TeamProfile | None:
        provider_id = str(
            fixture.get(f"statpal_{side}_team_id")
            or fixture.get(f"{side}_team_id")
            or fixture.get("hid" if side == "home" else "aid")
            or ""
        ).strip()
        if provider_id:
            team = self._team_by_provider_id(provider_id, league_key=league_key, season=season)
            if team:
                return team

        name = str(
            fixture.get(f"{side}_team")
            or fixture.get("hname" if side == "home" else "aname")
            or ""
        ).strip()
        if not name:
            return None
        return self._team_by_name(name, league_key=league_key, season=season)

    @staticmethod
    def _team_by_provider_id(provider_id: str, *, league_key: str, season: str) -> TeamProfile | None:
        query = (
            Q(provider_ids__statpal__team_id=provider_id)
            | Q(provider_ids__api_football__team_id=provider_id)
            | Q(provider_ids__statpal_team_id=provider_id)
            | Q(provider_ids__api_football_team_id=provider_id)
        )
        teams = TeamProfile.objects.filter(query, active=True)
        if league_key:
            teams = teams.filter(Q(primary_league_key=league_key) | Q(season_profiles__league_key=league_key))
        if season:
            teams = teams.filter(Q(season_profiles__season=season) | Q(season_profiles__isnull=True))
        return teams.order_by("-updated_at").distinct().first()

    @staticmethod
    def _team_by_name(name: str, *, league_key: str, season: str) -> TeamProfile | None:
        normalized = normalize_fixture_text(name)
        query = Q(canonical_normalized=normalized) | Q(aliases__contains=[name])
        teams = TeamProfile.objects.filter(query, active=True)
        if league_key:
            teams = teams.filter(Q(primary_league_key=league_key) | Q(season_profiles__league_key=league_key))
        if season:
            teams = teams.filter(Q(season_profiles__season=season) | Q(season_profiles__isnull=True))
        return teams.order_by("-updated_at").distinct().first()

    def _team_payload(self, team: TeamProfile, *, league_key: str, season: str) -> dict[str, Any]:
        season_profile = self._season_profile(team, league_key=league_key, season=season)
        return {
            "team_id": team.pk,
            "canonical_name": team.canonical_name,
            "canonical_normalized": team.canonical_normalized,
            "country": team.country,
            "provider_ids": team.provider_ids,
            "coverage": self._coverage_payload(team, league_key=league_key, season=season),
            "season_profile": self._season_profile_payload(season_profile),
            "recent_form": [
                self._recent_form_payload(profile)
                for profile in TeamRecentFormProfile.objects.filter(
                    team=team,
                    league_key=league_key,
                    season=season,
                ).order_by("window", "scope")
            ],
            "market_profiles": [
                self._team_market_payload(profile)
                for profile in TeamMarketProfile.objects.filter(
                    team=team,
                    league_key=league_key,
                    season=season,
                    attempts__gt=0,
                ).order_by("market_family", "market", "scope")
            ],
        }

    @staticmethod
    def _season_profile(team: TeamProfile, *, league_key: str, season: str) -> TeamSeasonProfile | None:
        return (
            TeamSeasonProfile.objects.filter(team=team, league_key=league_key, season=season)
            .order_by("-updated_at")
            .first()
        )

    @staticmethod
    def _coverage_payload(team: TeamProfile, *, league_key: str, season: str) -> dict[str, Any]:
        row = (
            DataCoverage.objects.filter(
                team=team,
                league_key=league_key,
                season=season,
                provider=DataCoverageTracker.PROVIDER,
                coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
            )
            .order_by("-updated_at")
            .first()
        )
        if not row:
            return {"status": "missing", "coverage_key": DataCoverageTracker.TEAM_COVERAGE_KEY}
        return {
            "status": row.status,
            "coverage_key": row.coverage_key,
            "freshness_seconds": row.freshness_seconds,
            "available_requirements": row.available_requirements,
            "missing_requirements": row.missing_requirements,
            "last_success_at": row.last_success_at,
            "expires_at": row.expires_at,
            "metadata": row.metadata,
        }

    @staticmethod
    def _season_profile_payload(profile: TeamSeasonProfile | None) -> dict[str, Any]:
        if not profile:
            return {}
        return {
            "matches_played": profile.matches_played,
            "home_matches": profile.home_matches,
            "away_matches": profile.away_matches,
            "goals_for": profile.goals_for,
            "goals_against": profile.goals_against,
            "home_goals_for": profile.home_goals_for,
            "home_goals_against": profile.home_goals_against,
            "away_goals_for": profile.away_goals_for,
            "away_goals_against": profile.away_goals_against,
            "xg_for": profile.xg_for,
            "xg_against": profile.xg_against,
            "corners_for": profile.corners_for,
            "corners_against": profile.corners_against,
            "cards_for": profile.cards_for,
            "cards_against": profile.cards_against,
            "shots_for": profile.shots_for,
            "shots_against": profile.shots_against,
            "shots_on_target_for": profile.shots_on_target_for,
            "shots_on_target_against": profile.shots_on_target_against,
            "clean_sheet_rate": profile.clean_sheet_rate,
            "btts_rate": profile.btts_rate,
            "over_15_rate": profile.over_15_rate,
            "over_25_rate": profile.over_25_rate,
            "data_quality": profile.data_quality,
            "source": profile.source,
            "computed_at": profile.computed_at,
            "fetched_at": profile.fetched_at,
            "stats": profile.stats,
        }

    @staticmethod
    def _recent_form_payload(profile: TeamRecentFormProfile) -> dict[str, Any]:
        return {
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
            "corners_for": profile.corners_for,
            "corners_against": profile.corners_against,
            "cards_for": profile.cards_for,
            "cards_against": profile.cards_against,
            "shots_on_target_for": profile.shots_on_target_for,
            "shots_on_target_against": profile.shots_on_target_against,
            "form": profile.form,
            "stats": profile.stats,
            "computed_at": profile.computed_at,
        }

    @staticmethod
    def _team_market_payload(profile: TeamMarketProfile) -> dict[str, Any]:
        return {
            "market_family": profile.market_family,
            "market": profile.market,
            "scope": profile.scope,
            "side": profile.side,
            "line": profile.line,
            "attempts": profile.attempts,
            "wins": profile.wins,
            "losses": profile.losses,
            "voids": profile.voids,
            "hit_rate": profile.hit_rate,
            "avg_odds": profile.avg_odds,
            "roi_flat": profile.roi_flat,
            "confidence": profile.confidence,
            "data_quality": profile.data_quality,
            "stats": profile.stats,
            "computed_at": profile.computed_at,
        }

    def _league_payload(self, *, league_key: str, season: str) -> dict[str, Any]:
        if not league_key or not season:
            return {}
        coverage = (
            DataCoverage.objects.filter(
                subject_type=DataCoverage.SubjectType.LEAGUE,
                subject_key=f"{league_key}:{season}",
                provider=DataCoverageTracker.PROVIDER,
                coverage_key=DataCoverageTracker.LEAGUE_COVERAGE_KEY,
            )
            .order_by("-updated_at")
            .first()
        )
        profiles = LeagueMarketProfile.objects.filter(
            league_key=league_key,
            season=season,
            attempts__gt=0,
        ).order_by("market_family", "market")
        return {
            "coverage": {
                "status": coverage.status,
                "freshness_seconds": coverage.freshness_seconds,
                "missing_requirements": coverage.missing_requirements,
                "metadata": coverage.metadata,
            }
            if coverage
            else {"status": "missing", "coverage_key": DataCoverageTracker.LEAGUE_COVERAGE_KEY},
            "market_profiles": [
                {
                    "market_family": profile.market_family,
                    "market": profile.market,
                    "line": profile.line,
                    "attempts": profile.attempts,
                    "hit_rate": profile.hit_rate,
                    "avg_odds": profile.avg_odds,
                    "roi_flat": profile.roi_flat,
                    "confidence": profile.confidence,
                    "fairness_score": profile.fairness_score,
                    "volatility": profile.volatility,
                    "data_quality": profile.data_quality,
                    "stats": profile.stats,
                }
                for profile in profiles
            ],
        }

    @staticmethod
    def _league_for_fixture(fixture: dict[str, Any]):
        provider_league_id = str(
            fixture.get("statpal_provider_competition_id")
            or fixture.get("provider_competition_id")
            or fixture.get("league_id")
            or fixture.get("code")
            or ""
        ).strip()
        league_name = normalize_fixture_text(fixture.get("league") or "")
        country = normalize_fixture_text(fixture.get("country") or "")
        for league in team_intelligence_leagues():
            if provider_league_id and provider_league_id in {
                str(league.statpal_league_id),
                str(league.api_football_league_id),
            }:
                return league
            if league_name and league_name == normalize_fixture_text(league.name):
                return league
            if country and league_name and country == normalize_fixture_text(league.country):
                if normalize_fixture_text(league.name).endswith(league_name) or league_name in normalize_fixture_text(league.name):
                    return league
        return None

    @staticmethod
    def _league_key_from_profiles(fixture: dict[str, Any]) -> str:
        league_name = str(fixture.get("league") or "").strip()
        if not league_name:
            return ""
        profile = TeamSeasonProfile.objects.filter(league_name__iexact=league_name).order_by("-updated_at").first()
        return profile.league_key if profile else ""


team_intelligence_service = TeamIntelligenceService()
