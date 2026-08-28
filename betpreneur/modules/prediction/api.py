"""Shared prediction engine public surface.

prediction owns football probabilities: fixture features, team-strength
signals, score distributions, market probabilities, calibration, and ticket
simulation. It is deliberately product-neutral. All Games, Top Picks, and Slip
Review consume this module through this file only.
"""

from __future__ import annotations

from .calibration import calibrate_probability
from .contracts import (
    CalibrationResult,
    CorrelationPair,
    CorrelationReport,
    CountModelOutput,
    FixtureFeatureSet,
    FixturePrediction,
    GoalModelOutput,
    MarketProbability,
    PredictionDiagnostics,
    RecommendationScore,
    ResultProbabilityOutput,
    TeamStrengthSnapshot,
    TicketSimulation,
    TrainingSampleRecord,
    ValueAssessment,
    WalkForwardEvaluation,
    WalkForwardFold,
)
from .correlation import analyze_ticket_correlation
from .count_models import count_distributions
from .diagnostics import diagnostics_for_prediction
from .elo import result_probabilities
from .explanation import (
    Explanation,
    Fact,
    FactKind,
    build_explanation,
    explain_market,
    explain_value,
    explanation_facts_for_market,
    explanation_facts_for_value,
)
from .feature_builder import build_fixture_features
from .market_probabilities import evaluate_market_probability
from .models import PredictionTrainingSample
from .monte_carlo import simulate_ticket_probabilities
from .poisson import goal_distribution
from .recommendation import score_recommendation
from .training_samples import record_training_sample
from .value import assess_market_value
from .walk_forward import evaluate_walk_forward


def predict_fixture(
    fixture_id: str, *, fixture=None, markets: tuple[str, ...] = ()
) -> FixturePrediction:
    """Build the product-neutral prediction bundle for a fixture."""
    features = (
        fixture
        if isinstance(fixture, FixtureFeatureSet)
        else build_fixture_features(fixture or str(fixture_id), fixture_id=str(fixture_id))
    )
    goals = goal_distribution(features)
    counts = count_distributions(features)
    result = result_probabilities(features)
    prediction = FixturePrediction(
        fixture_id=str(fixture_id),
        fixture_name=features.fixture_name,
        features=features,
        goals=goals,
        counts=counts,
        result=result,
        diagnostics=features.diagnostics,
    )
    if not markets:
        return prediction
    market_probabilities = tuple(
        evaluate_market_probability(prediction, market) for market in markets
    )
    return FixturePrediction(
        fixture_id=prediction.fixture_id,
        fixture_name=prediction.fixture_name,
        features=prediction.features,
        goals=prediction.goals,
        counts=prediction.counts,
        result=prediction.result,
        market_probabilities=market_probabilities,
        diagnostics=prediction.diagnostics,
    )


def evaluate_market(fixture_id, market: str, *, fixture=None) -> MarketProbability:
    """Evaluate one market on one fixture through the prediction boundary."""
    if isinstance(fixture_id, FixturePrediction):
        prediction = fixture_id
    elif isinstance(fixture_id, FixtureFeatureSet):
        prediction = predict_fixture(fixture_id.fixture_id, fixture=fixture_id)
    else:
        prediction = predict_fixture(str(fixture_id), fixture=fixture)
    return evaluate_market_probability(prediction, market)


def simulate_ticket(
    selections, *, simulations: int = 0, seed: int | None = None
) -> TicketSimulation:
    """Estimate ticket-level probability through the prediction boundary."""
    return simulate_ticket_probabilities(selections, simulations=simulations, seed=seed)


__all__ = [
    "CalibrationResult",
    "CorrelationPair",
    "CorrelationReport",
    "CountModelOutput",
    "Explanation",
    "Fact",
    "FactKind",
    "FixtureFeatureSet",
    "FixturePrediction",
    "GoalModelOutput",
    "MarketProbability",
    "PredictionDiagnostics",
    "PredictionTrainingSample",
    "RecommendationScore",
    "ResultProbabilityOutput",
    "TeamStrengthSnapshot",
    "TicketSimulation",
    "TrainingSampleRecord",
    "ValueAssessment",
    "WalkForwardEvaluation",
    "WalkForwardFold",
    "analyze_ticket_correlation",
    "assess_market_value",
    "build_explanation",
    "build_fixture_features",
    "calibrate_probability",
    "count_distributions",
    "diagnostics_for_prediction",
    "evaluate_market",
    "evaluate_market_probability",
    "evaluate_walk_forward",
    "explain_market",
    "explain_value",
    "explanation_facts_for_market",
    "explanation_facts_for_value",
    "goal_distribution",
    "predict_fixture",
    "record_training_sample",
    "result_probabilities",
    "score_recommendation",
    "simulate_ticket",
    "simulate_ticket_probabilities",
]
