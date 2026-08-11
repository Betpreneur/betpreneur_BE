from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from .market_taxonomy import MarketDescriptor, describe_market
from .models import FixtureCache, ProviderFixtureMap, StatPalFixtureSnapshot
from .services import json_safe, normalize_fixture_text
from .statpal import StatPalClient, StatPalError
from .statpal_provider import (
    normalize_head_to_head,
    normalize_injuries_suspensions,
    normalize_league_standings,
    normalize_league_stats,
    normalize_match_stats,
    normalize_prematch_odds,
    normalize_team,
    normalize_team_lineups,
)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


class StatPalSnapshotService:
    DEFAULT_TTL = timedelta(hours=6)
    CONTEXT_LIST_LIMIT = 50
    CONTEXT_MAX_DEPTH = 8
    FIXTURE_ENDPOINTS = {
        StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS: "SOCCER_PREMATCH_ODDS",
        StatPalFixtureSnapshot.SnapshotType.LINEUPS: "SOCCER_LINEUPS",
        StatPalFixtureSnapshot.SnapshotType.PREDICTIONS: "SOCCER_PREDICTIONS",
        StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS: "SOCCER_DETAILED_STATS",
    }
    MARKET_SNAPSHOT_TYPES = {
        "match_result": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "double_chance": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "draw_no_bet": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "asian_handicap": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "handicap": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "total_goals": (
            StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "team_total_goals": (
            StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "btts": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "clean_sheet": (
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "corners_total": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "team_corners": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "both_halves_total_goals": (
            StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "shots_on_target_total": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "team_shots_on_target": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "cards_total": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "team_cards": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "booking_points": (
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "player_goal": (
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "player_shots": (
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "player_shots_on_target": (
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "player_card": (
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
        "player_assist": (
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ),
    }
    FALLBACK_MARKET_SNAPSHOT_TYPES = (
        StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
        StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        StatPalFixtureSnapshot.SnapshotType.LINEUPS,
    )

    def __init__(self, client: StatPalClient | None = None):
        self.client = client or StatPalClient()

    def snapshot_types_for_market(self, market: str | MarketDescriptor | dict[str, Any]) -> list[str]:
        descriptor = self._market_descriptor(market)
        snapshot_types = self.MARKET_SNAPSHOT_TYPES.get(descriptor.family, self.FALLBACK_MARKET_SNAPSHOT_TYPES)
        return list(dict.fromkeys(snapshot_types))

    def snapshot_plan_for_market(
        self,
        market: str | MarketDescriptor | dict[str, Any],
        *,
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
    ) -> dict[str, Any]:
        descriptor = self._market_descriptor(market)
        snapshot_types = self.snapshot_types_for_market(descriptor)
        existing = []
        fresh = []
        stale = []
        missing = []
        for snapshot_type in snapshot_types:
            snapshot = self.get_snapshot(
                match_id=match_id,
                provider_match_id=provider_match_id,
                snapshot_type=snapshot_type,
            )
            if not snapshot:
                missing.append(snapshot_type)
                continue
            existing.append(snapshot_type)
            if self._is_expired(snapshot):
                stale.append(snapshot_type)
            else:
                fresh.append(snapshot_type)
        requires_league_id = [
            snapshot_type
            for snapshot_type in snapshot_types
            if self._fixture_endpoint_path_params(snapshot_type, provider_competition_id) is None
        ]
        return {
            "market": descriptor.to_dict(),
            "snapshot_types": snapshot_types,
            "existing_snapshot_types": existing,
            "fresh_snapshot_types": fresh,
            "stale_snapshot_types": stale,
            "missing_snapshot_types": missing,
            "requires_provider_competition_id": requires_league_id,
            "coverage_percent": round((len(fresh) / len(snapshot_types)) * 100, 1) if snapshot_types else 0,
        }

    def prepare_fixture_context_for_market(
        self,
        market: str | MarketDescriptor | dict[str, Any],
        *,
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        before = self.snapshot_plan_for_market(
            market,
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
        )
        refreshed = self.refresh_fixture_snapshots(
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
            force=force,
            snapshot_types=before["snapshot_types"],
        )
        context = self.fixture_context(match_id=match_id, provider_match_id=provider_match_id)
        after = self.snapshot_plan_for_market(
            market,
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
        )
        context["market_snapshot_plan"] = after
        context["market_snapshot_coverage"] = {
            "required": after["snapshot_types"],
            "available": sorted((context.get("snapshots") or {}).keys()),
            "fresh": after["fresh_snapshot_types"],
            "missing": after["missing_snapshot_types"],
            "coverage_percent": after["coverage_percent"],
        }
        return {
            "plan": after,
            "plan_before_refresh": before,
            "refreshed": refreshed,
            "context": context,
        }

    @staticmethod
    def _market_descriptor(market: str | MarketDescriptor | dict[str, Any]) -> MarketDescriptor:
        if isinstance(market, MarketDescriptor):
            return market
        if isinstance(market, dict):
            raw = market.get("raw") or market.get("canonical") or market.get("market") or ""
            return describe_market(
                raw,
                market_name=market.get("market_name") or "",
                outcome_name=market.get("outcome_name") or "",
                specifier=market.get("specifier") or "",
            )
        return describe_market(market)

    def save_snapshot(
        self,
        *,
        snapshot_type: str,
        payload: dict[str, Any],
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
        source_endpoint: str = "",
        provider_fixture: ProviderFixtureMap | None = None,
        fixture: FixtureCache | None = None,
        ttl: timedelta | None = None,
    ) -> StatPalFixtureSnapshot:
        provider_fixture = provider_fixture or self._provider_fixture(match_id=match_id, provider_match_id=provider_match_id)
        fixture = fixture or self._fixture(match_id=match_id, provider_fixture=provider_fixture)
        match_id = str(match_id or (fixture.match_id if fixture else "") or (provider_fixture.api_fixture_id if provider_fixture else "") or "")
        provider_match_id = str(provider_match_id or (provider_fixture.provider_event_id if provider_fixture else "") or "")
        provider_competition_id = str(provider_competition_id or (provider_fixture.provider_competition_id if provider_fixture else "") or "")
        fetched_at = timezone.now()
        expires_at = fetched_at + (ttl or self.DEFAULT_TTL)
        summary = self.summarize(snapshot_type=snapshot_type, payload=payload, match_id=match_id, provider_match_id=provider_match_id)
        row, _ = StatPalFixtureSnapshot.objects.update_or_create(
            match_id=match_id,
            provider_match_id=provider_match_id,
            snapshot_type=snapshot_type,
            defaults={
                "provider_fixture": provider_fixture,
                "fixture": fixture,
                "provider_competition_id": provider_competition_id,
                "source_endpoint": source_endpoint,
                "status": "available",
                "payload": json_safe(payload or {}),
                "summary": summary,
                "fetched_at": fetched_at,
                "expires_at": expires_at,
            },
        )
        return row

    def get_snapshot(self, *, match_id="", provider_match_id="", snapshot_type: str) -> StatPalFixtureSnapshot | None:
        qs = StatPalFixtureSnapshot.objects.filter(snapshot_type=snapshot_type, status="available")
        filters = Q()
        if match_id:
            filters |= Q(match_id=str(match_id))
        if provider_match_id:
            filters |= Q(provider_match_id=str(provider_match_id))
        if not filters:
            return None
        candidates = list(qs.filter(filters).order_by("-fetched_at", "-updated_at")[:6])
        if not candidates:
            return None
        fresh = [row for row in candidates if not self._is_expired(row)]
        return fresh[0] if fresh else candidates[0]

    def refresh_fixture_snapshots(
        self,
        *,
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
        force: bool = False,
        snapshot_types: list[str] | None = None,
    ) -> dict[str, Any]:
        match_id = str(match_id or "")
        provider_match_id = str(provider_match_id or "")
        provider_competition_id = str(provider_competition_id or "")
        snapshot_types = snapshot_types or [
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        ]
        result = {
            "attempted": [],
            "refreshed": [],
            "skipped": [],
            "errors": [],
            "api_usage": {
                "provider": "statpal",
                "attempted_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "skipped_by_cache": 0,
                "skipped_without_call": 0,
                "snapshot_types_attempted": [],
                "snapshot_types_refreshed": [],
                "snapshot_types_failed": [],
            },
        }
        for snapshot_type in snapshot_types:
            existing = self.get_snapshot(
                match_id=match_id,
                provider_match_id=provider_match_id,
                snapshot_type=snapshot_type,
            )
            if existing and not force and not self._is_expired(existing):
                result["api_usage"]["skipped_by_cache"] += 1
                result["skipped"].append(
                    {
                        "snapshot_type": snapshot_type,
                        "reason": "fresh_snapshot_exists",
                        "snapshot_id": existing.id,
                    }
                )
                continue
            result["attempted"].append(snapshot_type)
            result["api_usage"]["attempted_calls"] += 1
            result["api_usage"]["snapshot_types_attempted"].append(snapshot_type)
            try:
                if snapshot_type == StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS:
                    refresh = self.refresh_injuries_suspensions()
                    result["refreshed"].append(refresh)
                    result["api_usage"]["successful_calls"] += 1
                    result["api_usage"]["snapshot_types_refreshed"].append(snapshot_type)
                    continue
                endpoint_name = self.FIXTURE_ENDPOINTS.get(snapshot_type)
                if not endpoint_name:
                    result["api_usage"]["attempted_calls"] -= 1
                    result["api_usage"]["skipped_without_call"] += 1
                    result["skipped"].append({"snapshot_type": snapshot_type, "reason": "no_endpoint_mapping"})
                    continue
                target_match_id = provider_match_id or match_id
                if not target_match_id:
                    result["api_usage"]["attempted_calls"] -= 1
                    result["api_usage"]["skipped_without_call"] += 1
                    result["skipped"].append({"snapshot_type": snapshot_type, "reason": "missing_match_id"})
                    continue
                endpoint_params = self._fixture_endpoint_params(snapshot_type, target_match_id)
                endpoint_path_params = self._fixture_endpoint_path_params(snapshot_type, provider_competition_id)
                if endpoint_path_params is None:
                    result["api_usage"]["attempted_calls"] -= 1
                    result["api_usage"]["skipped_without_call"] += 1
                    result["skipped"].append({"snapshot_type": snapshot_type, "reason": "missing_league_id"})
                    continue
                payload = self.client.soccer_endpoint(endpoint_name, params=endpoint_params, **endpoint_path_params)
                row = self.save_endpoint_payload(
                    snapshot_type=snapshot_type,
                    endpoint_name=endpoint_name,
                    payload=payload,
                    match_id=match_id,
                    provider_match_id=provider_match_id,
                    provider_competition_id=provider_competition_id,
                )
                result["refreshed"].append(
                    {
                        "snapshot_type": snapshot_type,
                        "snapshot_id": row.id,
                        "match_id": row.match_id,
                        "provider_match_id": row.provider_match_id,
                    }
                )
                result["api_usage"]["successful_calls"] += 1
                result["api_usage"]["snapshot_types_refreshed"].append(snapshot_type)
            except StatPalError as exc:
                result["errors"].append({"snapshot_type": snapshot_type, "error": str(exc)})
                result["api_usage"]["failed_calls"] += 1
                result["api_usage"]["snapshot_types_failed"].append(snapshot_type)
            except Exception as exc:
                result["errors"].append({"snapshot_type": snapshot_type, "error": str(exc)})
                result["api_usage"]["failed_calls"] += 1
                result["api_usage"]["snapshot_types_failed"].append(snapshot_type)
        for key in ("snapshot_types_attempted", "snapshot_types_refreshed", "snapshot_types_failed"):
            result["api_usage"][key] = list(dict.fromkeys(result["api_usage"][key]))
        return result

    @staticmethod
    def _fixture_endpoint_params(snapshot_type: str, target_match_id: str) -> dict[str, Any]:
        if snapshot_type in {
            StatPalFixtureSnapshot.SnapshotType.LINEUPS,
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
        }:
            return {"match_id": target_match_id}
        return {}

    @staticmethod
    def _fixture_endpoint_path_params(snapshot_type: str, provider_competition_id: str) -> dict[str, Any] | None:
        if snapshot_type in {
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
        }:
            if not provider_competition_id:
                return None
            return {"league_id": provider_competition_id}
        return {}

    def fixture_context(self, *, match_id="", provider_match_id="") -> dict[str, Any]:
        snapshots = StatPalFixtureSnapshot.objects.filter(status="available")
        filters = Q()
        if match_id:
            filters |= Q(match_id=str(match_id))
        if provider_match_id:
            filters |= Q(provider_match_id=str(provider_match_id))
        if not filters:
            return {"available": False, "snapshots": {}}
        snapshots = snapshots.filter(filters)

        by_type = {}
        team_rows = []
        for row in snapshots.order_by("snapshot_type", "-fetched_at", "-updated_at"):
            if row.snapshot_type in by_type and not self._is_expired(by_type[row.snapshot_type]):
                continue
            if row.snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS:
                team_rows.append(row)
                continue
            current = by_type.get(row.snapshot_type)
            if current is None or (self._is_expired(current) and not self._is_expired(row)):
                by_type[row.snapshot_type] = row
        snapshot_context = {key: self._snapshot_context(row) for key, row in by_type.items()}
        if team_rows:
            snapshot_context[StatPalFixtureSnapshot.SnapshotType.TEAM_STATS] = self._team_stats_context(team_rows)
        return {
            "available": bool(snapshot_context),
            "snapshots": snapshot_context,
        }

    def _snapshot_context(self, row: StatPalFixtureSnapshot) -> dict[str, Any]:
        return {
            "status": row.status,
            "summary": row.summary,
            "payload": self._compact_context_payload(row.payload),
            "payload_available": bool(row.payload),
            "fetched_at": row.fetched_at,
            "expires_at": row.expires_at,
            "source_endpoint": row.source_endpoint,
        }

    def _team_stats_context(self, rows: list[StatPalFixtureSnapshot]) -> dict[str, Any]:
        teams = []
        payloads = []
        fetched_at = None
        expires_at = None
        for row in rows:
            side = (row.summary or {}).get("fixture_side") or (row.payload or {}).get("fixture_side") or ""
            summary = {**(row.summary or {}), "fixture_side": side}
            payload = self._compact_context_payload(row.payload)
            teams.append(summary)
            payloads.append(payload)
            fetched_at = max(filter(None, [fetched_at, row.fetched_at]), default=None)
            expires_at = min(filter(None, [expires_at, row.expires_at]), default=None)
        summary = {
            "team_count": len(teams),
            "teams": teams,
            "home": next((team for team in teams if team.get("fixture_side") == "home"), {}),
            "away": next((team for team in teams if team.get("fixture_side") == "away"), {}),
        }
        return {
            "status": "available",
            "summary": summary,
            "payload": {"teams": payloads},
            "payload_available": bool(payloads),
            "fetched_at": fetched_at,
            "expires_at": expires_at,
            "source_endpoint": "SOCCER_TEAM",
        }

    @classmethod
    def _compact_context_payload(cls, value: Any, *, depth: int = 0) -> Any:
        if depth > cls.CONTEXT_MAX_DEPTH:
            return None
        if isinstance(value, dict):
            return {
                key: cls._compact_context_payload(child, depth=depth + 1)
                for key, child in value.items()
                if key != "raw"
            }
        if isinstance(value, list):
            return [cls._compact_context_payload(item, depth=depth + 1) for item in value[: cls.CONTEXT_LIST_LIMIT]]
        return value

    @staticmethod
    def _is_expired(snapshot: StatPalFixtureSnapshot) -> bool:
        return bool(snapshot.expires_at and snapshot.expires_at <= timezone.now())

    def refresh_injuries_suspensions(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.client.soccer_endpoint("SOCCER_INJURIES_SUSPENSIONS", params=params or {})
        rows = self.save_injuries_suspensions_payload(payload)
        return {
            "snapshot_type": StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
            "count": len(rows),
            "match_ids": [row.match_id for row in rows[:25]],
        }

    def save_injuries_suspensions_payload(self, payload: dict[str, Any]) -> list[StatPalFixtureSnapshot]:
        rows = []
        for item in normalize_injuries_suspensions(payload):
            provider_match_id = str(item.get("provider_match_id") or "")
            provider_fixture = self._provider_fixture(match_id=item.get("match_id") or "", provider_match_id=provider_match_id)
            fixture = self._fixture(match_id=item.get("match_id") or "", provider_fixture=provider_fixture)
            row = self.save_snapshot(
                snapshot_type=StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
                payload=item,
                match_id=(fixture.match_id if fixture else item.get("match_id") or ""),
                provider_match_id=provider_match_id,
                provider_competition_id=str(item.get("provider_competition_id") or ""),
                source_endpoint="SOCCER_INJURIES_SUSPENSIONS",
                provider_fixture=provider_fixture,
                fixture=fixture,
            )
            rows.append(row)
        return rows

    def save_team_stats(self, *, team_id: str, payload: dict[str, Any]) -> StatPalFixtureSnapshot:
        return self.save_snapshot(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            payload=payload,
            provider_match_id=str(team_id or ""),
            source_endpoint="SOCCER_TEAM_STATS",
            ttl=timedelta(hours=12),
        )

    def refresh_fixture_team_stats(
        self,
        *,
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
        home_team_id: str = "",
        away_team_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        match_id = str(match_id or (f"statpal:{provider_match_id}" if provider_match_id else "") or "")
        provider_match_id = str(provider_match_id or "").strip()
        provider_competition_id = str(provider_competition_id or "").strip()
        teams = [("home", str(home_team_id or "").strip()), ("away", str(away_team_id or "").strip())]
        result = {
            "snapshot_type": StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            "attempted": [],
            "refreshed": [],
            "skipped": [],
            "errors": [],
            "api_usage": {
                "provider": "statpal",
                "attempted_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "skipped_by_cache": 0,
                "skipped_without_call": 0,
            },
        }
        for side, team_id in teams:
            if not team_id:
                result["api_usage"]["skipped_without_call"] += 1
                result["skipped"].append({"side": side, "reason": "missing_team_id"})
                continue
            team_provider_match_id = self._team_stats_provider_match_id(
                match_id=match_id,
                provider_match_id=provider_match_id,
                side=side,
                team_id=team_id,
            )
            existing = self._fixture_team_stats_snapshot(match_id=match_id, provider_match_id=team_provider_match_id)
            if existing and not force and not self._is_expired(existing):
                result["api_usage"]["skipped_by_cache"] += 1
                result["skipped"].append({"side": side, "team_id": team_id, "reason": "fresh_snapshot_exists", "snapshot_id": existing.id})
                continue
            result["attempted"].append({"side": side, "team_id": team_id})
            result["api_usage"]["attempted_calls"] += 1
            try:
                raw_payload = self.client.soccer_endpoint("SOCCER_TEAM", team_id=team_id)
                payload = normalize_team(raw_payload) or raw_payload or {}
                if isinstance(payload, dict):
                    payload = {
                        **payload,
                        "fixture_side": side,
                        "fixture_provider_match_id": provider_match_id,
                    }
                row = self.save_snapshot(
                    snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
                    payload=payload,
                    match_id=match_id,
                    provider_match_id=team_provider_match_id,
                    provider_competition_id=provider_competition_id,
                    source_endpoint="SOCCER_TEAM",
                    ttl=timedelta(hours=12),
                )
                result["api_usage"]["successful_calls"] += 1
                result["refreshed"].append({"side": side, "team_id": team_id, "snapshot_id": row.id})
            except StatPalError as exc:
                result["api_usage"]["failed_calls"] += 1
                result["errors"].append({"side": side, "team_id": team_id, "error": str(exc)})
            except Exception as exc:
                result["api_usage"]["failed_calls"] += 1
                result["errors"].append({"side": side, "team_id": team_id, "error": str(exc)})
        return result

    @staticmethod
    def _team_stats_provider_match_id(*, match_id: str, provider_match_id: str, side: str, team_id: str) -> str:
        base = str(provider_match_id or match_id or "fixture").replace("statpal:", "", 1)
        return f"{base}:{side}:{team_id}"

    @staticmethod
    def _fixture_team_stats_snapshot(*, match_id: str, provider_match_id: str) -> StatPalFixtureSnapshot | None:
        return (
            StatPalFixtureSnapshot.objects.filter(
                match_id=str(match_id or ""),
                provider_match_id=str(provider_match_id or ""),
                snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
                status="available",
            )
            .order_by("-fetched_at", "-updated_at")
            .first()
        )

    def save_endpoint_payload(
        self,
        *,
        snapshot_type: str,
        endpoint_name: str,
        payload: dict[str, Any],
        match_id: str = "",
        provider_match_id: str = "",
        provider_competition_id: str = "",
    ) -> StatPalFixtureSnapshot:
        payload = self._normalized_endpoint_payload(
            snapshot_type=snapshot_type,
            endpoint_name=endpoint_name,
            payload=payload,
            provider_match_id=provider_match_id,
        )
        return self.save_snapshot(
            snapshot_type=snapshot_type,
            payload=payload,
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
            source_endpoint=endpoint_name,
        )

    def _normalized_endpoint_payload(
        self,
        *,
        snapshot_type: str,
        endpoint_name: str,
        payload: dict[str, Any],
        provider_match_id="",
    ) -> dict[str, Any]:
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS:
            return self._normalized_detailed_stats_payload(payload, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LINEUPS:
            return normalize_team_lineups(payload) or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS:
            rows = normalize_prematch_odds(payload)
            return self._select_match_payload(rows, provider_match_id=provider_match_id) or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS:
            rows = normalize_injuries_suspensions(payload)
            return self._select_match_payload(rows, provider_match_id=provider_match_id) or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS and endpoint_name in {"SOCCER_TEAM", "SOCCER_TEAM_STATS"}:
            return normalize_team(payload) or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD:
            return normalize_head_to_head(payload) or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS:
            rows = normalize_league_standings(payload)
            return self._league_rows_payload(rows, row_key="standings") or payload
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS:
            rows = normalize_league_stats(payload)
            return self._league_rows_payload(rows, row_key="players") or payload
        return payload

    @staticmethod
    def _select_match_payload(rows: list[dict[str, Any]], *, provider_match_id="") -> dict[str, Any]:
        if not rows:
            return {}
        target = str(provider_match_id or "").strip()
        if target:
            for row in rows:
                if target == str(row.get("provider_match_id") or "") or target in (row.get("fallback_match_ids") or []):
                    return row
        return rows[0]

    @staticmethod
    def _league_rows_payload(rows: list[dict[str, Any]], *, row_key: str) -> dict[str, Any]:
        if not rows:
            return {}
        first = rows[0]
        return {
            row_key: rows,
            "provider_competition_id": str(first.get("provider_competition_id") or ""),
            "league": str(first.get("league") or ""),
            "country": str(first.get("country") or ""),
            "source": "statpal",
        }

    @staticmethod
    def _normalized_detailed_stats_payload(payload: dict[str, Any], *, provider_match_id="") -> dict[str, Any]:
        matches = normalize_match_stats(payload)
        if not matches:
            return payload
        target = str(provider_match_id or "").strip()
        selected = None
        if target:
            for match in matches:
                if target == str(match.get("provider_match_id") or "") or target in (match.get("fallback_match_ids") or []):
                    selected = match
                    break
        selected = selected or matches[0]
        return selected

    def summarize(self, *, snapshot_type: str, payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS:
            return self._summarize_injuries(payload)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS:
            return self._summarize_team_stats(payload)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD:
            return self._summarize_head_to_head(payload, match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS:
            return self._summarize_league_rows(payload, row_key="standings", match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS:
            return self._summarize_league_rows(payload, row_key="players", match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.WEATHER_FORECAST:
            return self._summarize_weather(payload, match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.PREDICTIONS:
            return self._summarize_predictions(payload, match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS:
            return self._summarize_detailed_stats(payload, match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS:
            return self._summarize_odds(payload, match_id=match_id, provider_match_id=provider_match_id)
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.LINEUPS:
            return self._summarize_lineups(payload, match_id=match_id, provider_match_id=provider_match_id)
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_head_to_head(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        recent = _as_list((payload or {}).get("recent_meetings"))
        leagues = _as_list((payload or {}).get("leagues"))
        total = (((payload or {}).get("overall_record") or {}).get("total") or {})
        goals = (((payload or {}).get("goals") or {}).get("total") or {})
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "team1_id": str((payload or {}).get("team1_id") or ""),
            "team2_id": str((payload or {}).get("team2_id") or ""),
            "recent_meetings_count": len(recent),
            "league_count": len(leagues),
            "games": total.get("games"),
            "team1_won": total.get("team1_won"),
            "team2_won": total.get("team2_won"),
            "draws": total.get("draws"),
            "team1_scored": goals.get("team1_scored"),
            "team2_scored": goals.get("team2_scored"),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_league_rows(payload: dict[str, Any], *, row_key: str, match_id="", provider_match_id="") -> dict[str, Any]:
        rows = _as_list((payload or {}).get(row_key))
        teams = {str(row.get("team_id") or "") for row in rows if isinstance(row, dict) and row.get("team_id")}
        players = {str(row.get("player_id") or "") for row in rows if isinstance(row, dict) and row.get("player_id")}
        summary = {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "row_key": row_key,
            "row_count": len(rows),
            "team_count": len(teams),
            "player_count": len(players),
            "provider_competition_id": str((payload or {}).get("provider_competition_id") or ""),
            "league": str((payload or {}).get("league") or ""),
            "country": str((payload or {}).get("country") or ""),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }
        if row_key == "players":
            team_summaries = {}
            populated_player_count = 0
            injured_player_count = 0
            stat_fields = (
                "appearances",
                "minutes_played",
                "goals",
                "assists",
                "shots_total",
                "shots_on",
                "yellowcards",
                "redcards",
                "saves",
                "rating",
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                team_id = str(row.get("team_id") or "")
                if not team_id:
                    continue
                item = team_summaries.setdefault(
                    team_id,
                    {
                        "team_id": team_id,
                        "team_name": row.get("team_name") or "",
                        "venue": row.get("venue") or {},
                        "coach": row.get("coach") or {},
                        "squad_count": 0,
                        "injured_count": 0,
                        "populated_player_stat_count": 0,
                    },
                )
                item["squad_count"] += 1
                has_stats = any(row.get(field) not in (None, "") for field in stat_fields)
                if has_stats:
                    item["populated_player_stat_count"] += 1
                    populated_player_count += 1
                if row.get("injured"):
                    item["injured_count"] += 1
                    injured_player_count += 1
            summary["populated_player_stat_count"] = populated_player_count
            summary["injured_player_count"] = injured_player_count
            summary["team_summaries"] = sorted(
                team_summaries.values(),
                key=lambda item: (str(item.get("team_name") or ""), str(item.get("team_id") or "")),
            )
        return summary

    @staticmethod
    def _summarize_weather(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        weather = (payload or {}).get("weather") if isinstance(payload, dict) else {}
        forecast = (payload or {}).get("forecast") if isinstance(payload, dict) else {}
        container = weather if isinstance(weather, dict) else forecast if isinstance(forecast, dict) else payload if isinstance(payload, dict) else {}
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "temperature": StatPalSnapshotService._find_numeric(container, "temperature", "temp", "temp_c"),
            "wind_speed": StatPalSnapshotService._find_numeric(container, "wind_speed", "wind"),
            "humidity": StatPalSnapshotService._find_numeric(container, "humidity"),
            "condition": str(container.get("condition") or container.get("description") or container.get("summary") or ""),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_injuries(payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and "total_to_miss_count" in payload:
            def player_list(side, key):
                return [
                    {
                        "id": str(player.get("id") or ""),
                        "name": str(player.get("name") or ""),
                        "status": str(player.get("status") or ""),
                    }
                    for player in _as_list(((payload.get(side) or {}).get(key) or []))
                    if isinstance(player, dict) and (player.get("id") or player.get("name"))
                ]

            return {
                "league": payload.get("league") or "",
                "provider_match_id": payload.get("provider_match_id") or "",
                "date": str(payload.get("date") or ""),
                "time": payload.get("kickoff") or "",
                "home": {
                    "team_id": (payload.get("home") or {}).get("team_id", ""),
                    "team_name": (payload.get("home") or {}).get("team_name", ""),
                    "to_miss_count": (payload.get("home") or {}).get("to_miss_count", 0),
                    "questionable_count": (payload.get("home") or {}).get("questionable_count", 0),
                    "availability_risk": (payload.get("home") or {}).get("availability_risk", "low"),
                    "to_miss": player_list("home", "to_miss"),
                    "questionable": player_list("home", "questionable"),
                },
                "away": {
                    "team_id": (payload.get("away") or {}).get("team_id", ""),
                    "team_name": (payload.get("away") or {}).get("team_name", ""),
                    "to_miss_count": (payload.get("away") or {}).get("to_miss_count", 0),
                    "questionable_count": (payload.get("away") or {}).get("questionable_count", 0),
                    "availability_risk": (payload.get("away") or {}).get("availability_risk", "low"),
                    "to_miss": player_list("away", "to_miss"),
                    "questionable": player_list("away", "questionable"),
                },
                "total_to_miss_count": payload.get("total_to_miss_count", 0),
                "total_questionable_count": payload.get("total_questionable_count", 0),
            }
        match = (payload or {}).get("match") or payload or {}
        home = match.get("home") or {}
        away = match.get("away") or {}
        return {
            "league": ((payload or {}).get("league") or {}).get("name", ""),
            "provider_match_id": match.get("main_id") or "",
            "date": match.get("date") or "",
            "time": match.get("time") or "",
            "home": StatPalSnapshotService._team_sidelined_summary(home),
            "away": StatPalSnapshotService._team_sidelined_summary(away),
        }

    @staticmethod
    def _team_sidelined_summary(team: dict[str, Any]) -> dict[str, Any]:
        sidelined = team.get("sidelined") or {}
        to_miss = StatPalSnapshotService._players_count(sidelined.get("to_miss"))
        questionable = StatPalSnapshotService._players_count(sidelined.get("questionable"))
        return {
            "team_id": team.get("id") or "",
            "team_name": team.get("name") or "",
            "to_miss_count": to_miss,
            "questionable_count": questionable,
            "availability_risk": "high" if to_miss >= 3 else "medium" if to_miss or questionable >= 2 else "low",
        }

    @staticmethod
    def _players_count(value) -> int:
        if not value:
            return 0
        if isinstance(value, dict):
            value = value.get("player") or value
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return 1
        return 0

    @staticmethod
    def _summarize_team_stats(payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and payload.get("provider_team_id"):
            league_stats = _as_list(payload.get("league_stats"))
            current = StatPalSnapshotService._team_history_row(league_stats)
            fulltime = current.get("fulltime") or {}
            firsthalf = current.get("firsthalf") or {}
            secondhalf = current.get("secondhalf") or {}
            squad = _as_list(payload.get("squad"))
            stat_fields = (
                "appearances",
                "minutes_played",
                "goals",
                "assists",
                "shots_total",
                "shots_on",
                "yellowcards",
                "redcards",
                "saves",
                "rating",
            )
            position_counts = {}
            for player in squad:
                if not isinstance(player, dict):
                    continue
                position = str(player.get("position") or "unknown").strip() or "unknown"
                position_counts[position] = position_counts.get(position, 0) + 1
            games_played = StatPalSnapshotService._team_phase_value(fulltime, "win")
            games_played = (games_played or 0) + (StatPalSnapshotService._team_phase_value(fulltime, "draw") or 0) + (StatPalSnapshotService._team_phase_value(fulltime, "lost") or 0)
            wins = StatPalSnapshotService._team_phase_value(fulltime, "win")
            draws = StatPalSnapshotService._team_phase_value(fulltime, "draw")
            losses = StatPalSnapshotService._team_phase_value(fulltime, "lost")
            goals_for_avg = StatPalSnapshotService._team_goal_average_value(fulltime, "avg_goals_per_game_scored")
            goals_against_avg = StatPalSnapshotService._team_goal_average_value(fulltime, "avg_goals_per_game_conceded")
            avg_total_goals = None
            if goals_for_avg is not None or goals_against_avg is not None:
                avg_total_goals = round((goals_for_avg or 0) + (goals_against_avg or 0), 2)
            shots_on_target_home = StatPalSnapshotService._team_count_per_game(fulltime, "shots_on_goal", "home", games_played)
            shots_on_target_away = StatPalSnapshotService._team_count_per_game(fulltime, "shots_on_goal", "away", games_played)
            shots_total_home = StatPalSnapshotService._team_count_per_game(fulltime, "shots_total", "home", games_played)
            shots_total_away = StatPalSnapshotService._team_count_per_game(fulltime, "shots_total", "away", games_played)
            return {
                "team_id": payload.get("provider_team_id") or "",
                "team_name": payload.get("name") or "",
                "normalized_team_name": normalize_fixture_text(payload.get("name") or ""),
                "fixture_side": payload.get("fixture_side") or "",
                "squad_count": payload.get("squad_count"),
                "injured_player_count": sum(1 for player in squad if isinstance(player, dict) and player.get("injured")),
                "populated_player_stat_count": sum(
                    1
                    for player in squad
                    if isinstance(player, dict) and any(player.get(field) not in (None, "") for field in stat_fields)
                ),
                "position_counts": position_counts,
                "venue": payload.get("venue") or {},
                "coach": payload.get("coach") or {},
                "league_count": len(league_stats),
                "sample_size": int(games_played) if games_played is not None else None,
                "wins": int(wins or 0),
                "draws": int(draws or 0),
                "losses": int(losses or 0),
                "current_league": current.get("league") or "",
                "current_season": current.get("season") or "",
                "avg_goals_for": goals_for_avg,
                "avg_goals_against": goals_against_avg,
                "avg_total_goals": avg_total_goals,
                "clean_sheets": StatPalSnapshotService._team_phase_value(fulltime, "clean_sheet"),
                "failed_to_score": StatPalSnapshotService._team_phase_value(fulltime, "failed_to_score"),
                "avg_corners": StatPalSnapshotService._team_phase_value(fulltime, "avg_corners"),
                "avg_yellowcards": StatPalSnapshotService._team_phase_value(fulltime, "avg_yellowcards"),
                "shots_on_target_home": shots_on_target_home,
                "shots_on_target_away": shots_on_target_away,
                "shots_on_target_total": (
                    round((shots_on_target_home or 0) + (shots_on_target_away or 0), 3)
                    if shots_on_target_home is not None or shots_on_target_away is not None
                    else None
                ),
                "shots_total_home": shots_total_home,
                "shots_total_away": shots_total_away,
                "shots_total": (
                    round((shots_total_home or 0) + (shots_total_away or 0), 3)
                    if shots_total_home is not None or shots_total_away is not None
                    else None
                ),
                "firsthalf_avg_goals_for": StatPalSnapshotService._team_goal_average_value(firsthalf, "avg_goals_per_game_scored"),
                "firsthalf_avg_goals_against": StatPalSnapshotService._team_goal_average_value(firsthalf, "avg_goals_per_game_conceded"),
                "secondhalf_avg_goals_for": StatPalSnapshotService._team_goal_average_value(secondhalf, "avg_goals_per_game_scored"),
                "secondhalf_avg_goals_against": StatPalSnapshotService._team_goal_average_value(secondhalf, "avg_goals_per_game_conceded"),
                "top_level_keys": sorted(payload.keys()),
            }
        team = (payload or {}).get("team") or {}
        stats = (payload or {}).get("stats") or (payload or {}).get("statistics") or payload or {}
        return {
            "team_id": team.get("id") or stats.get("team_id") or "",
            "team_name": team.get("name") or stats.get("team_name") or "",
            "normalized_team_name": normalize_fixture_text(team.get("name") or stats.get("team_name") or ""),
            "fixture_side": stats.get("fixture_side") or "",
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _team_history_row(rows) -> dict[str, Any]:
        for row in rows:
            if isinstance(row, dict) and row.get("league"):
                return row
        for row in rows:
            if isinstance(row, dict):
                return row
        return {}

    @staticmethod
    def _team_phase_value(phase: dict[str, Any], field: str, scope: str = "total"):
        if not isinstance(phase, dict):
            return None
        bucket = phase.get(field)
        if not isinstance(bucket, dict):
            return None
        return StatPalSnapshotService._to_number(bucket.get(scope))

    @staticmethod
    def _team_goal_average_value(phase: dict[str, Any], field: str, scope: str = "total"):
        value = StatPalSnapshotService._team_phase_value(phase, field, scope=scope)
        # StatPal occasionally returns percentage-like values in half-specific goal
        # buckets. A team averaging 25 first-half goals is not a football signal; it is
        # malformed for this model, so do not let it drive recommendations.
        if value is not None and value > 10:
            return None
        return value

    @staticmethod
    def _team_count_per_game(phase: dict[str, Any], field: str, scope: str, games_played):
        value = StatPalSnapshotService._team_phase_value(phase, field, scope=scope)
        games = StatPalSnapshotService._to_number(games_played) or 0
        if value is None:
            return None
        if games > 0:
            return round(value / games, 3)
        return value if value <= 20 else None

    @staticmethod
    def _summarize_predictions(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "home_win_percent": StatPalSnapshotService._find_numeric(payload, "home_win", "home_win_percent", "home_probability", "home_percent", "home"),
            "draw_percent": StatPalSnapshotService._find_numeric(payload, "draw", "draw_probability", "draw_percent"),
            "away_win_percent": StatPalSnapshotService._find_numeric(payload, "away_win", "away_win_percent", "away_probability", "away_percent", "away"),
            "over25_percent": StatPalSnapshotService._find_numeric(payload, "over_2_5", "over25", "over_25"),
            "btts_percent": StatPalSnapshotService._find_numeric(payload, "btts", "both_teams_to_score"),
            "expected_goals": StatPalSnapshotService._find_numeric(payload, "expected_goals", "xg_total", "total_xg"),
            "home_xg": StatPalSnapshotService._find_numeric(payload, "home_xg", "xg_home"),
            "away_xg": StatPalSnapshotService._find_numeric(payload, "away_xg", "xg_away"),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_detailed_stats(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        team_stats = (payload or {}).get("team_stats") if isinstance(payload, dict) else {}
        player_stats = (payload or {}).get("player_stats") if isinstance(payload, dict) else {}
        event_summary = (payload or {}).get("event_summary") if isinstance(payload, dict) else {}
        home_xg = (
            StatPalSnapshotService._team_metric(team_stats, "home", "expected_goals")
            or StatPalSnapshotService._find_numeric(payload, "home_xg", "xg_home", "home_expected_goals")
        )
        away_xg = (
            StatPalSnapshotService._team_metric(team_stats, "away", "expected_goals")
            or StatPalSnapshotService._find_numeric(payload, "away_xg", "xg_away", "away_expected_goals")
        )
        total_xg = StatPalSnapshotService._find_numeric(payload, "expected_goals", "xg_total", "total_xg")
        if total_xg is None and home_xg is not None and away_xg is not None:
            total_xg = round(home_xg + away_xg, 2)
        home_yellows = (
            StatPalSnapshotService._find_numeric(payload, "home_yellow_cards", "yellow_cards_home", "home_yellowcards", "yellowcards_home")
            or StatPalSnapshotService._event_count(event_summary, "yellowcards", "home")
        )
        away_yellows = (
            StatPalSnapshotService._find_numeric(payload, "away_yellow_cards", "yellow_cards_away", "away_yellowcards", "yellowcards_away")
            or StatPalSnapshotService._event_count(event_summary, "yellowcards", "away")
        )
        home_reds = (
            StatPalSnapshotService._find_numeric(payload, "home_red_cards", "red_cards_home", "home_redcards", "redcards_home")
            or StatPalSnapshotService._event_count(event_summary, "redcards", "home")
        )
        away_reds = (
            StatPalSnapshotService._find_numeric(payload, "away_red_cards", "red_cards_away", "away_redcards", "redcards_away")
            or StatPalSnapshotService._event_count(event_summary, "redcards", "away")
        )
        total_cards = StatPalSnapshotService._find_numeric(payload, "total_cards", "cards_total")
        if total_cards is None and any(value is not None for value in (home_yellows, away_yellows, home_reds, away_reds)):
            total_cards = (home_yellows or 0) + (away_yellows or 0) + (home_reds or 0) + (away_reds or 0)
        booking_points = StatPalSnapshotService._find_numeric(payload, "booking_points", "total_booking_points")
        if booking_points is None and any(value is not None for value in (home_yellows, away_yellows, home_reds, away_reds)):
            booking_points = ((home_yellows or 0) + (away_yellows or 0)) * 10 + ((home_reds or 0) + (away_reds or 0)) * 25
        home_player_shots = StatPalSnapshotService._player_stat_total(player_stats, "home", "shots_total", "shots")
        away_player_shots = StatPalSnapshotService._player_stat_total(player_stats, "away", "shots_total", "shots")
        home_player_sot = StatPalSnapshotService._player_stat_total(
            player_stats,
            "home",
            "shots_on",
            "shots_on_target",
            "shots_on_goal",
        )
        away_player_sot = StatPalSnapshotService._player_stat_total(
            player_stats,
            "away",
            "shots_on",
            "shots_on_target",
            "shots_on_goal",
        )
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id or (payload or {}).get("provider_match_id", ""),
            "home_xg": home_xg,
            "away_xg": away_xg,
            "expected_goals": total_xg,
            "home_shots": (
                StatPalSnapshotService._find_numeric(payload, "home_shots", "shots_home", "home_total_shots")
                or home_player_shots
            ),
            "away_shots": (
                StatPalSnapshotService._find_numeric(payload, "away_shots", "shots_away", "away_total_shots")
                or away_player_shots
            ),
            "home_shots_on_target": (
                StatPalSnapshotService._team_metric(team_stats, "home", "shots_on_goal")
                or StatPalSnapshotService._team_metric(team_stats, "home", "shots_on_target")
                or StatPalSnapshotService._find_numeric(
                    payload,
                    "home_shots_on_target",
                    "home_shots_on_goal",
                    "shots_on_target_home",
                    "shots_on_goal_home",
                    "home_sot",
                )
                or home_player_sot
            ),
            "away_shots_on_target": (
                StatPalSnapshotService._team_metric(team_stats, "away", "shots_on_goal")
                or StatPalSnapshotService._team_metric(team_stats, "away", "shots_on_target")
                or StatPalSnapshotService._find_numeric(
                    payload,
                    "away_shots_on_target",
                    "away_shots_on_goal",
                    "shots_on_target_away",
                    "shots_on_goal_away",
                    "away_sot",
                )
                or away_player_sot
            ),
            "home_corners": (
                StatPalSnapshotService._team_metric(team_stats, "home", "corners")
                or StatPalSnapshotService._find_numeric(payload, "home_corners", "corners_home", "home_corner_kicks", "home_avg_corners", "avg_corners_home")
            ),
            "away_corners": (
                StatPalSnapshotService._team_metric(team_stats, "away", "corners")
                or StatPalSnapshotService._find_numeric(payload, "away_corners", "corners_away", "away_corner_kicks", "away_avg_corners", "avg_corners_away")
            ),
            "home_fouls": (
                StatPalSnapshotService._team_metric(team_stats, "home", "fouls")
                or StatPalSnapshotService._find_numeric(payload, "home_fouls", "fouls_home")
            ),
            "away_fouls": (
                StatPalSnapshotService._team_metric(team_stats, "away", "fouls")
                or StatPalSnapshotService._find_numeric(payload, "away_fouls", "fouls_away")
            ),
            "home_yellow_cards": home_yellows,
            "away_yellow_cards": away_yellows,
            "home_red_cards": home_reds,
            "away_red_cards": away_reds,
            "total_cards": total_cards,
            "booking_points": booking_points,
            "goal_events": len((event_summary or {}).get("goals") or []),
            "yellowcard_events": len((event_summary or {}).get("yellowcards") or []),
            "redcard_events": len((event_summary or {}).get("redcards") or []),
            "var_events": len((event_summary or {}).get("var") or []),
            "has_lineups": bool(((payload or {}).get("lineups") or {}).get("home") or ((payload or {}).get("lineups") or {}).get("away")),
            "has_player_stats": bool(((payload or {}).get("player_stats") or {}).get("home") or ((payload or {}).get("player_stats") or {}).get("away")),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _player_stat_total(player_stats, side: str, *keys: str) -> float | None:
        if not isinstance(player_stats, dict):
            return None
        players = player_stats.get(side)
        if not isinstance(players, list):
            return None
        total = 0.0
        found = False
        for player in players:
            if not isinstance(player, dict):
                continue
            stats = player.get("stats") if isinstance(player.get("stats"), dict) else player
            for key in keys:
                value = StatPalSnapshotService._to_number(stats.get(key))
                if value is not None:
                    total += value
                    found = True
                    break
        if not found:
            return None
        return round(total, 3)

    @staticmethod
    def _team_metric(team_stats, side: str, metric: str, field: str = "total"):
        if not isinstance(team_stats, dict):
            return None
        bucket = ((team_stats.get(side) or {}).get(metric) or {})
        if not isinstance(bucket, dict):
            return None
        return StatPalSnapshotService._to_number(bucket.get(field))

    @staticmethod
    def _event_count(event_summary, kind: str, side: str) -> int | None:
        if not isinstance(event_summary, dict):
            return None
        events = event_summary.get(kind)
        if not isinstance(events, list):
            return None
        return sum(1 for item in events if isinstance(item, dict) and item.get("team") == side)

    @staticmethod
    def _summarize_odds(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("markets"), list):
            simple = StatPalSnapshotService._simple_market_odds(payload)
            return {
                "match_id": match_id or payload.get("match_id", ""),
                "provider_match_id": provider_match_id or payload.get("provider_match_id", ""),
                "market_count": payload.get("market_count", len(payload.get("markets") or [])),
                "bookmaker_count": sum(len(market.get("bookmakers") or []) for market in payload.get("markets") or []),
                "home_odds": simple.get("home"),
                "draw_odds": simple.get("draw"),
                "away_odds": simple.get("away"),
                "home_away_home_odds": simple.get("home_away_home"),
                "home_away_away_odds": simple.get("home_away_away"),
                "over15_odds": simple.get("over15"),
                "under15_odds": simple.get("under15"),
                "over25_odds": simple.get("over25"),
                "under25_odds": simple.get("under25"),
                "over35_odds": simple.get("over35"),
                "under35_odds": simple.get("under35"),
                "btts_yes_odds": simple.get("btts_yes"),
                "btts_no_odds": simple.get("btts_no"),
                "double_chance_1x_odds": simple.get("1x"),
                "double_chance_12_odds": simple.get("12"),
                "double_chance_x2_odds": simple.get("x2"),
                "first_half_over05_odds": simple.get("1h_o05"),
                "first_half_under05_odds": simple.get("1h_u05"),
                "first_half_over15_odds": simple.get("1h_o15"),
                "first_half_under15_odds": simple.get("1h_u15"),
                "second_half_over05_odds": simple.get("2h_o05"),
                "second_half_under05_odds": simple.get("2h_u05"),
                "second_half_over15_odds": simple.get("2h_o15"),
                "second_half_under15_odds": simple.get("2h_u15"),
                "top_level_keys": sorted(payload.keys()),
            }
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "home_odds": StatPalSnapshotService._find_numeric(payload, "home_odds", "home_win_odds", "odds_home"),
            "draw_odds": StatPalSnapshotService._find_numeric(payload, "draw_odds", "odds_draw"),
            "away_odds": StatPalSnapshotService._find_numeric(payload, "away_odds", "away_win_odds", "odds_away"),
            "over25_odds": StatPalSnapshotService._find_numeric(payload, "over25_odds", "over_2_5_odds"),
            "under35_odds": StatPalSnapshotService._find_numeric(payload, "under35_odds", "under_3_5_odds"),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _simple_market_odds(payload: dict[str, Any]) -> dict[str, float | None]:
        samples = {}

        def remember(key, value):
            try:
                odd = float(value)
            except (TypeError, ValueError):
                return
            if odd <= 0:
                return
            samples.setdefault(key, []).append(odd)

        def remember_odd_items(prefix_map, odds):
            for odd in odds or []:
                odd_name = normalize_fixture_text(odd.get("name") or "")
                key = prefix_map.get(odd_name)
                if key:
                    remember(key, odd.get("value"))

        def remember_total(prefix, total):
            line = total.get("line")
            if line is None:
                return
            try:
                line_text = f"{float(line):g}"
            except (TypeError, ValueError):
                return
            for odd in total.get("odds") or []:
                odd_name = normalize_fixture_text(odd.get("name") or "")
                if odd_name == "over":
                    remember(f"{prefix}o{line_text.replace('.', '')}", odd.get("value"))
                elif odd_name == "under":
                    remember(f"{prefix}u{line_text.replace('.', '')}", odd.get("value"))

        for market in payload.get("markets") or []:
            market_name = normalize_fixture_text(market.get("name") or "")
            for bookmaker in market.get("bookmakers") or []:
                if market_name in {"1x2", "1 x 2", "match winner", "fulltime result"}:
                    remember_odd_items({"home": "home", "draw": "draw", "away": "away"}, bookmaker.get("odds"))
                elif market_name == "home/away":
                    remember_odd_items({"home": "home_away_home", "away": "home_away_away"}, bookmaker.get("odds"))
                elif market_name in {"both teams to score", "both teams score", "btts"}:
                    remember_odd_items({"yes": "btts_yes", "no": "btts_no"}, bookmaker.get("odds"))
                elif market_name == "double chance":
                    remember_odd_items({
                        "home/draw": "1x",
                        "home draw": "1x",
                        "home/away": "12",
                        "home away": "12",
                        "draw/away": "x2",
                        "draw away": "x2",
                        "1x": "1x",
                        "12": "12",
                        "x2": "x2",
                    }, bookmaker.get("odds"))
                elif market_name in {"over/under", "over under"}:
                    for total in bookmaker.get("totals") or []:
                        remember_total("", total)
                elif market_name in {"over/under 1st half", "over under 1st half"}:
                    for total in bookmaker.get("totals") or []:
                        remember_total("1h_", total)
                elif market_name in {"over/under 2nd half", "over under 2nd half"}:
                    for total in bookmaker.get("totals") or []:
                        remember_total("2h_", total)

        values = {
            key: round(max(items), 3)
            for key, items in samples.items()
            if items
        }
        return {
            "home": values.get("home"),
            "draw": values.get("draw"),
            "away": values.get("away"),
            "home_away_home": values.get("home_away_home"),
            "home_away_away": values.get("home_away_away"),
            "over15": values.get("o15"),
            "under15": values.get("u15"),
            "over25": values.get("o25"),
            "under25": values.get("u25"),
            "over35": values.get("o35"),
            "under35": values.get("u35"),
            "btts_yes": values.get("btts_yes"),
            "btts_no": values.get("btts_no"),
            "1x": values.get("1x"),
            "12": values.get("12"),
            "x2": values.get("x2"),
            "1h_o05": values.get("1h_o05"),
            "1h_u05": values.get("1h_u05"),
            "1h_o15": values.get("1h_o15"),
            "1h_u15": values.get("1h_u15"),
            "2h_o05": values.get("2h_o05"),
            "2h_u05": values.get("2h_u05"),
            "2h_o15": values.get("2h_o15"),
            "2h_u15": values.get("2h_u15"),
        }

    @staticmethod
    def _summarize_lineups(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
        home = (payload or {}).get("home") or {}
        away = (payload or {}).get("away") or {}
        return {
            "match_id": match_id or (payload or {}).get("match_id", ""),
            "provider_match_id": provider_match_id or (payload or {}).get("provider_match_id", ""),
            "status": (payload or {}).get("lineup_status") or (payload or {}).get("status", ""),
            "home_team": home.get("team_name", ""),
            "away_team": away.get("team_name", ""),
            "home_formation": home.get("formation", ""),
            "away_formation": away.get("formation", ""),
            "home_confidence": home.get("confidence"),
            "away_confidence": away.get("confidence"),
            "starting_count": (payload or {}).get("starting_count"),
            "bench_count": (payload or {}).get("bench_count"),
            "sidelined_count": (payload or {}).get("sidelined_count"),
            "home_sidelined_count": home.get("sidelined_count"),
            "away_sidelined_count": away.get("sidelined_count"),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _find_numeric(payload, *keys):
        wanted = {str(key).lower() for key in keys if key}
        if not wanted:
            return None

        def walk(value, parent_key=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = str(key).lower().replace(" ", "_").replace("-", "_")
                    if normalized_key in wanted:
                        as_percentage = any(term in normalized_key for term in ("percent", "probability"))
                        parsed = StatPalSnapshotService._to_number(child, as_percentage=as_percentage)
                        if parsed is not None:
                            return parsed
                    found = walk(child, normalized_key)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child, parent_key)
                    if found is not None:
                        return found
            return None

        return walk(payload)

    @staticmethod
    def _to_number(value, *, as_percentage=False):
        if isinstance(value, dict):
            for key in ("value", "percent", "probability", "odds", "avg", "average"):
                if key in value:
                    return StatPalSnapshotService._to_number(
                        value.get(key),
                        as_percentage=as_percentage or key in {"percent", "probability"},
                    )
            return None
        try:
            if value in (None, ""):
                return None
            text = str(value).strip().replace("%", "")
            number = float(text)
            if as_percentage and 0 < number <= 1:
                return round(number * 100, 2)
            return round(number, 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _provider_fixture(*, match_id="", provider_match_id="") -> ProviderFixtureMap | None:
        if provider_match_id:
            found = ProviderFixtureMap.objects.filter(provider="statpal", provider_event_id=str(provider_match_id), active=True).first()
            if found:
                return found
        if match_id:
            return ProviderFixtureMap.objects.filter(api_fixture_id=str(match_id), active=True).order_by("-verified_at").first()
        return None

    @staticmethod
    def _fixture(*, match_id="", provider_fixture: ProviderFixtureMap | None = None) -> FixtureCache | None:
        if match_id:
            found = FixtureCache.objects.filter(match_id=str(match_id)).first()
            if found:
                return found
        if provider_fixture:
            return FixtureCache.objects.filter(match_id=str(provider_fixture.api_fixture_id)).first()
        return None


statpal_snapshot_service = StatPalSnapshotService()
