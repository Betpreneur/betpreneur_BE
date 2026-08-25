"""Feeds settled slip legs to pricing's ticket-risk calibration.

Pricing sits below slips and cannot query settled legs itself, so the source is
registered from up here, by the module that owns the legs.
"""
from __future__ import annotations

from collections.abc import Iterator

from betpreneur.modules.pricing.api import SettledLeg, register_calibration_source


def settled_legs() -> Iterator[SettledLeg]:
    from betpreneur.modules.slips.models import SlipSelection

    rows = SlipSelection.objects.filter(
        outcome__in=[SlipSelection.Outcome.WIN, SlipSelection.Outcome.LOSS],
        advisory_score__isnull=False,
    ).values_list("advisory_score", "outcome")

    for score, outcome in rows.iterator(chunk_size=1000):
        yield SettledLeg(
            advisory_score=float(score),
            won=outcome == SlipSelection.Outcome.WIN,
        )


def register() -> None:
    register_calibration_source(settled_legs)
