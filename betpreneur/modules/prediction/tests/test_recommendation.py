from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    MarketProbability,
    PredictionDiagnostics,
    ValueAssessment,
    score_recommendation,
)


class RecommendationScoreTests(SimpleTestCase):
    def test_balances_probability_market_fit_and_value(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Over 2.5",
            calibrated_probability=0.72,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "total_goals", "market_support_level": "strong"}
            ),
        )
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 2.5",
            calibrated_probability=0.72,
            value_score=18,
        )

        score = score_recommendation(probability, value, market_fit_score=80)

        self.assertEqual(score.calibrated_probability_score, 72)
        self.assertEqual(score.market_fit_score, 80)
        self.assertEqual(score.value_score, 68)
        self.assertEqual(score.recommendation_score, 73.4)
        self.assertEqual(score.total_penalty, 0.0)

    def test_high_probability_short_odds_and_weak_market_do_not_auto_win(self):
        over_15 = MarketProbability(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.83,
            data_quality="limited",
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "total_goals", "market_support_level": "weak"}
            ),
        )
        weak_value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.83,
            value_score=-20,
            sample_size_penalty=8,
            league_uncertainty_penalty=7,
        )
        balanced = MarketProbability(
            fixture_id="fixture-2",
            market="Over 2.5",
            calibrated_probability=0.70,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "total_goals", "market_support_level": "strong"}
            ),
        )
        better_value = ValueAssessment(
            fixture_id="fixture-2",
            market="Over 2.5",
            calibrated_probability=0.70,
            value_score=15,
        )

        over_15_score = score_recommendation(over_15, weak_value, market_fit_score=45)
        balanced_score = score_recommendation(balanced, better_value, market_fit_score=76)

        self.assertLess(over_15_score.recommendation_score, balanced_score.recommendation_score)
        self.assertIn("weak_market_penalty", over_15_score.warnings)
        self.assertIn("uncertainty_penalty", over_15_score.warnings)
        self.assertIn("watchlist_market_family", over_15_score.warnings)

    def test_watchlist_markets_require_stronger_proof_before_ranking(self):
        watched = MarketProbability(
            fixture_id="fixture-1",
            market="Home or Away",
            calibrated_probability=0.78,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "double_chance", "market_support_level": "strong"}
            ),
        )
        ordinary = MarketProbability(
            fixture_id="fixture-2",
            market="Under 3.5",
            calibrated_probability=0.78,
            data_quality="strong",
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "total_goals", "market_support_level": "strong"}
            ),
        )
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Home or Away",
            calibrated_probability=0.78,
            available_odds=1.55,
            ev=0.209,
            value_score=20,
            diagnostics=PredictionDiagnostics(metadata={"sample_size": 220}),
        )

        watched_score = score_recommendation(watched, value, market_fit_score=82)
        ordinary_score = score_recommendation(ordinary, value, market_fit_score=82)

        self.assertLess(watched_score.recommendation_score, ordinary_score.recommendation_score)
        self.assertIn("watchlist_market_family", watched_score.warnings)
        self.assertEqual(watched_score.diagnostics.metadata["watchlist_penalty"], 6.0)

    def test_double_chance_12_watchlist_uses_real_market_taxonomy(self):
        markets = ("DC:12", "DC: 12", "12", "Double Chance 12", "DC: 12 1UP")

        for market in markets:
            with self.subTest(market=market):
                probability = MarketProbability(
                    fixture_id="fixture-1",
                    market=market,
                    calibrated_probability=0.78,
                    data_quality="strong",
                    diagnostics=PredictionDiagnostics(
                        metadata={
                            "market_family": "double_chance",
                            "market_support_level": "strong",
                        }
                    ),
                )

                score = score_recommendation(probability, market_fit_score=82)

                self.assertIn("watchlist_market_family", score.warnings)
                self.assertEqual(score.diagnostics.metadata["watchlist_penalty"], 6.0)

    def test_other_double_chance_markets_are_not_dc12_watchlisted(self):
        for market in ("DC: 1X", "DC: X2"):
            with self.subTest(market=market):
                probability = MarketProbability(
                    fixture_id="fixture-1",
                    market=market,
                    calibrated_probability=0.78,
                    data_quality="strong",
                    diagnostics=PredictionDiagnostics(
                        metadata={
                            "market_family": "double_chance",
                            "market_support_level": "strong",
                        }
                    ),
                )

                score = score_recommendation(probability, market_fit_score=82)

                self.assertNotIn("watchlist_market_family", score.warnings)
                self.assertEqual(score.diagnostics.metadata["watchlist_penalty"], 0.0)

    def test_short_odds_estimated_odds_and_low_sample_high_ev_are_watchlisted(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.82,
            data_quality="medium",
            diagnostics=PredictionDiagnostics(metadata={"market_family": "total_goals"}),
        )
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.82,
            available_odds=1.24,
            ev=0.10,
            value_score=16,
            pricing_warnings=("odds_source_penalty",),
            diagnostics=PredictionDiagnostics(metadata={"estimated_odds": True, "sample_size": 12}),
        )

        score = score_recommendation(probability, value, market_fit_score=82)

        self.assertIn("watchlist_market_family", score.warnings)
        self.assertIn("very_short_odds_watchlist", score.warnings)
        self.assertIn("estimated_odds_watchlist", score.warnings)
        self.assertIn("low_sample_high_ev_watchlist", score.warnings)
        self.assertEqual(score.diagnostics.metadata["watchlist_penalty"], 25.0)

    def test_explicit_penalties_are_subtracted(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Home Win",
            calibrated_probability=0.68,
            data_quality="medium",
            diagnostics=PredictionDiagnostics(metadata={"market_family": "match_result"}),
        )

        score = score_recommendation(
            probability,
            market_fit_score=70,
            uncertainty_penalty=5,
            weak_market_penalty=4,
            correlation_penalty=3,
            stale_data_penalty=2,
        )

        self.assertEqual(score.total_penalty, 14)
        self.assertEqual(score.recommendation_score, 50.1)
        self.assertIn("correlation_penalty", score.warnings)
        self.assertIn("stale_data_penalty", score.warnings)

    def test_missing_probability_returns_no_score(self):
        probability = MarketProbability(
            fixture_id="fixture-1",
            market="Away Win",
            calibrated_probability=None,
            data_quality="unavailable",
        )

        score = score_recommendation(probability, market_fit_score=80)

        self.assertIsNone(score.recommendation_score)
        self.assertIsNone(score.calibrated_probability_score)
