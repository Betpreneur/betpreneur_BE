from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from apps.algo.models import FixtureCache, ProviderFixtureMap, StatPalFixtureSnapshot
from apps.algo.statpal_snapshots import StatPalSnapshotService
from apps.algo.statpal import StatPalError


INJURY_PAYLOAD = {
    "injuries_suspensions": {
        "league": [
            {
                "name": "Premier League",
                "id": "3037",
                "match": [
                    {
                        "main_id": "statpal-match-1",
                        "date": "08.08.2026",
                        "time": "15:00",
                        "home": {
                            "id": "home-1",
                            "name": "Liverpool",
                            "sidelined": {
                                "to_miss": {"player": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]},
                                "questionable": {"player": {"id": "p4"}},
                            },
                        },
                        "away": {
                            "id": "away-1",
                            "name": "Chelsea",
                            "sidelined": {
                                "to_miss": None,
                                "questionable": {"player": [{"id": "p5"}]},
                            },
                        },
                    }
                ],
            }
        ]
    }
}


class StatPalSnapshotContextPayloadTests(SimpleTestCase):
    def test_compact_context_payload_strips_raw_keys_and_caps_lists(self):
        payload = {
            "raw": {"provider": "full response"},
            "items": [{"id": str(index), "raw": {"hidden": True}} for index in range(55)],
        }

        compact = StatPalSnapshotService._compact_context_payload(payload)

        self.assertNotIn("raw", compact)
        self.assertEqual(len(compact["items"]), 50)
        self.assertEqual(compact["items"][0]["id"], "0")
        self.assertNotIn("raw", compact["items"][0])

    def test_team_stats_summary_extracts_history_signals(self):
        summary = StatPalSnapshotService._summarize_team_stats(
            {
                "provider_team_id": "2340835",
                "name": "Arsenal",
                "squad_count": 25,
                "league_stats": [
                    {
                        "league": "Premier League",
                        "season": "2025/2026",
                        "fulltime": {
                            "win": {"total": 12, "home": 7, "away": 5},
                            "draw": {"total": 4, "home": 2, "away": 2},
                            "lost": {"total": 3, "home": 0, "away": 3},
                            "avg_goals_per_game_scored": {"total": 2.1, "home": 2.4, "away": 1.8},
                            "avg_goals_per_game_conceded": {"total": 0.9, "home": 0.7, "away": 1.1},
                            "clean_sheet": {"total": 8, "home": 5, "away": 3},
                            "failed_to_score": {"total": 2, "home": 0, "away": 2},
                            "avg_corners": {"total": 6.2, "home": 6.8, "away": 5.6},
                            "avg_yellowcards": {"total": 1.7, "home": 1.5, "away": 1.9},
                        },
                        "firsthalf": {
                            "avg_goals_per_game_scored": {"total": 0.9},
                            "avg_goals_per_game_conceded": {"total": 0.4},
                        },
                        "secondhalf": {
                            "avg_goals_per_game_scored": {"total": 1.2},
                            "avg_goals_per_game_conceded": {"total": 0.5},
                        },
                    }
                ],
            }
        )

        self.assertEqual(summary["team_id"], "2340835")
        self.assertEqual(summary["normalized_team_name"], "arsenal")
        self.assertEqual(summary["sample_size"], 19)
        self.assertEqual(summary["current_league"], "Premier League")
        self.assertEqual(summary["avg_goals_for"], 2.1)
        self.assertEqual(summary["avg_goals_against"], 0.9)
        self.assertEqual(summary["avg_total_goals"], 3.0)
        self.assertEqual(summary["avg_corners"], 6.2)
        self.assertEqual(summary["firsthalf_avg_goals_for"], 0.9)

    def test_team_stats_summary_ignores_impossible_half_goal_averages(self):
        summary = StatPalSnapshotService._summarize_team_stats(
            {
                "provider_team_id": "2340835",
                "name": "Arsenal FC",
                "league_stats": [
                    {
                        "league": "UEFA Champions League",
                        "season": "2025/2026",
                        "fulltime": {
                            "win": {"total": 10},
                            "draw": {"total": 3},
                            "lost": {"total": 2},
                            "avg_goals_per_game_scored": {"total": 2.0},
                            "avg_goals_per_game_conceded": {"total": 0.53},
                        },
                        "firsthalf": {
                            "avg_goals_per_game_scored": {"total": 25},
                            "avg_goals_per_game_conceded": {"total": 19},
                        },
                        "secondhalf": {
                            "avg_goals_per_game_scored": {"total": 70},
                            "avg_goals_per_game_conceded": {"total": 55},
                        },
                    }
                ],
            }
        )

        self.assertEqual(summary["avg_goals_for"], 2.0)
        self.assertEqual(summary["avg_goals_against"], 0.53)
        self.assertIsNone(summary["firsthalf_avg_goals_for"])
        self.assertIsNone(summary["firsthalf_avg_goals_against"])
        self.assertIsNone(summary["secondhalf_avg_goals_for"])
        self.assertIsNone(summary["secondhalf_avg_goals_against"])

    def test_team_stats_context_combines_home_and_away_profiles(self):
        service = StatPalSnapshotService()
        rows = [
            SimpleNamespace(
                status="available",
                summary={"fixture_side": "home", "team_id": "h1", "team_name": "Home", "avg_corners": 5.5},
                payload={"fixture_side": "home", "provider_team_id": "h1", "raw": {"hidden": True}},
                fetched_at=None,
                expires_at=None,
                source_endpoint="SOCCER_TEAM",
            ),
            SimpleNamespace(
                status="available",
                summary={"fixture_side": "away", "team_id": "a1", "team_name": "Away", "avg_corners": 4.5},
                payload={"fixture_side": "away", "provider_team_id": "a1", "raw": {"hidden": True}},
                fetched_at=None,
                expires_at=None,
                source_endpoint="SOCCER_TEAM",
            ),
        ]

        context = service._team_stats_context(rows)

        self.assertEqual(context["summary"]["team_count"], 2)
        self.assertEqual(context["summary"]["home"]["team_id"], "h1")
        self.assertEqual(context["summary"]["away"]["team_id"], "a1")
        self.assertEqual(len(context["payload"]["teams"]), 2)
        self.assertNotIn("raw", context["payload"]["teams"][0])


