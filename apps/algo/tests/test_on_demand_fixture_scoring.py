from datetime import date

from django.test import TestCase

from apps.algo.models import FixtureCache
from apps.algo.services import AlgoRunnerService


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
