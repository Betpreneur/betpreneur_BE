from datetime import date
from unittest import mock

from django.test import TestCase

from apps.algo.models import FixtureCache
from apps.algo.services import AlgoRunnerService, FixtureSearchService


class OnDemandFixtureScoringPayloadTests(TestCase):
    def test_cached_provider_payload_is_normalized_for_runner(self):
        cached = FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            league="Premiership",
            country="Scotland",
            match_id="1556634",
            api_payload={
                "fixture": {
                    "id": 1556634,
                    "date": "2026-08-08T15:00:00+01:00",
                },
                "league": {
                    "id": 179,
                    "name": "Premiership",
                    "country": "Scotland",
                    "season": 2026,
                },
                "teams": {
                    "home": {"id": 246, "name": "Dundee", "logo": ""},
                    "away": {"id": 250, "name": "Aberdeen", "logo": ""},
                },
            },
        )

        payload = AlgoRunnerService()._cached_fixture_runner_payload(cached)

        self.assertEqual(payload["aps_id"], 1556634)
        self.assertEqual(payload["hid"], 246)
        self.assertEqual(payload["aid"], 250)
        self.assertEqual(payload["hname"], "Dundee")
        self.assertEqual(payload["aname"], "Aberdeen")
        self.assertEqual(payload["code"], "179")
        self.assertEqual(payload["season"], 2026)

    def test_numeric_cached_match_id_backfills_aps_id(self):
        cached = FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Forge vs Vancouver FC",
            home_team="Forge",
            away_team="Vancouver FC",
            league="Canadian Premier League",
            country="Canada",
            match_id="1517321",
            api_payload={
                "fixture": "Forge vs Vancouver FC",
                "hname": "Forge",
                "aname": "Vancouver FC",
            },
        )

        payload = AlgoRunnerService()._cached_fixture_runner_payload(cached)

        self.assertEqual(payload["aps_id"], "1517321")
        self.assertEqual(payload["match_id"], "1517321")

    def test_statpal_only_fixture_is_not_backfilled_as_api_football_fixture(self):
        cached = FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Team A vs Team B",
            home_team="Team A",
            away_team="Team B",
            match_id="statpal:abc123",
            source="statpal",
            api_payload={},
        )

        payload = AlgoRunnerService()._cached_fixture_runner_payload(cached)

        self.assertNotIn("aps_id", payload)

    def test_cross_provider_enrichment_adds_api_football_to_statpal_fixture(self):
        FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            home_team_normalized="dundee",
            away_team_normalized="aberdeen",
            fixture_normalized="dundee vs aberdeen",
            match_id="1556634",
            source="aps_provider_lookup",
            api_payload={
                "provider_competition_id": "179",
                "provider_home_team_id": "246",
                "provider_away_team_id": "250",
            },
        )
        statpal = {
            "fixture": "Dundee vs Aberdeen",
            "hname": "Dundee",
            "aname": "Aberdeen",
            "match_id": "statpal:sp-991",
            "source": "statpal_daily_cache",
            "statpal_provider_match_id": "sp-991",
            "statpal_provider_competition_id": "3203",
            "statpal_home_team_id": "2341",
            "statpal_away_team_id": "2342",
            "hid": "2341",
            "aid": "2342",
            "code": "3203",
        }

        enriched = AlgoRunnerService()._enrich_fixture_for_cross_provider_scoring(statpal, date(2026, 8, 8))

        self.assertEqual(enriched["match_id"], "statpal:sp-991")
        self.assertEqual(enriched["hid"], "2341")
        self.assertEqual(enriched["aid"], "2342")
        self.assertEqual(enriched["api_football_fixture_id"], "1556634")
        self.assertEqual(enriched["api_football_home_team_id"], "246")
        self.assertEqual(enriched["api_football_away_team_id"], "250")
        self.assertTrue(enriched["provider_merge"]["api_football"]["matched"])

    def test_cross_provider_enrichment_adds_statpal_to_api_football_fixture(self):
        FixtureCache.objects.create(
            match_date=date(2026, 8, 8),
            fixture="Dundee vs Aberdeen",
            home_team="Dundee",
            away_team="Aberdeen",
            home_team_normalized="dundee",
            away_team_normalized="aberdeen",
            fixture_normalized="dundee vs aberdeen",
            match_id="statpal:sp-991",
            source="statpal",
            api_payload={
                "provider_match_id": "sp-991",
                "provider_competition_id": "3203",
                "provider_home_team_id": "2341",
                "provider_away_team_id": "2342",
            },
        )
        api_fixture = {
            "fixture": "Dundee vs Aberdeen",
            "hname": "Dundee",
            "aname": "Aberdeen",
            "match_id": "1556634",
            "aps_id": "1556634",
            "source": "aps_provider_lookup",
            "hid": "246",
            "aid": "250",
            "code": "179",
        }

        with mock.patch.object(FixtureSearchService, "sync_statpal_daily", return_value={"synced": 0, "errors": []}):
            enriched = AlgoRunnerService()._enrich_fixture_for_cross_provider_scoring(api_fixture, date(2026, 8, 8))

        self.assertEqual(enriched["match_id"], "1556634")
        self.assertEqual(enriched["aps_id"], "1556634")
        self.assertEqual(enriched["statpal_match_id"], "statpal:sp-991")
        self.assertEqual(enriched["statpal_provider_match_id"], "sp-991")
        self.assertEqual(enriched["statpal_provider_competition_id"], "3203")
        self.assertEqual(enriched["statpal_home_team_id"], "2341")
        self.assertEqual(enriched["statpal_away_team_id"], "2342")
        self.assertTrue(enriched["provider_merge"]["statpal"]["matched"])

    def test_algo_runner_service_hydrates_statpal_context(self):
        refresh = {"errors": [], "refreshed": ["form"]}
        context = {"available": True, "snapshots": {"form": {"home": {}}}}
        snapshots = mock.Mock()
        snapshots.refresh_fixture_snapshots.return_value = refresh
        snapshots.fixture_context.return_value = context

        with mock.patch(
            "apps.algo.statpal_snapshots.statpal_snapshot_service", snapshots
        ):
            fixture = AlgoRunnerService()._hydrate_statpal_scoring_context({
                "match_id": "1556634",
                "aps_id": "1556634",
                "code": "179",
                "statpal_provider_match_id": "sp-991",
                "fixture": "Dundee vs Aberdeen",
            })

        snapshots.refresh_fixture_snapshots.assert_called_once_with(
            match_id="1556634",
            provider_match_id="sp-991",
            provider_competition_id="179",
        )
        snapshots.fixture_context.assert_called_once_with(
            match_id="1556634",
            provider_match_id="sp-991",
        )
        self.assertEqual(fixture["statpal_refresh"], refresh)
        self.assertEqual(fixture["statpal_context"], context)
        self.assertEqual(fixture["fixture"], "Dundee vs Aberdeen")

    def test_statpal_hydration_failure_does_not_break_scoring_payload(self):
        snapshots = mock.Mock()
        snapshots.refresh_fixture_snapshots.side_effect = RuntimeError("statpal down")

        with mock.patch(
            "apps.algo.statpal_snapshots.statpal_snapshot_service", snapshots
        ):
            fixture = AlgoRunnerService()._hydrate_statpal_scoring_context({
                "match_id": "1556634",
                "fixture": "Dundee vs Aberdeen",
            })

        self.assertEqual(fixture["statpal_context"], {"available": False, "snapshots": {}})
        self.assertEqual(
            fixture["statpal_refresh"]["errors"],
            [{"provider": "statpal", "error": "statpal down"}],
        )

    def test_statpal_hydration_skipped_without_identifiers(self):
        source = {"fixture": "Dundee vs Aberdeen"}

        fixture = AlgoRunnerService()._hydrate_statpal_scoring_context(source)

        self.assertIs(fixture, source)
        self.assertNotIn("statpal_context", fixture)
