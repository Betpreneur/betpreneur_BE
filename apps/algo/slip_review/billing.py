import json
import logging

from django.conf import settings

from apps.algo.models import TokenReservation
from apps.algo.wallet.api import token_wallet_service


log = logging.getLogger(__name__)


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


def slip_review_token_cost(selection_count):
    return max(0, int(selection_count or 0)) * int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1))


def slip_review_billing_payload(review, *, selection_count, reservation=None, status_value="reserved"):
    token_cost = slip_review_token_cost(selection_count)
    payload = {
        "status": status_value,
        "token_cost": token_cost,
        "cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        "games": int(selection_count or 0),
    }
    if reservation:
        payload["reservation_id"] = reservation.id
        payload["reservation_status"] = reservation.status
        payload["reservation_expires_at"] = reservation.expires_at.isoformat() if reservation.expires_at else None
    review_payload = dict(review.submitted_payload or {})
    if review_payload.get("token_reservation_id"):
        payload["reservation_id"] = review_payload.get("token_reservation_id")
    return payload


def store_slip_review_billing(review, billing):
    summary = dict(review.summary or {})
    summary["billing"] = _json_safe(billing)
    review.summary = summary


def reserve_slip_review_tokens(review, selection_count):
    token_cost = slip_review_token_cost(selection_count)
    if token_cost <= 0:
        billing = slip_review_billing_payload(review, selection_count=selection_count, status_value="not_required")
        store_slip_review_billing(review, billing)
        return None

    result = token_wallet_service.reserve_tokens(
        review.user,
        token_cost,
        reference_type="slip_review",
        reference_id=str(review.id),
        metadata={
            "review_id": review.id,
            "source": review.source,
            "selection_count": int(selection_count or 0),
            "cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        },
    )
    submitted_payload = dict(review.submitted_payload or {})
    submitted_payload["token_reservation_id"] = result.reservation.id
    submitted_payload["token_cost"] = token_cost
    submitted_payload["selection_count"] = int(selection_count or 0)
    review.submitted_payload = _json_safe(submitted_payload)
    store_slip_review_billing(
        review,
        slip_review_billing_payload(
            review,
            selection_count=selection_count,
            reservation=result.reservation,
            status_value="reserved",
        ),
    )
    return result


def slip_review_billable_selection_count(review):
    summary = review.summary or {}
    if summary.get("analysed_count") is not None:
        return max(0, int(summary.get("analysed_count") or 0))
    return max(0, int((review.submitted_payload or {}).get("selection_count") or review.selections.count() or 0))


def consume_slip_review_token_reservation(review):
    reservation_id = (review.submitted_payload or {}).get("token_reservation_id")
    if not reservation_id:
        return None
    submitted_payload = review.submitted_payload or {}
    reserved_count = int(submitted_payload.get("selection_count") or review.selections.count() or 0)
    billable_count = slip_review_billable_selection_count(review)
    reserved_tokens = slip_review_token_cost(reserved_count)
    charged_tokens = slip_review_token_cost(billable_count)
    refunded_tokens = max(0, reserved_tokens - charged_tokens)
    try:
        result = token_wallet_service.consume_reservation_amount(int(reservation_id), charged_tokens)
        billing = slip_review_billing_payload(
            review,
            selection_count=reserved_count,
            reservation=result.reservation,
            status_value="consumed",
        )
        billing.update(
            {
                "billable_games": billable_count,
                "charged_tokens": charged_tokens,
                "refunded_tokens": refunded_tokens,
                "non_billable_games": max(0, reserved_count - billable_count),
            }
        )
        store_slip_review_billing(review, billing)
        return result
    except Exception:
        log.exception(
            "Slip review token reservation consume failed review=%s reservation=%s "
            "-- left open for reconciliation",
            review.id,
            reservation_id,
        )
        store_slip_review_billing(
            review,
            {
                **slip_review_billing_payload(
                    review,
                    selection_count=reserved_count,
                    status_value="consume_failed",
                ),
                "billable_games": billable_count,
                "charged_tokens": charged_tokens,
                "refunded_tokens": refunded_tokens,
                "non_billable_games": max(0, reserved_count - billable_count),
                "reconciliation_pending": True,
            },
        )
        return None


def release_slip_review_token_reservation(review):
    reservation_id = (review.submitted_payload or {}).get("token_reservation_id")
    if not reservation_id:
        return None
    try:
        result = token_wallet_service.release_reservation(int(reservation_id))
        store_slip_review_billing(
            review,
            slip_review_billing_payload(
                review,
                selection_count=(review.submitted_payload or {}).get("selection_count") or review.selections.count(),
                reservation=result.reservation,
                status_value="released",
            ),
        )
        return result
    except ValueError:
        return None
    except TokenReservation.DoesNotExist:
        return None
    except Exception:
        log.exception("Slip review token reservation release failed review=%s reservation=%s", review.id, reservation_id)
        return None


def insufficient_tokens_payload(exc, *, review_id=None, selection_count=0):
    required = int(getattr(exc, "required_tokens", 0) or 0)
    available = int(getattr(exc, "available_tokens", 0) or 0)
    payload = {
        "code": "insufficient_tokens",
        "message": f"You need {required} tokens to analyse this slip, but you only have {available}.",
        "required_tokens": required,
        "available_tokens": available,
        "shortfall": max(required - available, 0),
        "selection_count": int(selection_count or 0),
        "cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        "wallet": exc.to_dict().get("wallet", {}),
    }
    if review_id:
        payload["review_id"] = review_id
    return payload


def insufficient_feature_tokens_payload(exc, *, feature, token_cost, review_id=None):
    available = int(getattr(exc, "available_tokens", 0) or 0)
    payload = {
        "code": "insufficient_tokens",
        "feature": str(feature or ""),
        "message": f"You need {int(token_cost or 0)} tokens to use this feature, but you only have {available}.",
        "required_tokens": int(token_cost or 0),
        "available_tokens": available,
        "shortfall": max(int(token_cost or 0) - available, 0),
        "wallet": exc.to_dict().get("wallet", {}),
    }
    if review_id:
        payload["review_id"] = review_id
    return payload


__all__ = [
    "consume_slip_review_token_reservation",
    "insufficient_feature_tokens_payload",
    "insufficient_tokens_payload",
    "release_slip_review_token_reservation",
    "reserve_slip_review_tokens",
    "slip_review_billable_selection_count",
    "slip_review_billing_payload",
    "slip_review_token_cost",
    "store_slip_review_billing",
]
