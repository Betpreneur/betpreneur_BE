"""Stage 20: daily model-health metrics.

Read-only aggregation over everything below. Analytics never writes to another
module's tables and never decides anything — it reports.

Two things about the design are deliberate:

* **Every metric declares its own availability.** A dashboard that shows "ROI:
  0.0%" when the truth is "nothing has settled yet" is worse than useless, so a
  metric with no data reports ``no_data`` and a sample size rather than a
  confident zero.
* **Nothing here silently substitutes one source for another.** Where a metric
  is computed from the live pick pipeline rather than the training-sample
  dataset, the metric says so in ``source``, because the two do not always
  agree and a reader needs to know which one they are looking at.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from django.db.models import Count, Q
from django.utils import timezone

from betpreneur.modules.picks.api import MarketPrediction, Pick

DEFAULT_WINDOW_DAYS = 30

#: Probability buckets for the calibration curve, as (low, high] percentages.
CONFIDENCE_BANDS: tuple[tuple[int, int], ...] = (
    (0, 50),
    (50, 60),
    (60, 70),
    (70, 80),
    (80, 90),
    (90, 100),
)

#: Decimal-odds buckets. The short-odds band is where the calibration report
#: found the worst overconfidence, so it is deliberately narrow.
ODDS_BANDS: tuple[tuple[str, float, float], ...] = (
    ("1.00-1.29", 1.00, 1.30),
    ("1.30-1.49", 1.30, 1.50),
    ("1.50-1.99", 1.50, 2.00),
    ("2.00-2.99", 2.00, 3.00),
    ("3.00+", 3.00, float("inf")),
)


class Availability(StrEnum):
    OK = "ok"
    THIN = "thin"  # computed, but on too few rows to trust
    NO_DATA = "no_data"  # nothing to compute from
    UNAVAILABLE = "unavailable"  # the source itself is missing


#: Below this, a metric is reported but flagged as thin.
MIN_TRUSTWORTHY_SAMPLE = 30


@dataclass(frozen=True, slots=True)
class Metric:
    """One number, plus enough context to know whether to believe it."""

    key: str
    label: str
    value: float | None = None
    unit: str = ""
    sample_size: int = 0
    availability: Availability = Availability.NO_DATA
    source: str = ""
    breakdown: tuple[dict[str, Any], ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "sample_size": self.sample_size,
            "availability": str(self.availability),
            "source": self.source,
            "breakdown": [dict(row) for row in self.breakdown],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ModelHealthReport:
    window_days: int
    window_from: date
    window_to: date
    metrics: tuple[Metric, ...] = field(default_factory=tuple)

    def get(self, key: str) -> Metric | None:
        return next((m for m in self.metrics if m.key == key), None)

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Metrics a reader should not draw conclusions from."""
        return tuple(
            m.key
            for m in self.metrics
            if m.availability in {Availability.NO_DATA, Availability.UNAVAILABLE}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "window_from": self.window_from.isoformat(),
            "window_to": self.window_to.isoformat(),
            "unavailable": list(self.unavailable),
            "metrics": [m.to_dict() for m in self.metrics],
        }


def _availability(sample: int) -> Availability:
    if not sample:
        return Availability.NO_DATA
    return Availability.OK if sample >= MIN_TRUSTWORTHY_SAMPLE else Availability.THIN


def _pct(part: float, whole: float) -> float | None:
    return round((part / whole) * 100, 2) if whole else None


def _roi(rows: Iterable[tuple[float | None, str]]) -> tuple[float | None, int]:
    """ROI as a percentage of turnover, counting one unit staked per settled row.

    Voids are excluded rather than counted as losses — a pushed bet returns the
    stake, so including it would drag ROI toward zero and understate the edge.
    """
    staked = 0
    returned = 0.0
    for odds, status in rows:
        if status == Pick.Status.VOID:
            continue
        staked += 1
        if status == Pick.Status.WIN and odds:
            returned += float(odds)
    if not staked:
        return None, 0
    return round(((returned - staked) / staked) * 100, 2), staked


