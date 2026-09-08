"""Market probability dispatch."""

from __future__ import annotations

from functools import cache
from typing import Any

from betpreneur.modules.markets.api import MarketDescriptor, describe_market

from .calibration import calibrate_probability
from .contracts import FixturePrediction, MarketProbability, PredictionDiagnostics

RESULT_FAMILIES = {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}
GOAL_FAMILIES = {"total_goals", "team_total_goals", "btts", "correct_score"}
COUNT_FAMILIES = {
    "corners_total": "corners",
    "team_corners": "corners",
    "cards_total": "cards",
    "team_cards": "cards",
    "booking_points": "cards",
    "shots_on_target_total": "sot",
    "team_shots_on_target": "sot",
}

GOAL_LINE_BUFFER = {
    "total_goals": 0.45,
    "team_total_goals": 0.35,
}
CORNER_LINE_BUFFER = {
    "corners_total": 1.0,
    "team_corners": 0.75,
}
GERMAN_UNDER_GOALS_LEAGUE_TERMS = (
    "bundesliga",
    "2. bundesliga",
    "bundesliga 2",
    "germany-bundesliga",
    "germany-2-bundesliga",
)


def evaluate_market_probability(
    fixture_prediction: FixturePrediction, market: str
) -> MarketProbability:
    """Evaluate one market probability from product-neutral model outputs."""
    descriptor = describe_market(market)
    probability, model, facts, warnings, quality = _probability_for_descriptor(
        fixture_prediction, descriptor
    )
    calibration = calibrate_probability(
        probability,
        market=descriptor.canonical or market,
        context={
            "descriptor": descriptor,
            "fixture_prediction": fixture_prediction,
            "data_quality": quality,
        },
    )
    confidence = _confidence_score(calibration.calibrated_probability, quality)
    combined_warnings = tuple(dict.fromkeys([*warnings, *calibration.diagnostics.warnings]))
    return MarketProbability(
        fixture_id=fixture_prediction.fixture_id,
        market=descriptor.canonical or market,
        raw_probability=probability,
        calibrated_probability=calibration.calibrated_probability,
        confidence_score=confidence,
        fair_odds=None,
        model=model,
        data_quality=quality,
        model_sources=(model,) if model else ("prediction.market_probabilities",),
        warnings=combined_warnings,
        explanation_facts=tuple(facts),
        supporting_facts=tuple(facts),
        diagnostics=PredictionDiagnostics(
            data_quality=quality,
            model_sources=("prediction.market_probabilities", model)
            if model
            else ("prediction.market_probabilities",),
            warnings=combined_warnings,
            metadata={
                "market_family": descriptor.family,
                "recognized": descriptor.recognized,
                "market_code": descriptor.code,
                "early_payout": descriptor.early_payout,
                "calibration_method": calibration.method,
                "calibration_penalty": calibration.calibration_penalty,
                "calibration_sample_count": calibration.diagnostics.metadata.get("sample_count"),
                "calibration_scope": calibration.diagnostics.metadata.get("scope"),
            },
        ),
    )


