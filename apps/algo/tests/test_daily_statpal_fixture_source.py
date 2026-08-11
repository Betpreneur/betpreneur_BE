from unittest.mock import patch
from datetime import datetime, timezone
import json

from django.test import SimpleTestCase, TestCase, override_settings

from apps.algo.grindalgo import algo_runner
from apps.algo.models import FixtureCache
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
            "statpal_context": {
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "home": {
                                "sample_size": 10,
                                "wins": 5,
                                "draws": 3,
                                "losses": 2,
                                "avg_goals_for": 1.6,
                                "avg_goals_against": 1.1,
                                "clean_sheets": 3,
                                "squad_count": 26,
                                "injured_player_count": 2,
                                "populated_player_stat_count": 4,
                            },
                            "away": {
                                "sample_size": 10,
                                "wins": 3,
                                "draws": 2,
                                "losses": 5,
                                "avg_goals_for": 1.3,
                                "avg_goals_against": 1.4,
                                "clean_sheets": 2,
                                "squad_count": 24,
                                "injured_player_count": 0,
                                "populated_player_stat_count": 3,
                            },
                        }
                    },
                    "prematch_odds": {
                        "summary": {
                            "home_odds": 2.1,
                            "draw_odds": 3.2,
                            "away_odds": 3.4,
                            "over25_odds": 1.85,
                            "under25_odds": 1.95,
                        }
                    },
                    "injuries_suspensions": {
                        "summary": {
                            "home": {
                                "team_id": "2348384",
                                "team_name": "Sirius",
                                "to_miss_count": 2,
                                "questionable_count": 1,
                                "availability_risk": "medium",
                                "to_miss": [
                                    {"id": "p1", "name": "Home Defender", "status": "Knee Injury"},
                                    {"id": "p2", "name": "Home Forward", "status": "Suspended"},
                                ],
                                "questionable": [
                                    {"id": "p3", "name": "Home Midfielder", "status": "Doubtful"},
                                ],
                            },
                            "away": {
                                "team_id": "2348252",
                                "team_name": "IF Brommapojkarna",
                                "to_miss_count": 0,
                                "questionable_count": 0,
                                "availability_risk": "low",
                                "to_miss": [],
                                "questionable": [],
                            },
                            "total_to_miss_count": 2,
                            "total_questionable_count": 1,
                        }
                    },
                    "league_standings": {
                        "summary": {
                            "row_count": 2,
                            "team_count": 2,
                            "provider_competition_id": "3240",
                        },
                        "payload": {
                            "standings": [
                                {
                                    "team_id": "2348384",
                                    "team_name": "Sirius",
                                    "position": 1,
                                    "points": 39,
                                    "goal_difference": 16,
                                    "recent_form": "WWDWW",
                                    "overall": {"games_played": 22, "wins": 11},
                                    "home": {"games_played": 11, "wins": 6},
                                    "away": {"games_played": 11, "wins": 5},
                                    "description": "Promotion",
                                },
                                {
                                    "team_id": "2348252",
                                    "team_name": "IF Brommapojkarna",
                                    "position": 2,
                                    "points": 34,
                                    "goal_difference": 12,
                                    "recent_form": "LDDLW",
                                    "overall": {"games_played": 22, "wins": 8},
                                    "home": {"games_played": 11, "wins": 6},
                                    "away": {"games_played": 11, "wins": 2},
                                    "description": "Promotion",
                                },
                            ]
                        },
                    },
                    "league_stats": {
                        "summary": {
                            "row_count": 30,
                            "team_count": 2,
                            "player_count": 30,
                            "provider_competition_id": "3240",
                            "populated_player_stat_count": 0,
                            "injured_player_count": 1,
                            "team_summaries": [
                                {
                                    "team_id": "2348384",
                                    "team_name": "Sirius",
                                    "venue": {"id": "venue-home", "name": "Home Ground"},
                                    "coach": {"id": "coach-home", "name": "Home Coach"},
                                    "squad_count": 15,
                                    "injured_count": 1,
                                    "populated_player_stat_count": 0,
                                },
                                {
                                    "team_id": "2348252",
                                    "team_name": "IF Brommapojkarna",
                                    "venue": {"id": "venue-away", "name": "Away Ground"},
                                    "coach": {"id": "coach-away", "name": "Away Coach"},
                                    "squad_count": 15,
                                    "injured_count": 0,
                                    "populated_player_stat_count": 0,
                                },
                            ],
                        },
                        "payload": {"players": []},
                    },
                    "lineups": {
                        "summary": {
                            "match_id": "statpal:2026081032970",
                            "provider_match_id": "2026081032970",
                            "status": "projected",
                            "home_team": "Sirius",
                            "away_team": "IF Brommapojkarna",
                            "home_formation": "4-2-3-1",
                            "away_formation": "4-4-2",
                            "home_confidence": 75,
                            "away_confidence": 72,
                            "starting_count": 22,
                            "bench_count": 14,
                            "sidelined_count": 1,
                            "home_sidelined_count": 1,
                            "away_sidelined_count": 0,
                        },
                        "payload": {
                            "home": {
                                "formation": "4-2-3-1",
                                "starting_count": 11,
                                "bench_count": 7,
                                "sidelined_count": 1,
                            },
                            "away": {
                                "formation": "4-4-2",
                                "starting_count": 11,
                                "bench_count": 7,
                                "sidelined_count": 0,
                            },
                        },
                    },
                    "head_to_head": {
                        "summary": {
                            "team1_id": "2348384",
                            "team2_id": "2348252",
                            "recent_meetings_count": 5,
                            "games": 5,
                            "team1_won": 3,
                            "team2_won": 1,
                            "draws": 1,
                            "team1_scored": 9,
                            "team2_scored": 6,
                        },
                        "payload": {
                            "team1_id": "2348384",
                            "team2_id": "2348252",
                            "overall_record": {
                                "total": {
                                    "games": 5,
                                    "team1_won": 3,
                                    "team2_won": 1,
                                    "draws": 1,
                                }
                            },
                            "goals": {
                                "total": {
                                    "team1_scored": 9,
                                    "team2_scored": 6,
                                }
                            },
                            "recent_meetings": [
                                {"team1_id": "2348384", "team2_id": "2348252", "team1_score": 2, "team2_score": 1},
                                {"team1_id": "2348252", "team2_id": "2348384", "team1_score": 0, "team2_score": 2},
                                {"team1_id": "2348384", "team2_id": "2348252", "team1_score": 1, "team2_score": 1},
                                {"team1_id": "2348252", "team2_id": "2348384", "team1_score": 3, "team2_score": 1},
                                {"team1_id": "2348384", "team2_id": "2348252", "team1_score": 3, "team2_score": 1},
                            ],
                        },
                    },
                }
            },
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
        self.assertEqual(confs, {"Over 1.5": 67})
        self.assertEqual(real_odds["hw"], 2.1)
        self.assertEqual(real_odds["o25"], 1.85)
        self.assertIn("statpal_fixture_source", scored_fixture["fixture_context"]["flags"])
        self.assertIn("api_football_fixture_unavailable", scored_fixture["fixture_context"]["flags"])
        self.assertIn("h2h_available", scored_fixture["fixture_context"]["flags"])
        self.assertNotIn("h2h_unavailable", scored_fixture["fixture_context"]["flags"])
        self.assertIn("statpal_h2h_context", scored_fixture["fixture_context"]["flags"])
        self.assertIn("statpal_standings_context", scored_fixture["fixture_context"]["flags"])
        self.assertIn("statpal_league_stats_context", scored_fixture["fixture_context"]["flags"])
        self.assertIn("statpal_league_player_stats_unpopulated", scored_fixture["fixture_context"]["flags"])
        self.assertIn("home_top_table", scored_fixture["fixture_context"]["flags"])
        self.assertEqual(scored_fixture["fixture_context"]["h2h"]["games"], 5)
        self.assertEqual(scored_fixture["fixture_context"]["h2h"]["t1w"], 3)
        self.assertEqual(scored_fixture["fixture_context"]["h2h"]["t2w"], 1)
        self.assertEqual(scored_fixture["fixture_context"]["h2h"]["draws"], 1)
        self.assertEqual(scored_fixture["fixture_context"]["h2h"]["avg_goals"], 3.0)
        self.assertEqual(scored_fixture["home_recent_form"]["wins"], 5)
        self.assertEqual(scored_fixture["home_recent_form"]["draws"], 3)
        self.assertEqual(scored_fixture["home_recent_form"]["losses"], 2)
        self.assertEqual(scored_fixture["away_recent_form"]["wins"], 3)
        self.assertEqual(scored_fixture["away_recent_form"]["draws"], 2)
        self.assertEqual(scored_fixture["away_recent_form"]["losses"], 5)
        self.assertEqual(scored_fixture["fixture_context"]["home_standing"]["rank"], 1)
        self.assertEqual(scored_fixture["fixture_context"]["away_standing"]["points"], 34)
        self.assertEqual(scored_fixture["fixture_context"]["home_league_stats"]["squad_count"], 15)
        self.assertEqual(scored_fixture["fixture_context"]["away_league_stats"]["coach"]["name"], "Away Coach")
        self.assertIn("team_stats", scored_fixture["fixture_context"]["statpal"]["snapshots"])
        self.assertIn("league_standings", scored_fixture["fixture_context"]["statpal"]["snapshots"])
        self.assertIn("league_stats", scored_fixture["fixture_context"]["statpal"]["snapshots"])
        self.assertIn("lineups", scored_fixture["fixture_context"]["statpal"]["snapshots"])
        self.assertIn("head_to_head", scored_fixture["fixture_context"]["statpal"]["snapshots"])
        self.assertTrue(scored_fixture["team_news"]["available"])
        self.assertTrue(scored_fixture["team_news"]["lineups_available"])
        self.assertTrue(scored_fixture["team_news"]["injuries_available"])
        self.assertIn("api_football_fixture_unavailable", scored_fixture["team_news"]["flags"])
        self.assertIn("statpal_lineups_available", scored_fixture["team_news"]["flags"])
        self.assertIn("statpal_projected_lineups", scored_fixture["team_news"]["flags"])
        self.assertEqual(scored_fixture["team_news"]["lineup_status"], "projected")
        self.assertEqual(scored_fixture["team_news"]["home"]["formation"], "4-2-3-1")
        self.assertEqual(scored_fixture["team_news"]["home"]["starter_count"], 11)
        self.assertEqual(scored_fixture["team_news"]["home"]["lineup_confidence"], 75)
        self.assertEqual(scored_fixture["team_news"]["home"]["injuries"], 2)
        self.assertEqual(scored_fixture["team_news"]["home"]["questionable_count"], 1)
        self.assertEqual(scored_fixture["team_news"]["home"]["to_miss"][0]["name"], "Home Defender")
        self.assertEqual(scored_fixture["team_news"]["home"]["to_miss"][1]["status"], "Suspended")
        self.assertEqual(scored_fixture["team_news"]["home"]["questionable"][0]["status"], "Doubtful")
        self.assertEqual(scored_fixture["team_news"]["home"]["squad_count"], 26)
        self.assertEqual(scored_fixture["team_news"]["home"]["populated_player_stat_count"], 4)
        self.assertEqual(scored_fixture["team_news"]["away"]["substitute_count"], 7)

    def test_statpal_only_fixture_without_real_context_does_not_score_fake_default_markets(self):
        fixture = {
            "fixture": "Kairat Almaty vs Levski Sofia",
            "hname": "Kairat Almaty",
            "aname": "Levski Sofia",
            "hid": "2348001",
            "aid": "2348002",
            "match_id": "statpal:2026081120049",
            "source": "statpal_daily_cache",
            "statpal_context": {"snapshots": {}},
        }

        with (
            patch.object(algo_runner, "fetch_prediction_data") as fetch_prediction_data,
            patch.object(algo_runner, "fetch_team_recent_form") as fetch_team_recent_form,
            patch.object(algo_runner, "fetch_fixture_team_news") as fetch_fixture_team_news,
            patch.object(algo_runner, "get_api_football_odds") as get_api_football_odds,
            patch.object(algo_runner, "build_corner_profile") as build_corner_profile,
            patch.object(algo_runner, "score_fixture") as score_fixture,
        ):
            scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(fixture)

        fetch_prediction_data.assert_not_called()
        fetch_team_recent_form.assert_not_called()
        fetch_fixture_team_news.assert_not_called()
        get_api_football_odds.assert_not_called()
        build_corner_profile.assert_not_called()
        score_fixture.assert_not_called()
        self.assertEqual(confs, {})
        self.assertEqual(real_odds, {})
        self.assertEqual(scored_fixture["home_recent_form"]["games"], 0)
        self.assertEqual(scored_fixture["away_recent_form"]["games"], 0)
        self.assertIn("insufficient_statpal_fixture_data", scored_fixture["fixture_context"]["flags"])

    def test_statpal_standings_can_supply_form_when_team_stats_are_missing(self):
        context = {
            "snapshots": {
                "league_standings": {
                    "payload": {
                        "standings": [
                            {
                                "team_id": "home-1",
                                "team_name": "Home",
                                "recent_form": "WWDLW",
                                "overall": {
                                    "games_played": 12,
                                    "wins": 7,
                                    "draws": 2,
                                    "losses": 3,
                                    "goals_for": 20,
                                    "goals_against": 13,
                                },
                            },
                            {
                                "team_id": "away-1",
                                "team_name": "Away",
                                "recent_form": "LLDWW",
                                "overall": {
                                    "games_played": 12,
                                    "wins": 4,
                                    "draws": 3,
                                    "losses": 5,
                                    "goals_for": 14,
                                    "goals_against": 19,
                                },
                            },
                        ]
                    }
                }
            }
        }

        statpal_context = algo_runner.statpal_scoring_context(context)
        home, away = algo_runner._statpal_forms(
            statpal_context,
            {"hid": "home-1", "aid": "away-1"},
        )

        self.assertEqual(home["scope"], "statpal_standings")
        self.assertEqual(home["wins"], 7)
        self.assertEqual(home["draws"], 2)
        self.assertEqual(home["losses"], 3)
        self.assertEqual(home["games"], 12)
        self.assertEqual(home["avg_scored"], 1.67)
        self.assertEqual(away["wins"], 4)
        self.assertEqual(away["avg_conceded"], 1.58)

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

    def test_statpal_tracked_league_ids_accept_multiline_export(self):
        service = AlgoRunnerService()
        with patch.dict(
            "os.environ",
            {
                "STATPAL_TRACKED_LEAGUES": "3037 | Premier League | england\n3240 | Allsvenskan | sweden",
            },
        ):
            self.assertEqual(service._statpal_tracked_league_ids(), {"3037", "3240"})


class DailyStatPalFixtureSourceDbTests(TestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_statpal_daily_fixture_source_respects_tracked_league_allowlist(self):
        target_date = datetime(2026, 8, 11, tzinfo=timezone.utc).date()
        FixtureCache.objects.create(
            match_date=target_date,
            fixture="Allowed FC vs Visitor FC",
            home_team="Allowed FC",
            away_team="Visitor FC",
            league="Premier League",
            match_id="statpal:allowed",
            source="statpal",
            api_payload={"provider_competition_id": "3037"},
        )
        FixtureCache.objects.create(
            match_date=target_date,
            fixture="Blocked FC vs Visitor FC",
            home_team="Blocked FC",
            away_team="Visitor FC",
            league="Lower League",
            match_id="statpal:blocked",
            source="statpal",
            api_payload={"provider_competition_id": "999999"},
        )

        service = AlgoRunnerService()
        with patch.dict("os.environ", {"STATPAL_TRACKED_LEAGUES": "3037"}):
            fixtures = service._statpal_cached_runner_fixtures(target_date)

        self.assertEqual([fixture["fixture"] for fixture in fixtures], ["Allowed FC vs Visitor FC"])
