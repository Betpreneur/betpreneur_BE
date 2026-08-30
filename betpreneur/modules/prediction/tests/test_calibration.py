from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.prediction.api import (
    FixtureFeatureSet,
    FixturePrediction,
    GoalModelOutput,
    PredictionDiagnostics,
    ResultProbabilityOutput,
    calibrate_probability,
    evaluate_market_probability,
    record_training_sample,
)
from betpreneur.modules.prediction.models import PredictionTrainingSample


class CalibrationTests(TestCase):
    def test_isotonic_calibration_reduces_overconfident_bucket(self):
        self._seed_samples(total=40, wins=30, market="Over 2.5", score=82)

        result = calibrate_probability(
            0.82,
            market="Over 2.5",
            context={"market_family": "total_goals", "league_key": "england-premier-league"},
        )

        self.assertEqual(result.method, "isotonic")
        self.assertEqual(result.calibrated_probability, 0.75)
        self.assertEqual(result.calibration_penalty, -7.0)
        self.assertIn("raw_probability_reduced_by_calibration", result.diagnostics.warnings)

    def test_platt_calibration_is_used_for_smaller_but_usable_samples(self):
        self._seed_samples(
            total=25, wins=15, market="Home Win", score=70, market_family="match_result"
        )

        result = calibrate_probability(
            0.7, market="Home Win", context={"market_family": "match_result"}
        )

        self.assertEqual(result.method, "platt")
        self.assertLess(result.calibrated_probability, 0.7)
        self.assertLess(result.calibration_penalty, 0)

    def test_calibration_ignores_void_samples_for_fit(self):
        self._seed_samples(total=40, wins=30, market="Over 2.5", score=82)
        for index in range(10):
            record_training_sample(
                fixture_id=f"void-{index}",
                canonical_market="Over 2.5",
                first_prediction_score=82,
                settlement_result="void",
                league_key="england-premier-league",
            )

        result = calibrate_probability(
            0.82,
            market="Over 2.5",
            context={"market_family": "total_goals", "league_key": "england-premier-league"},
        )

        self.assertEqual(result.diagnostics.metadata["sample_count"], 40)
        self.assertEqual(result.calibrated_probability, 0.75)

    def test_calibration_uses_deterministic_recent_sample_window(self):
        now = timezone.now()
        rows = [
            PredictionTrainingSample(
                fixture_id=f"new-loss-{index}",
                canonical_market="Over 2.5",
                first_prediction_score=82,
                last_prediction_score=82,
                settlement_result="loss",
                market_family="total_goals",
                league_key="england-premier-league",
                prediction_created_at=now - timedelta(minutes=index),
                metadata={"data_quality": "medium", "season_maturity": "mature"},
            )
            for index in range(5000)
        ]
        rows.append(
            PredictionTrainingSample(
                fixture_id="old-win",
                canonical_market="Over 2.5",
                first_prediction_score=82,
                last_prediction_score=82,
                settlement_result="win",
                market_family="total_goals",
                league_key="england-premier-league",
                prediction_created_at=now - timedelta(days=30),
                metadata={"data_quality": "medium", "season_maturity": "mature"},
            )
        )
        PredictionTrainingSample.objects.bulk_create(rows)

        result = calibrate_probability(
            0.82,
            market="Over 2.5",
            context={"market_family": "total_goals", "league_key": "england-premier-league"},
        )

        self.assertEqual(result.method, "isotonic")
        self.assertEqual(result.diagnostics.metadata["sample_count"], 5000)
        self.assertEqual(result.calibrated_probability, 0.0)

    def test_identity_when_calibration_bucket_is_too_small(self):
        self._seed_samples(total=3, wins=2, market="Over 2.5", score=82)

        result = calibrate_probability(
            0.82, market="Over 2.5", context={"market_family": "total_goals"}
        )

        self.assertEqual(result.method, "identity")
        self.assertEqual(result.calibrated_probability, 0.82)
        self.assertIn("calibration_sample_too_small", result.diagnostics.warnings)

    def test_market_probability_uses_calibrated_probability_for_confidence(self):
        self._seed_samples(total=40, wins=30, market="Over 2.5", score=82)
        features = FixtureFeatureSet(
            fixture_id="fixture-1",
            league_key="england-premier-league",
            features={"league": {"season_maturity": {"minimum_team_matches": 14}}},
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )
        prediction = FixturePrediction(
            fixture_id="fixture-1",
            features=features,
            goals=GoalModelOutput(
                over_2_5_probability=0.82,
                diagnostics=PredictionDiagnostics(data_quality="medium"),
            ),
        )

        market = evaluate_market_probability(prediction, "Over 2.5")

        self.assertEqual(market.raw_probability, 0.82)
        self.assertEqual(market.calibrated_probability, 0.75)
        self.assertEqual(market.confidence_score, 75)
        self.assertEqual(market.diagnostics.metadata["calibration_method"], "isotonic")

    def test_low_probability_market_confidence_is_not_inverted_by_certainty(self):
        prediction = FixturePrediction(
            fixture_id="fixture-1",
            result=ResultProbabilityOutput(
                home_win=0.39,
                draw=0.27,
                away_win=0.34,
                diagnostics=PredictionDiagnostics(data_quality="limited"),
            ),
        )

        market = evaluate_market_probability(prediction, "Away Win")

        self.assertEqual(market.raw_probability, 0.34)
        self.assertEqual(market.calibrated_probability, 0.34)
        self.assertEqual(market.confidence_score, 34)

    def _seed_samples(
        self,
        *,
        total: int,
        wins: int,
        market: str,
        score: float,
        market_family: str = "total_goals",
    ) -> None:
        for index in range(total):
            record_training_sample(
                fixture_id=f"{market}-{index}",
                canonical_market=market,
                first_prediction_score=score,
                settlement_result="win" if index < wins else "loss",
                market_family=market_family,
                league_key="england-premier-league",
                metadata={"data_quality": "medium", "season_maturity": "mature"},
            )
