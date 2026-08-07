from datetime import date
from unittest.mock import patch

from django.test import TestCase

from apps.algo.models import FixtureCache
from apps.algo.services import FixtureSearchService
from apps.algo.statpal import StatPalConfigurationError
from apps.algo.statpal_provider import normalize_daily_matches


DAILY_PAYLOAD = {
    "matches": [
        {
            "id": "sp-100",
            "date": "2026-08-07",
            "time": "18:00",
            "home": {"id": "h1", "name": "Norway"},
            "away": {"id": "a1", "name": "England"},
            "league": {"id": "1", "name": "World Cup", "country": "World"},
            "round": "Quarter-finals",
        }
    ]
}


class StatPalProviderTests(TestCase):
    def test_normalize_daily_matches_outputs_fixture_cache_shape(self):
        fixtures = normalize_daily_matches(DAILY_PAYLOAD, target_date=date(2026, 8, 7))

        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["match_id"], "statpal:sp-100")
        self.assertEqual(fixture["provider_match_id"], "sp-100")
        self.assertEqual(fixture["fixture"], "Norway vs England")
        self.assertEqual(fixture["league"], "World Cup")
        self.assertEqual(fixture["country"], "World")
        self.assertEqual(fixture["source"], "statpal")

    def test_sync_statpal_daily_upserts_fixture_cache(self):
        class DummyProvider:
            def fixtures_for_date(self, target_date):
                return normalize_daily_matches(DAILY_PAYLOAD, target_date=target_date)

        with patch("apps.algo.statpal_provider.StatPalDailyMatchProvider", return_value=DummyProvider()):
            result = FixtureSearchService().sync_statpal_daily(target_date=date(2026, 8, 7))

        self.assertEqual(result, {"synced": 1, "errors": []})
        cached = FixtureCache.objects.get(match_id="statpal:sp-100")
        self.assertEqual(cached.fixture, "Norway vs England")
        self.assertEqual(cached.source, "statpal")
        self.assertEqual(cached.api_payload["id"], "sp-100")

    def test_sync_statpal_daily_disabled_is_silent(self):
        class DisabledProvider:
            def fixtures_for_date(self, target_date):
                raise StatPalConfigurationError("disabled")

        with patch("apps.algo.statpal_provider.StatPalDailyMatchProvider", return_value=DisabledProvider()):
            result = FixtureSearchService().sync_statpal_daily(target_date=date(2026, 8, 7))

        self.assertEqual(result, {"synced": 0, "errors": []})
