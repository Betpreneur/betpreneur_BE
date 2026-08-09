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


def _statpal_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = parse_date(value)
    if parsed:
        return parsed
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _int_or_none(value):
    try:
        if value in ("", None, "?"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _num_or_none(value):
    try:
        if value in ("", None, "?"):
            return None
        number = float(str(value).replace(" ", ""))
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _score_from(team):
    if not isinstance(team, dict):
        return None
    return _int_or_none(_first(team.get("goals"), team.get("score")))


def _unwrap_container(root: dict[str, Any]) -> dict[str, Any]:
    """
    Descend through StatPal's single top-level container.

    The daily feed nests everything under a wrapper whose name varies by endpoint
    (`live_matches` for daily matches). Looking for `league` at the root therefore found
    nothing and the sync silently returned zero fixtures.
    """
    if not isinstance(root, dict):
        return {}
    for key in ("live_matches", "daily_matches", "matches_daily", "matches", "fixtures", "results"):
        nested = root.get(key)
        if isinstance(nested, dict) and ("league" in nested or "match" in nested or "tournament" in nested):
            return nested
    if len(root) == 1:
        only = next(iter(root.values()))
        if isinstance(only, dict) and ("league" in only or "match" in only or "tournament" in only):
            return only
    return root


def _looks_like_match(node: dict[str, Any]) -> bool:
    return bool(node.get("date")) and ("home" in node or "away" in node)


def _looks_like_competition(node: dict[str, Any]) -> bool:
    return bool(node.get("id")) and bool(node.get("name") or node.get("league"))


def _iter_matches(node, league=None, week=None):
    """
    Yield every match in a StatPal payload, whatever the nesting.

    The daily feed nests as `league[].match[]` while a league's own fixture list nests as
    `tournament.stage[].week[].match[]`. Walking for match-shaped nodes, and carrying the
    nearest enclosing competition as context, handles both without hard-coding either
    path — and survives the next endpoint that nests differently again.
    """
    if isinstance(node, dict):
        if _looks_like_match(node):
            yield node, league, week
            return
        if "number" in node and "match" in node:
            week = str(node.get("number") or "")
        if _looks_like_competition(node):
            league = {
                "id": node.get("id"),
                "name": node.get("name") or node.get("league"),
                "country": node.get("country") or (league or {}).get("country") or "",
                "cup": node.get("cup") or (league or {}).get("cup") or "",
                "type": node.get("type") or (league or {}).get("type") or "",
                "season": node.get("season") or (league or {}).get("season") or "",
                "stage_id": node.get("stage_id") or (league or {}).get("stage_id") or "",
                "is_current": node.get("is_current") or (league or {}).get("is_current") or "",
            }
        for value in node.values():
            yield from _iter_matches(value, league, week)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_matches(item, league, week)


def _match_items(payload: dict[str, Any]):
    root = _unwrap_container(payload or {})
    country = root.get("country") if isinstance(root, dict) else ""
    feed = {
        "updated": root.get("updated") if isinstance(root, dict) else "",
        "updated_ts": root.get("updated_ts") if isinstance(root, dict) else None,
    }
    items = []
    for match, league, week in _iter_matches(root):
        item = dict(match or {})
        item.setdefault("_feed", feed)
        if week:
            item.setdefault("_week", week)
        if league:
            context = dict(league)
            if not context.get("country"):
                context["country"] = country or ""
            item.setdefault("_league", context)
        items.append(item)
    return items


def normalize_leagues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues response into a stable league catalogue."""
    root = (payload or {}).get("leagues") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    sport = str(root.get("sport") or "").strip()
    leagues = []
    for item in _as_list(root.get("league")):
        if not isinstance(item, dict):
            continue
        provider_league_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not provider_league_id or not name:
            continue
        raw_start = str(item.get("date_start") or "").strip()
        raw_end = str(item.get("date_end") or "").strip()
        date_start = _statpal_date(raw_start)
        date_end = _statpal_date(raw_end)
        leagues.append(
            {
                "provider": "statpal",
                "sport": sport or "soccer",
                "id": provider_league_id,
                "provider_league_id": provider_league_id,
                "country": str(item.get("country") or "").strip(),
                "name": name,
                "season": str(item.get("season") or "").strip(),
                "date_start": date_start,
                "date_end": date_end,
                "date_start_raw": raw_start,
                "date_end_raw": raw_end,
                "raw": item,
            }
        )
    return leagues


def _season_names(container) -> list[str]:
    if not isinstance(container, dict):
        return []
    names = []
    for item in _as_list(container.get("season")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def normalize_league_seasons(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues/seasons response for historical backfills."""
    root = (payload or {}).get("seasons") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    sport = str(root.get("sport") or "").strip()
    leagues = []
    for item in _as_list(root.get("league")):
        if not isinstance(item, dict):
            continue
        provider_league_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not provider_league_id or not name:
            continue
        match_seasons = _season_names(item.get("matches"))
        standing_seasons = _season_names(item.get("standings"))
        leagues.append(
            {
                "provider": "statpal",
                "sport": sport or "soccer",
                "id": provider_league_id,
                "provider_league_id": provider_league_id,
                "country": str(item.get("country") or "").strip(),
                "name": name,
                "match_seasons": match_seasons,
                "standing_seasons": standing_seasons,
                "has_match_history": bool(match_seasons),
                "has_standings_history": bool(standing_seasons),
                "raw": item,
            }
        )
    return leagues


def _standing_stats(data):
    if not isinstance(data, dict):
        return {
            "games_played": None,
            "wins": None,
            "draws": None,
            "losses": None,
            "goals_scored": None,
            "goals_allowed": None,
        }
    return {
        "games_played": _int_or_none(data.get("games_played")),
        "wins": _int_or_none(data.get("wins")),
        "draws": _int_or_none(data.get("draws")),
        "losses": _int_or_none(data.get("losses")),
        "goals_scored": _int_or_none(data.get("goals_scored")),
        "goals_allowed": _int_or_none(data.get("goals_allowed")),
    }


def normalize_league_standings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues/{league_id}/standings response one row per team."""
    root = (payload or {}).get("standings") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    country = str(root.get("country") or "").strip()
    feed_updated = str(root.get("updated") or "")
    feed_updated_ts = _int_or_none(root.get("updated_ts"))
    rows = []
    for tournament in _as_list(root.get("tournament")):
        if not isinstance(tournament, dict):
            continue
        provider_competition_id = str(tournament.get("id") or "").strip()
        season = str(tournament.get("season") or "").strip()
        stage_id = str(tournament.get("stage_id") or "").strip()
        group_id = str(tournament.get("group_id") or "").strip()
        league_name = str(_first(tournament.get("league"), tournament.get("name"))).strip()
        for team in _as_list(tournament.get("team")):
            if not isinstance(team, dict):
                continue
            team_id = str(team.get("id") or "").strip()
            team_name = str(team.get("name") or "").strip()
            if not team_id and not team_name:
                continue
            total = team.get("total") if isinstance(team.get("total"), dict) else {}
            description = team.get("description") if isinstance(team.get("description"), dict) else {}
            row_id = ":".join(
                [
                    "statpal",
                    provider_competition_id,
                    season,
                    stage_id,
                    group_id,
                    team_id,
                ]
            )
            rows.append(
                {
                    "provider": "statpal",
                    "source": "statpal",
                    "id": row_id,
                    "provider_competition_id": provider_competition_id,
                    "league": league_name,
                    "country": country,
                    "season": season,
                    "stage_id": stage_id,
                    "stage_is_current": _bool(tournament.get("is_current")),
                    "stage_name": str(tournament.get("name") or "").strip(),
                    "stage_date": _statpal_date(str(tournament.get("date") or "")),
                    "group": str(tournament.get("group") or "").strip(),
                    "group_id": group_id,
                    "team_id": team_id,
                    "team_name": team_name,
                    "position": _int_or_none(team.get("position")),
                    "position_raw": str(team.get("position") or "").strip(),
                    "movement_status": str(team.get("status") or "").strip(),
                    "recent_form": str(team.get("recent_form") or "").strip(),
                    "overall": _standing_stats(team.get("overall")),
                    "home": _standing_stats(team.get("home")),
                    "away": _standing_stats(team.get("away")),
                    "goal_difference": _int_or_none(total.get("goal_difference")),
                    "goal_difference_raw": str(total.get("goal_difference") or "").strip(),
                    "points": _int_or_none(total.get("points")),
                    "description": str(description.get("value") or "").strip(),
                    "feed_updated": feed_updated,
                    "feed_updated_ts": feed_updated_ts,
                    "raw": team,
                }
            )
    return rows


PLAYER_STAT_FIELDS = {
    "appearences",
    "appearances",
    "assists",
    "blocks",
    "clearances",
    "crosses_accurate",
    "crosses_total",
    "dispossesed",
    "dribble_attempts",
    "dribble_success",
    "duels_total",
    "duels_won",
    "fouls_committed",
    "fouls_drawn",
    "goals",
    "goals_conceded",
    "inside_box_saves",
    "interceptions",
    "key_passes",
    "lineups",
    "minutes_played",
    "pass_attempts",
    "pass_success",
    "penalties_committed",
    "penalties_missed",
    "penalties_saved",
    "penalties_scored",
    "penalties_won",
    "rating",
    "redcards",
    "saves",
    "shots_on",
    "shots_total",
    "shots_woodwork",
    "substitute_in",
    "substitute_out",
    "substitutes_on_bench",
    "tackles",
    "yellowcards",
    "yellowred",
}


TEAM_PLAYER_STAT_ALIASES = {
    "appearances": ("appearances", "appearences"),
    "penalties_committed": ("penalties_committed", "pen_committed"),
    "penalties_missed": ("penalties_missed", "pen_missed"),
    "penalties_saved": ("penalties_saved", "pen_saved"),
    "penalties_scored": ("penalties_scored", "pen_scored"),
    "penalties_won": ("penalties_won", "pen_won"),
    "shots_on": ("shots_on", "shots_on_target"),
    "lineups": ("lineups", "starting_lineups"),
    "substitutes_on_bench": ("substitutes_on_bench", "on_bench"),
}


def _player_stat_values(player: dict[str, Any]) -> dict[str, Any]:
    stats = {}
    for key in sorted(PLAYER_STAT_FIELDS):
        source_keys = TEAM_PLAYER_STAT_ALIASES.get(key, (key,))
        stats[key] = _num_or_none(_first(*(player.get(source_key) for source_key in source_keys)))
    if stats.get("appearances") is None:
        stats["appearances"] = stats.get("appearences")
    return stats


def normalize_league_stats(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues/{league_id}/stats response one row per player."""
    root = (payload or {}).get("league_stats") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    league = root.get("league") if isinstance(root.get("league"), dict) else {}
    provider_competition_id = str(league.get("id") or "").strip()
    league_name = str(league.get("name") or "").strip()
    country = str(league.get("country") or "").strip()
    feed_updated = str(root.get("updated") or "")
    feed_updated_ts = _int_or_none(root.get("updated_ts"))
    rows = []
    for team in _as_list(league.get("team")):
        if not isinstance(team, dict):
            continue
        team_id = str(team.get("id") or "").strip()
        team_name = str(team.get("name") or "").strip()
        venue = team.get("venue") if isinstance(team.get("venue"), dict) else {}
        coach = team.get("coach") if isinstance(team.get("coach"), dict) else {}
        squad = team.get("squad") if isinstance(team.get("squad"), dict) else {}
        for player in _as_list(squad.get("player")):
            if not isinstance(player, dict):
                continue
            player_id = str(player.get("id") or "").strip()
            player_name = str(player.get("name") or "").strip()
            if not player_id and not player_name:
                continue
            stats = _player_stat_values(player)
            rows.append(
                {
                    "provider": "statpal",
                    "source": "statpal",
                    "id": f"statpal:{provider_competition_id}:{team_id}:{player_id}",
                    "provider_competition_id": provider_competition_id,
                    "league": league_name,
                    "country": country,
                    "team_id": team_id,
                    "team_name": team_name,
                    "venue": {
                        "id": str(venue.get("id") or "").strip(),
                        "name": str(venue.get("name") or "").strip(),
                    },
                    "coach": {
                        "id": str(coach.get("id") or "").strip(),
                        "name": str(coach.get("name") or "").strip(),
                    },
                    "player_id": player_id,
                    "player_name": player_name,
                    "number": str(player.get("number") or "").strip(),
                    "age": _int_or_none(player.get("age")),
                    "position": str(player.get("position") or "").strip(),
                    "injured": _bool(player.get("injured")),
                    "appearances": stats.get("appearances"),
                    "minutes_played": stats.get("minutes_played"),
                    "goals": stats.get("goals"),
                    "assists": stats.get("assists"),
                    "shots_total": stats.get("shots_total"),
                    "shots_on": stats.get("shots_on"),
                    "yellowcards": stats.get("yellowcards"),
                    "redcards": stats.get("redcards"),
                    "saves": stats.get("saves"),
                    "rating": stats.get("rating"),
                    "stats": stats,
                    "feed_updated": feed_updated,
                    "feed_updated_ts": feed_updated_ts,
                    "raw": player,
                }
            )
    return rows


def _league_ids(container):
    if not isinstance(container, dict):
        return []
    ids = container.get("league_id")
    if isinstance(ids, list):
        return [str(item).strip() for item in ids if str(item or "").strip()]
    value = str(ids or "").strip()
    return [value] if value else []


def _transfer_rows(container, direction):
    if not isinstance(container, dict):
        return []
    section = container.get(direction) if isinstance(container.get(direction), dict) else {}
    rows = []
    for item in _as_list(section.get("player")):
        if not isinstance(item, dict):
            continue
        player_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not player_id and not name:
            continue
        rows.append(
            {
                "direction": direction,
                "player_id": player_id,
                "player_name": name,
                "date": _statpal_date(str(item.get("date") or "")),
                "date_raw": str(item.get("date") or "").strip(),
                "age": _int_or_none(item.get("age")),
                "position": str(item.get("position") or "").strip(),
                "from": str(item.get("from") or "").strip(),
                "to": str(item.get("to") or "").strip(),
                "team_id": str(item.get("team_id") or "").strip(),
                "type": str(item.get("type") or "").strip(),
                "price": str(item.get("price") or "").strip(),
                "raw": item,
            }
        )
    return rows


def _trophies(container):
    if not isinstance(container, dict):
        return []
    rows = []
    for item in _as_list(container.get("trophy")):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "country": str(item.get("country") or "").strip(),
                "league": str(item.get("league") or "").strip(),
                "status": str(item.get("status") or "").strip(),
                "count": _int_or_none(item.get("count")),
                "seasons": [
                    season.strip()
                    for season in str(item.get("seasons") or "").split(",")
                    if season.strip()
                ],
                "seasons_raw": str(item.get("seasons") or "").strip(),
                "raw": item,
            }
        )
    return rows


