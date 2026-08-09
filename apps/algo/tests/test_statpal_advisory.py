from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.algo.market_taxonomy import MarketDescriptor, describe_market
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

    def test_injury_snapshot_alone_cannot_price_under_goal_market(self):
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

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "goal_profile_missing")
        self.assertIsNone(result["score"])
        self.assertIn("goal_profile_missing", result["warnings"])

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

    def test_first_half_total_goal_market_uses_first_half_team_history(self):
        descriptor = describe_market("1H Over 0.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "teams": [
                                {
                                    "fixture_side": "home",
                                    "sample_size": 12,
                                    "firsthalf_avg_goals_for": 0.7,
                                    "firsthalf_avg_goals_against": 0.4,
                                },
                                {
                                    "fixture_side": "away",
                                    "sample_size": 12,
                                    "firsthalf_avg_goals_for": 0.6,
                                    "firsthalf_avg_goals_against": 0.5,
                                },
                            ]
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._evaluate_total_goal_market(descriptor, fixture=fixture).to_dict()

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["period"], "first_half")
        self.assertEqual(result["basis"], "statpal_goal_market_model")
        self.assertGreater(result["evidence"]["expected_total_goals"], 0.5)
        self.assertGreater(result["evidence"]["estimated_probability"], 50)

    def test_result_or_btts_uses_statpal_score_matrix_fallback(self):
        descriptor = MarketDescriptor(
            raw="Draw or GG / BTTS Yes - Yes",
            canonical="Draw or GG / BTTS Yes - Yes",
            code="result_or_btts_draw_btts_yes",
            family="result_or_btts",
            category="Combo",
            side="draw_btts_yes",
            selection="yes",
            period="full_match",
        )
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "teams": [
                                {
                                    "fixture_side": "home",
                                    "sample_size": 14,
                                    "avg_goals_for": 1.5,
                                    "avg_goals_against": 1.1,
                                },
                                {
                                    "fixture_side": "away",
                                    "sample_size": 14,
                                    "avg_goals_for": 1.4,
                                    "avg_goals_against": 1.2,
                                },
                            ]
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._score_matrix_fallback(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_score_matrix_fallback")
        self.assertEqual(result["evidence"]["market_family"], "result_or_btts")
        self.assertGreater(result["evidence"]["estimated_probability"], 50)
        self.assertIn("score_matrix_fit_missing", result["warnings"])

    def test_match_result_uses_statpal_score_matrix_fallback(self):
        descriptor = describe_market("Home Win")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "teams": [
                                {
                                    "fixture_side": "home",
                                    "sample_size": 14,
                                    "avg_goals_for": 2.0,
                                    "avg_goals_against": 0.8,
                                },
                                {
                                    "fixture_side": "away",
                                    "sample_size": 14,
                                    "avg_goals_for": 0.9,
                                    "avg_goals_against": 1.6,
                                },
                            ]
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._score_matrix_fallback(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_score_matrix_fallback")
        self.assertEqual(result["evidence"]["market_family"], "match_result")
        self.assertGreater(result["evidence"]["estimated_probability"], 45)

    def test_snapshot_odds_payload_adds_positive_value_evidence(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "totals": [
                                                {
                                                    "line": 2.5,
                                                    "odds": [
                                                        {"name": "Over", "value": 2.0},
                                                        {"name": "Under", "value": 1.8},
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "2.20"},
        )

        self.assertGreater(result["score"], 60)
        self.assertEqual(result["evidence"]["odds_value"]["statpal_reference_odds"], 2.0)
        self.assertEqual(result["evidence"]["odds_value"]["offered_odds"], 2.2)
        self.assertEqual(result["evidence"]["odds_value"]["value_edge_pct"], 10.0)
        self.assertIn("positive_price_edge", result["warnings"])

    def test_snapshot_odds_reference_uses_median_across_bookmakers(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 1.90}]}],
                                        },
                                        {
                                            "name": "Betway",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.00}]}],
                                        },
                                        {
                                            "name": "Unibet",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.20}]}],
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "2.10"},
        )

        odds_value = result["evidence"]["odds_value"]
        self.assertEqual(odds_value["statpal_reference_odds"], 2.0)
        self.assertEqual(odds_value["statpal_reference_min_odds"], 1.9)
        self.assertEqual(odds_value["statpal_reference_max_odds"], 2.2)
        self.assertEqual(odds_value["statpal_reference_bookmaker_count"], 3)
        self.assertEqual(odds_value["statpal_reference_spread_pct"], 15.0)
        self.assertEqual(odds_value["reference_method"], "median_bookmaker_odds")
        self.assertEqual(odds_value["bookmaker"], "median_of_3")
        self.assertEqual(odds_value["reference_reliability"], "solid")

    def test_wide_odds_reference_spread_dampens_value_adjustment(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "Low",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 1.70}]}],
                                        },
                                        {
                                            "name": "Mid",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.00}]}],
                                        },
                                        {
                                            "name": "High",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.30}]}],
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "2.20"},
        )

        odds_value = result["evidence"]["odds_value"]
        self.assertEqual(odds_value["reference_reliability"], "wide")
        self.assertEqual(odds_value["statpal_reference_spread_pct"], 30.0)
        self.assertLess(result["evidence"]["odds_adjustment"], 3.5)
        self.assertIn("wide_odds_reference_spread", result["warnings"])

    def test_volatile_odds_reference_spread_warns_and_heavily_dampens_edge(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "Low",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 1.50}]}],
                                        },
                                        {
                                            "name": "Mid",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.00}]}],
                                        },
                                        {
                                            "name": "High",
                                            "totals": [{"line": 2.5, "odds": [{"name": "Over", "value": 2.70}]}],
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "2.20"},
        )

        odds_value = result["evidence"]["odds_value"]
        self.assertEqual(odds_value["reference_reliability"], "volatile")
        self.assertLess(result["evidence"]["odds_adjustment"], 1.0)
        self.assertIn("volatile_odds_reference_spread", result["warnings"])

    def test_snapshot_odds_payload_warns_when_offered_price_is_short(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "totals": [
                                                {
                                                    "line": 2.5,
                                                    "odds": [
                                                        {"name": "Over", "value": 2.0},
                                                        {"name": "Under", "value": 1.8},
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"provider_payload": {"selection": {"odds": "1.80"}}},
        )

        self.assertLess(result["score"], 60)
        self.assertEqual(result["evidence"]["odds_value"]["value_edge_pct"], -10.0)
        self.assertIn("odds_below_statpal_reference", result["warnings"])
        self.assertIn("odds_snapshot_caution", result["warnings"])

    def test_snapshot_odds_matcher_respects_first_half_corner_period(self):
        descriptor = MarketDescriptor(
            raw="1H Corners Over 4.5",
            canonical="1H Corners Over 4.5",
            code="corners_total_over_4.5",
            family="corners_total",
            category="Corners",
            selection="over",
            side="over",
            line="4.5",
            period="first_half",
        )
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 2},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Corners - Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "Decoy",
                                            "totals": [{"line": 4.5, "odds": [{"name": "Over", "value": 2.50}]}],
                                        }
                                    ],
                                },
                                {
                                    "name": "1st Half Corners - Over/Under",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "totals": [{"line": 4.5, "odds": [{"name": "Over", "value": 1.75}]}],
                                        }
                                    ],
                                },
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "1.90"},
        )

        self.assertEqual(result["evidence"]["odds_value"]["statpal_reference_odds"], 1.75)
        self.assertEqual(result["evidence"]["odds_value"]["matched_market"], "1st Half Corners - Over/Under")
        self.assertIn("positive_price_edge", result["warnings"])

    def test_snapshot_odds_matcher_supports_second_half_btts_no(self):
        descriptor = MarketDescriptor(
            raw="2H BTTS No",
            canonical="2H BTTS No",
            code="btts_no",
            family="btts",
            category="Goals",
            selection="no",
            side="no",
            period="second_half",
        )
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "2nd Half - GG/NG",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "odds": [{"name": "Yes", "value": 2.95}, {"name": "No", "value": 1.40}],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "1.20"},
        )

        self.assertEqual(result["evidence"]["odds_value"]["matched_market"], "2nd Half - GG/NG")
        self.assertEqual(result["evidence"]["odds_value"]["matched_outcome"], "No")
        self.assertIn("odds_below_statpal_reference", result["warnings"])

    def test_snapshot_odds_matcher_supports_team_clean_sheet_no(self):
        descriptor = MarketDescriptor(
            raw="Home Team Clean Sheet - No",
            canonical="Home Team Clean Sheet - No",
            code="team_clean_sheet_home_no",
            family="team_clean_sheet",
            category="Clean Sheet",
            selection="no",
            side="no",
            team="home",
            period="full_match",
        )
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "prematch_odds": {
                        "summary": {"market_count": 1},
                        "payload": {
                            "markets": [
                                {
                                    "name": "Home Team Clean Sheet",
                                    "bookmakers": [
                                        {
                                            "name": "10Bet",
                                            "odds": [{"name": "Yes", "value": 6.0}, {"name": "No", "value": 1.56}],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_odds_overlay(
            {"available": True, "score": 60, "status": "caution", "evidence": {}, "warnings": []},
            descriptor=descriptor,
            fixture=fixture,
            provider_payload={"odds": "1.70"},
        )

        self.assertEqual(result["evidence"]["odds_value"]["matched_market"], "Home Team Clean Sheet")
        self.assertEqual(result["evidence"]["odds_value"]["matched_outcome"], "No")
        self.assertGreater(result["score"], 60)

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

    def test_team_history_snapshot_adjusts_total_goals_market(self):
        descriptor = describe_market("Over 2.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "team_id": "2340835",
                            "team_name": "Arsenal",
                            "sample_size": 19,
                            "current_league": "Premier League",
                            "current_season": "2025/2026",
                            "avg_goals_for": 2.1,
                            "avg_goals_against": 1.3,
                            "avg_total_goals": 3.4,
                        }
                    }
                },
            }
        }

        result = statpal_market_advisory._apply_snapshot_context(
            60,
            descriptor=descriptor,
            fixture=fixture,
        )

        score, evidence, warnings = result
        self.assertGreater(score, 60)
        self.assertGreater(evidence["team_history_adjustment"], 0)
        self.assertEqual(evidence["team_history"]["metric"], "avg_total_goals")
        self.assertEqual(evidence["team_history"]["metric_value"], 3.4)
        self.assertIn("team_history_context_applied", warnings)

    def test_team_history_snapshot_warns_on_small_sample(self):
        descriptor = describe_market("Home Team Over 0.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "team_id": "2340835",
                            "team_name": "Arsenal",
                            "sample_size": 3,
                            "avg_goals_for": 1.8,
                        }
                    }
                },
            }
        }

        _, evidence, warnings = statpal_market_advisory._apply_snapshot_context(
            60,
            descriptor=descriptor,
            fixture=fixture,
        )

        self.assertGreater(evidence["team_history_adjustment"], 0)
        self.assertIn("small_team_stat_sample", warnings)
        self.assertIn("team_history_context_applied", warnings)

    def test_team_history_combined_summary_uses_requested_side(self):
        descriptor = describe_market("Away Team Over 0.5")
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "team_count": 2,
                            "home": {"fixture_side": "home", "team_name": "Home", "sample_size": 20, "avg_goals_for": 0.7},
                            "away": {"fixture_side": "away", "team_name": "Away", "sample_size": 20, "avg_goals_for": 1.8},
                            "teams": [
                                {"fixture_side": "home", "team_name": "Home", "sample_size": 20, "avg_goals_for": 0.7},
                                {"fixture_side": "away", "team_name": "Away", "sample_size": 20, "avg_goals_for": 1.8},
                            ],
                        }
                    }
                },
            }
        }

        score, evidence, warnings = statpal_market_advisory._apply_snapshot_context(
            60,
            descriptor=descriptor,
            fixture=fixture,
        )

        self.assertGreater(score, 60)
        self.assertEqual(evidence["team_history"]["team_name"], "Away")
        self.assertEqual(evidence["team_history"]["metric_value"], 1.8)
        self.assertIn("team_history_context_applied", warnings)

    def test_score_matrix_no_fit_falls_back_to_team_history_for_team_goals(self):
        descriptor = describe_market("Home Team Goals Over 1.5")
        fixture = {
            "hname": "Anderlecht",
            "aname": "RAAL La Louviere",
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "team_count": 2,
                            "home": {
                                "fixture_side": "home",
                                "team_name": "Anderlecht",
                                "sample_size": 20,
                                "avg_goals_for": 2.2,
                            },
                            "away": {
                                "fixture_side": "away",
                                "team_name": "RAAL La Louviere",
                                "sample_size": 20,
                                "avg_goals_for": 0.9,
                            },
                            "teams": [
                                {"fixture_side": "home", "team_name": "Anderlecht", "sample_size": 20, "avg_goals_for": 2.2},
                                {"fixture_side": "away", "team_name": "RAAL La Louviere", "sample_size": 20, "avg_goals_for": 0.9},
                            ],
                        }
                    }
                },
            },
        }

        with mock.patch(
            "apps.algo.evaluators.score_matrix_evaluator.evaluate",
            return_value={
                "available": False,
                "score": None,
                "status": "needs_data",
                "basis": "score_matrix_no_fit",
                "evidence": {},
                "warnings": ["score_matrix_fit_missing"],
            },
        ):
            result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_team_goal_market_model")
        self.assertEqual(result["evidence"]["team_goal_model_source"], "statpal_team_history")
        self.assertEqual(result["evidence"]["statpal_team_history_goals_for"], 2.2)
        self.assertIn("score_matrix_fit_missing", result["warnings"])

    def test_both_halves_total_goals_uses_period_team_history(self):
        descriptor = MarketDescriptor(
            raw="Both Halves Over 1.5 - Yes",
            canonical="Both Halves Over 1.5 - Yes",
            code="both_halves_total_goals",
            family="both_halves_total_goals",
            category="Goals",
            selection="over_yes",
            side="over_yes",
            line="1.5",
            period="match",
        )
        fixture = {
            "statpal_context": {
                "available": True,
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "team_count": 2,
                            "home": {
                                "fixture_side": "home",
                                "team_name": "Home",
                                "sample_size": 20,
                                "firsthalf_avg_goals_for": 1.1,
                                "firsthalf_avg_goals_against": 0.5,
                                "secondhalf_avg_goals_for": 1.4,
                                "secondhalf_avg_goals_against": 0.7,
                            },
                            "away": {
                                "fixture_side": "away",
                                "team_name": "Away",
                                "sample_size": 20,
                                "firsthalf_avg_goals_for": 0.6,
                                "firsthalf_avg_goals_against": 1.0,
                                "secondhalf_avg_goals_for": 0.8,
                                "secondhalf_avg_goals_against": 1.2,
                            },
                            "teams": [
                                {
                                    "fixture_side": "home",
                                    "team_name": "Home",
                                    "sample_size": 20,
                                    "firsthalf_avg_goals_for": 1.1,
                                    "firsthalf_avg_goals_against": 0.5,
                                    "secondhalf_avg_goals_for": 1.4,
                                    "secondhalf_avg_goals_against": 0.7,
                                },
                                {
                                    "fixture_side": "away",
                                    "team_name": "Away",
                                    "sample_size": 20,
                                    "firsthalf_avg_goals_for": 0.6,
                                    "firsthalf_avg_goals_against": 1.0,
                                    "secondhalf_avg_goals_for": 0.8,
                                    "secondhalf_avg_goals_against": 1.2,
                                },
                            ],
                        }
                    }
                },
            },
        }

        result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "statpal_both_halves_goal_model")
        self.assertEqual(result["assessment_type"], "quantitative_model")
        self.assertGreater(result["evidence"]["first_half_expected_goals"], 0)
        self.assertGreater(result["evidence"]["second_half_expected_goals"], 0)

    def test_team_goal_market_declines_when_expected_profile_is_zero(self):
        descriptor = describe_market("Home Team Goals Over 1.5")

        result = statpal_market_advisory._evaluate_team_goal_market(descriptor, fixture={}).to_dict()

        self.assertFalse(result["available"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["basis"], "team_goal_profile_missing")
        self.assertIn("team_goal_profile_missing", result["warnings"])

    def test_total_goal_market_declines_when_expected_profile_is_zero(self):
        descriptor = describe_market("Over 2.5")

        result = statpal_market_advisory._evaluate_total_goal_market(descriptor, fixture={}).to_dict()

        self.assertFalse(result["available"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["basis"], "goal_profile_missing")
        self.assertIn("goal_profile_missing", result["warnings"])


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