class StatPalSnapshotServiceTests(TestCase):
    def test_save_injuries_payload_creates_fixture_snapshot_summary(self):
        fixture = FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Liverpool vs Chelsea",
            home_team="Liverpool",
            away_team="Chelsea",
            match_id="12345",
        )
        ProviderFixtureMap.objects.create(
            provider="statpal",
            provider_event_id="statpal-match-1",
            provider_competition_id="3037",
            api_fixture_id="12345",
            provider_home_team="Liverpool",
            provider_away_team="Chelsea",
            api_home_team="Liverpool",
            api_away_team="Chelsea",
        )

        rows = StatPalSnapshotService().save_injuries_suspensions_payload(INJURY_PAYLOAD)

        self.assertEqual(len(rows), 1)
        snapshot = rows[0]
        self.assertEqual(snapshot.fixture, fixture)
        self.assertEqual(snapshot.match_id, "12345")
        self.assertEqual(snapshot.provider_match_id, "statpal-match-1")
        self.assertEqual(snapshot.snapshot_type, StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS)
        self.assertEqual(snapshot.summary["home"]["to_miss_count"], 3)
        self.assertEqual(snapshot.summary["home"]["availability_risk"], "high")
        self.assertEqual(snapshot.summary["away"]["questionable_count"], 1)

    def test_fixture_context_returns_compact_snapshot_data(self):
        StatPalFixtureSnapshot.objects.create(
            match_id="12345",
            provider_match_id="statpal-match-1",
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            source_endpoint="SOCCER_LINEUPS",
            summary={"home_confirmed": True},
            payload={
                "id": "statpal:lineups:statpal-match-1",
                "raw": {"provider": "full response"},
                "home": {
                    "starting_xi": [
                        {
                            "id": "p1",
                            "name": "Starter One",
                            "raw": {"hidden": True},
                        }
                    ]
                },
            },
        )

        context = StatPalSnapshotService().fixture_context(match_id="12345")

        self.assertTrue(context["available"])
        self.assertIn("lineups", context["snapshots"])
        lineups = context["snapshots"]["lineups"]
        self.assertEqual(lineups["summary"]["home_confirmed"], True)
        self.assertTrue(lineups["payload_available"])
        self.assertEqual(lineups["payload"]["id"], "statpal:lineups:statpal-match-1")
        self.assertNotIn("raw", lineups["payload"])
        self.assertNotIn("raw", lineups["payload"]["home"]["starting_xi"][0])

    def test_save_endpoint_payload_upserts_same_fixture_type(self):
        service = StatPalSnapshotService()

        first = service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            endpoint_name="SOCCER_PREMATCH_ODDS",
            payload={"odds": [{"market": "1X2"}]},
            match_id="12345",
            provider_match_id="statpal-match-1",
        )
        second = service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            endpoint_name="SOCCER_PREMATCH_ODDS",
            payload={"odds": [{"market": "Totals"}]},
            match_id="12345",
            provider_match_id="statpal-match-1",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(StatPalFixtureSnapshot.objects.count(), 1)
        self.assertEqual(second.payload["odds"][0]["market"], "Totals")

    def test_lineups_endpoint_payload_is_normalized_before_save(self):
        row = StatPalSnapshotService().save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            endpoint_name="SOCCER_LINEUPS",
            match_id="statpal:2026061822389",
            provider_match_id="2026061822389",
            payload={
                "main_id": "2026061822389",
                "status": "projected",
                "updated": "06.17.2026 12:31:15",
                "updated_ts": 1781699475314,
                "home": {
                    "team_id": "2339730",
                    "team_name": "Canada",
                    "coach": {"name": "Jesse Marsch", "id": "3381958"},
                    "team_formation": "4-4-2",
                    "starting_xi": [{"id": "2504652", "name": "Maxime Crepeau", "number": "16", "position": "goalkeeper"}],
                    "bench": [],
                    "sidelined": [{"id": "2773229", "name": "Alphonso Davies", "number": "19", "position": "defender", "status": "out", "reason": "injury"}],
                    "confidence": 45,
                },
                "away": {
                    "team_id": "2346325",
                    "team_name": "Qatar",
                    "coach": {"name": "Julen Lopetegui", "id": "2529722"},
                    "team_formation": "4-3-3",
                    "starting_xi": [{"id": "2923575", "name": "Mahmoud Abunada", "number": "1", "position": "goalkeeper"}],
                    "bench": [],
                    "sidelined": [],
                    "confidence": 45,
                },
            },
        )

        self.assertEqual(row.payload["id"], "statpal:lineups:2026061822389")
        self.assertEqual(row.payload["home"]["formation"], "4-4-2")
        self.assertEqual(row.payload["starting_count"], 2)
        self.assertEqual(row.summary["status"], "projected")
        self.assertEqual(row.summary["home_sidelined_count"], 1)

    def test_prematch_odds_endpoint_payload_is_normalized_before_save(self):
        row = StatPalSnapshotService().save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            endpoint_name="SOCCER_PREMATCH_ODDS",
            match_id="statpal:2025121318250",
            provider_match_id="2025121318250",
            provider_competition_id="3037",
            payload={
                "prematch_odds": {
                    "updated": "09.12.2025 17:15:44",
                    "updated_ts": 1765300544,
                    "league": {
                        "id": "3037",
                        "name": "England: Premier League",
                        "country": "england",
                        "match": [
                            {
                                "main_id": "2025121318250",
                                "date": "13.12.2025",
                                "time": "15:00",
                                "home": {"id": "2340925", "name": "Chelsea"},
                                "away": {"id": "2340991", "name": "Everton"},
                                "odds": [
                                    {
                                        "id": "1834",
                                        "name": "1x2",
                                        "stop": "False",
                                        "bookmaker": [
                                            {
                                                "id": "1847",
                                                "name": "10Bet",
                                                "timestamp": "1765252069",
                                                "odd": [
                                                    {"name": "Home", "value": "1.64"},
                                                    {"name": "Draw", "value": "3.75"},
                                                    {"name": "Away", "value": "5.10"},
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                }
            },
        )

        self.assertEqual(row.payload["id"], "statpal:prematch_odds:2025121318250")
        self.assertEqual(row.payload["markets"][0]["bookmakers"][0]["odds"][0]["value"], 1.64)
        self.assertEqual(row.summary["home_odds"], 1.64)
        self.assertEqual(row.summary["draw_odds"], 3.75)
        self.assertEqual(row.summary["away_odds"], 5.1)

    def test_prediction_summary_extracts_core_scoring_signals(self):
        summary = StatPalSnapshotService().summarize(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            payload={
                "prediction": {
                    "home_win_percent": "62%",
                    "draw_probability": 0.22,
                    "away_win_percent": 16,
                    "expected_goals": 3.1,
                    "over_2_5": 71,
                    "btts": 64,
                }
            },
            match_id="12345",
        )

        self.assertEqual(summary["home_win_percent"], 62)
        self.assertEqual(summary["draw_percent"], 22)
        self.assertEqual(summary["expected_goals"], 3.1)
        self.assertEqual(summary["over25_percent"], 71)
        self.assertEqual(summary["btts_percent"], 64)

    def test_detailed_stats_summary_extracts_xg(self):
        summary = StatPalSnapshotService().summarize(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            payload={
                "stats": {
                    "home_xg": 1.7,
                    "away_xg": 1.2,
                    "home_shots": 12,
                    "home_shots_on_target": 5,
                    "away_shots_on_goal": 4,
                    "away_corners": 5,
                    "home_yellow_cards": 2,
                    "away_yellow_cards": 3,
                    "home_red_cards": 0,
                    "away_red_cards": 1,
                }
            },
            match_id="12345",
        )

        self.assertEqual(summary["home_xg"], 1.7)
        self.assertEqual(summary["away_xg"], 1.2)
        self.assertEqual(summary["expected_goals"], 2.9)
        self.assertEqual(summary["home_shots"], 12)
        self.assertEqual(summary["home_shots_on_target"], 5)
        self.assertEqual(summary["away_shots_on_target"], 4)
        self.assertEqual(summary["away_corners"], 5)
        self.assertEqual(summary["home_yellow_cards"], 2)
        self.assertEqual(summary["away_yellow_cards"], 3)
        self.assertEqual(summary["away_red_cards"], 1)

    def test_detailed_stats_summary_aggregates_player_shot_stats(self):
        summary = StatPalSnapshotService().summarize(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            payload={
                "player_stats": {
                    "home": [
                        {"stats": {"shots_total": 4, "shots_on": 2}},
                        {"stats": {"shots_total": 3, "shots_on_target": 1}},
                    ],
                    "away": [
                        {"stats": {"shots_total": 5, "shots_on_goal": 3}},
                        {"stats": {"shots_total": 2}},
                    ],
                }
            },
            match_id="12345",
        )

        self.assertEqual(summary["home_shots"], 7)
        self.assertEqual(summary["away_shots"], 7)
        self.assertEqual(summary["home_shots_on_target"], 3)
        self.assertEqual(summary["away_shots_on_target"], 3)
        self.assertTrue(summary["has_player_stats"])

    def test_detailed_stats_snapshot_normalizes_match_stats_endpoint_payload(self):
        service = StatPalSnapshotService()
        row = service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            endpoint_name="SOCCER_DETAILED_STATS",
            match_id="12345",
            provider_match_id="2025120818706",
            provider_competition_id="3037",
            payload={
                "match-stats": {
                    "updated": "09.12.2025 04:22:31",
                    "updated_ts": 1765254151,
                    "tournament": {
                        "id": "3037",
                        "name": "England - Premier League",
                        "matches": {
                            "main_id": "2025120818706",
                            "date": "08.12.2025",
                            "time": "20:00",
                            "status": "Full-time",
                            "match_info": {
                                "stadium": {"name": "Molineux Stadium, Wolverhampton"},
                                "referee": {"name": "Michael Salisbury, England"},
                            },
                            "home": {"id": "2341279", "name": "Wolverhampton", "goals": "1"},
                            "away": {"id": "2341093", "name": "Manchester United", "goals": "4"},
                            "team_stats": {
                                "home": {
                                    "corners": {"total": "1", "total_h1": "1", "total_h2": "0"},
                                    "expected_goals": {"total": "0.41"},
                                    "fouls": {"total": "17"},
                                },
                                "away": {
                                    "corners": {"total": "9", "total_h1": "6", "total_h2": "3"},
                                    "expected_goals": {"total": "4.24"},
                                    "fouls": {"total": "12"},
                                },
                            },
                            "event_summary": {
                                "home": {
                                    "goals": {"event": {"minute": "45", "player_id": "2752619"}},
                                    "yellowcards": "",
                                    "redcards": "",
                                    "var": "",
                                },
                                "away": {
                                    "goals": "",
                                    "yellowcards": {"event": [{"minute": "90", "player_id": "2848210"}]},
                                    "redcards": "",
                                    "var": {"event": {"minute": "80", "event_type": "Penalty confirmed"}},
                                },
                            },
                        },
                    },
                }
            },
        )

        self.assertEqual(row.payload["provider_match_id"], "2025120818706")
        self.assertEqual(row.payload["team_stats"]["away"]["expected_goals"]["total"], 4.24)
        self.assertEqual(row.summary["home_xg"], 0.41)
        self.assertEqual(row.summary["away_xg"], 4.24)
        self.assertEqual(row.summary["expected_goals"], 4.65)
        self.assertEqual(row.summary["home_corners"], 1)
        self.assertEqual(row.summary["away_corners"], 9)
        self.assertEqual(row.summary["home_fouls"], 17)
        self.assertEqual(row.summary["away_fouls"], 12)
        self.assertEqual(row.summary["away_yellow_cards"], 1)
        self.assertEqual(row.summary["var_events"], 1)

    def test_refresh_fixture_snapshots_saves_fixture_endpoint_payload(self):
        class DummyClient:
            def __init__(self):
                self.calls = []

            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                self.calls.append({"endpoint_name": endpoint_name, "params": params, "path_params": path_params})
                return {"endpoint": endpoint_name, "match": path_params.get("match_id")}

        client = DummyClient()
        service = StatPalSnapshotService(client=client)

        result = service.refresh_fixture_snapshots(
            match_id="12345",
            snapshot_types=[StatPalFixtureSnapshot.SnapshotType.LINEUPS],
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["attempted"], ["lineups"])
        self.assertEqual(result["refreshed"][0]["snapshot_type"], "lineups")
        self.assertEqual(result["api_usage"]["attempted_calls"], 1)
        self.assertEqual(result["api_usage"]["successful_calls"], 1)
        self.assertEqual(result["api_usage"]["failed_calls"], 0)
        self.assertEqual(result["api_usage"]["snapshot_types_refreshed"], ["lineups"])
        self.assertEqual(client.calls[0]["endpoint_name"], "SOCCER_LINEUPS")
        self.assertEqual(client.calls[0]["path_params"], {})
        self.assertEqual(client.calls[0]["params"]["match_id"], "12345")
        snapshot = StatPalFixtureSnapshot.objects.get(match_id="12345", snapshot_type="lineups")
        self.assertEqual(snapshot.payload["endpoint"], "SOCCER_LINEUPS")

    def test_refresh_fixture_snapshots_skips_league_scoped_endpoint_without_league_id(self):
        class ExplodingClient:
            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                raise AssertionError("league-scoped endpoint should not be called without league id")

        result = StatPalSnapshotService(client=ExplodingClient()).refresh_fixture_snapshots(
            match_id="12345",
            snapshot_types=[StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS],
        )

        self.assertEqual(result["attempted"], ["prematch_odds"])
        self.assertEqual(result["refreshed"], [])
        self.assertEqual(result["skipped"][0]["reason"], "missing_league_id")
        self.assertEqual(result["api_usage"]["attempted_calls"], 0)
        self.assertEqual(result["api_usage"]["skipped_without_call"], 1)

    def test_refresh_fixture_snapshots_uses_league_id_for_prematch_odds(self):
        class DummyClient:
            def __init__(self):
                self.calls = []

            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                self.calls.append({"endpoint_name": endpoint_name, "params": params, "path_params": path_params})
                return {"endpoint": endpoint_name, "match": params.get("match_id"), "league": path_params.get("league_id")}

        client = DummyClient()
        result = StatPalSnapshotService(client=client).refresh_fixture_snapshots(
            match_id="12345",
            provider_competition_id="39",
            snapshot_types=[StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS],
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(client.calls[0]["endpoint_name"], "SOCCER_PREMATCH_ODDS")
        self.assertEqual(client.calls[0]["params"]["match_id"], "12345")
        self.assertEqual(client.calls[0]["path_params"]["league_id"], "39")

    def test_refresh_fixture_snapshots_skips_fresh_snapshot(self):
        StatPalSnapshotService().save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            endpoint_name="SOCCER_LINEUPS",
            payload={"lineups": []},
            match_id="12345",
        )

        class ExplodingClient:
            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                raise AssertionError("fresh snapshots should not be refetched")

        result = StatPalSnapshotService(client=ExplodingClient()).refresh_fixture_snapshots(
            match_id="12345",
            snapshot_types=[StatPalFixtureSnapshot.SnapshotType.LINEUPS],
        )

        self.assertEqual(result["attempted"], [])
        self.assertEqual(result["refreshed"], [])
        self.assertEqual(result["skipped"][0]["reason"], "fresh_snapshot_exists")
        self.assertEqual(result["api_usage"]["attempted_calls"], 0)
        self.assertEqual(result["api_usage"]["skipped_by_cache"], 1)

    def test_refresh_fixture_snapshots_collects_statpal_errors(self):
        class FailingClient:
            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                raise StatPalError("provider unavailable")

        result = StatPalSnapshotService(client=FailingClient()).refresh_fixture_snapshots(
            match_id="12345",
            snapshot_types=[StatPalFixtureSnapshot.SnapshotType.LINEUPS],
        )

        self.assertEqual(result["attempted"], ["lineups"])
        self.assertEqual(result["refreshed"], [])
        self.assertIn("provider unavailable", result["errors"][0]["error"])
        self.assertEqual(result["api_usage"]["attempted_calls"], 1)
        self.assertEqual(result["api_usage"]["failed_calls"], 1)
        self.assertEqual(result["api_usage"]["snapshot_types_failed"], ["lineups"])

    def test_snapshot_types_for_market_are_market_family_aware(self):
        service = StatPalSnapshotService()

        goals = service.snapshot_types_for_market("Over 2.5")
        cards = service.snapshot_types_for_market("Cards Over 3.5")
        player = service.snapshot_types_for_market("Player To Be Booked")

        self.assertIn(StatPalFixtureSnapshot.SnapshotType.PREDICTIONS, goals)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS, goals)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LINEUPS, cards)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS, cards)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LINEUPS, player)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS, player)

    def test_snapshot_plan_reports_cache_coverage_for_market(self):
        service = StatPalSnapshotService()
        service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            endpoint_name="SOCCER_LINEUPS",
            payload={"lineups": []},
            match_id="12345",
        )

        plan = service.snapshot_plan_for_market("Cards Over 3.5", match_id="12345")

        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LINEUPS, plan["fresh_snapshot_types"])
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS, plan["missing_snapshot_types"])
        self.assertGreater(plan["coverage_percent"], 0)

    def test_prepare_fixture_context_for_market_fetches_required_snapshots(self):
        class DummyClient:
            def __init__(self):
                self.calls = []

            def soccer_endpoint(self, endpoint_name, params=None, **path_params):
                self.calls.append({"endpoint_name": endpoint_name, "params": params, "path_params": path_params})
                if endpoint_name == "SOCCER_INJURIES_SUSPENSIONS":
                    return {"injuries_suspensions": {"league": []}}
                return {"endpoint": endpoint_name, "params": params or {}, "path_params": path_params}

        client = DummyClient()
        service = StatPalSnapshotService(client=client)

        bundle = service.prepare_fixture_context_for_market(
            "Cards Over 3.5",
            match_id="12345",
            provider_competition_id="39",
        )

        called = [call["endpoint_name"] for call in client.calls]
        self.assertIn("SOCCER_LINEUPS", called)
        self.assertIn("SOCCER_DETAILED_STATS", called)
        self.assertIn("SOCCER_PREMATCH_ODDS", called)
        self.assertTrue(bundle["context"]["available"])
        self.assertIn("market_snapshot_plan", bundle["context"])
        self.assertIn("market_snapshot_coverage", bundle["context"])
