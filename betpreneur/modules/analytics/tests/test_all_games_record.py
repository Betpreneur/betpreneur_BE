from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from betpreneur.modules.picks.api import AlgoRun, MarketPrediction


class AllGamesRecordTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("algo-public-all-games-record")
        self.run = AlgoRun.objects.create(
            target_date=date(2026, 9, 13),
            status=AlgoRun.Status.SUCCESS,
            result={"publish_policy": "celery_fanout_pipeline"},
        )

    def _prediction(self, *, match_id, fixture, market="Over 2.5", status=MarketPrediction.Status.WIN):
        return MarketPrediction.objects.create(
            run=self.run,
            match_date=self.run.target_date,
            fixture=fixture,
            home_team=fixture.split(" vs ")[0],
            away_team=fixture.split(" vs ")[1],
            league="Premier League",
            kickoff="15:00",
            match_id=match_id,
            market=market,
            meaning="3 or more total goals",
            raw_confidence=76,
            confidence=74,
            odds=Decimal("1.80"),
            ev=Decimal("0.050"),
            odds_source="statpal_summary",
            eligible=True,
            status=status,
            result="2-1",
            insights={
                "analysis_available": True,
                "data_status": "modelled",
                "market_family": "total_goals",
                "calibrated_probability": 0.74,
            },
        )

    def test_index_returns_available_settled_dates_and_overall_summary(self):
        self._prediction(match_id="m-win", fixture="Alpha FC vs Beta FC", status=MarketPrediction.Status.WIN)
        self._prediction(match_id="m-loss", fixture="Gamma FC vs Delta FC", status=MarketPrediction.Status.LOSS)
        self._prediction(match_id="m-void", fixture="Void FC vs Echo FC", status=MarketPrediction.Status.VOID)
        self._prediction(match_id="m-pending", fixture="Open FC vs Hold FC", status=MarketPrediction.Status.PENDING)

        payload = self.client.get(self.url).json()

        self.assertEqual(payload["overall"], {"total_games": 2, "wins": 1, "losses": 1, "win_rate": 50.0})
        self.assertEqual(
            payload["dates"],
            [{"date": "2026-09-13", "total_games": 2, "wins": 1, "losses": 1, "win_rate": 50.0}],
        )
        self.assertNotIn("games", payload)

    def test_date_detail_returns_settled_games_without_internal_fields(self):
        self._prediction(match_id="m-win", fixture="Alpha FC vs Beta FC", status=MarketPrediction.Status.WIN)
        self._prediction(match_id="m-loss", fixture="Gamma FC vs Delta FC", status=MarketPrediction.Status.LOSS)
        self._prediction(match_id="m-void", fixture="Void FC vs Echo FC", status=MarketPrediction.Status.VOID)

        payload = self.client.get(self.url, {"date": "2026-09-13"}).json()

        self.assertEqual(payload["date"], "2026-09-13")
        self.assertEqual(payload["summary"]["total_games"], 2)
        self.assertEqual({item["settlement"] for item in payload["games"]}, {"WIN", "LOST"})
        self.assertEqual(len(payload["games"]), 2)
        for game in payload["games"]:
            self.assertIn("game", game)
            self.assertIn("league", game)
            self.assertIn("top_market", game)
            self.assertIn("confidence", game)
            self.assertIn("odds", game)
            self.assertIn("settlement_detail", game)
            self.assertNotIn("run_id", game)
            self.assertNotIn("match_id", game)
            self.assertNotIn("settled_at", game)
