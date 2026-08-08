"""
Staff endpoint for queueing the Match Checker's data jobs.

These jobs make roughly two thousand provider calls between them, so the endpoint is
staff-only and queues rather than running inline — an open or blocking version would be
a quota-drain vector and a request that never returns.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient


class MaintenanceEndpointTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="ops", email="ops@example.com", password="pw", is_staff=True
        )
        self.punter = get_user_model().objects.create_user(
            username="punter", email="p@example.com", password="pw"
        )
        self.client = APIClient()
        self.url = reverse("algo-maintenance-run")

        patcher = mock.patch("apps.algo.views._maintenance_jobs")
        self.jobs = patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = {}

        def _fake(name):
            task = mock.Mock()

            def _delay(**kwargs):
                self.calls[name] = kwargs
                return mock.Mock(id=f"task-{name}")

            task.delay.side_effect = _delay
            return task

        self.jobs.return_value = {
            "fixture_horizon": (_fake("fixture_horizon"), "Cache fixtures"),
            "score_models": (_fake("score_models"), "Refit models"),
            "player_availability": (_fake("player_availability"), "Reload injuries"),
        }

    def _post(self, body=None):
        return self.client.post(self.url, body or {}, format="json")

    def test_an_anonymous_request_is_rejected(self):
        self.assertIn(self._post().status_code, (401, 403))

    def test_a_normal_user_cannot_run_the_jobs(self):
        self.client.force_authenticate(user=self.punter)

        self.assertEqual(self._post().status_code, 403)

    def test_staff_can_queue_every_job(self):
        self.client.force_authenticate(user=self.staff)

        response = self._post()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["queued"]), 3)

    def test_a_subset_can_be_requested(self):
        self.client.force_authenticate(user=self.staff)

        response = self._post({"jobs": ["score_models"]})

        queued = response.json()["queued"]
        self.assertEqual([item["job"] for item in queued], ["score_models"])

    def test_the_response_carries_task_ids_and_where_to_poll(self):
        self.client.force_authenticate(user=self.staff)

        payload = self._post({"jobs": ["score_models"]}).json()

        self.assertEqual(payload["queued"][0]["task_id"], "task-score_models")
        self.assertIn("/api/algo/tasks/", payload["poll"])

    def test_the_horizon_job_receives_the_requested_window(self):
        self.client.force_authenticate(user=self.staff)

        self._post({"jobs": ["fixture_horizon"], "days": 5})

        self.assertEqual(self.calls["fixture_horizon"], {"days": 5})

    def test_an_unknown_job_is_rejected_and_lists_the_valid_ones(self):
        self.client.force_authenticate(user=self.staff)

        response = self._post({"jobs": ["drop_everything"]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("drop_everything", response.json()["detail"])
        self.assertIn("score_models", response.json()["available"])

    def test_an_out_of_range_window_is_rejected(self):
        self.client.force_authenticate(user=self.staff)

        self.assertEqual(self._post({"jobs": ["fixture_horizon"], "days": 99}).status_code, 400)

    def test_nothing_is_queued_when_a_job_name_is_invalid(self):
        self.client.force_authenticate(user=self.staff)

        self._post({"jobs": ["score_models", "nonsense"]})

        self.assertEqual(self.calls, {})

    def test_an_unrecognised_field_is_rejected_rather_than_running_everything(self):
        # Omitting `jobs` means "run everything", so a mistyped key must fail loudly
        # instead of quietly launching every job.
        self.client.force_authenticate(user=self.staff)

        response = self._post({"task": "fit_score_models"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("task", response.json()["unknown_fields"])
        self.assertEqual(self.calls, {})

    def test_an_empty_body_still_means_run_everything(self):
        self.client.force_authenticate(user=self.staff)

        response = self._post({})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["queued"]), 3)