def _split_stat_values(data):
    if not isinstance(data, dict):
        return {"total": None, "home": None, "away": None}
    return {
        "total": _num_or_none(data.get("total")),
        "home": _num_or_none(data.get("home")),
        "away": _num_or_none(data.get("away")),
    }


def _transition_values(data):
    if not isinstance(data, dict):
        return {"ft_win": None, "ft_draw": None, "ft_lost": None}
    return {
        "ft_win": _num_or_none(data.get("ft_win")),
        "ft_draw": _num_or_none(data.get("ft_draw")),
        "ft_lost": _num_or_none(data.get("ft_lost")),
    }


TEAM_LEAGUE_SPLIT_FIELDS = {
    "avg_corners",
    "avg_first_goal_conceded",
    "avg_first_goal_scored",
    "avg_goals_per_game_conceded",
    "avg_goals_per_game_scored",
    "avg_redcards",
    "avg_yellowcards",
    "biggest_defeat",
    "biggest_victory",
    "clean_sheet",
    "corners",
    "draw",
    "failed_to_score",
    "fouls",
    "goals_against",
    "goals_for",
    "lost",
    "offsides",
    "possession",
    "redcards",
    "shots_on_goal",
    "shots_total",
    "win",
    "yellowcards",
}

TEAM_LEAGUE_TRANSITION_FIELDS = {"draw_halftime", "lost_halftime", "win_halftime"}


