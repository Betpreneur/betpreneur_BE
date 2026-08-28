"""Stage 21: the public reporting dataset.

One source of truth for anything published outwardly — the transparency report,
the public record endpoint, a spreadsheet a human exports. Built so that the
numbers cannot be quietly wrong.

Four rules shape the whole module:

* **Settled only.** A pending pick has no result. It is excluded from the
  dataset rather than counted as anything, so no figure here can drift as
  results land.
* **Real odds are separated, never blended.** Headline ROI is computed *only*
  from picks priced with real bookmaker odds. Estimated-odds ROI is reported
  beside it and marked non-comparable, because a return computed against a
  price nobody could have taken is not a return.
* **Voids are declared, not absorbed.** A void returns the stake. Counting it
  as a loss understates the edge; counting it as a win overstates it. It leaves
  the ROI denominator and is reported as its own number.
* **The dataset is frozen and identified.** Every build stamps ``frozen_at``
  and a ``dataset_id`` derived from the content, so a published figure can be
  traced back to the exact rows that produced it.

Provenance is a three-way answer, never a two-way one. A pick whose prediction
row cannot be found is ``unknown`` — not assumed real. ``Pick`` itself carries
no odds provenance; it lives on ``MarketPrediction`` and is joined on the
``(run, match_id, fixture, market)`` unique key.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from django.utils import timezone

from betpreneur.modules.picks.api import MarketPrediction, Pick

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365

#: Below this many settled picks a rate is reported but must not be presented
#: as evidence of an edge.
MIN_REPORTABLE_SAMPLE = 30

CONFIDENCE_BANDS: tuple[tuple[int, int], ...] = (
    (50, 60), (60, 70), (70, 80), (80, 90), (90, 101),
)


class Provenance(StrEnum):
    """Where a pick's odds came from. ``UNKNOWN`` is a real answer."""

    REAL = "real"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class Outcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    VOID = "void"


_STATUS_TO_OUTCOME = {
    Pick.Status.WIN: Outcome.WIN,
    Pick.Status.LOSS: Outcome.LOSS,
    Pick.Status.VOID: Outcome.VOID,
}

#: ``MarketPrediction.odds_source`` is written as ``"api_football"`` for a real
#: quote and ``"estimated"`` for a modelled one. Blank means the writer never
#: recorded it, which is not the same as real.
_ESTIMATED_SOURCE = "estimated"


@dataclass(frozen=True, slots=True)
class Record:
    """One settled pick, as published."""

    pick_id: int
    match_date: date | None
    fixture: str
    league: str
    market: str
    tier: str
    confidence: int
    odds: float
    provenance: Provenance
    odds_source: str
    outcome: Outcome
    stake: float
    pnl: float
    settled_at: str = ""

    @property
    def counts_toward_roi(self) -> bool:
        """Voids return the stake, so they are not a priced outcome."""
        return self.outcome is not Outcome.VOID

    def as_dict(self) -> dict[str, Any]:
        return {
            "pick_id": self.pick_id,
            "match_date": self.match_date.isoformat() if self.match_date else None,
            "fixture": self.fixture,
            "league": self.league,
            "market": self.market,
            "tier": self.tier,
            "confidence": self.confidence,
            "odds": self.odds,
            "odds_provenance": str(self.provenance),
            "odds_source": self.odds_source,
            "outcome": str(self.outcome),
            "stake": self.stake,
            "pnl": self.pnl,
            "settled_at": self.settled_at,
        }


@dataclass(frozen=True, slots=True)
class RoiBlock:
    """A return figure that always carries the basis it was computed on."""

    provenance: Provenance
    picks: int = 0
    wins: int = 0
    losses: int = 0
    stake: float = 0.0
    pnl: float = 0.0
    comparable: bool = True
    note: str = ""

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def hit_rate(self) -> float | None:
        return round(self.wins / self.settled * 100, 1) if self.settled else None

    @property
    def roi(self) -> float | None:
        return round(self.pnl / self.stake * 100, 1) if self.stake else None

    @property
    def reportable(self) -> bool:
        return self.settled >= MIN_REPORTABLE_SAMPLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "basis": str(self.provenance),
            "picks": self.picks,
            "wins": self.wins,
            "losses": self.losses,
            "hit_rate": self.hit_rate,
            "roi": self.roi,
            "stake": round(self.stake, 2),
            "pnl": round(self.pnl, 2),
            "comparable": self.comparable,
            "reportable": self.reportable,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CalibrationBand:
    """Stated confidence against what actually happened."""

    lower: int
    upper: int
    picks: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def stated(self) -> float:
        return round((self.lower + min(self.upper, 100)) / 2, 1)

    @property
    def actual(self) -> float | None:
        return round(self.wins / self.settled * 100, 1) if self.settled else None

    @property
    def drift(self) -> float | None:
        actual = self.actual
        return None if actual is None else round(actual - self.stated, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "band": f"{self.lower}-{min(self.upper, 100)}",
            "stated_confidence": self.stated,
            "actual_hit_rate": self.actual,
            "drift": self.drift,
            "picks": self.picks,
            "settled": self.settled,
            "reportable": self.settled >= MIN_REPORTABLE_SAMPLE,
        }


