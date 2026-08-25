"""Runs a settlement attempt under a lock, and records what it did.

Two attempts at the same date must not overlap: they would duplicate the
provider calls and interleave their writes. Re-running *sequentially* is
expected and supported — fixtures finish late — so a held lock is reported as
a skip, not an error.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from django.utils import timezone

from betpreneur.platform.tasks.idempotency import AlreadyRunning, run_once

from ..models import SettlementRun

logger = logging.getLogger(__name__)


def recorded(scope: str, target_date: date, work: Callable[[], dict]) -> dict:
    """Run `work` under a per-(scope, date) lock, writing a SettlementRun row.

    Returns the work's own payload on success. If another attempt holds the
    lock, returns a skipped payload instead of running.
    """
    try:
        with run_once("settlement", scope, target_date.isoformat(), ttl=3600):
            run = SettlementRun.objects.create(target_date=target_date, scope=scope)
            try:
                summary = work()
            except Exception as exc:
                run.status = SettlementRun.Status.FAILED
                run.error = str(exc)
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "error", "finished_at"])
                raise
            run.status = SettlementRun.Status.SUCCESS
            run.summary = summary
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "summary", "finished_at"])
            return summary
    except AlreadyRunning:
        logger.info("settlement already running scope=%s date=%s", scope, target_date)
        SettlementRun.objects.create(
            target_date=target_date,
            scope=scope,
            status=SettlementRun.Status.SKIPPED,
            finished_at=timezone.now(),
        )
        return {
            "status": "skipped",
            "reason": "another settlement attempt for this date is already running",
            "date": target_date.isoformat(),
            "scope": scope,
        }
