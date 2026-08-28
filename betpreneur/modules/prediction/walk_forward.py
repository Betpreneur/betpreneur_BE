"""Walk-forward out-of-sample evaluation."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from django.db.models import QuerySet
from django.utils import timezone

from .contracts import PredictionDiagnostics, WalkForwardEvaluation, WalkForwardFold
from .models import PredictionTrainingSample

SETTLED_RESULTS = {"win", "loss", "void", "push"}
WIN_RESULTS = {"win"}
LOSS_RESULTS = {"loss"}
VOID_RESULTS = {"void", "push"}


def evaluate_walk_forward(
    *,
    market_family: str = "",
    league_key: str = "",
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    min_train_samples: int = 40,
    queryset: QuerySet | Iterable[PredictionTrainingSample] | None = None,
) -> WalkForwardEvaluation:
    """Evaluate frozen predictions one date at a time without future leakage."""
    samples = _samples(
        queryset if queryset is not None else PredictionTrainingSample.objects.all(),
        market_family=market_family,
        league_key=league_key,
        start_date=_date_or_none(start_date),
        end_date=_date_or_none(end_date),
    )
    if not samples:
        return WalkForwardEvaluation(
            diagnostics=PredictionDiagnostics(
                data_quality="unavailable",
                model_sources=("prediction.walk_forward",),
                warnings=("no_settled_prediction_samples",),
            )
        )

    folds = []
    leakage_warnings = []
    by_date = defaultdict(list)
    for sample in samples:
        by_date[_sample_date(sample)].append(sample)

    for prediction_date in sorted(by_date):
        train_cutoff = prediction_date - timedelta(days=1)
        train = [sample for sample in samples if _sample_date(sample) <= train_cutoff]
        test = by_date[prediction_date]
        fold = _fold(
            prediction_date, train_cutoff, train, test, min_train_samples=min_train_samples
        )
        folds.append(fold)
        leakage_warnings.extend(fold.leakage_warnings)

    valid_folds = [fold for fold in folds if fold.test_samples]
    total_test = sum(fold.test_samples for fold in valid_folds)
    total_wins = sum(fold.wins for fold in valid_folds)
    total_losses = sum(fold.losses for fold in valid_folds)
    total_train = max((fold.train_samples for fold in folds), default=0)
    brier_scores = [fold.brier_score for fold in valid_folds if fold.brier_score is not None]
    roi_values = [fold.roi for fold in valid_folds if fold.roi is not None]
    warnings = tuple(dict.fromkeys(leakage_warnings))
    return WalkForwardEvaluation(
        folds=tuple(folds),
        total_train_samples=total_train,
        total_test_samples=total_test,
        average_brier_score=round(sum(brier_scores) / len(brier_scores), 6)
        if brier_scores
        else None,
        actual_hit_rate=_rate(total_wins, total_wins + total_losses),
        roi=round(sum(roi_values) / len(roi_values), 6) if roi_values else None,
        leakage_warnings=warnings,
        diagnostics=PredictionDiagnostics(
            data_quality="strong" if total_test else "unavailable",
            model_sources=("prediction.walk_forward",),
            warnings=warnings,
            metadata={
                "fold_count": len(folds),
                "min_train_samples": min_train_samples,
                "market_family": market_family,
                "league_key": league_key,
                "process": "train_through_t_minus_1_predict_t_settle_t_update_to_t_plus_1",
            },
        ),
    )


def _samples(
    queryset: QuerySet | Iterable[PredictionTrainingSample],
    *,
    market_family: str,
    league_key: str,
    start_date: date | None,
    end_date: date | None,
) -> list[PredictionTrainingSample]:
    if isinstance(queryset, QuerySet):
        queryset = queryset.filter(settlement_result__in=SETTLED_RESULTS)
        if market_family:
            queryset = queryset.filter(market_family=market_family)
        if league_key:
            queryset = queryset.filter(league_key=league_key)
        if start_date:
            queryset = queryset.filter(kickoff__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(kickoff__date__lte=end_date)
        return list(queryset.order_by("kickoff", "prediction_created_at", "id"))

    samples = [
        sample
        for sample in queryset
        if sample.settlement_result in SETTLED_RESULTS
        and (not market_family or sample.market_family == market_family)
        and (not league_key or sample.league_key == league_key)
        and (not start_date or _sample_date(sample) >= start_date)
        and (not end_date or _sample_date(sample) <= end_date)
    ]
    return sorted(
        samples,
        key=lambda sample: (_sample_datetime(sample), sample.prediction_created_at, sample.id or 0),
    )


def _fold(
    prediction_date: date,
    train_cutoff: date,
    train: list[PredictionTrainingSample],
    test: list[PredictionTrainingSample],
    *,
    min_train_samples: int,
) -> WalkForwardFold:
    leakage_warnings = []
    if len(train) < min_train_samples:
        leakage_warnings.append("insufficient_prior_training_samples")
    valid_test = []
    skipped_voids = 0
    for sample in test:
        if _has_prediction_leakage(sample):
            leakage_warnings.append("prediction_created_after_kickoff")
            continue
        if sample.settlement_result in VOID_RESULTS:
            skipped_voids += 1
            continue
        valid_test.append(sample)

    wins = sum(1 for sample in valid_test if sample.settlement_result in WIN_RESULTS)
    losses = sum(1 for sample in valid_test if sample.settlement_result in LOSS_RESULTS)
    probabilities = [
        _probability(sample) for sample in valid_test if _probability(sample) is not None
    ]
    return WalkForwardFold(
        prediction_date=prediction_date.isoformat(),
        train_through=train_cutoff.isoformat(),
        train_samples=len(train),
        test_samples=len(valid_test),
        wins=wins,
        losses=losses,
        voids=skipped_voids,
        average_predicted_probability=round(sum(probabilities) / len(probabilities), 6)
        if probabilities
        else None,
        actual_hit_rate=_rate(wins, wins + losses),
        brier_score=_brier_score(valid_test),
        roi=_roi(valid_test),
        leakage_warnings=tuple(dict.fromkeys(leakage_warnings)),
    )


def _has_prediction_leakage(sample: PredictionTrainingSample) -> bool:
    kickoff = _sample_datetime(sample)
    created_at = sample.prediction_created_at
    if kickoff is None or created_at is None:
        return False
    return created_at > kickoff


def _brier_score(samples: list[PredictionTrainingSample]) -> float | None:
    terms = []
    for sample in samples:
        probability = _probability(sample)
        if probability is None:
            continue
        outcome = 1.0 if sample.settlement_result in WIN_RESULTS else 0.0
        terms.append((probability - outcome) ** 2)
    return round(sum(terms) / len(terms), 6) if terms else None


def _roi(samples: list[PredictionTrainingSample]) -> float | None:
    returns = []
    for sample in samples:
        odds = float(sample.real_odds) if sample.real_odds is not None else None
        if odds is None:
            continue
        returns.append(odds - 1.0 if sample.settlement_result in WIN_RESULTS else -1.0)
    return round(sum(returns) / len(returns), 6) if returns else None


def _probability(sample: PredictionTrainingSample) -> float | None:
    score = sample.first_prediction_score
    if score is None:
        score = sample.last_prediction_score
    if score is None:
        return None
    return max(0.0, min(1.0, float(score) / 100.0))


def _rate(wins: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(wins / total, 6)


def _sample_date(sample: PredictionTrainingSample) -> date:
    value = _sample_datetime(sample)
    if value is None:
        return timezone.localdate(sample.prediction_created_at or timezone.now())
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()


def _sample_datetime(sample: PredictionTrainingSample) -> datetime | None:
    return sample.kickoff or sample.prediction_created_at


def _date_or_none(value: date | str | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
