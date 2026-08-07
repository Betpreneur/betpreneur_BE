import json

from django.core.management.base import BaseCommand, CommandError

from apps.algo.models import StatPalFixtureSnapshot
from apps.algo.statpal import StatPalClient, StatPalError
from apps.algo.statpal_snapshots import statpal_snapshot_service


class Command(BaseCommand):
    help = "Fetch or import StatPal fixture-level snapshots into the local cache."

    def add_arguments(self, parser):
        parser.add_argument(
            "snapshot_type",
            choices=[choice.value for choice in StatPalFixtureSnapshot.SnapshotType],
            help="Snapshot type to save.",
        )
        parser.add_argument("--endpoint", default="", help="Configured StatPal endpoint name or raw path.")
        parser.add_argument("--file", default="", help="Import a saved JSON response instead of calling StatPal.")
        parser.add_argument("--match-id", default="", help="Internal/API fixture id for this snapshot.")
        parser.add_argument("--provider-match-id", default="", help="StatPal/provider match id for this snapshot.")
        parser.add_argument("--competition-id", default="", help="Provider competition id.")
        parser.add_argument("--team-id", default="", help="Team id for team-stat endpoint templates.")
        parser.add_argument("--player-id", default="", help="Player id for player-stat endpoint templates.")
        parser.add_argument("--param", action="append", default=[], help="Extra query param in key=value form.")

    def handle(self, *args, **options):
        snapshot_type = options["snapshot_type"]
        endpoint = options["endpoint"] or self._default_endpoint(snapshot_type)
        params = {}
        for item in options["param"]:
            if "=" not in item:
                raise CommandError(f"Invalid --param value {item!r}; expected key=value.")
            key, value = item.split("=", 1)
            params[key] = value

        if options["file"]:
            with open(options["file"], encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            try:
                client = StatPalClient()
                path_params = {
                    "match_id": options["provider_match_id"] or options["match_id"],
                    "team_id": options["team_id"],
                    "player_id": options["player_id"],
                }
                if "/" in endpoint or endpoint.startswith("http"):
                    payload = client.get(endpoint, params=params)
                else:
                    payload = client.soccer_endpoint(endpoint, params=params, **path_params)
            except StatPalError as exc:
                raise CommandError(str(exc)) from exc

        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS:
            rows = statpal_snapshot_service.save_injuries_suspensions_payload(payload)
            result = {"count": len(rows), "ids": [row.id for row in rows[:25]]}
        else:
            row = statpal_snapshot_service.save_endpoint_payload(
                snapshot_type=snapshot_type,
                endpoint_name=endpoint,
                payload=payload,
                match_id=options["match_id"],
                provider_match_id=options["provider_match_id"],
                provider_competition_id=options["competition_id"],
            )
            result = {"count": 1, "id": row.id, "summary": row.summary}

        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))

    @staticmethod
    def _default_endpoint(snapshot_type):
        return {
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS: "SOCCER_INJURIES_SUSPENSIONS",
            StatPalFixtureSnapshot.SnapshotType.TEAM_STATS: "SOCCER_TEAM_STATS",
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS: "SOCCER_PREMATCH_ODDS",
            StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS: "SOCCER_LIVE_ODDS",
            StatPalFixtureSnapshot.SnapshotType.LINEUPS: "SOCCER_LINEUPS",
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS: "SOCCER_PREDICTIONS",
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS: "SOCCER_DETAILED_STATS",
        }.get(snapshot_type, "")
