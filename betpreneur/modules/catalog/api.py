"""Fixtures, provider identity and cached market data.

catalog is the single source of truth about what a fixture is: which teams,
which league, which provider ids resolve to it, and what we have cached
about it. Everything above asks here rather than resolving names itself.

This module is the only importable surface. Nothing outside the module
may reach into catalog.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""
from __future__ import annotations

from betpreneur.platform.db.json import json_safe

from .domain.bridge import descriptor_from_canonical
from .domain.league_registry import (
    IntelligenceLeague,
    team_intelligence_league_ids,
    team_intelligence_leagues,
    team_intelligence_registry_payload,
)
from .domain.sportybet_normalize import resolve
from .domain.text import normalize_fixture_text
from .interface.views import api_response_payload
from .models import (
    DataCoverage,
    FixtureCache,
    LeagueMarketProfile,
    ProviderFixtureMap,
    ProviderPlayerMap,
    ProviderTeamMap,
    SlipReviewMarketCache,
    StatPalFixtureSnapshot,
    TeamAliasMap,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from .services import legacy_runner
from .services.coverage_tracker import DataCoverageScope, DataCoverageTracker
from .services.daily_build import StatPalDailyBuildService
from .services.historical_hydrator import HistoricalHydrationScope, HistoricalTeamHydrator
from .services.legacy_runner import (
    aps_get,
    fetch_team_recent_form,
    recent_form_summary,
    run_daily_algo,
)
from .services.market_cache import SlipReviewMarketCacheWriter
from .services.market_profiles import MarketProfileBuilder, MarketProfileScope
from .services.planner import FixtureHydrator, plan_slip_hydration
from .services.provider_client import statpal_client
from .services.recent_form import DEFAULT_RECENT_FORM_WINDOWS, RecentFormBuilder, RecentFormScope
from .services.resolution import ProviderMappingService, provider_mapping_service
from .services.runner_env import runner_env
from .services.search import FixtureSearchService, token_side_score
from .services.snapshots import statpal_snapshot_service
from .services.team_intelligence import TeamIntelligenceService, team_intelligence_service
from .services.team_intelligence_backfill import (
    TeamIntelligenceBackfillService,
    team_intelligence_backfill_service,
)

__all__ = [
    "DEFAULT_RECENT_FORM_WINDOWS",
    "DataCoverage",
    "DataCoverageScope",
    "DataCoverageTracker",
    "FixtureCache",
    "FixtureHydrator",
    "FixtureSearchService",
    "HistoricalHydrationScope",
    "HistoricalTeamHydrator",
    "IntelligenceLeague",
    "LeagueMarketProfile",
    "MarketProfileBuilder",
    "MarketProfileScope",
    "ProviderFixtureMap",
    "ProviderMappingService",
    "ProviderPlayerMap",
    "ProviderTeamMap",
    "RecentFormBuilder",
    "RecentFormScope",
    "SlipReviewMarketCache",
    "SlipReviewMarketCacheWriter",
    "StatPalDailyBuildService",
    "StatPalFixtureSnapshot",
    "TeamAliasMap",
    "TeamIntelligenceBackfillService",
    "TeamIntelligenceService",
    "TeamMarketProfile",
    "TeamProfile",
    "TeamRecentFormProfile",
    "TeamSeasonProfile",
    "api_response_payload",
    "aps_get",
    "descriptor_from_canonical",
    "fetch_team_recent_form",
    "legacy_runner",
    "normalize_fixture_text",
    "plan_slip_hydration",
    "provider_mapping_service",
    "recent_form_summary",
    "resolve",
    "run_daily_algo",
    "runner_env",
    "statpal_client",
    "statpal_snapshot_service",
    "team_intelligence_backfill_service",
    "team_intelligence_league_ids",
    "team_intelligence_leagues",
    "team_intelligence_registry_payload",
    "team_intelligence_service",
    "token_side_score",
]
