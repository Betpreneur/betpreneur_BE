"""
Cache team corner/card rates from `soccer/teams/{id}`.

Fetched on demand rather than nightly: a per-team call across every league would be
enormous, and only the fixtures someone actually reviews need these numbers. Profiles
are cached with a TTL, so a busy fixture costs one call regardless of how many slips
reference it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import TeamRateProfile
from .fitting import normalize_team_name

log = logging.getLogger(__name__)

PROFILE_TTL = timedelta(hours=12)


def _num(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _split(node, key):
    value = (node or {}).get(key) or {}
    if not isinstance(value, dict):
        return None, None, None
    return _num(value.get("home")), _num(value.get("away")), _num(value.get("total"))


def parse_team_payload(payload) -> dict:
    """
    Pull per-game corner and card rates out of a StatPal team response.

    Bookings follow the bookmaker scale: a red counts as two.
    """
    team = (payload or {}).get("team") or {}
    leagues = (team.get("league_stats") or {}).get("league") or []
    if isinstance(leagues, dict):
        leagues = [leagues]
    if not leagues:
        return {}

    current = leagues[0]
    fulltime = current.get("fulltime") or {}

    corners_home, corners_away, _ = _split(fulltime, "avg_corners")
    yellow_home, yellow_away, _ = _split(fulltime, "avg_yellowcards")
    red_home, red_away, _ = _split(fulltime, "avg_redcards")
    _, _, fouls_total = _split(fulltime, "fouls")

    def bookings(yellow, red):
        if yellow is None and red is None:
            return None
        return (yellow or 0) + (red or 0) * 2

    win, draw, lost = (
        _split(fulltime, "win")[2], _split(fulltime, "draw")[2], _split(fulltime, "lost")[2]
    )
    matches = int((win or 0) + (draw or 0) + (lost or 0))

    return {
        "team_id": str(team.get("id") or ""),
        "team_name": str(team.get("name") or ""),
        "league_id": str(current.get("id") or ""),
        "corners_home": corners_home,
        "corners_away": corners_away,
        "cards_home": bookings(yellow_home, red_home),
        "cards_away": bookings(yellow_away, red_away),
        "fouls_per_game": fouls_total,
        "matches": matches,
    }


class TeamRateProfileService:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from ..statpal import StatPalClient

            self._client = StatPalClient()
        return self._client

    def _fresh(self, profile) -> bool:
        return bool(profile and profile.fetched_at and timezone.now() - profile.fetched_at < PROFILE_TTL)

    def profile_for(self, *, team_id="", team_name="") -> TeamRateProfile | None:
        """Cached profile for a team, refreshed from StatPal when stale."""
        team_id = str(team_id or "").strip()
        profile = None
        if team_id:
            profile = TeamRateProfile.objects.filter(provider="statpal", team_id=team_id).first()
        if profile is None and team_name:
            profile = TeamRateProfile.objects.filter(
                provider="statpal", team_name_normalized=normalize_team_name(team_name)
            ).first()

        if self._fresh(profile):
            return profile
        if not team_id:
            # Without an id there is nothing to fetch; a stale profile beats nothing.
            return profile

        try:
            payload = self.client.soccer_endpoint("SOCCER_TEAM", team_id=team_id)
            parsed = parse_team_payload(payload)
        except Exception as exc:
            log.info("Team rate profile fetch failed team_id=%s error=%s", team_id, str(exc)[:200])
            return profile

        if not parsed:
            return profile

        profile, _ = TeamRateProfile.objects.update_or_create(
            provider="statpal",
            team_id=parsed["team_id"] or team_id,
            defaults={
                "team_name": parsed["team_name"],
                "team_name_normalized": normalize_team_name(parsed["team_name"]),
                "league_id": parsed["league_id"],
                "corners_home": parsed["corners_home"],
                "corners_away": parsed["corners_away"],
                "cards_home": parsed["cards_home"],
                "cards_away": parsed["cards_away"],
                "fouls_per_game": parsed["fouls_per_game"],
                "matches": parsed["matches"],
            },
        )
        return profile


team_rate_profile_service = TeamRateProfileService()
