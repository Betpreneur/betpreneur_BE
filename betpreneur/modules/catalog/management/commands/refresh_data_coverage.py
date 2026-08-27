import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.coverage_tracker import DataCoverageTracker


class Command(BaseCommand):
    help = "Refresh derived Team Intelligence data coverage and readiness rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--league-key",
            action="append",
            dest="league_keys",
            default=None,
            help="Registry league key to inspect. Can be supplied more than once.",
        )
        parser.add_argument(
            "--season",
            action="append",
            dest="seasons",
            default=None,
            help="Season to inspect. Can be supplied more than once.",
        )
        parser.add_argument("--ttl-hours", type=int, default=24, help="Freshness TTL for readiness rows.")

    def handle(self, *args, **options):
        result = DataCoverageTracker().refresh(
            league_keys=options["league_keys"],
            seasons=options["seasons"],
            ttl_hours=options["ttl_hours"],
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
