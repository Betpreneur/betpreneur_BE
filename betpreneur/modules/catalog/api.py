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
from .domain.sportybet_normalize import resolve
from .domain.text import normalize_fixture_text
from .interface.views import api_response_payload
from .models import (
    FixtureCache,
    ProviderFixtureMap,
    ProviderPlayerMap,
    SlipReviewMarketCache,
    StatPalFixtureSnapshot,
)
from .services import legacy_runner
from .services.daily_build import StatPalDailyBuildService
from .services.legacy_runner import (
    aps_get,
    fetch_team_recent_form,
    recent_form_summary,
    run_daily_algo,
)
from .services.market_cache import SlipReviewMarketCacheWriter
from .services.planner import FixtureHydrator, plan_slip_hydration
from .services.provider_client import statpal_client
from .services.resolution import provider_mapping_service
from .services.runner_env import runner_env
from .services.search import FixtureSearchService, token_side_score
from .services.snapshots import statpal_snapshot_service

__all__ = [
    "FixtureCache",
    "FixtureHydrator",
    "FixtureSearchService",
    "ProviderFixtureMap",
    "ProviderPlayerMap",
    "SlipReviewMarketCache",
    "SlipReviewMarketCacheWriter",
    "StatPalDailyBuildService",
    "StatPalFixtureSnapshot",
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
    "token_side_score",
]
