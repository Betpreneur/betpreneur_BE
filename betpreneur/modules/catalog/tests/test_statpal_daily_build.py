from datetime import date

from django.test import SimpleTestCase

from betpreneur.modules.catalog.interface.serializers import StatPalReadinessQuerySerializer
from betpreneur.modules.catalog.models import StatPalFixtureSnapshot
from betpreneur.modules.catalog.services.daily_build import (
    StatPalBuildScope,
    StatPalDailyBuildService,
    build_fixture_coverage_item,
    canonical_endpoint_name,
    daily_build_window,
    endpoint_specs_for_daily_build,
    plan_statpal_daily_build,
    statpal_cache_readiness,
    statpal_snapshot_usable_fields,
)


class StatPalDailyBuildContractTests(SimpleTestCase):
    def test_daily_build_window_defaults_to_three_days(self):
        self.assertEqual(
            daily_build_window(date(2026, 8, 10)),
            [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)],
        )

    def test_endpoint_aliases_match_existing_snapshot_names(self):
        self.assertEqual(canonical_endpoint_name("SOCCER_LINEUPS"), "SOCCER_TEAM_LINEUPS")
        self.assertEqual(canonical_endpoint_name("SOCCER_DETAILED_STATS"), "SOCCER_LEAGUE_MATCH_STATS")
        self.assertEqual(canonical_endpoint_name("SOCCER_TEAM_STATS"), "SOCCER_TEAM")

    def test_daily_endpoint_scope_excludes_optional_live_and_player_fetches(self):
        endpoints = {spec.endpoint_name for spec in endpoint_specs_for_daily_build()}
        optional = {spec.endpoint_name for spec in endpoint_specs_for_daily_build(include_optional=True)}

        self.assertIn("SOCCER_MATCHES_DAILY", endpoints)
        self.assertIn("SOCCER_PREMATCH_ODDS", endpoints)
        self.assertIn("SOCCER_HEAD_TO_HEAD", endpoints)
        self.assertNotIn("SOCCER_LIVE_ODDS", endpoints)
        self.assertNotIn("SOCCER_PLAYER", endpoints)
        self.assertIn("SOCCER_PLAYER", optional)
        self.assertIn("SOCCER_COACH", optional)
        self.assertIn("SOCCER_LIVE_ODDS_MARKETS", optional)

    def test_daily_build_plan_dedupes_leagues_teams_and_h2h(self):
        fixtures = [
            {
                "provider_match_id": "m1",
                "provider_competition_id": "l1",
                "home_team_id": "h1",
                "away_team_id": "a1",
                "home_team": "Home One",
                "away_team": "Away One",
            },
            {
                "provider_match_id": "m2",
                "provider_competition_id": "l1",
                "home_team_id": "h1",
                "away_team_id": "a2",
                "home_team": "Home One",
                "away_team": "Away Two",
            },
        ]

        plan = plan_statpal_daily_build(fixtures)

        self.assertEqual(plan["fixtures"], 2)
        self.assertEqual(plan["summary"]["league_tasks"], 5)
        self.assertEqual(plan["summary"]["team_tasks"], 3)
        self.assertEqual(plan["summary"]["h2h_tasks"], 2)
        self.assertEqual(plan["summary"]["global_tasks"], 2)
        team_ids = {
            task["ids"]["team_id"]
            for task in plan["tasks"]
            if task["scope"] == StatPalBuildScope.TEAM
        }
        self.assertEqual(team_ids, {"h1", "a1", "a2"})

    def test_daily_build_plan_marks_missing_required_ids(self):
        plan = plan_statpal_daily_build([{"provider_match_id": "m1"}])

        missing = [task for task in plan["tasks"] if task["status"] == "missing_ids"]

        self.assertTrue(missing)
        self.assertGreater(plan["summary"]["missing_identity_tasks"], 0)


class DummySnapshot:
    def __init__(self, snapshot_id):
        self.id = snapshot_id


class DummySnapshotService:
    def __init__(self):
        self.saved = []
        self.injuries_payloads = []

    def get_snapshot(self, **kwargs):
        return None

    def _is_expired(self, snapshot):
        return True

    def fixture_context(self, **kwargs):
        return {"available": False, "snapshots": {}}

    def save_injuries_suspensions_payload(self, payload):
        self.injuries_payloads.append(payload)
        return [DummySnapshot(900)]

    def save_endpoint_payload(self, **kwargs):
        self.saved.append(kwargs)
        return DummySnapshot(len(self.saved))


class DummyFixtureService:
    def __init__(self):
        self.upserts = []

    def _upsert_fixtures(self, fixtures, target_date):
        self.upserts.append({"fixtures": list(fixtures), "target_date": target_date})
        return len(fixtures)


