from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.algo.models import StatPalFixtureSnapshot


class StatPalSnapshotApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="pass",
        )

    def test_fixture_context_returns_compact_snapshots(self):
        StatPalFixtureSnapshot.objects.create(
            match_id="1581037",
            provider_match_id="statpal-match-1",
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            source_endpoint="SOCCER_LINEUPS",
            summary={"home_confirmed": True},
            payload={
                "id": "statpal:lineups:statpal-match-1",
                "raw": {"large": "raw provider payload should not be returned"},
                "home": {"starting_xi": [{"id": "p1", "raw": {"hidden": True}}]},
            },
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("algo-statpal-fixture-context"), {"match_id": "1581037"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["match_id"], "1581037")
        self.assertTrue(body["context"]["available"])
        lineups = body["context"]["snapshots"]["lineups"]
        self.assertEqual(lineups["summary"]["home_confirmed"], True)
        self.assertTrue(lineups["payload_available"])
        self.assertEqual(lineups["payload"]["id"], "statpal:lineups:statpal-match-1")
        self.assertNotIn("raw", lineups["payload"])
        self.assertNotIn("raw", lineups["payload"]["home"]["starting_xi"][0])

    def test_fixture_context_requires_fixture_identifier(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("algo-statpal-fixture-context"))

        self.assertEqual(response.status_code, 400)

    def test_fixture_context_can_refresh_without_forcing(self):
        self.client.force_authenticate(self.user)
        with patch("apps.algo.api.market_data_views.statpal_snapshot_service.refresh_fixture_snapshots") as refresh:
            with patch("apps.algo.api.market_data_views.statpal_snapshot_service.fixture_context") as context:
                refresh.return_value = {
                    "attempted": [],
                    "refreshed": [],
                    "skipped": [],
                    "errors": [],
                    "api_usage": {"attempted_calls": 1},
                }
                context.return_value = {"available": False, "snapshots": {}}

                response = self.client.get(
                    reverse("algo-statpal-fixture-context"),
                    {"match_id": "1581037", "refresh": "true"},
                )

        self.assertEqual(response.status_code, 200)
        refresh.assert_called_once_with(match_id="1581037", provider_match_id="", force=False)
        self.assertIn("refreshed", response.json())
        self.assertNotIn("api_usage", response.json()["refreshed"])

    def test_refresh_endpoint_requires_admin_user(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse("algo-statpal-fixture-refresh"),
            {"match_id": "1581037", "snapshot_types": ["lineups"]},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_refresh_endpoint_returns_context(self):
        self.client.force_authenticate(self.admin)
        with patch("apps.algo.api.market_data_views.statpal_snapshot_service.refresh_fixture_snapshots") as refresh:
            with patch("apps.algo.api.market_data_views.statpal_snapshot_service.fixture_context") as context:
                refresh.return_value = {
                    "attempted": ["lineups"],
                    "refreshed": [{"snapshot_type": "lineups", "snapshot_id": 1}],
                    "skipped": [],
                    "errors": [],
                    "api_usage": {"attempted_calls": 1},
                }
                context.return_value = {"available": True, "snapshots": {"lineups": {"summary": {}}}}

                response = self.client.post(
                    reverse("algo-statpal-fixture-refresh"),
                    {
                        "match_id": "1581037",
                        "provider_match_id": "statpal-match-1",
                        "provider_competition_id": "3037",
                        "snapshot_types": ["lineups"],
                        "force": True,
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200)
        refresh.assert_called_once_with(
            match_id="1581037",
            provider_match_id="statpal-match-1",
            provider_competition_id="3037",
            force=True,
            snapshot_types=["lineups"],
        )
        self.assertEqual(response.json()["context"]["available"], True)
        self.assertNotIn("api_usage", response.json()["refreshed"])
