"""Odds-aware value scoring for calibrated probabilities."""

from __future__ import annotations

from typing import Any

from .contracts import MarketProbability, PredictionDiagnostics, ValueAssessment


def assess_market_value(
    market_probability: MarketProbability,
    *,
    available_odds: float | int | str | None,
    odds_source: str = "",
    estimated_odds: bool = False,
    sample_size: int | None = None,
    market_volatility: float | None = None,
    league_uncertainty: float | None = None,
    correlation: float | None = None,
    context: dict[str, Any] | None = None,
) -> ValueAssessment:
    """Compare calibrated model probability with bookmaker price."""
    probability = market_probability.effective_probability
    odds = _float_or_none(available_odds)
    warnings = list(market_probability.warnings)
    if probability is None:
        warnings.append("probability_missing")
    if odds is None or odds <= 1.0:
        warnings.append("available_odds_missing")

    fair_odds = _fair_odds(probability)
    implied = _implied_probability(odds)
    edge = (
        _round(probability - implied) if probability is not None and implied is not None else None
    )
    ev = (
        _round(probability * odds - 1.0)
        if probability is not None and odds is not None and odds > 1.0
        else None
    )
    penalties = _penalties(
        market_probability,
        odds_source=odds_source,
        estimated_odds=estimated_odds,
        sample_size=sample_size,
        market_volatility=market_volatility,
        league_uncertainty=league_uncertainty,
        correlation=correlation,
        context=context or {},
    )
    warnings.extend(penalties["warnings"])
    edge_score = _edge_score(edge)
    value_score = _value_score(edge_score=edge_score, ev=ev, total_penalty=penalties["total"])
    pricing_warnings = tuple(dict.fromkeys(warnings))
    explanation_facts = _value_explanation_facts(
        fair_odds=fair_odds,
        available_odds=odds,
        edge=edge,
        ev=ev,
        total_penalty=penalties["total"],
    )
    return ValueAssessment(
        fixture_id=market_probability.fixture_id,
        market=market_probability.market,
        calibrated_probability=probability,
        available_odds=odds,
        fair_odds=fair_odds,
        bookmaker_implied_probability=implied,
        edge=edge,
        ev=ev,
        edge_score=edge_score,
        value_score=value_score,
        odds_source_penalty=penalties["odds_source_penalty"],
        sample_size_penalty=penalties["sample_size_penalty"],
        market_volatility_penalty=penalties["market_volatility_penalty"],
        league_uncertainty_penalty=penalties["league_uncertainty_penalty"],
        correlation_penalty=penalties["correlation_penalty"],
        pricing_warning=pricing_warnings[0] if pricing_warnings else "",
        pricing_warnings=pricing_warnings,
        explanation_facts=explanation_facts,
        diagnostics=PredictionDiagnostics(
            data_quality=market_probability.data_quality,
            model_sources=("prediction.value",),
            warnings=pricing_warnings,
            metadata={
                "odds_source": odds_source,
                "estimated_odds": estimated_odds,
                "sample_size": _resolved_sample_size(market_probability, sample_size),
                "total_penalty": penalties["total"],
                "penalties": {
                    "odds_source": penalties["odds_source_penalty"],
                    "sample_size": penalties["sample_size_penalty"],
                    "market_volatility": penalties["market_volatility_penalty"],
                    "league_uncertainty": penalties["league_uncertainty_penalty"],
                    "correlation": penalties["correlation_penalty"],
                },
            },
        ),
    )


