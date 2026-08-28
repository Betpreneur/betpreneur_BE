"""Balanced market ranking score."""

from __future__ import annotations

from typing import Any

from betpreneur.modules.markets.api import describe_market

from .contracts import (
    MarketProbability,
    PredictionDiagnostics,
    RecommendationScore,
    ValueAssessment,
)

WEAK_MARKET_WATCHLIST_MARKETS = {"over 1.5"}
VERY_SHORT_ODDS_MIN = 1.20
VERY_SHORT_ODDS_MAX = 1.29
LOW_SAMPLE_HIGH_EV_MIN_EV = 0.08
LOW_SAMPLE_HIGH_EV_MAX_SAMPLE = 40


def score_recommendation(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None = None,
    *,
    market_fit_score: float | int | None = None,
    uncertainty_penalty: float | int | None = None,
    weak_market_penalty: float | int | None = None,
    correlation_penalty: float | int | None = None,
    stale_data_penalty: float | int | None = None,
    context: dict[str, Any] | None = None,
) -> RecommendationScore:
    """Create one balanced score from probability, market fit, value, and risk."""
    context = context or {}
    probability_score = _probability_score(market_probability)
    fit_score = _fit_score(market_fit_score, market_probability, context)
    value_score = _value_score(value_assessment)
    penalties = {
        "uncertainty": _uncertainty_penalty(
            uncertainty_penalty, market_probability, value_assessment, context
        ),
        "weak_market": _weak_market_penalty(
            weak_market_penalty, market_probability, value_assessment, context
        ),
        "correlation": _correlation_penalty(correlation_penalty, value_assessment, context),
        "stale_data": _stale_data_penalty(stale_data_penalty, market_probability, context),
    }
    score = _balanced_score(
        probability_score=probability_score,
        market_fit_score=fit_score,
        value_score=value_score,
        penalties=penalties,
    )
    watchlist_penalty, watchlist_signals = _weak_market_watchlist(
        market_probability,
        value_assessment,
        context,
    )
    warnings = _warnings(market_probability, value_assessment, penalties, watchlist_signals)
    return RecommendationScore(
        fixture_id=market_probability.fixture_id,
        market=market_probability.market,
        recommendation_score=score,
        calibrated_probability_score=probability_score,
        market_fit_score=fit_score,
        value_score=value_score,
        uncertainty_penalty=penalties["uncertainty"],
        weak_market_penalty=penalties["weak_market"],
        correlation_penalty=penalties["correlation"],
        stale_data_penalty=penalties["stale_data"],
        warnings=warnings,
        diagnostics=PredictionDiagnostics(
            data_quality=market_probability.data_quality,
            model_sources=("prediction.recommendation",),
            warnings=warnings,
            metadata={
                "weights": {"probability": 0.45, "market_fit": 0.30, "value": 0.25},
                "penalties": penalties,
                "watchlist_penalty": watchlist_penalty,
                "watchlist_signals": watchlist_signals,
                "source": "balanced_probability_fit_value",
            },
        ),
    )


def _probability_score(market_probability: MarketProbability) -> float | None:
    probability = market_probability.effective_probability
    if probability is None:
        return None
    return round(min(100.0, max(0.0, probability * 100.0)), 2)


def _fit_score(
    explicit, market_probability: MarketProbability, context: dict[str, Any]
) -> float | None:
    value = explicit
    if value is None:
        value = context.get("market_fit_score")
    if value is None:
        value = market_probability.diagnostics.metadata.get("market_fit_score")
    if value is None:
        value = _fit_from_support(
            context.get("market_support_level")
            or market_probability.diagnostics.metadata.get("market_support_level")
        )
    return _score_or_none(value)


def _fit_from_support(value) -> float | None:
    return {
        "full": 82.0,
        "strong": 82.0,
        "medium": 68.0,
        "weak": 45.0,
        "unsupported": 25.0,
    }.get(str(value or "").strip().lower())


def _value_score(value_assessment: ValueAssessment | None) -> float | None:
    if value_assessment is None or value_assessment.value_score is None:
        return None
    return round(max(0.0, min(100.0, 50.0 + value_assessment.value_score)), 2)


def _balanced_score(
    *,
    probability_score: float | None,
    market_fit_score: float | None,
    value_score: float | None,
    penalties: dict[str, float],
) -> float | None:
    if probability_score is None:
        return None
    fit = market_fit_score if market_fit_score is not None else 50.0
    value = value_score if value_score is not None else 50.0
    base = probability_score * 0.45 + fit * 0.30 + value * 0.25
    return round(max(0.0, min(100.0, base - sum(penalties.values()))), 2)


def _uncertainty_penalty(
    explicit,
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None,
    context: dict[str, Any],
) -> float:
    if explicit is not None:
        return _penalty(explicit)
    if value_assessment is not None:
        return _penalty(
            value_assessment.sample_size_penalty + value_assessment.league_uncertainty_penalty
        )
    quality = str(context.get("data_quality") or market_probability.data_quality or "").lower()
    return {
        "calibrated": 0.0,
        "strong": 0.0,
        "fresh": 0.0,
        "medium": 4.0,
        "limited": 8.0,
        "partial": 8.0,
        "poor": 12.0,
        "unavailable": 15.0,
        "unknown": 10.0,
    }.get(quality, 10.0)


def _weak_market_penalty(
    explicit,
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None,
    context: dict[str, Any],
) -> float:
    if explicit is not None:
        base_penalty = _penalty(explicit)
    else:
        base_penalty = _base_weak_market_penalty(market_probability, context)
    watchlist_penalty, _signals = _weak_market_watchlist(
        market_probability, value_assessment, context
    )
    return _penalty(base_penalty + watchlist_penalty)


