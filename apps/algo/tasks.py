from datetime import timedelta
from datetime import date

from celery import chord, shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .services import algo_runner_service


@shared_task(bind=True, ignore_result=False)
def generate_daily_picks(self, target_date=None):
    if target_date is None:
        target_date = timezone.localdate()
    else:
        target_date = date.fromisoformat(target_date)

    algo_run = algo_runner_service.create_run(target_date=target_date)
    fixture_ids = algo_runner_service.prepare_fanout_run(algo_run)
    if fixture_ids:
        workflow = chord(
            [score_fixture_for_daily_run.s(fixture_id) for fixture_id in fixture_ids]
        )(publish_daily_run.s(algo_run.id))
        status_value = "scoring_queued"
        child_task_id = workflow.id
    else:
        algo_run.refresh_from_db()
        status_value = algo_run.status
        child_task_id = ""
    return {
        "run_id": algo_run.id,
        "target_date": algo_run.target_date.isoformat(),
        "status": status_value,
        "publish_task_id": child_task_id,
        "picks_count": algo_run.picks_count,
        "bankers": algo_run.bankers,
        "value_gems": algo_run.value_gems,
        "wild_cards": algo_run.wild_cards,
        "error": algo_run.error,
    }


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=3,
    default_retry_delay=60,
    rate_limit="6/m",
    soft_time_limit=900,
    time_limit=1200,
)
def score_fixture_for_daily_run(self, fixture_id):
    try:
        result = algo_runner_service.score_fixture_for_run(fixture_id)
    except ObjectDoesNotExist as exc:
        return {
            "fixture_id": fixture_id,
            "status": "skipped",
            "error": str(exc) or "object_not_found",
        }
    if result.get("status") == "failed" and self.request.retries < self.max_retries:
        raise self.retry(countdown=60 * (self.request.retries + 1))
    return result


@shared_task(bind=True, ignore_result=False, max_retries=2, default_retry_delay=120)
def publish_daily_run(self, score_results, run_id):
    algo_run = algo_runner_service.publish_fanout_run(run_id)
    explain_picks_for_run.delay(algo_run.id)
    return {
        "run_id": algo_run.id,
        "target_date": algo_run.target_date.isoformat(),
        "status": algo_run.status,
        "picks_count": algo_run.picks_count,
        "bankers": algo_run.bankers,
        "value_gems": algo_run.value_gems,
        "wild_cards": algo_run.wild_cards,
        "scored": sum(1 for item in score_results or [] if (item or {}).get("status") == "scored"),
        "failed": sum(1 for item in score_results or [] if (item or {}).get("status") == "failed"),
        "error": algo_run.error,
    }


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=3,
    default_retry_delay=120,
    rate_limit="12/m",
    soft_time_limit=300,
    time_limit=420,
)
def explain_picks_for_run(self, run_id):
    try:
        return algo_runner_service.explain_picks_for_run(run_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120 * (self.request.retries + 1))


@shared_task(bind=True, ignore_result=False)
def recover_daily_run(self, run_id, rescore_failed=False):
    return algo_runner_service.recover_fanout_run(run_id, rescore_failed=rescore_failed)


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
