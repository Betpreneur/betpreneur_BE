import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.services.coach_intelligence import coach_intelligence_sync_service


class Command(BaseCommand):
    help = "Import current coaches for teams in the tracked StatPal leagues."

    def add_arguments(self, parser):
        parser.add_argument("--league", action="append", dest="league_keys")
        parser.add_argument("--max-teams", type=int)
        parser.add_argument("--include-coach-details", action="store_true")

    def handle(self, *args, **options):
        result = coach_intelligence_sync_service.sync(
            league_keys=options.get("league_keys"),
            max_teams=options.get("max_teams"),
            include_coach_details=options.get("include_coach_details", False),
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if result.get("errors"):
            self.stdout.write(self.style.WARNING("Coach sync completed with errors."))
        else:
            self.stdout.write(self.style.SUCCESS("Coach sync complete."))