def _team_league_phase(data):
    if not isinstance(data, dict):
        return {}
    phase = {}
    for key in sorted(TEAM_LEAGUE_SPLIT_FIELDS):
        phase[key] = _split_stat_values(data.get(key))
    for key in sorted(TEAM_LEAGUE_TRANSITION_FIELDS):
        phase[key] = _transition_values(data.get(key))
    return phase


def _minute_periods(container):
    if not isinstance(container, dict):
        return []
    periods = []
    for item in _as_list(container.get("period")):
        if not isinstance(item, dict):
            continue
        pct_raw = str(item.get("pct") or "").strip()
        periods.append(
            {
                "minute_range": str(item.get("min") or "").strip(),
                "pct": _num_or_none(pct_raw.replace("%", "")),
                "pct_raw": pct_raw,
                "count": _int_or_none(item.get("count")),
                "raw": item,
            }
        )
    return periods


def _team_league_stats(container):
    if not isinstance(container, dict):
        return []
    rows = []
    for item in _as_list(container.get("league")):
        if not isinstance(item, dict):
            continue
        league_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not league_id and not name:
            continue
        rows.append(
            {
                "league_id": league_id,
                "league": name,
                "season": str(item.get("season") or "").strip(),
                "fulltime": _team_league_phase(item.get("fulltime")),
                "firsthalf": _team_league_phase(item.get("firsthalf")),
                "secondhalf": _team_league_phase(item.get("secondhalf")),
                "scoring_minutes": _minute_periods(item.get("scoring_minutes")),
                "goals_conceded_minutes": _minute_periods(item.get("goals_conceded_minutes")),
                "yellowcard_minutes": _minute_periods(item.get("yellowcard_minutes")),
                "redcard_minutes": _minute_periods(item.get("redcard_minutes")),
                "raw": item,
            }
        )
    return rows


def normalize_team(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize StatPal's soccer/teams/{team_id} response into a team profile."""
    if not isinstance(payload, dict):
        return {}
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    provider_team_id = str(team.get("id") or "").strip()
    name = str(team.get("name") or "").strip()
    if not provider_team_id and not name:
        return {}

    squad = []
    squad_container = team.get("squad") if isinstance(team.get("squad"), dict) else {}
    for player in _as_list(squad_container.get("player")):
        if not isinstance(player, dict):
            continue
        player_id = str(player.get("id") or "").strip()
        player_name = str(player.get("name") or "").strip()
        if not player_id and not player_name:
            continue
        stats = _player_stat_values(player)
        squad.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "number": str(player.get("number") or "").strip(),
                "age": _int_or_none(player.get("age")),
                "position": str(player.get("position") or "").strip(),
                "is_captain": _bool(player.get("is_captain")),
                "injured": _bool(player.get("injured")),
                "appearances": stats.get("appearances"),
                "minutes_played": stats.get("minutes_played"),
                "goals": stats.get("goals"),
                "assists": stats.get("assists"),
                "shots_total": stats.get("shots_total"),
                "shots_on": stats.get("shots_on"),
                "yellowcards": stats.get("yellowcards"),
                "redcards": stats.get("redcards"),
                "rating": stats.get("rating"),
                "stats": stats,
                "raw": player,
            }
        )

    transfers = team.get("transfers") if isinstance(team.get("transfers"), dict) else {}
    coach = team.get("coach") if isinstance(team.get("coach"), dict) else {}
    return {
        "provider": "statpal",
        "source": "statpal",
        "id": f"statpal:{provider_team_id}",
        "provider_team_id": provider_team_id,
        "team_id": provider_team_id,
        "name": name,
        "country": str(team.get("country") or "").strip(),
        "founded": _int_or_none(team.get("founded")),
        "is_national_team": _bool(team.get("is_national_team")),
        "is_women": _bool(team.get("is_women")),
        "league_ids": _league_ids(team.get("leagues")),
        "venue": {
            "id": str(team.get("venue_id") or "").strip(),
            "name": str(team.get("venue_name") or "").strip(),
            "surface": str(team.get("venue_surface") or "").strip(),
            "capacity": _int_or_none(team.get("venue_capacity")),
            "address": str(team.get("venue_address") or "").strip(),
            "city": str(team.get("venue_city") or "").strip(),
        },
        "coach": {
            "id": str(coach.get("id") or "").strip(),
            "name": str(coach.get("name") or "").strip(),
        },
        "squad": squad,
        "squad_count": len(squad),
        "transfers": {
            "in": _transfer_rows(transfers, "in"),
            "out": _transfer_rows(transfers, "out"),
        },
        "trophies": _trophies(team.get("trophies")),
        "league_stats": _team_league_stats(team.get("league_stats")),
        "feed_updated": str(payload.get("updated") or "").strip(),
        "feed_updated_ts": _int_or_none(payload.get("updated_ts")),
        "raw": team,
    }


