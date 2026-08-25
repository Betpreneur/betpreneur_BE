"""Which fixtures are worth spending provider calls on.

Lineups only firm up in the hour before kickoff, so the refresh runs often and
should target fixtures users actually have money on. Only slips knows that, and
slips sits above scoring — so scoring states the question and slips registers
an answer.

With nothing registered, the refresh falls back to whatever the caller passes
explicitly, which is the same behaviour as having no pending selections.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

#: () -> provider match ids worth refreshing today
Source = Callable[[], Iterable[str]]

_sources: list[Source] = []


def register_priority_fixture_source(fn: Source) -> None:
    _sources.append(fn)


def clear_priority_fixture_sources() -> None:
    """Tests only."""
    _sources.clear()


def priority_match_ids() -> set[str]:
    """Every match id any registered source considers worth refreshing."""
    out: set[str] = set()
    for source in _sources:
        try:
            out.update(str(m) for m in source() if m)
        except Exception:
            logger.exception("priority fixture source failed")
    return out
