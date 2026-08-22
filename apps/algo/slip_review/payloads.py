import hashlib
from datetime import timedelta

from django.utils import timezone

from apps.algo.models import SlipReview, SlipSelection


def public_slip_review_status(value):
    return SlipReview.Status.COMPLETED if value == SlipReview.Status.PARTIAL else value


def public_slip_review_progress(progress):
    progress = dict(progress or {})
    if progress.get("final_status"):
        progress["final_status"] = public_slip_review_status(progress.get("final_status"))
    return progress


def _plural(value, singular, plural=None):
    return singular if value == 1 else (plural or f"{singular}s")


def _hit_rate(wins, losses):
    settled = wins + losses
    return round((wins / settled) * 100, 1) if settled else None


def slip_recap_payload(user, *, days):
    since = timezone.localdate() - timedelta(days=days)
    selections = list(
        SlipSelection.objects.filter(
            review__user=user,
            match_date__gte=since,
        ).only("outcome", "flagged_risky", "review_id")
    )

    wins = [item for item in selections if item.outcome == SlipSelection.Outcome.WIN]
    losses = [item for item in selections if item.outcome == SlipSelection.Outcome.LOSS]
    void = [item for item in selections if item.outcome == SlipSelection.Outcome.VOID]
    unsettleable = [item for item in selections if item.outcome == SlipSelection.Outcome.UNSETTLEABLE]
    pending = [item for item in selections if item.outcome == SlipSelection.Outcome.PENDING]

    flagged_wins = [item for item in wins if item.flagged_risky]
    flagged_losses = [item for item in losses if item.flagged_risky]
    unflagged_wins = [item for item in wins if not item.flagged_risky]
    unflagged_losses = [item for item in losses if not item.flagged_risky]

    ticket_count = len({item.review_id for item in selections})
    settled_count = len(wins) + len(losses)

    if not settled_count:
        message = "None of your selections in this window have been settled yet."
    else:
        message = (
            f"You submitted {ticket_count} {_plural(ticket_count, 'ticket')}. "
            f"{len(wins)} of {settled_count} settled {_plural(settled_count, 'selection')} were correct."
        )
        if losses:
            message += (
                f" {len(flagged_losses)} of the {len(losses)} that failed "
                f"{'was' if len(flagged_losses) == 1 else 'were'} flagged as risky before kickoff."
            )

    return {
        "contract_version": "match_checker_public_v2",
        "window": {"days": days, "from": since.isoformat(), "to": timezone.localdate().isoformat()},
        "tickets": ticket_count,
        "selections": {
            "total": len(selections),
            "settled": settled_count,
            "correct": len(wins),
            "failed": len(losses),
            "void": len(void),
            "unsettleable": len(unsettleable),
            "awaiting_result": len(pending),
        },
        "flagged": {
            "flagged_before_kickoff": len(flagged_wins) + len(flagged_losses),
            "failed_and_flagged": len(flagged_losses),
            "failed_and_not_flagged": len(unflagged_losses),
            "flagged_hit_rate_percent": _hit_rate(len(flagged_wins), len(flagged_losses)),
            "unflagged_hit_rate_percent": _hit_rate(len(unflagged_wins), len(unflagged_losses)),
        },
        "message": message,
    }


def repair_payload(review, plan, repair):
    return {
        "repair_id": repair.id,
        "review_id": review.id,
        "mode": repair.mode,
        "original": {
            "legs": plan.original_legs,
            "combined_odds": plan.original_combined_odds,
            "estimated_success_percent": plan.original_success_percent,
        },
        "revised": {
            "legs": plan.revised_legs,
            "combined_odds": plan.revised_combined_odds,
            "estimated_success_percent": plan.revised_success_percent,
        },
        "changes": plan.changes,
        "decisions": [decision.to_dict() for decision in plan.decisions],
        "disclosure": plan.disclosure,
    }


def slip_review_event_payload(event):
    payload = dict(event.payload or {})
    if payload.get("status"):
        payload["status"] = public_slip_review_status(payload["status"])
    if isinstance(payload.get("progress"), dict):
        payload["progress"] = public_slip_review_progress(payload["progress"])
    return {
        "id": event.id,
        "review_id": event.review_id,
        "event_type": event.event_type,
        "payload": payload,
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }


def public_slip_review_stream_event(event):
    event = dict(event or {})
    payload = dict(event.get("payload") or {})
    if payload.get("status"):
        payload["status"] = public_slip_review_status(payload["status"])
    if isinstance(payload.get("progress"), dict):
        payload["progress"] = public_slip_review_progress(payload["progress"])
    event["payload"] = payload
    return event


def stream_ticket_hash(ticket):
    return hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()


__all__ = [
    "public_slip_review_progress",
    "public_slip_review_status",
    "public_slip_review_stream_event",
    "repair_payload",
    "slip_recap_payload",
    "slip_review_event_payload",
    "stream_ticket_hash",
]
