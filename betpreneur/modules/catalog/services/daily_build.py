from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from betpreneur.modules.catalog.models import FixtureCache, StatPalFixtureSnapshot
from betpreneur.modules.catalog.services.provider_client import (
    StatPalClient,
    StatPalConfigurationError,
    StatPalError,
    statpal_client,
)
from betpreneur.modules.catalog.services.snapshots import StatPalSnapshotService
from betpreneur.modules.catalog.services.statpal_normalize import normalize_daily_matches

DEFAULT_BUILD_DAYS = 3


class StatPalBuildScope:
    GLOBAL = "global"
    FIXTURE = "fixture"
    LEAGUE = "league"
    TEAM = "team"
    PLAYER = "player"
    COACH = "coach"
    H2H = "head_to_head"


OPTIONAL_GLOBAL_ENDPOINTS = {
    "SOCCER_IMAGES",
    "SOCCER_LIVE_ODDS",
    "SOCCER_LIVE_ODDS_MARKETS",
    "SOCCER_LIVE_ODDS_MATCH_STATES",
    "SOCCER_LIVE_STORYLINES",
}


@dataclass(frozen=True)
class StatPalEndpointSpec:
    endpoint_name: str
    scope: str
    required_ids: tuple[str, ...] = ()
    refresh_group: str = "baseline"
    cache_kind: str = "snapshot"
    priority: int = 100
    enabled_for_daily_build: bool = True


STATPAL_DAILY_BUILD_ENDPOINTS: tuple[StatPalEndpointSpec, ...] = (
    StatPalEndpointSpec("SOCCER_MATCHES_DAILY", StatPalBuildScope.GLOBAL, refresh_group="fixture_universe", cache_kind="fixture", priority=10),
    StatPalEndpointSpec("SOCCER_LEAGUE_MATCHES", StatPalBuildScope.LEAGUE, ("league_id",), refresh_group="fixture_universe", cache_kind="fixture", priority=20),
    StatPalEndpointSpec("SOCCER_LEAGUE_MATCH_STATS", StatPalBuildScope.LEAGUE, ("league_id",), refresh_group="fixture_detail", cache_kind="fixture_snapshot", priority=30),
    StatPalEndpointSpec("SOCCER_PREMATCH_ODDS", StatPalBuildScope.LEAGUE, ("league_id",), refresh_group="odds", cache_kind="fixture_snapshot", priority=40),
    StatPalEndpointSpec("SOCCER_PREDICTIONS", StatPalBuildScope.FIXTURE, ("match_id",), refresh_group="fixture_detail", cache_kind="fixture_snapshot", priority=50),
    StatPalEndpointSpec("SOCCER_TEAM_LINEUPS", StatPalBuildScope.FIXTURE, ("match_id",), refresh_group="team_news", cache_kind="fixture_snapshot", priority=60),
    StatPalEndpointSpec("SOCCER_INJURIES_SUSPENSIONS", StatPalBuildScope.GLOBAL, refresh_group="team_news", cache_kind="fixture_snapshot", priority=70),
    StatPalEndpointSpec("SOCCER_WEATHER_FORECAST", StatPalBuildScope.FIXTURE, ("match_id",), refresh_group="environment", cache_kind="fixture_snapshot", priority=80),
    StatPalEndpointSpec("SOCCER_HEAD_TO_HEAD", StatPalBuildScope.H2H, ("home_team_id", "away_team_id"), refresh_group="team_context", cache_kind="fixture_snapshot", priority=90),
    StatPalEndpointSpec("SOCCER_TEAM", StatPalBuildScope.TEAM, ("team_id",), refresh_group="team_context", cache_kind="team_snapshot", priority=100),
    StatPalEndpointSpec("SOCCER_LEAGUE_STANDINGS", StatPalBuildScope.LEAGUE, ("league_id",), refresh_group="league_context", cache_kind="league_snapshot", priority=110),
    StatPalEndpointSpec("SOCCER_LEAGUE_STATS", StatPalBuildScope.LEAGUE, ("league_id",), refresh_group="league_context", cache_kind="league_snapshot", priority=120),
    StatPalEndpointSpec("SOCCER_PLAYER", StatPalBuildScope.PLAYER, ("player_id",), refresh_group="player_context", cache_kind="player_snapshot", priority=130, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_COACH", StatPalBuildScope.COACH, ("coach_id",), refresh_group="staff_context", cache_kind="coach_snapshot", priority=140, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_IMAGES", StatPalBuildScope.GLOBAL, refresh_group="media", cache_kind="asset_snapshot", priority=150, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_LIVE_ODDS", StatPalBuildScope.GLOBAL, refresh_group="live", cache_kind="odds_snapshot", priority=160, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_LIVE_ODDS_MARKETS", StatPalBuildScope.GLOBAL, refresh_group="live", cache_kind="odds_snapshot", priority=170, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_LIVE_ODDS_MATCH_STATES", StatPalBuildScope.GLOBAL, refresh_group="live", cache_kind="odds_snapshot", priority=180, enabled_for_daily_build=False),
    StatPalEndpointSpec("SOCCER_LIVE_STORYLINES", StatPalBuildScope.GLOBAL, refresh_group="live", cache_kind="live_snapshot", priority=190, enabled_for_daily_build=False),
)


STATPAL_ENDPOINT_ALIASES = {
    "SOCCER_LINEUPS": "SOCCER_TEAM_LINEUPS",
    "SOCCER_DETAILED_STATS": "SOCCER_LEAGUE_MATCH_STATS",
    "SOCCER_TEAM_STATS": "SOCCER_TEAM",
    "SOCCER_PLAYER_STATS": "SOCCER_PLAYER",
}


DAILY_REQUIRED_SNAPSHOT_TYPES: tuple[str, ...] = (
    StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
    StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
    StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
    StatPalFixtureSnapshot.SnapshotType.LINEUPS,
    StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
    StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
    StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD,
    StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS,
    StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS,
    StatPalFixtureSnapshot.SnapshotType.WEATHER_FORECAST,
)


DAILY_OPTIONAL_SNAPSHOT_TYPES: tuple[str, ...] = (
    StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS,
    StatPalFixtureSnapshot.SnapshotType.COACH,
)


SNAPSHOT_USABLE_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS: (
        "expected_goals",
        "home_xg",
        "away_xg",
        "home_shots",
        "away_shots",
        "home_shots_on_target",
        "away_shots_on_target",
        "home_corners",
        "away_corners",
        "total_cards",
        "booking_points",
    ),
    StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS: (
        "market_count",
        "bookmaker_count",
        "home_odds",
        "draw_odds",
        "away_odds",
        "over25_odds",
        "under25_odds",
    ),
    StatPalFixtureSnapshot.SnapshotType.PREDICTIONS: (
        "expected_goals",
        "home_xg",
        "away_xg",
        "home_win_percent",
        "draw_percent",
        "away_win_percent",
        "over25_percent",
        "btts_percent",
    ),
    StatPalFixtureSnapshot.SnapshotType.LINEUPS: (
        "starting_count",
        "bench_count",
        "home_confidence",
        "away_confidence",
        "home_formation",
        "away_formation",
    ),
    StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS: (
        "total_to_miss_count",
        "total_questionable_count",
        "home",
        "away",
    ),
    StatPalFixtureSnapshot.SnapshotType.TEAM_STATS: (
        "team_count",
        "home",
        "away",
        "avg_goals_for",
        "avg_goals_against",
        "avg_corners",
        "avg_yellowcards",
    ),
    StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD: (
        "games",
        "recent_meetings_count",
        "home_wins",
        "away_wins",
        "draws",
        "avg_total_goals",
    ),
    StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS: (
        "row_count",
        "team_count",
        "standings",
    ),
    StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS: (
        "row_count",
        "team_count",
        "player_count",
        "players",
    ),
    StatPalFixtureSnapshot.SnapshotType.WEATHER_FORECAST: (
        "temperature",
        "condition",
        "wind_speed",
        "humidity",
        "forecast",
    ),
    StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS: (
        "player_id",
        "player_name",
        "team_id",
        "appearances",
        "goals",
        "assists",
        "rating",
    ),
    StatPalFixtureSnapshot.SnapshotType.COACH: (
        "coach_id",
        "coach_name",
        "team_id",
        "career",
    ),
}


