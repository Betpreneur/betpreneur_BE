from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    MarketProbability,
    PredictionDiagnostics,
    assess_market_value,
)


class ValueLayerTests(SimpleTestCase):
    def test_value_math_uses_calibrated_probability_against_available_odds(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Over 2.5",
            raw_probability=0.82,
            calibrated_probability=0.74,
            confidence_score=74,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(metadata={"calibration_sample_count": 220}),
        )

        value = assess_market_value(probability, available_odds=1.7, odds_source="sportybet")

        self.assertEqual(value.fair_odds, 1.3514)
        self.assertEqual(value.bookmaker_implied_probability, 0.588235)
        self.assertEqual(value.edge, 0.151765)
        self.assertEqual(value.ev, 0.258)
        self.assertEqual(value.edge_score, 15.18)
        self.assertEqual(value.total_penalty, 0.0)
        self.assertEqual(value.value_score, 18.37)
        self.assertEqual(value.pricing_warning, "")
        self.assertIn("Model fair odds: 1.35.", value.explanation_facts)
        self.assertIn("Available odds: 1.70.", value.explanation_facts)
        self.assertIn("Positive edge remains after calibration.", value.explanation_facts)

    def test_estimated_odds_and_small_sample_reduce_value_score(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Home Win",
            calibrated_probability=0.62,
            data_quality="limited",
            diagnostics=PredictionDiagnostics(metadata={"calibration_sample_count": 12}),
        )

        value = assess_market_value(
            probability,
            available_odds=1.9,
            odds_source="estimated",
            estimated_odds=True,
            market_volatility=0.5,
            correlation=0.25,
            context={"season_maturity": "early"},
        )

        self.assertEqual(value.odds_source_penalty, 12.0)
        self.assertEqual(value.sample_size_penalty, 12.0)
        self.assertEqual(value.market_volatility_penalty, 5.0)
        self.assertEqual(value.league_uncertainty_penalty, 11.0)
        self.assertEqual(value.correlation_penalty, 2.0)
        self.assertLess(value.value_score, 0)
        self.assertIn("odds_source_penalty", value.pricing_warnings)
        self.assertIn("sample_size_penalty", value.pricing_warnings)

    def test_missing_odds_returns_warning_without_value_score(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Away Win",
            calibrated_probability=0.55,
            data_quality="medium",
        )

        value = assess_market_value(probability, available_odds=None, odds_source="")

        self.assertIsNone(value.bookmaker_implied_probability)
        self.assertIsNone(value.edge)
        self.assertIsNone(value.ev)
        self.assertIsNone(value.value_score)
        self.assertIn("available_odds_missing", value.pricing_warnings)
        self.assertIn("odds_source_penalty", value.pricing_warnings)

    def test_raw_probability_is_ignored_when_calibrated_probability_exists(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Over 1.5",
            raw_probability=0.9,
            calibrated_probability=0.7,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(metadata={"calibration_sample_count": 220}),
        )

        value = assess_market_value(probability, available_odds=1.5, odds_source="sportybet")

        self.assertEqual(value.calibrated_probability, 0.7)
        self.assertEqual(value.ev, 0.05)
