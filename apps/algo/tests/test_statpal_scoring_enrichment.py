from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.algo.grindalgo import algo_runner
from apps.algo.models import FixtureCache
from apps.algo.services import FixtureSearchService


def _form(avg_scored=1.2, wins=3):
    return {
        "games": 8,
        "wins": wins,
        "draws": 2,
        "avg_scored": avg_scored,
        "avg_conceded": 1.0,
        "over25_count": 3,
        "btts_count": 4,
        "clean_sheets": 2,
        "attack_str": 0.55,
        "defence_str": 0.50,
        "streak": 1,
    }


class StatPalScoringEnrichmentTests(SimpleTestCase):
    def test_statpal_expected_goals_blends_into_goal_model(self):
        fixture_context = {
            "flags": [],
            "statpal": {
                "available": True,
                "predictions": {
                    "expected_goals": 4.2,
                    "over25_percent": 78,
                    "btts_percent": 72,
                    "home_win_percent": 62,
                    "away_win_percent": 18,
                    "draw_percent": 20,
                },
            },
        }

        scores = algo_runner.score_fixture(
            _form(avg_scored=1.1, wins=4),
            _form(avg_scored=1.0, wins=2),
            {"games": 0},
            {},
            fixture_context=fixture_context,
        )

        self.assertGreater(fixture_context["goal_model"]["expected_total"], 2.1)
        self.assertEqual(fixture_context["goal_model"]["statpal_expected_goals"], 4.2)
        self.assertGreater(scores["Over 2.5"], 50)
        self.assertGreater(scores["GG / BTTS Yes"], 40)

    def test_statpal_absence_flags_adjust_result_markets(self):
        base_scores = {
            "Home Win": 75,
            "Away Win": 50,
            "Over 2.5": 72,
            "Under 3.5": 68,
        }

        adjusted = algo_runner.apply_context_adjustments(
            base_scores,
            fixture_context={"flags": ["statpal_home_absence_risk", "statpal_heavy_absences"]},
        )

        self.assertLess(adjusted["Home Win"], base_scores["Home Win"])
        self.assertGreater(adjusted["Away Win"], base_scores["Away Win"])
        self.assertLess(adjusted["Over 2.5"], base_scores["Over 2.5"])


class StatPalFixtureMappingTests(TestCase):
    def test_attach_statpal_fixture_context_matches_same_day_teams(self):
        FixtureCache.objects.create(
            match_date=date(2026, 8, 7),
            fixture="Norway vs England",
            home_team="Norway",
            away_team="England",
            home_team_normalized="norway",
            away_team_normalized="england",
            match_id="statpal:sp-100",
            source="statpal",
            api_payload={
                "provider_match_id": "sp-100",
                "provider_competition_id": "1",
            },
        )
        fixtures = [
            {
                "fixture": "Norway vs England",
                "hname": "Norway",
                "aname": "England",
                "match_id": "1581037",
                "aps_id": "1581037",
            }
        ]

        with patch.object(FixtureSearchService, "sync_statpal_daily", return_value={"synced": 0, "errors": []}):
            enriched = FixtureSearchService()._attach_statpal_fixture_context(fixtures, date(2026, 8, 7))

        self.assertEqual(enriched[0]["statpal_match_id"], "statpal:sp-100")
        self.assertEqual(enriched[0]["statpal_provider_match_id"], "sp-100")
        self.assertEqual(enriched[0]["statpal_provider_competition_id"], "1")
