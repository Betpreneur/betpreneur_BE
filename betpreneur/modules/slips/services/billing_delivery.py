"""Tells billing whether a slip review was actually delivered.

Billing sits below slips and cannot ask this itself, so the answer is
registered from up here, by the module that owns the review.
"""
from __future__ import annotations

from django.conf import settings

from betpreneur.modules.billing.api import Delivery, DeliveryVerdict, register_delivery_resolver

REFERENCE_TYPE = "slip_review"


def slip_review_delivery(reference_id: str, amount: int) -> DeliveryVerdict:
    from betpreneur.modules.slips.models import SlipReview

    try:
        review = (
            SlipReview.objects.filter(pk=int(reference_id))
            .values("status", "summary")
            .first()
        )
    except (TypeError, ValueError):
        return DeliveryVerdict(Delivery.UNDELIVERABLE)
    if review is None:
        return DeliveryVerdict(Delivery.UNDELIVERABLE)

    status_value = review["status"]
    if status_value in {SlipReview.Status.COMPLETED, SlipReview.Status.PARTIAL}:
        return DeliveryVerdict(Delivery.DELIVERED, _billable_tokens(review["summary"], amount))
    if status_value in {
        SlipReview.Status.QUEUED,
        SlipReview.Status.IMPORTING,
        SlipReview.Status.ANALYSING,
    }:
        # Still running. The stale-review recovery job drives it to a terminal
        # state, which releases the escrow; refunding a live review would let it
        # finish and be delivered for free.
        return DeliveryVerdict(Delivery.IN_FLIGHT)
    return DeliveryVerdict(Delivery.UNDELIVERABLE)


def _billable_tokens(summary, amount: int) -> int:
    summary = summary or {}
    if summary.get("analysed_count") is None:
        return int(amount or 0)
    cost_per_game = int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1) or 1)
    return max(0, int(summary.get("analysed_count") or 0) * cost_per_game)


def register() -> None:
    register_delivery_resolver(REFERENCE_TYPE, slip_review_delivery)
