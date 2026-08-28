"""Next-period evaluation for adaptive strategy memory."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Q

from betpreneur.modules.picks.api import MarketPrediction, StrategyReview

from ..models import StrategyActionOutcome

DEFAULT_EVALUATION_DAYS = 14
MIN_AUTHORITY_SAMPLE = 5


def evaluate_strategy_memory(
    *,
    decision_date: date | str | None = None,
    evaluation_days: int = DEFAULT_EVALUATION_DAYS,
) -> dict[str, Any]:
    """Evaluate strategy actions against what happened after the decision."""
    reviews = _reviews(decision_date)
    outcomes = []
    for review in reviews:
        window_start = review.target_date + timedelta(days=1)
        window_end = review.target_date + timedelta(
            days=max(1, int(evaluation_days or DEFAULT_EVALUATION_DAYS))
        )
        actions = _strategy_actions(review)
        for action in actions:
            metrics = _future_metrics(action, window_start, window_end)
            baseline = _baseline_metrics(window_start, window_end)
            outcome = _save_outcome(
                review=review,
                action=action,
                window_start=window_start,
                window_end=window_end,
                metrics=metrics,
                baseline=baseline,
            )
            outcomes.append(outcome)
    return {
        "reviews": len(reviews),
        "outcomes": len(outcomes),
        "authority_reduced": sum(1 for item in outcomes if item.authority_multiplier < 1),
        "authority_preserved": sum(1 for item in outcomes if item.authority_multiplier == 1),
        "authority_increased": sum(1 for item in outcomes if item.authority_multiplier > 1),
        "evaluation_days": evaluation_days,
    }


def _reviews(decision_date):
    queryset = StrategyReview.objects.all().order_by("target_date")
    if decision_date:
        queryset = queryset.filter(target_date=_date(decision_date))
    return list(queryset)


def _strategy_actions(review: StrategyReview) -> list[dict[str, Any]]:
    actions = []
    for market in review.markets_promoted or []:
        actions.append({"scope": "market", "action": "promote", "key": market, "market": market})
    for market in review.markets_cooling or []:
        actions.append({"scope": "market", "action": "cool", "key": market, "market": market})
    for market in review.markets_suppressed or []:
        actions.append({"scope": "market", "action": "suppress", "key": market, "market": market})

    for key, payload in (review.league_market_actions or {}).items():
        action = str((payload or {}).get("action") or "")
        if action not in {"promote", "cool", "suppress"}:
            continue
        league, market = _split_league_market(key)
        actions.append(
            {
                "scope": "league_market",
                "action": action,
                "key": key,
                "league": league,
                "market": market,
            }
        )

    for band, payload in ((review.profile or {}).get("confidence_bands") or {}).items():
        action = str((payload or {}).get("action") or "")
        if action not in {"promote", "cool", "suppress"}:
            continue
        actions.append(
            {
                "scope": "confidence_band",
                "action": action,
                "key": str(band),
                "confidence_band": str(band),
            }
        )
    return actions


def _future_metrics(action: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    queryset = _settled_market_predictions(start, end)
    if action["scope"] == "market":
        queryset = queryset.filter(market=action["market"])
    elif action["scope"] == "league_market":
        queryset = queryset.filter(league=action["league"], market=action["market"])
    elif action["scope"] == "confidence_band":
        queryset = _apply_confidence_band(queryset, action["confidence_band"])
    return _metrics(list(queryset))


def _baseline_metrics(start: date, end: date) -> dict[str, Any]:
    return _metrics(list(_settled_market_predictions(start, end)))


def _settled_market_predictions(start: date, end: date):
    return MarketPrediction.objects.filter(
        match_date__gte=start,
        match_date__lte=end,
        status__in=[
            MarketPrediction.Status.WIN,
            MarketPrediction.Status.LOSS,
            MarketPrediction.Status.VOID,
        ],
    ).filter(Q(published=True) | Q(selected_pick__isnull=False))


def _metrics(predictions: list[MarketPrediction]) -> dict[str, Any]:
    wins = sum(1 for item in predictions if item.status == MarketPrediction.Status.WIN)
    losses = sum(1 for item in predictions if item.status == MarketPrediction.Status.LOSS)
    voids = sum(1 for item in predictions if item.status == MarketPrediction.Status.VOID)
    settled = wins + losses
    stake = settled
    pnl = sum(
        float(item.pnl_simulated or 0)
        for item in predictions
        if item.status in {MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS}
    )
    return {
        "sample_size": settled,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "hit_rate": round(wins / settled, 6) if settled else None,
        "roi": round(pnl / stake, 6) if stake else None,
    }


def _save_outcome(
    *,
    review: StrategyReview,
    action: dict[str, Any],
    window_start: date,
    window_end: date,
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> StrategyActionOutcome:
    roi = metrics["roi"]
    baseline_roi = baseline["roi"]
    roi_delta = (
        round(roi - baseline_roi, 6) if roi is not None and baseline_roi is not None else None
    )
    verdict = _verdict(action["action"], metrics["sample_size"], roi_delta)
    authority = _authority_multiplier(action["action"], verdict, metrics["sample_size"], roi_delta)
    outcome, _created = StrategyActionOutcome.objects.update_or_create(
        decision_date=review.target_date,
        scope=action["scope"],
        action=action["action"],
        key=action["key"],
        defaults={
            "evaluated_from": window_start,
            "evaluated_to": window_end,
            "market": action.get("market") or "",
            "league": action.get("league") or "",
            "sample_size": metrics["sample_size"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "voids": metrics["voids"],
            "hit_rate": metrics["hit_rate"],
            "roi": roi,
            "baseline_roi": baseline_roi,
            "roi_delta": roi_delta,
            "authority_multiplier": authority,
            "verdict": verdict,
            "metadata": {
                "baseline_sample_size": baseline["sample_size"],
                "principle": "next_period_performance_not_historical_trigger_performance",
            },
        },
    )
    return outcome


def _verdict(action: str, sample_size: int, roi_delta: float | None) -> str:
    if sample_size < MIN_AUTHORITY_SAMPLE or roi_delta is None:
        return "insufficient_sample"
    if action == "promote":
        return "validated" if roi_delta > 0 else "failed_to_improve"
    if action == "suppress":
        return "validated" if roi_delta < 0 else "unnecessary_suppression"
    if action == "cool":
        return "validated" if roi_delta <= 0 else "overcooled"
    return "unknown"


def _authority_multiplier(
    action: str, verdict: str, sample_size: int, roi_delta: float | None
) -> float:
    if sample_size < MIN_AUTHORITY_SAMPLE or roi_delta is None:
        return 0.75
    if verdict == "validated":
        return 1.10 if action == "promote" else 1.0
    if verdict in {"failed_to_improve", "unnecessary_suppression", "overcooled"}:
        return 0.60
    return 1.0


def _split_league_market(key: str) -> tuple[str, str]:
    if "::" not in str(key):
        return "", str(key)
    league, market = str(key).split("::", 1)
    return league, market


def _apply_confidence_band(queryset, band: str):
    if band == "80+":
        return queryset.filter(confidence__gte=80)
    if band == "75-79":
        return queryset.filter(confidence__gte=75, confidence__lte=79)
    if band == "70-74":
        return queryset.filter(confidence__gte=70, confidence__lte=74)
    if band == "65-69":
        return queryset.filter(confidence__gte=65, confidence__lte=69)
    if band == "Below 65":
        return queryset.filter(confidence__lt=65)
    return queryset.none()


def _date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
