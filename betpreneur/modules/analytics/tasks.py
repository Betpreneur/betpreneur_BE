"""Audit tasks.

Thin adapters — the work lives in services/.
"""
from datetime import date

from celery import shared_task

from .services.runner import run_monthly_auditor as run_monthly_auditor_service


@shared_task(bind=True, ignore_result=False)
def run_monthly_auditor(self, from_date=None, to_date=None):
    if from_date is not None:
        from_date = date.fromisoformat(from_date)
    if to_date is not None:
        to_date = date.fromisoformat(to_date)

    return run_monthly_auditor_service(from_date=from_date, to_date=to_date)
