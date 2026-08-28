from datetime import date
from decimal import Decimal

from django.test import TestCase

from betpreneur.modules.analytics.api import StrategyActionOutcome, evaluate_strategy_memory
from betpreneur.modules.picks.api import AlgoRun, MarketPrediction, StrategyReview


class StrategyMemoryTests(TestCase):
    def test_failed_promotion_reduces_authority(self):
        review = StrategyReview.objects.create(
            target_date=date(2026, 8, 1),
            markets_promoted=["Over 1.5"],
        )
        run = AlgoRun.objects.create(target_date=date(2026, 8, 2), status=AlgoRun.Status.SUCCESS)
        for index in range(5):
            self._prediction(
                run,
                index=index,
                market="Over 1.5",
                status=MarketPrediction.Status.LOSS,
                pnl=Decimal("-1.00"),
            )
            self._prediction(
                run,
                index=index + 20,
                market="Home Win",
                status=MarketPrediction.Status.WIN,
                pnl=Decimal("0.80"),
            )

        result = evaluate_strategy_memory(decision_date=review.target_date, evaluation_days=7)

        outcome = StrategyActionOutcome.objects.get(
            decision_date=review.target_date,
            scope=StrategyActionOutcome.Scope.MARKET,
            action=StrategyActionOutcome.Action.PROMOTE,
            key="Over 1.5",
        )
        self.assertEqual(result["outcomes"], 1)
        self.assertEqual(outcome.verdict, "failed_to_improve")
        self.assertEqual(outcome.authority_multiplier, 0.60)
        self.assertLess(outcome.roi_delta, 0)

    def test_suppression_is_validated_when_future_market_underperforms(self):
        review = StrategyReview.objects.create(
            target_date=date(2026, 8, 1),
            markets_suppressed=["DC: 12"],
        )
        run = AlgoRun.objects.create(target_date=date(2026, 8, 2), status=AlgoRun.Status.SUCCESS)
        for index in range(5):
            self._prediction(
                run,
                index=index,
                market="DC: 12",
                status=MarketPrediction.Status.LOSS,
                pnl=Decimal("-1.00"),
            )
            self._prediction(
                run,
                index=index + 20,
                market="Under 3.5",
                status=MarketPrediction.Status.WIN,
                pnl=Decimal("0.70"),
            )

        evaluate_strategy_memory(decision_date=review.target_date, evaluation_days=7)

        outcome = StrategyActionOutcome.objects.get(key="DC: 12")
        self.assertEqual(outcome.verdict, "validated")
        self.assertEqual(outcome.authority_multiplier, 1.0)
        self.assertLess(outcome.roi_delta, 0)

    def test_confidence_band_actions_are_measured_and_reruns_are_idempotent(self):
        review = StrategyReview.objects.create(
            target_date=date(2026, 8, 1),
            profile={"confidence_bands": {"70-74": {"action": "cool"}}},
        )
        run = AlgoRun.objects.create(target_date=date(2026, 8, 2), status=AlgoRun.Status.SUCCESS)
        for index in range(5):
            self._prediction(
                run,
                index=index,
                market="Over 2.5",
                confidence=72,
                status=MarketPrediction.Status.WIN,
                pnl=Decimal("0.75"),
            )

        first = evaluate_strategy_memory(decision_date=review.target_date, evaluation_days=7)
        second = evaluate_strategy_memory(decision_date=review.target_date, evaluation_days=7)

        outcome = StrategyActionOutcome.objects.get(
            decision_date=review.target_date,
            scope=StrategyActionOutcome.Scope.CONFIDENCE_BAND,
            key="70-74",
        )
        self.assertEqual(first["outcomes"], 1)
        self.assertEqual(second["outcomes"], 1)
        self.assertEqual(StrategyActionOutcome.objects.count(), 1)
        self.assertEqual(outcome.verdict, "validated")

    def test_small_future_sample_lowers_authority_until_more_results_exist(self):
        review = StrategyReview.objects.create(
            target_date=date(2026, 8, 1),
            markets_promoted=["BTTS Yes"],
        )
        run = AlgoRun.objects.create(target_date=date(2026, 8, 2), status=AlgoRun.Status.SUCCESS)
        self._prediction(
            run,
            index=1,
            market="BTTS Yes",
            status=MarketPrediction.Status.WIN,
            pnl=Decimal("0.90"),
        )

        evaluate_strategy_memory(decision_date=review.target_date, evaluation_days=7)

        outcome = StrategyActionOutcome.objects.get(key="BTTS Yes")
        self.assertEqual(outcome.verdict, "insufficient_sample")
        self.assertEqual(outcome.authority_multiplier, 0.75)

    def _prediction(
        self,
        run,
        *,
        index,
        market,
        status,
        pnl,
        confidence=70,
        league="Premier League",
    ):
        return MarketPrediction.objects.create(
            run=run,
            match_date=date(2026, 8, 2),
            fixture=f"Fixture {index}",
            league=league,
            match_id=f"match-{index}",
            market=market,
            confidence=confidence,
            raw_confidence=confidence,
            odds=Decimal("1.80"),
            pnl_simulated=pnl,
            status=status,
            published=True,
        )