def daily_build_window(start: date, *, days: int = DEFAULT_BUILD_DAYS) -> list[date]:
    days = max(1, int(days or DEFAULT_BUILD_DAYS))
    return [start + timedelta(days=offset) for offset in range(days)]


def canonical_endpoint_name(endpoint_name: str) -> str:
    name = str(endpoint_name or "").strip().upper()
    return STATPAL_ENDPOINT_ALIASES.get(name, name)


def daily_coverage_snapshot_types(*, include_optional: bool = False) -> tuple[str, ...]:
    if include_optional:
        return DAILY_REQUIRED_SNAPSHOT_TYPES + DAILY_OPTIONAL_SNAPSHOT_TYPES
    return DAILY_REQUIRED_SNAPSHOT_TYPES


def statpal_snapshot_usable_fields(snapshot_type: str, summary: dict[str, Any] | None) -> list[str]:
    summary = summary or {}
    usable = []
    for key in SNAPSHOT_USABLE_FIELD_KEYS.get(snapshot_type, ()):
        value = summary.get(key)
        if _has_usable_value(value):
            usable.append(key)

    if snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS:
        for side in ("home", "away"):
            side_summary = summary.get(side) if isinstance(summary.get(side), dict) else {}
            for key in ("avg_goals_for", "avg_goals_against", "avg_corners", "avg_yellowcards"):
                if _has_usable_value(side_summary.get(key)):
                    usable.append(f"{side}.{key}")
    return list(dict.fromkeys(usable))