def _player_competition_stats(container, key, scope):
    if not isinstance(container, dict):
        return []
    rows = []
    for item in _as_list(container.get(key)):
        if not isinstance(item, dict):
            continue
        team_id = str(item.get("team_id") or "").strip()
        team_name = str(item.get("team_name") or "").strip()
        league_id = str(item.get("league_id") or "").strip()
        league_name = str(item.get("league") or "").strip()
        if not team_id and not team_name and not league_id and not league_name:
            continue
        stats = _player_stat_values(item)
        rows.append(
            {
                "scope": scope,
                "team_id": team_id,
                "team_name": team_name,
                "league_id": league_id,
                "league": league_name,
                "season": str(item.get("season") or "").strip(),
                "is_captain": _bool(item.get("is_captain")),
                "appearances": stats.get("appearances"),
                "minutes_played": stats.get("minutes_played"),
                "goals": stats.get("goals"),
                "assists": stats.get("assists"),
                "shots_total": stats.get("shots_total"),
                "shots_on": stats.get("shots_on"),
                "yellowcards": stats.get("yellowcards"),
                "redcards": stats.get("redcards"),
                "rating": stats.get("rating"),
                "stats": stats,
                "raw": item,
            }
        )
    return rows


def _player_transfers(value):
    rows = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "date": _statpal_date(str(item.get("date") or "")),
                "date_raw": str(item.get("date") or "").strip(),
                "from": str(item.get("from") or "").strip(),
                "from_id": str(item.get("from_id") or "").strip(),
                "to": str(item.get("to") or "").strip(),
                "to_id": str(item.get("to_id") or "").strip(),
                "type": str(item.get("type") or "").strip(),
                "price": str(item.get("price") or "").strip(),
                "raw": item,
            }
        )
    return rows


def _sidelined_history(value):
    rows = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "type": str(item.get("type") or "").strip(),
                "date_start": _statpal_date(str(item.get("date_start") or "")),
                "date_end": _statpal_date(str(item.get("date_end") or "")),
                "date_start_raw": str(item.get("date_start") or "").strip(),
                "date_end_raw": str(item.get("date_end") or "").strip(),
                "raw": item,
            }
        )
    return rows


