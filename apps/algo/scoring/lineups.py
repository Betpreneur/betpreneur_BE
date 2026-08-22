"""
Team sheets: projected and confirmed.

The distinction is everything. StatPal reports `confidence: 100` once a lineup is
confirmed, and only then does an omission become a fact:

* **Confirmed sheet, player absent** — the bet is dead. Refuse to price it.
* **Confirmed sheet, player benched** — still live, but priced down: they may not appear.
* **Projected sheet, player absent** — a signal, not a fact. Flag it and carry on.

Treating a projected omission as certain would kill perfectly good props on the strength
of a guess, which is the same class of error as pricing an injured player.

Names are matched rather than ids, for the reason established in `availability`:
SportyBet carries Sportradar player ids and StatPal carries its own, and the two ranges
do not overlap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import FixtureLineup
from .availability import name_keys, normalize_person

log = logging.getLogger(__name__)

STARTING, BENCH, OMITTED, UNKNOWN = "starting", "bench", "omitted", "unknown"


@dataclass(frozen=True)
class LineupVerdict:
    status: str = UNKNOWN
    confirmed: bool = False
    team_name: str = ""

    @property
    def blocks_pricing(self) -> bool:
        """Only a confirmed omission makes the bet dead."""
        return self.confirmed and self.status == OMITTED

    @property
    def rotation_risk(self) -> bool:
        return self.status == BENCH

    def to_dict(self):
        return {"status": self.status, "confirmed": self.confirmed, "team": self.team_name}


def parse_lineups_payload(payload, *, match_id: str) -> list[dict]:
    rows = []
    for side in ("home", "away"):
        node = (payload or {}).get(side) or {}
        if not node:
            continue
        rows.append({
            "match_id": str(match_id),
            "side": side,
            "team_id": str(node.get("team_id") or ""),
            "team_name": str(node.get("team_name") or ""),
            "formation": str(node.get("team_formation") or ""),
            "confidence": int(node.get("confidence") or 0),
            "starting_xi": [
                {"id": str(p.get("id") or ""), "name": str(p.get("name") or ""),
                 "position": str(p.get("position") or "")}
                for p in node.get("starting_xi") or []
            ],
            "bench": [
                {"id": str(p.get("id") or ""), "name": str(p.get("name") or ""),
                 "position": str(p.get("position") or "")}
                for p in node.get("bench") or []
            ],
        })
    return rows


class LineupService:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from ..market_data.api import StatPalClient

            self._client = StatPalClient()
        return self._client

    def refresh(self, *, match_id: str, payload=None) -> dict:
        """Fetch and store both sides of one fixture's team sheet."""
        if payload is None:
            payload = self.client.soccer_endpoint("SOCCER_TEAM_LINEUPS", params={"match_id": str(match_id)})
        rows = parse_lineups_payload(payload, match_id=match_id)
        for row in rows:
            FixtureLineup.objects.update_or_create(
                provider="statpal",
                match_id=row["match_id"],
                side=row["side"],
                defaults={
                    "team_id": row["team_id"],
                    "team_name": row["team_name"],
                    "team_name_normalized": normalize_person(row["team_name"]),
                    "formation": row["formation"],
                    "confidence": row["confidence"],
                    "starting_xi": row["starting_xi"],
                    "bench": row["bench"],
                },
            )
        return {
            "match_id": str(match_id),
            "sides": len(rows),
            "confirmed": all(row["confidence"] >= 100 for row in rows) if rows else False,
        }

    def verdict_for(self, *, match_id: str, player_name: str, team_name: str = "") -> LineupVerdict:
        keys = name_keys(player_name)
        if not keys or not match_id:
            return LineupVerdict()

        try:
            sheets = list(FixtureLineup.objects.filter(provider="statpal", match_id=str(match_id)))
        except Exception as exc:
            # A lineup lookup is a safety check on top of pricing; an infrastructure
            # fault must not silently kill every player prop.
            log.info("Lineup lookup failed: %s", str(exc)[:200])
            return LineupVerdict()

        if not sheets:
            return LineupVerdict()

        if team_name:
            team_key = normalize_person(team_name)
            scoped = [sheet for sheet in sheets if sheet.team_name_normalized == team_key]
            sheets = scoped or sheets

        for sheet in sheets:
            for entry in sheet.starting_xi or []:
                if name_keys(entry.get("name", "")) & keys:
                    return LineupVerdict(STARTING, sheet.confirmed, sheet.team_name)
            for entry in sheet.bench or []:
                if name_keys(entry.get("name", "")) & keys:
                    return LineupVerdict(BENCH, sheet.confirmed, sheet.team_name)

        # Present in the fixture's sheets but in neither list for the scoped team.
        confirmed = all(sheet.confirmed for sheet in sheets)
        return LineupVerdict(OMITTED, confirmed, sheets[0].team_name if sheets else "")


lineup_service = LineupService()