def build_fixture_coverage_item(
    fixture: dict[str, Any],
    context: dict[str, Any] | None,
    *,
    include_optional: bool = False,
    league_snapshots: dict[str, dict[str, Any]] | None = None,
    player_snapshots: dict[str, dict[str, Any]] | None = None,
    coach_snapshots: dict[str, dict[str, Any]] | None = None,
    endpoint_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    identity = statpal_fixture_identity(fixture)
    snapshots = dict((context or {}).get("snapshots") or {})
    snapshots.update(league_snapshots or {})
    now = timezone.now()
    expected_types = daily_coverage_snapshot_types(include_optional=include_optional)

    snapshot_report = {}
    usable_field_count = 0
    stale_types = []
    missing_types = []
    for snapshot_type in expected_types:
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS:
            entity_report = _entity_snapshot_group(player_snapshots or {}, now=now)
            snapshot_report[snapshot_type] = entity_report
            if not entity_report["present"]:
                missing_types.append(snapshot_type)
            usable_field_count += entity_report["usable_field_count"]
            continue
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.COACH:
            entity_report = _entity_snapshot_group(coach_snapshots or {}, now=now)
            snapshot_report[snapshot_type] = entity_report
            if not entity_report["present"]:
                missing_types.append(snapshot_type)
            usable_field_count += entity_report["usable_field_count"]
            continue

        item = snapshots.get(snapshot_type) or {}
        present = bool(item)
        stale = _snapshot_context_is_stale(item, now=now) if present else False
        usable_fields = statpal_snapshot_usable_fields(snapshot_type, item.get("summary") if isinstance(item, dict) else {})
        if not present:
            missing_types.append(snapshot_type)
        if stale:
            stale_types.append(snapshot_type)
        usable_field_count += len(usable_fields)
        snapshot_report[snapshot_type] = {
            "present": present,
            "stale": stale,
            "source_endpoint": item.get("source_endpoint", "") if isinstance(item, dict) else "",
            "usable_fields": usable_fields,
            "usable_field_count": len(usable_fields),
            "payload_available": bool(item.get("payload_available")) if isinstance(item, dict) else False,
            "fetched_at": _iso_or_blank(item.get("fetched_at")) if isinstance(item, dict) else "",
            "expires_at": _iso_or_blank(item.get("expires_at")) if isinstance(item, dict) else "",
        }

    identity_presence = {
        "provider_match_id": bool(identity["match_id"]),
        "league_id": bool(identity["league_id"]),
        "home_team_id": bool(identity["home_team_id"]),
        "away_team_id": bool(identity["away_team_id"]),
    }
    present_required = sum(1 for snapshot_type in DAILY_REQUIRED_SNAPSHOT_TYPES if snapshot_report.get(snapshot_type, {}).get("present"))
    required_total = len(DAILY_REQUIRED_SNAPSHOT_TYPES)
    coverage_percent = round((present_required / required_total) * 100, 1) if required_total else 100.0
    if not all(identity_presence.values()):
        status = "identity_missing"
    elif missing_types:
        status = "partial"
    elif stale_types:
        status = "stale"
    else:
        status = "complete"

    return {
        "match_id": str(fixture.get("match_id") or ""),
        "fixture": str(fixture.get("fixture") or f"{identity['home_team']} vs {identity['away_team']}").strip(),
        "date": str(fixture.get("date") or ""),
        "identity": {
            **identity,
            "present": identity_presence,
        },
        "status": status,
        "coverage_percent": coverage_percent,
        "required_snapshot_types": list(DAILY_REQUIRED_SNAPSHOT_TYPES),
        "optional_snapshot_types": list(DAILY_OPTIONAL_SNAPSHOT_TYPES) if include_optional else [],
        "snapshots": snapshot_report,
        "missing_snapshot_types": missing_types,
        "stale_snapshot_types": stale_types,
        "usable_field_count": usable_field_count,
        "endpoint_failures": endpoint_failures or [],
    }


def statpal_cache_readiness(
    coverage: dict[str, Any],
    *,
    minimum_average_coverage: float = 70.0,
) -> dict[str, Any]:
    fixtures = int((coverage or {}).get("fixtures") or 0)
    complete = int((coverage or {}).get("complete") or 0)
    partial = int((coverage or {}).get("partial") or 0)
    stale = int((coverage or {}).get("stale") or 0)
    identity_missing = int((coverage or {}).get("identity_missing") or 0)
    average = float((coverage or {}).get("average_coverage_percent") or 0.0)
    reasons = []
    if fixtures <= 0:
        reasons.append("no_statpal_fixtures_cached")
    if identity_missing:
        reasons.append("statpal_identity_missing")
    if average < minimum_average_coverage:
        reasons.append("average_coverage_below_threshold")
    if stale:
        reasons.append("stale_snapshots_present")
    if partial:
        reasons.append("missing_snapshots_present")

    if fixtures <= 0 or identity_missing or average < minimum_average_coverage:
        status = "not_ready"
    elif stale or partial:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "ready": status == "ready",
        "degraded": status == "degraded",
        "minimum_average_coverage": float(minimum_average_coverage),
        "average_coverage_percent": average,
        "fixtures": fixtures,
        "complete": complete,
        "partial": partial,
        "stale": stale,
        "identity_missing": identity_missing,
        "reasons": reasons,
        "summary": _readiness_summary(
            status=status,
            fixtures=fixtures,
            complete=complete,
            partial=partial,
            stale=stale,
            identity_missing=identity_missing,
            average=average,
            threshold=minimum_average_coverage,
        ),
    }


def endpoint_specs_for_daily_build(*, include_optional: bool = False) -> list[StatPalEndpointSpec]:
    specs = [
        spec
        for spec in STATPAL_DAILY_BUILD_ENDPOINTS
        if include_optional or spec.enabled_for_daily_build
    ]
    return sorted(specs, key=lambda item: item.priority)


def statpal_fixture_identity(fixture: dict[str, Any]) -> dict[str, str]:
    provider_match_id = str(
        fixture.get("statpal_provider_match_id")
        or fixture.get("provider_match_id")
        or fixture.get("main_id")
        or fixture.get("match_id")
        or ""
    ).replace("statpal:", "", 1).strip()
    league_id = str(
        fixture.get("statpal_provider_competition_id")
        or fixture.get("provider_competition_id")
        or fixture.get("league_id")
        or fixture.get("code")
        or ""
    ).strip()
    return {
        "match_id": provider_match_id,
        "league_id": league_id,
        "home_team_id": str(fixture.get("statpal_home_team_id") or fixture.get("home_team_id") or fixture.get("hid") or "").strip(),
        "away_team_id": str(fixture.get("statpal_away_team_id") or fixture.get("away_team_id") or fixture.get("aid") or "").strip(),
        "home_team": str(fixture.get("home_team") or fixture.get("hname") or fixture.get("home") or "").strip(),
        "away_team": str(fixture.get("away_team") or fixture.get("aname") or fixture.get("away") or "").strip(),
    }