@dataclass(frozen=True, slots=True)
class VoidBlock:
    """Voids get their own statement rather than vanishing into a rate."""

    voids: int = 0
    total: int = 0

    @property
    def rate(self) -> float | None:
        return round(self.voids / self.total * 100, 1) if self.total else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "voids": self.voids,
            "void_rate": self.rate,
            "policy": (
                "A void returns the stake. Voids are excluded from the ROI "
                "denominator and from hit rate, and are reported here instead."
            ),
        }


@dataclass(frozen=True, slots=True)
class PublicDataset:
    """A frozen, self-describing published dataset."""

    frozen_at: str
    window_days: int
    window_start: date
    window_end: date
    records: tuple[Record, ...] = ()
    headline: RoiBlock = field(default_factory=lambda: RoiBlock(Provenance.REAL))
    by_provenance: tuple[RoiBlock, ...] = ()
    calibration: tuple[CalibrationBand, ...] = ()
    voids: VoidBlock = field(default_factory=VoidBlock)
    duplicates_removed: int = 0
    pending_excluded: int = 0
    dataset_id: str = ""

    @property
    def caveats(self) -> tuple[str, ...]:
        """Everything a reader must be told before quoting a number."""
        out: list[str] = []
        if not self.headline.reportable:
            out.append(
                f"Headline ROI rests on {self.headline.settled} settled real-odds "
                f"picks, below the {MIN_REPORTABLE_SAMPLE} needed to be meaningful."
            )
        for block in self.by_provenance:
            if block.provenance is Provenance.ESTIMATED and block.picks:
                out.append(
                    f"{block.picks} picks were priced with estimated odds and are "
                    "excluded from headline ROI."
                )
            if block.provenance is Provenance.UNKNOWN and block.picks:
                out.append(
                    f"{block.picks} picks have no recorded odds provenance and are "
                    "excluded from headline ROI."
                )
        if self.voids.voids:
            out.append(f"{self.voids.voids} picks were voided and returned the stake.")
        return tuple(out)

    def as_dict(self, *, include_records: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "frozen_at": self.frozen_at,
            "window": {
                "days": self.window_days,
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "headline": self.headline.as_dict(),
            "by_odds_provenance": [b.as_dict() for b in self.by_provenance],
            "calibration": [b.as_dict() for b in self.calibration],
            "voids": self.voids.as_dict(),
            "hygiene": {
                "records_published": len(self.records),
                "duplicates_removed": self.duplicates_removed,
                "pending_excluded": self.pending_excluded,
                "settled_only": True,
            },
            "caveats": list(self.caveats),
        }
        if include_records:
            payload["records"] = [r.as_dict() for r in self.records]
        return payload


class PublicDatasetService:
    """Builds the published dataset. Read-only, deterministic, no side effects."""

    def build(
        self,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        today: date | None = None,
    ) -> PublicDataset:
        window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
        end = today or timezone.localdate()
        start = end - timedelta(days=window_days)

        picks, duplicates_removed = self._deduplicated(start, end)
        pending_excluded = self._pending_count(start, end)
        provenance = self._provenance_for(picks)
        records = tuple(self._record(p, provenance) for p in picks)

        by_provenance = self._roi_blocks(records)
        headline = next(
            (b for b in by_provenance if b.provenance is Provenance.REAL),
            RoiBlock(Provenance.REAL),
        )
        dataset = PublicDataset(
            frozen_at=timezone.now().isoformat(),
            window_days=window_days,
            window_start=start,
            window_end=end,
            records=records,
            headline=headline,
            by_provenance=by_provenance,
            calibration=self._calibration(records),
            voids=VoidBlock(
                voids=sum(1 for r in records if r.outcome is Outcome.VOID),
                total=len(records),
            ),
            duplicates_removed=duplicates_removed,
            pending_excluded=pending_excluded,
        )
        return self._stamp(dataset)

    # -- sourcing ---------------------------------------------------------

    def _settled_queryset(self, start: date, end: date):
        return Pick.objects.filter(
            status__in=[Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID],
            match_date__gte=start,
            match_date__lte=end,
        ).select_related("run")

    def _pending_count(self, start: date, end: date) -> int:
        return Pick.objects.filter(
            status=Pick.Status.PENDING,
            match_date__gte=start,
            match_date__lte=end,
        ).count()

    def _deduplicated(self, start: date, end: date) -> tuple[list[Pick], int]:
        """Collapse republished copies of the same pick.

        Ordering is fully specified down to ``id`` so that two builds over
        unchanged data return the same rows in the same order — a dataset that
        reshuffles cannot be checksummed.
        """
        rows = list(
            self._settled_queryset(start, end).order_by(
                "-match_date", "-run__target_date", "-created_at", "-id"
            )
        )
        latest: dict[tuple, Pick] = {}
        for pick in rows:
            key = (
                pick.match_date,
                str(pick.match_id or "").strip(),
                pick.fixture,
                pick.market,
            )
            if key not in latest:
                latest[key] = pick
        return list(latest.values()), len(rows) - len(latest)

    def _provenance_for(self, picks: Sequence[Pick]) -> dict[tuple, str]:
        """Odds provenance lives on ``MarketPrediction``, not on ``Pick``.

        Joined on the ``(run, match_id, fixture, market)`` unique key, so a pick
        matches at most one prediction. A miss stays absent and is read as
        ``unknown`` rather than defaulting to real.
        """
        if not picks:
            return {}
        rows = MarketPrediction.objects.filter(
            run_id__in={p.run_id for p in picks},
            fixture__in={p.fixture for p in picks},
            market__in={p.market for p in picks},
        ).values_list("run_id", "match_id", "fixture", "market", "odds_source")
        return {
            (run_id, str(match_id or "").strip(), fixture, market): source
            for run_id, match_id, fixture, market, source in rows
        }

    def _record(self, pick: Pick, provenance: dict[tuple, str]) -> Record:
        key = (pick.run_id, str(pick.match_id or "").strip(), pick.fixture, pick.market)
        source = provenance.get(key)
        if source is None or not str(source).strip():
            kind = Provenance.UNKNOWN
        elif str(source).strip().lower() == _ESTIMATED_SOURCE:
            kind = Provenance.ESTIMATED
        else:
            kind = Provenance.REAL
        return Record(
            pick_id=pick.id,
            match_date=pick.match_date,
            fixture=pick.fixture,
            league=pick.league,
            market=pick.market,
            tier=str(pick.tier or ""),
            confidence=int(pick.confidence or 0),
            odds=float(pick.odds or 0),
            provenance=kind,
            odds_source=str(source or ""),
            outcome=_STATUS_TO_OUTCOME[pick.status],
            stake=float(pick.stake or 0),
            pnl=float(pick.pnl or 0),
            settled_at=(
                timezone.localtime(pick.settled_at).isoformat()
                if pick.settled_at
                else ""
            ),
        )

    # -- aggregation ------------------------------------------------------

    def _roi_blocks(self, records: Iterable[Record]) -> tuple[RoiBlock, ...]:
        buckets: dict[Provenance, list[Record]] = {p: [] for p in Provenance}
        for record in records:
            buckets[record.provenance].append(record)

        notes = {
            Provenance.REAL: "Priced with real bookmaker odds. This is the headline figure.",
            Provenance.ESTIMATED: (
                "Priced with modelled odds. Not comparable to real-odds ROI and "
                "never combined with it."
            ),
            Provenance.UNKNOWN: (
                "No odds provenance recorded. Excluded from headline ROI because "
                "the price cannot be verified."
            ),
        }
        blocks = []
        for kind in Provenance:
            rows = buckets[kind]
            priced = [r for r in rows if r.counts_toward_roi]
            blocks.append(
                RoiBlock(
                    provenance=kind,
                    picks=len(rows),
                    wins=sum(1 for r in priced if r.outcome is Outcome.WIN),
                    losses=sum(1 for r in priced if r.outcome is Outcome.LOSS),
                    stake=sum(r.stake for r in priced),
                    pnl=sum(r.pnl for r in priced),
                    comparable=kind is Provenance.REAL,
                    note=notes[kind],
                )
            )
        return tuple(blocks)

    def _calibration(self, records: Iterable[Record]) -> tuple[CalibrationBand, ...]:
        """Stated confidence against realised hit rate, real odds only.

        Voids are excluded: they resolve to neither outcome, so including them
        would drag every band toward zero regardless of model quality.
        """
        rows = [
            r for r in records
            if r.provenance is Provenance.REAL and r.counts_toward_roi
        ]
        bands = []
        for lower, upper in CONFIDENCE_BANDS:
            inside = [r for r in rows if lower <= r.confidence < upper]
            bands.append(
                CalibrationBand(
                    lower=lower,
                    upper=upper,
                    picks=len(inside),
                    wins=sum(1 for r in inside if r.outcome is Outcome.WIN),
                    losses=sum(1 for r in inside if r.outcome is Outcome.LOSS),
                )
            )
        return tuple(bands)

    # -- freeze -----------------------------------------------------------

    def _stamp(self, dataset: PublicDataset) -> PublicDataset:
        """Derive a content hash so a published figure is traceable.

        The hash covers the rows and the window, never ``frozen_at`` — two
        builds over unchanged data must agree on the id, so that a changed id
        means the underlying data actually moved.
        """
        payload = json.dumps(
            {
                "window": [
                    dataset.window_start.isoformat(),
                    dataset.window_end.isoformat(),
                ],
                "records": [r.as_dict() for r in dataset.records],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return PublicDataset(
            frozen_at=dataset.frozen_at,
            window_days=dataset.window_days,
            window_start=dataset.window_start,
            window_end=dataset.window_end,
            records=dataset.records,
            headline=dataset.headline,
            by_provenance=dataset.by_provenance,
            calibration=dataset.calibration,
            voids=dataset.voids,
            duplicates_removed=dataset.duplicates_removed,
            pending_excluded=dataset.pending_excluded,
            dataset_id=f"pub-{dataset.window_end.isoformat()}-{digest}",
        )


public_dataset_service = PublicDatasetService()
