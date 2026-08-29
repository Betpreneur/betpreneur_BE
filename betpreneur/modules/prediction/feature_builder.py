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
    team_intelligence_service,
)
from betpreneur.modules.scoring.api import (
    FixtureLineup,
    PlayerAvailability,
    TeamRateProfile,
    score_model_service,
)

from .contracts import FixtureFeatureSet, PredictionDiagnostics, TeamStrengthSnapshot

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
    league_features = _league_features(intelligence, goal_model=goal_model)
    freshness = _freshness_payload(
        intelligence=intelligence,
        goal_model=goal_model,
        home=home_features,
        away=away_features,
        snapshots=snapshots,
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
        },
        diagnostics=_diagnostics(
            intelligence=intelligence,
            goal_model=goal_model,
            home=home_features,
            away=away_features,
            snapshots=snapshots,
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
    return {
        "odds": {
            "prematch": by_type.get(StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS, {"available": False}),
            "live": by_type.get(StatPalFixtureSnapshot.SnapshotType.LIVE_ODDS, {"available": False}),
        },
        "by_type": by_type,
    }


def _freshness_payload(
    *,
    intelligence: dict[str, Any],
    goal_model: dict[str, Any],
    home: dict[str, Any],
    away: dict[str, Any],
    snapshots: dict[str, Any],
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
