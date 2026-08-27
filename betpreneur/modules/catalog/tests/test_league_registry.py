from django.test import SimpleTestCase

from betpreneur.modules.catalog.api import (
    team_intelligence_league_ids,
    team_intelligence_leagues,
    team_intelligence_registry_payload,
)


class TeamIntelligenceLeagueRegistryTests(SimpleTestCase):
    def test_top_european_registry_has_expected_shape(self):
        leagues = team_intelligence_leagues()

        self.assertEqual(len(leagues), 10)
        self.assertEqual(leagues[0].key, "england-premier-league")
        self.assertEqual(leagues[0].api_football_league_id, "39")
        self.assertEqual(leagues[0].statpal_league_id, "3037")
        self.assertEqual(leagues[-1].key, "scotland-premiership")
        self.assertEqual(leagues[-1].api_football_league_id, "179")
        self.assertEqual({league.current_season for league in leagues}, {"2026-2027"})
        self.assertEqual({league.previous_season for league in leagues}, {"2025-2026"})
        self.assertTrue(all(league.active for league in leagues))

    def test_provider_id_helpers_only_return_known_ids(self):
        self.assertEqual(
            team_intelligence_league_ids("api_football"),
            {"39", "40", "61", "78", "88", "94", "135", "140", "144", "179"},
        )
        self.assertEqual(
            team_intelligence_league_ids("statpal"),
            {"2935", "3037", "3038", "3054", "3062", "3102", "3155", "3185", "3203", "3232"},
        )

    def test_registry_payload_includes_provider_ids(self):
        payload = team_intelligence_registry_payload()

        self.assertEqual(payload[0]["provider_ids"]["api_football"], "39")
        self.assertEqual(payload[0]["provider_ids"]["statpal"], "3037")
