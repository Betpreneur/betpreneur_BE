import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.historical_hydrator import HistoricalTeamHydrator


class Command(BaseCommand):
    help = "Hydrate current/previous season team profiles for the Team Intelligence Store."

    def add_arguments(self, parser):
        parser.add_argument(
            "--league-key",
            action="append",
            dest="league_keys",
            default=None,
            help="Registry league key to hydrate. Can be supplied more than once.",
        )
        parser.add_argument(
            "--season",
            action="append",
            dest="seasons",
            default=None,
            help="Season to hydrate. Can be supplied more than once. Defaults to registry current and previous seasons.",
        )
        parser.add_argument("--max-teams", type=int, default=None, help="Limit teams per league/season for smoke testing.")

    def handle(self, *args, **options):
        result = HistoricalTeamHydrator().hydrate(
            league_keys=options["league_keys"],
            seasons=options["seasons"],
            max_teams=options["max_teams"],
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
