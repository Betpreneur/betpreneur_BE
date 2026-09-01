"""Poisson goal model.

The goal model estimates expected goals and a scoreline distribution from the
shared feature set. It does not use odds or recommendation policy.
"""
from __future__ import annotations

from typing import Any

from betpreneur.modules.scoring.api import build_score_matrix

from .contracts import FixtureFeatureSet, GoalModelOutput, PredictionDiagnostics

MODEL_VERSION = "poisson-goals-v1"
DEFAULT_HOME_GOALS = 1.35
DEFAULT_AWAY_GOALS = 1.1


def goal_distribution(features: FixtureFeatureSet) -> GoalModelOutput:
    """Return scoreline and goal-market probabilities for one fixture."""
    payload = features.features or {}
    home_expected, away_expected, warnings = _expected_goals(features)
    matrix = build_score_matrix(home_expected, away_expected)
    scorelines = _scoreline_payload(matrix)
    result_probabilities = _result_probabilities(matrix)
    team_goals = {
        "home": _team_goal_probabilities(matrix.home_goal_distribution()),
        "away": _team_goal_probabilities(matrix.away_goal_distribution()),
    }
    data_quality = _goal_quality(features, payload)

    return GoalModelOutput(
        home_expected_goals=home_expected,
        away_expected_goals=away_expected,
        scoreline_matrix=scorelines,
        over_1_5_probability=_total_goals_probability(matrix, 1.5, side="over"),
        over_2_5_probability=_total_goals_probability(matrix, 2.5, side="over"),
        under_3_5_probability=_total_goals_probability(matrix, 3.5, side="under"),
        btts_probability=round(matrix.sum_where(lambda home, away: home > 0 and away > 0), 6),
        team_goal_probabilities=team_goals,
        result_probabilities=result_probabilities,
        diagnostics=PredictionDiagnostics(
            data_quality=data_quality,
            model_version=MODEL_VERSION,
            model_sources=("prediction.poisson",),
            warnings=tuple(dict.fromkeys((*features.diagnostics.warnings, *warnings))),
            metadata={
                "expected_total_goals": round(home_expected + away_expected, 4),
                "inputs": _input_summary(features, payload),
                "result_probabilities_from_score_matrix": result_probabilities,
            },
        ),
    )


def _expected_goals(features: FixtureFeatureSet) -> tuple[float, float, tuple[str, ...]]:
    payload = features.features or {}
    goal_model = payload.get("goal_model") or {}
    league = payload.get("league") or {}
    scoring_environment = league.get("scoring_environment") or {}
    warnings: list[str] = []

    model_home = _float(goal_model.get("home_expected_goals"))
    model_away = _float(goal_model.get("away_expected_goals"))
    model_usable = bool(goal_model.get("usable")) or goal_model.get("data_quality") not in {"poor", "missing", None}
    baseline_home = _float(scoring_environment.get("home_goal_baseline")) or _float(goal_model.get("home_baseline"))
    baseline_away = _float(scoring_environment.get("away_goal_baseline")) or _float(goal_model.get("away_baseline"))
    baseline_home = baseline_home or DEFAULT_HOME_GOALS
    baseline_away = baseline_away or DEFAULT_AWAY_GOALS

    feature_home = _feature_expected_goals(features, "home", baseline_home)
    feature_away = _feature_expected_goals(features, "away", baseline_away)
    if model_home is None or model_away is None:
        warnings.append("score_model_expected_goals_missing")
        model_usable = False

    if model_usable:
        home_expected = (model_home * 0.72) + (feature_home * 0.28)
        away_expected = (model_away * 0.72) + (feature_away * 0.28)
    else:
        warnings.append("using_feature_derived_expected_goals")
        home_expected = feature_home
        away_expected = feature_away

    home_expected = _recent_goal_adjustment(payload.get("home") or {}, home_expected, side="home")
    away_expected = _recent_goal_adjustment(payload.get("away") or {}, away_expected, side="away")
    home_expected = _feedback_goal_adjustment(payload, "home", home_expected)
    away_expected = _feedback_goal_adjustment(payload, "away", away_expected)
    return round(_clamp(home_expected, 0.15, 5.0), 4), round(_clamp(away_expected, 0.15, 5.0), 4), tuple(warnings)


