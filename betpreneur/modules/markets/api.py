"""The betting vocabulary — the public surface of the markets module.

markets owns what a bet *is*: how a bookmaker's market string resolves to a
canonical name, which family it belongs to, what data is needed to evaluate
it, and which engine can price it. It holds no database and no opinion about
whether a bet is good — that is pricing's job.

This module is the only importable surface. Nothing outside markets may reach
into markets.domain.
"""
from __future__ import annotations

from .contracts import (
    CanonicalMarket,
    DailyMarketCatalogEntry,
    DataCapability,
    EvaluatorSpec,
    MarketCapability,
    MarketDescriptor,
    Period,
    Resolution,
    Settlement,
    Subject,
)

# -- contract types, re-exported so one import line serves a caller ---------
from .domain.canonical import settlement_for_line

# -- capability: what data a market needs, and how well we can serve it -----
from .domain.capabilities import (
    FULL,
    MEDIUM,
    UNSUPPORTED,
    WEAK,
    MarketCapabilityService,
    market_capability_service,
)

# -- the daily catalogue ----------------------------------------------------
from .domain.catalogue import (
    DAILY_MARKET_CATALOG,
    DAILY_MARKET_DISCOVERY_POOL,
    DAILY_MARKET_FAMILY_OVERRIDES,
    DAILY_MARKET_LOOKUP,
    DAILY_MARKET_MEANINGS,
    EXCLUDED_DAILY_MARKETS,
    PROVEN_DAILY_MARKETS,
    build_daily_market_scores,
    daily_catalog_entry,
    daily_discovery_market_names,
    daily_evaluation_route,
    daily_market_family_payload,
    daily_market_names,
    daily_markets_by_family,
    daily_odds_key_map,
    daily_scoring_market_names,
)
from .domain.data_capability import (
    capabilities_from_snapshots,
    coverage,
    missing,
    snapshots_for_capabilities,
)

# -- evaluation routing: family -> engine, declarative only -----------------
from .domain.evaluation import (
    COUNT_MODEL_ENGINE,
    HEURISTIC,
    MARKET_EVALUATORS,
    NONE,
    QUANTITATIVE,
    SCORE_MATRIX_ENGINE,
    STATPAL_ENGINE,
    assessment_type_for,
    evaluator_for,
    modelled_families,
    required_capabilities,
)
from .domain.settleable import SETTLEABLE_MARKETS, can_settle_market

# -- vocabulary ------------------------------------------------------------
from .domain.taxonomy import (
    CORE_MARKETS,
    canonical_market_name,
    describe_market,
    market_matches,
    market_options,
    normalize_market_text,
)

__all__ = [
    "CORE_MARKETS",
    "COUNT_MODEL_ENGINE",
    "DAILY_MARKET_CATALOG",
    "DAILY_MARKET_DISCOVERY_POOL",
    "DAILY_MARKET_FAMILY_OVERRIDES",
    "DAILY_MARKET_LOOKUP",
    "DAILY_MARKET_MEANINGS",
    "EXCLUDED_DAILY_MARKETS",
    "FULL",
    "HEURISTIC",
    "MARKET_EVALUATORS",
    "MEDIUM",
    "NONE",
    "PROVEN_DAILY_MARKETS",
    "QUANTITATIVE",
    "SCORE_MATRIX_ENGINE",
    "SETTLEABLE_MARKETS",
    "STATPAL_ENGINE",
    "UNSUPPORTED",
    "WEAK",
    "CanonicalMarket",
    "DailyMarketCatalogEntry",
    "DataCapability",
    "EvaluatorSpec",
    "MarketCapability",
    "MarketCapabilityService",
    "MarketDescriptor",
    "Period",
    "Resolution",
    "Settlement",
    "Subject",
    "assessment_type_for",
    "build_daily_market_scores",
    "can_settle_market",
    "canonical_market_name",
    "capabilities_from_snapshots",
    "coverage",
    "daily_catalog_entry",
    "daily_discovery_market_names",
    "daily_evaluation_route",
    "daily_market_family_payload",
    "daily_market_names",
    "daily_markets_by_family",
    "daily_odds_key_map",
    "daily_scoring_market_names",
    "describe_market",
    "evaluator_for",
    "market_capability_service",
    "market_matches",
    "market_options",
    "missing",
    "modelled_families",
    "normalize_market_text",
    "required_capabilities",
    "settlement_for_line",
    "snapshots_for_capabilities",
]
