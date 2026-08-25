import json

from django.core.management.base import BaseCommand, CommandError

from betpreneur.modules.catalog.services.provider_client import (
    StatPalError,
    statpal_client,
)


class Command(BaseCommand):
    help = "Probe a StatPal endpoint and print a compact JSON summary."

    def add_arguments(self, parser):
        parser.add_argument(
            "endpoint",
            nargs="?",
            default="SOCCER_LIVE_MATCHES",
            help=(
                "Configured endpoint name, e.g. SOCCER_LIVE_MATCHES, "
                "SOCCER_INJURIES_SUSPENSIONS, SOCCER_PLAYER_STATS, or a raw path."
            ),
        )
        parser.add_argument("--player-id", default="", help="Player id for endpoint templates.")
        parser.add_argument("--team-id", default="", help="Team id for endpoint templates.")
        parser.add_argument("--param", action="append", default=[], help="Extra query param in key=value form.")
        parser.add_argument("--raw", action="store_true", help="Print full JSON response.")

    def handle(self, *args, **options):
        endpoint = options["endpoint"]
        params = {}
        for item in options["param"]:
            if "=" not in item:
                raise CommandError(f"Invalid --param value {item!r}; expected key=value.")
            key, value = item.split("=", 1)
            params[key] = value

        path_params = {
            "player_id": options.get("player_id") or "",
            "team_id": options.get("team_id") or "",
        }
        client = statpal_client()
        try:
            if "/" in endpoint or endpoint.startswith("http"):
                payload = client.get(endpoint, params=params)
            else:
                payload = client.soccer_endpoint(endpoint, params=params, **path_params)
        except StatPalError as exc:
            raise CommandError(str(exc)) from exc

        if options["raw"]:
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return

        summary = {
            "type": type(payload).__name__,
            "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            "result_count": self._result_count(payload),
            "sample": self._sample(payload),
        }
        self.stdout.write(json.dumps(summary, indent=2, sort_keys=True, default=str))

    @staticmethod
    def _result_count(payload):
        if isinstance(payload, list):
            return len(payload)
        if not isinstance(payload, dict):
            return None
        for key in ("response", "data", "matches", "league", "injuries_suspensions"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                nested = value.get("league") or value.get("match")
                if isinstance(nested, list):
                    return len(nested)
        return None

    @staticmethod
    def _sample(payload):
        if isinstance(payload, list):
            return payload[:1]
        if not isinstance(payload, dict):
            return payload
        for key in ("response", "data", "matches", "league"):
            value = payload.get(key)
            if isinstance(value, list):
                return value[:1]
        return {key: payload[key] for key in list(payload.keys())[:5]}
