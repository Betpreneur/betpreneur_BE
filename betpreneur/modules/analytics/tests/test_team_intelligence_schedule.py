from django.conf import settings
from django.test import SimpleTestCase

from betpreneur.modules.analytics.interface.views import _maintenance_jobs
from config.celery.schedules import BEAT_SCHEDULE


class TeamIntelligenceScheduleTests(SimpleTestCase):
    def test_nightly_refresh_is_scheduled_before_daily_products(self):
        nightly = BEAT_SCHEDULE["refresh-team-intelligence-nightly"]

        self.assertEqual(
            nightly["task"],
            "betpreneur.modules.analytics.tasks.refresh_team_intelligence_nightly",
        )
        self.assertEqual(nightly["kwargs"]["days"], 3)
        self.assertIn("generate-daily-picks", BEAT_SCHEDULE)
        self.assertIn("build-slip-review-market-cache", BEAT_SCHEDULE)

    def test_intelligence_tasks_are_routed_to_expected_queues(self):
        routes = settings.CELERY_TASK_ROUTES

        self.assertEqual(
            routes["betpreneur.modules.analytics.tasks.refresh_team_intelligence_nightly"]["queue"],
            settings.ALGO_MAINTENANCE_QUEUE,
        )
        self.assertEqual(
            routes["betpreneur.modules.catalog.tasks.hydrate_team_intelligence_history"]["queue"],
            settings.ALGO_STATPAL_QUEUE,
        )
        self.assertEqual(
            routes["betpreneur.modules.catalog.tasks.build_team_recent_form"]["queue"],
            settings.ALGO_STATPAL_QUEUE,
        )
        self.assertEqual(
            routes["betpreneur.modules.catalog.tasks.build_team_market_profiles"]["queue"],
            settings.ALGO_STATPAL_QUEUE,
        )
        self.assertEqual(
            routes["betpreneur.modules.catalog.tasks.refresh_team_data_coverage"]["queue"],
            settings.ALGO_MAINTENANCE_QUEUE,
        )
        self.assertEqual(
            routes["betpreneur.modules.catalog.tasks.backfill_team_intelligence"]["queue"],
            settings.ALGO_STATPAL_QUEUE,
        )

    def test_ordered_refresh_can_be_triggered_from_maintenance_jobs(self):
        jobs = _maintenance_jobs()

        self.assertIn("team_intelligence_nightly", jobs)
        self.assertIn("team_intelligence_backfill", jobs)