class DummyStatPalBuildClient:
    def __init__(self, daily_payload=None, failures=None):
        self.calls = []
        self.daily_payload = daily_payload or daily_payload_for_build()
        self.failures = set(failures or [])

    def soccer_daily_matches(self):
        self.calls.append({"endpoint_name": "SOCCER_MATCHES_DAILY", "params": {}, "path_params": {}})
        return self.daily_payload

    def soccer_endpoint(self, endpoint_name, params=None, **path_params):
        self.calls.append({"endpoint_name": endpoint_name, "params": params or {}, "path_params": path_params})
        if endpoint_name in self.failures:
            raise RuntimeError(f"{endpoint_name} failed")
        return {"endpoint": endpoint_name, "params": params or {}, "path_params": path_params}


def daily_payload_for_build():
    return {
        "matches_10_08_2026": {
            "league": [
                {
                    "id": "3240",
                    "name": "Sweden: Allsvenskan",
                    "country": "sweden",
                    "match": [
                        {
                            "main_id": "2026081032970",
                            "date": "10.08.2026",
                            "time": "17:00",
                            "home": {"id": "2348384", "name": "Sirius"},
                            "away": {"id": "2348252", "name": "Brommapojkarna"},
                            "coaches": {
                                "home": {"coach": {"id": "c1", "name": "Coach One"}},
                                "away": {"coach": {"id": "c2", "name": "Coach Two"}},
                            },
                            "lineups": {
                                "home": {
                                    "player": [
                                        {"id": "p1", "name": "Player One"},
                                        {"id": "p2", "name": "Player Two"},
                                    ]
                                },
                                "away": {"player": {"id": "p3", "name": "Player Three"}},
                            },
                        },
                        {
                            "main_id": "2026081032971",
                            "date": "11.08.2026",
                            "time": "17:00",
                            "home": {"id": "2348000", "name": "Home Two"},
                            "away": {"id": "2348001", "name": "Away Two"},
                        },
                    ],
                }
            ]
        }
    }


class StatPalDailyBuildServiceTests(SimpleTestCase):
    def test_build_fetches_fixture_universe_and_executes_deduped_tasks(self):
        snapshot_service = DummySnapshotService()
        fixture_service = DummyFixtureService()
        client = DummyStatPalBuildClient()
        service = StatPalDailyBuildService(
            client=client,
            snapshot_service=snapshot_service,
            fixture_service=fixture_service,
        )

        result = service.build(start_date=date(2026, 8, 10), days=3)

        self.assertEqual(result["fixture_universe"]["fetched"], 2)
        self.assertEqual(result["fixture_universe"]["cached"], 2)
        self.assertEqual(result["execution"]["failed"], 0)
        called = [call["endpoint_name"] for call in client.calls]
        self.assertIn("SOCCER_MATCHES_DAILY", called)
        self.assertIn("SOCCER_PREMATCH_ODDS", called)
        self.assertIn("SOCCER_LEAGUE_MATCH_STATS", called)
        self.assertIn("SOCCER_TEAM", called)
        self.assertIn("SOCCER_HEAD_TO_HEAD", called)
        self.assertTrue(snapshot_service.saved)
        self.assertEqual(len(fixture_service.upserts), 3)

        saved_types = {item["snapshot_type"] for item in snapshot_service.saved}
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.TEAM_STATS, saved_types)

    def test_build_isolates_endpoint_failures(self):
        service = StatPalDailyBuildService(
            client=DummyStatPalBuildClient(failures={"SOCCER_PREMATCH_ODDS"}),
            snapshot_service=DummySnapshotService(),
            fixture_service=DummyFixtureService(),
        )

        result = service.build(start_date=date(2026, 8, 10), days=1)

        self.assertGreater(result["execution"]["failed"], 0)
        self.assertGreater(result["execution"]["succeeded"], 0)

    def test_build_optional_mode_fetches_player_coach_live_and_media_endpoints(self):
        snapshot_service = DummySnapshotService()
        client = DummyStatPalBuildClient()
        service = StatPalDailyBuildService(
            client=client,
            snapshot_service=snapshot_service,
            fixture_service=DummyFixtureService(),
        )

        result = service.build(start_date=date(2026, 8, 10), days=1, include_optional=True)

        self.assertEqual(result["execution"]["failed"], 0)
        called = [call["endpoint_name"] for call in client.calls]
        self.assertIn("SOCCER_PLAYER", called)
        self.assertIn("SOCCER_COACH", called)
        self.assertIn("SOCCER_IMAGES", called)
        self.assertIn("SOCCER_LIVE_ODDS", called)
        self.assertIn("SOCCER_LIVE_ODDS_MARKETS", called)
        self.assertIn("SOCCER_LIVE_ODDS_MATCH_STATES", called)
        self.assertIn("SOCCER_LIVE_STORYLINES", called)
        saved_types = {item["snapshot_type"] for item in snapshot_service.saved}
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.COACH, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.IMAGES, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS, saved_types)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.LIVE_STORYLINES, saved_types)


