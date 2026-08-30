"""Market probability dispatch."""

from __future__ import annotations

from functools import cache

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
    return (
        _round_probability(probability),
        "poisson_goals",
        _goal_facts(prediction, goals, descriptor),
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
    return probability, "poisson_counts", facts, warnings, counts.diagnostics.data_quality


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
    if line is not None and total is not None and descriptor.side in {"over", "under"}:
        direction = "below" if line < total else "above"
        facts.append(f"Line {line:g} is {direction} the model projection of {total:.2f} goals.")
    facts.extend(_team_market_profile_facts(prediction, descriptor))
    facts.extend(_league_market_profile_facts(prediction, descriptor))
    return facts


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
    line = _float(descriptor.line)
    if line is not None and expected is not None and descriptor.side in {"over", "under"}:
        direction = "below" if line < expected else "above"
        facts.append(
            f"Line {line:g} is {direction} the model projection of {expected:.2f} {label}."
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


def _round_probability(value: float | None) -> float | None:
    if value is None:
        return None
    return round(min(1.0, max(0.0, float(value))), 6)
