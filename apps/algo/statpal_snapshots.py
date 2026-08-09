from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from .market_taxonomy import MarketDescriptor, describe_market
from .models import FixtureCache, ProviderFixtureMap, StatPalFixtureSnapshot
from .services import json_safe, normalize_fixture_text
from .statpal import StatPalClient, StatPalError
from .statpal_provider import (
    normalize_injuries_suspensions,
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
        if match_id:
            found = qs.filter(match_id=str(match_id)).order_by("-fetched_at", "-updated_at").first()
            if found:
                return found
        if provider_match_id:
            return qs.filter(provider_match_id=str(provider_match_id)).order_by("-fetched_at", "-updated_at").first()
        return None

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
        if match_id:
            snapshots = snapshots.filter(match_id=str(match_id))
        elif provider_match_id:
            snapshots = snapshots.filter(provider_match_id=str(provider_match_id))
        else:
            return {"available": False, "snapshots": {}}

        by_type = {}
        team_rows = []
        for row in snapshots.order_by("snapshot_type", "-fetched_at", "-updated_at"):
            if row.snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS:
                team_rows.append(row)
                continue
            by_type.setdefault(row.snapshot_type, row)
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
    def _summarize_injuries(payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and "total_to_miss_count" in payload:
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
                },
                "away": {
                    "team_id": (payload.get("away") or {}).get("team_id", ""),
                    "team_name": (payload.get("away") or {}).get("team_name", ""),
                    "to_miss_count": (payload.get("away") or {}).get("to_miss_count", 0),
                    "questionable_count": (payload.get("away") or {}).get("questionable_count", 0),
                    "availability_risk": (payload.get("away") or {}).get("availability_risk", "low"),
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
            games_played = StatPalSnapshotService._team_phase_value(fulltime, "win")
            games_played = (games_played or 0) + (StatPalSnapshotService._team_phase_value(fulltime, "draw") or 0) + (StatPalSnapshotService._team_phase_value(fulltime, "lost") or 0)
            goals_for_avg = StatPalSnapshotService._team_goal_average_value(fulltime, "avg_goals_per_game_scored")
            goals_against_avg = StatPalSnapshotService._team_goal_average_value(fulltime, "avg_goals_per_game_conceded")
            avg_total_goals = None
            if goals_for_avg is not None or goals_against_avg is not None:
                avg_total_goals = round((goals_for_avg or 0) + (goals_against_avg or 0), 2)
            return {
                "team_id": payload.get("provider_team_id") or "",
                "team_name": payload.get("name") or "",
                "normalized_team_name": normalize_fixture_text(payload.get("name") or ""),
                "fixture_side": payload.get("fixture_side") or "",
                "squad_count": payload.get("squad_count"),
                "league_count": len(league_stats),
                "sample_size": int(games_played) if games_played is not None else None,
                "current_league": current.get("league") or "",
                "current_season": current.get("season") or "",
                "avg_goals_for": goals_for_avg,
                "avg_goals_against": goals_against_avg,
                "avg_total_goals": avg_total_goals,
                "clean_sheets": StatPalSnapshotService._team_phase_value(fulltime, "clean_sheet"),
                "failed_to_score": StatPalSnapshotService._team_phase_value(fulltime, "failed_to_score"),
                "avg_corners": StatPalSnapshotService._team_phase_value(fulltime, "avg_corners"),
                "avg_yellowcards": StatPalSnapshotService._team_phase_value(fulltime, "avg_yellowcards"),
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
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id or (payload or {}).get("provider_match_id", ""),
            "home_xg": home_xg,
            "away_xg": away_xg,
            "expected_goals": total_xg,
            "home_shots": StatPalSnapshotService._find_numeric(payload, "home_shots", "shots_home", "home_total_shots"),
            "away_shots": StatPalSnapshotService._find_numeric(payload, "away_shots", "shots_away", "away_total_shots"),
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
                "over25_odds": simple.get("over25"),
                "under25_odds": simple.get("under25"),
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
        values = {}
        for market in payload.get("markets") or []:
            market_name = normalize_fixture_text(market.get("name") or "")
            for bookmaker in market.get("bookmakers") or []:
                for odd in bookmaker.get("odds") or []:
                    odd_name = normalize_fixture_text(odd.get("name") or "")
                    if odd_name in {"home", "draw", "away"} and odd_name not in values:
                        values[odd_name] = odd.get("value")
                for total in bookmaker.get("totals") or []:
                    if total.get("line") != 2.5:
                        continue
                    for odd in total.get("odds") or []:
                        odd_name = normalize_fixture_text(odd.get("name") or "")
                        if odd_name == "over":
                            values.setdefault("over25", odd.get("value"))
                        elif odd_name == "under":
                            values.setdefault("under25", odd.get("value"))
            if {"1x2", "1 x 2"} & {market_name} and all(key in values for key in ("home", "draw", "away")):
                continue
        return {
            "home": values.get("home"),
            "draw": values.get("draw"),
            "away": values.get("away"),
            "over25": values.get("over25"),
            "under25": values.get("under25"),
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
