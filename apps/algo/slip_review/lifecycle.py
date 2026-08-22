import json
import logging

from django.conf import settings
from django.utils import timezone

from apps.algo.models import SlipReview, SlipReviewEvent
from apps.algo.slip_review.market_descriptors import selection_market_descriptor
from apps.algo.slip_review import redis_progress
from apps.algo.slip_review.payloads import public_slip_review_progress, public_slip_review_status


log = logging.getLogger(__name__)


def json_safe(value):
    return json.loads(json.dumps(value, default=str))


def empty_api_usage():
    return {
        "provider": "statpal",
        "attempted_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "skipped_by_cache": 0,
        "skipped_without_call": 0,
        "snapshot_types_attempted": [],
        "snapshot_types_refreshed": [],
        "snapshot_types_failed": [],
    }


def empty_slip_summary(verdict, *, task_id="", error=""):
    summary = {
        "count": 0,
        "analysed_count": 0,
        "keep_count": 0,
        "caution_count": 0,
        "replace_count": 0,
        "remove_count": 0,
        "expired_count": 0,
        "unmatched_count": 0,
        "pending_analysis_count": 0,
        "api_usage": empty_api_usage(),
        "intelligence": {
            "overall_score": 0,
            "risk_level": "medium" if not error else "high",
            "verdict": verdict,
            "api_usage": empty_api_usage(),
            "original_combined_odds": None,
            "suggested_combined_odds": None,
            "strongest_legs": [],
            "weakest_legs": [],
            "legs_to_keep": [],
            "legs_to_caution": [],
            "legs_to_replace": [],
            "legs_to_remove": [],
            "expired_legs": [],
            "unverified_legs": [],
        },
    }
    if task_id:
        summary["task_id"] = task_id
    if error:
        summary["error"] = str(error)
    return summary


def slip_review_progress(*, phase, total=0, completed=0, message="", **extra):
    total = max(0, int(total or 0))
    completed = max(0, min(int(completed or 0), total)) if total else max(0, int(completed or 0))
    percent = round((completed / total) * 100, 1) if total else (100.0 if phase in {"completed", "failed"} else 0.0)
    progress = {
        "phase": str(phase or ""),
        "total": total,
        "completed": completed,
        "percent": percent,
        "message": str(message or ""),
        "updated_at": timezone.now().isoformat(),
    }
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            progress[key] = json_safe(value)
    return progress


def public_slip_review_error_message(error_code="analysis_failed"):
    messages = {
        "soft_time_limit_exceeded": "This selection took too long to analyse. Please retry in a moment.",
        "analysis_failed": "We could not analyse this selection right now. Please retry in a moment.",
        "failed": "Slip review failed. Please retry in a moment.",
    }
    return messages.get(str(error_code or ""), messages["analysis_failed"])


def completed_slip_review_leg_count(review):
    return review.selections.exclude(status__in=["queued", "analysing", ""]).count()


def slip_review_leg_failure_result(index, selection, message=None, *, error_code="analysis_failed"):
    selection = selection or {}
    provider_payload = json_safe(selection.get("provider_payload") or {})
    public_message = public_slip_review_error_message(error_code)
    return {
        "match": selection.get("match", ""),
        "submitted_market": selection.get("market", ""),
        "market_taxonomy": selection_market_descriptor(selection, selection.get("market", "")).to_dict(),
        "status": "analysis_failed",
        "verdict": "not_assessed",
        "message": public_message,
        "provider": selection.get("provider", ""),
        "provider_payload": provider_payload,
        "fixture_resolution": {
            "status": "analysis_failed",
            "attempts": [
                {
                    "strategy": "celery_leg_task",
                    "error_code": error_code,
                    "index": index,
                }
            ],
        },
        "possible_matches": [],
    }


def publish_slip_review_event(review, event_type, payload=None):
    payload = json_safe(payload or {})
    public_payload = dict(payload)
    if public_payload.get("status"):
        public_payload["status"] = public_slip_review_status(public_payload["status"])
    if isinstance(public_payload.get("progress"), dict) and public_payload["progress"].get("final_status"):
        public_payload["progress"] = public_slip_review_progress(public_payload["progress"])
    try:
        event = SlipReviewEvent.objects.create(
            review=review,
            event_type=str(event_type or ""),
            payload=public_payload,
        )
        log.info(
            "Slip review event review=%s event=%s event_id=%s payload=%s",
            review.id,
            event_type,
            event.id,
            payload,
        )
        event_payload = {
            "type": "slip_review.event",
            "id": event.id,
            "review_id": review.id,
            "event_type": event.event_type,
            "payload": event.payload or {},
            "created_at": event.created_at.isoformat() if event.created_at else "",
        }
        redis_progress.push_event(
            review.id,
            event_payload,
            snapshot={
                "type": "slip_review.snapshot",
                "review_id": review.id,
                "status": public_slip_review_status(review.status),
                "progress": public_slip_review_progress((review.summary or {}).get("progress") or {}),
                "latest_event_id": event.id,
                "updated_at": review.updated_at.isoformat() if review.updated_at else "",
            },
        )
        if getattr(settings, "ENABLE_WEBSOCKETS", False):
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer

                channel_layer = get_channel_layer()
                if channel_layer:
                    async_to_sync(channel_layer.group_send)(
                        f"slip_review_{review.id}",
                        {
                            "type": "slip_review.event",
                            "payload": event_payload,
                        },
                    )
            except Exception:
                log.exception(
                    "Slip review websocket publish failed review=%s event=%s event_id=%s",
                    review.id,
                    event_type,
                    event.id,
                )
        return event
    except Exception:
        log.exception("Slip review event publish failed review=%s event=%s", getattr(review, "id", None), event_type)
        return None


