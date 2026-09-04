"""Settlement entry points. Thin adapters — logic lives in services/."""
from __future__ import annotations

from datetime import date

from celery import shared_task
from django.conf import settings

from .services.settle import settlement_service


@shared_task(bind=True, ignore_result=False)
def settle_daily_results(self, target_date=None):
    parsed = date.fromisoformat(target_date) if target_date else None
    return settlement_service.update_results(target_date=parsed)


@shared_task(bind=True, ignore_result=False)
def settle_recent_results(self, days=None, end_date=None):
    parsed_end = date.fromisoformat(end_date) if end_date else None
    if days is None:
        days = getattr(settings, "ALGO_SETTLE_LOOKBACK_DAYS", 7)
    return settlement_service.update_recent_results(days=days, end_date=parsed_end)


@shared_task(bind=True, ignore_result=False)
def settle_slip_selections(self, target_date=None):
    parsed = date.fromisoformat(target_date) if target_date else None
    return settlement_service.settle_slip_selections(target_date=parsed)
