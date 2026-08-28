"""Write path for the canonical calibration dataset."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from django.utils import timezone

from betpreneur.modules.markets.api import describe_market

from .contracts import TrainingSampleRecord
from .models import PredictionTrainingSample

PENDING_RESULTS = {"", "pending", "open", "unsettled", "in_progress", "started"}
VOID_RESULTS = {"void", "push", "refund", "refunded", "cancelled", "canceled"}


def record_training_sample(sample: TrainingSampleRecord | None = None, **kwargs) -> PredictionTrainingSample | None:
    """Persist one settled calibration row, deduping reruns.

    The uniqueness key is fixture + canonical market + line + side. Repeated
    reruns keep the first prediction score and timestamp, then update the last
    prediction score and latest settlement/odds metadata.
    """
    record = sample or TrainingSampleRecord(**kwargs)
    record = _normalize_record(record)
    if record.settlement_result in PENDING_RESULTS:
        return None

    defaults = {
        "last_prediction_score": record.last_prediction_score,
        "selected_status": record.selected_status,
        "published_status": record.published_status,
        "odds_source": record.odds_source,
        "real_odds": record.real_odds,
        "estimated_odds": record.estimated_odds,
        "settlement_result": record.settlement_result,
        "market_family": record.market_family,
        "league_key": record.league_key,
        "season": record.season,
        "kickoff": record.kickoff,
        "last_prediction_created_at": record.prediction_created_at,
        "source": record.source,
        "source_reference": record.source_reference,
        "metadata": record.metadata,
    }
    obj, created = PredictionTrainingSample.objects.get_or_create(
        fixture_id=record.fixture_id,
        canonical_market=record.canonical_market,
        line=record.line,
        side=record.side,
        defaults={
            **defaults,
            "first_prediction_score": record.first_prediction_score,
            "prediction_created_at": record.prediction_created_at,
        },
    )
    if created:
        return obj

    changed = False
    if _is_earlier(record.prediction_created_at, obj.prediction_created_at):
        obj.prediction_created_at = record.prediction_created_at
        obj.first_prediction_score = record.first_prediction_score
        changed = True
    if record.last_prediction_score != obj.last_prediction_score:
        obj.last_prediction_score = record.last_prediction_score
        changed = True
    for field, value in defaults.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed = True
    if changed:
        obj.save()
    return obj


def _normalize_record(record: TrainingSampleRecord) -> TrainingSampleRecord:
    descriptor = describe_market(record.canonical_market)
    canonical = descriptor.canonical or record.canonical_market
    line = str(record.line if record.line not in (None, "") else descriptor.line or "").strip()
    side = str(record.side if record.side not in (None, "") else descriptor.side or "").strip().lower()
    market_family = record.market_family or descriptor.family
    real_odds = _decimal_or_none(record.real_odds)
    if record.estimated_odds:
        real_odds = None
    settlement_result = _normalize_result(record.settlement_result)
    prediction_created_at = record.prediction_created_at or timezone.now()
    first_prediction_score = record.first_prediction_score
    last_prediction_score = record.last_prediction_score if record.last_prediction_score is not None else first_prediction_score
    return replace(
        record,
        fixture_id=str(record.fixture_id).strip(),
        canonical_market=canonical.strip(),
        line=line,
        side=side,
        market_family=market_family,
        real_odds=real_odds,
        settlement_result=settlement_result,
        prediction_created_at=prediction_created_at,
        first_prediction_score=first_prediction_score,
        last_prediction_score=last_prediction_score,
    )


def _normalize_result(value: str) -> str:
    result = str(value or "").strip().lower()
    if result in VOID_RESULTS:
        return "void" if result != "push" else "push"
    return result


def _decimal_or_none(value) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _is_earlier(candidate, current) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate < current
