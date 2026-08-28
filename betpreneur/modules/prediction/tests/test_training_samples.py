from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.prediction.api import (
    PredictionTrainingSample,
    TrainingSampleRecord,
    record_training_sample,
)


class TrainingSampleTests(TestCase):
    def test_records_settled_training_sample(self):
        created_at = timezone.now()

        sample = record_training_sample(
            TrainingSampleRecord(
                fixture_id="1557381",
                canonical_market="Over 2.5",
                first_prediction_score=71,
                selected_status="eligible",
                published_status="published",
                odds_source="sportybet",
                real_odds="1.710",
                estimated_odds=False,
                settlement_result="win",
                league_key="england-premier-league",
                season="2026-2027",
                prediction_created_at=created_at,
                source="picks",
                source_reference="marketprediction:1",
            )
        )

        self.assertIsNotNone(sample)
        self.assertEqual(PredictionTrainingSample.objects.count(), 1)
        self.assertEqual(sample.canonical_market, "Over 2.5")
        self.assertEqual(sample.market_family, "total_goals")
        self.assertEqual(sample.real_odds, Decimal("1.710"))
        self.assertFalse(sample.estimated_odds)
        self.assertEqual(sample.prediction_created_at, created_at)

    def test_deduplicates_reruns_and_preserves_first_prediction(self):
        first_at = timezone.now() - timedelta(hours=3)
        last_at = timezone.now()
        first = record_training_sample(
            fixture_id="1557381",
            canonical_market="Over 2.5",
            first_prediction_score=66,
            last_prediction_score=66,
            settlement_result="loss",
            prediction_created_at=first_at,
            source_reference="marketprediction:old",
        )
        rerun = record_training_sample(
            fixture_id="1557381",
            canonical_market="Over 2.5",
            first_prediction_score=74,
            last_prediction_score=74,
            settlement_result="loss",
            prediction_created_at=last_at,
            published_status="published",
            source_reference="marketprediction:new",
        )

        self.assertEqual(first.id, rerun.id)
        self.assertEqual(PredictionTrainingSample.objects.count(), 1)
        rerun.refresh_from_db()
        self.assertEqual(rerun.first_prediction_score, 66)
        self.assertEqual(rerun.last_prediction_score, 74)
        self.assertEqual(rerun.prediction_created_at, first_at)
        self.assertEqual(rerun.last_prediction_created_at, last_at)
        self.assertEqual(rerun.published_status, "published")

    def test_excludes_pending_results(self):
        sample = record_training_sample(
            fixture_id="pending-1",
            canonical_market="Home Win",
            first_prediction_score=63,
            settlement_result="pending",
        )

        self.assertIsNone(sample)
        self.assertFalse(PredictionTrainingSample.objects.exists())

    def test_voids_are_kept_separate_from_pending(self):
        sample = record_training_sample(
            fixture_id="void-1",
            canonical_market="DNB Home",
            first_prediction_score=69,
            settlement_result="push",
        )

        self.assertIsNotNone(sample)
        self.assertEqual(sample.settlement_result, "push")
        self.assertTrue(sample.is_void)

    def test_estimated_odds_do_not_populate_real_odds(self):
        sample = record_training_sample(
            fixture_id="estimated-1",
            canonical_market="Away Win",
            first_prediction_score=57,
            real_odds="2.100",
            estimated_odds=True,
            settlement_result="win",
        )

        self.assertIsNone(sample.real_odds)
        self.assertTrue(sample.estimated_odds)
