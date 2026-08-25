"""Slip-review pipeline tasks.

Thin adapters — the work lives in services/.
"""
from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task

from betpreneur.platform.db.json import json_safe


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=1500,
    time_limit=1800,
)
def import_slip_review(self, review_id):
    try:
        from .interface.views import process_slip_review_import

        return json_safe(process_slip_review_import(review_id))
    except SoftTimeLimitExceeded:
        from .interface.views import fail_slip_review_import

        return fail_slip_review_import(
            review_id,
            "Slip review timed out while analysing selections. Please retry with fewer legs or try again later.",
            error_code="soft_time_limit_exceeded",
        )
    except ValueError as exc:
        return {
            "review_id": review_id,
            "status": "failed",
            "error": str(exc),
        }
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=1,
    default_retry_delay=60,
    soft_time_limit=900,
    time_limit=1200,
)
def analyse_slip_review_leg(self, review_id, index, selection, days=3):
    try:
        from .interface.views import process_slip_review_leg_analysis

        return json_safe(process_slip_review_leg_analysis(review_id, index, selection, days=days))
    except SoftTimeLimitExceeded:
        from .interface.views import process_slip_review_leg_failure

        return json_safe(
            process_slip_review_leg_failure(
                review_id,
                index,
                selection,
                "Slip leg analysis timed out.",
                error_code="soft_time_limit_exceeded",
            )
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        from .interface.views import process_slip_review_leg_failure

        return json_safe(process_slip_review_leg_failure(review_id, index, selection, str(exc)))


@shared_task(bind=True, ignore_result=False, max_retries=1, default_retry_delay=60)
def finalize_slip_review_import(self, leg_results, review_id):
    try:
        from .interface.views import finalize_slip_review_import_results

        return json_safe(finalize_slip_review_import_results(review_id, leg_results))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)
        from .interface.views import fail_slip_review_import

        return fail_slip_review_import(review_id, f"Slip review finalization failed: {exc}", error_code="finalize_failed")


@shared_task(bind=True, ignore_result=False)
def recover_stale_slip_reviews(self, stale_after_seconds=None, limit=25):
    from .interface.views import recover_stale_slip_reviews as recover_stale

    return json_safe(recover_stale(stale_after_seconds=stale_after_seconds, limit=limit))
