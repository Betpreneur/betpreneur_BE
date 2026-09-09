from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from betpreneur.modules.catalog.domain.daily_league_registry import daily_tracked_leagues
from betpreneur.modules.catalog.domain.text import normalize_fixture_text
from betpreneur.modules.catalog.models import CoachProfile, TeamCoachAssignment
from betpreneur.modules.catalog.services.provider_client import (
    StatPalClient,
    StatPalConfigurationError,
    StatPalError,
    statpal_client,
)
from betpreneur.modules.catalog.services.resolution import (
    ProviderMappingService,
    provider_mapping_service,
)
from betpreneur.modules.catalog.services.statpal_normalize import (
    normalize_daily_matches,
    normalize_league_standings,
    normalize_team,
)
from betpreneur.platform.db.json import json_safe


class CoachIntelligenceSyncService:
    """Discover tracked teams and maintain their current StatPal coach assignments."""

    def __init__(
        self,
        *,
        client: StatPalClient | None = None,
        mapping_service: ProviderMappingService | None = None,
    ):
        self.client = client or statpal_client()
        self.mapping_service = mapping_service or provider_mapping_service

    def sync(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        max_teams: int | None = None,
        include_coach_details: bool = False,
    ) -> dict[str, Any]:
        selected_keys = set(league_keys or [])
        leagues = [
            league
            for league in daily_tracked_leagues(active_only=True)
            if not selected_keys or league.key in selected_keys
        ]
        results = [
            self.sync_league(
                league,
                max_teams=max_teams,
                include_coach_details=include_coach_details,
            )
            for league in leagues
        ]
        return {
            "provider": "statpal",
            "leagues_considered": len(results),
            "leagues_complete": sum(item["status"] == "complete" for item in results),
            "teams_considered": sum(item.get("teams_considered", 0) for item in results),
            "teams_synced": sum(item.get("teams_synced", 0) for item in results),
            "coaches_created": sum(item.get("coaches_created", 0) for item in results),
            "assignments_created": sum(item.get("assignments_created", 0) for item in results),
            "manager_changes": sum(item.get("manager_changes", 0) for item in results),
            "missing_managers": sum(item.get("missing_managers", 0) for item in results),
            "errors": sum(item.get("error_count", 0) for item in results),
            "results": results,
        }

    def sync_league(self, league, *, max_teams=None, include_coach_details=False) -> dict[str, Any]:
        league_id = str(league.statpal_id or "").strip()
        result = {
            "league_key": league.key,
            "league_name": league.name,
            "provider_league_id": league_id,
            "status": "complete",
            "teams_considered": 0,
            "teams_synced": 0,
            "coaches_created": 0,
            "assignments_created": 0,
            "manager_changes": 0,
            "missing_managers": 0,
            "error_count": 0,
            "errors": [],
            "discovery_source": "standings",
        }
        if not league_id:
            result.update(status="skipped", error_count=1)
            result["errors"].append({"error": "missing_statpal_league_id"})
            return result

        discovery_errors = []
        standings = []
        try:
            payload = self.client.soccer_league_standings(
                league_id,
            )
            standings = self._current_standings(normalize_league_standings(payload))
        except Exception as exc:
            discovery_errors.append({"source": "standings", "error": str(exc)[:300]})

        if not standings:
            result["discovery_source"] = "league_matches"
            try:
                matches_payload = self.client.soccer_league_matches(league_id)
                fixtures = normalize_daily_matches(matches_payload, target_date=timezone.localdate())
                standings = self._teams_from_fixtures(fixtures, league_id=league_id)
            except Exception as exc:
                discovery_errors.append({"source": "league_matches", "error": str(exc)[:300]})

        if not standings:
            result.update(status="failed", error_count=max(1, len(discovery_errors)))
            result["errors"].extend(discovery_errors or [{"error": "no_team_rows"}])
            return result

        if max_teams is not None:
            standings = standings[: max(0, int(max_teams))]
        result["teams_considered"] = len(standings)

        for standing in standings:
            team_id = str(standing.get("team_id") or "").strip()
            team_name = str(standing.get("team_name") or "").strip()
            try:
                try:
                    team_payload = normalize_team(self.client.soccer_team(team_id)) if team_id else {}
                except Exception:
                    team_payload = {
                        "provider_team_id": team_id,
                        "name": team_name,
                        "country": standing.get("country") or league.country,
                        "coach": standing.get("coach") or {},
                    }
                outcome = self.sync_team(
                    league=league,
                    standing=standing,
                    team_payload=team_payload or {},
                    include_coach_details=include_coach_details,
                )
                result["teams_synced"] += 1
                for key in ("coaches_created", "assignments_created", "manager_changes", "missing_managers"):
                    result[key] += int(outcome.get(key, 0))
            except Exception as exc:
                result["status"] = "partial"
                result["error_count"] += 1
                result["errors"].append(
                    {"team_id": team_id, "team_name": team_name, "error": str(exc)[:300]}
                )
        result["errors"] = result["errors"][:25]
        return result

    def sync_team(self, *, league, standing, team_payload, include_coach_details=False) -> dict[str, int]:
        now = timezone.now()
        team_id = str(team_payload.get("provider_team_id") or standing.get("team_id") or "").strip()
        team_name = str(team_payload.get("name") or standing.get("team_name") or "").strip()
        team = self.mapping_service.link_provider_team_identity(
            provider="statpal",
            provider_team_id=team_id,
            provider_team_name=team_name,
            canonical_name=team_name,
            country=league.country or standing.get("country") or team_payload.get("country") or "",
            league_key=league.key,
            league_name=league.name,
            provider_league_id=league.statpal_id,
            season=str(standing.get("season") or ""),
            confidence=95 if team_id else 70,
            resolution_method="coach_intelligence_sync",
            payload={"standing": standing, "team": self._compact_team_payload(team_payload)},
        )

        coach_data = team_payload.get("coach") if isinstance(team_payload.get("coach"), dict) else {}
        if not coach_data:
            coach_data = standing.get("coach") if isinstance(standing.get("coach"), dict) else {}
        coach_id = str(coach_data.get("id") or "").strip()
        coach_name = str(coach_data.get("name") or "").strip()
        if not coach_id and not coach_name:
            return {"missing_managers": 1}

        coach_details = {}
        if include_coach_details and coach_id:
            try:
                coach_details = self.client.soccer_coach(coach_id) or {}
            except (StatPalConfigurationError, StatPalError):
                coach_details = {}

        coach, coach_created = self._upsert_coach(
            coach_id=coach_id,
            coach_name=coach_name,
            provider_payload=coach_details or coach_data,
            now=now,
        )
        assignment_created, manager_changed = self._upsert_assignment(
            team=team,
            coach=coach,
            team_id=team_id,
            team_name=team_name,
            coach_id=coach_id,
            coach_name=coach_name,
            provider_payload={"coach": coach_data, "team": self._compact_team_payload(team_payload)},
            now=now,
        )
        return {
            "coaches_created": int(coach_created),
            "assignments_created": int(assignment_created),
            "manager_changes": int(manager_changed),
        }

    @staticmethod
    def _current_standings(rows):
        current = [row for row in rows if row.get("stage_is_current")]
        candidates = current or rows
        deduplicated = {}
        for row in candidates:
            key = str(row.get("team_id") or normalize_fixture_text(row.get("team_name")) or "").strip()
            if key:
                deduplicated[key] = row
        return list(deduplicated.values())

    @staticmethod
    def _teams_from_fixtures(fixtures, *, league_id):
        current = [fixture for fixture in fixtures if fixture.get("stage_is_current")]
        candidates = current or fixtures
        teams = {}
        for fixture in candidates:
            if str(fixture.get("provider_competition_id") or "") != str(league_id):
                continue
            coaches = fixture.get("coaches") if isinstance(fixture.get("coaches"), dict) else {}
            for side, id_key, name_key in (
                ("home", "hid", "hname"),
                ("away", "aid", "aname"),
            ):
                team_id = str(fixture.get(id_key) or "").strip()
                team_name = str(fixture.get(name_key) or "").strip()
                key = team_id or normalize_fixture_text(team_name)
                if not key:
                    continue
                teams[key] = {
                    "team_id": team_id,
                    "team_name": team_name,
                    "country": fixture.get("country") or "",
                    "season": fixture.get("season") or "",
                    "stage_is_current": fixture.get("stage_is_current", False),
                    "coach": coaches.get(side) if isinstance(coaches.get(side), dict) else {},
                }
        return list(teams.values())

    @staticmethod
    def _compact_team_payload(team_payload):
        return {
            "provider_team_id": team_payload.get("provider_team_id"),
            "name": team_payload.get("name"),
            "country": team_payload.get("country"),
            "coach": team_payload.get("coach") or {},
            "feed_updated": team_payload.get("feed_updated"),
            "feed_updated_ts": team_payload.get("feed_updated_ts"),
        }

    @staticmethod
    def _upsert_coach(*, coach_id, coach_name, provider_payload, now):
        normalized = normalize_fixture_text(coach_name)
        with transaction.atomic():
            coach = None
            if coach_id:
                coach = (
                    CoachProfile.objects.select_for_update()
                    .filter(provider="statpal", provider_coach_id=coach_id)
                    .first()
                )
            if coach is None and normalized:
                candidates = CoachProfile.objects.select_for_update().filter(
                    provider="statpal",
                    canonical_normalized=normalized,
                )
                if coach_id:
                    candidates = candidates.filter(provider_coach_id="")
                coach = candidates.order_by("id").first()
            created = coach is None
            if created:
                coach = CoachProfile(
                    canonical_name=coach_name or f"StatPal coach {coach_id}",
                    canonical_normalized=normalized or f"statpal coach {coach_id}",
                    provider="statpal",
                    first_seen_at=now,
                )

            aliases = list(coach.aliases or [])
            for alias in (coach.provider_name, coach_name):
                if alias and alias not in aliases and alias != coach.canonical_name:
                    aliases.append(alias)
            coach.provider_coach_id = coach_id or coach.provider_coach_id
            coach.provider_name = coach_name or coach.provider_name
            coach.aliases = aliases
            coach.provider_payload = json_safe(provider_payload or coach.provider_payload)
            coach.last_seen_at = now
            coach.active = True
            coach.save()
        return coach, created

    @staticmethod
    def _upsert_assignment(
        *, team, coach, team_id, team_name, coach_id, coach_name, provider_payload, now
    ):
        with transaction.atomic():
            current = (
                TeamCoachAssignment.objects.select_for_update()
                .filter(team=team, currently_active=True)
                .first()
            )
            if current and current.coach_id == coach.id:
                current.last_confirmed_at = now
                current.provider_team_id = team_id
                current.provider_coach_id = coach_id
                current.provider_team_name = team_name
                current.provider_coach_name = coach_name
                current.provider_payload = json_safe(provider_payload)
                current.save(
                    update_fields=[
                        "last_confirmed_at", "provider_team_id", "provider_coach_id",
                        "provider_team_name", "provider_coach_name", "provider_payload", "updated_at",
                    ]
                )
                return False, False

            manager_changed = current is not None
            previous_coach_id = current.coach_id if current else None
            if current:
                current.currently_active = False
                current.ended_on = now.date()
                current.change_reason = "provider_manager_change"
                current.save(update_fields=["currently_active", "ended_on", "change_reason", "updated_at"])

            TeamCoachAssignment.objects.create(
                team=team,
                coach=coach,
                role=TeamCoachAssignment.Role.UNKNOWN,
                started_on=now.date(),
                date_precision=TeamCoachAssignment.DatePrecision.DETECTED,
                currently_active=True,
                change_reason="provider_manager_change" if manager_changed else "initial_import",
                provider="statpal",
                provider_team_id=team_id,
                provider_coach_id=coach_id,
                provider_team_name=team_name,
                provider_coach_name=coach_name,
                first_detected_at=now,
                last_confirmed_at=now,
                provider_payload=json_safe(provider_payload),
            )

        if previous_coach_id and not TeamCoachAssignment.objects.filter(
            coach_id=previous_coach_id,
            currently_active=True,
        ).exists():
            CoachProfile.objects.filter(id=previous_coach_id).update(active=False)
        return True, manager_changed


coach_intelligence_sync_service = CoachIntelligenceSyncService()
