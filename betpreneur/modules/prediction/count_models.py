"""Poisson count models for corners, cards, and shots on target."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .contracts import CountModelOutput, FixtureFeatureSet, PredictionDiagnostics

MODEL_VERSION = "count-events-v1"

EVENTS = {
    "corners": {
        "total_field": "expected_total_corners",
        "rate_prefix": "corners",
        "season_for": "corners_for",
        "season_against": "corners_against",
        "recent_for": "corners_for_per_match",
        "league_team_average": 5.1,
        "team_rate_ceiling": 10.5,
        "total_lines": (7.5, 8.5, 9.5, 10.5),
        "team_lines": (1.5, 2.5, 3.5, 4.5, 5.5),
    },
    "cards": {
        "total_field": "expected_total_cards",
        "rate_prefix": "cards",
        "season_for": "cards_for",
        "season_against": "cards_against",
        "recent_for": "cards_for_per_match",
        "league_team_average": 1.9,
        "team_rate_ceiling": 5.5,
        "total_lines": (2.5, 3.5, 4.5, 5.5),
        "team_lines": (0.5, 1.5, 2.5, 3.5),
    },
    "sot": {
        "total_field": "expected_total_sot",
        "rate_prefix": "shots_on_target",
        "season_for": "shots_on_target_for",
        "season_against": "shots_on_target_against",
        "recent_for": "shots_on_target_for_per_match",
        "league_team_average": 4.2,
        "team_rate_ceiling": 10.0,
        "total_lines": (5.5, 6.5, 7.5, 8.5, 9.5, 10.5),
        "team_lines": (1.5, 2.5, 3.5, 4.5, 5.5),
    },
}


@dataclass(frozen=True)
class _EventForecast:
    home_expected: float
    away_expected: float
    warnings: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def total_expected(self) -> float:
        return round(self.home_expected + self.away_expected, 4)


def count_distributions(features: FixtureFeatureSet) -> CountModelOutput:
    """Return count-event expectations and practical line probabilities."""
    forecasts = {event: _event_forecast(features, event, config) for event, config in EVENTS.items()}
    line_probabilities = {
        event: _line_probabilities(forecast.total_expected, config["total_lines"])
        for event, config in EVENTS.items()
        for forecast in (forecasts[event],)
    }
    team_line_probabilities = {
        event: {
            "home": _line_probabilities(forecast.home_expected, config["team_lines"]),
            "away": _line_probabilities(forecast.away_expected, config["team_lines"]),
        }
        for event, config in EVENTS.items()
        for forecast in (forecasts[event],)
    }
    expected_team_counts = {
        event: {
            "home": round(forecast.home_expected, 4),
            "away": round(forecast.away_expected, 4),
        }
        for event, forecast in forecasts.items()
    }
    warnings = tuple(
        dict.fromkeys(
            (
                *features.diagnostics.warnings,
                *(warning for forecast in forecasts.values() for warning in forecast.warnings),
            )
        )
    )

    return CountModelOutput(
        expected_total_corners=forecasts["corners"].total_expected,
        expected_total_cards=forecasts["cards"].total_expected,
        expected_total_sot=forecasts["sot"].total_expected,
        line_probabilities=line_probabilities,
        team_line_probabilities=team_line_probabilities,
        expected_team_counts=expected_team_counts,
        diagnostics=PredictionDiagnostics(
            data_quality=_count_quality(features, forecasts),
            model_version=MODEL_VERSION,
            model_sources=("prediction.count_models",),
            warnings=warnings,
            metadata={
                "sources": {event: forecast.sources for event, forecast in forecasts.items()},
                "events": {
                    event: {
                        "home_expected": round(forecast.home_expected, 4),
                        "away_expected": round(forecast.away_expected, 4),
                        "total_expected": forecast.total_expected,
                    }
                    for event, forecast in forecasts.items()
                },
            },
        ),
    )


def _event_forecast(features: FixtureFeatureSet, event: str, config: dict[str, Any]) -> _EventForecast:
    home = _team_event_expected(features, event, config, side="home")
    away = _team_event_expected(features, event, config, side="away")
    warnings = tuple(
        warning
        for side, forecast in (("home", home), ("away", away))
        for warning in _side_warnings(event, side, forecast["sources"])
    )
    sources = tuple(dict.fromkeys((*home["sources"], *away["sources"])))
    return _EventForecast(
        home_expected=home["expected"],
        away_expected=away["expected"],
        warnings=warnings,
        sources=sources,
    )


def _team_event_expected(features: FixtureFeatureSet, event: str, config: dict[str, Any], *, side: str) -> dict[str, Any]:
    payload = (features.features or {}).get(side) or {}
    opponent_payload = (features.features or {}).get("away" if side == "home" else "home") or {}
    rate_profile = payload.get("rate_profile") or {}
    season_profile = payload.get("season_profile") or {}
    opponent_season_profile = opponent_payload.get("season_profile") or {}
    recent = payload.get("recent_form") or {}
    referee = (features.features or {}).get("referee") or {}
    league_average = float(config["league_team_average"])
    sources: list[str] = []

    own_rate = _rate_profile_value(rate_profile, config["rate_prefix"], side)
    if own_rate is not None and _plausible_team_rate(own_rate, config):
        sources.append("team_rate_profile")
    else:
        own_rate = None
    own_season = _per_match(season_profile.get(config["season_for"]), season_profile.get("matches_played"))
    if own_season is not None and _plausible_team_rate(own_season, config):
        sources.append("team_season_profile")
    else:
        own_season = None
    opponent_concedes = _per_match(
        opponent_season_profile.get(config["season_against"]),
        opponent_season_profile.get("matches_played"),
    )
    if opponent_concedes is not None and _plausible_team_rate(opponent_concedes, config):
        sources.append("opponent_concession_profile")
    else:
        opponent_concedes = None
    recent_value = _recent_value(recent, key=config["recent_for"], side=side)
    if recent_value is not None and _plausible_team_rate(recent_value, config):
        sources.append("recent_form_profile")
    else:
        recent_value = None
    referee_rate = None
    if event == "cards":
        referee_rate = _referee_team_card_rate(referee, config)
        if referee_rate is not None:
            sources.append("referee_card_profile")

    base = _weighted_average(
        (
            (own_rate, 0.48),
            (own_season, 0.22),
            (opponent_concedes, 0.18),
            (recent_value, 0.12),
            (referee_rate, 0.18),
        ),
        fallback=league_average,
    )
    matches = _float(rate_profile.get("matches")) or _float(season_profile.get("matches_played")) or 0.0
    expected = _shrink(base, matches=matches, prior=league_average)
    return {"expected": round(_clamp(expected, 0.05, float(config["team_rate_ceiling"])), 4), "sources": tuple(sources)}


def _plausible_team_rate(value: float, config: dict[str, Any]) -> bool:
    return 0.0 <= float(value) <= float(config["team_rate_ceiling"])


def _rate_profile_value(rate_profile: dict[str, Any], prefix: str, side: str) -> float | None:
    return _float(rate_profile.get(f"{prefix}_{side}"))


def _recent_value(recent: dict[str, Any], *, key: str, side: str) -> float | None:
    scoped = recent.get(side) or {}
    all_scope = recent.get("all") or {}
    form5 = scoped.get("5") or all_scope.get("5") or {}
    value = _float(form5.get(key))
    if value is not None:
        return value
    raw_key = key.replace("_per_match", "")
    return _per_match(form5.get(raw_key), form5.get("matches"))


def _referee_team_card_rate(referee: dict[str, Any], config: dict[str, Any]) -> float | None:
    cards = _float(referee.get("avg_cards_per_match"))
    sample = _float(referee.get("sample_matches")) or 0.0
    if cards is None or sample < 3:
        return None
    per_team = cards / 2.0
    if not _plausible_team_rate(per_team, config):
        return None
    return per_team


def _line_probabilities(expected: float, lines: tuple[float, ...]) -> dict[str, float]:
    probabilities: dict[str, float] = {}
    for line in lines:
        probabilities[f"over_{_line_key(line)}"] = round(_poisson_over(expected, line), 6)
        probabilities[f"under_{_line_key(line)}"] = round(1.0 - _poisson_over(expected, line), 6)
    return probabilities


def _poisson_over(expected: float, line: float) -> float:
    expected = _clamp(float(expected), 1e-6, 40.0)
    ceiling = max(40, int(expected * 4) + 12)
    masses = []
    mass = math.exp(-expected)
    for count in range(ceiling + 1):
        if count == 0:
            masses.append(mass)
        else:
            mass = mass * expected / count
            masses.append(mass)
    total = sum(masses)
    at_or_below = sum(masses[: int(math.floor(line)) + 1])
    if total <= 0:
        return 0.0
    return _clamp(1.0 - (at_or_below / total), 0.0, 1.0)


def _side_warnings(event: str, side: str, sources: tuple[str, ...]) -> tuple[str, ...]:
    warnings = []
    if not sources:
        warnings.append(f"{side}_{event}_using_league_average")
    if event == "cards" and "referee_card_profile" not in sources:
        warnings.append("referee_card_profile_missing")
    return tuple(warnings)


def _count_quality(features: FixtureFeatureSet, forecasts: dict[str, _EventForecast]) -> str:
    if all(forecast.sources for forecast in forecasts.values()):
        return features.diagnostics.data_quality
    if any(forecast.sources for forecast in forecasts.values()):
        return "limited"
    return "poor"


def _weighted_average(weighted_values, *, fallback: float) -> float:
    total_weight = 0.0
    total = 0.0
    for value, weight in weighted_values:
        number = _float(value)
        if number is None:
            continue
        total += number * weight
        total_weight += weight
    if total_weight <= 0:
        return fallback
    return total / total_weight


def _shrink(value: float, *, matches: float, prior: float) -> float:
    matches = max(0.0, matches)
    prior_weight = 5.0
    return ((value * matches) + (prior * prior_weight)) / (matches + prior_weight)


def _per_match(total, matches) -> float | None:
    total_value = _float(total)
    match_count = _float(matches)
    if total_value is None or not match_count:
        return None
    return total_value / match_count


def _line_key(line: float) -> str:
    return str(line).replace(".", "_")


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return min(ceiling, max(floor, value))
