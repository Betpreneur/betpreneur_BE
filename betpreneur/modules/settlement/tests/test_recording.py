"""Settlement must be re-runnable but never concurrent."""
from datetime import date

from django.core.cache import cache
from django.test import TestCase

from betpreneur.modules.settlement.models import SettlementRun
from betpreneur.modules.settlement.services.recording import recorded
from betpreneur.platform.tasks.idempotency import run_once

DAY = date(2026, 8, 25)


class RecordedSettlementTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_successful_run_returns_the_payload_and_records_it(self):
        result = recorded(SettlementRun.Scope.PICKS, DAY, lambda: {"settled": 3})

        self.assertEqual(result, {"settled": 3})
        run = SettlementRun.objects.get()
        self.assertEqual(run.scope, SettlementRun.Scope.PICKS)
        self.assertEqual(run.status, SettlementRun.Status.SUCCESS)
        self.assertEqual(run.summary, {"settled": 3})
        self.assertIsNotNone(run.finished_at)

    def test_the_same_date_can_be_settled_again_later(self):
        # Fixtures finish late, so a second sequential attempt must be allowed.
        recorded(SettlementRun.Scope.PICKS, DAY, lambda: {"settled": 1})
        second = recorded(SettlementRun.Scope.PICKS, DAY, lambda: {"settled": 2})

        self.assertEqual(second, {"settled": 2})
        self.assertEqual(SettlementRun.objects.count(), 2)
        self.assertEqual(
            list(SettlementRun.objects.values_list("status", flat=True)),
            [SettlementRun.Status.SUCCESS, SettlementRun.Status.SUCCESS],
        )

    def test_a_concurrent_attempt_is_skipped_not_run(self):
        ran = []
        with run_once("settlement", SettlementRun.Scope.PICKS, DAY.isoformat()):
            result = recorded(
                SettlementRun.Scope.PICKS, DAY, lambda: ran.append(1) or {"settled": 9}
            )

        self.assertEqual(ran, [], "the work must not run while the lock is held")
        self.assertEqual(result["status"], "skipped")
        run = SettlementRun.objects.get()
        self.assertEqual(run.status, SettlementRun.Status.SKIPPED)

    def test_picks_and_slips_do_not_block_each_other(self):
        ran = []
        with run_once("settlement", SettlementRun.Scope.PICKS, DAY.isoformat()):
            recorded(SettlementRun.Scope.SLIPS, DAY, lambda: ran.append("slips") or {})

        self.assertEqual(ran, ["slips"], "a different scope must be free to run")

    def test_different_dates_do_not_block_each_other(self):
        ran = []
        with run_once("settlement", SettlementRun.Scope.PICKS, DAY.isoformat()):
            recorded(SettlementRun.Scope.PICKS, date(2026, 8, 26), lambda: ran.append("d2") or {})

        self.assertEqual(ran, ["d2"])

    def test_a_failure_is_recorded_and_re_raised(self):
        def explodes():
            raise RuntimeError("provider down")

        with self.assertRaises(RuntimeError):
            recorded(SettlementRun.Scope.SLIPS, DAY, explodes)

        run = SettlementRun.objects.get()
        self.assertEqual(run.status, SettlementRun.Status.FAILED)
        self.assertEqual(run.error, "provider down")
        self.assertIsNotNone(run.finished_at)

    def test_the_lock_is_released_after_a_failure(self):
        def explodes():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            recorded(SettlementRun.Scope.PICKS, DAY, explodes)

        # A failed attempt must not wedge the date.
        self.assertEqual(recorded(SettlementRun.Scope.PICKS, DAY, lambda: {"ok": True}), {"ok": True})
