from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase

from betpreneur.modules.prediction.api import (
    PredictionTrainingSample,
    WalkForwardEvaluation,
    evaluate_walk_forward,
)

UTC = ZoneInfo("UTC")


def _sample(
    fixture_id,
    *,
    kickoff,
    created_at,
    score,
    result,
    market="Over 2.5",
    family="total_goals",
    odds="1.800",
):
    return PredictionTrainingSample.objects.create(
        fixture_id=fixture_id,
        canonical_market=market,
        first_prediction_score=score,
        last_prediction_score=score,
        settlement_result=result,
        market_family=family,
        league_key="england-premier-league",
        season="2026-2027",
        kickoff=kickoff,
        prediction_created_at=created_at,
        real_odds=Decimal(odds) if odds else None,
        source="test",
    )


def _dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class WalkForwardEvaluationTests(TestCase):
    def test_evaluates_each_day_against_prior_training_only(self):
        _sample(
            "fixture-1",
            kickoff=_dt("2026-08-20T15:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=70,
            result="win",
        )
        _sample(
            "fixture-2",
            kickoff=_dt("2026-08-21T15:00:00"),
            created_at=_dt("2026-08-21T09:00:00"),
            score=60,
            result="loss",
        )
        _sample(
            "fixture-3",
            kickoff=_dt("2026-08-22T15:00:00"),
            created_at=_dt("2026-08-22T09:00:00"),
            score=80,
            result="win",
        )

        report = evaluate_walk_forward(min_train_samples=1)

        self.assertIsInstance(report, WalkForwardEvaluation)
        self.assertEqual(len(report.folds), 3)
        self.assertEqual(report.folds[0].train_samples, 0)
        self.assertEqual(report.folds[1].train_samples, 1)
        self.assertEqual(report.folds[2].train_samples, 2)
        self.assertEqual(report.total_test_samples, 3)
        self.assertEqual(report.actual_hit_rate, 0.666667)

    def test_flags_prediction_created_after_kickoff_as_leakage(self):
        _sample(
            "fixture-1",
            kickoff=_dt("2026-08-20T15:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=70,
            result="win",
        )
        _sample(
            "fixture-2",
            kickoff=_dt("2026-08-21T15:00:00"),
            created_at=_dt("2026-08-21T18:00:00"),
            score=95,
            result="win",
        )

        report = evaluate_walk_forward(min_train_samples=1)

        self.assertEqual(report.total_test_samples, 1)
        self.assertIn("prediction_created_after_kickoff", report.leakage_warnings)
        self.assertIn("prediction_created_after_kickoff", report.folds[1].leakage_warnings)

    def test_filters_by_market_family_and_league(self):
        _sample(
            "fixture-1",
            kickoff=_dt("2026-08-20T15:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=70,
            result="win",
        )
        _sample(
            "fixture-2",
            kickoff=_dt("2026-08-20T16:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=52,
            result="loss",
            market="Home Win",
            family="match_result",
        )

        report = evaluate_walk_forward(
            market_family="match_result", league_key="england-premier-league", min_train_samples=0
        )

        self.assertEqual(report.total_test_samples, 1)
        self.assertEqual(report.folds[0].average_predicted_probability, 0.52)

    def test_voids_are_counted_but_excluded_from_hit_rate(self):
        _sample(
            "fixture-1",
            kickoff=_dt("2026-08-20T15:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=70,
            result="push",
        )
        _sample(
            "fixture-2",
            kickoff=_dt("2026-08-20T16:00:00"),
            created_at=_dt("2026-08-20T09:00:00"),
            score=70,
            result="win",
        )

        report = evaluate_walk_forward(min_train_samples=0)

        self.assertEqual(report.folds[0].voids, 1)
        self.assertEqual(report.folds[0].test_samples, 1)
        self.assertEqual(report.actual_hit_rate, 1.0)
