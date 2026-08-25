from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from betpreneur.modules.catalog.api import FixtureCache, ProviderFixtureMap
from betpreneur.modules.markets.api import can_settle_market
from betpreneur.modules.picks.api import AlgoRun, Pick
from betpreneur.modules.settlement.services.settle import SettlementService
from betpreneur.modules.slips.api import SlipReview, SlipSelection, slip_recap_payload

SETTLE_DATE = date(2026, 8, 8)


def _finished_fixture(match_id, home_goals, away_goals, *, home="Dundee", away="Aberdeen"):
    return {
        "fixture": {"id": match_id, "status": {"short": "FT"}},
        "goals": {"home": home_goals, "away": away_goals},
        "teams": {"home": {"name": home}, "away": {"name": away}},
    }


class CanSettleMarketTests(TestCase):
    def test_supported_markets_are_settleable(self):
        for market in ["Home Win", "Away Win", "Draw", "Over 2.5", "Under 3.5", "DC: 1X", "DNB Home", "First to Score H"]:
            self.assertTrue(can_settle_market(market), market)

    def test_corner_lines_are_settleable(self):
        self.assertTrue(can_settle_market("Corners Over 9.5"))
        self.assertTrue(can_settle_market("Corners Under 11.5"))

    def test_corner_market_without_a_numeric_line_is_not_settleable(self):
        self.assertFalse(can_settle_market("Corners Over many"))

    def test_unsupported_bookmaker_markets_are_not_settleable(self):
        # These all appeared on a real SportyBet slip and must never be settled as a void.
        for market in ["Over 9.5", "Cards Over 3.5", "Vitoria Guimaraes 2+", "Haller, Sebastian", ""]:
            self.assertFalse(can_settle_market(market), market)


class SettleSlipSelectionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bettor", email="bettor@example.com", password="pw"
        )
        self.review = SlipReview.objects.create(user=self.user, source=SlipReview.Source.SPORTYBET)

    def _selection(self, **kwargs):
        defaults = {
            "review": self.review,
            "submitted_match": "Dundee vs Aberdeen",
            "submitted_market": "Over 2.5",
            "settlement_market": "Over 2.5",
            "match_id": "1556634",
            "match_date": SETTLE_DATE,
        }
        return SlipSelection.objects.create(**{**defaults, **kwargs})

    def test_winning_leg_is_settled_with_score_and_result(self):
        selection = self._selection()
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 2, 1)}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.WIN)
        self.assertEqual(selection.score, "2-1")
        self.assertEqual(selection.result, "2-1")
        self.assertIsNotNone(selection.settled_at)
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["settled"], 1)

    def test_losing_leg_is_settled_as_loss(self):
        selection = self._selection()
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 1, 0)}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.LOSS)
        self.assertEqual(report["losses"], 1)

    def test_flagged_risky_losses_are_counted(self):
        self._selection(flagged_risky=True)
        self._selection(flagged_risky=False)
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 1, 0)}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        self.assertEqual(report["losses"], 2)
        self.assertEqual(report["flagged_risky_losses"], 1)

    def test_unsupported_market_is_unsettleable_not_void(self):
        selection = self._selection(submitted_market="Cards Over 3.5", settlement_market="")
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 2, 1)}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.UNSETTLEABLE)
        self.assertEqual(report["unsettleable"], 1)
        self.assertEqual(report["void"], 0)

    def test_leg_without_a_finished_fixture_stays_pending(self):
        selection = self._selection()
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.PENDING)
        self.assertEqual(report["awaiting_result"], 1)
        self.assertEqual(report["settled"], 0)

    def test_draw_no_bet_on_a_draw_is_void(self):
        selection = self._selection(settlement_market="DNB Home")
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 1, 1)}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.VOID)
        self.assertEqual(report["void"], 1)

    def test_already_settled_legs_are_not_reprocessed(self):
        self._selection(outcome=SlipSelection.Outcome.WIN, score="3-0")
        service = SettlementService()

        with mock.patch.object(service, "_finished_fixture_map") as fixture_map:
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        fixture_map.assert_not_called()
        self.assertEqual(report["considered"], 0)

    def test_statpal_cached_fixture_settles_statpal_match_id(self):
        selection = self._selection(match_id="statpal:2026080812345")
        FixtureCache.objects.create(
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            source="statpal",
            api_payload={
                "provider_match_id": "2026080812345",
                "status": "finished",
                "home_goals": 2,
                "away_goals": 1,
            },
        )

        service = SettlementService()
        with mock.patch.object(service, "_api_football_get", return_value=[]):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.WIN)
        self.assertEqual(selection.score, "2-1")
        self.assertEqual(report["settled"], 1)

    def test_statpal_cached_fixture_settles_raw_provider_match_id(self):
        selection = self._selection(match_id="2026080812345")
        FixtureCache.objects.create(
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            source="statpal",
            api_payload={
                "provider_match_id": "2026080812345",
                "status": "FT",
                "goals": {"home": 2, "away": 1},
            },
        )
        ProviderFixtureMap.objects.create(
            provider="statpal",
            provider_event_id="2026080812345",
            api_fixture_id="1556634",
            active=True,
        )

        service = SettlementService()
        with mock.patch.object(service, "_api_football_get", return_value=[]):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)

        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.WIN)
        self.assertEqual(selection.score, "2-1")
        self.assertEqual(report["settled"], 1)


