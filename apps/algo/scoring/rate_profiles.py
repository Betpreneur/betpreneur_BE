"""
Cache team corner/card/shots-on-target rates from `soccer/teams/{id}`.

Fetched on demand rather than nightly: a per-team call across every league would be
enormous, and only the fixtures someone actually reviews need these numbers. Profiles
are cached with a TTL, so a busy fixture costs one call regardless of how many slips
reference it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import StatPalFixtureSnapshot, TeamRateProfile
from .fitting import normalize_team_name

log = logging.getLogger(__name__)

PROFILE_TTL = timedelta(hours=12)
FAILURE_TTL = timedelta(minutes=10)
_fetch_failures: dict[str, object] = {}


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


def _per_game(value, matches):
    value = _num(value)
    matches = _num(matches)
    if value is None:
        return None
    if matches and matches > 0:
        return round(value / matches, 3)
    return value if value <= 20 else None


def parse_team_payload(payload) -> dict:
    """
    Pull per-game corner, card and shots-on-target rates out of a StatPal team response.

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

    win, draw, lost = (
        _split(fulltime, "win")[2], _split(fulltime, "draw")[2], _split(fulltime, "lost")[2]
    )
    matches = int((win or 0) + (draw or 0) + (lost or 0))

    corners_home, corners_away, _ = _split(fulltime, "avg_corners")
    yellow_home, yellow_away, _ = _split(fulltime, "avg_yellowcards")
    red_home, red_away, _ = _split(fulltime, "avg_redcards")
    shots_on_target_home_total, shots_on_target_away_total, _ = _split(fulltime, "shots_on_goal")
    shots_on_target_home = _per_game(shots_on_target_home_total, matches)
    shots_on_target_away = _per_game(shots_on_target_away_total, matches)
    _, _, fouls_total = _split(fulltime, "fouls")

    def bookings(yellow, red):
        if yellow is None and red is None:
            return None
        return (yellow or 0) + (red or 0) * 2

    return {
        "team_id": str(team.get("id") or ""),
        "team_name": str(team.get("name") or ""),
        "league_id": str(current.get("id") or ""),
        "corners_home": corners_home,
        "corners_away": corners_away,
        "cards_home": bookings(yellow_home, red_home),
        "cards_away": bookings(yellow_away, red_away),
        "shots_on_target_home": shots_on_target_home,
        "shots_on_target_away": shots_on_target_away,
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

    def _recent_failure(self, team_id: str) -> bool:
        failed_at = _fetch_failures.get(str(team_id or ""))
        return bool(failed_at and timezone.now() - failed_at < FAILURE_TTL)

    def _remember_failure(self, team_id: str):
        if team_id:
            _fetch_failures[str(team_id)] = timezone.now()

    @staticmethod
    def _missing_new_rate_fields(profile) -> bool:
        if profile is None:
            return False
        return (
            getattr(profile, "shots_on_target_home", None) is None
            and getattr(profile, "shots_on_target_away", None) is None
        )

    @staticmethod
    def _payload_from_snapshot(team_id: str) -> dict:
        if not team_id:
            return {}
        row = (
            StatPalFixtureSnapshot.objects.filter(
                Q(provider_match_id=str(team_id)) | Q(provider_match_id__endswith=f":{team_id}"),
                provider="statpal",
                snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
                status="available",
            )
            .order_by("-fetched_at", "-updated_at")
            .first()
        )
        return row.payload if row else {}

    def _profile_from_payload(self, *, team_id: str, payload: dict) -> TeamRateProfile | None:
        parsed = parse_team_payload(payload)
        if not parsed:
            return None
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
                "shots_on_target_home": parsed["shots_on_target_home"],
                "shots_on_target_away": parsed["shots_on_target_away"],
                "fouls_per_game": parsed["fouls_per_game"],
                "matches": parsed["matches"],
                "payload": payload or {},
            },
        )
        return profile

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

        if self._fresh(profile) and not self._missing_new_rate_fields(profile):
            return profile
        if not team_id:
            # Without an id there is nothing to fetch; a stale profile beats nothing.
            return profile

        snapshot_payload = self._payload_from_snapshot(team_id)
        if snapshot_payload:
            snapshot_profile = self._profile_from_payload(team_id=team_id, payload=snapshot_payload)
            if snapshot_profile:
                return snapshot_profile

        if self._recent_failure(team_id):
            return profile

        try:
            payload = self.client.soccer_endpoint("SOCCER_TEAM", team_id=team_id)
        except Exception as exc:
            self._remember_failure(team_id)
            log.info("Team rate profile fetch failed team_id=%s error=%s", team_id, str(exc)[:200])
            return profile

        return self._profile_from_payload(team_id=team_id, payload=payload) or profile


team_rate_profile_service = TeamRateProfileService()