class StatPalDailyBuildCoverageTests(SimpleTestCase):
    def test_snapshot_usable_fields_reports_nested_team_stats(self):
        summary = {
            "team_count": 2,
            "home": {"avg_goals_for": 2.1, "avg_corners": 5.0},
            "away": {"avg_goals_against": 1.2},
        }

        fields = statpal_snapshot_usable_fields(StatPalFixtureSnapshot.SnapshotType.TEAM_STATS, summary)

        self.assertIn("team_count", fields)
        self.assertIn("home.avg_goals_for", fields)
        self.assertIn("home.avg_corners", fields)
        self.assertIn("away.avg_goals_against", fields)

    def test_fixture_coverage_marks_present_missing_and_usable_fields(self):
        fixture = {
            "match_id": "1494239",
            "provider_match_id": "2026081032970",
            "provider_competition_id": "3240",
            "home_team_id": "2348384",
            "away_team_id": "2348252",
            "fixture": "Sirius vs IF Brommapojkarna",
        }
        context = {
            "available": True,
            "snapshots": {
                StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS: {
                    "status": "available",
                    "summary": {"market_count": 85, "home_odds": 1.3, "away_odds": 8.75},
                    "source_endpoint": "SOCCER_PREMATCH_ODDS",
                    "payload_available": True,
                },
                StatPalFixtureSnapshot.SnapshotType.LINEUPS: {
                    "status": "available",
                    "summary": {"starting_count": 22, "home_confidence": 75, "away_confidence": 75},
                    "source_endpoint": "SOCCER_TEAM_LINEUPS",
                    "payload_available": True,
                },
            },
        }

        coverage = build_fixture_coverage_item(fixture, context)

        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["identity"]["present"]["provider_match_id"], True)
        self.assertIn(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS, coverage["missing_snapshot_types"])
        self.assertIn("market_count", coverage["snapshots"][StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS]["usable_fields"])
        self.assertGreater(coverage["usable_field_count"], 0)

    def test_readiness_is_ready_only_when_all_cached_fixtures_are_complete(self):
        readiness = statpal_cache_readiness(
            {
                "fixtures": 2,
                "complete": 2,
                "partial": 0,
                "stale": 0,
                "identity_missing": 0,
                "average_coverage_percent": 100.0,
            }
        )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["reasons"], [])

    def test_readiness_is_degraded_when_coverage_is_usable_but_incomplete(self):
        readiness = statpal_cache_readiness(
            {
                "fixtures": 2,
                "complete": 1,
                "partial": 1,
                "stale": 0,
                "identity_missing": 0,
                "average_coverage_percent": 75.0,
            },
            minimum_average_coverage=70.0,
        )

        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["degraded"])
        self.assertEqual(readiness["status"], "degraded")
        self.assertIn("missing_snapshots_present", readiness["reasons"])

    def test_readiness_is_not_ready_when_average_is_below_threshold(self):
        readiness = statpal_cache_readiness(
            {
                "fixtures": 3,
                "complete": 0,
                "partial": 3,
                "stale": 0,
                "identity_missing": 0,
                "average_coverage_percent": 45.0,
            },
            minimum_average_coverage=70.0,
        )

        self.assertEqual(readiness["status"], "not_ready")
        self.assertIn("average_coverage_below_threshold", readiness["reasons"])

    def test_readiness_is_not_ready_without_cached_fixtures(self):
        readiness = statpal_cache_readiness({"fixtures": 0, "average_coverage_percent": 0})

        self.assertEqual(readiness["status"], "not_ready")
        self.assertIn("no_statpal_fixtures_cached", readiness["reasons"])


class StatPalReadinessApiContractTests(SimpleTestCase):
    def test_readiness_query_serializer_accepts_operational_filters(self):
        serializer = StatPalReadinessQuerySerializer(
            data={
                "start_date": "2026-08-10",
                "days": "3",
                "include_optional": "true",
                "min_coverage": "75",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["days"], 3)
        self.assertEqual(serializer.validated_data["min_coverage"], 75.0)
        self.assertTrue(serializer.validated_data["include_optional"])

    def test_readiness_query_serializer_rejects_out_of_range_window(self):
        serializer = StatPalReadinessQuerySerializer(data={"days": "99"})

        self.assertFalse(serializer.is_valid())
        self.assertIn("days", serializer.errors)
