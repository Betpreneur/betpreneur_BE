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
        self.assertEqual(result["evidence"]["line"], 0.5)
        self.assertEqual(result["evidence"]["player_market_family"], "player_goal")
        self.assertGreater(result["evidence"]["estimated_probability"], 10)
        self.assertGreater(result["score"], 50)

    def test_player_shots_market_uses_line(self):
        descriptor = describe_market("Florian Wirtz Shots Over 1.5")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=PLAYER_PAYLOAD)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["shots_per_appearance"], 0.97)
        self.assertEqual(result["evidence"]["line"], 1.5)
        self.assertEqual(result["evidence"]["expected_player_metric"], 0.97)
        self.assertIn(result["status"], {"avoid", "caution", "playable", "strong"})

    def test_player_assist_market_uses_assist_rate(self):
        descriptor = describe_market("Florian Wirtz Assist")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=PLAYER_PAYLOAD)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["player_market_family"], "player_assist")
        self.assertEqual(result["evidence"]["expected_player_metric"], 0.091)
        self.assertEqual(result["evidence"]["line"], 0.5)

    def test_player_card_market_uses_card_rate(self):
        descriptor = describe_market("Florian Wirtz To Be Booked")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=PLAYER_PAYLOAD)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["player_market_family"], "player_card")
        self.assertEqual(result["evidence"]["expected_player_metric"], 0.03)
        self.assertLess(result["evidence"]["estimated_probability"], 10)

    def test_player_saves_market_uses_saves_line(self):
        payload = {
            "player": {
                "id": "keeper-1",
                "name": "Test Keeper",
                "position": "Goalkeeper",
                "team": "A",
                "club_league_statistics": {
                    "club": [
                        {
                            "team_name": "A",
                            "appearances": "20",
                            "starting_lineups": "20",
                            "minutes_played": "1800",
                            "saves": "68",
                        }
                    ]
                },
            }
        }
        descriptor = describe_market("Test Keeper Saves Over 2.5")

        result = statpal_market_advisory.evaluate_market(descriptor, statpal_payload=payload)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["player_market_family"], "player_saves")
        self.assertEqual(result["evidence"]["expected_player_metric"], 3.4)
        self.assertGreater(result["evidence"]["estimated_probability"], 60)

    def test_cards_market_is_recognized_but_requests_deeper_profiles(self):
        descriptor = describe_market("Cards Over 3.5")

        result = statpal_market_advisory._evaluate_cards_market(descriptor).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_cards_advisory_stub")
        self.assertIn("league_card_rates", result["evidence"]["data_needed"])

    def test_cards_market_uses_statpal_detailed_card_profile(self):
        descriptor = describe_market("Cards Over 3.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_yellow_cards": 2.2,
                            "away_yellow_cards": 2.1,
                            "home_red_cards": 0.1,
                            "away_red_cards": 0.1,
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._evaluate_cards_market(descriptor, fixture=fixture).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_cards_market_model")
        self.assertGreater(result["evidence"]["expected_cards"], 4)
        self.assertGreater(result["evidence"]["estimated_probability"], 50)
        self.assertIn("statpal_detailed_stats", result["evidence"]["card_model_sources"])

    def test_booking_points_market_uses_card_points_profile(self):
        descriptor = describe_market("Total Booking Points", outcome_name="Over 45.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_yellow_cards": 2,
                            "away_yellow_cards": 2,
                            "home_red_cards": 0,
                            "away_red_cards": 0.3,
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._evaluate_cards_market(descriptor, fixture=fixture).to_dict()

        self.assertEqual(result["basis"], "statpal_cards_market_model")
        self.assertEqual(result["evidence"]["booking_points"], 47.5)
        self.assertGreater(result["evidence"]["estimated_probability"], 45)

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

        result = statpal_market_advisory._evaluate_team_goal_market(descriptor, fixture=fixture).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["statpal_snapshots_available"], True)
        self.assertLess(result["evidence"]["injury_adjustment"], 0)
        self.assertIn("team_news_affects_goal_market", result["warnings"])

    def test_corners_market_uses_statpal_detailed_corner_profile(self):
        descriptor = describe_market("Corners Over 8.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_corners": 5.4,
                            "away_corners": 4.8,
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._evaluate_corners_market(descriptor, fixture=fixture).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_corner_market_model")
        self.assertEqual(result["evidence"]["expected_total_corners"], 10.2)
        self.assertGreater(result["evidence"]["estimated_probability"], 60)
        self.assertIn("statpal_detailed_stats", result["evidence"]["corner_model_sources"])

    def test_team_corners_market_uses_team_side_profile(self):
        descriptor = describe_market("Home Team Corners Over 4.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_corners": 5.8,
                            "away_corners": 2.7,
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._evaluate_corners_market(descriptor, fixture=fixture).to_dict()

        self.assertEqual(descriptor.family, "team_corners")
        self.assertEqual(result["basis"], "statpal_corner_market_model")
        self.assertEqual(result["evidence"]["expected_total_corners"], 5.8)

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

        result = statpal_market_advisory._evaluate_total_goal_market(descriptor, fixture=fixture).to_dict()

        self.assertEqual(result["basis"], "statpal_goal_market_model")
        self.assertGreater(result["evidence"]["injury_adjustment"], 0)
        self.assertIn("team_news_affects_goal_market", result["warnings"])

    def test_total_goal_market_uses_expected_goals_and_line_probability(self):
        descriptor = describe_market("Over 3.5")
        fixture = {
            "fixture_context": {
                "goal_model": {"expected_total": 4.1},
            },
            "home_recent_form": {"avg_scored": 2.4},
            "away_recent_form": {"avg_scored": 1.9},
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "predictions": {
                        "summary": {
                            "expected_goals": 4.3,
                        }
                    },
                    "detailed_stats": {
                        "summary": {
                            "home_xg": 2.6,
                            "away_xg": 1.6,
                            "expected_goals": 4.2,
                        }
                    },
                },
            },
        }

        result = statpal_market_advisory._evaluate_total_goal_market(descriptor, fixture=fixture).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_goal_market_model")
        self.assertGreater(result["evidence"]["expected_total_goals"], 3.0)
        self.assertGreater(result["evidence"]["estimated_probability"], 40)
        self.assertIn(result["status"], {"caution", "playable", "strong"})

    def test_team_goal_market_uses_team_xg_and_team_side(self):
        descriptor = describe_market("Home Team Over 1.5")
        fixture = {
            "home_recent_form": {"avg_scored": 2.0},
            "away_recent_form": {"avg_conceded": 1.6},
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_xg": 2.4,
                            "away_xg": 0.9,
                        }
                    },
                },
            },
        }

        result = statpal_market_advisory._evaluate_team_goal_market(descriptor, fixture=fixture).to_dict()

        self.assertEqual(descriptor.family, "team_total_goals")
        self.assertEqual(descriptor.team, "home")
        self.assertEqual(result["basis"], "statpal_team_goal_market_model")
        self.assertEqual(result["evidence"]["team"], "home")
        self.assertEqual(result["evidence"]["expected_team_goals"], 2.4)
        self.assertGreater(result["evidence"]["estimated_probability"], 60)

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
