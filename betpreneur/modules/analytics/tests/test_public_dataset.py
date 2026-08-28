"""Stage 21: a published number must not be able to be quietly wrong."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betpreneur.modules.analytics.services.public_dataset import (
    MIN_REPORTABLE_SAMPLE,
    Outcome,
    Provenance,
    PublicDatasetService,
)
from betpreneur.modules.picks.api import AlgoRun, MarketPrediction, Pick

TODAY = date(2026, 8, 25)


class PublicDatasetTestCase(TestCase):
    def setUp(self):
        self.service = PublicDatasetService()
        self.run = AlgoRun.objects.create(target_date=TODAY)

    def _pick(self, *, fixture="A vs B", market="Over 2.5", match_id="m1",
              odds="2.00", stake="10", pnl="10", confidence=70,
              status=Pick.Status.WIN, days_ago=1, run=None, league="EPL",
              tier="banker", settled=True):
        match_date = TODAY - timedelta(days=days_ago)
        return Pick.objects.create(
            run=run or self.run, match_date=match_date, fixture=fixture,
            match_id=match_id, market=market, league=league, tier=tier,
            odds=Decimal(odds), ev=Decimal("0.1"), confidence=confidence,
            stake=Decimal(stake), pnl=Decimal(pnl), status=status,
            settled_at=timezone.now() if settled else None,
        )

    def _prediction(self, pick, *, odds_source="api_football"):
        return MarketPrediction.objects.create(
            run=pick.run, match_date=pick.match_date, fixture=pick.fixture,
            match_id=pick.match_id, market=pick.market, odds=pick.odds,
            odds_source=odds_source, raw_confidence=pick.confidence,
            confidence=pick.confidence,
        )

    def _real(self, **kwargs):
        pick = self._pick(**kwargs)
        self._prediction(pick)
        return pick


class SettledOnlyTests(PublicDatasetTestCase):
    def test_pending_picks_never_enter_the_dataset(self):
        self._real(match_id="settled")
        self._pick(match_id="pending", status=Pick.Status.PENDING, settled=False)

        data = self.service.build(today=TODAY)

        self.assertEqual(len(data.records), 1)
        self.assertEqual(data.records[0].pick_id, Pick.objects.get(match_id="settled").id)

    def test_excluded_pending_picks_are_counted_not_hidden(self):
        self._pick(match_id="p1", status=Pick.Status.PENDING, settled=False)
        self._pick(match_id="p2", status=Pick.Status.PENDING, settled=False)

        data = self.service.build(today=TODAY)

        self.assertEqual(data.pending_excluded, 2)
        self.assertTrue(data.as_dict()["hygiene"]["settled_only"])


class DeduplicationTests(PublicDatasetTestCase):
    def test_republished_copies_of_a_pick_collapse_to_one(self):
        later = AlgoRun.objects.create(target_date=TODAY)
        self._real(match_id="dup")
        self._real(match_id="dup", run=later)

        data = self.service.build(today=TODAY)

        self.assertEqual(len(data.records), 1)
        self.assertEqual(data.duplicates_removed, 1)

    def test_different_markets_on_one_fixture_are_not_duplicates(self):
        self._real(market="Over 2.5")
        self._real(market="BTTS")

        data = self.service.build(today=TODAY)

        self.assertEqual(len(data.records), 2)
        self.assertEqual(data.duplicates_removed, 0)


class ProvenanceTests(PublicDatasetTestCase):
    def test_real_estimated_and_unknown_are_three_distinct_answers(self):
        self._real(match_id="real")
        estimated = self._pick(match_id="est")
        self._prediction(estimated, odds_source="estimated")
        self._pick(match_id="orphan")  # no prediction row at all

        data = self.service.build(today=TODAY)
        found = {r.match_id if hasattr(r, "match_id") else r.pick_id: r.provenance
                 for r in data.records}

        self.assertEqual(sorted(str(p) for p in found.values()),
                         ["estimated", "real", "unknown"])

    def test_a_pick_with_no_prediction_row_is_unknown_never_real(self):
        self._pick(match_id="orphan")

        data = self.service.build(today=TODAY)

        self.assertEqual(data.records[0].provenance, Provenance.UNKNOWN)

    def test_a_blank_odds_source_is_unknown_never_real(self):
        pick = self._pick(match_id="blank")
        self._prediction(pick, odds_source="")

        data = self.service.build(today=TODAY)

        self.assertEqual(data.records[0].provenance, Provenance.UNKNOWN)


class HeadlineRoiTests(PublicDatasetTestCase):
    def test_headline_roi_is_computed_only_from_real_odds(self):
        # Real odds: one win at 2.00, stake 10 -> +10 on 10 staked = +100%.
        self._real(match_id="r1", pnl="10", stake="10")
        # Estimated odds: a large fabricated win that must not inflate headline.
        est = self._pick(match_id="e1", pnl="500", stake="10")
        self._prediction(est, odds_source="estimated")

        data = self.service.build(today=TODAY)

        self.assertEqual(data.headline.provenance, Provenance.REAL)
        self.assertEqual(data.headline.picks, 1)
        self.assertEqual(data.headline.roi, 100.0)

    def test_estimated_odds_roi_is_reported_but_marked_non_comparable(self):
        est = self._pick(match_id="e1", pnl="500", stake="10")
        self._prediction(est, odds_source="estimated")

        data = self.service.build(today=TODAY)
        block = next(b for b in data.by_provenance
                     if b.provenance is Provenance.ESTIMATED)

        self.assertEqual(block.roi, 5000.0)
        self.assertFalse(block.comparable)
        self.assertIn("not comparable", block.note.lower())

    def test_no_block_ever_blends_real_and_estimated(self):
        self._real(match_id="r1")
        est = self._pick(match_id="e1")
        self._prediction(est, odds_source="estimated")

        data = self.service.build(today=TODAY)

        for block in data.by_provenance:
            self.assertLessEqual(block.picks, 1,
                                 "a provenance block must hold one basis only")

    def test_a_thin_sample_is_not_presented_as_evidence(self):
        self._real(match_id="r1")

        data = self.service.build(today=TODAY)

        self.assertFalse(data.headline.reportable)
        self.assertTrue(any("below the" in c for c in data.caveats))

    def test_a_sufficient_sample_is_reportable(self):
        for i in range(MIN_REPORTABLE_SAMPLE):
            self._real(match_id=f"r{i}", fixture=f"F{i}")

        data = self.service.build(today=TODAY)

        self.assertTrue(data.headline.reportable)


class VoidTests(PublicDatasetTestCase):
    def test_a_void_leaves_the_roi_denominator(self):
        self._real(match_id="w", pnl="10", stake="10")
        self._real(match_id="v", fixture="C vs D", status=Pick.Status.VOID,
                   pnl="0", stake="10")

        data = self.service.build(today=TODAY)

        self.assertEqual(data.headline.stake, 10.0,
                         "a voided stake must not sit in the denominator")
        self.assertEqual(data.headline.roi, 100.0)

    def test_a_void_is_not_counted_as_a_loss(self):
        self._real(match_id="w", pnl="10", stake="10")
        self._real(match_id="v", fixture="C vs D", status=Pick.Status.VOID,
                   pnl="0", stake="10")

        data = self.service.build(today=TODAY)

        self.assertEqual(data.headline.losses, 0)
        self.assertEqual(data.headline.hit_rate, 100.0)

    def test_voids_are_declared_with_a_stated_policy(self):
        self._real(match_id="v", status=Pick.Status.VOID)

        data = self.service.build(today=TODAY)
        block = data.voids.as_dict()

        self.assertEqual(block["voids"], 1)
        self.assertEqual(block["void_rate"], 100.0)
        self.assertIn("returns the stake", block["policy"])
        self.assertTrue(any("voided" in c for c in data.caveats))

    def test_voids_still_appear_as_published_records(self):
        self._real(match_id="v", status=Pick.Status.VOID)

        data = self.service.build(today=TODAY)

        self.assertEqual(data.records[0].outcome, Outcome.VOID)
        self.assertFalse(data.records[0].counts_toward_roi)


class CalibrationTests(PublicDatasetTestCase):
    def test_stated_confidence_is_shown_against_actual_outcome(self):
        for i in range(4):
            self._real(match_id=f"w{i}", fixture=f"F{i}", confidence=75,
                       status=Pick.Status.WIN)
        for i in range(4):
            self._real(match_id=f"l{i}", fixture=f"G{i}", confidence=75,
                       status=Pick.Status.LOSS, pnl="-10")

        data = self.service.build(today=TODAY)
        band = next(b for b in data.calibration if b.lower == 70)

        self.assertEqual(band.settled, 8)
        self.assertEqual(band.actual, 50.0)
        self.assertEqual(band.stated, 75.0)
        self.assertEqual(band.drift, -25.0,
                         "a band claiming 75% that lands 50% must show the gap")

    def test_calibration_excludes_voids_rather_than_scoring_them(self):
        self._real(match_id="w", confidence=75, status=Pick.Status.WIN)
        self._real(match_id="v", fixture="C vs D", confidence=75,
                   status=Pick.Status.VOID, pnl="0")

        data = self.service.build(today=TODAY)
        band = next(b for b in data.calibration if b.lower == 70)

        self.assertEqual(band.settled, 1)
        self.assertEqual(band.actual, 100.0)

    def test_an_empty_band_reports_none_not_zero(self):
        data = self.service.build(today=TODAY)
        band = next(b for b in data.calibration if b.lower == 90)

        self.assertIsNone(band.actual, "an empty band must not read as 0% hit rate")
        self.assertIsNone(band.drift)


class FreezeTests(PublicDatasetTestCase):
    def test_every_dataset_carries_a_freeze_timestamp(self):
        data = self.service.build(today=TODAY)

        self.assertTrue(data.frozen_at)
        self.assertTrue(data.as_dict()["frozen_at"])

    def test_the_same_data_produces_the_same_dataset_id(self):
        self._real(match_id="r1")

        first = self.service.build(today=TODAY)
        second = self.service.build(today=TODAY)

        self.assertEqual(first.dataset_id, second.dataset_id)
        self.assertNotEqual(first.frozen_at, second.frozen_at,
                            "the stamp moves even when the id does not")

    def test_changed_data_produces_a_different_dataset_id(self):
        self._real(match_id="r1")
        before = self.service.build(today=TODAY)

        self._real(match_id="r2", fixture="C vs D")
        after = self.service.build(today=TODAY)

        self.assertNotEqual(before.dataset_id, after.dataset_id)


class SerialisationTests(PublicDatasetTestCase):
    def test_the_payload_is_json_safe(self):
        import json

        self._real(match_id="r1")
        self._real(match_id="v", fixture="C vs D", status=Pick.Status.VOID)

        json.dumps(self.service.build(today=TODAY).as_dict())

    def test_records_can_be_omitted_for_a_summary_view(self):
        self._real(match_id="r1")

        payload = self.service.build(today=TODAY).as_dict(include_records=False)

        self.assertNotIn("records", payload)
        self.assertIn("headline", payload)


class EndpointTests(PublicDatasetTestCase):
    def test_the_dataset_is_publicly_readable(self):
        self._real(match_id="r1")

        response = self.client.get(reverse("algo-public-dataset"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("dataset_id", response.json())
        self.assertIn("frozen_at", response.json())

    def test_the_payload_states_its_basis_and_caveats(self):
        est = self._pick(match_id="e1")
        self._prediction(est, odds_source="estimated")

        payload = self.client.get(reverse("algo-public-dataset")).json()

        self.assertEqual(payload["headline"]["basis"], "real")
        self.assertTrue(payload["caveats"])

    def test_the_public_endpoint_never_exposes_the_underlying_rows(self):
        self._real(match_id="r1")

        payload = self.client.get(reverse("algo-public-dataset")).json()

        self.assertNotIn(
            "records", payload,
            "this response is cached publicly, so rows must never ride along",
        )

    def _staff_client(self):
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(
            username="staff", email="staff@example.com", password="x", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_an_admin_can_export_the_underlying_rows(self):
        self._real(match_id="r1")

        payload = self._staff_client().get(
            reverse("algo-public-dataset-export")
        ).json()

        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["odds_provenance"], "real")

    def test_the_row_export_is_closed_to_anonymous_callers(self):
        self._real(match_id="r1")

        response = self.client.get(reverse("algo-public-dataset-export"))

        self.assertIn(response.status_code, (401, 403))
