"""Monte Carlo ticket simulation."""

from __future__ import annotations

from math import sqrt
from random import Random
from statistics import NormalDist

from .contracts import MarketProbability, PredictionDiagnostics, TicketSimulation
from .correlation import analyze_ticket_correlation

DEFAULT_SIMULATIONS = 50_000
MAX_SIMULATIONS = 200_000
SAME_FIXTURE_CORRELATION = 0.35
MAX_CORRELATION = 0.85
CONCENTRATION_WARNING_SHARE = 0.40

NORMAL = NormalDist()


def simulate_ticket_probabilities(
    selections,
    *,
    simulations: int = 0,
    seed: int | None = None,
) -> TicketSimulation:
    """Estimate accumulator probability with same-fixture correlation.

    The current product payload gives this layer calibrated market
    probabilities, not full event paths. We therefore simulate correlated leg
    hits with a normal copula. This preserves each leg's calibrated probability
    while avoiding the old independent multiplication assumption.
    """
    market_probabilities = tuple(
        item for item in selections or () if isinstance(item, MarketProbability)
    )
    valid = tuple(item for item in market_probabilities if item.effective_probability is not None)
    run_count = _simulation_count(simulations)
    warnings = []
    if len(valid) != len(market_probabilities):
        warnings.append("selection_probability_missing")
    if not valid:
        return TicketSimulation(
            selections=market_probabilities,
            simulations=run_count,
            estimated_success_probability=None,
            independent_success_probability=None,
            correlation_adjustment=None,
            risk_concentration_score=None,
            fixture_exposure={},
            portfolio_exposure={},
            correlation_warnings=tuple(warnings),
            diagnostics=PredictionDiagnostics(
                data_quality="unavailable",
                model_sources=("prediction.monte_carlo",),
                warnings=tuple(warnings),
            ),
        )

    correlation = analyze_ticket_correlation(valid)
    fixture_exposure = correlation.fixture_exposure
    portfolio_exposure = correlation.market_family_exposure
    independent = _independent_probability(valid)
    simulated = _run_simulation(valid, fixture_exposure, simulations=run_count, seed=seed)
    concentration = correlation.concentration_score
    warnings.extend(correlation.warnings)
    warnings.append("monte_carlo_probability_space")

    return TicketSimulation(
        selections=market_probabilities,
        simulations=run_count,
        estimated_success_probability=simulated,
        independent_success_probability=independent,
        correlation_adjustment=_round_probability(
            None if independent is None else simulated - independent
        ),
        risk_concentration_score=concentration,
        fixture_exposure=fixture_exposure,
        portfolio_exposure=portfolio_exposure,
        correlation_warnings=tuple(dict.fromkeys(warnings)),
        diagnostics=PredictionDiagnostics(
            data_quality=_combined_quality(valid),
            model_sources=("prediction.monte_carlo",),
            warnings=tuple(dict.fromkeys(warnings)),
            metadata={
                "simulation_method": "normal_copula_correlated_bernoulli",
                "default_simulations": DEFAULT_SIMULATIONS,
                "max_simulations": MAX_SIMULATIONS,
                "same_fixture_correlation": SAME_FIXTURE_CORRELATION,
                "risk_concentration_score": concentration,
                "correlation_pair_count": len(correlation.pairs),
            },
        ),
    )


def _run_simulation(
    selections: tuple[MarketProbability, ...],
    fixture_exposure: dict[str, int],
    *,
    simulations: int,
    seed: int | None,
) -> float:
    rng = Random(seed)
    thresholds = [_threshold(item.effective_probability) for item in selections]
    wins = 0
    for _ in range(simulations):
        fixture_shocks = {
            fixture_id: rng.gauss(0.0, 1.0)
            for fixture_id, count in fixture_exposure.items()
            if count > 1
        }
        ticket_hit = True
        for selection, threshold in zip(selections, thresholds, strict=True):
            correlation = _selection_correlation(selection, fixture_exposure)
            common = fixture_shocks.get(selection.fixture_id, rng.gauss(0.0, 1.0))
            individual = rng.gauss(0.0, 1.0)
            latent = correlation * common + sqrt(max(0.0, 1.0 - correlation**2)) * individual
            if latent > threshold:
                ticket_hit = False
                break
        if ticket_hit:
            wins += 1
    return _round_probability(wins / simulations) or 0.0


def _selection_correlation(selection: MarketProbability, fixture_exposure: dict[str, int]) -> float:
    explicit = selection.diagnostics.metadata.get("correlation")
    if explicit not in (None, ""):
        return _clamp(float(explicit), 0.0, MAX_CORRELATION)
    if fixture_exposure.get(selection.fixture_id, 0) > 1:
        return SAME_FIXTURE_CORRELATION
    return 0.0


def _independent_probability(selections: tuple[MarketProbability, ...]) -> float | None:
    probability = 1.0
    for selection in selections:
        if selection.effective_probability is None:
            return None
        probability *= selection.effective_probability
    return _round_probability(probability)


def _combined_quality(selections: tuple[MarketProbability, ...]) -> str:
    ranks = {
        "calibrated": 5,
        "strong": 4,
        "fresh": 4,
        "medium": 3,
        "limited": 2,
        "partial": 2,
        "poor": 1,
        "unavailable": 0,
        "unknown": 0,
    }
    quality = min(
        (str(item.data_quality or "unknown").lower() for item in selections),
        key=lambda item: ranks.get(item, 0),
    )
    return quality


def _simulation_count(value: int) -> int:
    if value is None or value <= 0:
        return DEFAULT_SIMULATIONS
    return max(1, min(int(value), MAX_SIMULATIONS))


def _threshold(probability: float | None) -> float:
    probability = _clamp(float(probability or 0.0), 1e-9, 1.0 - 1e-9)
    return NORMAL.inv_cdf(probability)


def _round_probability(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
