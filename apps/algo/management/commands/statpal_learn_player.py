import json

from django.core.management.base import BaseCommand, CommandError

from apps.algo.market_data.api import StatPalClient, StatPalError, provider_mapping_service


class Command(BaseCommand):
    help = "Learn StatPal provider player/team mappings from a player-stat payload."

    def add_arguments(self, parser):
        parser.add_argument("--player-id", default="", help="Fetch and learn this StatPal player id.")
        parser.add_argument("--file", default="", help="Learn from a saved StatPal player JSON payload.")
        parser.add_argument("--raw", action="store_true", help="Print the learned mapping payload.")

    def handle(self, *args, **options):
        if not options["player_id"] and not options["file"]:
            raise CommandError("Provide --player-id or --file.")

        if options["file"]:
            with open(options["file"], encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            try:
                payload = StatPalClient().soccer_endpoint(
                    "SOCCER_PLAYER_STATS",
                    player_id=options["player_id"],
                )
            except StatPalError as exc:
                raise CommandError(str(exc)) from exc

        mapping = provider_mapping_service.learn_statpal_player_payload(payload)
        if not mapping:
            raise CommandError("Could not learn player mapping from this payload.")

        result = {
            "provider": mapping.provider,
            "provider_player_id": mapping.provider_player_id,
            "provider_player_name": mapping.provider_player_name,
            "provider_team_id": mapping.provider_team_id,
            "provider_team_name": mapping.provider_team_name,
            "position": mapping.position,
            "nationality": mapping.nationality,
            "verified_at": mapping.verified_at,
        }
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
        if options["raw"]:
            self.stdout.write(json.dumps(mapping.payload, indent=2, sort_keys=True, default=str))
