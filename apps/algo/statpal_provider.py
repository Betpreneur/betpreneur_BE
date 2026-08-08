from __future__ import annotations

from datetime import datetime, timezone as py_timezone
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .statpal import StatPalClient


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _name(value):
    if isinstance(value, dict):
        return _first(value.get("name"), value.get("team_name"), value.get("title"))
    return str(value or "")


def _id(value):
    if isinstance(value, dict):
        return _first(value.get("id"), value.get("team_id"), value.get("main_id"))
    return ""


def _date_from_match(match: dict[str, Any], fallback_date):
    raw = _first(match.get("date"), match.get("match_date"), match.get("start_date"))
    if isinstance(raw, str):
        parsed = parse_date(raw)
        if parsed:
            return parsed
        try:
            return datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            pass
    kickoff = _kickoff_from_match(match)
    return kickoff.date() if kickoff else fallback_date


def _kickoff_from_match(match: dict[str, Any]):
    raw = _first(match.get("kickoff_utc"), match.get("datetime"), match.get("start_time"), match.get("time_utc"))
    if isinstance(raw, str):
        parsed = parse_datetime(raw)
        if parsed:
            return parsed if parsed.tzinfo else timezone.make_aware(parsed)
    timestamp = _first(match.get("timestamp"), match.get("updated_ts"), match.get("kickoff_ts"))
    try:
        if timestamp not in ("", None):
            return datetime.fromtimestamp(float(timestamp), tz=py_timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return None


def _kickoff_label(match: dict[str, Any]):
    raw = _first(match.get("time"), match.get("kickoff"), match.get("start_time"))
    return str(raw or "")


def _unwrap_container(root: dict[str, Any]) -> dict[str, Any]:
    """
    Descend through StatPal's single top-level container.

    The daily feed nests everything under a wrapper whose name varies by endpoint
    (`live_matches` for daily matches). Looking for `league` at the root therefore found
    nothing and the sync silently returned zero fixtures.
    """
    if not isinstance(root, dict):
        return {}
    for key in ("live_matches", "daily_matches", "matches_daily", "fixtures", "results"):
        nested = root.get(key)
        if isinstance(nested, dict) and ("league" in nested or "match" in nested):
            return nested
    if len(root) == 1:
        only = next(iter(root.values()))
        if isinstance(only, dict) and ("league" in only or "match" in only):
            return only
    return root


def _looks_like_match(node: dict[str, Any]) -> bool:
    return bool(node.get("date")) and ("home" in node or "away" in node)


def _looks_like_competition(node: dict[str, Any]) -> bool:
    return bool(node.get("id")) and bool(node.get("name") or node.get("league"))


def _iter_matches(node, league=None):
    """
    Yield every match in a StatPal payload, whatever the nesting.

    The daily feed nests as `league[].match[]` while a league's own fixture list nests as
    `tournament.stage[].week[].match[]`. Walking for match-shaped nodes, and carrying the
    nearest enclosing competition as context, handles both without hard-coding either
    path — and survives the next endpoint that nests differently again.
    """
    if isinstance(node, dict):
        if _looks_like_match(node):
            yield node, league
            return
        if _looks_like_competition(node):
            league = {
                "id": node.get("id"),
                "name": node.get("name") or node.get("league"),
                "country": node.get("country") or (league or {}).get("country") or "",
            }
        for value in node.values():
            yield from _iter_matches(value, league)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_matches(item, league)


def _match_items(payload: dict[str, Any]):
    root = _unwrap_container(payload or {})
    country = root.get("country") if isinstance(root, dict) else ""
    items = []
    for match, league in _iter_matches(root):
        item = dict(match or {})
        if league:
            context = dict(league)
            context.setdefault("country", country or "")
            item.setdefault("_league", context)
        items.append(item)
    return items


class StatPalDailyMatchProvider:
    def __init__(self, client: StatPalClient | None = None):
        self.client = client or StatPalClient()

    def fetch_daily(self, target_date) -> dict[str, Any]:
        return self.client.soccer_daily_matches(params={"date": target_date.isoformat()})

    def fixtures_for_date(self, target_date) -> list[dict[str, Any]]:
        return normalize_daily_matches(self.fetch_daily(target_date), target_date=target_date)


def normalize_daily_matches(payload: dict[str, Any], *, target_date) -> list[dict[str, Any]]:
    fixtures = []
    for match in _match_items(payload):
        if not isinstance(match, dict):
            continue
        league = match.get("_league") or match.get("league") or match.get("competition") or {}
        home = match.get("home") or match.get("home_team") or match.get("team_home") or {}
        away = match.get("away") or match.get("away_team") or match.get("team_away") or {}
        home_name = _name(home) or str(match.get("home_name") or "")
        away_name = _name(away) or str(match.get("away_name") or "")
        provider_match_id = str(_first(match.get("id"), match.get("main_id"), match.get("match_id"), match.get("fallback_id_1"))).strip()
        if not provider_match_id or not home_name or not away_name:
            continue
        kickoff_utc = _kickoff_from_match(match)
        match_date = _date_from_match(match, target_date)
        provider_competition_id = str(_first(
            match.get("league_id"),
            match.get("competition_id"),
            league.get("id") if isinstance(league, dict) else "",
        ))
        fixtures.append(
            {
                "fixture": f"{home_name} vs {away_name}",
                "hname": home_name,
                "aname": away_name,
                "hid": _id(home) or match.get("home_id"),
                "aid": _id(away) or match.get("away_id"),
                "league": _name(league) or str(match.get("league_name") or ""),
                "country": str((league.get("country") if isinstance(league, dict) else "") or match.get("country") or ""),
                "round": str(match.get("round") or ""),
                "league_type": str((league.get("type") if isinstance(league, dict) else "") or ""),
                "code": provider_competition_id,
                "kickoff": _kickoff_label(match),
                "kickoff_utc": kickoff_utc.isoformat() if kickoff_utc else "",
                "match_id": f"statpal:{provider_match_id}",
                "provider_match_id": provider_match_id,
                "provider_competition_id": provider_competition_id,
                "source": "statpal",
                "date": match_date,
                "api_payload": match,
            }
        )
    return fixtures
