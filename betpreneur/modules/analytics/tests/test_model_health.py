"""Stage 20: the dashboard must be honest about what it does not know."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.analytics.models import StrategyActionOutcome
from betpreneur.modules.analytics.services.model_health import (
    Availability,
    ModelHealthService,
)
from betpreneur.modules.picks.api import AlgoRun, MarketPrediction, Pick

TODAY = date(2026, 8, 25)


class ModelHealthTestCase(TestCase):
    def setUp(self):
        self.service = ModelHealthService()
        self.run = AlgoRun.objects.create(target_date=TODAY)

    def _pick(
        self,
        *,
        market="Over 2.5",
        tier="banker",
        odds="1.80",
        status=Pick.Status.WIN,
        days_ago=1,
        settled=True,
        league="EPL",
    ):
        match_date = TODAY - timedelta(days=days_ago)
        return Pick.objects.create(
            run=self.run,
            match_date=match_date,
            market=market,
            tier=tier,
            league=league,
            odds=Decimal(odds),
            ev=Decimal("0.1"),
            confidence=70,
            status=status,
            settled_at=timezone.make_aware(
                timezone.datetime.combine(
                    match_date + timedelta(days=1), timezone.datetime.min.time()
                )
            )
            if settled
            else None,
        )

    def _prediction(
        self,
        *,
        raw=80,
        calibrated=70,
        status=MarketPrediction.Status.WIN,
        market="Over 2.5",
        odds_source="bookmaker",
        match_id="m1",
        fixture="Team A vs Team B",
        days_ago=1,
        run=None,
    ):
        return MarketPrediction.objects.create(
            run=run or self.run,
            match_date=TODAY - timedelta(days=days_ago),
            fixture=fixture,
            market=market,
            match_id=match_id,
            raw_confidence=raw,
            confidence=calibrated,
            odds=Decimal("1.80"),
            odds_source=odds_source,
            status=status,
        )


class AvailabilityTests(ModelHealthTestCase):
    def test_an_empty_system_reports_no_data_not_zero(self):
        report = self.service.report(today=TODAY)

        roi = report.get("roi_by_market")
        self.assertIsNone(roi.value, "no settled picks must not read as 0% ROI")
        self.assertEqual(roi.availability, Availability.NO_DATA)
        self.assertIn("roi_by_market", report.unavailable)

    def test_a_thin_sample_is_flagged_rather_than_trusted(self):
        self._pick()
        report = self.service.report(today=TODAY)

        roi = report.get("roi_by_market")
        self.assertIsNotNone(roi.value)
        self.assertEqual(roi.availability, Availability.THIN)

    def test_every_metric_declares_its_source(self):
        report = self.service.report(today=TODAY)
        for metric in report.metrics:
            with self.subTest(metric=metric.key):
                self.assertTrue(metric.source, f"{metric.key} must say where it came from")


class RoiTests(ModelHealthTestCase):
    def test_roi_counts_one_unit_per_settled_pick(self):
        self._pick(odds="2.00", status=Pick.Status.WIN)
        self._pick(odds="2.00", status=Pick.Status.LOSS)
        report = self.service.report(today=TODAY)

        # staked 2, returned 2.00 -> break even
        self.assertEqual(report.get("roi_by_market").value, 0.0)

    def test_voids_are_excluded_not_counted_as_losses(self):
        self._pick(odds="2.00", status=Pick.Status.WIN)
        self._pick(odds="2.00", status=Pick.Status.LOSS)
        self._pick(odds="2.00", status=Pick.Status.VOID)
        report = self.service.report(today=TODAY)

        roi = report.get("roi_by_market")
        self.assertEqual(roi.sample_size, 2, "a void returns the stake; it is not a loss")
        self.assertEqual(roi.value, 0.0)

    def test_roi_splits_by_tier_and_odds_band(self):
        self._pick(tier="banker", odds="1.20", status=Pick.Status.WIN)
        self._pick(tier="wild_card", odds="3.50", status=Pick.Status.LOSS)
        report = self.service.report(today=TODAY)

        tiers = {r["tier"] for r in report.get("roi_by_tier").breakdown}
        bands = {r["odds_band"] for r in report.get("roi_by_odds_band").breakdown}
        self.assertEqual(tiers, {"banker", "wild_card"})
        self.assertEqual(bands, {"1.00-1.29", "3.00+"})


class CalibrationTests(ModelHealthTestCase):
    def test_overconfidence_shows_as_a_negative_gap(self):
        # stated 80-90 (midpoint 85), actual 50%. Distinct match_ids because
        # (run, match_id, fixture, market) is unique.
        self._prediction(raw=85, calibrated=85, status=MarketPrediction.Status.WIN, match_id="m1")
        self._prediction(raw=85, calibrated=85, status=MarketPrediction.Status.LOSS, match_id="m2")
        report = self.service.report(today=TODAY)

        raw = report.get("raw_probability_vs_actual")
        self.assertLess(raw.value, 0, "claiming 85% and hitting 50% is overconfidence")
        self.assertEqual(raw.breakdown[0]["band"], "80-90")

    def test_raw_and_calibrated_are_reported_separately(self):
        self._prediction(raw=85, calibrated=65, status=MarketPrediction.Status.LOSS)
        report = self.service.report(today=TODAY)

        self.assertNotEqual(
            report.get("raw_probability_vs_actual").breakdown[0]["band"],
            report.get("calibrated_probability_vs_actual").breakdown[0]["band"],
            "calibration must be measurable separately from the raw model",
        )


class OddsQualityTests(ModelHealthTestCase):
    def test_estimated_odds_are_separated_from_real_odds(self):
        self._prediction(odds_source="bookmaker", match_id="m1")
        self._prediction(odds_source="estimated", match_id="m2")
        self._prediction(odds_source="", match_id="m3")
        report = self.service.report(today=TODAY)

        self.assertAlmostEqual(report.get("real_odds_coverage").value, 33.33, places=1)
        self.assertAlmostEqual(report.get("estimated_odds_usage").value, 66.67, places=1)


class HygieneTests(ModelHealthTestCase):
    def test_a_rerun_of_the_same_fixture_market_counts_as_duplication(self):
        """Within a run the DB forbids it; across runs it is the real risk."""
        second_run = AlgoRun.objects.create(target_date=TODAY - timedelta(days=1))
        self._prediction(match_id="m1", market="Over 2.5")
        self._prediction(match_id="m1", market="Over 2.5", run=second_run)
        self._prediction(match_id="m2", market="Over 2.5")
        report = self.service.report(today=TODAY)

        duplicates = report.get("duplicate_prediction_rate")
        self.assertAlmostEqual(duplicates.value, 33.33, places=1)

    def test_reused_match_ids_on_different_fixtures_are_not_duplicates(self):
        second_run = AlgoRun.objects.create(target_date=TODAY - timedelta(days=1))
        self._prediction(match_id="provider-reused", fixture="Alpha vs Beta")
        self._prediction(
            match_id="provider-reused",
            fixture="Gamma vs Delta",
            run=second_run,
        )
        report = self.service.report(today=TODAY)

        duplicates = report.get("duplicate_prediction_rate")
        self.assertEqual(duplicates.value, 0.0)
        self.assertIn("0 rerun rows across 2 fixture/market pairs", duplicates.note)

    def test_blank_match_ids_use_fixture_text_for_duplicate_grouping(self):
        second_run = AlgoRun.objects.create(target_date=TODAY - timedelta(days=1))
        self._prediction(match_id="", fixture="Alpha vs Beta")
        self._prediction(match_id="", fixture="Gamma vs Delta", run=second_run)
        self._prediction(match_id="", fixture="Alpha vs Beta", run=second_run)
        report = self.service.report(today=TODAY)

        duplicates = report.get("duplicate_prediction_rate")
        self.assertAlmostEqual(duplicates.value, 33.33, places=1)
        self.assertIn("1 rerun rows across 2 fixture/market pairs", duplicates.note)

    def test_settlement_lag_is_measured_in_hours(self):
        self._pick(days_ago=2)
        report = self.service.report(today=TODAY)

        self.assertEqual(report.get("settlement_lag").value, 24.0)


class StrategyTests(ModelHealthTestCase):
    def _action_outcome(self, action, roi_delta, key="Over 1.5"):
        # (decision_date, scope, action, key) is unique, so vary the key.
        return StrategyActionOutcome.objects.create(
            decision_date=TODAY - timedelta(days=10),
            evaluated_from=TODAY - timedelta(days=9),
            evaluated_to=TODAY - timedelta(days=2),
            scope=StrategyActionOutcome.Scope.MARKET,
            action=action,
            key=key,
            market=key,
            sample_size=40,
            roi_delta=roi_delta,
        )

    def test_promotion_that_changes_nothing_reads_as_noise(self):
        self._action_outcome(StrategyActionOutcome.Action.PROMOTE, 0.1, key="Over 1.5")
        self._action_outcome(StrategyActionOutcome.Action.PROMOTE, -0.1, key="Over 2.5")
        report = self.service.report(today=TODAY)

        self.assertEqual(report.get("promotion_next_period_performance").value, 0.0)

    def test_suppression_is_judged_by_what_happened_after(self):
        self._action_outcome(StrategyActionOutcome.Action.SUPPRESS, -12.0)
        report = self.service.report(today=TODAY)

        metric = report.get("market_suppression_effectiveness")
        self.assertEqual(metric.value, -12.0)
        self.assertIn("suppressed markets did worse", metric.note)


class SerialisationTests(ModelHealthTestCase):
    def test_the_report_serialises_for_an_endpoint(self):
        self._pick()
        payload = self.service.report(today=TODAY).to_dict()

        self.assertEqual(payload["window_days"], 30)
        self.assertTrue(payload["metrics"])
        self.assertIn("unavailable", payload)
        keys = {m["key"] for m in payload["metrics"]}
        self.assertEqual(len(keys), 12, "Stage 20 specifies twelve metrics")


class EndpointTests(ModelHealthTestCase):
    """The dashboard is internal: it exposes calibration gaps and per-market
    ROI, which is not something a bettor should be able to read."""

    def _client(self, *, staff):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(
            username="staff" if staff else "punter",
            email=("staff" if staff else "punter") + "@example.com",
            password="x",
            is_staff=staff,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_a_normal_user_cannot_read_model_health(self):
        self.assertEqual(self._client(staff=False).get("/api/algo/model-health/").status_code, 403)

    def test_staff_get_every_metric_with_its_availability(self):
        self._pick()
        response = self._client(staff=True).get("/api/algo/model-health/")

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(len(body["metrics"]), 12)
        self.assertTrue(all("availability" in m for m in body["metrics"]))

    def test_the_window_is_configurable_and_bounded(self):
        client = self._client(staff=True)
        self.assertEqual(client.get("/api/algo/model-health/?days=7").json()["window_days"], 7)
        self.assertEqual(client.get("/api/algo/model-health/?days=9999").json()["window_days"], 365)
        self.assertEqual(client.get("/api/algo/model-health/?days=junk").json()["window_days"], 30)
