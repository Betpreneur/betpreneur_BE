from datetime import timedelta
from datetime import date

from celery import shared_task
from django.utils import timezone

from .services import algo_runner_service


@shared_task(bind=True, ignore_result=False)
def generate_daily_picks(self, target_date=None):
    if target_date is None:
        target_date = timezone.localdate() + timedelta(days=1)
    else:
        target_date = date.fromisoformat(target_date)

    algo_run = algo_runner_service.create_run(target_date=target_date)
    algo_run = algo_runner_service.run(algo_run)
    return {
        "run_id": algo_run.id,
        "target_date": algo_run.target_date.isoformat(),
        "status": algo_run.status,
        "picks_count": algo_run.picks_count,
        "bankers": algo_run.bankers,
        "value_gems": algo_run.value_gems,
        "wild_cards": algo_run.wild_cards,
        "error": algo_run.error,
    }


@shared_task(bind=True, ignore_result=False)
def settle_daily_results(self, target_date=None):
    if target_date is None:
        target_date = timezone.localdate() - timedelta(days=1)
    else:
        target_date = date.fromisoformat(target_date)

    return algo_runner_service.update_results(target_date=target_date)


@shared_task(bind=True, ignore_result=False)
def run_monthly_auditor(self, from_date=None, to_date=None):
    if from_date is not None:
        from_date = date.fromisoformat(from_date)
    if to_date is not None:
        to_date = date.fromisoformat(to_date)

    return algo_runner_service.run_auditor(from_date=from_date, to_date=to_date)