def _base_weak_market_penalty(
    market_probability: MarketProbability, context: dict[str, Any]
) -> float:
    support = str(
        context.get("market_support_level")
        or market_probability.diagnostics.metadata.get("market_support_level")
        or ""
    ).lower()
    if support in {"weak", "unsupported"}:
        return 12.0 if support == "weak" else 18.0
    family = str(
        context.get("market_family")
        or market_probability.diagnostics.metadata.get("market_family")
        or ""
    ).lower()
    if family in {"", "unsupported"}:
        return 8.0
    return 0.0


def _weak_market_watchlist(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None,
    context: dict[str, Any],
) -> tuple[float, tuple[str, ...]]:
    """Extra proof burden for markets called out by calibration analysis."""
    signals = []
    penalty = 0.0
    market_name = _normalized_market_name(market_probability.market)

    if market_name in WEAK_MARKET_WATCHLIST_MARKETS or _is_double_chance_12(
        market_probability.market
    ):
        signals.append("watchlist_market_family")
        penalty += 6.0

    odds = _available_odds(value_assessment, context)
    if odds is not None and VERY_SHORT_ODDS_MIN <= odds <= VERY_SHORT_ODDS_MAX:
        signals.append("very_short_odds_watchlist")
        penalty += 5.0

    if _uses_estimated_odds(value_assessment, context):
        signals.append("estimated_odds_watchlist")
        penalty += 8.0

    sample_size = _sample_size(market_probability, value_assessment, context)
    ev = _ev(value_assessment, context)
    if (
        ev is not None
        and ev >= LOW_SAMPLE_HIGH_EV_MIN_EV
        and (sample_size is None or sample_size < LOW_SAMPLE_HIGH_EV_MAX_SAMPLE)
    ):
        signals.append("low_sample_high_ev_watchlist")
        penalty += 6.0

    return (_penalty(penalty), tuple(signals))


def _normalized_market_name(value: str) -> str:
    return " ".join(str(value or "").replace("DC:", "DC: ").split()).lower()


def _is_double_chance_12(market: str) -> bool:
    descriptor = describe_market(market)
    if descriptor.family != "double_chance":
        return False
    return descriptor.side in {"home_or_away", "12"} or descriptor.code in {
        "double_chance_12",
        "double_chance_home_or_away",
        "double_chance_home_or_away_1up",
        "double_chance_home_or_away_2up",
    }


def _available_odds(
    value_assessment: ValueAssessment | None, context: dict[str, Any]
) -> float | None:
    if value_assessment is not None and value_assessment.available_odds is not None:
        return float(value_assessment.available_odds)
    value = context.get("available_odds") or context.get("odds")
    return float(value) if value not in (None, "") else None


def _uses_estimated_odds(value_assessment: ValueAssessment | None, context: dict[str, Any]) -> bool:
    if bool(context.get("estimated_odds")):
        return True
    if value_assessment is None:
        return False
    return bool(
        value_assessment.diagnostics.metadata.get("estimated_odds")
        or "odds_source_penalty" in value_assessment.pricing_warnings
    )


def _sample_size(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None,
    context: dict[str, Any],
) -> int | None:
    if context.get("sample_size") not in (None, ""):
        return int(context["sample_size"])
    if value_assessment is not None and value_assessment.diagnostics.metadata.get(
        "sample_size"
    ) not in (None, ""):
        return int(value_assessment.diagnostics.metadata["sample_size"])
    sample_count = market_probability.diagnostics.metadata.get("calibration_sample_count")
    if sample_count is None:
        sample_count = market_probability.diagnostics.metadata.get("sample_count")
    return int(sample_count) if sample_count not in (None, "") else None


def _ev(value_assessment: ValueAssessment | None, context: dict[str, Any]) -> float | None:
    if value_assessment is not None and value_assessment.ev is not None:
        return float(value_assessment.ev)
    value = context.get("ev")
    return float(value) if value not in (None, "") else None


def _correlation_penalty(
    explicit, value_assessment: ValueAssessment | None, context: dict[str, Any]
) -> float:
    if explicit is not None:
        return _penalty(explicit)
    if value_assessment is not None:
        return _penalty(value_assessment.correlation_penalty)
    return _scaled(context.get("correlation"), maximum=8.0)


def _stale_data_penalty(
    explicit, market_probability: MarketProbability, context: dict[str, Any]
) -> float:
    if explicit is not None:
        return _penalty(explicit)
    status = str(
        context.get("freshness_status")
        or market_probability.diagnostics.metadata.get("freshness_status")
        or ""
    ).lower()
    if status in {"fresh", "current"}:
        return 0.0
    if status == "stale" or "stale" in market_probability.warnings:
        return 10.0
    if market_probability.data_quality in {"unavailable", "unknown"}:
        return 6.0
    return 0.0


def _warnings(
    market_probability: MarketProbability,
    value_assessment: ValueAssessment | None,
    penalties: dict[str, float],
    watchlist_signals: tuple[str, ...] = (),
) -> tuple[str, ...]:
    warnings = list(market_probability.warnings)
    if value_assessment is not None:
        warnings.extend(value_assessment.pricing_warnings)
    warnings.extend(watchlist_signals)
    warnings.extend(f"{name}_penalty" for name, value in penalties.items() if value)
    return tuple(dict.fromkeys(warnings))


def _score_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    return round(max(0.0, min(100.0, float(value))), 2)


def _penalty(value) -> float:
    return round(max(0.0, min(100.0, float(value or 0.0))), 2)


def _scaled(value, *, maximum: float) -> float:
    if value in (None, ""):
        return 0.0
    return round(max(0.0, min(1.0, float(value))) * maximum, 2)
