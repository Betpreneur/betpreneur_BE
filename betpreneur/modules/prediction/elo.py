"""Elo team-strength model.

Elo estimates relative team quality only. It does not read odds, prices, EV, or
product policy. Product layers can use the probabilities for result-family
markets, while recommendation decisions stay outside prediction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .contracts import FixtureFeatureSet, PredictionDiagnostics, ResultProbabilityOutput

BASE_ELO = 1500.0
DEFAULT_HOME_ADVANTAGE = 65.0
DEFAULT_DRAW_PROBABILITY = 0.26
MODEL_VERSION = "elo-v1"


@dataclass(frozen=True)
class _RatingBuild:
    elo: float
    raw_elo: float
    attack_component: float
    defence_component: float
    recent_form_component: float
    market_component: float
    decay_factor: float
    evidence_matches: int
    warnings: tuple[str, ...]


def result_probabilities(features: FixtureFeatureSet) -> ResultProbabilityOutput:
    """Estimate 1X2 probabilities from Elo-style team ratings."""
    if features.home_team is None or features.away_team is None:
        return _unavailable(features, "missing_team_strength")
    if not features.home_team.team_name or not features.away_team.team_name:
        return _unavailable(features, "missing_team_identity")

    payload = features.features or {}
    home = _build_rating(features, "home")
    away = _build_rating(features, "away")
    league_context = _league_context(payload.get("league") or {})
    elo_gap = (home.elo + league_context["home_advantage"]) - away.elo
    home_without_draw = _logistic(elo_gap)
    draw_probability = _draw_probability(elo_gap, league_context["draw_baseline"])
    live_probability = 1.0 - draw_probability
    home_probability = live_probability * home_without_draw
    away_probability = live_probability * (1.0 - home_without_draw)
    home_probability, draw_probability, away_probability = _normalize(
        home_probability,
        draw_probability,
        away_probability,
    )
    warnings = tuple(
        dict.fromkeys(
            (
                *features.diagnostics.warnings,
                *home.warnings,
                *away.warnings,
                *league_context["warnings"],
            )
        )
    )
    data_quality = _result_quality(features, home, away)

    return ResultProbabilityOutput(
        home_win=home_probability,
        draw=draw_probability,
        away_win=away_probability,
        home_elo=round(home.elo, 2),
        away_elo=round(away.elo, 2),
        elo_gap=round(elo_gap, 2),
        diagnostics=PredictionDiagnostics(
            data_quality=data_quality,
            model_version=MODEL_VERSION,
            model_sources=("prediction.elo",),
            warnings=warnings,
            metadata={
                "home_rating": _rating_payload(home),
                "away_rating": _rating_payload(away),
                "home_advantage": league_context["home_advantage"],
                "draw_baseline": league_context["draw_baseline"],
                "league_result_rates": league_context["result_rates"],
            },
        ),
    )


def _unavailable(features: FixtureFeatureSet, warning: str) -> ResultProbabilityOutput:
    return ResultProbabilityOutput(
        diagnostics=PredictionDiagnostics(
            data_quality=features.diagnostics.data_quality,
            model_version=MODEL_VERSION,
            model_sources=("prediction.elo",),
            warnings=tuple(dict.fromkeys((*features.diagnostics.warnings, warning))),
        )
    )


def _build_rating(features: FixtureFeatureSet, side: str) -> _RatingBuild:
    team = features.home_team if side == "home" else features.away_team
    opponent = features.away_team if side == "home" else features.home_team
    side_payload = (features.features or {}).get(side) or {}
    recent = side_payload.get("recent_form") or {}
    season_profile = side_payload.get("season_profile") or {}
    market_profiles = side_payload.get("market_profiles_by_family") or {}
    coverage = side_payload.get("coverage") or {}

    evidence_matches = int(season_profile.get("matches_played") or 0)
    attack = _float(team.attack_rating if team else None)
    defence = _float(team.defence_rating if team else None)
    opponent_defence = _float(opponent.defence_rating if opponent else None)
    opponent_attack = _float(opponent.attack_rating if opponent else None)

    attack_component = _clamp(((attack or 1.25) - 1.25) * 95.0, -90.0, 90.0)
    defensive_edge = (1.25 - (defence or 1.25)) + ((opponent_attack or 1.25) - (opponent_defence or 1.25)) * 0.1
    defence_component = _clamp(defensive_edge * 85.0, -80.0, 80.0)
    recent_form_component = _recent_form_component(recent, side=side)
    market_component = _market_component(market_profiles, side=side)
    raw_elo = BASE_ELO + attack_component + defence_component + recent_form_component + market_component
    decay_factor, decay_warnings = _decay_factor(
        data_quality=str(team.data_quality if team else "missing"),
        coverage_status=str(coverage.get("status") or "missing"),
        evidence_matches=evidence_matches,
    )
    elo = BASE_ELO + ((raw_elo - BASE_ELO) * decay_factor)
    return _RatingBuild(
        elo=elo,
        raw_elo=raw_elo,
        attack_component=attack_component,
        defence_component=defence_component,
        recent_form_component=recent_form_component,
        market_component=market_component,
        decay_factor=decay_factor,
        evidence_matches=evidence_matches,
        warnings=tuple(f"{side}_{warning}" for warning in decay_warnings),
    )


def _recent_form_component(recent: dict[str, Any], *, side: str) -> float:
    scoped = recent.get(side) or {}
    all_scope = recent.get("all") or {}
    form5 = scoped.get("5") or all_scope.get("5") or {}
    form10 = scoped.get("10") or all_scope.get("10") or {}
    score = _float(form5.get("points_per_game"))
    if score is None:
        score = _points_per_game(form5)
    longer_score = _float(form10.get("points_per_game"))
    if longer_score is None:
        longer_score = _points_per_game(form10)
    if score is None and longer_score is None:
        return 0.0
    weighted = score if longer_score is None else (score or longer_score) * 0.7 + longer_score * 0.3
    return _clamp((weighted - 1.4) * 32.0, -45.0, 45.0)


def _points_per_game(form: dict[str, Any]) -> float | None:
    matches = _float(form.get("matches"))
    if not matches:
        return None
    return ((float(form.get("wins") or 0) * 3.0) + float(form.get("draws") or 0)) / matches


def _market_component(market_profiles: dict[str, Any], *, side: str) -> float:
    rows = market_profiles.get("match_result") or []
    target = "Home Win" if side == "home" else "Away Win"
    for row in rows:
        if str(row.get("market") or "").lower() != target.lower():
            continue
        hit_rate = _rate(row.get("hit_rate"))
        confidence = _rate(row.get("confidence")) or 0.35
        if hit_rate is None:
            continue
        return _clamp((hit_rate - 0.38) * 100.0 * confidence, -35.0, 35.0)
    return 0.0


def _decay_factor(*, data_quality: str, coverage_status: str, evidence_matches: int) -> tuple[float, tuple[str, ...]]:
    factor = 1.0
    warnings: list[str] = []
    if data_quality in {"missing", "unavailable", "poor"}:
        factor *= 0.55
        warnings.append("weak_or_missing_quality")
    elif data_quality == "limited":
        factor *= 0.72
        warnings.append("limited_quality")
    if coverage_status in {"missing", "failed"}:
        factor *= 0.65
        warnings.append("coverage_missing")
    elif coverage_status in {"stale", "partial"}:
        factor *= 0.82
        warnings.append("coverage_not_fresh")
    if evidence_matches == 0:
        factor *= 0.62
        warnings.append("no_current_match_evidence")
    elif evidence_matches < 4:
        factor *= 0.78
        warnings.append("promoted_or_low_sample_team")
    return max(0.2, factor), tuple(warnings)


def _league_context(league_payload: dict[str, Any]) -> dict[str, Any]:
    result_rates = _league_result_rates(league_payload.get("market_profiles_by_family") or {})
    home_rate = result_rates.get("home")
    away_rate = result_rates.get("away")
    draw_rate = result_rates.get("draw") or DEFAULT_DRAW_PROBABILITY
    warnings: list[str] = []
    if home_rate is None or away_rate is None:
        home_advantage = DEFAULT_HOME_ADVANTAGE
        warnings.append("league_result_baseline_missing")
    else:
        home_advantage = _home_advantage_from_rates(home_rate, away_rate)
    return {
        "home_advantage": home_advantage,
        "draw_baseline": _clamp(draw_rate, 0.18, 0.32),
        "result_rates": result_rates,
        "warnings": tuple(warnings),
    }


def _league_result_rates(grouped: dict[str, Any]) -> dict[str, float | None]:
    rows = grouped.get("match_result") or []
    rates = {"home": None, "draw": None, "away": None}
    for row in rows:
        market = str(row.get("market") or "").lower()
        hit_rate = _rate(row.get("hit_rate"))
        if hit_rate is None:
            continue
        if "home" in market:
            rates["home"] = hit_rate
        elif "away" in market:
            rates["away"] = hit_rate
        elif "draw" in market:
            rates["draw"] = hit_rate
    return rates


def _home_advantage_from_rates(home_rate: float, away_rate: float) -> float:
    live = home_rate + away_rate
    if live <= 0:
        return DEFAULT_HOME_ADVANTAGE
    home_without_draw = _clamp(home_rate / live, 0.38, 0.68)
    return _clamp(-400.0 * math.log10((1.0 / home_without_draw) - 1.0), 35.0, 95.0)


def _draw_probability(elo_gap: float, draw_baseline: float) -> float:
    gap_penalty = math.exp(-abs(elo_gap) / 520.0)
    return round(_clamp(draw_baseline * (0.58 + 0.42 * gap_penalty), 0.12, 0.33), 6)


def _logistic(elo_gap: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, -elo_gap / 400.0))


def _normalize(home: float, draw: float, away: float) -> tuple[float, float, float]:
    total = home + draw + away
    if total <= 0:
        return 0.0, 0.0, 0.0
    return round(home / total, 6), round(draw / total, 6), round(away / total, 6)


def _result_quality(features: FixtureFeatureSet, home: _RatingBuild, away: _RatingBuild) -> str:
    base = features.diagnostics.data_quality
    if min(home.evidence_matches, away.evidence_matches) == 0:
        return "limited" if base not in {"missing", "unavailable"} else base
    if min(home.decay_factor, away.decay_factor) < 0.5:
        return "limited"
    return base


def _rating_payload(rating: _RatingBuild) -> dict[str, Any]:
    return {
        "elo": round(rating.elo, 2),
        "raw_elo": round(rating.raw_elo, 2),
        "attack_component": round(rating.attack_component, 2),
        "defence_component": round(rating.defence_component, 2),
        "recent_form_component": round(rating.recent_form_component, 2),
        "market_component": round(rating.market_component, 2),
        "decay_factor": round(rating.decay_factor, 4),
        "evidence_matches": rating.evidence_matches,
    }


def _rate(value) -> float | None:
    number = _float(value)
    if number is None:
        return None
    return number / 100.0 if number > 1 else number


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return min(ceiling, max(floor, value))
