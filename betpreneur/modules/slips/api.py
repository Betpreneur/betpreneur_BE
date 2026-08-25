"""Slip review — the paid product.

Imports a bookmaker slip, analyses each leg against our own view of the
fixture, and reports what the ticket is really worth. Also answers the two
questions modules below cannot: whether a review was delivered, and which
fixtures still have money on them.

This module is the only importable surface. Nothing outside the module
may reach into slips.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""
from __future__ import annotations

from .interface.views import manual_fixture_game, slip_recap_payload
from .models import SlipReview, SlipSelection

__all__ = [
    "SlipReview",
    "SlipSelection",
    "manual_fixture_game",
    "slip_recap_payload",
]
