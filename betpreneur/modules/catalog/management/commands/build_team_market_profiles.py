import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.market_profiles import MarketProfileBuilder


class Command(BaseCommand):
    help = "Build team and league market behaviour profiles from completed fixtures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--league-key",
            action="append",
            dest="league_keys",
            default=None,
            help="Registry league key to build. Can be supplied more than once.",
        )
        parser.add_argument(
            "--season",
            action="append",
            dest="seasons",
            default=None,
            help="Season to build. Can be supplied more than once.",
        )
        parser.add_argument("--min-attempts", type=int, default=1, help="Minimum sample size before saving a market profile.")

    def handle(self, *args, **options):
        result = MarketProfileBuilder().build(
            league_keys=options["league_keys"],
            seasons=options["seasons"],
            min_attempts=options["min_attempts"],
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
