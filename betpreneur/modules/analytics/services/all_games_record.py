"""Public record for settled all-games top market predictions."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Case, Count, IntegerField, Value, When

from betpreneur.modules.picks.models import AlgoRun, MarketPrediction
from betpreneur.modules.picks.services.presentation import (
    EXCLUDED_MARKETS,
    _apply_council_recommendation_gate,
    _game_market_rank,
    _rebalance_fixture_headline_markets,
    market_analysis_displayable,
    market_prediction_payload,
)
from betpreneur.modules.pricing.api import market_display_score, market_publicly_paused

SETTLED_RECORD_STATUSES = {MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS}


def build_all_games_record(target_date: date | None = None) -> dict:
    runs = _record_runs(target_date)
    records = _settled_top_market_records(runs)
    dates = _date_summaries(records)
    payload = {
        "overall": _summary(records),
        "dates": dates,
    }
    if target_date is not None:
        day_records = [record for record in records if record["date"] == target_date.isoformat()]
        payload.update(
            {
                "date": target_date.isoformat(),
                "summary": _summary(day_records),
                "games": day_records,
            }
        )
    return payload


def _record_runs(target_date: date | None):
    queryset = (
        AlgoRun.objects.filter(status=AlgoRun.Status.SUCCESS)
        .exclude(result__publish_policy="on_demand_fixture_analysis")
        .filter(market_predictions__status__in=SETTLED_RECORD_STATUSES)
        .annotate(
            fixture_total=Count("fixtures", distinct=True),
            policy_rank=Case(
                When(result__publish_policy__in=["strict_accuracy_gate", "celery_fanout_pipeline"], then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
        .only("id", "target_date", "created_at", "result")
        .order_by("target_date", "-policy_rank", "-fixture_total", "-created_at")
        .distinct()
    )
    if target_date is not None:
        queryset = queryset.filter(target_date=target_date)

    latest_by_date = {}
    for run in queryset:
        latest_by_date.setdefault(run.target_date, run)
    return list(latest_by_date.values())


def _settled_top_market_records(runs) -> list[dict]:
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    predictions = (
        MarketPrediction.objects.filter(run_id__in=run_ids)
        .exclude(market__in=EXCLUDED_MARKETS)
        .select_related("selected_pick")
        .only(
            "id",
            "run_id",
            "match_date",
            "fixture",
            "home_team",
            "away_team",
            "league",
            "kickoff",
            "match_id",
            "market",
            "meaning",
            "raw_confidence",
            "confidence",
            "odds",
            "odds_meta",
            "ev",
            "odds_source",
            "eligible",
            "published",
            "risk_flags",
            "insights",
            "selected_pick_id",
            "selected_pick__tier",
            "selected_pick__insights",
            "status",
            "score",
            "result",
        )
        .order_by("run_id", "match_id", "-published", "-confidence", "-ev", "market")
    )
    grouped = defaultdict(list)
    for prediction in predictions.iterator(chunk_size=1000):
        if not prediction.match_id or market_publicly_paused(prediction.market):
            continue
        payload = market_prediction_payload(prediction)
        payload["publicly_paused"] = False
        payload.update(_apply_council_recommendation_gate(payload))
        payload["display_score"] = round(market_display_score(payload)[0], 3)
        if market_analysis_displayable(payload):
            grouped[(prediction.run_id, str(prediction.match_id or ""))].append((prediction, payload))

    records = []
    for items in grouped.values():
        ranked = sorted(items, key=lambda item: _game_market_rank(item[1]), reverse=True)
        rebalanced_payloads = _rebalance_fixture_headline_markets([payload for _, payload in ranked])
        if not rebalanced_payloads:
            continue
        top_payload = rebalanced_payloads[0]
        top_prediction = next(prediction for prediction, payload in ranked if payload is top_payload)
        if top_prediction.status not in SETTLED_RECORD_STATUSES:
            continue
        records.append(_record_payload(top_prediction, top_payload))
    return sorted(records, key=lambda item: (item["date"], item["league"], item["kickoff"], item["game"]))


def _record_payload(prediction, payload) -> dict:
    return {
        "date": prediction.match_date.isoformat(),
        "game": prediction.fixture,
        "home_team": prediction.home_team,
        "away_team": prediction.away_team,
        "league": prediction.league,
        "kickoff": prediction.kickoff,
        "top_market": prediction.market,
        "meaning": prediction.meaning,
        "confidence": int(payload.get("final_confidence") or prediction.confidence or 0),
        "odds": _decimal_or_none(prediction.odds),
        "settlement": "WIN" if prediction.status == MarketPrediction.Status.WIN else "LOST",
        "settlement_detail": prediction.result,
    }


def _date_summaries(records: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for record in records:
        grouped[record["date"]].append(record)
    return [
        {"date": day, **_summary(day_records)}
        for day, day_records in sorted(grouped.items(), reverse=True)
    ]


def _summary(records: list[dict]) -> dict:
    wins = sum(1 for record in records if record["settlement"] == "WIN")
    losses = sum(1 for record in records if record["settlement"] == "LOST")
    total = wins + losses
    return {
        "total_games": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / total) * 100, 1) if total else 0.0,
    }


def _decimal_or_none(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
