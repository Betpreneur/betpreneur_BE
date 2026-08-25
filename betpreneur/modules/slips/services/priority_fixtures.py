"""Tells scoring which fixtures still have money riding on them.

Scoring sits below slips and cannot query selections itself, so the source is
registered from up here, by the module that owns them.
"""
from __future__ import annotations

from collections.abc import Iterator

from django.utils import timezone

from betpreneur.modules.scoring.api import register_priority_fixture_source


def pending_selection_match_ids() -> Iterator[str]:
    """Provider match ids for today's selections still awaiting settlement."""
    from ..models import SlipSelection

    pending = SlipSelection.objects.filter(
        match_date=timezone.localdate(), outcome=SlipSelection.Outcome.PENDING
    ).values_list("analysis_payload", flat=True)

    for payload in pending:
        matched = (payload or {}).get("matched_fixture") or {}
        candidate = (
            matched.get("statpal_provider_match_id")
            or matched.get("provider_match_id")
            or matched.get("main_id")
            or ""
        )
        if candidate:
            yield str(candidate)


def register() -> None:
    register_priority_fixture_source(pending_selection_match_ids)