def _feature_expected_goals(features: FixtureFeatureSet, side: str, league_baseline: float) -> float:
    team = features.home_team if side == "home" else features.away_team
    opponent = features.away_team if side == "home" else features.home_team
    attack = _float(team.attack_rating if team else None) or league_baseline
    opponent_defence = _float(opponent.defence_rating if opponent else None) or league_baseline
    attack_factor = _clamp(attack / max(league_baseline, 0.1), 0.55, 1.65)
    defence_factor = _clamp(opponent_defence / max(league_baseline, 0.1), 0.55, 1.65)
    return league_baseline * attack_factor * defence_factor


def _recent_goal_adjustment(side_payload: dict[str, Any], expected_goals: float, *, side: str) -> float:
    recent = side_payload.get("recent_form") or {}
    scoped = recent.get(side) or {}
    all_scope = recent.get("all") or {}
    form5 = scoped.get("5") or all_scope.get("5") or {}
    recent_goals = _float(form5.get("goals_for_per_match"))
    if recent_goals is None:
        recent_goals = _per_match(form5.get("goals_for"), form5.get("matches"))
    if recent_goals is None:
        return expected_goals
    return (expected_goals * 0.82) + (_clamp(recent_goals, 0.1, 4.5) * 0.18)


def _feedback_goal_adjustment(payload: dict[str, Any], side: str, expected_goals: float) -> float:
    feedback = ((payload.get("prediction_feedback") or {}).get(side) or {}).get("summary") or {}
    matches = _float(feedback.get("matches")) or 0.0
    if matches < 2:
        return expected_goals
    feedback_goals = _float(feedback.get("avg_goals_for"))
    if feedback_goals is None:
        return expected_goals
    weight = min(0.12, matches / 80.0)
    return (expected_goals * (1.0 - weight)) + (_clamp(feedback_goals, 0.1, 4.5) * weight)


def _scoreline_payload(matrix) -> dict[str, float]:
    scorelines: dict[str, float] = {}
    for home_goals, row in enumerate(matrix.grid):
        for away_goals, probability in enumerate(row):
            scorelines[f"{home_goals}-{away_goals}"] = round(probability, 8)
    return scorelines


def _result_probabilities(matrix) -> dict[str, float]:
    return {
        "home_win": round(matrix.sum_where(lambda home, away: home > away), 6),
        "draw": round(matrix.sum_where(lambda home, away: home == away), 6),
        "away_win": round(matrix.sum_where(lambda home, away: home < away), 6),
    }


def _total_goals_probability(matrix, line: float, *, side: str) -> float:
    if side == "under":
        return round(matrix.sum_where(lambda home, away: home + away < line), 6)
    return round(matrix.sum_where(lambda home, away: home + away > line), 6)


def _team_goal_probabilities(distribution) -> dict[str, float]:
    return {
        "over_0_5": round(sum(distribution[1:]), 6),
        "over_1_5": round(sum(distribution[2:]), 6),
        "over_2_5": round(sum(distribution[3:]), 6),
        "under_1_5": round(sum(distribution[:2]), 6),
        "under_2_5": round(sum(distribution[:3]), 6),
        "clean_sheet_against": round(distribution[0], 6),
    }


def _goal_quality(features: FixtureFeatureSet, payload: dict[str, Any]) -> str:
    goal_model = payload.get("goal_model") or {}
    quality = str(goal_model.get("data_quality") or features.diagnostics.data_quality or "missing")
    if quality == "poor" and features.diagnostics.data_quality not in {"missing", "unavailable"}:
        return "limited"
    return quality


def _input_summary(features: FixtureFeatureSet, payload: dict[str, Any]) -> dict[str, Any]:
    goal_model = payload.get("goal_model") or {}
    league = payload.get("league") or {}
    scoring_environment = league.get("scoring_environment") or {}
    return {
        "home_attack": features.home_team.attack_rating if features.home_team else None,
        "away_attack": features.away_team.attack_rating if features.away_team else None,
        "home_defence": features.home_team.defence_rating if features.home_team else None,
        "away_defence": features.away_team.defence_rating if features.away_team else None,
        "league_home_goal_baseline": scoring_environment.get("home_goal_baseline"),
        "league_away_goal_baseline": scoring_environment.get("away_goal_baseline"),
        "score_model_quality": goal_model.get("data_quality"),
        "prediction_feedback": payload.get("prediction_feedback"),
    }


def _per_match(total, matches) -> float | None:
    total_value = _float(total)
    match_count = _float(matches)
    if total_value is None or not match_count:
        return None
    return total_value / match_count


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return min(ceiling, max(floor, value))
