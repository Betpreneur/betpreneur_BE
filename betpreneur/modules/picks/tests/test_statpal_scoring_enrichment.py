from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.catalog.api import FixtureCache, FixtureSearchService
from betpreneur.modules.catalog.api import legacy_runner as algo_runner
from betpreneur.modules.picks.services.runner_service import AlgoRunnerService


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


class StatPalFixtureMappingPureTests(SimpleTestCase):
    def test_serialized_enriched_fixture_keeps_statpal_team_ids(self):
        cached = FixtureCache(
            match_date=date(2026, 8, 7),
            fixture="Norway vs England",
            home_team="Norway",
            away_team="England",
            home_team_normalized="norway",
            away_team_normalized="england",
            match_id="1581037",
            source="api_football",
            api_payload={
                "provider_competition_id": "999",
                "provider_home_team_id": "10",
                "provider_away_team_id": "20",
                "statpal_provider_match_id": "sp-100",
                "statpal_provider_competition_id": "1",
                "statpal_home_team_id": "2339730",
                "statpal_away_team_id": "2346325",
            },
        )

        fixture = FixtureSearchService()._serialize_fixture(cached, 100, "direct")

        self.assertEqual(fixture["statpal_provider_match_id"], "sp-100")
        self.assertEqual(fixture["statpal_provider_competition_id"], "1")
        self.assertEqual(fixture["statpal_home_team_id"], "2339730")
        self.assertEqual(fixture["statpal_away_team_id"], "2346325")

    def test_statpal_daily_fixture_merges_api_football_metadata_without_overwriting_statpal_ids(self):
        api_row = FixtureCache(
            match_date=date(2026, 8, 11),
            fixture="CSKA 1948 Sofia vs Panathinaikos",
            home_team="CSKA 1948 Sofia",
            away_team="Panathinaikos",
            home_team_normalized="cska 1948 sofia",
            away_team_normalized="panathinaikos",
            fixture_normalized="cska 1948 sofia vs panathinaikos",
            home_logo="https://api-football/home.png",
            away_logo="https://api-football/away.png",
            league_logo="https://api-football/league.png",
            country_flag="https://api-football/eu.svg",
            round="Qualifying Round",
            league_type="Cup",
            match_id="123456",
            source="aps_provider_lookup",
            api_payload={
                "provider_competition_id": "848",
                "provider_home_team_id": "10",
                "provider_away_team_id": "20",
            },
        )
        statpal_fixture = {
            "fixture": "CSKA 1948 Sofia vs Panathinaikos",
            "hname": "CSKA 1948 Sofia",
            "aname": "Panathinaikos",
            "home_logo": "",
            "away_logo": "",
            "league_logo": "",
            "country_flag": "",
            "match_id": "statpal:2026081139083",
            "source": "statpal_daily_cache",
            "statpal_provider_match_id": "2026081139083",
            "statpal_provider_competition_id": "20686",
            "statpal_home_team_id": "2341111",
            "statpal_away_team_id": "2342222",
            "hid": "2341111",
            "aid": "2342222",
            "code": "20686",
        }

        merged = AlgoRunnerService()._merge_api_football_enrichment(statpal_fixture, api_row, score=98.0, orientation="direct")

        self.assertEqual(merged["match_id"], "statpal:2026081139083")
        self.assertEqual(merged["hid"], "2341111")
        self.assertEqual(merged["aid"], "2342222")
        self.assertEqual(merged["code"], "20686")
        self.assertEqual(merged["statpal_provider_competition_id"], "20686")
        self.assertEqual(merged["home_logo"], "https://api-football/home.png")
        self.assertEqual(merged["away_logo"], "https://api-football/away.png")
        self.assertEqual(merged["api_football_fixture_id"], "123456")
        self.assertEqual(merged["api_football_league_id"], "848")
        self.assertEqual(merged["api_football_home_team_id"], "10")
        self.assertEqual(merged["api_football_away_team_id"], "20")
        self.assertEqual(merged["aps_id"], "123456")
        self.assertEqual(merged["provider_merge"]["primary"], "statpal")


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
                "provider_home_team_id": "2339730",
                "provider_away_team_id": "2346325",
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
        self.assertEqual(enriched[0]["statpal_home_team_id"], "2339730")
        self.assertEqual(enriched[0]["statpal_away_team_id"], "2346325")