class ModelHealthService:
    """Computes the Stage 20 metrics for a trailing window."""

    def report(
        self, *, window_days: int = DEFAULT_WINDOW_DAYS, today: date | None = None
    ) -> ModelHealthReport:
        end = today or date.today()
        start = end - timedelta(days=window_days)
        metrics = (
            *self._calibration_metrics(start, end),
            self._roi_by_market(start, end),
            self._roi_by_odds_band(start, end),
            self._roi_by_tier(start, end),
            *self._odds_quality_metrics(start, end),
            self._duplicate_prediction_rate(start, end),
            self._settlement_lag(start, end),
            self._league_coverage(start, end),
            *self._strategy_metrics(end),
        )
        return ModelHealthReport(
            window_days=window_days, window_from=start, window_to=end, metrics=metrics
        )

    # -- probability vs actual --------------------------------------------
    def _calibration_metrics(self, start: date, end: date) -> tuple[Metric, ...]:
        """Raw and calibrated confidence against what actually happened.

        MarketPrediction carries both `raw_confidence` and `confidence`, which
        is the live pipeline's raw-vs-calibrated pair. The Stage 8 training
        dataset would be the better source once it is being written.
        """
        rows = list(
            MarketPrediction.objects.filter(
                match_date__gte=start,
                match_date__lte=end,
                status__in=[MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS],
            ).values("raw_confidence", "confidence", "status")
        )
        return (
            self._calibration_curve(
                rows, "raw_confidence", "raw_probability_vs_actual", "Raw confidence vs actual"
            ),
            self._calibration_curve(
                rows,
                "confidence",
                "calibrated_probability_vs_actual",
                "Calibrated confidence vs actual",
            ),
        )

    def _calibration_curve(self, rows, field_name: str, key: str, label: str) -> Metric:
        buckets: dict[tuple[int, int], list[int]] = {b: [0, 0] for b in CONFIDENCE_BANDS}
        for row in rows:
            stated = row.get(field_name)
            if stated is None:
                continue
            band = next((b for b in CONFIDENCE_BANDS if b[0] <= float(stated) < b[1]), None)
            if band is None:
                band = CONFIDENCE_BANDS[-1]
            buckets[band][0] += 1
            if row["status"] == MarketPrediction.Status.WIN:
                buckets[band][1] += 1

        breakdown = []
        total = 0
        weighted_gap = 0.0
        for (low, high), (attempts, wins) in buckets.items():
            if not attempts:
                continue
            actual = (wins / attempts) * 100
            stated_mid = (low + high) / 2
            breakdown.append(
                {
                    "band": f"{low}-{high}",
                    "stated_midpoint": stated_mid,
                    "actual_hit_rate": round(actual, 2),
                    "gap": round(actual - stated_mid, 2),
                    "sample_size": attempts,
                }
            )
            total += attempts
            weighted_gap += (actual - stated_mid) * attempts

        return Metric(
            key=key,
            label=label,
            value=round(weighted_gap / total, 2) if total else None,
            unit="percentage points",
            sample_size=total,
            availability=_availability(total),
            source="picks.MarketPrediction",
            breakdown=tuple(breakdown),
            note="Negative means the stated confidence was higher than reality.",
        )

    # -- ROI ---------------------------------------------------------------
    def _settled_picks(self, start: date, end: date):
        return Pick.objects.filter(
            match_date__gte=start,
            match_date__lte=end,
            status__in=[Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID],
        )

    def _roi_grouped(self, start: date, end: date, group: str, key: str, label: str) -> Metric:
        rows = list(self._settled_picks(start, end).values(group, "odds", "status"))
        groups: dict[str, list[tuple[float | None, str]]] = {}
        for row in rows:
            groups.setdefault(str(row[group] or "unknown"), []).append((row["odds"], row["status"]))
        breakdown = []
        for name, entries in sorted(groups.items()):
            roi, staked = _roi(entries)
            if not staked:
                continue
            breakdown.append({group: name, "roi_percent": roi, "settled": staked})
        overall, total = _roi([(r["odds"], r["status"]) for r in rows])
        breakdown.sort(key=lambda r: (r["roi_percent"] is None, -(r["roi_percent"] or 0)))
        return Metric(
            key=key,
            label=label,
            value=overall,
            unit="%",
            sample_size=total,
            availability=_availability(total),
            source="picks.Pick",
            breakdown=tuple(breakdown),
        )

    def _roi_by_market(self, start, end):
        return self._roi_grouped(start, end, "market", "roi_by_market", "ROI by market")

    def _roi_by_tier(self, start, end):
        return self._roi_grouped(start, end, "tier", "roi_by_tier", "ROI by tier")

    def _roi_by_odds_band(self, start: date, end: date) -> Metric:
        rows = list(self._settled_picks(start, end).values("odds", "status"))
        bands: dict[str, list[tuple[float | None, str]]] = {b[0]: [] for b in ODDS_BANDS}
        for row in rows:
            odds = float(row["odds"]) if row["odds"] is not None else None
            if odds is None:
                continue
            band = next((b[0] for b in ODDS_BANDS if b[1] <= odds < b[2]), ODDS_BANDS[-1][0])
            bands[band].append((row["odds"], row["status"]))
        breakdown = []
        for name, entries in bands.items():
            roi, staked = _roi(entries)
            if not staked:
                continue
            breakdown.append({"odds_band": name, "roi_percent": roi, "settled": staked})
        overall, total = _roi([(r["odds"], r["status"]) for r in rows])
        return Metric(
            key="roi_by_odds_band",
            label="ROI by odds band",
            value=overall,
            unit="%",
            sample_size=total,
            availability=_availability(total),
            source="picks.Pick",
            breakdown=tuple(breakdown),
        )

    # -- odds quality ------------------------------------------------------
    def _odds_quality_metrics(self, start: date, end: date) -> tuple[Metric, ...]:
        qs = MarketPrediction.objects.filter(match_date__gte=start, match_date__lte=end)
        total = qs.count()
        real = qs.exclude(Q(odds_source="") | Q(odds_source__iexact="estimated")).count()
        estimated = total - real
        by_source = [
            {"odds_source": row["odds_source"] or "unset", "count": row["n"]}
            for row in qs.values("odds_source").annotate(n=Count("id")).order_by("-n")
        ]
        return (
            Metric(
                key="real_odds_coverage",
                label="Real odds coverage",
                value=_pct(real, total),
                unit="%",
                sample_size=total,
                availability=_availability(total),
                source="picks.MarketPrediction",
                breakdown=tuple(by_source),
            ),
            Metric(
                key="estimated_odds_usage",
                label="Estimated odds usage",
                value=_pct(estimated, total),
                unit="%",
                sample_size=total,
                availability=_availability(total),
                source="picks.MarketPrediction",
                note="ROI computed on estimated odds is not comparable to real-odds ROI.",
            ),
        )

    # -- pipeline hygiene --------------------------------------------------
    def _duplicate_prediction_rate(self, start: date, end: date) -> Metric:
        """How often the same fixture/market is predicted in more than one run.

        Grouped across runs, not within one: MarketPrediction already carries a
        unique constraint on (run, match_id, fixture, market), so duplication
        inside a single run cannot happen. What can happen is a rerun producing
        a second row for the same fixture and market — which is the duplication
        that skews a training set, and what Stage 8's dedupe rule is for.
        """
        qs = (
            MarketPrediction.objects.filter(match_date__gte=start, match_date__lte=end)
            .values("match_date", "match_id", "fixture", "market")
            .annotate(n=Count("id"))
        )
        rows = list(qs)
        groups = len(rows)
        duplicated = sum(row["n"] - 1 for row in rows if row["n"] > 1)
        total = sum(row["n"] for row in rows)
        return Metric(
            key="duplicate_prediction_rate",
            label="Duplicate prediction rate",
            value=_pct(duplicated, total),
            unit="%",
            sample_size=total,
            availability=_availability(total),
            source="picks.MarketPrediction",
            note=f"{duplicated} rerun rows across {groups} fixture/market pairs.",
        )

    def _settlement_lag(self, start: date, end: date) -> Metric:
        """Hours from the match date to settlement.

        Measured from match_date rather than kickoff time, because kickoff is
        stored as free text on Pick and cannot be relied on for arithmetic.

        settled_at is converted with localtime() first: calling .date() on an
        aware datetime resolves in UTC, which in Africa/Lagos would report a
        settlement just after local midnight as having happened the day before.
        """
        rows = list(
            self._settled_picks(start, end)
            .exclude(settled_at__isnull=True)
            .values("match_date", "settled_at")
        )
        lags = [
            (timezone.localtime(row["settled_at"]).date() - row["match_date"]).total_seconds()
            / 3600
            for row in rows
            if row["match_date"] and row["settled_at"]
        ]
        lags = [lag for lag in lags if lag >= 0]
        return Metric(
            key="settlement_lag",
            label="Median settlement lag",
            value=round(sorted(lags)[len(lags) // 2], 1) if lags else None,
            unit="hours",
            sample_size=len(lags),
            availability=_availability(len(lags)),
            source="picks.Pick",
        )

    def _league_coverage(self, start: date, end: date) -> Metric:
        """What share of leagues we published picks for are actually hydrated."""
        try:
            from betpreneur.modules.catalog.api import DataCoverage
        except ImportError:  # pragma: no cover - catalog always present in practice
            return Metric(
                key="league_coverage",
                label="League coverage",
                availability=Availability.UNAVAILABLE,
                source="catalog.DataCoverage",
            )
        leagues = set(
            self._settled_picks(start, end).values_list("league", flat=True).distinct()
        ) - {"", None}
        covered = set(
            DataCoverage.objects.filter(
                status__in=[DataCoverage.Status.FRESH, DataCoverage.Status.PARTIAL]
            ).values_list("league_key", flat=True)
        )
        matched = {lg for lg in leagues if lg in covered}
        return Metric(
            key="league_coverage",
            label="League coverage",
            value=_pct(len(matched), len(leagues)),
            unit="%",
            sample_size=len(leagues),
            availability=_availability(len(leagues)),
            source="catalog.DataCoverage",
            breakdown=tuple({"league": lg, "covered": lg in covered} for lg in sorted(leagues)),
        )

    # -- strategy actions --------------------------------------------------
    def _strategy_metrics(self, end: date) -> tuple[Metric, ...]:
        """Did suppression avoid losses, and did promotion improve ROI?

        Both read Stage 18's next-period evaluation rather than the trigger
        window, which is the whole point of that redesign: a decision is judged
        by what happened after it, not by the history that prompted it.
        """
        from ..models import StrategyActionOutcome

        def summarise(action: str, key: str, label: str, note: str) -> Metric:
            rows = list(
                StrategyActionOutcome.objects.filter(action=action, evaluated_to__lte=end).values(
                    "roi", "roi_delta", "baseline_roi", "sample_size"
                )
            )
            deltas = [r["roi_delta"] for r in rows if r["roi_delta"] is not None]
            return Metric(
                key=key,
                label=label,
                value=round(sum(deltas) / len(deltas), 2) if deltas else None,
                unit="ROI points",
                sample_size=len(rows),
                availability=_availability(len(rows)),
                source="analytics.StrategyActionOutcome",
                note=note,
            )

        return (
            summarise(
                StrategyActionOutcome.Action.SUPPRESS,
                "market_suppression_effectiveness",
                "Market suppression effectiveness",
                "Negative delta means suppressed markets did worse afterwards — the call was right.",
            ),
            summarise(
                StrategyActionOutcome.Action.PROMOTE,
                "promotion_next_period_performance",
                "Promotion next-period performance",
                "Positive delta means promoted markets improved afterwards. Near zero means promotion is chasing noise.",
            ),
        )


model_health_service = ModelHealthService()
