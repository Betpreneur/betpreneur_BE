from dataclasses import fields

from django.test import TestCase

from betpreneur.modules.prediction.api import (
    CalibrationResult,
    FixturePrediction,
    GoalModelOutput,
    MarketProbability,
    PredictionDiagnostics,
    ResultProbabilityOutput,
    TeamStrengthSnapshot,
    TicketSimulation,
    evaluate_market,
    predict_fixture,
    simulate_ticket,
)


class PredictionContractTests(TestCase):
    def test_market_probability_exposes_product_neutral_probability_fields(self):
        names = {field.name for field in fields(MarketProbability)}

        self.assertTrue(
            {
                "raw_probability",
                "calibrated_probability",
                "confidence_score",
                "data_quality",
                "model_sources",
                "warnings",
                "explanation_facts",
            }.issubset(names)
        )

    def test_market_probability_excludes_recommendation_decisions(self):
        names = {field.name for field in fields(MarketProbability)}

        self.assertFalse(
            {
                "tier",
                "banker",
                "value_gem",
                "wild_card",
                "recommendation_score",
                "action",
                "replace",
                "published",
                "eligible",
            }.intersection(names)
        )

    def test_market_probability_derives_fair_odds_from_calibrated_probability(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Over 2.5",
            raw_probability=0.72,
            calibrated_probability=0.68,
            confidence_score=68,
            data_quality="medium",
            model_sources=("poisson",),
            explanation_facts=("Projected total goals: 3.1.",),
        )

        self.assertEqual(probability.effective_probability, 0.68)
        self.assertEqual(probability.fair_odds, 1.4706)

    def test_market_probability_rejects_invalid_probability_and_score_values(self):
        with self.assertRaises(ValueError):
            MarketProbability(fixture_id="fixture-1", market="Over 2.5", raw_probability=1.2)

        with self.assertRaises(ValueError):
            MarketProbability(fixture_id="fixture-1", market="Over 2.5", confidence_score=101)

    def test_result_and_goal_outputs_validate_probability_ranges(self):
        ResultProbabilityOutput(home_win=0.45, draw=0.27, away_win=0.28)
        GoalModelOutput(scoreline_matrix={"1-0": 0.12, "1-1": 0.1})

        with self.assertRaises(ValueError):
            ResultProbabilityOutput(home_win=-0.1)

        with self.assertRaises(ValueError):
            GoalModelOutput(scoreline_matrix={"9-9": 1.1})

    def test_public_api_returns_contract_objects_without_product_decisions(self):
        prediction = predict_fixture("fixture-1", markets=("Home Win",))
        market = evaluate_market("fixture-1", "Home Win")
        ticket = simulate_ticket((market,), simulations=1000)

        self.assertIsInstance(prediction, FixturePrediction)
        self.assertIsInstance(prediction.diagnostics, PredictionDiagnostics)
        self.assertIsInstance(market, MarketProbability)
        self.assertIsInstance(ticket, TicketSimulation)
        self.assertIsNone(market.raw_probability)
        self.assertIn("missing_team_identity", market.warnings)
        self.assertEqual(prediction.market("Home Win"), prediction.market_probabilities[0])

    def test_contracts_serialize_for_product_layers(self):
        home = TeamStrengthSnapshot(team_id="home-1", team_name="Home FC", elo=1520.5)
        diagnostics = PredictionDiagnostics(
            data_quality="medium",
            model_sources=("fixture_features",),
            warnings=("partial_recent_form",),
            metadata={"sample": 12},
        )
        calibration = CalibrationResult(raw_probability=0.74, calibrated_probability=0.69, method="isotonic")

        self.assertEqual(home.to_dict()["team_name"], "Home FC")
        self.assertEqual(diagnostics.to_dict()["metadata"]["sample"], 12)
        self.assertEqual(calibration.to_dict()["method"], "isotonic")
