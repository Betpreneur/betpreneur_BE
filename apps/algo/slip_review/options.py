"""Slip-review API options and runtime settings."""

import os

from apps.algo.markets.api import market_options


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


SLIP_REVIEW_STREAM_TICKET_SECONDS = env_int("SLIP_REVIEW_STREAM_TICKET_SECONDS", 30 * 60)
SLIP_REVIEW_MARKET_OPTIONS = market_options()
SLIP_REVIEW_VERDICT_OPTIONS = [
    {"value": "keep", "label": "Keep", "description": "Selection is strong enough to stay on the slip."},
    {"value": "caution", "label": "Caution", "description": "Selection has some support but carries warnings."},
    {"value": "replace", "label": "Replace", "description": "A stronger market exists for the same game."},
    {"value": "remove", "label": "Remove", "description": "Selection does not show enough edge."},
    {"value": "unmatched", "label": "Unmatched", "description": "Fixture could not be confidently matched."},
    {"value": "pending_analysis", "label": "Pending Analysis", "description": "Fixture matched but has not been scored yet."},
]


__all__ = [
    "SLIP_REVIEW_MARKET_OPTIONS",
    "SLIP_REVIEW_STREAM_TICKET_SECONDS",
    "SLIP_REVIEW_VERDICT_OPTIONS",
    "env_int",
]
