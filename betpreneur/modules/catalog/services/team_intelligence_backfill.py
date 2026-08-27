from __future__ import annotations

import logging
from typing import Any

from django.db.models import Count

from betpreneur.modules.catalog.domain.league_registry import (
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.models import (
    DataCoverage,
    TeamMarketProfile,
)
from betpreneur.modules.catalog.services.coverage_tracker import (
    REQUIRED_MARKET_FAMILIES,
    DataCoverageTracker,
)
from betpreneur.modules.catalog.services.historical_hydrator import HistoricalTeamHydrator
from betpreneur.modules.catalog.services.market_profiles import MarketProfileBuilder
from betpreneur.modules.catalog.services.recent_form import (
    DEFAULT_RECENT_FORM_WINDOWS,
    RecentFormBuilder,
)
from betpreneur.platform.db.json import json_safe

log = logging.getLogger(__name__)


class TeamIntelligenceBackfillService:
    """One-time current/previous season backfill for top-league intelligence."""

    def backfill(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        max_teams: int | None = None,
        max_matches: int | None = None,
        min_attempts: int = 1,
        ttl_hours: int = 24,
        sync_recent_matches: bool = True,
    ) -> dict[str, Any]:
        leagues = [
            league
            for league in team_intelligence_leagues()
            if not league_keys or league.key in set(league_keys)
        ]
        seasons = sorted({league.current_season for league in leagues} | {league.previous_season for league in leagues})
        selected_league_keys = [league.key for league in leagues]
        log.info(
            "team_intelligence_backfill_started leagues=%s seasons=%s max_teams=%s max_matches=%s",
            len(selected_league_keys),
            seasons,
            max_teams,
            max_matches,
        )

        hydration = HistoricalTeamHydrator().hydrate(
            league_keys=selected_league_keys,
            seasons=seasons,
            max_teams=max_teams,
        )
        self._log_step("hydrate_history", hydration)

        recent_form = RecentFormBuilder().build(
            league_keys=selected_league_keys,
            seasons=seasons,
            windows=DEFAULT_RECENT_FORM_WINDOWS,
            sync_matches=sync_recent_matches,
            max_matches=max_matches,
        )
        self._log_step("build_recent_form", recent_form)

        market_profiles = MarketProfileBuilder().build(
            league_keys=selected_league_keys,
            seasons=seasons,
            min_attempts=min_attempts,
        )
        self._log_step("build_market_profiles", market_profiles)

        coverage = DataCoverageTracker().refresh(
            league_keys=selected_league_keys,
            seasons=seasons,
            ttl_hours=ttl_hours,
        )
        self._log_step("refresh_coverage", coverage)

        monitoring = self.monitoring_report(
            league_keys=selected_league_keys,
            seasons=seasons,
        )
        result = {
            "status": self._status([hydration, recent_form, market_profiles, coverage]),
            "leagues": selected_league_keys,
            "seasons": seasons,
            "steps": {
                "hydrate_history": hydration,
                "build_recent_form": recent_form,
                "build_market_profiles": market_profiles,
                "refresh_coverage": coverage,
            },
            "monitoring": monitoring,
        }
        log.info("team_intelligence_backfill_finished status=%s monitoring=%s", result["status"], monitoring)
        return json_safe(result)

    def monitoring_report(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        seasons: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        league_keys = list(league_keys or [league.key for league in team_intelligence_leagues()])
        if seasons is None:
            selected = [league for league in team_intelligence_leagues() if league.key in set(league_keys)]
            seasons = sorted({league.current_season for league in selected} | {league.previous_season for league in selected})
        seasons = list(seasons or [])

        coverage = DataCoverage.objects.filter(
            coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
            league_key__in=league_keys,
            season__in=seasons,
        )
        coverage_counts = dict(coverage.values_list("status").annotate(total=Count("id")))
        stale_teams = list(
            coverage.filter(status__in=[DataCoverage.Status.STALE, DataCoverage.Status.FAILED])
            .select_related("team")
            .order_by("league_key", "season", "team__canonical_name")
            .values(
                "team__canonical_name",
                "league_key",
                "season",
                "status",
                "last_success_at",
                "expires_at",
                "error",
            )[:50]
        )
        missing_families = self._missing_market_families(league_keys=league_keys, seasons=seasons)
        confidence_values = [
            float(row.metadata.get("confidence"))
            for row in coverage.only("metadata")
            if isinstance(row.metadata, dict) and row.metadata.get("confidence") is not None
        ]
        score_values = [float(value) for value in coverage.values_list("score", flat=True) if value is not None]
        return json_safe(
            {
                "coverage_counts": coverage_counts,
                "stale_or_failed_teams": stale_teams,
                "missing_market_families": missing_families,
                "profile_confidence": {
                    "average": round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0.0,
                    "average_coverage_score": round(sum(score_values) / len(score_values), 1) if score_values else 0.0,
                },
            }
        )

    @staticmethod
    def _missing_market_families(*, league_keys: list[str], seasons: list[str]) -> list[dict[str, Any]]:
        rows = []
        for league_key in league_keys:
            for season in seasons:
                present = set(
                    TeamMarketProfile.objects.filter(
                        league_key=league_key,
                        season=season,
                        attempts__gt=0,
                    )
                    .values_list("market_family", flat=True)
                    .distinct()
                )
                missing = sorted(set(REQUIRED_MARKET_FAMILIES) - present)
                if missing:
                    rows.append(
                        {
                            "league_key": league_key,
                            "season": season,
                            "missing_families": missing,
                        }
                    )
        return rows

    @staticmethod
    def _status(results: list[dict[str, Any]]) -> str:
        if any(result.get("status") == "failed" for result in results):
            return "failed"
        if any(result.get("status") in {"partial", "skipped"} for result in results):
            return "partial"
        return "complete"

    @staticmethod
    def _log_step(step: str, result: dict[str, Any]) -> None:
        api_usage = result.get("api_usage") or {}
        failed = result.get("failed") or result.get("coverage_failed") or result.get("skipped") or 0
        errors = result.get("errors") or []
        log.info(
            "team_intelligence_backfill_step step=%s status=%s api_usage=%s failed=%s errors=%s",
            step,
            result.get("status", "unknown"),
            api_usage,
            failed,
            len(errors),
        )


team_intelligence_backfill_service = TeamIntelligenceBackfillService()
