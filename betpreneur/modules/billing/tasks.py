"""Scheduled billing work. Thin adapters — the logic lives in services/."""
from __future__ import annotations

from datetime import date

from celery import shared_task

from .services.wallet import token_wallet_service


@shared_task(bind=True, ignore_result=False)
def refill_daily_free_tokens(self, run_date=None, limit=None):
    parsed_date = date.fromisoformat(run_date) if run_date else None
    return token_wallet_service.refill_daily_free_tokens(run_date=parsed_date, limit=limit)


@shared_task(bind=True, ignore_result=False)
def expire_token_reservations(self, limit=200):
    return token_wallet_service.expire_stale_reservations(limit=limit)
