"""Where calibration evidence comes from.

Ticket risk is calibrated against legs that actually settled, but settled legs
belong to slips, which sits *above* pricing. So pricing states what it needs —
a stream of (score, won) pairs — and the module that owns the legs registers a
source for it.

With nothing registered, calibration falls back to its prior, which is exactly
what happens when there is not enough settled evidence anyway.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettledLeg:
    """One graded leg: the advisory score it carried, and whether it won."""

    advisory_score: float
    won: bool


Source = Callable[[], Iterable[SettledLeg]]

_source: Source | None = None


def register_calibration_source(fn: Source) -> None:
    global _source
    _source = fn


def clear_calibration_source() -> None:
    """Tests only."""
    global _source
    _source = None


def settled_legs() -> Iterator[SettledLeg]:
    """Every settled leg, or nothing when no source is registered."""
    if _source is None:
        return iter(())
    try:
        return iter(_source())
    except Exception:
        logger.exception("calibration source failed; falling back to the prior")
        return iter(())