class SettleDailyPickTests(TestCase):
    def test_statpal_cached_fixture_settles_daily_pick_for_public_record(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        pick = Pick.objects.create(
            run=run,
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            tier=Pick.Tier.BANKER,
            market="Over 2.5",
            confidence=72,
            odds="1.70",
            ev="0.050",
            stake="1000",
        )
        FixtureCache.objects.create(
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            source="statpal",
            api_payload={
                "provider_match_id": "2026080812345",
                "status": "finished",
                "home_goals": 2,
                "away_goals": 1,
            },
        )

        service = SettlementService()
        with mock.patch.object(service, "_api_football_get", return_value=[]):
            report = service.update_results(target_date=SETTLE_DATE)

        pick.refresh_from_db()
        self.assertEqual(pick.status, Pick.Status.WIN)
        self.assertEqual(pick.score, "2-1")
        self.assertEqual(report["updated_count"], 1)


class SlipRecapTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bettor", email="bettor@example.com", password="pw"
        )
        self.other = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.review = SlipReview.objects.create(user=self.user, source=SlipReview.Source.SPORTYBET)

    def _selection(self, outcome, *, flagged=False, review=None):
        return SlipSelection.objects.create(
            review=review or self.review,
            submitted_match="Dundee vs Aberdeen",
            submitted_market="Over 2.5",
            settlement_market="Over 2.5",
            match_id="1556634",
            match_date=date.today(),
            outcome=outcome,
            flagged_risky=flagged,
        )

    def test_recap_counts_outcomes_and_flagged_failures(self):
        for _ in range(4):
            self._selection(SlipSelection.Outcome.WIN)
        self._selection(SlipSelection.Outcome.LOSS, flagged=True)
        self._selection(SlipSelection.Outcome.LOSS, flagged=False)
        self._selection(SlipSelection.Outcome.UNSETTLEABLE)

        payload = slip_recap_payload(self.user, days=1)

        self.assertEqual(payload["selections"]["correct"], 4)
        self.assertEqual(payload["selections"]["failed"], 2)
        self.assertEqual(payload["selections"]["unsettleable"], 1)
        self.assertEqual(payload["flagged"]["failed_and_flagged"], 1)
        self.assertEqual(payload["flagged"]["failed_and_not_flagged"], 1)
        self.assertEqual(payload["tickets"], 1)
        self.assertIn("4 of 6 settled selections were correct", payload["message"])

    def test_unsettleable_legs_are_excluded_from_hit_rates(self):
        self._selection(SlipSelection.Outcome.WIN)
        self._selection(SlipSelection.Outcome.UNSETTLEABLE)
        self._selection(SlipSelection.Outcome.PENDING)

        payload = slip_recap_payload(self.user, days=1)

        self.assertEqual(payload["selections"]["settled"], 1)
        self.assertEqual(payload["flagged"]["unflagged_hit_rate_percent"], 100.0)

    def test_flagged_and_unflagged_hit_rates_are_reported_separately(self):
        self._selection(SlipSelection.Outcome.LOSS, flagged=True)
        self._selection(SlipSelection.Outcome.WIN, flagged=True)
        self._selection(SlipSelection.Outcome.WIN, flagged=False)
        self._selection(SlipSelection.Outcome.WIN, flagged=False)

        payload = slip_recap_payload(self.user, days=1)

        self.assertEqual(payload["flagged"]["flagged_hit_rate_percent"], 50.0)
        self.assertEqual(payload["flagged"]["unflagged_hit_rate_percent"], 100.0)

    def test_recap_is_scoped_to_the_requesting_user(self):
        other_review = SlipReview.objects.create(user=self.other, source=SlipReview.Source.SPORTYBET)
        self._selection(SlipSelection.Outcome.WIN, review=other_review)

        payload = slip_recap_payload(self.user, days=1)

        self.assertEqual(payload["selections"]["total"], 0)
        self.assertEqual(payload["tickets"], 0)

    def test_recap_without_settled_legs_says_so(self):
        self._selection(SlipSelection.Outcome.PENDING)

        payload = slip_recap_payload(self.user, days=1)

        self.assertIsNone(payload["flagged"]["flagged_hit_rate_percent"])
        self.assertIn("have been settled", payload["message"])
