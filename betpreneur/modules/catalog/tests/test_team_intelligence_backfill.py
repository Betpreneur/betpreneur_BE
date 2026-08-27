from unittest.mock import patch

from django.test import SimpleTestCase

from betpreneur.modules.catalog.services.team_intelligence_backfill import (
    TeamIntelligenceBackfillService,
)


class TeamIntelligenceBackfillServiceTests(SimpleTestCase):
    @patch("betpreneur.modules.catalog.services.team_intelligence_backfill.DataCoverageTracker")
    @patch("betpreneur.modules.catalog.services.team_intelligence_backfill.MarketProfileBuilder")
    @patch("betpreneur.modules.catalog.services.team_intelligence_backfill.RecentFormBuilder")
    @patch("betpreneur.modules.catalog.services.team_intelligence_backfill.HistoricalTeamHydrator")
    def test_backfill_runs_all_steps_and_returns_monitoring(
        self,
        hydrator_cls,
        recent_cls,
        market_cls,
        coverage_cls,
    ):
        hydrator_cls.return_value.hydrate.return_value = {
            "status": "complete",
            "api_usage": {"attempted_calls": 10},
        }
        recent_cls.return_value.build.return_value = {"status": "complete", "profiles_saved": 20}
        market_cls.return_value.build.return_value = {"status": "complete", "team_profiles_saved": 30}
        coverage_cls.return_value.refresh.return_value = {"status": "complete", "fresh": 10}
        service = TeamIntelligenceBackfillService()

        with patch.object(service, "monitoring_report", return_value={"coverage_counts": {"fresh": 10}}):
            result = service.backfill(league_keys=["england-premier-league"], max_teams=2, max_matches=5)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["leagues"], ["england-premier-league"])
        self.assertEqual(result["monitoring"]["coverage_counts"]["fresh"], 10)
        hydrator_cls.return_value.hydrate.assert_called_once()
        recent_cls.return_value.build.assert_called_once()
        market_cls.return_value.build.assert_called_once()
        coverage_cls.return_value.refresh.assert_called_once()

    def test_backfill_status_is_partial_when_steps_skip_or_fail(self):
        service = TeamIntelligenceBackfillService()
        results = [
            {"status": "complete", "skipped": 18},
            {"status": "complete", "coverage_failed": 1},
            {"status": "complete", "team_profiles_saved": 0},
        ]

        self.assertEqual(service._status(results), "partial")
        warnings = service._warnings(results)
        self.assertIn("18 scopes were skipped", warnings)
        self.assertIn("1 scopes or rows failed", warnings)
        self.assertIn("no team intelligence profiles were saved", warnings)
