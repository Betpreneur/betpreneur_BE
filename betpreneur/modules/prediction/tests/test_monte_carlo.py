from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    MarketProbability,
    PredictionDiagnostics,
    simulate_ticket,
)


def _selection(fixture_id, market, probability, *, family="total_goals"):
    return MarketProbability(
        fixture_id=fixture_id,
        market=market,
        calibrated_probability=probability,
        data_quality="strong",
        diagnostics=PredictionDiagnostics(metadata={"market_family": family}),
    )


class MonteCarloSimulationTests(SimpleTestCase):
    def test_simulates_ticket_success_near_independent_probability_for_unrelated_fixtures(self):
        selections = (
            _selection("fixture-1", "Over 2.5", 0.70),
            _selection("fixture-2", "BTTS Yes", 0.60, family="btts"),
            _selection("fixture-3", "Home Win", 0.55, family="match_result"),
        )

        simulation = simulate_ticket(selections, simulations=20_000, seed=7)

        self.assertEqual(simulation.simulations, 20_000)
        self.assertEqual(simulation.independent_success_probability, 0.231)
        self.assertAlmostEqual(simulation.estimated_success_probability, 0.231, delta=0.02)
        self.assertEqual(
            simulation.fixture_exposure, {"fixture-1": 1, "fixture-2": 1, "fixture-3": 1}
        )
        self.assertNotIn("same_fixture_correlation", simulation.correlation_warnings)

    def test_same_fixture_markets_are_simulated_with_correlation_warning(self):
        selections = (
            _selection("fixture-1", "Over 2.5", 0.70),
            _selection("fixture-1", "BTTS Yes", 0.62, family="btts"),
            _selection("fixture-2", "Home Win", 0.57, family="match_result"),
        )

        simulation = simulate_ticket(selections, simulations=20_000, seed=11)

        self.assertEqual(simulation.fixture_exposure["fixture-1"], 2)
        self.assertIn("same_fixture_correlation", simulation.correlation_warnings)
        self.assertIn("concentrated_fixture_exposure", simulation.correlation_warnings)
        self.assertNotEqual(
            simulation.estimated_success_probability,
            simulation.independent_success_probability,
        )

    def test_portfolio_concentration_is_reported(self):
        selections = (
            _selection("fixture-1", "Over 1.5", 0.76),
            _selection("fixture-2", "Over 2.5", 0.64),
            _selection("fixture-3", "Under 3.5", 0.72),
            _selection("fixture-4", "BTTS Yes", 0.58, family="btts"),
        )

        simulation = simulate_ticket(selections, simulations=10_000, seed=3)

        self.assertEqual(simulation.portfolio_exposure["total_goals"], 3)
        self.assertEqual(simulation.risk_concentration_score, 75.0)
        self.assertIn("concentrated_market_family", simulation.correlation_warnings)

    def test_missing_probabilities_return_diagnostic_warning(self):
        simulation = simulate_ticket(
            (MarketProbability(fixture_id="fixture-1", market="Unknown"),),
            simulations=1000,
            seed=1,
        )

        self.assertIsNone(simulation.estimated_success_probability)
        self.assertIn("selection_probability_missing", simulation.correlation_warnings)
