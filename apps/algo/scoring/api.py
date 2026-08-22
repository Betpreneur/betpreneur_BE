"""Public scoring API."""

from .data.capability import (
    DataCapability,
    capabilities_from_snapshots,
    coverage,
    missing,
    snapshots_for_capabilities,
)
from .data.planner import (
    FixtureHydrator,
    HydrationStats,
    capability_for_descriptor,
    model_backed_capability,
    plan_slip_hydration,
    snapshots_for_family,
)
from .availability import player_availability_service
from .correlation import CorrelationResult, combine
from .evaluators.registry import (
    COUNT_MODEL_ENGINE,
    HEURISTIC,
    NONE,
    QUANTITATIVE,
    SCORE_MATRIX_ENGINE,
    STATPAL_ENGINE,
    EvaluatorSpec,
    assessment_type_for,
    evaluator_for,
    modelled_families,
    required_capabilities,
)
from .evaluators.score_matrix_evaluator import RESULT_DEPENDENT_FAMILIES, _outcome_probability
from .dixon_coles import build_score_matrix
from .lineups import lineup_service
from .service import FixtureRates, ScoreModelService, score_model_service


def rates_for_fixture(*args, **kwargs) -> FixtureRates:
    """Return fitted fixture rates through the scoring boundary."""
    return score_model_service.rates_for_fixture(*args, **kwargs)


def evaluate(descriptor, *, fixture=None, **kwargs) -> dict:
    """Evaluate a descriptor with a scoring-owned engine when one exists."""
    spec = evaluator_for(getattr(descriptor, "family", ""))
    if spec is None:
        return {"available": False, "basis": "no_model_for_family"}
    if spec.engine == SCORE_MATRIX_ENGINE:
        from .evaluators import score_matrix_evaluator as engine
    elif spec.engine == COUNT_MODEL_ENGINE:
        from .evaluators import count_market_evaluator as engine
    else:
        return {"available": False, "basis": "advisory_engine_required"}
    return engine.evaluate(descriptor, fixture=fixture, **kwargs)


def outcome_probability(descriptor, matrix):
    """Map a descriptor to a probability from the shared score matrix."""
    return _outcome_probability(descriptor, matrix)


__all__ = [
    "COUNT_MODEL_ENGINE",
    "CorrelationResult",
    "DataCapability",
    "EvaluatorSpec",
    "FixtureHydrator",
    "FixtureRates",
    "HEURISTIC",
    "HydrationStats",
    "NONE",
    "QUANTITATIVE",
    "RESULT_DEPENDENT_FAMILIES",
    "SCORE_MATRIX_ENGINE",
    "STATPAL_ENGINE",
    "ScoreModelService",
    "assessment_type_for",
    "build_score_matrix",
    "capabilities_from_snapshots",
    "capability_for_descriptor",
    "combine",
    "coverage",
    "evaluate",
    "evaluator_for",
    "missing",
    "model_backed_capability",
    "modelled_families",
    "lineup_service",
    "outcome_probability",
    "plan_slip_hydration",
    "player_availability_service",
    "rates_for_fixture",
    "required_capabilities",
    "score_model_service",
    "snapshots_for_capabilities",
    "snapshots_for_family",
]
