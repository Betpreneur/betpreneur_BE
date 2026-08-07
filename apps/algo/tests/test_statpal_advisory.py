from django.test import SimpleTestCase, TestCase

from apps.algo.market_taxonomy import describe_market
from apps.algo.models import ProviderPlayerMap
from apps.algo.statpal_advisory import statpal_market_advisory


PLAYER_PAYLOAD = {
    "player": {
        "id": "2891848",
        "name": "Florian Wirtz",
        "position": "Midfielder",
        "team": "Liverpool",
        "team_id": "2341082",
        "club_league_statistics": {
            "club": [
                {
                    "team_id": "2341082",
                    "team_name": "Liverpool",
                    "league": "Premier League",
                    "season": "2025/2026",
                    "minutes_played": "2388",
                    "appearances": "33",
                    "starting_lineups": "29",
                    "assists": "3",
                    "goals": "5",
                    "shots_on_target": "18",
                    "shots_total": "32",
                    "yellowcards": "1",
                    "redcards": "0",
                    "saves": "",
                    "rating": "6.796969",
                }
            ]
        },
    }
}


class StatPalAdvisoryUnitTests(SimpleTestCase):
    def test_player_goal_market_uses_player_payload_rates(self):
        descriptor = describe_market("Florian Wirtz To Score")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=PLAYER_PAYLOAD)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_player_stats")
        self.assertEqual(result["evidence"]["player_name"], "Florian Wirtz")
        self.assertEqual(result["evidence"]["sample_appearances"], 33)
        self.assertGreater(result["score"], 50)

    def test_player_shots_market_uses_line(self):
        descriptor = describe_market("Florian Wirtz Shots Over 1.5")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=PLAYER_PAYLOAD)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["shots_per_appearance"], 0.97)
        self.assertIn(result["status"], {"avoid", "caution", "playable", "strong"})

    def test_cards_market_is_recognized_but_requests_deeper_profiles(self):
        descriptor = describe_market("Cards Over 3.5")

        result = statpal_market_advisory.evaluate_market(descriptor)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_cards_advisory_stub")
        self.assertIn("league_card_rates", result["evidence"]["data_needed"])

    def test_injury_snapshot_penalises_over_goal_market(self):
        descriptor = describe_market("Home Team Over 0.5")
        fixture = {
            "home_recent_form": {"avg_scored": 1.4},
            "away_recent_form": {"avg_scored": 1.0},
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "injuries_suspensions": {
                        "summary": {
                            "home": {
                                "team_name": "Liverpool",
                                "to_miss_count": 4,
                                "questionable_count": 1,
                                "availability_risk": "high",
                            },
                            "away": {
                                "team_name": "Chelsea",
                                "to_miss_count": 0,
                                "questionable_count": 0,
                                "availability_risk": "low",
                            },
                        }
                    }
                },
            },
        }

        result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["statpal_snapshots_available"], True)
        self.assertLess(result["evidence"]["injury_adjustment"], 0)
        self.assertIn("team_news_affects_goal_market", result["warnings"])

    def test_injury_snapshot_can_help_under_goal_market(self):
        descriptor = describe_market("Under 3.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "injuries_suspensions": {
                        "summary": {
                            "home": {"team_name": "A", "to_miss_count": 2, "questionable_count": 0},
                            "away": {"team_name": "B", "to_miss_count": 2, "questionable_count": 0},
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture)

        self.assertEqual(result["basis"], "statpal_fixture_context")
        self.assertGreater(result["evidence"]["injury_adjustment"], 0)
        self.assertIn("team_news_affects_goal_market", result["warnings"])

    def test_player_market_uses_fixture_injury_snapshot(self):
        descriptor = describe_market("Florian Wirtz To Score")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "injuries_suspensions": {
                        "summary": {
                            "home": {"team_name": "Liverpool", "to_miss_count": 3, "questionable_count": 1},
                            "away": {"team_name": "Chelsea", "to_miss_count": 0, "questionable_count": 0},
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture, statpal_payload=PLAYER_PAYLOAD)

        self.assertLess(result["evidence"]["injury_adjustment"], 0)
        self.assertIn("player_team_availability_risk", result["warnings"])


class StatPalAdvisoryMappingTests(TestCase):
    def test_player_market_can_use_learned_provider_payload(self):
        ProviderPlayerMap.objects.create(
            provider="statpal",
            provider_player_id="2891848",
            provider_player_name="Florian Wirtz",
            provider_player_normalized="florian wirtz",
            payload=PLAYER_PAYLOAD,
        )

        result = statpal_market_advisory.evaluate_market("Florian Wirtz To Score")

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["player_id"], "2891848")

    def test_missing_player_payload_returns_needs_data(self):
        result = statpal_market_advisory.evaluate_market("Unknown Player To Score")

        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "needs_data")
        self.assertIn("player_stats_missing", result["warnings"])
