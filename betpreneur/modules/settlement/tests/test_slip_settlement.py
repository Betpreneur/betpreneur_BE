from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from betpreneur.modules.catalog.api import FixtureCache, ProviderFixtureMap
from betpreneur.modules.markets.api import can_settle_market
from betpreneur.modules.picks.api import AlgoFixture, AlgoRun, MarketPrediction, Pick
from betpreneur.modules.settlement.services.settle import SettlementService
from betpreneur.modules.slips.api import SlipReview, SlipSelection, slip_recap_payload

SETTLE_DATE = date(2026, 8, 8)


def _finished_fixture(match_id, home_goals, away_goals, *, home="Dundee", away="Aberdeen", actual_stats=None):
    return {
        "fixture": {"id": match_id, "status": {"short": "FT"}},
        "goals": {"home": home_goals, "away": away_goals},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "actual_stats": actual_stats or {},
    }


class FinalStatisticsTests(TestCase):
    def _fixture(self):
        fixture = _finished_fixture(123, 1, 1)
        fixture["teams"]["home"]["id"] = 10
        fixture["teams"]["away"]["id"] = 20
        return fixture

    def _stats(self):
        return [
            {"team": {"id": team_id}, "statistics": [
                {"type": "Corner Kicks", "value": corners},
                {"type": "Yellow Cards", "value": 0},
                {"type": "Red Cards", "value": 0},
                {"type": "Shots on Goal", "value": 3},
            ]}
            for team_id, corners in ((20, 5), (10, 0))
        ]

    def test_statistics_use_team_ids_and_preserve_zero_and_unknown(self):
        service = SettlementService()
        fixture = self._fixture()
        fixture["statistics"] = self._stats()
        fixture["statistics"][0]["statistics"][1]["value"] = None
        with mock.patch.object(service, "_api_football_get") as get:
            actuals = service._api_final_statistics(fixture)
        get.assert_not_called()
        self.assertEqual(actuals["home"]["corners"], 0)
        self.assertEqual(actuals["away"]["corners"], 5)
        self.assertTrue(service._missing_market_statistics("Cards Over 2.5", actuals))
        self.assertFalse(service._missing_market_statistics("Corners Over 7.5", actuals))

    def test_failed_statistics_request_stays_missing(self):
        service = SettlementService()
        with mock.patch.object(service, "_api_football_get", side_effect=RuntimeError("quota")):
            actuals = service._api_final_statistics(self._fixture())
        self.assertTrue(service._missing_market_statistics("Corners Over 7.5", actuals))

    def test_published_pick_waits_for_statistics(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        pick = Pick.objects.create(run=run, match_date=SETTLE_DATE, match_id="123",
            fixture="Dundee vs Aberdeen", tier=Pick.Tier.BANKER,
            market="Corners Over 7.5", confidence=70, odds="1.8", ev="0.05", stake="1000")
        service = SettlementService()
        fixture = self._fixture()
        with (
            mock.patch.object(service, "_finished_fixture_map", return_value={"123": fixture}),
            mock.patch.object(service, "_record_team_match_feedback"),
        ):
            report = service.update_results(target_date=SETTLE_DATE)
            pick.refresh_from_db()
            self.assertEqual(pick.status, Pick.Status.PENDING)
            self.assertIsNone(pick.settled_at)
            self.assertEqual(report["awaiting_statistics"]["picks"], 1)
            fixture["actual_stats"] = {"home": {"corners": 5}, "away": {"corners": 5}}
            service.update_results(target_date=SETTLE_DATE)
        pick.refresh_from_db()
        self.assertEqual(pick.status, Pick.Status.WIN)

    def test_extra_time_statistics_are_not_used_for_regulation_markets(self):
        fixture = self._fixture()
        fixture["fixture"]["status"]["short"] = "AET"
        fixture["statistics"] = self._stats()
        self.assertEqual(SettlementService()._api_final_statistics(fixture), {"home": {}, "away": {}})

    def test_saved_provider_link_fetches_once_across_runs_and_retries_missing_stats(self):
        match_id = "statpal:456"
        markets = ("Corners Over 7.5", "Home Team Corners Under 2.5", "Cards Under 2.5", "Shots On Target Over 6.5", "DNB Home")
        for _ in range(2):
            run = AlgoRun.objects.create(target_date=SETTLE_DATE)
            AlgoFixture.objects.create(run=run, match_date=SETTLE_DATE, match_id=match_id,
                fixture="Dundee vs Aberdeen", source_payload={"api_football_fixture_id": "123"})
            for market in markets:
                MarketPrediction.objects.create(run=run, match_date=SETTLE_DATE, match_id=match_id,
                    fixture="Dundee vs Aberdeen", market=market, confidence=70, raw_confidence=70,
                    odds="1.8", ev="0.05")
        service = SettlementService()
        calls = []
        stats = []
        def get(path, params):
            calls.append((path, params))
            return [self._fixture()] if path == "/fixtures" else stats
        with (
            mock.patch.object(service, "_api_football_get", side_effect=get),
            mock.patch.object(service, "_record_team_match_feedback"),
        ):
            service.update_results(target_date=SETTLE_DATE)
            self.assertEqual(MarketPrediction.objects.filter(status="pending").count(), 8)
            self.assertEqual(MarketPrediction.objects.filter(status="void").count(), 2)
            self.assertEqual(MarketPrediction.objects.filter(result="Awaiting final match statistics.").count(), 8)
            stats = self._stats()
            calls.clear()
            service.update_results(target_date=SETTLE_DATE)
            self.assertEqual(calls.count(("/fixtures/statistics", {"fixture": 123})), 1)
            self.assertEqual(MarketPrediction.objects.filter(status="pending").count(), 0)
            self.assertEqual(MarketPrediction.objects.filter(status="win").count(), 4)
            self.assertEqual(MarketPrediction.objects.filter(status="loss").count(), 4)
            calls.clear()
            report = service.update_results(target_date=SETTLE_DATE)
            self.assertEqual(report["internal_predictions_updated_count"], 0)
            self.assertEqual(calls, [])


class CanSettleMarketTests(TestCase):
    def test_supported_markets_are_settleable(self):
        for market in [
            "Home Win",
            "Away Win",
            "Draw",
            "Over 2.5",
            "Under 3.5",
            "Under 4.5",
            "BTTS No",
            "DC: 1X",
            "DNB Home",
            "First to Score H",
        ]:
            self.assertTrue(can_settle_market(market), market)

    def test_corner_lines_are_settleable(self):
        self.assertTrue(can_settle_market("Corners Over 9.5"))
        self.assertTrue(can_settle_market("Corners Under 11.5"))
        self.assertTrue(can_settle_market("Home Team Corners Over 2.5"))
        self.assertTrue(can_settle_market("Away Team Corners Under 5.5"))
        self.assertTrue(can_settle_market("Cards Over 3.5"))
        self.assertTrue(can_settle_market("Home Team Cards Under 2.5"))
        self.assertTrue(can_settle_market("Shots On Target Over 8.5"))
        self.assertTrue(can_settle_market("Away Team Shots On Target Under 5.5"))

    def test_corner_market_without_a_numeric_line_is_not_settleable(self):
        self.assertFalse(can_settle_market("Corners Over many"))

    def test_unsupported_bookmaker_markets_are_not_settleable(self):
        # These all appeared on a real SportyBet slip and must never be settled as a void.
        for market in ["Over 9.5", "Cards Over many", "Vitoria Guimaraes 2+", "Haller, Sebastian", ""]:
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

    def test_corner_leg_waits_then_uses_normalized_statistics(self):
        selection = self._selection(submitted_market="Corners Over 7.5", settlement_market="Corners Over 7.5")
        service = SettlementService()
        fixture = _finished_fixture(1556634, 2, 1)
        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": fixture}):
            report = service.settle_slip_selections(target_date=SETTLE_DATE)
            selection.refresh_from_db()
            self.assertEqual(report["awaiting_result"], 1)
            self.assertIsNone(selection.settled_at)
            self.assertEqual(report["void"], 0)
            fixture["actual_stats"] = {"home": {"corners": 4}, "away": {"corners": 5}}
            report = service.settle_slip_selections(target_date=SETTLE_DATE)
        selection.refresh_from_db()
        self.assertEqual(selection.outcome, SlipSelection.Outcome.WIN)
        self.assertEqual(selection.result, "9 corners")

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

    def test_under_four_five_market_prediction_is_settled(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        prediction = MarketPrediction.objects.create(
            run=run,
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="1556634",
            market="Under 4.5",
            meaning="4 or fewer total goals",
            raw_confidence=70,
            confidence=70,
            odds="1.40",
            ev="0.050",
            eligible=True,
        )

        service = SettlementService()
        with mock.patch.object(service, "_finished_fixture_map", return_value={"1556634": _finished_fixture(1556634, 2, 1)}):
            report = service.update_results(target_date=SETTLE_DATE)

        prediction.refresh_from_db()
        self.assertEqual(prediction.status, MarketPrediction.Status.WIN)
        self.assertEqual(prediction.score, "2-1")
        self.assertEqual(report["internal_predictions_updated_count"], 1)

    def test_statpal_team_corner_market_prediction_is_settled_from_actual_stats(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        prediction = MarketPrediction.objects.create(
            run=run,
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            market="Away Team Corners Over 2.5",
            meaning="Away team to finish with more than 2.5 corners",
            raw_confidence=70,
            confidence=70,
            odds="1.40",
            ev="0.050",
            eligible=True,
        )
        fixture = _finished_fixture(
            "statpal:2026080812345",
            1,
            1,
            actual_stats={
                "home": {"corners": 2},
                "away": {"corners": 5},
            },
        )

        service = SettlementService()
        with mock.patch.object(service, "_finished_fixture_map", return_value={"statpal:2026080812345": fixture}):
            report = service.update_results(target_date=SETTLE_DATE)

        prediction.refresh_from_db()
        self.assertEqual(prediction.status, MarketPrediction.Status.WIN)
        self.assertEqual(prediction.result, "5 away team corners")
        self.assertEqual(report["internal_prediction_status_counts"]["win"], 1)

    def test_statpal_card_market_prediction_is_settled_from_actual_stats(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        prediction = MarketPrediction.objects.create(
            run=run,
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            market="Cards Under 4.5",
            meaning="Match to finish with fewer than 4.5 cards",
            raw_confidence=70,
            confidence=70,
            odds="1.40",
            ev="0.050",
            eligible=True,
        )
        fixture = _finished_fixture(
            "statpal:2026080812345",
            1,
            1,
            actual_stats={
                "home": {"yellow_cards": 1, "red_cards": 0},
                "away": {"yellow_cards": 2, "red_cards": 0},
            },
        )

        service = SettlementService()
        with mock.patch.object(service, "_finished_fixture_map", return_value={"statpal:2026080812345": fixture}):
            report = service.update_results(target_date=SETTLE_DATE)

        prediction.refresh_from_db()
        self.assertEqual(prediction.status, MarketPrediction.Status.WIN)
        self.assertEqual(prediction.result, "3 cards")
        self.assertEqual(report["internal_prediction_status_counts"]["win"], 1)

    def test_statpal_shots_on_target_market_prediction_is_settled_from_actual_stats(self):
        run = AlgoRun.objects.create(target_date=SETTLE_DATE)
        prediction = MarketPrediction.objects.create(
            run=run,
            match_date=SETTLE_DATE,
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            match_id="statpal:2026080812345",
            market="Shots On Target Over 8.5",
            meaning="Match to finish with more than 8.5 shots on target",
            raw_confidence=70,
            confidence=70,
            odds="1.40",
            ev="0.050",
            eligible=True,
        )
        fixture = _finished_fixture(
            "statpal:2026080812345",
            1,
            1,
            actual_stats={
                "home": {"shots_on_target": 4},
                "away": {"shots_on_target": 6},
            },
        )

        service = SettlementService()
        with mock.patch.object(service, "_finished_fixture_map", return_value={"statpal:2026080812345": fixture}):
            report = service.update_results(target_date=SETTLE_DATE)

        prediction.refresh_from_db()
        self.assertEqual(prediction.status, MarketPrediction.Status.WIN)
        self.assertEqual(prediction.result, "10 shots on target")
        self.assertEqual(report["internal_prediction_status_counts"]["win"], 1)


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
