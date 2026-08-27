import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.recent_form import (
    DEFAULT_RECENT_FORM_WINDOWS,
    RecentFormBuilder,
)


class Command(BaseCommand):
    help = "Build rolling recent-form profiles for Team Intelligence teams."

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
        parser.add_argument(
            "--window",
            action="append",
            dest="windows",
            type=int,
            default=None,
            help="Rolling window size. Can be supplied more than once.",
        )
        parser.add_argument("--no-sync", action="store_true", help="Use already cached fixtures only.")
        parser.add_argument("--max-matches", type=int, default=None, help="Limit synced matches per league/season.")

    def handle(self, *args, **options):
        result = RecentFormBuilder().build(
            league_keys=options["league_keys"],
            seasons=options["seasons"],
            windows=options["windows"] or DEFAULT_RECENT_FORM_WINDOWS,
            sync_matches=not options["no_sync"],
            max_matches=options["max_matches"],
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
