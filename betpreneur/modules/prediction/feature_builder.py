"""Feature assembly for the shared prediction engine.

This module is deliberately read-only: it gathers stored fixture, team,
league, lineup, availability, snapshot, and score-model context into one
normalized payload for statistical models. It must not make recommendation
decisions or call external providers.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    FixtureCache,
    StatPalFixtureSnapshot,
    normalize_referee_name,
    team_intelligence_service,
)
from betpreneur.modules.scoring.api import (
    FixtureLineup,
    PlayerAvailability,
    TeamRateProfile,
    score_model_service,
)

from .contracts import FixtureFeatureSet, PredictionDiagnostics, TeamStrengthSnapshot
from .models import PredictionTeamMatchFeedback

QUALITY_RANK = {
    "missing": 0,
    "unavailable": 0,
    "poor": 1,
    "limited": 2,
    "partial": 2,
    "medium": 3,
    "fresh": 3,
    "strong": 4,
    "available": 4,
}


def build_fixture_features(fixture=None, *, fixture_id: str = "") -> FixtureFeatureSet:
    """Return a normalized feature container for one fixture.

    ``fixture`` may be a ``FixtureCache`` instance, a fixture-shaped dict, or a
    fixture id. The returned ``features`` dict is stable enough for Poisson,
    Elo, Monte Carlo, calibration, ML models, and explanation generation.
    """
    fixture_obj, fixture_payload, resolved_id = _resolve_fixture(fixture, fixture_id=fixture_id)
    fixture_name = _fixture_name(fixture_payload)
    league_id = _provider_league_id(fixture_payload)
    home_team_id = _side_provider_id(fixture_payload, "home")
    away_team_id = _side_provider_id(fixture_payload, "away")

    intelligence = team_intelligence_service.for_fixture(fixture_payload)
    league_key = str(intelligence.get("league_key") or fixture_payload.get("league_key") or "")
    season = str(
        intelligence.get("season")
        or fixture_payload.get("season")
        or _season_from_fixture(fixture_payload)
        or ""
    )
    if season and "season" not in fixture_payload:
        fixture_payload["season"] = season

    goal_model = _goal_model_payload(
        league_id=league_id,
        home_team_name=str(fixture_payload.get("home_team") or ""),
        away_team_name=str(fixture_payload.get("away_team") or ""),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    home_features = _side_features(
        intelligence.get("home"),
        side="home",
        league_key=league_key,
        season=season,
        fallback_name=str(fixture_payload.get("home_team") or ""),
        provider_team_id=home_team_id,
        fixture_id=resolved_id,
    )
    away_features = _side_features(
        intelligence.get("away"),
        side="away",
        league_key=league_key,
        season=season,
        fallback_name=str(fixture_payload.get("away_team") or ""),
        provider_team_id=away_team_id,
        fixture_id=resolved_id,
    )
    snapshots = _snapshot_payloads(fixture_obj=fixture_obj, fixture_id=resolved_id)
    referee = _referee_context(fixture_payload, snapshots)
    league_features = _league_features(intelligence, goal_model=goal_model)
    api_football_features = _api_football_features(fixture_payload)
    scoreline_profile = _scoreline_profile_payload(
        home=home_features,
        away=away_features,
        snapshots=snapshots,
    )
    freshness = _freshness_payload(
        intelligence=intelligence,
        goal_model=goal_model,
        home=home_features,
        away=away_features,
        snapshots=snapshots,
        referee=referee,
    )

    return FixtureFeatureSet(
        fixture_id=resolved_id,
        fixture_name=fixture_name,
        league_key=league_key,
        season=season,
        home_team=home_features["strength_snapshot"],
        away_team=away_features["strength_snapshot"],
        features={
            "fixture": _public_fixture_payload(fixture_payload, fixture_id=resolved_id),
            "goal_model": goal_model,
            "home": _feature_side_payload(home_features),
            "away": _feature_side_payload(away_features),
            "league": league_features,
            "odds_snapshots": snapshots["odds"],
            "provider_snapshots": snapshots["by_type"],
            "lineups": {
                "home": home_features["lineup"],
                "away": away_features["lineup"],
            },
            "player_availability": {
                "home": home_features["availability"],
                "away": away_features["availability"],
            },
            "market_family_history": {
                "home": home_features["market_profiles_by_family"],
                "away": away_features["market_profiles_by_family"],
                "league": league_features["market_profiles_by_family"],
            },
            "data_freshness": freshness,
            "provider_quality": _provider_quality(intelligence, freshness),
            "referee": referee,
            "api_football": api_football_features,
            "scoreline_profile": scoreline_profile,
            "prediction_feedback": _prediction_feedback_payload(
                home_team=str(fixture_payload.get("home_team") or ""),
                away_team=str(fixture_payload.get("away_team") or ""),
                fixture_id=resolved_id,
                before_date=_as_date(fixture_payload.get("match_date") or fixture_payload.get("kickoff_utc")),
            ),
        },
        diagnostics=_diagnostics(
            intelligence=intelligence,
            goal_model=goal_model,
            home=home_features,
            away=away_features,
            snapshots=snapshots,
            referee=referee,
            fixture_found=fixture_obj is not None or bool(fixture_payload),
        ),
    )


def _resolve_fixture(fixture, *, fixture_id: str) -> tuple[FixtureCache | None, dict[str, Any], str]:
    if fixture is None and fixture_id:
        fixture_obj = _fixture_from_id(fixture_id)
        return fixture_obj, _fixture_to_payload(fixture_obj), str(fixture_id)
    if isinstance(fixture, FixtureCache):
        resolved_id = str(fixture_id or fixture.match_id or fixture.pk)
        return fixture, _fixture_to_payload(fixture), resolved_id
    if isinstance(fixture, dict):
        payload = dict(fixture)
        resolved_id = str(fixture_id or payload.get("match_id") or payload.get("id") or "")
        fixture_obj = _fixture_from_id(resolved_id) if resolved_id else None
        return fixture_obj, payload, resolved_id
    if fixture is not None:
        resolved_id = str(fixture_id or fixture)
        fixture_obj = _fixture_from_id(resolved_id)
        return fixture_obj, _fixture_to_payload(fixture_obj), resolved_id
    return None, {}, str(fixture_id or "")


def _fixture_from_id(fixture_id: str) -> FixtureCache | None:
    if not fixture_id:
        return None
    return FixtureCache.objects.filter(match_id=str(fixture_id)).first()


def _fixture_to_payload(fixture: FixtureCache | None) -> dict[str, Any]:
    if fixture is None:
        return {}
    payload = dict(fixture.api_payload or {})
    payload.update(
        {
            "match_id": fixture.match_id,
            "fixture": fixture.fixture,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
            "country": fixture.country,
            "round": fixture.round,
            "league_type": fixture.league_type,
            "kickoff": fixture.kickoff,
            "kickoff_utc": fixture.kickoff_utc,
            "match_date": fixture.match_date,
            "source": fixture.source,
        }
    )
    return payload


def _public_fixture_payload(fixture: dict[str, Any], *, fixture_id: str) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "fixture_name": _fixture_name(fixture),
        "home_team": fixture.get("home_team") or "",
        "away_team": fixture.get("away_team") or "",
        "league": fixture.get("league") or "",
        "country": fixture.get("country") or "",
        "season": fixture.get("season") or _season_from_fixture(fixture) or "",
        "match_date": _iso(fixture.get("match_date")),
        "kickoff": fixture.get("kickoff") or "",
        "kickoff_utc": _iso(fixture.get("kickoff_utc")),
        "provider_league_id": _provider_league_id(fixture),
        "home_team_id": _side_provider_id(fixture, "home"),
        "away_team_id": _side_provider_id(fixture, "away"),
        "source": fixture.get("source") or "",
    }


def _prediction_feedback_payload(
    *, home_team: str, away_team: str, fixture_id: str, before_date: date | None
) -> dict[str, Any]:
    return {
        "home": _team_prediction_feedback(
            team_name=home_team,
            opponent_name=away_team,
            fixture_id=fixture_id,
            before_date=before_date,
        ),
        "away": _team_prediction_feedback(
            team_name=away_team,
            opponent_name=home_team,
            fixture_id=fixture_id,
            before_date=before_date,
        ),
    }


def _team_prediction_feedback(
    *, team_name: str, opponent_name: str, fixture_id: str, before_date: date | None
) -> dict[str, Any]:
    team_name = str(team_name or "").strip()
    if not team_name:
        return {"matches": 0, "recent": [], "vs_opponent": []}
    qs = PredictionTeamMatchFeedback.objects.filter(team_name__iexact=team_name).exclude(
        fixture_id=str(fixture_id or "")
    )
    if before_date:
        qs = qs.filter(match_date__lt=before_date)
    recent_rows = list(qs.order_by("-match_date", "-id")[:8])
    opponent_rows = []
    opponent_name = str(opponent_name or "").strip()
    if opponent_name:
        opponent_rows = list(
            qs.filter(opponent_name__iexact=opponent_name).order_by("-match_date", "-id")[:5]
        )
    return {
        "matches": qs.count(),
        "recent": [_feedback_row_payload(row) for row in recent_rows],
        "vs_opponent": [_feedback_row_payload(row) for row in opponent_rows],
        "summary": _feedback_summary(recent_rows),
    }


def _feedback_row_payload(row: PredictionTeamMatchFeedback) -> dict[str, Any]:
    return {
        "fixture_id": row.fixture_id,
        "fixture": row.fixture_name,
        "match_date": _iso(row.match_date),
        "team": row.team_name,
        "opponent": row.opponent_name,
        "side": row.side,
        "actual_result": row.actual_result,
        "goals_for": row.goals_for,
        "goals_against": row.goals_against,
        "corners_for": row.corners_for,
        "corners_against": row.corners_against,
        "cards_for": row.cards_for,
        "cards_against": row.cards_against,
        "shots_on_target_for": row.shots_on_target_for,
        "shots_on_target_against": row.shots_on_target_against,
        "prediction_snapshot": row.prediction_snapshot,
    }


def _feedback_summary(rows: list[PredictionTeamMatchFeedback]) -> dict[str, Any]:
    if not rows:
        return {}
    return {
        "matches": len(rows),
        "wins": sum(1 for row in rows if row.actual_result == "win"),
        "draws": sum(1 for row in rows if row.actual_result == "draw"),
        "losses": sum(1 for row in rows if row.actual_result == "loss"),
        "avg_goals_for": _average(row.goals_for for row in rows),
        "avg_goals_against": _average(row.goals_against for row in rows),
        "avg_corners_for": _average(row.corners_for for row in rows),
        "avg_corners_against": _average(row.corners_against for row in rows),
        "avg_cards_for": _average(row.cards_for for row in rows),
        "avg_cards_against": _average(row.cards_against for row in rows),
        "avg_shots_on_target_for": _average(row.shots_on_target_for for row in rows),
        "avg_shots_on_target_against": _average(row.shots_on_target_against for row in rows),
    }


def _average(values) -> float | None:
    items = [float(value) for value in values if value is not None]
    if not items:
        return None
    return round(sum(items) / len(items), 3)


def _scoreline_profile_payload(*, home: dict[str, Any], away: dict[str, Any], snapshots: dict[str, Any]) -> dict[str, Any]:
    home_team = getattr(home.get("strength_snapshot"), "team_name", "") if home.get("strength_snapshot") else ""
    away_team = getattr(away.get("strength_snapshot"), "team_name", "") if away.get("strength_snapshot") else ""
    home_rows = _recent_fixture_rows(
        home.get("recent_form") or {},
        preferred_scope="all",
        source="home_recent",
        team_name=home_team,
    )
    away_rows = _recent_fixture_rows(
        away.get("recent_form") or {},
        preferred_scope="all",
        source="away_recent",
        team_name=away_team,
    )
    h2h_rows = _h2h_fixture_rows(snapshots)
    combined_rows = [*home_rows, *away_rows, *h2h_rows]
    return {
        "home_recent": _scoreline_profile(home_rows),
        "away_recent": _scoreline_profile(away_rows),
        "head_to_head": _scoreline_profile(h2h_rows),
        "combined": _scoreline_profile(combined_rows),
    }


def _api_football_features(fixture_payload: dict[str, Any]) -> dict[str, Any]:
    context = fixture_payload.get("api_football_context")
    if not isinstance(context, dict):
        fixture_context = fixture_payload.get("fixture_context") if isinstance(fixture_payload.get("fixture_context"), dict) else {}
        context = (fixture_context.get("api_football") or {})
    snapshots = context.get("snapshots") if isinstance(context, dict) else {}
    if not isinstance(snapshots, dict) or not snapshots:
        return {"available": False, "snapshots": {}}

    home_team_id = str(fixture_payload.get("api_football_home_team_id") or fixture_payload.get("hid") or "").strip()
    away_team_id = str(fixture_payload.get("api_football_away_team_id") or fixture_payload.get("aid") or "").strip()
    prediction = _api_snapshot_payload(snapshots, "prediction")
    home_stats = _api_snapshot_payload(snapshots, "team_statistics_home")
    away_stats = _api_snapshot_payload(snapshots, "team_statistics_away")
    home_recent = _api_recent_scorelines(
        _api_snapshot_payload(snapshots, "recent_fixtures_home"),
        team_id=home_team_id,
        source="api_football_home_recent",
    )
    away_recent = _api_recent_scorelines(
        _api_snapshot_payload(snapshots, "recent_fixtures_away"),
        team_id=away_team_id,
        source="api_football_away_recent",
    )
    h2h_rows = _api_prediction_h2h_scorelines(prediction)
    corner_samples = {
        "home": _api_corner_samples(_api_snapshot_payload(snapshots, "fixture_statistics_home"), team_id=home_team_id),
        "away": _api_corner_samples(_api_snapshot_payload(snapshots, "fixture_statistics_away"), team_id=away_team_id),
    }

    available_snapshots = sorted(
        key for key, value in snapshots.items() if isinstance(value, dict) and value.get("available")
    )
    return {
        "available": bool(available_snapshots),
        "available_snapshots": available_snapshots,
        "prediction_opinion": _api_prediction_opinion(prediction),
        "team_statistics": {
            "home": _api_team_statistics_summary(home_stats),
            "away": _api_team_statistics_summary(away_stats),
        },
        "recent_scorelines": {
            "home": _scoreline_profile(home_recent),
            "away": _scoreline_profile(away_recent),
            "combined": _scoreline_profile([*home_recent, *away_recent]),
        },
        "head_to_head": _scoreline_profile(h2h_rows),
        "corner_samples": {
            "home": _corner_sample_summary(corner_samples["home"]),
            "away": _corner_sample_summary(corner_samples["away"]),
            "combined": _corner_sample_summary([*corner_samples["home"], *corner_samples["away"]]),
        },
    }


def _api_snapshot_payload(snapshots: dict[str, Any], key: str):
    item = snapshots.get(key) if isinstance(snapshots, dict) else {}
    payload = item.get("payload") if isinstance(item, dict) else item
    if isinstance(payload, list) and len(payload) == 1 and key.startswith("team_statistics"):
        return payload[0]
    return payload or {}


def _api_prediction_opinion(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"available": False}
    predictions = payload.get("predictions") if isinstance(payload.get("predictions"), dict) else {}
    percent = predictions.get("percent") if isinstance(predictions.get("percent"), dict) else {}
    winner = predictions.get("winner") if isinstance(predictions.get("winner"), dict) else {}
    comparison = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else {}
    return {
        "available": bool(predictions),
        "winner": {
            "id": winner.get("id"),
            "name": winner.get("name") or "",
            "comment": winner.get("comment") or "",
        },
        "win_or_draw": predictions.get("win_or_draw"),
        "under_over": predictions.get("under_over") or "",
        "team_goals": predictions.get("goals") if isinstance(predictions.get("goals"), dict) else {},
        "advice": predictions.get("advice") or "",
        "percent": {
            "home": _percent_or_none(percent.get("home")),
            "draw": _percent_or_none(percent.get("draw")),
            "away": _percent_or_none(percent.get("away")),
        },
        "comparison": {
            key: {
                "home": _percent_or_none((value or {}).get("home")) if isinstance(value, dict) else None,
                "away": _percent_or_none((value or {}).get("away")) if isinstance(value, dict) else None,
            }
            for key, value in comparison.items()
            if isinstance(value, dict)
        },
    }


def _api_team_statistics_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"available": False}
    fixtures = payload.get("fixtures") if isinstance(payload.get("fixtures"), dict) else {}
    goals = payload.get("goals") if isinstance(payload.get("goals"), dict) else {}
    played = fixtures.get("played") if isinstance(fixtures.get("played"), dict) else {}
    wins = fixtures.get("wins") if isinstance(fixtures.get("wins"), dict) else {}
    draws = fixtures.get("draws") if isinstance(fixtures.get("draws"), dict) else {}
    losses = fixtures.get("loses") if isinstance(fixtures.get("loses"), dict) else {}
    goals_for = goals.get("for") if isinstance(goals.get("for"), dict) else {}
    goals_against = goals.get("against") if isinstance(goals.get("against"), dict) else {}
    total_played = _int_or_none(played.get("total")) or 0
    return {
        "available": True,
        "team": payload.get("team") if isinstance(payload.get("team"), dict) else {},
        "league": payload.get("league") if isinstance(payload.get("league"), dict) else {},
        "form": payload.get("form") or "",
        "record": {
            "played": _side_totals(played),
            "wins": _side_totals(wins),
            "draws": _side_totals(draws),
            "losses": _side_totals(losses),
        },
        "goals_for": _goal_totals(goals_for),
        "goals_against": _goal_totals(goals_against),
        "goal_line_counts": {
            "for": _goal_line_counts(goals_for.get("under_over") if isinstance(goals_for.get("under_over"), dict) else {}),
            "against": _goal_line_counts(goals_against.get("under_over") if isinstance(goals_against.get("under_over"), dict) else {}),
        },
        "clean_sheet_rate": _rate(_int_or_none((payload.get("clean_sheet") or {}).get("total")) or 0, total_played),
        "failed_to_score_rate": _rate(_int_or_none((payload.get("failed_to_score") or {}).get("total")) or 0, total_played),
        "lineups": [
            {"formation": item.get("formation") or "", "played": _int_or_none(item.get("played")) or 0}
            for item in (payload.get("lineups") if isinstance(payload.get("lineups"), list) else [])[:5]
            if isinstance(item, dict)
        ],
        "cards": payload.get("cards") if isinstance(payload.get("cards"), dict) else {},
    }


def _side_totals(payload: dict[str, Any]) -> dict[str, int | None]:
    return {
        "home": _int_or_none(payload.get("home")),
        "away": _int_or_none(payload.get("away")),
        "total": _int_or_none(payload.get("total")),
    }


def _goal_totals(payload: dict[str, Any]) -> dict[str, Any]:
    total = payload.get("total") if isinstance(payload.get("total"), dict) else {}
    average = payload.get("average") if isinstance(payload.get("average"), dict) else {}
    return {
        "total": _side_totals(total),
        "average": {
            "home": _float_or_none(average.get("home")),
            "away": _float_or_none(average.get("away")),
            "total": _float_or_none(average.get("total")),
        },
    }


def _goal_line_counts(payload: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    return {
        str(line): {
            "over": _int_or_none((value or {}).get("over")) if isinstance(value, dict) else None,
            "under": _int_or_none((value or {}).get("under")) if isinstance(value, dict) else None,
        }
        for line, value in payload.items()
    }


def _api_recent_scorelines(payload: Any, *, team_id: str, source: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        goals = row.get("goals") if isinstance(row.get("goals"), dict) else {}
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        home_goals = _int_or_none(goals.get("home"))
        away_goals = _int_or_none(goals.get("away"))
        if home_goals is None or away_goals is None:
            continue
        is_home = str(home.get("id") or "") == str(team_id)
        is_away = str(away.get("id") or "") == str(team_id)
        if not is_home and not is_away:
            continue
        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals
        opponent = away if is_home else home
        team = home if is_home else away
        parsed.append({
            "match_id": fixture.get("id") or "",
            "match_date": _iso(fixture.get("date")),
            "fixture": f"{home.get('name') or ''} vs {away.get('name') or ''}".strip(),
            "opponent": opponent.get("name") or "",
            "source": source,
            "team_name": team.get("name") or "",
            "opponent_name": opponent.get("name") or "",
            "result": "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L",
            "goals_for": goals_for,
            "goals_against": goals_against,
            "scoreline": f"{goals_for}-{goals_against}",
            "total_goals": goals_for + goals_against,
        })
    return parsed


def _api_prediction_h2h_scorelines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("h2h") if isinstance(payload.get("h2h"), list) else []
    parsed = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        goals = row.get("goals") if isinstance(row.get("goals"), dict) else {}
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        home_goals = _int_or_none(goals.get("home"))
        away_goals = _int_or_none(goals.get("away"))
        if home_goals is None or away_goals is None:
            continue
        fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
        parsed.append({
            "match_id": fixture.get("id") or "",
            "match_date": _iso(fixture.get("date")),
            "fixture": f"{home.get('name') or ''} vs {away.get('name') or ''}".strip(),
            "opponent": away.get("name") or "",
            "source": "api_football_prediction_h2h",
            "team_name": home.get("name") or "",
            "opponent_name": away.get("name") or "",
            "result": "W" if home_goals > away_goals else "D" if home_goals == away_goals else "L",
            "goals_for": home_goals,
            "goals_against": away_goals,
            "scoreline": f"{home_goals}-{away_goals}",
            "total_goals": home_goals + away_goals,
        })
    return parsed


def _api_corner_samples(payload: Any, *, team_id: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    samples = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        own = _float_or_none(row.get("corner_kicks_for"))
        stats = row.get("payload") if isinstance(row.get("payload"), list) else []
        total = 0.0
        for stat_row in stats:
            statistics = stat_row.get("statistics") if isinstance(stat_row, dict) else []
            total += _api_stat_value(statistics, "Corner Kicks") or 0.0
        if own is None:
            own = _api_team_stat_value(stats, team_id=team_id, stat_type="Corner Kicks")
        against = max(total - own, 0.0) if own is not None and total else None
        if own is None:
            continue
        samples.append({
            "fixture_id": row.get("fixture_id") or "",
            "fixture": row.get("fixture") or "",
            "date": _iso(row.get("date")),
            "corners_for": own,
            "corners_against": against,
            "total_corners": total or None,
        })
    return samples


def _api_team_stat_value(stats: list[Any], *, team_id: str, stat_type: str) -> float | None:
    for row in stats or []:
        if not isinstance(row, dict):
            continue
        team = row.get("team") if isinstance(row.get("team"), dict) else {}
        if str(team.get("id") or "") != str(team_id):
            continue
        return _api_stat_value(row.get("statistics") if isinstance(row.get("statistics"), list) else [], stat_type)
    return None


def _api_stat_value(statistics: list[Any], stat_type: str) -> float | None:
    for item in statistics or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != stat_type.strip().lower():
            continue
        return _float_or_none(item.get("value"))
    return None


def _corner_sample_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        return {"games": 0, "samples": [], "avg_for": None, "avg_against": None, "avg_total": None}
    return {
        "games": len(clean),
        "samples": clean[:10],
        "avg_for": _average(row.get("corners_for") for row in clean),
        "avg_against": _average(row.get("corners_against") for row in clean),
        "avg_total": _average(row.get("total_corners") for row in clean),
    }


def _recent_fixture_rows(
    recent_form: dict[str, Any],
    *,
    preferred_scope: str,
    source: str,
    team_name: str,
) -> list[dict[str, Any]]:
    form = (
        (recent_form.get(preferred_scope) or {}).get("10")
        or (recent_form.get("all") or {}).get("10")
        or (recent_form.get(preferred_scope) or {}).get("5")
        or (recent_form.get("all") or {}).get("5")
        or {}
    )
    stats = form.get("stats") if isinstance(form.get("stats"), dict) else {}
    rows = stats.get("fixtures") if isinstance(stats.get("fixtures"), list) else []
    return [
        parsed
        for row in rows
        if (parsed := _scoreline_row(row, source=source, team_name=team_name))
    ]


def _h2h_fixture_rows(snapshots: dict[str, Any]) -> list[dict[str, Any]]:
    by_type = (snapshots or {}).get("by_type") or {}
    h2h = by_type.get(StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD) or {}
    payload = h2h.get("payload") if isinstance(h2h.get("payload"), dict) else {}
    recent = payload.get("recent_meetings") if isinstance(payload.get("recent_meetings"), list) else []
    rows = []
    for row in recent[:10]:
        if not isinstance(row, dict):
            continue
        goals_for = _int_or_none(row.get("team1_score"))
        goals_against = _int_or_none(row.get("team2_score"))
        if goals_for is None or goals_against is None:
            continue
        rows.append({
            "match_id": row.get("match_id") or row.get("provider_match_id") or "",
            "match_date": _iso(row.get("date")),
            "fixture": f"{row.get('team1_name') or ''} vs {row.get('team2_name') or ''}".strip(),
            "opponent": row.get("team2_name") or "",
            "source": "head_to_head",
            "team_name": row.get("team1_name") or "",
            "opponent_name": row.get("team2_name") or "",
            "result": "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L",
            "goals_for": goals_for,
            "goals_against": goals_against,
        })
    return rows


def _scoreline_row(row: dict[str, Any], *, source: str = "", team_name: str = "") -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    goals_for = _int_or_none(row.get("goals_for"))
    goals_against = _int_or_none(row.get("goals_against"))
    if goals_for is None or goals_against is None:
        return {}
    return {
        "match_id": row.get("match_id") or "",
        "match_date": row.get("match_date") or "",
        "fixture": row.get("fixture") or "",
        "opponent": row.get("opponent") or "",
        "source": source or row.get("source") or "",
        "team_name": team_name or row.get("team_name") or "",
        "opponent_name": row.get("opponent") or row.get("opponent_name") or "",
        "result": row.get("result") or ("W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "scoreline": f"{goals_for}-{goals_against}",
        "total_goals": goals_for + goals_against,
    }


def _scoreline_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean_rows = [row for row in rows if isinstance(row, dict)]
    games = len(clean_rows)
    if not games:
        return {
            "games": 0,
            "scorelines": [],
            "avg_total_goals": None,
            "volatility": "unknown",
        }
    totals = [int(row.get("total_goals") or 0) for row in clean_rows]
    over_15 = _rate(sum(1 for total in totals if total > 1.5), games)
    over_25 = _rate(sum(1 for total in totals if total > 2.5), games)
    over_35 = _rate(sum(1 for total in totals if total > 3.5), games)
    over_45 = _rate(sum(1 for total in totals if total > 4.5), games)
    low_total = _rate(sum(1 for total in totals if total <= 2), games)
    volatility = "high" if over_35 >= 45 or over_25 >= 70 else "low" if low_total >= 70 else "medium"
    return {
        "games": games,
        "scorelines": [
            {
                "match_id": row.get("match_id") or "",
                "match_date": row.get("match_date") or "",
                "fixture": row.get("fixture") or "",
                "opponent": row.get("opponent") or "",
                "source": row.get("source") or "",
                "team_name": row.get("team_name") or "",
                "opponent_name": row.get("opponent_name") or row.get("opponent") or "",
                "result": row.get("result") or "",
                "goals_for": row.get("goals_for"),
                "goals_against": row.get("goals_against"),
                "scoreline": row.get("scoreline") or f"{row.get('goals_for')}-{row.get('goals_against')}",
                "total_goals": row.get("total_goals"),
            }
            for row in clean_rows[:10]
        ],
        "avg_total_goals": round(sum(totals) / games, 2),
        "over_1_5_rate": over_15,
        "over_2_5_rate": over_25,
        "over_3_5_rate": over_35,
        "over_4_5_rate": over_45,
        "btts_rate": _rate(
            sum(1 for row in clean_rows if int(row.get("goals_for") or 0) > 0 and int(row.get("goals_against") or 0) > 0),
            games,
        ),
        "clean_sheet_rate": _rate(sum(1 for row in clean_rows if int(row.get("goals_against") or 0) == 0), games),
        "failed_to_score_rate": _rate(sum(1 for row in clean_rows if int(row.get("goals_for") or 0) == 0), games),
        "scored_2_plus_rate": _rate(sum(1 for row in clean_rows if int(row.get("goals_for") or 0) >= 2), games),
        "conceded_2_plus_rate": _rate(sum(1 for row in clean_rows if int(row.get("goals_against") or 0) >= 2), games),
        "low_total_rate": low_total,
        "volatility": volatility,
    }


def _rate(count: int, games: int) -> float | None:
    if not games:
        return None
    return round((count / games) * 100, 1)


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _percent_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    return _float_or_none(value)


def _fixture_name(fixture: dict[str, Any]) -> str:
    name = str(fixture.get("fixture") or fixture.get("name") or "").strip()
    if name:
        return name
    home = str(fixture.get("home_team") or "").strip()
    away = str(fixture.get("away_team") or "").strip()
    return f"{home} vs {away}".strip() if home or away else ""


def _provider_league_id(fixture: dict[str, Any]) -> str:
    return str(
        fixture.get("statpal_provider_competition_id")
        or fixture.get("api_football_league_id")
        or fixture.get("provider_competition_id")
        or fixture.get("league_id")
        or fixture.get("code")
        or ""
    ).strip()


def _side_provider_id(fixture: dict[str, Any], side: str) -> str:
    return str(
        fixture.get(f"statpal_{side}_team_id")
        or fixture.get(f"api_football_{side}_team_id")
        or fixture.get(f"{side}_team_id")
        or fixture.get("hid" if side == "home" else "aid")
        or ""
    ).strip()


def _season_from_fixture(fixture: dict[str, Any]) -> str:
    raw = fixture.get("match_date") or fixture.get("kickoff_utc")
    value = _as_date(raw)
    if value is None:
        return ""
    return f"{value.year}-{value.year + 1}" if value.month >= 7 else f"{value.year - 1}-{value.year}"


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _side_features(
    team_payload: dict[str, Any] | None,
    *,
    side: str,
    league_key: str,
    season: str,
    fallback_name: str,
    provider_team_id: str,
    fixture_id: str,
) -> dict[str, Any]:
    season_profile = dict((team_payload or {}).get("season_profile") or {})
    recent_form = _recent_form_by_scope((team_payload or {}).get("recent_form") or ())
    market_profiles = _market_profiles_by_family((team_payload or {}).get("market_profiles") or ())
    rate_profile = _team_rate_profile(provider_team_id=provider_team_id, fallback_name=fallback_name)
    lineup = _lineup_payload(fixture_id=fixture_id, side=side)
    availability = _availability_payload(
        fixture_id=fixture_id,
        provider_team_id=provider_team_id,
        fallback_name=fallback_name,
    )
    strength = _strength_snapshot(
        team_payload,
        side=side,
        league_key=league_key,
        season=season,
        fallback_name=fallback_name,
        provider_team_id=provider_team_id,
        season_profile=season_profile,
        recent_form=recent_form,
    )
    return {
        "strength_snapshot": strength,
        "season_profile": season_profile,
        "recent_form": recent_form,
        "market_profiles_by_family": market_profiles,
        "rate_profile": rate_profile,
        "lineup": lineup,
        "availability": availability,
        "coverage": dict((team_payload or {}).get("coverage") or {"status": "missing"}),
    }


def _feature_side_payload(features: dict[str, Any]) -> dict[str, Any]:
    return {
        "strength": features["strength_snapshot"].to_dict(),
        "season_profile": features["season_profile"],
        "recent_form": features["recent_form"],
        "market_profiles_by_family": features["market_profiles_by_family"],
        "rate_profile": features["rate_profile"],
        "coverage": features["coverage"],
    }


def _strength_snapshot(
    team_payload: dict[str, Any] | None,
    *,
    side: str,
    league_key: str,
    season: str,
    fallback_name: str,
    provider_team_id: str,
    season_profile: dict[str, Any],
    recent_form: dict[str, Any],
) -> TeamStrengthSnapshot:
    team_payload = team_payload or {}
    team_id = str(team_payload.get("team_id") or provider_team_id or "")
    recent_all = recent_form.get("all", {}).get("5") or {}
    return TeamStrengthSnapshot(
        team_id=team_id,
        team_name=str(team_payload.get("canonical_name") or fallback_name or ""),
        league_key=league_key,
        season=season,
        attack_rating=_attack_rating(season_profile, side=side),
        defence_rating=_defence_rating(season_profile, side=side),
        recent_form_score=_points_per_game(recent_all),
        data_quality=str(season_profile.get("data_quality") or "missing"),
    )


def _attack_rating(profile: dict[str, Any], *, side: str) -> float | None:
    if side == "home":
        return _per_match(profile.get("home_goals_for"), profile.get("home_matches")) or _per_match(
            profile.get("goals_for"), profile.get("matches_played")
        )
    return _per_match(profile.get("away_goals_for"), profile.get("away_matches")) or _per_match(
        profile.get("goals_for"), profile.get("matches_played")
    )


def _defence_rating(profile: dict[str, Any], *, side: str) -> float | None:
    if side == "home":
        return _per_match(profile.get("home_goals_against"), profile.get("home_matches")) or _per_match(
            profile.get("goals_against"), profile.get("matches_played")
        )
    return _per_match(profile.get("away_goals_against"), profile.get("away_matches")) or _per_match(
        profile.get("goals_against"), profile.get("matches_played")
    )


def _per_match(total, matches) -> float | None:
    total_value = _float_or_none(total)
    match_count = _float_or_none(matches)
    if total_value is None or not match_count:
        return None
    return round(total_value / match_count, 4)


def _recent_average(value, matches, *, ceiling: float) -> float | None:
    total_value = _float_or_none(value)
    if total_value is None:
        return None
    match_count = _float_or_none(matches)
    if match_count and total_value > ceiling:
        return round(total_value / match_count, 4)
    return round(total_value, 4)


def _points_per_game(form: dict[str, Any]) -> float | None:
    matches = _float_or_none(form.get("matches"))
    if not matches:
        return None
    points = float(form.get("wins") or 0) * 3 + float(form.get("draws") or 0)
    return round(points / matches, 4)


def _recent_form_by_scope(rows) -> dict[str, dict[str, Any]]:
    by_scope: dict[str, dict[str, Any]] = {"all": {}, "home": {}, "away": {}}
    for row in rows:
        scope = str(row.get("scope") or "all")
        window = str(row.get("window") or "")
        if not window:
            continue
        item = dict(row)
        item["points_per_game"] = _points_per_game(item)
        item["goals_for_per_match"] = _recent_average(item.get("goals_for"), item.get("matches"), ceiling=6.0)
        item["goals_against_per_match"] = _recent_average(item.get("goals_against"), item.get("matches"), ceiling=6.0)
        item["corners_for_per_match"] = _recent_average(item.get("corners_for"), item.get("matches"), ceiling=15.0)
        item["cards_for_per_match"] = _recent_average(item.get("cards_for"), item.get("matches"), ceiling=8.0)
        item["shots_on_target_for_per_match"] = _recent_average(
            item.get("shots_on_target_for"),
            item.get("matches"),
            ceiling=15.0,
        )
        by_scope.setdefault(scope, {})[window] = item
    return by_scope


def _market_profiles_by_family(rows) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        family = str(row.get("market_family") or "unknown")
        grouped.setdefault(family, []).append(dict(row))
    return grouped


def _goal_model_payload(
    *,
    league_id: str,
    home_team_name: str,
    away_team_name: str,
    home_team_id: str,
    away_team_id: str,
) -> dict[str, Any]:
    rates = score_model_service.rates_for_fixture(
        league_id=league_id,
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    return {
        "home_expected_goals": rates.home_rate,
        "away_expected_goals": rates.away_rate,
        "expected_total_goals": round(rates.home_rate + rates.away_rate, 4),
        "home_baseline": rates.home_baseline,
        "away_baseline": rates.away_baseline,
        "baseline_total_goals": round(rates.home_baseline + rates.away_baseline, 4),
        "data_quality": rates.data_quality,
        "league_id": rates.league_id,
        "model_version": rates.model_version,
        "matched_home": rates.matched_home,
        "matched_away": rates.matched_away,
        "home_matches": rates.home_matches,
        "away_matches": rates.away_matches,
        "usable": rates.usable,
        "differentiated": rates.differentiated,
    }


def _league_features(intelligence: dict[str, Any], *, goal_model: dict[str, Any]) -> dict[str, Any]:
    league_payload = dict(intelligence.get("league") or {})
    market_profiles = _market_profiles_by_family(league_payload.get("market_profiles") or ())
    return {
        "league_key": intelligence.get("league_key") or "",
        "league_name": intelligence.get("league_name") or "",
        "season": intelligence.get("season") or "",
        "coverage": league_payload.get("coverage") or {"status": "missing"},
        "scoring_environment": {
            "home_goal_baseline": goal_model.get("home_baseline"),
            "away_goal_baseline": goal_model.get("away_baseline"),
            "expected_total_goals": goal_model.get("baseline_total_goals"),
            "goal_model_quality": goal_model.get("data_quality"),
        },
        "season_maturity": {
            "home_team_matches": goal_model.get("home_matches") or 0,
            "away_team_matches": goal_model.get("away_matches") or 0,
            "minimum_team_matches": min(goal_model.get("home_matches") or 0, goal_model.get("away_matches") or 0),
            "differentiated": bool(goal_model.get("differentiated")),
        },
        "market_profiles_by_family": market_profiles,
    }


def _team_rate_profile(*, provider_team_id: str, fallback_name: str) -> dict[str, Any]:
    query = TeamRateProfile.objects.filter(provider="statpal")
    profile = None
    if provider_team_id:
        profile = query.filter(team_id=provider_team_id).order_by("-fetched_at").first()
    if profile is None and fallback_name:
        profile = query.filter(team_name__iexact=fallback_name).order_by("-fetched_at").first()
    if profile is None:
        return {"available": False}
    return {
        "available": True,
        "team_id": profile.team_id,
        "team_name": profile.team_name,
        "league_id": profile.league_id,
        "corners_home": profile.corners_home,
        "corners_away": profile.corners_away,
        "cards_home": profile.cards_home,
        "cards_away": profile.cards_away,
        "shots_on_target_home": profile.shots_on_target_home,
        "shots_on_target_away": profile.shots_on_target_away,
        "fouls_per_game": profile.fouls_per_game,
        "matches": profile.matches,
        "fetched_at": _iso(profile.fetched_at),
    }


def _lineup_payload(*, fixture_id: str, side: str) -> dict[str, Any]:
    if not fixture_id:
        return {"available": False}
    lineup = (
        FixtureLineup.objects.filter(provider="statpal", match_id=fixture_id, side=side)
        .order_by("-fetched_at")
        .first()
    )
    if lineup is None:
        return {"available": False}
    return {
        "available": True,
        "team_id": lineup.team_id,
        "team_name": lineup.team_name,
        "formation": lineup.formation,
        "confidence": lineup.confidence,
        "confirmed": lineup.confirmed,
        "starting_count": len(lineup.starting_xi or []),
        "bench_count": len(lineup.bench or []),
        "fetched_at": _iso(lineup.fetched_at),
    }


def _availability_payload(*, fixture_id: str, provider_team_id: str, fallback_name: str) -> dict[str, Any]:
    if not fixture_id and not provider_team_id and not fallback_name:
        return {"available": False, "total": 0, "by_status": {}}
    rows = PlayerAvailability.objects.filter(provider="statpal")
    if fixture_id:
        rows = rows.filter(match_id=fixture_id)
    if provider_team_id:
        rows = rows.filter(Q(team_id=provider_team_id) | Q(team_name__iexact=fallback_name))
    elif fallback_name:
        rows = rows.filter(team_name__iexact=fallback_name)
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    return {"available": bool(by_status), "total": sum(by_status.values()), "by_status": by_status}


def _snapshot_payloads(*, fixture_obj: FixtureCache | None, fixture_id: str) -> dict[str, Any]:
    if not fixture_id and fixture_obj is None:
        return {"odds": {}, "by_type": {}}
    query = StatPalFixtureSnapshot.objects.filter(status="available")
    if fixture_obj is not None:
        query = query.filter(
            Q(fixture=fixture_obj) | Q(match_id=fixture_obj.match_id) | Q(provider_match_id=fixture_obj.match_id)
        )
    else:
        query = query.filter(Q(match_id=fixture_id) | Q(provider_match_id=fixture_id))
    by_type: dict[str, dict[str, Any]] = {}
    for snapshot in query.order_by("snapshot_type", "-fetched_at", "-updated_at"):
        if snapshot.snapshot_type in by_type:
            continue
        by_type[snapshot.snapshot_type] = {
            "available": True,
            "source_endpoint": snapshot.source_endpoint,
            "provider_competition_id": snapshot.provider_competition_id,
            "summary": snapshot.summary,
            "fetched_at": _iso(snapshot.fetched_at),
            "expires_at": _iso(snapshot.expires_at),
        }
        if snapshot.snapshot_type == StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD:
            by_type[snapshot.snapshot_type]["payload"] = {
                "recent_meetings": (snapshot.payload or {}).get("recent_meetings") or [],
            }
    return {
        "odds": {
            "prematch": by_type.get(StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS, {"available": False}),
            "live": by_type.get(StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS, {"available": False}),
        },
        "by_type": by_type,
    }


def _referee_context(fixture_payload: dict[str, Any], snapshots: dict[str, Any]) -> dict[str, Any]:
    referee_payload = fixture_payload.get("referee") if isinstance(fixture_payload.get("referee"), dict) else {}
    match_info = fixture_payload.get("match_info") if isinstance(fixture_payload.get("match_info"), dict) else {}
    match_info_referee = match_info.get("referee")
    if isinstance(match_info_referee, dict):
        match_info_referee = match_info_referee.get("name")
    detailed = (
        (snapshots.get("by_type") or {})
        .get(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS, {})
        .get("summary")
        or {}
    )
    referee_name = str(
        referee_payload.get("name")
        or detailed.get("referee_name")
        or match_info_referee
        or ""
    ).strip()
    referee_key = normalize_referee_name(referee_name)
    referee_id = str(referee_payload.get("id") or detailed.get("referee_id") or "").strip()
    if not referee_name and not referee_id:
        return {"available": False}

    query = StatPalFixtureSnapshot.objects.filter(
        snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
        status="available",
    )
    fixture_id = str(fixture_payload.get("match_id") or "").strip()
    provider_match_id = str(fixture_payload.get("provider_match_id") or "").strip()
    current_ids = {value for value in (fixture_id, provider_match_id, fixture_id.removeprefix("statpal:")) if value}
    if current_ids:
        query = query.exclude(Q(match_id__in=current_ids) | Q(provider_match_id__in=current_ids))
    if referee_id:
        query = query.filter(Q(summary__referee_id=referee_id) | Q(payload__referee__id=referee_id))
    elif referee_key:
        query = query.filter(
            Q(summary__referee_normalized=referee_key)
            | Q(summary__referee_name__iexact=referee_name)
            | Q(payload__referee__name__iexact=referee_name)
            | Q(payload__referee_normalized__name__iexact=referee_name)
            | Q(payload__match_info__referee__iexact=referee_name)
        )

    league_id = str(
        fixture_payload.get("statpal_provider_competition_id")
        or fixture_payload.get("provider_competition_id")
        or fixture_payload.get("league_id")
        or fixture_payload.get("code")
        or ""
    ).strip()
    if league_id:
        query = query.filter(Q(provider_competition_id=league_id) | Q(payload__provider_competition_id=league_id))

    cards: list[float] = []
    booking_points: list[float] = []
    for row in query.order_by("-fetched_at", "-updated_at")[:50]:
        summary = row.summary or {}
        total_cards = _float_or_none(summary.get("total_cards"))
        points = _float_or_none(summary.get("booking_points"))
        if total_cards is not None and 0 <= total_cards <= 12:
            cards.append(total_cards)
        if points is not None and 0 <= points <= 140:
            booking_points.append(points)

    payload = {
        "available": True,
        "name": referee_name,
        "normalized": referee_key,
        "id": referee_id,
        "sample_matches": len(cards),
        "avg_cards_per_match": round(sum(cards) / len(cards), 3) if cards else None,
        "avg_booking_points": round(sum(booking_points) / len(booking_points), 3) if booking_points else None,
        "source": "statpal_detailed_stats",
    }
    if not cards:
        payload["warning"] = "referee_card_history_missing"
    return payload


def _freshness_payload(
    *,
    intelligence: dict[str, Any],
    goal_model: dict[str, Any],
    home: dict[str, Any],
    away: dict[str, Any],
    snapshots: dict[str, Any],
    referee: dict[str, Any],
) -> dict[str, Any]:
    statuses = {
        "team_intelligence": intelligence.get("status") or "missing",
        "goal_model": goal_model.get("data_quality") or "missing",
        "home_coverage": home.get("coverage", {}).get("status") or "missing",
        "away_coverage": away.get("coverage", {}).get("status") or "missing",
        "league_coverage": (intelligence.get("league") or {}).get("coverage", {}).get("status") or "missing",
        "prematch_odds": "fresh" if snapshots["odds"].get("prematch", {}).get("available") else "missing",
        "lineups": "fresh" if home["lineup"].get("available") or away["lineup"].get("available") else "missing",
        "player_availability": "fresh"
        if home["availability"].get("available") or away["availability"].get("available")
        else "missing",
        "referee_context": "fresh"
        if referee.get("avg_cards_per_match")
        else "partial"
        if referee.get("available")
        else "missing",
    }
    return {
        "statuses": statuses,
        "worst_status": _worst_quality(statuses.values()),
        "generated_at": _iso(timezone.now()),
    }


def _provider_quality(intelligence: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    statuses = freshness.get("statuses") or {}
    return {
        "primary_source": str(intelligence.get("source") or "stored_team_intelligence"),
        "team_intelligence_available": bool(intelligence.get("available")),
        "quality": _worst_quality(statuses.values()),
        "statuses": statuses,
    }


def _diagnostics(
    *,
    intelligence: dict[str, Any],
    goal_model: dict[str, Any],
    home: dict[str, Any],
    away: dict[str, Any],
    snapshots: dict[str, Any],
    referee: dict[str, Any],
    fixture_found: bool,
) -> PredictionDiagnostics:
    warnings: list[str] = []
    if not fixture_found:
        warnings.append("fixture_not_found")
    warnings.extend(str(item) for item in intelligence.get("missing") or ())
    if goal_model.get("data_quality") == "poor":
        warnings.append("goal_model_unavailable")
    if not snapshots["odds"].get("prematch", {}).get("available"):
        warnings.append("prematch_odds_snapshot_missing")
    if not home["lineup"].get("available") and not away["lineup"].get("available"):
        warnings.append("lineup_snapshot_missing")
    if not referee.get("available"):
        warnings.append("referee_context_missing")
    elif not referee.get("avg_cards_per_match"):
        warnings.append("referee_card_history_missing")

    data_quality = _worst_quality(
        [
            intelligence.get("status"),
            goal_model.get("data_quality"),
            home.get("coverage", {}).get("status"),
            away.get("coverage", {}).get("status"),
        ]
    )
    return PredictionDiagnostics(
        data_quality=data_quality,
        model_sources=(
            "prediction.feature_builder",
            "catalog.team_intelligence",
            "scoring.score_model_service",
        ),
        warnings=tuple(dict.fromkeys(warnings)),
        metadata={
            "team_intelligence_status": intelligence.get("status"),
            "goal_model_quality": goal_model.get("data_quality"),
            "referee": referee,
        },
    )


def _worst_quality(values) -> str:
    cleaned = [str(value or "missing") for value in values]
    if not cleaned:
        return "missing"
    return min(cleaned, key=lambda item: QUALITY_RANK.get(item, 0))


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
