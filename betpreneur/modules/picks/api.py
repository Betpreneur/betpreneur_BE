"""The daily free product.

Orchestrates a run end to end and owns everything it produces: the run
record, the fixtures it considered, the picks it published and the markets
it priced. slips sits above this and reads the analysis back.

This module is the only importable surface. Nothing outside the module
may reach into picks.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""

from __future__ import annotations

from betpreneur.platform.cache.http import cached_response

from .interface.serializers import PickSerializer
from .models import AlgoFixture, AlgoRun, MarketPrediction, Pick, StrategyReview
from .services.presentation import (
    EXCLUDED_MARKETS,
    decimal_or_none,
    format_game_form_line,
    game_detail_payload,
    game_summary_from_fixture,
    market_prediction_payload,
    normalise_council_review,
    picks_by_match_for_run,
)
from .services.runner_service import AlgoRunnerService, algo_runner_service

__all__ = [
    "EXCLUDED_MARKETS",
    "AlgoFixture",
    "AlgoRun",
    "AlgoRunnerService",
    "MarketPrediction",
    "Pick",
    "PickSerializer",
    "StrategyReview",
    "algo_runner_service",
    "decimal_or_none",
    "format_game_form_line",
    "game_detail_payload",
    "game_summary_from_fixture",
    "market_prediction_payload",
    "normalise_council_review",
    "picks_by_match_for_run",
]