def normalize_player(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize StatPal's soccer/players/{player_id} response into a player profile."""
    if not isinstance(payload, dict):
        return {}
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    provider_player_id = str(player.get("id") or "").strip()
    name = str(player.get("name") or "").strip()
    if not provider_player_id and not name:
        return {}

    club_league_stats = _player_competition_stats(player.get("club_league_statistics"), "club", "club_league")
    club_domestic_cup_stats = _player_competition_stats(
        player.get("club_domestic_cup_statistics"),
        "club",
        "club_domestic_cup",
    )
    club_intl_cup_stats = _player_competition_stats(player.get("club_intl_cup_statistics"), "club", "club_intl_cup")
    national_team_stats = _player_competition_stats(player.get("national_team_statistics"), "leagues", "national_team")

    return {
        "provider": "statpal",
        "source": "statpal",
        "id": f"statpal:{provider_player_id}",
        "provider_player_id": provider_player_id,
        "player_id": provider_player_id,
        "name": name,
        "firstname": str(player.get("firstname") or "").strip(),
        "lastname": str(player.get("lastname") or "").strip(),
        "age": _int_or_none(player.get("age")),
        "birthdate": _statpal_date(str(player.get("birthdate") or "")),
        "nationality": str(player.get("nationality") or "").strip(),
        "birthplace": str(player.get("birthplace") or "").strip(),
        "birthcountry": str(player.get("birthcountry") or "").strip(),
        "position": str(player.get("position") or "").strip(),
        "height_cm": _int_or_none(player.get("height")),
        "weight_kg": _int_or_none(player.get("weight")),
        "preferred_foot": str(player.get("preferred_foot") or "").strip(),
        "team": str(player.get("team") or "").strip(),
        "team_id": str(player.get("team_id") or "").strip(),
        "national_team_id": str(player.get("national_team_id") or "").strip(),
        "market_value_eur": _int_or_none(player.get("market_value_eur")),
        "club_league_statistics": club_league_stats,
        "club_domestic_cup_statistics": club_domestic_cup_stats,
        "club_intl_cup_statistics": club_intl_cup_stats,
        "overall_club_statistics": _player_stat_values(
            player.get("overall_club_statistics") if isinstance(player.get("overall_club_statistics"), dict) else {}
        ),
        "national_team_statistics": national_team_stats,
        "statistics": club_league_stats + club_domestic_cup_stats + club_intl_cup_stats + national_team_stats,
        "transfers": _player_transfers(player.get("transfers")),
        "trophies": _trophies(player.get("trophies")),
        "sidelined_history": _sidelined_history(player.get("sidelined_history")),
        "feed_updated": str(payload.get("updated") or "").strip(),
        "feed_updated_ts": _int_or_none(payload.get("updated_ts")),
        "raw": player,
    }


def _statpal_kv_list(value):
    merged = {}
    for item in _as_list(value):
        if isinstance(item, dict):
            for key, child in item.items():
                merged[str(key)] = _int_or_none(child)
    return merged


def _h2h_match(match):
    if not isinstance(match, dict):
        return {}
    return {
        "match_id": f"statpal:{str(match.get('main_id') or '').strip()}",
        "provider_match_id": str(match.get("main_id") or "").strip(),
        "fallback_match_ids": [str(match.get("fallback_id_1")).strip()] if match.get("fallback_id_1") not in ("", None) else [],
        "country": str(match.get("country") or "").strip(),
        "league": str(match.get("league") or "").strip(),
        "provider_competition_id": str(match.get("league_id") or "").strip(),
        "date": _statpal_date(str(match.get("date") or "")),
        "team1_name": str(match.get("team1_name") or "").strip(),
        "team2_name": str(match.get("team2_name") or "").strip(),
        "team1_id": str(match.get("team1_id") or "").strip(),
        "team2_id": str(match.get("team2_id") or "").strip(),
        "team1_score": _int_or_none(match.get("team1_score")),
        "team2_score": _int_or_none(match.get("team2_score")),
        "raw": match,
    }


def _h2h_match_list(container):
    if not isinstance(container, dict):
        return []
    return [item for item in (_h2h_match(match) for match in _as_list(container.get("match"))) if item]


def _h2h_match_section(root, section):
    data = root.get(section) if isinstance(root.get(section), dict) else {}
    return {
        "team1": _h2h_match((data.get("team1") or {}).get("match")) if isinstance(data.get("team1"), dict) else {},
        "team2": _h2h_match((data.get("team2") or {}).get("match")) if isinstance(data.get("team2"), dict) else {},
    }


def _h2h_form_section(root, section):
    data = root.get(section) if isinstance(root.get(section), dict) else {}
    return {
        "team1": _h2h_match_list(data.get("team1") if isinstance(data.get("team1"), dict) else {}),
        "team2": _h2h_match_list(data.get("team2") if isinstance(data.get("team2"), dict) else {}),
    }


def normalize_head_to_head(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize StatPal's soccer/head-to-head response into aggregate H2H context."""
    root = (payload or {}).get("head-to-head") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return {}
    overall = root.get("overall_record") if isinstance(root.get("overall_record"), dict) else {}
    goals = root.get("goals") if isinstance(root.get("goals"), dict) else {}
    leagues = []
    for league in _as_list((root.get("leagues") or {}).get("league") if isinstance(root.get("leagues"), dict) else None):
        if not isinstance(league, dict):
            continue
        leagues.append(
            {
                "id": str(league.get("id") or "").strip(),
                "name": str(league.get("name") or "").strip(),
                "games": _int_or_none(league.get("games")),
                "team1_won": _int_or_none(league.get("team1_won")),
                "team2_won": _int_or_none(league.get("team2_won")),
                "draws": _int_or_none(_first(league.get("draw"), league.get("draws"))),
                "raw": league,
            }
        )
    return {
        "provider": "statpal",
        "source": "statpal",
        "id": f"statpal:h2h:{str(root.get('team1_id') or '').strip()}:{str(root.get('team2_id') or '').strip()}",
        "team1_id": str(root.get("team1_id") or "").strip(),
        "team2_id": str(root.get("team2_id") or "").strip(),
        "recent_meetings": _h2h_match_list(root.get("recent_meetings") if isinstance(root.get("recent_meetings"), dict) else {}),
        "overall_record": {
            "total": _statpal_kv_list((overall.get("total") or {}).get("total") if isinstance(overall.get("total"), dict) else None),
            "home": {
                "team1": _statpal_kv_list((overall.get("home") or {}).get("team1") if isinstance(overall.get("home"), dict) else None),
                "team2": _statpal_kv_list((overall.get("home") or {}).get("team2") if isinstance(overall.get("home"), dict) else None),
            },
            "away": {
                "team1": _statpal_kv_list((overall.get("away") or {}).get("team1") if isinstance(overall.get("away"), dict) else None),
                "team2": _statpal_kv_list((overall.get("away") or {}).get("team2") if isinstance(overall.get("away"), dict) else None),
            },
        },
        "leagues": leagues,
        "goals": {
            "total": _statpal_kv_list((goals.get("total") or {}).get("total") if isinstance(goals.get("total"), dict) else None),
            "home": _statpal_kv_list((goals.get("home") or {}).get("home") if isinstance(goals.get("home"), dict) else None),
            "away": _statpal_kv_list((goals.get("away") or {}).get("away") if isinstance(goals.get("away"), dict) else None),
        },
        "biggest_victory": _h2h_match_section(root, "biggest_victory"),
        "biggest_defeat": _h2h_match_section(root, "biggest_defeat"),
        "last5_home": _h2h_form_section(root, "last5_home"),
        "last5_away": _h2h_form_section(root, "last5_away"),
        "raw": root,
    }


def _sidelined_players(container):
    if not isinstance(container, dict):
        return []
    players = []
    for player in _as_list(container.get("player")):
        if not isinstance(player, dict):
            continue
        player_id = str(player.get("id") or "").strip()
        name = str(player.get("name") or "").strip()
        if not player_id and not name:
            continue
        players.append(
            {
                "id": player_id,
                "name": name,
                "status": str(player.get("status") or "").strip(),
                "raw": player,
            }
        )
    return players


def _injury_side(team):
    if not isinstance(team, dict):
        return {"team_id": "", "team_name": "", "to_miss": [], "questionable": [], "to_miss_count": 0, "questionable_count": 0}
    sidelined = team.get("sidelined") if isinstance(team.get("sidelined"), dict) else {}
    to_miss = _sidelined_players(sidelined.get("to_miss") if isinstance(sidelined, dict) else None)
    questionable = _sidelined_players(sidelined.get("questionable") if isinstance(sidelined, dict) else None)
    return {
        "team_id": str(team.get("id") or "").strip(),
        "team_name": str(team.get("name") or "").strip(),
        "to_miss": to_miss,
        "questionable": questionable,
        "to_miss_count": len(to_miss),
        "questionable_count": len(questionable),
        "availability_risk": "high" if len(to_miss) >= 3 else "medium" if to_miss or len(questionable) >= 2 else "low",
    }


def normalize_injuries_suspensions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/injuries-suspensions response one row per match."""
    root = (payload or {}).get("injuries_suspensions") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    feed_updated = str(root.get("updated") or "")
    feed_updated_ts = _int_or_none(root.get("updated_ts"))
    rows = []
    for league in _as_list(root.get("league")):
        if not isinstance(league, dict):
            continue
        provider_competition_id = str(league.get("id") or "").strip()
        for match in _as_list(league.get("match")):
            if not isinstance(match, dict):
                continue
            provider_match_id = str(_first(match.get("main_id"), match.get("fallback_id_1"))).strip()
            if not provider_match_id:
                continue
            home = _injury_side(match.get("home"))
            away = _injury_side(match.get("away"))
            rows.append(
                {
                    "provider": "statpal",
                    "source": "statpal",
                    "match_id": f"statpal:{provider_match_id}",
                    "provider_match_id": provider_match_id,
                    "fallback_match_ids": [
                        str(value).strip()
                        for value in (match.get("fallback_id_1"), match.get("fallback_id_2"), match.get("fallback_id_3"))
                        if value not in ("", None)
                    ],
                    "provider_competition_id": provider_competition_id,
                    "league": str(league.get("name") or "").strip(),
                    "sub_id": str(league.get("sub_id") or "").strip(),
                    "date": _statpal_date(str(match.get("date") or "")),
                    "kickoff": str(match.get("time") or "").strip(),
                    "home": home,
                    "away": away,
                    "total_to_miss_count": home["to_miss_count"] + away["to_miss_count"],
                    "total_questionable_count": home["questionable_count"] + away["questionable_count"],
                    "feed_updated": feed_updated,
                    "feed_updated_ts": feed_updated_ts,
                    "raw": match,
                }
            )
    return rows


def _coach(side):
    data = side if isinstance(side, dict) else {}
    coach = data.get("coach") if isinstance(data.get("coach"), dict) else data
    if not isinstance(coach, dict):
        return {}
    return {"id": str(coach.get("id") or "").strip(), "name": str(coach.get("name") or "").strip()}


def _referee(data):
    if not isinstance(data, dict):
        return {}
    return {"id": str(data.get("id") or "").strip(), "name": str(data.get("name") or "").strip()}


def _lineup_side(data):
    if not isinstance(data, dict):
        return {"formation": "", "players": []}
    players = []
    for item in _as_list(data.get("player")):
        if not isinstance(item, dict):
            continue
        player_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not player_id and not name:
            continue
        players.append(
            {
                "id": player_id,
                "name": name,
                "number": str(item.get("number") or "").strip(),
                "position": str(item.get("pos") or "").strip(),
                "formation_position": str(item.get("formation_pos") or "").strip(),
                "booking": str(item.get("booking") or "").strip(),
            }
        )
    return {"formation": str(data.get("formation") or "").strip(), "players": players}


def _lineups(data):
    if not isinstance(data, dict):
        return {"home": {"formation": "", "players": []}, "away": {"formation": "", "players": []}}
    return {"home": _lineup_side(data.get("home")), "away": _lineup_side(data.get("away"))}


def _projected_lineup_players(value):
    players = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        player_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not player_id and not name:
            continue
        players.append(
            {
                "id": player_id,
                "name": name,
                "number": str(item.get("number") or "").strip(),
                "position": str(item.get("position") or "").strip(),
                "raw": item,
            }
        )
    return players


def _projected_sidelined_players(value):
    players = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        player_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not player_id and not name:
            continue
        players.append(
            {
                "id": player_id,
                "name": name,
                "number": str(item.get("number") or "").strip(),
                "position": str(item.get("position") or "").strip(),
                "status": str(item.get("status") or "").strip(),
                "reason": None if item.get("reason") is None else str(item.get("reason") or "").strip(),
                "raw": item,
            }
        )
    return players


def _projected_lineup_side(data):
    if not isinstance(data, dict):
        return {
            "team_id": "",
            "team_name": "",
            "coach": {},
            "formation": "",
            "starting_xi": [],
            "bench": [],
            "sidelined": [],
            "confidence": None,
        }
    coach = data.get("coach") if isinstance(data.get("coach"), dict) else {}
    starting_xi = _projected_lineup_players(data.get("starting_xi"))
    bench = _projected_lineup_players(data.get("bench"))
    sidelined = _projected_sidelined_players(data.get("sidelined"))
    return {
        "team_id": str(data.get("team_id") or "").strip(),
        "team_name": str(data.get("team_name") or "").strip(),
        "coach": {
            "id": str(coach.get("id") or "").strip(),
            "name": str(coach.get("name") or "").strip(),
        },
        "formation": str(data.get("team_formation") or "").strip(),
        "starting_xi": starting_xi,
        "bench": bench,
        "sidelined": sidelined,
        "confidence": _int_or_none(data.get("confidence")),
        "starting_count": len(starting_xi),
        "bench_count": len(bench),
        "sidelined_count": len(sidelined),
    }


def normalize_team_lineups(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize StatPal's soccer/team-lineups response into one match lineup profile."""
    if not isinstance(payload, dict):
        return {}
    provider_match_id = str(payload.get("main_id") or "").strip()
    if not provider_match_id:
        return {}
    home = _projected_lineup_side(payload.get("home"))
    away = _projected_lineup_side(payload.get("away"))
    return {
        "provider": "statpal",
        "source": "statpal",
        "id": f"statpal:lineups:{provider_match_id}",
        "match_id": f"statpal:{provider_match_id}",
        "provider_match_id": provider_match_id,
        "status": str(payload.get("status") or "").strip(),
        "lineup_status": str(payload.get("status") or "").strip(),
        "home": home,
        "away": away,
        "starting_count": home["starting_count"] + away["starting_count"],
        "bench_count": home["bench_count"] + away["bench_count"],
        "sidelined_count": home["sidelined_count"] + away["sidelined_count"],
        "feed_updated": str(payload.get("updated") or "").strip(),
        "feed_updated_ts": _int_or_none(payload.get("updated_ts")),
        "raw": payload,
    }


def _odds_items(value):
    items = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        items.append(
            {
                "name": name,
                "value": _num_or_none(item.get("value")),
                "value_raw": str(item.get("value") or "").strip(),
                "raw": item,
            }
        )
    return items


def _odds_lines(value, line_type):
    lines = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        line_name = str(item.get("name") or "").strip()
        if not line_name:
            continue
        lines.append(
            {
                "type": line_type,
                "name": line_name,
                "line": _num_or_none(line_name),
                "stop": _bool(item.get("stop")),
                "is_main": _bool(item.get("is_main")) if item.get("is_main") not in ("", None) else None,
                "odds": _odds_items(item.get("odd")),
                "raw": item,
            }
        )
    return lines


def _bookmakers(value):
    rows = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        bookmaker_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not bookmaker_id and not name:
            continue
        rows.append(
            {
                "id": bookmaker_id,
                "name": name,
                "timestamp": _int_or_none(item.get("timestamp")),
                "odds": _odds_items(item.get("odd")),
                "handicaps": _odds_lines(item.get("handicap"), "handicap"),
                "totals": _odds_lines(item.get("total"), "total"),
                "raw": item,
            }
        )
    return rows


def _prematch_markets(value):
    markets = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        market_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not market_id and not name:
            continue
        bookmakers = _bookmakers(item.get("bookmaker"))
        markets.append(
            {
                "id": market_id,
                "name": name,
                "stop": _bool(item.get("stop")),
                "bookmakers": bookmakers,
                "bookmaker_count": len(bookmakers),
                "raw": item,
            }
        )
    return markets


def normalize_prematch_odds(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues/{league_id}/odds/prematch response one row per match."""
    root = (payload or {}).get("prematch_odds") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    league = root.get("league") if isinstance(root.get("league"), dict) else {}
    provider_competition_id = str(league.get("id") or "").strip()
    rows = []
    for match in _as_list(league.get("match")):
        if not isinstance(match, dict):
            continue
        provider_match_id = str(_first(match.get("main_id"), match.get("fallback_id_1"))).strip()
        if not provider_match_id:
            continue
        home = match.get("home") if isinstance(match.get("home"), dict) else {}
        away = match.get("away") if isinstance(match.get("away"), dict) else {}
        markets = _prematch_markets(match.get("odds"))
        rows.append(
            {
                "provider": "statpal",
                "source": "statpal",
                "id": f"statpal:prematch_odds:{provider_match_id}",
                "match_id": f"statpal:{provider_match_id}",
                "provider_match_id": provider_match_id,
                "fallback_match_ids": [
                    str(value).strip()
                    for value in (match.get("fallback_id_1"), match.get("fallback_id_2"), match.get("fallback_id_3"))
                    if value not in ("", None)
                ],
                "provider_competition_id": provider_competition_id,
                "league": str(league.get("name") or "").strip(),
                "country": str(league.get("country") or "").strip(),
                "date": _statpal_date(str(match.get("date") or "")),
                "kickoff": str(match.get("time") or "").strip(),
                "home": {
                    "id": str(home.get("id") or "").strip(),
                    "name": str(home.get("name") or "").strip(),
                },
                "away": {
                    "id": str(away.get("id") or "").strip(),
                    "name": str(away.get("name") or "").strip(),
                },
                "fixture": f"{str(home.get('name') or '').strip()} vs {str(away.get('name') or '').strip()}",
                "markets": markets,
                "market_count": len(markets),
                "feed_updated": str(root.get("updated") or "").strip(),
                "feed_updated_ts": _int_or_none(root.get("updated_ts")),
                "raw": match,
            }
        )
    return rows


def _substitution_side(data):
    if not isinstance(data, dict):
        return []
    items = []
    for item in _as_list(data.get("substitution")):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "minute": str(item.get("minute") or "").strip(),
                "player_in_id": str(_first(item.get("player_in_id"), item.get("player_on_id"))).strip(),
                "player_in_name": str(_first(item.get("player_in_name"), item.get("player_on"))).strip(),
                "player_in_number": str(item.get("player_in_number") or "").strip(),
                "player_in_booking": str(item.get("player_in_booking") or "").strip(),
                "player_out_id": str(_first(item.get("player_out_id"), item.get("player_off_id"))).strip(),
                "player_out_name": str(_first(item.get("player_out_name"), item.get("player_off"))).strip(),
                "injury": _bool(item.get("injury")),
            }
        )
    return items


def _substitutions(data):
    if not isinstance(data, dict):
        return {"home": [], "away": []}
    return {"home": _substitution_side(data.get("home")), "away": _substitution_side(data.get("away"))}


def _goals(data):
    if not isinstance(data, dict):
        return []
    goals = []
    for item in _as_list(data.get("goal") or data.get("event")):
        if not isinstance(item, dict):
            continue
        if data.get("event") is not None and str(item.get("type") or "").lower() != "goal":
            continue
        goals.append(
            {
                "team": str(item.get("team") or "").strip(),
                "minute": str(item.get("minute") or "").strip(),
                "player": str(item.get("player") or "").strip(),
                "player_id": str(_first(item.get("playerid"), item.get("player_id"))).strip(),
                "assist": str(_first(item.get("assist"), item.get("assist_player"))).strip(),
                "assist_id": str(_first(item.get("assistid"), item.get("assist_id"))).strip(),
                "score": str(item.get("score") or item.get("result") or "").strip(),
            }
        )
    return goals


def _bench(data):
    return _lineups(data)


def _metric_bucket(data):
    if not isinstance(data, dict):
        return {}
    return {str(key): _num_or_none(value) for key, value in data.items()}


def _team_stats_side(data):
    if not isinstance(data, dict):
        return {}
    return {str(key): _metric_bucket(value) for key, value in data.items() if isinstance(value, dict)}


def _team_stats(data):
    if not isinstance(data, dict):
        return {"home": {}, "away": {}}
    return {"home": _team_stats_side(data.get("home")), "away": _team_stats_side(data.get("away"))}


def _player_stats_side(data):
    if not isinstance(data, dict):
        return []
    players = []
    for item in _as_list(data.get("player")):
        if not isinstance(item, dict):
            continue
        player_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not player_id and not name:
            continue
        stats = {}
        for key, value in item.items():
            if key in {"id", "name", "num", "number", "pos"}:
                continue
            stats[str(key)] = _num_or_none(value)
        players.append(
            {
                "id": player_id,
                "name": name,
                "number": str(_first(item.get("num"), item.get("number"))).strip(),
                "position": str(item.get("pos") or "").strip(),
                "stats": stats,
                "raw": item,
            }
        )
    return players


def _player_stats(data):
    if not isinstance(data, dict):
        return {"home": [], "away": []}
    return {"home": _player_stats_side(data.get("home")), "away": _player_stats_side(data.get("away"))}


def _summary_events(container, *, side: str, kind: str):
    if container in ("", None) or not isinstance(container, dict):
        return []
    events = []
    for item in _as_list(container.get("event")):
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "team": side,
                "type": kind,
                "minute": str(item.get("minute") or "").strip(),
                "extra_min": str(item.get("extra_min") or "").strip(),
                "player_id": str(item.get("player_id") or "").strip(),
                "player_name": str(item.get("player_name") or "").strip(),
                "assist_player_id": str(item.get("assist_player_id") or "").strip(),
                "assist_player_name": str(item.get("assist_player_name") or "").strip(),
                "comment": str(item.get("comment") or "").strip(),
                "event_type": str(item.get("event_type") or "").strip(),
                "ref_decision": str(item.get("ref_decision") or "").strip(),
                "var_decision": str(item.get("var_decision") or "").strip(),
                "own_goal": _bool(item.get("own_goal")),
                "penalty": _bool(item.get("penalty")),
                "penalty_missed": _bool(item.get("penalty_missed")),
                "var_cancelled": _bool(item.get("var_cancelled")),
                "raw": item,
            }
        )
    return events


def _event_summary(data):
    summary = {"goals": [], "yellowcards": [], "redcards": [], "var": []}
    if not isinstance(data, dict):
        return summary
    for side in ("home", "away"):
        side_data = data.get(side) if isinstance(data.get(side), dict) else {}
        for kind in summary:
            summary[kind].extend(_summary_events(side_data.get(kind), side=side, kind=kind))
    return summary


def _match_info(data):
    if not isinstance(data, dict):
        return {"stadium": "", "referee": "", "scheduled_time": "", "added_time_period_1": "", "added_time_period_2": ""}
    time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
    stadium = data.get("stadium") if isinstance(data.get("stadium"), dict) else {}
    referee = data.get("referee") if isinstance(data.get("referee"), dict) else {}
    return {
        "stadium": str(stadium.get("name") or "").strip(),
        "referee": str(referee.get("name") or "").strip(),
        "scheduled_time": str(time_info.get("name") or "").strip(),
        "added_time_period_1": str(time_info.get("added_time_period_1") or "").strip(),
        "added_time_period_2": str(time_info.get("added_time_period_2") or "").strip(),
    }


def normalize_match_stats(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize StatPal's soccer/leagues/{league_id}/matches/stats response."""
    root = (payload or {}).get("match-stats") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    tournament = root.get("tournament") if isinstance(root.get("tournament"), dict) else {}
    matches = _as_list(tournament.get("matches"))
    normalized = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        home = match.get("home") if isinstance(match.get("home"), dict) else {}
        away = match.get("away") if isinstance(match.get("away"), dict) else {}
        home_name, away_name = _name(home), _name(away)
        provider_match_id = str(_first(match.get("main_id"), match.get("id"), match.get("match_id"), match.get("fallback_id_1"))).strip()
        if not provider_match_id or not home_name or not away_name:
            continue
        fallback_ids = [
            str(value).strip()
            for value in (match.get("fallback_id_1"), match.get("fallback_id_2"), match.get("fallback_id_3"))
            if value not in ("", None)
        ]
        info = _match_info(match.get("match_info"))
        ht = match.get("ht") if isinstance(match.get("ht"), dict) else {}
        ft = match.get("ft") if isinstance(match.get("ft"), dict) else {}
        team_stats = _team_stats(match.get("team_stats"))
        event_summary = _event_summary(match.get("event_summary"))
        item = {
            "provider": "statpal",
            "source": "statpal",
            "match_id": f"statpal:{provider_match_id}",
            "provider_match_id": provider_match_id,
            "fallback_match_ids": fallback_ids,
            "provider_competition_id": str(tournament.get("id") or "").strip(),
            "league": str(tournament.get("name") or "").strip(),
            "fixture": f"{home_name} vs {away_name}",
            "hname": home_name,
            "aname": away_name,
            "hid": _id(home),
            "aid": _id(away),
            "date": _statpal_date(str(match.get("date") or "")),
            "kickoff": str(match.get("time") or info.get("scheduled_time") or "").strip(),
            "status": str(match.get("status") or "").strip(),
            "venue": info.get("stadium") or str(match.get("venue") or "").strip(),
            "referee": {"id": "", "name": info.get("referee") or ""},
            "home_goals": _score_from(home),
            "away_goals": _score_from(away),
            "ht_home_goals": _int_or_none(ht.get("home_goals")),
            "ht_away_goals": _int_or_none(ht.get("away_goals")),
            "ft_home_goals": _int_or_none(ft.get("home_goals")),
            "ft_away_goals": _int_or_none(ft.get("away_goals")),
            "match_info": info,
            "lineups": _lineups(match.get("lineups")),
            "bench": _bench(match.get("bench")),
            "substitutions": _substitutions(match.get("substitutions")),
            "team_stats": team_stats,
            "player_stats": _player_stats(match.get("player_stats")),
            "event_summary": event_summary,
            "goals": event_summary["goals"],
            "yellowcards": event_summary["yellowcards"],
            "redcards": event_summary["redcards"],
            "var_events": event_summary["var"],
            "team_colors": match.get("team_colors") if isinstance(match.get("team_colors"), dict) else {},
            "feed_updated": str(root.get("updated") or ""),
            "feed_updated_ts": root.get("updated_ts"),
            "raw": match,
        }
        normalized.append(item)
    return normalized


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
        feed = match.get("_feed") if isinstance(match.get("_feed"), dict) else {}
        context = match.get("match_context") if isinstance(match.get("match_context"), dict) else {}
        ht = match.get("ht") if isinstance(match.get("ht"), dict) else {}
        ft = match.get("ft") if isinstance(match.get("ft"), dict) else {}
        coaches = match.get("coaches") if isinstance(match.get("coaches"), dict) else {}
        normalized_lineups = _lineups(match.get("lineups"))
        normalized_substitutions = _substitutions(match.get("substitutions"))
        normalized_goals = _goals(match.get("goals") or match.get("events"))
        normalized_referee = _referee(match.get("referee"))
        provider_competition_id = str(_first(
            match.get("league_id"),
            match.get("competition_id"),
            league.get("id") if isinstance(league, dict) else "",
        ))
        fallback_ids = [
            str(value).strip()
            for value in (match.get("fallback_id_1"), match.get("fallback_id_2"), match.get("fallback_id_3"))
            if value not in ("", None)
        ]
        raw_payload = dict(match)
        raw_payload.update(
            {
                "provider_match_id": provider_match_id,
                "provider_competition_id": provider_competition_id,
                "fallback_match_ids": fallback_ids,
                "home_goals": _score_from(home),
                "away_goals": _score_from(away),
                "week": str(match.get("_week") or ""),
                "match_context": context,
                "lineups_normalized": normalized_lineups,
                "substitutions_normalized": normalized_substitutions,
                "goals_normalized": normalized_goals,
                "coaches_normalized": {"home": _coach(coaches.get("home")), "away": _coach(coaches.get("away"))},
                "referee_normalized": normalized_referee,
                "feed_updated": feed.get("updated") or "",
                "feed_updated_ts": feed.get("updated_ts"),
            }
        )
        fixtures.append(
            {
                "fixture": f"{home_name} vs {away_name}",
                "hname": home_name,
                "aname": away_name,
                "hid": _id(home) or match.get("home_id"),
                "aid": _id(away) or match.get("away_id"),
                "league": _name(league) or str(match.get("league_name") or ""),
                "country": str((league.get("country") if isinstance(league, dict) else "") or match.get("country") or ""),
                "round": str(match.get("round") or match.get("_week") or ""),
                "league_type": "cup" if _bool((league.get("cup") if isinstance(league, dict) else "") or match.get("cup")) else str((league.get("type") if isinstance(league, dict) else "") or ""),
                "code": provider_competition_id,
                "season": str((league.get("season") if isinstance(league, dict) else "") or match.get("season") or ""),
                "stage_id": str((league.get("stage_id") if isinstance(league, dict) else "") or match.get("stage_id") or ""),
                "stage_is_current": _bool((league.get("is_current") if isinstance(league, dict) else "") or match.get("is_current")),
                "week": str(match.get("_week") or ""),
                "kickoff": _kickoff_label(match),
                "kickoff_utc": kickoff_utc.isoformat() if kickoff_utc else "",
                "match_id": f"statpal:{provider_match_id}",
                "provider_match_id": provider_match_id,
                "fallback_match_ids": fallback_ids,
                "provider_competition_id": provider_competition_id,
                "status": str(match.get("status") or ""),
                "venue": str(match.get("venue") or ""),
                "venue_id": str(match.get("venue_id") or ""),
                "venue_city": str(match.get("venue_city") or ""),
                "attendance": str(match.get("attendance") or ""),
                "home_goals": _score_from(home),
                "away_goals": _score_from(away),
                "ht_home_goals": _int_or_none(ht.get("home_goals")),
                "ht_away_goals": _int_or_none(ht.get("away_goals")),
                "ft_home_goals": _int_or_none(ft.get("home_goals")),
                "ft_away_goals": _int_or_none(ft.get("away_goals")),
                "lineups": normalized_lineups,
                "substitutions": normalized_substitutions,
                "goals": normalized_goals,
                "coaches": {"home": _coach(coaches.get("home")), "away": _coach(coaches.get("away"))},
                "referee": normalized_referee,
                "has_live_stats": _bool(match.get("has_live_stats")),
                "inplay_odds_running": _bool(match.get("inplay_odds_running")),
                "match_context": {
                    "live_storylines": _bool(context.get("live_storylines")),
                    "weather_forecast": _bool(context.get("weather_forecast")),
                    "team_lineups": _bool(context.get("team_lineups")),
                    "predictions": _bool(context.get("predictions")),
                },
                "feed_updated": str(feed.get("updated") or ""),
                "feed_updated_ts": feed.get("updated_ts"),
                "source": "statpal",
                "date": match_date,
                "api_payload": raw_payload,
            }
        )
    return fixtures
