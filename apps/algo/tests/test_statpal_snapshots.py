from datetime import date

from django.test import TestCase

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
        )

        context = StatPalSnapshotService().fixture_context(match_id="12345")

        self.assertTrue(context["available"])
        self.assertIn("lineups", context["snapshots"])
        self.assertEqual(context["snapshots"]["lineups"]["summary"]["home_confirmed"], True)

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
                    "away_corners": 5,
                }
            },
            match_id="12345",
        )

        self.assertEqual(summary["home_xg"], 1.7)
        self.assertEqual(summary["away_xg"], 1.2)
        self.assertEqual(summary["expected_goals"], 2.9)
        self.assertEqual(summary["home_shots"], 12)
        self.assertEqual(summary["away_corners"], 5)

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