def build_task_key(endpoint_name: str, ids: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    endpoint = canonical_endpoint_name(endpoint_name)
    spec = next((item for item in STATPAL_DAILY_BUILD_ENDPOINTS if item.endpoint_name == endpoint), None)
    keys = spec.required_ids if spec else tuple(sorted(ids))
    return endpoint, tuple((key, str(ids.get(key) or "")) for key in keys)


def plan_statpal_daily_build(fixtures: list[dict[str, Any]], *, include_optional: bool = False) -> dict[str, Any]:
    specs = endpoint_specs_for_daily_build(include_optional=include_optional)
    tasks: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    identities = [statpal_fixture_identity(fixture) for fixture in fixtures or []]

    for spec in specs:
        if spec.scope == StatPalBuildScope.GLOBAL:
            key = build_task_key(spec.endpoint_name, {})
            tasks.setdefault(key, _task_payload(spec, {}))
            continue

        for fixture, identity in zip(fixtures or [], identities):
            if spec.scope == StatPalBuildScope.FIXTURE:
                ids = {"match_id": identity["match_id"], "league_id": identity["league_id"]}
                _add_task_if_ready(tasks, spec, ids)
            elif spec.scope == StatPalBuildScope.LEAGUE:
                _add_task_if_ready(tasks, spec, {"league_id": identity["league_id"]})
            elif spec.scope == StatPalBuildScope.TEAM:
                for team_id in (identity["home_team_id"], identity["away_team_id"]):
                    _add_task_if_ready(tasks, spec, {"team_id": team_id})
            elif spec.scope == StatPalBuildScope.H2H:
                _add_task_if_ready(
                    tasks,
                    spec,
                    {
                        "home_team_id": identity["home_team_id"],
                        "away_team_id": identity["away_team_id"],
                    },
                )
            elif spec.scope == StatPalBuildScope.PLAYER:
                for player_id in statpal_fixture_player_ids(fixture):
                    _add_task_if_ready(tasks, spec, {"player_id": player_id})
            elif spec.scope == StatPalBuildScope.COACH:
                for coach_id in statpal_fixture_coach_ids(fixture):
                    _add_task_if_ready(tasks, spec, {"coach_id": coach_id})

    task_list = sorted(
        tasks.values(),
        key=lambda item: (item["priority"], item["endpoint_name"], tuple(sorted((item["ids"] or {}).items()))),
    )
    return {
        "fixtures": len(fixtures or []),
        "fixture_identities": identities,
        "tasks": task_list,
        "summary": {
            "tasks": len(task_list),
            "fixture_tasks": sum(1 for item in task_list if item["scope"] == StatPalBuildScope.FIXTURE),
            "league_tasks": sum(1 for item in task_list if item["scope"] == StatPalBuildScope.LEAGUE),
            "team_tasks": sum(1 for item in task_list if item["scope"] == StatPalBuildScope.TEAM),
            "h2h_tasks": sum(1 for item in task_list if item["scope"] == StatPalBuildScope.H2H),
            "global_tasks": sum(1 for item in task_list if item["scope"] == StatPalBuildScope.GLOBAL),
            "missing_identity_tasks": sum(1 for item in task_list if item["status"] == "missing_ids"),
        },
    }


def _add_task_if_ready(tasks: dict, spec: StatPalEndpointSpec, ids: dict[str, str]) -> None:
    key = build_task_key(spec.endpoint_name, ids)
    tasks.setdefault(key, _task_payload(spec, ids))


def _task_payload(spec: StatPalEndpointSpec, ids: dict[str, str]) -> dict[str, Any]:
    missing = [key for key in spec.required_ids if not str(ids.get(key) or "").strip()]
    return {
        "endpoint_name": spec.endpoint_name,
        "scope": spec.scope,
        "refresh_group": spec.refresh_group,
        "cache_kind": spec.cache_kind,
        "priority": spec.priority,
        "ids": {key: str(ids.get(key) or "") for key in spec.required_ids},
        "missing_ids": missing,
        "status": "missing_ids" if missing else "ready",
    }


def statpal_fixture_player_ids(fixture: dict[str, Any]) -> list[str]:
    players = []
    lineups = fixture.get("lineups") or ((fixture.get("api_payload") or {}).get("lineups_normalized") if isinstance(fixture.get("api_payload"), dict) else {})
    if not isinstance(lineups, dict):
        return []
    for side in ("home", "away"):
        bucket = lineups.get(side) if isinstance(lineups.get(side), dict) else {}
        for player in bucket.get("players") or []:
            if isinstance(player, dict) and str(player.get("id") or "").strip():
                players.append(str(player.get("id")).strip())
    return list(dict.fromkeys(players))


def statpal_fixture_coach_ids(fixture: dict[str, Any]) -> list[str]:
    coaches = fixture.get("coaches") or ((fixture.get("api_payload") or {}).get("coaches_normalized") if isinstance(fixture.get("api_payload"), dict) else {})
    if not isinstance(coaches, dict):
        return []
    ids = []
    for side in ("home", "away"):
        coach = coaches.get(side) if isinstance(coaches.get(side), dict) else {}
        coach_id = str(coach.get("id") or "").strip()
        if coach_id:
            ids.append(coach_id)
    return list(dict.fromkeys(ids))


def _has_usable_value(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _snapshot_context_is_stale(item: dict[str, Any], *, now) -> bool:
    expires_at = item.get("expires_at")
    try:
        return bool(expires_at and expires_at <= now)
    except TypeError:
        return False


def _entity_snapshot_group(snapshots: dict[str, dict[str, Any]], *, now) -> dict[str, Any]:
    entity_reports = {}
    stale = False
    usable_field_count = 0
    for entity_id, item in sorted((snapshots or {}).items()):
        snapshot_type = item.get("snapshot_type") or ""
        usable_fields = statpal_snapshot_usable_fields(snapshot_type, item.get("summary") if isinstance(item, dict) else {})
        item_stale = _snapshot_context_is_stale(item, now=now)
        stale = stale or item_stale
        usable_field_count += len(usable_fields)
        entity_reports[entity_id] = {
            "present": True,
            "stale": item_stale,
            "source_endpoint": item.get("source_endpoint", ""),
            "usable_fields": usable_fields,
            "usable_field_count": len(usable_fields),
            "payload_available": bool(item.get("payload_available")),
            "fetched_at": _iso_or_blank(item.get("fetched_at")),
            "expires_at": _iso_or_blank(item.get("expires_at")),
        }
    return {
        "present": bool(entity_reports),
        "stale": stale,
        "entity_count": len(entity_reports),
        "entities": entity_reports,
        "usable_field_count": usable_field_count,
    }


def _iso_or_blank(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _readiness_summary(
    *,
    status: str,
    fixtures: int,
    complete: int,
    partial: int,
    stale: int,
    identity_missing: int,
    average: float,
    threshold: float,
) -> str:
    if fixtures <= 0:
        return "No StatPal fixtures are cached for this window."
    if status == "ready":
        return f"StatPal cache is ready: {complete}/{fixtures} fixtures complete at {average:.1f}% average coverage."
    if status == "degraded":
        return (
            f"StatPal cache is usable but degraded: {complete}/{fixtures} complete, "
            f"{partial} partial, {stale} stale at {average:.1f}% average coverage."
        )
    return (
        f"StatPal cache is not ready: {complete}/{fixtures} complete, {partial} partial, "
        f"{identity_missing} missing identity, average coverage {average:.1f}% below {threshold:.1f}% threshold."
    )


class StatPalDailyBuildService:
    """
    Build the StatPal cache for the 3-day Match Checker horizon.

    Stage 2 intentionally executes the core fixture/league/team/H2H tasks
    sequentially with failure isolation. Queue fan-out and admin dashboards can use
    the same service in later stages.
    """

    SNAPSHOT_TYPES = {
        "SOCCER_LEAGUE_MATCH_STATS": StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
        "SOCCER_PREMATCH_ODDS": StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
        "SOCCER_PREDICTIONS": StatPalFixtureSnapshot.SnapshotType.PREDICTIONS,
        "SOCCER_TEAM_LINEUPS": StatPalFixtureSnapshot.SnapshotType.LINEUPS,
        "SOCCER_INJURIES_SUSPENSIONS": StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS,
        "SOCCER_TEAM": StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
        "SOCCER_HEAD_TO_HEAD": StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD,
        "SOCCER_LEAGUE_STANDINGS": StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS,
        "SOCCER_LEAGUE_STATS": StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS,
        "SOCCER_WEATHER_FORECAST": StatPalFixtureSnapshot.SnapshotType.WEATHER_FORECAST,
        "SOCCER_PLAYER": StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS,
        "SOCCER_COACH": StatPalFixtureSnapshot.SnapshotType.COACH,
        "SOCCER_IMAGES": StatPalFixtureSnapshot.SnapshotType.IMAGES,
        "SOCCER_LIVE_STORYLINES": StatPalFixtureSnapshot.SnapshotType.LIVE_STORYLINES,
        "SOCCER_LIVE_ODDS": StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS,
        "SOCCER_LIVE_ODDS_MARKETS": StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS,
        "SOCCER_LIVE_ODDS_MATCH_STATES": StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS,
    }

    def __init__(self, *, client: StatPalClient | None = None, snapshot_service: StatPalSnapshotService | None = None, fixture_service=None):
        self.client = client or statpal_client()
        self.snapshot_service = snapshot_service or StatPalSnapshotService(client=self.client)
        if fixture_service is None:
            from betpreneur.modules.catalog.services.search import FixtureSearchService

            fixture_service = FixtureSearchService()
        self.fixture_service = fixture_service

    def build(
        self,
        *,
        start_date: date | None = None,
        days: int = DEFAULT_BUILD_DAYS,
        include_optional: bool = False,
        force: bool = False,
        max_tasks: int | None = None,
    ) -> dict[str, Any]:
        start_date = start_date or timezone.localdate()
        window = daily_build_window(start_date, days=days)
        universe = self.fetch_fixture_universe(start_date=start_date, days=days)
        fixtures = universe["fixtures"]
        plan = plan_statpal_daily_build(fixtures, include_optional=include_optional)
        ready_tasks = [task for task in plan["tasks"] if task["status"] == "ready"]
        if max_tasks is not None:
            ready_tasks = ready_tasks[: max(0, int(max_tasks))]

        results = []
        for task in ready_tasks:
            results.append(self.execute_task(task, fixtures=fixtures, force=force))

        coverage = self.coverage_report(
            fixtures,
            include_optional=include_optional,
            execution_results=results,
        )

        return {
            "window": [item.isoformat() for item in window],
            "fixture_universe": {
                "fetched": universe["fetched"],
                "cached": universe["cached"],
                "errors": universe["errors"],
            },
            "plan": plan,
            "execution": {
                "attempted": len(results),
                "succeeded": sum(1 for item in results if item["status"] in {"saved", "saved_many", "skipped"}),
                "failed": sum(1 for item in results if item["status"] == "failed"),
                "skipped": sum(1 for item in results if item["status"] == "skipped"),
                "results": results,
            },
            "coverage": coverage,
            "readiness": statpal_cache_readiness(coverage),
        }

    def readiness_report(
        self,
        *,
        start_date: date | None = None,
        days: int = DEFAULT_BUILD_DAYS,
        include_optional: bool = False,
        minimum_average_coverage: float = 70.0,
    ) -> dict[str, Any]:
        start_date = start_date or timezone.localdate()
        window = daily_build_window(start_date, days=days)
        rows = FixtureCache.objects.filter(match_date__in=window, source="statpal").order_by("match_date", "country", "league", "kickoff", "fixture")
        fixtures = [self._fixture_from_cache(row) for row in rows]
        coverage = self.coverage_report(fixtures, include_optional=include_optional, execution_results=[])
        return {
            "window": [item.isoformat() for item in window],
            "coverage": coverage,
            "readiness": statpal_cache_readiness(
                coverage,
                minimum_average_coverage=minimum_average_coverage,
            ),
        }

    def fetch_fixture_universe(self, *, start_date: date, days: int = DEFAULT_BUILD_DAYS) -> dict[str, Any]:
        window = set(daily_build_window(start_date, days=days))
        errors = []
        fixtures = []
        try:
            payload = self.client.soccer_daily_matches()
            for fixture in normalize_daily_matches(payload, target_date=start_date):
                if fixture.get("date") in window:
                    fixtures.append(fixture)
        except (StatPalConfigurationError, StatPalError) as exc:
            errors.append({"endpoint_name": "SOCCER_MATCHES_DAILY", "error": str(exc)})
        except Exception as exc:
            errors.append({"endpoint_name": "SOCCER_MATCHES_DAILY", "error": str(exc)})

        cached = 0
        for target_date in sorted(window):
            cached += self.fixture_service._upsert_fixtures(
                [fixture for fixture in fixtures if fixture.get("date") == target_date],
                target_date,
            )
        return {"fixtures": fixtures, "fetched": len(fixtures), "cached": cached, "errors": errors}

    def execute_task(self, task: dict[str, Any], *, fixtures: list[dict[str, Any]], force: bool = False) -> dict[str, Any]:
        endpoint_name = canonical_endpoint_name(task.get("endpoint_name") or "")
        ids = task.get("ids") or {}
        try:
            if endpoint_name == "SOCCER_MATCHES_DAILY":
                return {"endpoint_name": endpoint_name, "status": "skipped", "reason": "fixture_universe_already_fetched"}
            if endpoint_name == "SOCCER_INJURIES_SUSPENSIONS":
                return self._save_global_injuries(endpoint_name)
            if endpoint_name in {"SOCCER_LEAGUE_MATCH_STATS", "SOCCER_PREMATCH_ODDS"}:
                return self._save_league_match_rows(endpoint_name, ids, fixtures=fixtures, force=force)
            if endpoint_name in {"SOCCER_LEAGUE_MATCHES", "SOCCER_LEAGUE_STANDINGS", "SOCCER_LEAGUE_STATS"}:
                return self._save_league_snapshot(endpoint_name, ids)
            if endpoint_name in {"SOCCER_PREDICTIONS", "SOCCER_TEAM_LINEUPS", "SOCCER_WEATHER_FORECAST"}:
                return self._save_fixture_snapshot(endpoint_name, ids, fixtures=fixtures, force=force)
            if endpoint_name == "SOCCER_HEAD_TO_HEAD":
                return self._save_h2h_snapshot(endpoint_name, ids, fixtures=fixtures)
            if endpoint_name == "SOCCER_TEAM":
                return self._save_team_snapshot(endpoint_name, ids)
            if endpoint_name == "SOCCER_PLAYER":
                return self._save_entity_snapshot(endpoint_name, ids, id_name="player_id", snapshot_type=StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS)
            if endpoint_name == "SOCCER_COACH":
                return self._save_entity_snapshot(endpoint_name, ids, id_name="coach_id", snapshot_type=StatPalFixtureSnapshot.SnapshotType.COACH)
            if endpoint_name in OPTIONAL_GLOBAL_ENDPOINTS:
                return self._save_global_snapshot(endpoint_name)
            return {"endpoint_name": endpoint_name, "status": "skipped", "reason": "unsupported_endpoint_for_stage_2"}
        except Exception as exc:
            return {"endpoint_name": endpoint_name, "ids": ids, "status": "failed", "error": str(exc)[:300]}

    def coverage_report(
        self,
        fixtures: list[dict[str, Any]],
        *,
        include_optional: bool = False,
        execution_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        items = [
            self.coverage_for_fixture(
                fixture,
                include_optional=include_optional,
                execution_results=execution_results or [],
            )
            for fixture in fixtures or []
        ]
        status_counts: dict[str, int] = {}
        for item in items:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        return {
            "fixtures": len(items),
            "complete": status_counts.get("complete", 0),
            "partial": status_counts.get("partial", 0),
            "stale": status_counts.get("stale", 0),
            "identity_missing": status_counts.get("identity_missing", 0),
            "average_coverage_percent": round(
                sum(item["coverage_percent"] for item in items) / len(items),
                1,
            )
            if items
            else 0.0,
            "items": items,
        }

    def coverage_for_fixture(
        self,
        fixture: dict[str, Any],
        *,
        include_optional: bool = False,
        execution_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        identity = statpal_fixture_identity(fixture)
        match_id = str(fixture.get("match_id") or (f"statpal:{identity['match_id']}" if identity["match_id"] else ""))
        context = self.snapshot_service.fixture_context(
            match_id=match_id,
            provider_match_id=identity["match_id"],
        )
        can_query_snapshots = isinstance(self.snapshot_service, StatPalSnapshotService)
        league_snapshots = self._league_snapshot_context(identity["league_id"]) if can_query_snapshots else {}
        player_snapshots = (
            self._entity_snapshot_context(
                StatPalFixtureSnapshot.SnapshotType.PLAYER_STATS,
                statpal_fixture_player_ids(fixture),
            )
            if can_query_snapshots and include_optional
            else {}
        )
        coach_snapshots = (
            self._entity_snapshot_context(
                StatPalFixtureSnapshot.SnapshotType.COACH,
                statpal_fixture_coach_ids(fixture),
            )
            if can_query_snapshots and include_optional
            else {}
        )
        return build_fixture_coverage_item(
            fixture,
            context,
            include_optional=include_optional,
            league_snapshots=league_snapshots,
            player_snapshots=player_snapshots,
            coach_snapshots=coach_snapshots,
            endpoint_failures=self._endpoint_failures_for_fixture(fixture, execution_results or []),
        )

    def _save_global_injuries(self, endpoint_name: str) -> dict[str, Any]:
        payload = self.client.soccer_endpoint(endpoint_name)
        rows = self.snapshot_service.save_injuries_suspensions_payload(payload)
        return {"endpoint_name": endpoint_name, "status": "saved_many", "rows": len(rows)}

    def _save_league_match_rows(self, endpoint_name: str, ids: dict[str, str], *, fixtures: list[dict[str, Any]], force: bool) -> dict[str, Any]:
        league_id = ids.get("league_id") or ""
        payload = self.client.soccer_endpoint(endpoint_name, league_id=league_id)
        snapshot_type = self.SNAPSHOT_TYPES[endpoint_name]
        saved = []
        for fixture in fixtures:
            identity = statpal_fixture_identity(fixture)
            if identity["league_id"] != league_id:
                continue
            existing = self.snapshot_service.get_snapshot(
                match_id=fixture.get("match_id") or "",
                provider_match_id=identity["match_id"],
                snapshot_type=snapshot_type,
            )
            if existing and not force and not self.snapshot_service._is_expired(existing):
                continue
            row = self.snapshot_service.save_endpoint_payload(
                snapshot_type=snapshot_type,
                endpoint_name=endpoint_name,
                payload=payload,
                match_id=fixture.get("match_id") or "",
                provider_match_id=identity["match_id"],
                provider_competition_id=league_id,
            )
            saved.append(row.id)
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved_many", "rows": len(saved), "snapshot_ids": saved[:25]}

    def _save_fixture_snapshot(self, endpoint_name: str, ids: dict[str, str], *, fixtures: list[dict[str, Any]], force: bool) -> dict[str, Any]:
        provider_match_id = ids.get("match_id") or ""
        fixture = self._fixture_for_provider_match(fixtures, provider_match_id)
        internal_match_id = fixture.get("match_id") or f"statpal:{provider_match_id}"
        snapshot_type = self.SNAPSHOT_TYPES[endpoint_name]
        existing = self.snapshot_service.get_snapshot(
            match_id=internal_match_id,
            provider_match_id=provider_match_id,
            snapshot_type=snapshot_type,
        )
        if existing and not force and not self.snapshot_service._is_expired(existing):
            return {"endpoint_name": endpoint_name, "ids": ids, "status": "skipped", "reason": "fresh_snapshot_exists", "snapshot_id": existing.id}
        payload = self.client.soccer_endpoint(endpoint_name, params={"match_id": provider_match_id})
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=snapshot_type,
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=internal_match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=statpal_fixture_identity(fixture).get("league_id", ""),
        )
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved", "snapshot_id": row.id}

    def _save_league_snapshot(self, endpoint_name: str, ids: dict[str, str]) -> dict[str, Any]:
        league_id = ids.get("league_id") or ""
        payload = self.client.soccer_endpoint(endpoint_name, league_id=league_id)
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=self.SNAPSHOT_TYPES.get(endpoint_name, StatPalFixtureSnapshot.SnapshotType.RAW),
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=f"statpal:{endpoint_name.lower()}:{league_id}",
            provider_competition_id=league_id,
        )
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved", "snapshot_id": row.id}

    def _save_h2h_snapshot(self, endpoint_name: str, ids: dict[str, str], *, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
        home_team_id = ids.get("home_team_id") or ""
        away_team_id = ids.get("away_team_id") or ""
        payload = self.client.soccer_endpoint(endpoint_name, params={"team1_id": home_team_id, "team2_id": away_team_id})
        fixture = self._fixture_for_teams(fixtures, home_team_id, away_team_id)
        identity = statpal_fixture_identity(fixture)
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD,
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=f"statpal:h2h:{home_team_id}:{away_team_id}",
            provider_match_id=identity.get("match_id", ""),
            provider_competition_id=identity.get("league_id", ""),
        )
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved", "snapshot_id": row.id}

    def _save_team_snapshot(self, endpoint_name: str, ids: dict[str, str]) -> dict[str, Any]:
        team_id = ids.get("team_id") or ""
        payload = self.client.soccer_endpoint(endpoint_name, team_id=team_id)
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=f"statpal:team:{team_id}",
            provider_match_id=team_id,
        )
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved", "snapshot_id": row.id}

    def _save_entity_snapshot(self, endpoint_name: str, ids: dict[str, str], *, id_name: str, snapshot_type: str) -> dict[str, Any]:
        entity_id = ids.get(id_name) or ""
        payload = self.client.soccer_endpoint(endpoint_name, **{id_name: entity_id})
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=snapshot_type,
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=f"statpal:{snapshot_type}:{entity_id}",
            provider_match_id=entity_id,
        )
        return {"endpoint_name": endpoint_name, "ids": ids, "status": "saved", "snapshot_id": row.id}

    def _save_global_snapshot(self, endpoint_name: str) -> dict[str, Any]:
        payload = self.client.soccer_endpoint(endpoint_name)
        snapshot_type = self.SNAPSHOT_TYPES.get(endpoint_name, StatPalFixtureSnapshot.SnapshotType.RAW)
        row = self.snapshot_service.save_endpoint_payload(
            snapshot_type=snapshot_type,
            endpoint_name=endpoint_name,
            payload=payload,
            match_id=f"statpal:{endpoint_name.lower()}",
        )
        return {"endpoint_name": endpoint_name, "status": "saved", "snapshot_id": row.id}

    def _league_snapshot_context(self, league_id: str) -> dict[str, dict[str, Any]]:
        if not league_id:
            return {}
        rows = StatPalFixtureSnapshot.objects.filter(
            status="available",
            provider_competition_id=str(league_id),
            snapshot_type__in=[
                StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS,
                StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS,
            ],
        ).order_by("snapshot_type", "-fetched_at", "-updated_at")
        by_type = {}
        for row in rows:
            by_type.setdefault(row.snapshot_type, self.snapshot_service._snapshot_context(row))
        return by_type

    def _entity_snapshot_context(self, snapshot_type: str, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        snapshots = {}
        for entity_id in entity_ids:
            row = self.snapshot_service.get_snapshot(
                match_id=f"statpal:{snapshot_type}:{entity_id}",
                provider_match_id=entity_id,
                snapshot_type=snapshot_type,
            )
            if not row:
                continue
            item = self.snapshot_service._snapshot_context(row)
            item["snapshot_type"] = snapshot_type
            snapshots[entity_id] = item
        return snapshots

    @staticmethod
    def _endpoint_failures_for_fixture(fixture: dict[str, Any], execution_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        identity = statpal_fixture_identity(fixture)
        failures = []
        fixture_ids = {
            "match_id": identity["match_id"],
            "league_id": identity["league_id"],
            "home_team_id": identity["home_team_id"],
            "away_team_id": identity["away_team_id"],
        }
        for result in execution_results or []:
            if result.get("status") != "failed":
                continue
            ids = result.get("ids") or {}
            if not ids or any(str(ids.get(key) or "") == value for key, value in fixture_ids.items() if value):
                failures.append(
                    {
                        "endpoint_name": result.get("endpoint_name", ""),
                        "ids": ids,
                        "error": result.get("error", ""),
                    }
                )
        return failures

    @staticmethod
    def _fixture_from_cache(row: FixtureCache) -> dict[str, Any]:
        payload = row.api_payload if isinstance(row.api_payload, dict) else {}
        return {
            "match_id": row.match_id,
            "provider_match_id": payload.get("provider_match_id") or payload.get("main_id") or payload.get("statpal_provider_match_id") or "",
            "provider_competition_id": payload.get("provider_competition_id") or payload.get("statpal_provider_competition_id") or payload.get("code") or "",
            "home_team_id": payload.get("provider_home_team_id") or payload.get("statpal_home_team_id") or payload.get("hid") or "",
            "away_team_id": payload.get("provider_away_team_id") or payload.get("statpal_away_team_id") or payload.get("aid") or "",
            "home_team": row.home_team,
            "away_team": row.away_team,
            "fixture": row.fixture,
            "date": row.match_date,
            "league": row.league,
            "country": row.country,
            "api_payload": payload,
        }

    @staticmethod
    def _fixture_for_provider_match(fixtures: list[dict[str, Any]], provider_match_id: str) -> dict[str, Any]:
        for fixture in fixtures or []:
            if statpal_fixture_identity(fixture)["match_id"] == str(provider_match_id):
                return fixture
        return {}

    @staticmethod
    def _fixture_for_teams(fixtures: list[dict[str, Any]], home_team_id: str, away_team_id: str) -> dict[str, Any]:
        for fixture in fixtures or []:
            identity = statpal_fixture_identity(fixture)
            if identity["home_team_id"] == str(home_team_id) and identity["away_team_id"] == str(away_team_id):
                return fixture
        return {}
