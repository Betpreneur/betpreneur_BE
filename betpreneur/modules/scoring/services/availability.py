"""
Player availability: injuries and suspensions.

The dominant error in player props is not modelling — it is pricing a player who will
not be on the pitch. A prop on an injured or suspended player is a **dead** bet, not a
risky one, so the correct output is "unavailable", never a low score. Scoring it down
would leave it looking like a judgement call the user could disagree with.

Identity is matched on normalised name plus team, because SportyBet carries Sportradar
player ids and StatPal carries its own; the two ranges were checked and share nothing.
Name matching is inherently lossy, so an unresolved player is reported as unresolved
rather than assumed fit.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from django.utils import timezone

from betpreneur.modules.scoring.models import PlayerAvailability

log = logging.getLogger(__name__)

OUT = PlayerAvailability.Status.OUT
DOUBTFUL = PlayerAvailability.Status.DOUBTFUL


def normalize_person(value: str) -> str:
    """
    Normalise a player name for comparison.

    Feeds disagree on form: SportyBet sends `Haller, Sebastian (Sanfrecce Hiroshima)`
    while StatPal sends `S. Haller`. Surname plus initial is the only reliably shared
    part, so that is what we compare on.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.split("(")[0]                      # drop a trailing "(Team Name)"
    text = text.replace(".", " ").replace("-", " ")
    text = "".join(char for char in text.lower() if char.isalnum() or char.isspace())
    return " ".join(text.split())


def name_keys(value: str) -> set[str]:
    """Candidate keys for one name: the full form and `surname initial`."""
    normalized = normalize_person(value)
    if not normalized:
        return set()
    keys = {normalized}
    if "," in str(value):
        surname, _, rest = str(value).partition(",")
        surname = normalize_person(surname)
        given = normalize_person(rest)
        if surname:
            keys.add(surname)
            if given:
                keys.add(f"{given[0]} {surname}")
    else:
        parts = normalized.split()
        if len(parts) >= 2:
            keys.add(parts[-1])
            keys.add(f"{parts[0][0]} {parts[-1]}")
    return {key for key in keys if key}


def _players(node):
    """`player` is a bare dict for one entry and a list for several."""
    players = (node or {}).get("player")
    if isinstance(players, dict):
        return [players]
    return players or []


def parse_injuries_payload(payload) -> list[dict]:
    """Flatten the league -> match -> side -> sidelined structure into rows."""
    rows = []
    leagues = ((payload or {}).get("injuries_suspensions") or {}).get("league") or []
    if isinstance(leagues, dict):
        leagues = [leagues]
    for league in leagues:
        matches = league.get("match") or []
        if isinstance(matches, dict):
            matches = [matches]
        for match in matches:
            for side in ("home", "away"):
                team = match.get(side) or {}
                sidelined = team.get("sidelined") or {}
                for bucket, status in (("to_miss", OUT), ("questionable", DOUBTFUL)):
                    for player in _players(sidelined.get(bucket)):
                        rows.append({
                            "player_id": str(player.get("id") or ""),
                            "player_name": str(player.get("name") or ""),
                            "team_id": str(team.get("id") or ""),
                            "team_name": str(team.get("name") or ""),
                            "match_id": str(match.get("main_id") or ""),
                            "status": status,
                            "reason": str(player.get("status") or "")[:120],
                        })
    return rows


@dataclass(frozen=True)
class AvailabilityVerdict:
    resolved: bool
    status: str = ""
    reason: str = ""
    player_name: str = ""

    @property
    def is_out(self) -> bool:
        return self.status == OUT

    @property
    def is_doubtful(self) -> bool:
        return self.status == DOUBTFUL

    @property
    def playable(self) -> bool:
        """Unresolved counts as not playable: we decline rather than assume fitness."""
        return self.resolved and not self.is_out


class PlayerAvailabilityService:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from betpreneur.modules.catalog.api import statpal_client

            self._client = statpal_client()
        return self._client

    def refresh(self, payload=None) -> dict:
        """Reload the sidelined list. One league-wide call covers every fixture."""
        if payload is None:
            payload = self.client.soccer_endpoint("SOCCER_INJURIES_SUSPENSIONS")
        rows = parse_injuries_payload(payload)

        PlayerAvailability.objects.all().delete()
        PlayerAvailability.objects.bulk_create(
            [
                PlayerAvailability(
                    provider="statpal",
                    player_id=row["player_id"],
                    player_name=row["player_name"],
                    player_name_normalized=normalize_person(row["player_name"]),
                    team_id=row["team_id"],
                    team_name=row["team_name"],
                    team_name_normalized=normalize_person(row["team_name"]),
                    match_id=row["match_id"],
                    status=row["status"],
                    reason=row["reason"],
                )
                for row in rows
            ],
            batch_size=500,
        )
        return {
            "rows": len(rows),
            "out": sum(1 for row in rows if row["status"] == OUT),
            "doubtful": sum(1 for row in rows if row["status"] == DOUBTFUL),
            "refreshed_at": timezone.now().isoformat(),
        }

    def verdict_for(self, *, player_name: str, team_name: str = "", match_id: str = "") -> AvailabilityVerdict:
        """
        Whether this player is known to be sidelined.

        Absence from the sidelined list means available — the feed lists only players who
        are out or doubtful. An unparseable name is reported unresolved instead.
        """
        keys = name_keys(player_name)
        if not keys:
            return AvailabilityVerdict(resolved=False, reason="player_name_unreadable")

        try:
            queryset = PlayerAvailability.objects.all()
            if match_id:
                scoped = queryset.filter(match_id=str(match_id))
                queryset = scoped if scoped.exists() else queryset
            if team_name:
                team_key = normalize_person(team_name)
                scoped = queryset.filter(team_name_normalized=team_key)
                queryset = scoped if scoped.exists() else queryset

            rows = list(queryset.only("player_name", "player_name_normalized", "status", "reason"))
        except Exception as exc:
            # Availability is a safety check layered on top of pricing. If the feed or
            # its table is unreachable we say so and let the market be priced, rather
            # than letting an infrastructure fault silently kill every player prop.
            log.info("Player availability lookup failed: %s", str(exc)[:200])
            return AvailabilityVerdict(resolved=False, reason="availability_check_unavailable")

        for row in rows:
            if name_keys(row.player_name) & keys:
                return AvailabilityVerdict(
                    resolved=True, status=row.status, reason=row.reason, player_name=row.player_name
                )
        return AvailabilityVerdict(resolved=True, status="", player_name=player_name)


player_availability_service = PlayerAvailabilityService()
