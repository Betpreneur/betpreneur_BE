from django.test import SimpleTestCase

from betpreneur.modules.catalog.services import legacy_runner as algo_runner
from betpreneur.modules.markets.api import (
    COUNT_MODEL_ENGINE,
    EXCLUDED_DAILY_MARKETS,
    PROVEN_DAILY_MARKETS,
    QUANTITATIVE,
    SCORE_MATRIX_ENGINE,
    build_daily_market_scores,
    daily_catalog_entry,
    daily_evaluation_route,
    daily_market_family_payload,
    daily_market_names,
    daily_markets_by_family,
    daily_odds_key_map,
    daily_scoring_market_names,
)


def _form(avg_scored=1.4, wins=4):
    return {
        "games": 8,
        "wins": wins,
        "draws": 2,
        "avg_scored": avg_scored,
        "avg_conceded": 1.0,
        "over25_count": 4,
        "btts_count": 4,
        "clean_sheets": 2,
        "attack_str": 0.55,
        "defence_str": 0.50,
        "streak": 1,
    }


class DailyMarketCatalogTests(SimpleTestCase):
    def test_catalog_exposes_enabled_daily_markets_without_excluded_double_chances(self):
        markets = daily_market_names()

        self.assertIn("Home Win", markets)
        self.assertIn("Over 2.5", markets)
        self.assertIn("GG + Over 2.5", markets)
        self.assertNotIn("DC: 1X", markets)
        self.assertNotIn("DC: X2", markets)
        self.assertEqual(EXCLUDED_DAILY_MARKETS, {"DC: 1X", "DC: X2"})

    def test_catalog_groups_markets_by_evaluator_family(self):
        grouped = daily_markets_by_family()

        self.assertIn("Home Win", grouped["match_result"])
        self.assertIn("Over 2.5", grouped["total_goals"])
        self.assertIn("GG + Over 2.5", grouped["total_btts"])
        self.assertIn("Home CS", grouped["clean_sheet"])

    def test_family_payload_includes_engine_and_capabilities(self):
        payload = daily_market_family_payload("Home Win")

        self.assertEqual(payload["market_family"], "match_result")
        self.assertEqual(payload["assessment_type"], QUANTITATIVE)
        self.assertEqual(payload["evaluation_engine"], SCORE_MATRIX_ENGINE)
        self.assertIn("team_goals_for", payload["required_capabilities"])

    def test_evaluation_route_is_registry_backed(self):
        result_route = daily_evaluation_route("Home Win")
        corner_route = daily_evaluation_route("Corners Under 12.5")

        self.assertEqual(result_route["family"], "match_result")
        self.assertEqual(result_route["engine"], SCORE_MATRIX_ENGINE)
        self.assertTrue(result_route["publishes_probability"])
        self.assertEqual(corner_route["family"], "corners_total")
        self.assertEqual(corner_route["engine"], COUNT_MODEL_ENGINE)
        self.assertEqual(corner_route["assessment_type"], QUANTITATIVE)

    def test_dynamic_corner_entry_is_supported_by_catalog(self):
        entry = daily_catalog_entry("Corners Under 12.5")
        payload = daily_market_family_payload(entry.market)

        self.assertEqual(entry.generation, "odds_line")
        self.assertEqual(payload["market_family"], "corners_total")
        self.assertEqual(payload["evaluation_engine"], COUNT_MODEL_ENGINE)
        self.assertIn("team_corners", payload["required_capabilities"])

    def test_proven_markets_are_catalog_driven(self):
        self.assertEqual(
            PROVEN_DAILY_MARKETS,
            {"First to Score H", "Over 1.5", "AH Home +0.5", "Under 3.5", "GG / BTTS Yes"},
        )

    def test_odds_keys_are_catalog_driven(self):
        self.assertEqual(
            daily_odds_key_map(),
            {
                "Home Win": "hw",
                "Away Win": "aw",
                "Draw": "d",
                "Over 1.5": "o15",
                "Under 1.5": "u15",
                "Over 2.5": "o25",
                "Under 2.5": "u25",
                "Over 3.5": "o35",
                "Under 3.5": "u35",
                "GG / BTTS Yes": "btts_yes",
                "DC: 12": "12",
            },
        )

    def test_score_builder_uses_enabled_catalog_markets_and_dynamic_lines(self):
        fixed_values = dict.fromkeys(daily_market_names(include_excluded=True), 61)
        scores = build_daily_market_scores(
            fixed_values,
            {"Corners Under 12.5": 67, "Unsupported Custom Market": 90},
        )

        self.assertEqual(set(daily_scoring_market_names()), set(daily_market_names()))
        self.assertIn("Home Win", scores)
        self.assertIn("Over 3.5", scores)
        self.assertIn("Corners Under 12.5", scores)
        self.assertNotIn("DC: 1X", scores)
        self.assertNotIn("DC: X2", scores)
        self.assertNotIn("Unsupported Custom Market", scores)

    def test_score_fixture_output_is_catalog_driven(self):
        scores = algo_runner.score_fixture(
            _form(avg_scored=1.6, wins=4),
            _form(avg_scored=1.3, wins=3),
            {"games": 0},
            {},
        )

        self.assertEqual(set(daily_market_names()), set(scores))
        self.assertIn("Over 3.5", scores)
        self.assertNotIn("DC: 1X", scores)
        self.assertNotIn("DC: X2", scores)