def _penalties(
    market_probability: MarketProbability,
    *,
    odds_source: str,
    estimated_odds: bool,
    sample_size: int | None,
    market_volatility: float | None,
    league_uncertainty: float | None,
    correlation: float | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    warnings = []
    odds_source_penalty = _odds_source_penalty(odds_source, estimated_odds)
    if odds_source_penalty:
        warnings.append("odds_source_penalty")

    resolved_sample_size = _resolved_sample_size(market_probability, sample_size)
    sample_size_penalty = _sample_size_penalty(resolved_sample_size)
    if sample_size_penalty:
        warnings.append("sample_size_penalty")

    market_volatility_penalty = _scaled_penalty(
        _first_number(market_volatility, context.get("market_volatility")), 10.0
    )
    if market_volatility_penalty:
        warnings.append("market_volatility_penalty")

    league_uncertainty_penalty = _league_uncertainty_penalty(
        market_probability,
        _first_number(league_uncertainty, context.get("league_uncertainty")),
        context,
    )
    if league_uncertainty_penalty:
        warnings.append("league_uncertainty_penalty")

    correlation_penalty = _scaled_penalty(
        _first_number(correlation, context.get("correlation")), 8.0
    )
    if correlation_penalty:
        warnings.append("correlation_penalty")

    total = round(
        odds_source_penalty
        + sample_size_penalty
        + market_volatility_penalty
        + league_uncertainty_penalty
        + correlation_penalty,
        2,
    )
    return {
        "odds_source_penalty": odds_source_penalty,
        "sample_size_penalty": sample_size_penalty,
        "market_volatility_penalty": market_volatility_penalty,
        "league_uncertainty_penalty": league_uncertainty_penalty,
        "correlation_penalty": correlation_penalty,
        "total": total,
        "warnings": warnings,
    }


def _odds_source_penalty(odds_source: str, estimated_odds: bool) -> float:
    if estimated_odds:
        return 12.0
    source = str(odds_source or "").strip().lower()
    if not source:
        return 8.0
    if source in {"sportybet", "bookmaker", "market", "real", "api_football", "statpal"}:
        return 0.0
    return 3.0


def _sample_size_penalty(sample_size: int | None) -> float:
    if sample_size is None or sample_size <= 0:
        return 15.0
    if sample_size >= 200:
        return 0.0
    if sample_size >= 80:
        return 2.0
    if sample_size >= 40:
        return 5.0
    if sample_size >= 20:
        return 8.0
    return 12.0


def _league_uncertainty_penalty(
    market_probability: MarketProbability,
    explicit_uncertainty: float | None,
    context: dict[str, Any],
) -> float:
    if explicit_uncertainty is not None:
        return _scaled_penalty(explicit_uncertainty, 12.0)
    data_quality = str(context.get("data_quality") or market_probability.data_quality or "").lower()
    quality_penalty = {
        "calibrated": 0.0,
        "strong": 0.0,
        "fresh": 0.0,
        "medium": 3.0,
        "limited": 7.0,
        "partial": 7.0,
        "poor": 12.0,
        "unavailable": 12.0,
        "unknown": 10.0,
    }.get(data_quality, 10.0)
    maturity = str(context.get("season_maturity") or "").lower()
    maturity_penalty = {"mature": 0.0, "forming": 2.0, "early": 4.0, "new": 6.0}.get(maturity, 0.0)
    return round(quality_penalty + maturity_penalty, 2)


def _resolved_sample_size(
    market_probability: MarketProbability, explicit: int | None
) -> int | None:
    if explicit is not None:
        return int(explicit)
    sample_count = market_probability.diagnostics.metadata.get("calibration_sample_count")
    if sample_count is None:
        sample_count = market_probability.diagnostics.metadata.get("sample_count")
    return int(sample_count) if sample_count is not None else None


def _value_explanation_facts(
    *,
    fair_odds: float | None,
    available_odds: float | None,
    edge: float | None,
    ev: float | None,
    total_penalty: float,
) -> tuple[str, ...]:
    facts = []
    if fair_odds is not None:
        facts.append(f"Model fair odds: {fair_odds:.2f}.")
    if available_odds is not None:
        facts.append(f"Available odds: {available_odds:.2f}.")
    if edge is not None:
        if edge > 0.03:
            facts.append("Positive edge remains after calibration.")
        elif edge > 0:
            facts.append("Small positive edge after calibration.")
        elif edge < -0.03:
            facts.append("Available price is below the model's calibrated fair price.")
        else:
            facts.append("No meaningful pricing edge after calibration.")
    if ev is not None:
        facts.append(f"Estimated EV after calibration: {ev:+.3f}.")
    if total_penalty:
        facts.append(f"Value score includes {total_penalty:.1f} points of data/pricing penalties.")
    return tuple(facts)


def _edge_score(edge: float | None) -> float | None:
    if edge is None:
        return None
    return round(max(-100.0, min(100.0, edge * 100.0)), 2)


def _value_score(
    *, edge_score: float | None, ev: float | None, total_penalty: float
) -> float | None:
    if edge_score is None or ev is None:
        return None
    base = (edge_score * 0.7) + (ev * 100.0 * 0.3)
    return round(max(-100.0, min(100.0, base - total_penalty)), 2)


def _fair_odds(probability: float | None) -> float | None:
    if probability is None or probability <= 0:
        return None
    return round(1.0 / probability, 4)


def _implied_probability(odds: float | None) -> float | None:
    if odds is None or odds <= 1.0:
        return None
    return round(1.0 / odds, 6)


def _scaled_penalty(value: float | None, maximum: float) -> float:
    if value is None:
        return 0.0
    return round(max(0.0, min(1.0, value)) * maximum, 2)


def _first_number(*values) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _float_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
