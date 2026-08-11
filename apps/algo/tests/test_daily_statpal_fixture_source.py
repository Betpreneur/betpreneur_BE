from unittest.mock import patch
from datetime import datetime, timezone
import json

from django.test import SimpleTestCase

from apps.algo.grindalgo import algo_runner
from apps.algo.services import AlgoRunnerService


class DailyStatPalFixtureSourceTests(SimpleTestCase):
    def test_statpal_only_fixture_scores_without_api_football_fixture_calls(self):
        fixture = {
            "fixture": "Sirius vs IF Brommapojkarna",
            "hname": "Sirius",
            "aname": "IF Brommapojkarna",
            "hid": "2348384",
            "aid": "2348252",
            "match_id": "statpal:2026081032970",
            "source": "statpal_daily_cache",
            "statpal_provider_match_id": "2026081032970",
            "statpal_provider_competition_id": "3240",
            "statpal_context": {"snapshot_types": ["team_stats", "prematch_odds"]},
        }

        with (
            patch.object(algo_runner, "fetch_prediction_data") as fetch_prediction_data,
            patch.object(algo_runner, "fetch_team_recent_form") as fetch_team_recent_form,
            patch.object(algo_runner, "fetch_fixture_team_news") as fetch_fixture_team_news,
            patch.object(algo_runner, "get_api_football_odds") as get_api_football_odds,
            patch.object(algo_runner, "build_corner_profile") as build_corner_profile,
            patch.object(algo_runner, "score_fixture", return_value={"Over 1.5": 65}) as score_fixture,
        ):
            scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(fixture)

        fetch_prediction_data.assert_not_called()
        fetch_team_recent_form.assert_not_called()
        fetch_fixture_team_news.assert_not_called()
        get_api_football_odds.assert_not_called()
        build_corner_profile.assert_not_called()
        score_fixture.assert_called_once()
        self.assertEqual(confs, {"Over 1.5": 65})
        self.assertEqual(real_odds, {})
        self.assertIn("statpal_fixture_source", scored_fixture["fixture_context"]["flags"])
        self.assertIn("api_football_fixture_unavailable", scored_fixture["fixture_context"]["flags"])
        self.assertEqual(scored_fixture["team_news"]["flags"], ["api_football_fixture_unavailable"])

    def test_market_family_statpal_coverage_aggregates_market_diagnostics(self):
        service = AlgoRunnerService()

        coverage = service._market_family_statpal_coverage([
            {
                "market": "Over 2.5",
                "market_family": "total_goals",
                "insights": {
                    "statpal_market_coverage": {
                        "scoreable": True,
                        "coverage_percent": 100,
                        "missing_snapshot_types": [],
                        "warnings": [],
                    }
                },
            },
            {
                "market": "Under 3.5",
                "market_family": "total_goals",
                "insights": {
                    "statpal_market_coverage": {
                        "scoreable": True,
                        "coverage_percent": 50,
                        "missing_snapshot_types": ["predictions"],
                        "warnings": ["missing_required_snapshots"],
                    }
                },
            },
            {
                "market": "Home Win",
                "market_family": "match_result",
                "insights": {
                    "statpal_market_coverage": {
                        "scoreable": False,
                        "coverage_percent": 0,
                        "missing_snapshot_types": ["team_stats"],
                        "warnings": ["no_statpal_snapshots_available"],
                    }
                },
            },
        ])

        self.assertEqual(coverage["total_goals"]["markets"], 2)
        self.assertEqual(coverage["total_goals"]["scoreable"], 2)
        self.assertEqual(coverage["total_goals"]["full"], 1)
        self.assertEqual(coverage["total_goals"]["partial"], 1)
        self.assertEqual(coverage["total_goals"]["average_coverage_percent"], 75.0)
        self.assertEqual(coverage["total_goals"]["missing_snapshot_types"], ["predictions"])
        self.assertEqual(coverage["match_result"]["missing"], 1)

    def test_enrich_fixture_statpal_diagnostics_adds_fixture_and_market_family_payloads(self):
        service = AlgoRunnerService()
        fixture = {
            "match_id": "statpal:2026081032970",
            "fixture_context": {
                "statpal": {
                    "snapshots": {
                        "team_stats": {},
                        "predictions": {},
                        "prematch_odds": {},
                    }
                }
            },
            "markets": [
                {
                    "market": "Over 2.5",
                    "market_family": "total_goals",
                    "insights": {"market_family": "total_goals"},
                }
            ],
            "insights": {},
        }

        with patch.object(
            service,
            "_fixture_statpal_coverage",
            return_value={
                "status": "complete",
                "coverage_percent": 100,
                "present_snapshot_types": ["team_stats"],
                "missing_snapshot_types": [],
                "stale_snapshot_types": [],
                "required_snapshot_types": ["team_stats"],
                "usable_field_count": 2,
            },
        ):
            enriched = service._enrich_fixture_statpal_diagnostics(fixture)

        self.assertEqual(enriched["insights"]["statpal_fixture_coverage"]["status"], "complete")
        market_diag = enriched["markets"][0]["insights"]["statpal_market_coverage"]
        self.assertIn("coverage_percent", market_diag)
        self.assertIn("statpal_market_family_coverage", enriched["insights"])

    def test_fixture_defaults_json_safes_nested_datetimes(self):
        service = AlgoRunnerService()
        algo_run = type("Run", (), {"target_date": datetime(2026, 8, 11, tzinfo=timezone.utc).date()})()
        fixture = {
            "fixture": "Stratford vs Redditch",
            "match_id": "statpal:2026081118708",
            "fixture_context": {
                "statpal": {
                    "snapshots": {
                        "team_stats": {
                            "feed_updated": datetime(2026, 8, 11, 0, 49, tzinfo=timezone.utc),
                        }
                    }
                }
            },
            "source_payload": {
                "feed_updated": datetime(2026, 8, 11, 0, 49, tzinfo=timezone.utc),
            },
        }

        defaults = service._fixture_defaults(algo_run, fixture)

        json.dumps(defaults["fixture_context"])
        json.dumps(defaults["source_payload"])
        self.assertEqual(
            defaults["fixture_context"]["statpal"]["snapshots"]["team_stats"]["feed_updated"],
            "2026-08-11 00:49:00+00:00",
        )
