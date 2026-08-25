"""Fitted models and the distributions they produce.

scoring is mathematics with no betting opinion in it: given a fixture and
its context, what does the model say the scoreline looks like. Whether that
makes a bet worth taking is pricing's judgement, not scoring's.

This module is the only importable surface. Nothing outside the module
may reach into scoring.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""
from __future__ import annotations

from .domain.correlation import CorrelationResult, combine, group_factor
from .domain.dixon_coles import ScoreMatrix, build_score_matrix
from .domain.predicates import predicate_for
from .evaluators import count_market_evaluator, score_matrix_evaluator
from .evaluators.score_matrix_evaluator import (
    RESULT_DEPENDENT_FAMILIES,
    outcome_probability,
)
from .models import FixtureLineup, PlayerAvailability, TeamRateProfile
from .services.availability import (
    name_keys,
    normalize_person,
    parse_injuries_payload,
    player_availability_service,
)
from .services.capability import capability_for_descriptor
from .services.lineups import (
    BENCH,
    OMITTED,
    STARTING,
    lineup_service,
    parse_lineups_payload,
)
from .services.priority_fixtures import register_priority_fixture_source
from .services.service import score_model_service

__all__ = [
    "BENCH",
    "OMITTED",
    "RESULT_DEPENDENT_FAMILIES",
    "STARTING",
    "CorrelationResult",
    "FixtureLineup",
    "PlayerAvailability",
    "ScoreMatrix",
    "TeamRateProfile",
    "build_score_matrix",
    "capability_for_descriptor",
    "combine",
    "count_market_evaluator",
    "group_factor",
    "lineup_service",
    "name_keys",
    "normalize_person",
    "outcome_probability",
    "parse_injuries_payload",
    "parse_lineups_payload",
    "player_availability_service",
    "predicate_for",
    "register_priority_fixture_source",
    "score_matrix_evaluator",
    "score_model_service",
]
