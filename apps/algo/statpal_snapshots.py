from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from .market_taxonomy import MarketDescriptor, describe_market
from .models import FixtureCache, ProviderFixtureMap, StatPalFixtureSnapshot
from .services import json_safe, normalize_fixture_text
from .statpal import StatPalClient, StatPalError


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
            StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
            StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
            StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        ),
        "team_total_goals": (
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
        for row in snapshots.order_by("snapshot_type", "-fetched_at", "-updated_at"):
            by_type.setdefault(row.snapshot_type, row)
        return {
            "available": bool(by_type),
            "snapshots": {
                key: {
                    "status": row.status,
                    "summary": row.summary,
                    "fetched_at": row.fetched_at,
                    "expires_at": row.expires_at,
                    "source_endpoint": row.source_endpoint,
                }
                for key, row in by_type.items()
            },
        }

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
        root = (payload or {}).get("injuries_suspensions") or payload or {}
        leagues = _as_list(root.get("league"))
        for league in leagues:
            provider_competition_id = str(league.get("id") or "")
            for match in _as_list(league.get("match")):
                provider_match_id = str(match.get("main_id") or match.get("fallback_id_1") or "")
                provider_fixture = self._provider_fixture(provider_match_id=provider_match_id)
                fixture = self._fixture(provider_fixture=provider_fixture)
                row = self.save_snapshot(
                    snapshot_type=StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
                    payload={"league": league, "match": match},
                    match_id=(fixture.match_id if fixture else ""),
                    provider_match_id=provider_match_id,
                    provider_competition_id=provider_competition_id,
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
        return self.save_snapshot(
            snapshot_type=snapshot_type,
            payload=payload,
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
            source_endpoint=endpoint_name,
        )

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
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_injuries(payload: dict[str, Any]) -> dict[str, Any]:
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
        team = (payload or {}).get("team") or {}
        stats = (payload or {}).get("stats") or (payload or {}).get("statistics") or payload or {}
        return {
            "team_id": team.get("id") or stats.get("team_id") or "",
            "team_name": team.get("name") or stats.get("team_name") or "",
            "normalized_team_name": normalize_fixture_text(team.get("name") or stats.get("team_name") or ""),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

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
        home_xg = StatPalSnapshotService._find_numeric(payload, "home_xg", "xg_home", "home_expected_goals")
        away_xg = StatPalSnapshotService._find_numeric(payload, "away_xg", "xg_away", "away_expected_goals")
        total_xg = StatPalSnapshotService._find_numeric(payload, "expected_goals", "xg_total", "total_xg")
        if total_xg is None and home_xg is not None and away_xg is not None:
            total_xg = round(home_xg + away_xg, 2)
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "expected_goals": total_xg,
            "home_shots": StatPalSnapshotService._find_numeric(payload, "home_shots", "shots_home", "home_total_shots"),
            "away_shots": StatPalSnapshotService._find_numeric(payload, "away_shots", "shots_away", "away_total_shots"),
            "home_corners": StatPalSnapshotService._find_numeric(
                payload,
                "home_corners",
                "corners_home",
                "home_corner_kicks",
                "home_avg_corners",
                "avg_corners_home",
            ),
            "away_corners": StatPalSnapshotService._find_numeric(
                payload,
                "away_corners",
                "corners_away",
                "away_corner_kicks",
                "away_avg_corners",
                "avg_corners_away",
            ),
            "home_yellow_cards": StatPalSnapshotService._find_numeric(
                payload,
                "home_yellow_cards",
                "yellow_cards_home",
                "home_yellowcards",
                "yellowcards_home",
            ),
            "away_yellow_cards": StatPalSnapshotService._find_numeric(
                payload,
                "away_yellow_cards",
                "yellow_cards_away",
                "away_yellowcards",
                "yellowcards_away",
            ),
            "home_red_cards": StatPalSnapshotService._find_numeric(
                payload,
                "home_red_cards",
                "red_cards_home",
                "home_redcards",
                "redcards_home",
            ),
            "away_red_cards": StatPalSnapshotService._find_numeric(
                payload,
                "away_red_cards",
                "red_cards_away",
                "away_redcards",
                "redcards_away",
            ),
            "total_cards": StatPalSnapshotService._find_numeric(payload, "total_cards", "cards_total"),
            "booking_points": StatPalSnapshotService._find_numeric(payload, "booking_points", "total_booking_points"),
            "top_level_keys": sorted((payload or {}).keys()) if isinstance(payload, dict) else [],
        }

    @staticmethod
    def _summarize_odds(payload: dict[str, Any], match_id="", provider_match_id="") -> dict[str, Any]:
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
