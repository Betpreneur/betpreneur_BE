import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.algo.statpal_daily_build import DEFAULT_BUILD_DAYS, StatPalDailyBuildService


class Command(BaseCommand):
    help = "Build the StatPal cache for today, tomorrow, and next tomorrow."

    def add_arguments(self, parser):
        parser.add_argument("--start-date", default="", help="Start date in YYYY-MM-DD format. Defaults to today.")
        parser.add_argument("--days", type=int, default=DEFAULT_BUILD_DAYS, help="Number of days to build.")
        parser.add_argument("--force", action="store_true", help="Refresh even when a fresh snapshot exists.")
        parser.add_argument("--include-optional", action="store_true", help="Include optional/player/live endpoint groups.")
        parser.add_argument("--max-tasks", type=int, default=None, help="Limit endpoint tasks for smoke testing.")
        parser.add_argument("--readiness-only", action="store_true", help="Inspect cached StatPal coverage without making API calls.")
        parser.add_argument("--min-coverage", type=float, default=70.0, help="Minimum average coverage percentage for readiness.")
        parser.add_argument("--fail-under-threshold", action="store_true", help="Exit non-zero when readiness is not ready.")

    def handle(self, *args, **options):
        start_date = timezone.localdate()
        if options["start_date"]:
            start_date = datetime.strptime(options["start_date"], "%Y-%m-%d").date()

        service = StatPalDailyBuildService()
        if options["readiness_only"]:
            result = service.readiness_report(
                start_date=start_date,
                days=options["days"],
                include_optional=options["include_optional"],
                minimum_average_coverage=options["min_coverage"],
            )
        else:
            result = service.build(
                start_date=start_date,
                days=options["days"],
                include_optional=options["include_optional"],
                force=options["force"],
                max_tasks=options["max_tasks"],
            )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
        if options["fail_under_threshold"] and not (result.get("readiness") or {}).get("ready"):
            raise CommandError((result.get("readiness") or {}).get("summary") or "StatPal cache is not ready.")