def _probability_for_descriptor(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> tuple[float | None, str, list[str], list[str], str]:
    if not descriptor.recognized:
        return None, "", [], ["market_not_recognized"], "unavailable"
    if descriptor.family in RESULT_FAMILIES:
        return _result_probability(prediction, descriptor)
    if descriptor.family in GOAL_FAMILIES:
        return _goal_probability(prediction, descriptor)
    if descriptor.family in COUNT_FAMILIES:
        return _count_probability(prediction, descriptor)
    return None, "", [], [f"market_family_not_supported:{descriptor.family}"], "unavailable"


def _result_probability(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> tuple[float | None, str, list[str], list[str], str]:
    goals = prediction.goals
    result = prediction.result
    quality = _quality(result, fallback=_quality(goals, fallback="unavailable"))
    warnings = list(getattr(getattr(result, "diagnostics", None), "warnings", ()) or ())

    if descriptor.early_payout:
        probability = _early_payout_probability(goals, descriptor)
        model = "poisson_goals"
        facts = _goal_facts(
            prediction,
            goals,
            descriptor,
            prefix=f"{descriptor.early_payout} early payout result model",
        )
        return probability, model, facts, warnings, _quality(goals, fallback=quality)

    if descriptor.family == "match_result":
        key = {"home": "home_win", "draw": "draw", "away": "away_win"}.get(descriptor.side)
        probability = getattr(result, key, None) if key and result else None
        facts = _result_facts(result)
        return probability, "elo_result", facts, warnings, quality

    probability = _elo_result_probability(result, descriptor)
    if probability is not None:
        return (
            _round_probability(probability),
            "elo_result",
            _result_facts(result),
            warnings,
            quality,
        )

    matrix = _matrix(goals)
    if not matrix:
        return None, "poisson_goals", [], [*warnings, "scoreline_matrix_missing"], _quality(goals)
    home = _sum_matrix(matrix, lambda h, a: h > a)
    draw = _sum_matrix(matrix, lambda h, a: h == a)
    away = _sum_matrix(matrix, lambda h, a: h < a)
    if descriptor.family == "double_chance":
        probability = {
            "home_or_draw": home + draw,
            "1x": home + draw,
            "draw_or_away": draw + away,
            "x2": draw + away,
            "home_or_away": home + away,
            "12": home + away,
        }.get(descriptor.side.lower())
    elif descriptor.family == "draw_no_bet":
        win = home if descriptor.side == "home" else away
        probability = win / max(1.0 - draw, 1e-9)
    else:
        probability = _asian_handicap_probability(matrix, descriptor)
    return (
        _round_probability(probability),
        "poisson_goals",
        [
            *_scoreline_result_facts(home=home, draw=draw, away=away),
            *_goal_facts(prediction, goals, descriptor),
        ],
        warnings,
        _quality(goals),
    )


def _elo_result_probability(result, descriptor: MarketDescriptor) -> float | None:
    if result is None:
        return None
    home = result.home_result_probability
    draw = result.draw_probability
    away = result.away_result_probability
    if home is None or draw is None or away is None:
        return None
    if descriptor.family == "double_chance":
        return {
            "home_or_draw": home + draw,
            "1x": home + draw,
            "draw_or_away": draw + away,
            "x2": draw + away,
            "home_or_away": home + away,
            "12": home + away,
        }.get(str(descriptor.side or "").lower())
    if descriptor.family == "draw_no_bet":
        win = home if descriptor.side == "home" else away if descriptor.side == "away" else None
        return None if win is None else win / max(1.0 - draw, 1e-9)
    if descriptor.family in {"asian_handicap", "handicap"}:
        return _elo_handicap_probability(home, draw, away, descriptor)
    return None


def _elo_handicap_probability(
    home: float,
    draw: float,
    away: float,
    descriptor: MarketDescriptor,
) -> float | None:
    line = _signed_handicap_line(descriptor)
    side = descriptor.side or descriptor.team
    if line is None or side not in {"home", "away"}:
        return None
    win = home if side == "home" else away
    if line == 0:
        return win / max(1.0 - draw, 1e-9)
    if line == 0.5:
        return win + draw
    if line == -0.5:
        return win
    return None


def _signed_handicap_line(descriptor: MarketDescriptor) -> float | None:
    line = _float(descriptor.line)
    if line is None:
        return None
    text = f"{descriptor.raw or ''} {descriptor.canonical or ''}".lower()
    compact = text.replace(" ", "")
    magnitude = f"{abs(line):g}"
    if f"-{magnitude}" in compact or f"-{abs(line):.1f}" in compact:
        return -abs(line)
    return line


def _scoreline_result_facts(*, home: float, draw: float, away: float) -> list[str]:
    return [
        f"Home win probability: {_percent(home)}%.",
        f"Draw probability: {_percent(draw)}%.",
        f"Away win probability: {_percent(away)}%.",
        "Result probabilities are derived from the scoreline distribution.",
    ]


def _goal_probability(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> tuple[float | None, str, list[str], list[str], str]:
    goals = prediction.goals
    warnings = list(getattr(getattr(goals, "diagnostics", None), "warnings", ()) or [])
    if goals is None:
        return None, "poisson_goals", [], ["goal_model_missing"], "unavailable"

    probability = None
    if descriptor.family == "total_goals":
        probability = _scoreline_total_probability(goals, descriptor)
    elif descriptor.family == "team_total_goals":
        probability = _team_goal_probability(goals, descriptor)
    elif descriptor.family == "btts":
        probability = goals.btts_probability
        if descriptor.side == "no":
            probability = None if probability is None else 1.0 - probability
    elif descriptor.family == "correct_score":
        probability = goals.scoreline_matrix.get(
            str(descriptor.selection or descriptor.raw).strip()
        )
    if _is_german_under_goals_market(prediction, descriptor):
        warnings.append("german_under_goals_market_blocked")
    warnings.extend(
        _line_boundary_warnings(
            descriptor,
            _goal_expected_for_descriptor(goals, descriptor),
            unit="goals",
        )
    )
    probability, api_facts, api_warnings = _apply_api_football_goal_context(
        probability,
        prediction,
        descriptor,
    )
    warnings.extend(api_warnings)
    return (
        _round_probability(probability),
        "poisson_goals",
        [*_goal_facts(prediction, goals, descriptor), *api_facts],
        warnings,
        _quality(goals),
    )


def _count_probability(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> tuple[float | None, str, list[str], list[str], str]:
    counts = prediction.counts
    if counts is None:
        return None, "poisson_counts", [], ["count_model_missing"], "unavailable"
    event = COUNT_FAMILIES[descriptor.family]
    side = (descriptor.side or "over").lower()
    line = _line_key(descriptor.line)
    key = f"{side}_{line}" if line else ""
    if descriptor.family.startswith("team_"):
        team = descriptor.team or "home"
        probability = counts.team_line_probabilities.get(event, {}).get(team, {}).get(key)
    else:
        probability = counts.line_probabilities.get(event, {}).get(key)
    facts = _count_facts(prediction, counts, event, descriptor)
    warnings = list(counts.diagnostics.warnings)
    if event == "corners":
        sources = ((counts.diagnostics.metadata or {}).get("sources") or {}).get("corners") or ()
        if "api_football_corner_samples" not in sources:
            warnings.append("api_football_corner_samples_missing")
        warnings.extend(
            _line_boundary_warnings(
                descriptor,
                _count_expected_for_descriptor(counts, event, descriptor),
                unit="corners",
            )
        )
    return probability, "poisson_counts", facts, warnings, counts.diagnostics.data_quality


def _is_german_under_goals_market(prediction: FixturePrediction, descriptor: MarketDescriptor) -> bool:
    if descriptor.family not in {"total_goals", "team_total_goals"}:
        return False
    if str(descriptor.side or "").lower() != "under":
        return False
    haystack = " ".join(
        str(value or "").lower()
        for value in (
            getattr(getattr(prediction, "features", None), "league_key", ""),
            (((getattr(getattr(prediction, "features", None), "features", {}) or {}).get("league") or {}).get("league_key")),
            (((getattr(getattr(prediction, "features", None), "features", {}) or {}).get("league") or {}).get("league_name")),
            (((getattr(getattr(prediction, "features", None), "features", {}) or {}).get("fixture") or {}).get("league")),
            (((getattr(getattr(prediction, "features", None), "features", {}) or {}).get("fixture") or {}).get("country")),
        )
    )
    if "austria" in haystack:
        return False
    return any(term in haystack for term in GERMAN_UNDER_GOALS_LEAGUE_TERMS)


def _goal_expected_for_descriptor(goals, descriptor: MarketDescriptor) -> float | None:
    if goals is None:
        return None
    if descriptor.family == "team_total_goals":
        if descriptor.team == "home":
            return _float(goals.home_expected_goals)
        if descriptor.team == "away":
            return _float(goals.away_expected_goals)
        return None
    home = _float(goals.home_expected_goals)
    away = _float(goals.away_expected_goals)
    if home is None or away is None:
        return None
    return home + away


def _count_expected_for_descriptor(counts, event: str, descriptor: MarketDescriptor) -> float | None:
    if counts is None:
        return None
    if descriptor.family.startswith("team_"):
        return _float(counts.expected_team_counts.get(event, {}).get(descriptor.team or "home"))
    field = {
        "corners": "expected_total_corners",
        "cards": "expected_total_cards",
        "sot": "expected_total_sot",
    }.get(event)
    return _float(getattr(counts, field, None)) if field else None


def _line_boundary_warnings(
    descriptor: MarketDescriptor,
    expected: float | None,
    *,
    unit: str,
) -> list[str]:
    line = _float(descriptor.line)
    side = str(descriptor.side or "").lower()
    if expected is None or line is None or side not in {"over", "under"}:
        return []

    if side == "over":
        margin = expected - line
    else:
        margin = line - expected

    if unit == "goals":
        buffer = GOAL_LINE_BUFFER.get(descriptor.family, 0.45)
        flags = ["goal_line_boundary"] if margin < buffer else []
        if side == "under":
            if line <= 2.5 and expected >= 2.0:
                flags.append("under25_goal_volatility")
            elif line <= 3.5 and expected >= 2.75:
                flags.append("under35_blowout_risk")
            elif line <= 4.5 and expected >= 3.2:
                flags.append("under45_high_goal_volatility")
        return list(dict.fromkeys(flags))

    if unit == "corners":
        buffer = CORNER_LINE_BUFFER.get(descriptor.family, 1.0)
        flags = ["corner_line_boundary"] if margin < buffer else []
        if side == "under" and expected >= line - 1.25:
            flags.append("corner_under_pressure_risk")
        if side == "over" and expected <= line + 1.25:
            flags.append("corner_over_margin_risk")
        return list(dict.fromkeys(flags))

    return []


def _scoreline_total_probability(goals, descriptor: MarketDescriptor) -> float | None:
    matrix = _matrix(goals)
    line = _float(descriptor.line)
    if line is None:
        return None
    if descriptor.side == "over" and line == 1.5 and goals.over_1_5_probability is not None:
        return goals.over_1_5_probability
    if descriptor.side == "over" and line == 2.5 and goals.over_2_5_probability is not None:
        return goals.over_2_5_probability
    if descriptor.side == "under" and line == 3.5 and goals.under_3_5_probability is not None:
        return goals.under_3_5_probability
    if not matrix:
        return None
    if descriptor.side == "under":
        return _sum_matrix(matrix, lambda h, a: h + a < line)
    return _sum_matrix(matrix, lambda h, a: h + a > line)


def _team_goal_probability(goals, descriptor: MarketDescriptor) -> float | None:
    line = _line_key(descriptor.line)
    side = descriptor.side or "over"
    team = descriptor.team or "home"
    key = f"{side}_{line}" if line else ""
    return goals.team_goal_probabilities.get(team, {}).get(key)


def _asian_handicap_probability(
    matrix: dict[tuple[int, int], float], descriptor: MarketDescriptor
) -> float | None:
    line = _float(descriptor.line)
    if line is None:
        return None
    team = descriptor.side or descriptor.team or "home"
    win = push = 0.0
    for (home, away), mass in matrix.items():
        margin = home - away if team == "home" else away - home
        adjusted = margin + line
        if abs(adjusted) < 1e-12:
            push += mass
        elif adjusted > 0:
            win += mass
    return win / max(1.0 - push, 1e-9)


def _early_payout_probability(goals, descriptor: MarketDescriptor) -> float | None:
    matrix = _matrix(goals)
    if not matrix:
        return None
    lead = 2 if descriptor.early_payout == "2UP" else 1
    side = descriptor.side.lower()
    if descriptor.family == "match_result":
        if side == "draw":
            return _sum_matrix(matrix, lambda h, a: h == a)
        return _early_result_probability(matrix, side=side, lead=lead)
    if descriptor.family == "double_chance":
        return _early_double_chance_probability(matrix, side=side, lead=lead)
    return None


def _early_result_probability(
    matrix: dict[tuple[int, int], float], *, side: str, lead: int
) -> float:
    total = 0.0
    for (home, away), mass in matrix.items():
        final_wins = home > away if side == "home" else away > home
        if final_wins:
            total += mass
        else:
            total += mass * _lead_hit_probability(home, away, team=side, lead=lead)
    return _round_probability(total) or 0.0


def _early_double_chance_probability(
    matrix: dict[tuple[int, int], float], *, side: str, lead: int
) -> float:
    protected = {
        "home_or_draw": ("home",),
        "1x": ("home",),
        "draw_or_away": ("away",),
        "x2": ("away",),
        "home_or_away": ("home", "away"),
        "12": ("home", "away"),
    }.get(side, ())
    total = 0.0
    for (home, away), mass in matrix.items():
        if _double_chance_final_match(home, away, side):
            total += mass
            continue
        no_hit = 1.0
        for team in protected:
            no_hit *= 1.0 - _lead_hit_probability(home, away, team=team, lead=lead)
        total += mass * (1.0 - no_hit)
    return _round_probability(total) or 0.0


def _double_chance_final_match(home: int, away: int, side: str) -> bool:
    if side in {"home_or_draw", "1x"}:
        return home >= away
    if side in {"draw_or_away", "x2"}:
        return home <= away
    if side in {"home_or_away", "12"}:
        return home != away
    return False


@cache
def _lead_hit_probability(home_goals: int, away_goals: int, *, team: str, lead: int) -> float:
    if lead <= 0:
        return 1.0
    if team not in {"home", "away"}:
        return 0.0

    @cache
    def walk(home_remaining: int, away_remaining: int, current_diff: int) -> tuple[int, int]:
        if (team == "home" and current_diff >= lead) or (team == "away" and -current_diff >= lead):
            return 1, 1
        if home_remaining == 0 and away_remaining == 0:
            return 0, 1
        hit = total = 0
        if home_remaining:
            child_hit, child_total = walk(home_remaining - 1, away_remaining, current_diff + 1)
            hit += child_hit
            total += child_total
        if away_remaining:
            child_hit, child_total = walk(home_remaining, away_remaining - 1, current_diff - 1)
            hit += child_hit
            total += child_total
        return hit, total

    hit, total = walk(int(home_goals), int(away_goals), 0)
    return hit / total if total else 0.0


def _goal_facts(
    prediction: FixturePrediction,
    goals,
    descriptor: MarketDescriptor,
    *,
    prefix: str = "Poisson goal model",
) -> list[str]:
    if goals is None:
        return []
    total = None
    if goals.home_expected_goals is not None and goals.away_expected_goals is not None:
        total = goals.home_expected_goals + goals.away_expected_goals
    facts = [
        f"Projected total goals: {total:.2f}."
        if total is not None
        else f"{prefix}: expected goals unavailable.",
    ]
    if goals.home_expected_goals is not None:
        facts.append(f"Home average: {goals.home_expected_goals:.2f} xG.")
    if goals.away_expected_goals is not None:
        facts.append(f"Away average: {goals.away_expected_goals:.2f} xG.")
    if prefix != "Poisson goal model":
        facts.append(f"{prefix} used the scoreline distribution.")
    line = _float(descriptor.line)
    line_expected = _goal_expected_for_descriptor(goals, descriptor)
    if line is not None and line_expected is not None and descriptor.side in {"over", "under"}:
        direction = "below" if line < line_expected else "above"
        if descriptor.family == "team_total_goals":
            team_label = "home" if descriptor.team == "home" else "away" if descriptor.team == "away" else "team"
            facts.append(
                f"Line {line:g} is {direction} the {team_label} team projection of {line_expected:.2f} goals."
            )
        else:
            facts.append(f"Line {line:g} is {direction} the model projection of {line_expected:.2f} goals.")
    facts.extend(_recent_scoreline_facts(prediction, descriptor))
    facts.extend(_team_market_profile_facts(prediction, descriptor))
    facts.extend(_league_market_profile_facts(prediction, descriptor))
    return facts


def _recent_scoreline_facts(prediction: FixturePrediction, descriptor: MarketDescriptor) -> list[str]:
    feature_set = getattr(prediction, "features", None)
    feature_payload = getattr(feature_set, "features", None) or {}
    scoreline_profile = feature_payload.get("scoreline_profile") or {}
    profile = scoreline_profile.get("combined") or {}
    games = int(_float(profile.get("games")) or 0)
    if games < 4:
        return []
    facts = []
    facts.extend(_scoreline_evidence_bullets(scoreline_profile))
    avg_total = _float(profile.get("avg_total_goals"))
    over_25 = _float(profile.get("over_2_5_rate"))
    over_35 = _float(profile.get("over_3_5_rate"))
    btts = _float(profile.get("btts_rate"))
    if avg_total is not None:
        facts.append(f"Recent scorelines average {avg_total:.2f} total goals across {games} tracked matches.")
    if descriptor.family in {"total_goals", "btts"}:
        if over_25 is not None:
            facts.append(f"Recent scoreline Over 2.5 rate: {over_25:.1f}%.")
        if over_35 is not None:
            facts.append(f"Recent scoreline Over 3.5 rate: {over_35:.1f}%.")
        if btts is not None:
            facts.append(f"Recent scoreline BTTS rate: {btts:.1f}%.")
    return facts


def _apply_api_football_goal_context(
    probability: float | None,
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> tuple[float | None, list[str], list[str]]:
    if probability is None or descriptor.family not in {"total_goals", "team_total_goals", "btts"}:
        return probability, [], []
    feature_payload = getattr(getattr(prediction, "features", None), "features", None) or {}
    api = feature_payload.get("api_football") if isinstance(feature_payload.get("api_football"), dict) else {}
    if not api.get("available"):
        return probability, [], []

    facts: list[str] = []
    warnings: list[str] = []
    adjusted = float(probability)

    recent_adjustment = _api_recent_scoreline_probability(api, descriptor)
    if recent_adjustment is not None:
        recent_probability, games, label = recent_adjustment
        adjusted = (adjusted * 0.78) + (recent_probability * 0.22)
        facts.append(f"API-Football recent scorelines support {label} at {_percent(recent_probability)}% across {games} games.")
        if abs(recent_probability - probability) >= 0.18:
            warnings.append("api_football_recent_scorelines_disagree")

    opinion_adjustment = _api_prediction_opinion_adjustment(api, descriptor)
    if opinion_adjustment is not None:
        direction, label = opinion_adjustment
        if direction == "agree":
            adjusted += 0.025
            facts.append(f"API-Football prediction opinion agrees with {label}.")
        elif direction == "disagree":
            adjusted -= 0.05
            warnings.append("api_football_prediction_opinion_disagrees")
            facts.append(f"API-Football prediction opinion does not support {label}.")

    team_stats_adjustment = _api_team_statistics_goal_adjustment(api, descriptor)
    if team_stats_adjustment is not None:
        team_probability, label = team_stats_adjustment
        adjusted = (adjusted * 0.88) + (team_probability * 0.12)
        facts.append(f"API-Football team statistics support {label} at {_percent(team_probability)}%.")
        if abs(team_probability - probability) >= 0.22:
            warnings.append("api_football_team_statistics_disagree")

    return _clamp(adjusted, 0.01, 0.99), facts, list(dict.fromkeys(warnings))


def _api_recent_scoreline_probability(api: dict[str, Any], descriptor: MarketDescriptor) -> tuple[float, int, str] | None:
    profile = ((api.get("recent_scorelines") or {}).get("combined") or {})
    games = int(_float(profile.get("games")) or 0)
    if games < 6:
        return None
    if descriptor.family == "btts":
        rate = _float(profile.get("btts_rate"))
        if rate is None:
            return None
        probability = rate / 100.0
        if descriptor.side == "no":
            probability = 1.0 - probability
        return _clamp(probability, 0.01, 0.99), games, descriptor.canonical or "BTTS"
    if descriptor.family != "total_goals":
        return None
    line = _float(descriptor.line)
    side = str(descriptor.side or "").lower()
    if line is None or side not in {"over", "under"}:
        return None
    key = "over_2_5_rate" if line <= 2.5 else "over_3_5_rate" if line <= 3.5 else "over_4_5_rate"
    rate = _float(profile.get(key))
    if rate is None:
        return None
    probability = rate / 100.0
    if side == "under":
        probability = 1.0 - probability
    return _clamp(probability, 0.01, 0.99), games, descriptor.canonical or f"{side.title()} {line:g}"


def _api_prediction_opinion_adjustment(api: dict[str, Any], descriptor: MarketDescriptor) -> tuple[str, str] | None:
    opinion = api.get("prediction_opinion") if isinstance(api.get("prediction_opinion"), dict) else {}
    if not opinion.get("available"):
        return None
    label = descriptor.canonical or descriptor.raw or ""
    if descriptor.family == "total_goals":
        opinion_line = _api_under_over_line(opinion.get("under_over"))
        line = _float(descriptor.line)
        side = str(descriptor.side or "").lower()
        if opinion_line is None or line is None or side not in {"over", "under"}:
            return None
        opinion_side, opinion_value = opinion_line
        if abs(opinion_value - line) > 1.0:
            return None
        return ("agree" if opinion_side == side else "disagree"), label
    if descriptor.family == "team_total_goals":
        team_goals = opinion.get("team_goals") if isinstance(opinion.get("team_goals"), dict) else {}
        raw = team_goals.get(descriptor.team or "")
        opinion_line = _api_under_over_line(raw)
        line = _float(descriptor.line)
        side = str(descriptor.side or "").lower()
        if opinion_line is None or line is None or side not in {"over", "under"}:
            return None
        opinion_side, opinion_value = opinion_line
        if abs(opinion_value - line) > 1.0:
            return None
        return ("agree" if opinion_side == side else "disagree"), label
    return None


def _api_under_over_line(value) -> tuple[str, float] | None:
    text = str(value or "").strip()
    if not text:
        return None
    side = "over" if text.startswith("+") else "under" if text.startswith("-") else ""
    if not side:
        return None
    line = _float(text[1:])
    if line is None:
        return None
    return side, line


def _api_team_statistics_goal_adjustment(api: dict[str, Any], descriptor: MarketDescriptor) -> tuple[float, str] | None:
    if descriptor.family not in {"total_goals", "team_total_goals"}:
        return None
    team_stats = api.get("team_statistics") if isinstance(api.get("team_statistics"), dict) else {}
    home = team_stats.get("home") if isinstance(team_stats.get("home"), dict) else {}
    away = team_stats.get("away") if isinstance(team_stats.get("away"), dict) else {}
    if not home.get("available") or not away.get("available"):
        return None
    side = str(descriptor.side or "").lower()
    line = _float(descriptor.line)
    if line is None or side not in {"over", "under"}:
        return None
    if descriptor.family == "team_total_goals":
        bucket = home if descriptor.team == "home" else away if descriptor.team == "away" else {}
        avg = _float((((bucket.get("goals_for") or {}).get("average") or {}).get("total")))
        if avg is None:
            return None
        estimate = _clamp((avg - (line - 0.5)) / 1.8, 0.05, 0.95)
        if side == "under":
            estimate = 1.0 - estimate
        return estimate, descriptor.canonical or "team goals"
    home_for = _float((((home.get("goals_for") or {}).get("average") or {}).get("total")))
    away_for = _float((((away.get("goals_for") or {}).get("average") or {}).get("total")))
    if home_for is None or away_for is None:
        return None
    total_avg = home_for + away_for
    estimate = _clamp((total_avg - (line - 0.5)) / 2.6, 0.05, 0.95)
    if side == "under":
        estimate = 1.0 - estimate
    return estimate, descriptor.canonical or "total goals"


def _scoreline_evidence_bullets(scoreline_profile: dict[str, Any]) -> list[str]:
    buckets = (
        ("home_recent", "Home recent scorelines", 3),
        ("away_recent", "Away recent scorelines", 3),
        ("head_to_head", "Head-to-head scorelines", 2),
    )
    bullets = []
    for bucket, label, limit in buckets:
        values = _scoreline_labels_for_bucket(scoreline_profile, bucket, limit=limit)
        if values:
            bullets.append(f"{label}: {'; '.join(values)}.")
    if bullets:
        return bullets
    values = _balanced_scoreline_labels(scoreline_profile)
    return [f"Tracked scorelines used: {'; '.join(values)}."] if values else []


def _scoreline_labels_for_bucket(scoreline_profile: dict[str, Any], bucket: str, *, limit: int) -> list[str]:
    profile = scoreline_profile.get(bucket) if isinstance(scoreline_profile.get(bucket), dict) else {}
    labels = []
    seen = set()
    for row in (profile.get("scorelines") or [])[:limit]:
        if not isinstance(row, dict):
            continue
        label = _traceable_scoreline_label(row)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _balanced_scoreline_labels(scoreline_profile: dict[str, Any], limit: int = 6) -> list[str]:
    buckets = (
        ("home_recent", 3),
        ("away_recent", 3),
        ("head_to_head", 2),
    )
    labels: list[str] = []
    seen = set()
    for bucket, bucket_limit in buckets:
        profile = scoreline_profile.get(bucket) if isinstance(scoreline_profile.get(bucket), dict) else {}
        for row in (profile.get("scorelines") or [])[:bucket_limit]:
            if not isinstance(row, dict):
                continue
            label = _traceable_scoreline_label(row)
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= limit:
                return labels
    if labels:
        return labels
    combined = scoreline_profile.get("combined") if isinstance(scoreline_profile.get("combined"), dict) else {}
    for row in (combined.get("scorelines") or [])[:limit]:
        if not isinstance(row, dict):
            continue
        label = _traceable_scoreline_label(row)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _traceable_scoreline_label(row: dict[str, Any]) -> str:
    scoreline = str(row.get("scoreline") or "").strip()
    if not scoreline:
        return ""
    source = str(row.get("source") or "").strip()
    fixture = str(row.get("fixture") or "").strip()
    team = str(row.get("team_name") or "").strip()
    opponent = str(row.get("opponent_name") or row.get("opponent") or "").strip()
    if source == "head_to_head":
        return f"H2H: {fixture} {scoreline}" if fixture else f"H2H {scoreline}"
    if team and opponent:
        return f"{team} {scoreline} {opponent}"
    if fixture:
        return f"{fixture} {scoreline}"
    return scoreline


def _result_facts(result) -> list[str]:
    if result is None:
        return []
    facts = []
    if result.home_result_probability is not None:
        facts.append(f"Home win probability: {_percent(result.home_result_probability)}%.")
    if result.draw_probability is not None:
        facts.append(f"Draw probability: {_percent(result.draw_probability)}%.")
    if result.away_result_probability is not None:
        facts.append(f"Away win probability: {_percent(result.away_result_probability)}%.")
    if result.home_elo is not None and result.away_elo is not None:
        facts.append(f"Elo ratings: home {result.home_elo:.0f}, away {result.away_elo:.0f}.")
    if result.elo_gap is not None:
        lean = "home" if result.elo_gap > 0 else "away" if result.elo_gap < 0 else "neither side"
        facts.append(f"Elo gap after home advantage: {result.elo_gap:.0f}, supporting {lean}.")
    return facts


def _count_facts(
    prediction: FixturePrediction,
    counts,
    event: str,
    descriptor: MarketDescriptor,
) -> list[str]:
    field = {
        "corners": "expected_total_corners",
        "cards": "expected_total_cards",
        "sot": "expected_total_sot",
    }[event]
    label = {"corners": "corners", "cards": "cards", "sot": "shots on target"}[event]
    expected = getattr(counts, field, None)
    facts = []
    if expected is not None:
        facts.append(f"Projected {label}: {expected:.2f}.")
    home = counts.expected_team_counts.get(event, {}).get("home")
    away = counts.expected_team_counts.get(event, {}).get("away")
    if home is not None:
        facts.append(f"Home team averages {home:.2f} {label}.")
    if away is not None:
        facts.append(f"Away team averages {away:.2f} {label}.")
    if event == "cards":
        referee = ((prediction.features.features if prediction.features else {}) or {}).get("referee") or {}
        referee_cards = _float(referee.get("avg_cards_per_match"))
        sample = _int(referee.get("sample_matches"))
        if referee_cards is not None and sample:
            name = str(referee.get("name") or "The assigned referee").strip()
            facts.append(f"{name} averages {referee_cards:.2f} cards across {sample} tracked matches.")
    line = _float(descriptor.line)
    line_expected = _count_expected_for_descriptor(counts, event, descriptor)
    if line is not None and line_expected is not None and descriptor.side in {"over", "under"}:
        direction = "below" if line < line_expected else "above"
        if descriptor.family.startswith("team_"):
            team_label = "home" if descriptor.team == "home" else "away" if descriptor.team == "away" else "team"
            facts.append(
                f"Line {line:g} is {direction} the {team_label} team projection of {line_expected:.2f} {label}."
            )
            facts.extend(_team_market_profile_facts(prediction, descriptor))
            facts.extend(_league_market_profile_facts(prediction, descriptor))
            return facts
        facts.append(
            f"Line {line:g} is {direction} the model projection of {line_expected:.2f} {label}."
        )
    facts.extend(_team_market_profile_facts(prediction, descriptor))
    facts.extend(_league_market_profile_facts(prediction, descriptor))
    return facts


def _team_market_profile_facts(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> list[str]:
    payload = prediction.features.features if prediction.features else {}
    family = descriptor.family
    facts = []
    for side, label in (("home", "Home team"), ("away", "Away team")):
        profile = _matching_market_profile(
            ((payload.get("market_family_history") or {}).get(side) or {}).get(family) or [],
            descriptor,
            preferred_scope=side,
        )
        if not profile:
            continue
        wins = _int(profile.get("wins"))
        attempts = _int(profile.get("attempts"))
        if wins is None or not attempts:
            continue
        facts.append(
            f"{label} profile: {descriptor.canonical} landed in {wins} of {attempts} tracked comparable games."
        )
    return facts


def _league_market_profile_facts(
    prediction: FixturePrediction,
    descriptor: MarketDescriptor,
) -> list[str]:
    payload = prediction.features.features if prediction.features else {}
    profile = _matching_market_profile(
        ((payload.get("market_family_history") or {}).get("league") or {}).get(descriptor.family)
        or [],
        descriptor,
    )
    if not profile:
        return []
    attempts = _int(profile.get("attempts"))
    hit_rate = _float(profile.get("hit_rate"))
    if attempts is None or hit_rate is None:
        return []
    return [
        f"Stored league profile: {hit_rate:.1f}% hit rate for {descriptor.canonical} across {attempts} matches."
    ]


def _matching_market_profile(
    rows, descriptor: MarketDescriptor, *, preferred_scope: str = ""
) -> dict | None:
    canonical = str(descriptor.canonical or descriptor.raw or "").lower()
    side = str(descriptor.side or "").lower()
    line = _float(descriptor.line)
    matches = []
    for row in rows or []:
        if str(row.get("market") or "").lower() != canonical:
            continue
        row_line = _float(row.get("line"))
        if line is not None and row_line is not None and abs(line - row_line) > 1e-9:
            continue
        row_side = str(row.get("side") or "").lower()
        if side and row_side and row_side != side:
            continue
        matches.append(row)
    if not matches:
        return None
    if preferred_scope:
        for row in matches:
            if str(row.get("scope") or "").lower() == preferred_scope:
                return row
    return max(matches, key=lambda row: _int(row.get("attempts")) or 0)


def _matrix(goals) -> dict[tuple[int, int], float]:
    if goals is None:
        return {}
    matrix = {}
    for scoreline, probability in goals.scoreline_matrix.items():
        home, separator, away = str(scoreline).partition("-")
        if not separator:
            continue
        try:
            matrix[(int(home), int(away))] = float(probability)
        except (TypeError, ValueError):
            continue
    return matrix


def _sum_matrix(matrix: dict[tuple[int, int], float], predicate) -> float:
    return (
        _round_probability(
            sum(
                probability for (home, away), probability in matrix.items() if predicate(home, away)
            )
        )
        or 0.0
    )


def _confidence_score(probability: float | None, quality: str) -> float | None:
    if probability is None:
        return None
    quality_cap = {
        "strong": 92,
        "medium": 82,
        "fresh": 82,
        "limited": 70,
        "partial": 70,
        "poor": 55,
    }.get(quality, 45)
    return round(min(quality_cap, max(0.0, probability * 100.0)), 2)


def _quality(output, *, fallback: str = "unavailable") -> str:
    diagnostics = getattr(output, "diagnostics", None)
    return str(getattr(diagnostics, "data_quality", None) or fallback)


def _line_key(value) -> str:
    number = _float(value)
    if number is None:
        return ""
    return str(number).replace(".", "_")


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _percent(value: float) -> int:
    return int(round(value * 100))


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def _round_probability(value: float | None) -> float | None:
    if value is None:
        return None
    return round(min(1.0, max(0.0, float(value))), 6)
