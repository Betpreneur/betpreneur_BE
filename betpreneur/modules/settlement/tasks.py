"""Settlement entry points. Thin adapters — logic lives in services/."""
from __future__ import annotations

from datetime import date

from celery import shared_task

from .services.settle import settlement_service


@shared_task(bind=True, ignore_result=False)
def settle_daily_results(self, target_date=None):
    parsed = date.fromisoformat(target_date) if target_date else None
    return settlement_service.update_results(target_date=parsed)


@shared_task(bind=True, ignore_result=False)
def settle_slip_selections(self, target_date=None):
    parsed = date.fromisoformat(target_date) if target_date else None
    return settlement_service.settle_slip_selections(target_date=parsed)
