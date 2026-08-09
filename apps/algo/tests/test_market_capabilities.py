from django.test import SimpleTestCase

from apps.algo.market_capabilities import market_capability_service


class MarketCapabilityTests(SimpleTestCase):
    def test_goal_market_with_good_snapshot_coverage_is_full_supported(self):
        capability = market_capability_service.assess(
            "Over 2.5",
            statpal_context={
                "market_snapshot_plan": {
                    "snapshot_types": ["predictions", "detailed_stats", "prematch_odds", "injuries_suspensions"],
                    "missing_snapshot_types": [],
                    "coverage_percent": 100,
                },
                "snapshots": {
                    "predictions": {},
                    "detailed_stats": {},
                    "prematch_odds": {},
                    "injuries_suspensions": {},
                },
            },
        )

        self.assertTrue(capability.scoreable)
        self.assertEqual(capability.support_level, "full")
        self.assertEqual(capability.data_quality, "strong")
        self.assertEqual(capability.confidence_cap, 88)

    def test_cards_market_is_capped_when_snapshot_coverage_is_limited(self):
        capability = market_capability_service.assess(
            "Cards Over 3.5",
            statpal_context={
                "market_snapshot_plan": {
                    "snapshot_types": ["detailed_stats", "lineups", "prematch_odds", "injuries_suspensions"],
                    "missing_snapshot_types": ["lineups", "prematch_odds", "injuries_suspensions"],
                    "coverage_percent": 25,
                },
                "snapshots": {"detailed_stats": {}},
            },
        )

        self.assertTrue(capability.scoreable)
        self.assertEqual(capability.support_level, "medium")
        self.assertEqual(capability.data_quality, "limited")
        self.assertLess(capability.confidence_cap, 74)
        self.assertIn("low_statpal_coverage", capability.warnings)

    def test_player_market_is_weak_and_lineup_capped(self):
        capability = market_capability_service.assess(
            "Player To Be Booked",
            statpal_context={
                "market_snapshot_plan": {
                    "snapshot_types": ["lineups", "detailed_stats", "injuries_suspensions", "prematch_odds"],
                    "missing_snapshot_types": [],
                    "coverage_percent": 100,
                },
                "snapshots": {
                    "lineups": {},
                    "detailed_stats": {},
                    "injuries_suspensions": {},
                    "prematch_odds": {},
                },
            },
        )

        self.assertTrue(capability.scoreable)
        self.assertEqual(capability.support_level, "weak")
        self.assertEqual(capability.data_quality, "strong")
        self.assertLessEqual(capability.confidence_cap, 70)
        self.assertIn("player_market_needs_lineup_confirmation", capability.warnings)

    def test_unknown_market_is_not_scoreable(self):
        capability = market_capability_service.assess("Some Strange Market")

        self.assertFalse(capability.scoreable)
        self.assertEqual(capability.support_level, "unsupported")
        self.assertEqual(capability.data_quality, "unsupported")
        self.assertEqual(capability.confidence_cap, 0)

    def test_team_shots_on_target_is_recognized_and_scoreable(self):
        capability = market_capability_service.assess("Home Team Shots on Target Over 9.5")

        self.assertTrue(capability.scoreable)
        self.assertEqual(capability.market["family"], "team_shots_on_target")
        self.assertEqual(capability.support_level, "medium")
        self.assertGreater(capability.confidence_cap, 0)
        self.assertNotIn("unsupported_market_family", capability.warnings)
