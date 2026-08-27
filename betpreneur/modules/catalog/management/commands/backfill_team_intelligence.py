import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.team_intelligence_backfill import (
    team_intelligence_backfill_service,
)


class Command(BaseCommand):
    help = "Run one-time Team Intelligence backfill for top leagues and print monitoring output."

    def add_arguments(self, parser):
        parser.add_argument(
            "--league-key",
            action="append",
            dest="league_keys",
            default=None,
            help="Registry league key to backfill. Can be supplied more than once.",
        )
        parser.add_argument("--max-teams", type=int, default=None, help="Limit teams per league/season for smoke testing.")
        parser.add_argument("--max-matches", type=int, default=None, help="Limit synced matches per league/season.")
        parser.add_argument("--min-attempts", type=int, default=1, help="Minimum sample size before saving market profiles.")
        parser.add_argument("--ttl-hours", type=int, default=24, help="Freshness TTL for coverage rows.")
        parser.add_argument(
            "--no-sync-recent-matches",
            action="store_true",
            help="Build recent form from cached historical fixtures only.",
        )
        parser.add_argument(
            "--monitor-only",
            action="store_true",
            help="Print the current Team Intelligence monitoring report without running backfill.",
        )

    def handle(self, *args, **options):
        if options["monitor_only"]:
            result = team_intelligence_backfill_service.monitoring_report(
                league_keys=options["league_keys"],
            )
            self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
            return

        result = team_intelligence_backfill_service.backfill(
            league_keys=options["league_keys"],
            max_teams=options["max_teams"],
            max_matches=options["max_matches"],
            min_attempts=options["min_attempts"],
            ttl_hours=options["ttl_hours"],
            sync_recent_matches=not options["no_sync_recent_matches"],
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
