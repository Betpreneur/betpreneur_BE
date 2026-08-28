"""Probability calibration boundary.

Raw model outputs are useful, but they are often too confident. This layer
learns from settled prediction samples and returns the probability products
should show to users.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .contracts import CalibrationResult, PredictionDiagnostics
from .models import PredictionTrainingSample

MIN_PLATT_SAMPLES = 20
MIN_ISOTONIC_SAMPLES = 40
CONFIDENCE_BAND_SIZE = 10


@dataclass(frozen=True, slots=True)
class CalibrationScope:
    market: str = ""
    market_family: str = ""
    league_key: str = ""
    confidence_band: str = ""
    odds_band: str = ""
    season_maturity: str = ""
    data_quality: str = ""


def calibrate_probability(
    raw_probability: float | None, *, market: str = "", context=None
) -> CalibrationResult:
    """Calibrate a raw probability against settled historical samples."""
    if raw_probability is None:
        return _identity_result(raw_probability, market=market, warning="raw_probability_missing")

    raw_probability = _clamp_probability(raw_probability)
    scope = _scope_for(raw_probability, market=market, context=context)
    try:
        samples, sample_scope = _training_samples(scope)
    except Exception:
        return _identity_result(
            raw_probability, market=market, scope=scope, warning="calibration_dataset_unavailable"
        )

    if len(samples) >= MIN_ISOTONIC_SAMPLES:
        calibrated = _isotonic_calibrate(raw_probability, samples)
        method = "isotonic"
    elif len(samples) >= MIN_PLATT_SAMPLES:
        calibrated = _platt_calibrate(raw_probability, samples)
        method = "platt"
    else:
        return _identity_result(
            raw_probability,
            market=market,
            scope=scope,
            warning="calibration_sample_too_small",
            sample_count=len(samples),
            sample_scope=sample_scope,
        )

    calibrated = _clamp_probability(calibrated)
    penalty = round((calibrated - raw_probability) * 100.0, 2)
    warnings = []
    if penalty < 0:
        warnings.append("raw_probability_reduced_by_calibration")
    elif penalty > 0:
        warnings.append("raw_probability_lifted_by_calibration")
    return CalibrationResult(
        raw_probability=raw_probability,
        calibrated_probability=calibrated,
        method=method,
        calibration_penalty=penalty,
        diagnostics=PredictionDiagnostics(
            data_quality="calibrated",
            model_sources=("prediction.calibration", f"prediction.calibration.{method}"),
            warnings=tuple(warnings),
            metadata={
                "market": market,
                "sample_count": len(samples),
                "scope": asdict(sample_scope),
                "requested_scope": asdict(scope),
            },
        ),
    )


def _training_samples(scope: CalibrationScope) -> tuple[list[tuple[float, int]], CalibrationScope]:
    scopes = (
        scope,
        CalibrationScope(
            market_family=scope.market_family,
            league_key=scope.league_key,
            confidence_band=scope.confidence_band,
        ),
        CalibrationScope(market_family=scope.market_family, confidence_band=scope.confidence_band),
        CalibrationScope(market_family=scope.market_family),
        CalibrationScope(confidence_band=scope.confidence_band),
        CalibrationScope(),
    )
    best: list[tuple[float, int]] = []
    best_scope = scopes[-1]
    for candidate in scopes:
        samples = _samples_for_scope(candidate)
        if len(samples) >= MIN_PLATT_SAMPLES:
            return samples, candidate
        if len(samples) > len(best):
            best = samples
            best_scope = candidate
    return best, best_scope


def _samples_for_scope(scope: CalibrationScope) -> list[tuple[float, int]]:
    query = PredictionTrainingSample.objects.filter(settlement_result__in=["win", "loss"])
    if scope.market:
        query = query.filter(canonical_market=scope.market)
    if scope.market_family:
        query = query.filter(market_family=scope.market_family)
    if scope.league_key:
        query = query.filter(league_key=scope.league_key)
    if scope.data_quality:
        query = query.filter(metadata__data_quality=scope.data_quality)
    if scope.season_maturity:
        query = query.filter(metadata__season_maturity=scope.season_maturity)
    if scope.odds_band:
        query = query.filter(metadata__odds_band=scope.odds_band)
    if scope.confidence_band:
        low, high = _confidence_band_limits(scope.confidence_band)
        query = query.filter(last_prediction_score__gte=low, last_prediction_score__lt=high)

    rows = query.order_by("-prediction_created_at", "-id").values_list(
        "last_prediction_score", "first_prediction_score", "settlement_result"
    )[:5000]
    samples = []
    for last_score, first_score, result in rows:
        score = last_score if last_score is not None else first_score
        if score is None:
            continue
        samples.append((_clamp_probability(float(score) / 100.0), 1 if result == "win" else 0))
    return samples


def _isotonic_calibrate(raw_probability: float, samples: list[tuple[float, int]]) -> float:
    grouped: dict[int, list[int]] = {}
    for prediction, outcome in samples:
        grouped.setdefault(_band_index(prediction), []).append(outcome)
    blocks = [
        {
            "x": (index * CONFIDENCE_BAND_SIZE + CONFIDENCE_BAND_SIZE / 2) / 100.0,
            "y": sum(outcomes) / len(outcomes),
            "w": len(outcomes),
        }
        for index, outcomes in sorted(grouped.items())
    ]
    if not blocks:
        return raw_probability

    pooled = []
    for block in blocks:
        pooled.append(block)
        while len(pooled) >= 2 and pooled[-2]["y"] > pooled[-1]["y"]:
            right = pooled.pop()
            left = pooled.pop()
            weight = left["w"] + right["w"]
            pooled.append(
                {
                    "x": (left["x"] * left["w"] + right["x"] * right["w"]) / weight,
                    "y": (left["y"] * left["w"] + right["y"] * right["w"]) / weight,
                    "w": weight,
                }
            )
    return _interpolate(raw_probability, [(block["x"], block["y"]) for block in pooled])


def _platt_calibrate(raw_probability: float, samples: list[tuple[float, int]]) -> float:
    a = 1.0
    b = 0.0
    learning_rate = 0.05
    for _ in range(300):
        grad_a = 0.0
        grad_b = 0.0
        for prediction, outcome in samples:
            x = _logit(prediction)
            fitted = _sigmoid(a * x + b)
            grad_a += (fitted - outcome) * x
            grad_b += fitted - outcome
        scale = max(len(samples), 1)
        a -= learning_rate * grad_a / scale
        b -= learning_rate * grad_b / scale
    return _sigmoid(a * _logit(raw_probability) + b)


def _scope_for(raw_probability: float, *, market: str, context: Any) -> CalibrationScope:
    payload = _context_payload(context)
    descriptor = payload.get("descriptor")
    prediction = payload.get("fixture_prediction")
    features = getattr(prediction, "features", None)
    feature_payload = getattr(features, "features", {}) or {}
    league_payload = feature_payload.get("league") or {}
    market_family = payload.get("market_family") or getattr(descriptor, "family", "") or ""
    league_key = (
        payload.get("league_key")
        or getattr(features, "league_key", "")
        or league_payload.get("league_key")
        or ""
    )
    data_quality = (
        payload.get("data_quality")
        or getattr(getattr(prediction, "diagnostics", None), "data_quality", "")
        or ""
    )
    season_maturity = _season_maturity_band(
        payload.get("season_maturity") or league_payload.get("season_maturity") or {}
    )
    return CalibrationScope(
        market=market,
        market_family=market_family,
        league_key=league_key,
        confidence_band=_confidence_band(raw_probability),
        odds_band=_odds_band(payload.get("real_odds")),
        season_maturity=season_maturity,
        data_quality=str(data_quality or ""),
    )


def _context_payload(context: Any) -> dict[str, Any]:
    if context is None:
        return {}
    if isinstance(context, dict):
        return context
    return {"fixture_prediction": context}


def _identity_result(
    raw_probability: float | None,
    *,
    market: str,
    scope: CalibrationScope | None = None,
    warning: str,
    sample_count: int = 0,
    sample_scope: CalibrationScope | None = None,
) -> CalibrationResult:
    return CalibrationResult(
        raw_probability=raw_probability,
        calibrated_probability=raw_probability,
        method="identity",
        calibration_penalty=0.0 if raw_probability is not None else None,
        diagnostics=PredictionDiagnostics(
            data_quality="unavailable" if raw_probability is None else "uncalibrated",
            model_sources=("prediction.calibration",),
            warnings=(warning,),
            metadata={
                "market": market,
                "sample_count": sample_count,
                "scope": asdict(sample_scope or scope) if (sample_scope or scope) else {},
            },
        ),
    )


def _confidence_band(probability: float) -> str:
    index = _band_index(probability)
    return f"{index * CONFIDENCE_BAND_SIZE}-{(index + 1) * CONFIDENCE_BAND_SIZE}"


def _confidence_band_limits(label: str) -> tuple[int, int]:
    low, _, high = label.partition("-")
    return int(low or 0), int(high or 100)


def _band_index(probability: float) -> int:
    return min(9, max(0, int(_clamp_probability(probability) * 100) // CONFIDENCE_BAND_SIZE))


def _odds_band(value) -> str:
    if value in (None, ""):
        return ""
    odds = float(value)
    if odds < 1.4:
        return "low"
    if odds < 2.2:
        return "mid"
    return "high"


def _season_maturity_band(value) -> str:
    if not isinstance(value, dict):
        return ""
    minimum = int(value.get("minimum_team_matches") or 0)
    if minimum >= 12:
        return "mature"
    if minimum >= 5:
        return "forming"
    if minimum > 0:
        return "early"
    return "new"


def _interpolate(x: float, points: list[tuple[float, float]]) -> float:
    points = sorted(points)
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for index, (left_x, left_y) in enumerate(points[:-1]):
        right_x, right_y = points[index + 1]
        if left_x <= x <= right_x:
            width = max(right_x - left_x, 1e-9)
            ratio = (x - left_x) / width
            return left_y + ratio * (right_y - left_y)
    return points[-1][1]


def _logit(probability: float) -> float:
    probability = min(0.999, max(0.001, probability))
    return math.log(probability / (1.0 - probability))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp_probability(value: float) -> float:
    return min(1.0, max(0.0, float(value)))