def set_slip_review_progress(review, *, phase, total=0, completed=0, message="", status=None, save=True, **extra):
    summary = dict(review.summary or {})
    summary["progress"] = slip_review_progress(
        phase=phase,
        total=total,
        completed=completed,
        message=message,
        **extra,
    )
    review.summary = summary
    if status:
        review.status = status
    public_progress = public_slip_review_progress(summary["progress"])
    redis_progress.store_snapshot(
        review.id,
        {
            "type": "slip_review.snapshot",
            "review_id": review.id,
            "status": public_slip_review_status(review.status),
            "progress": public_progress,
            "latest_event_id": None,
            "updated_at": timezone.now().isoformat(),
        },
    )
    if save:
        fields = ["summary", "updated_at"]
        if status:
            fields.insert(0, "status")
        review.save(update_fields=fields)
        publish_slip_review_event(
            review,
            "review.progress",
            {
                "status": public_slip_review_status(review.status),
                "progress": public_progress,
            },
        )
    return summary["progress"]


def mark_slip_review_failed(review, message, *, error_code="failed", error_payload=None):
    review.status = SlipReview.Status.FAILED
    review.summary = empty_slip_summary(
        message,
        task_id=(review.summary or {}).get("task_id", ""),
        error=message,
    )
    review.summary["error_code"] = error_code
    if error_payload:
        review.summary["error_payload"] = json_safe(error_payload)
    review.summary["progress"] = slip_review_progress(
        phase="failed",
        message=message,
        error_code=error_code,
    )
    review.save(update_fields=["status", "summary", "updated_at"])
    publish_slip_review_event(
        review,
        "review.failed",
        {
            "status": review.status,
            "error": message,
            "error_code": error_code,
            "error_payload": json_safe(error_payload or {}),
            "progress": review.summary.get("progress") or {},
        },
    )
    return review


def mark_slip_review_completed(review, summary, *, submitted_payload=None, total=0):
    review.summary = json_safe(summary or {})
    if submitted_payload is not None:
        review.submitted_payload = json_safe(submitted_payload)
    final_updated_at = timezone.now()
    update_values = {
        "status": review.status,
        "summary": review.summary,
        "updated_at": final_updated_at,
    }
    if submitted_payload is not None:
        update_values["submitted_payload"] = review.submitted_payload
    SlipReview.objects.filter(id=review.id).update(**update_values)
    review.updated_at = final_updated_at
    publish_slip_review_event(
        review,
        "review.completed",
        {
            "status": public_slip_review_status(review.status),
            "total": total,
            "completed": total,
            "progress": review.summary.get("progress") or {},
        },
    )
    return review


def review_status_from_summary(summary):
    summary = summary or {}
    count = int(summary.get("count") or 0)
    analysed_count = int(summary.get("analysed_count") or 0)
    pending_count = int(summary.get("pending_analysis_count") or 0)
    not_assessed_count = int(summary.get("not_assessed_count") or 0)
    reviewable_count = max(0, count - int(summary.get("expired_count") or 0))
    if count and analysed_count == reviewable_count:
        return SlipReview.Status.COMPLETED
    if analysed_count:
        return SlipReview.Status.PARTIAL
    if pending_count:
        return SlipReview.Status.UNANALYSED
    if not_assessed_count:
        # The review ran to completion and concluded it could not assess anything.
        # That is a finding, not a crash, and must not be reported as a failure.
        return SlipReview.Status.UNANALYSED
    return SlipReview.Status.FAILED


def create_queued_slip_review(user, *, source, submitted_payload):
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.QUEUED,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary={
            **empty_slip_summary("Slip import queued."),
            "progress": slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        },
    )


def create_analysing_slip_review(user, *, source, submitted_payload):
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.ANALYSING,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary=empty_slip_summary("Slip analysis started."),
    )


def create_failed_slip_review(user, *, source, submitted_payload, error):
    summary = empty_slip_summary("Slip import failed.", error=error)
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.FAILED,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary=summary,
    )


__all__ = [
    "completed_slip_review_leg_count",
    "create_analysing_slip_review",
    "create_failed_slip_review",
    "create_queued_slip_review",
    "empty_api_usage",
    "empty_slip_summary",
    "json_safe",
    "mark_slip_review_completed",
    "mark_slip_review_failed",
    "public_slip_review_error_message",
    "publish_slip_review_event",
    "review_status_from_summary",
    "set_slip_review_progress",
    "slip_review_leg_failure_result",
    "slip_review_progress",
]
