"""Was the work a reservation paid for actually delivered?

Only the module that did the work can answer that, and those modules all sit
*above* billing. So billing states the question and they register answers.

A module that charges tokens registers a resolver for its reference_type:

    billing.api.register_delivery_resolver("slip_review", my_resolver)

Without one, a reservation is treated as undeliverable and refunded in full —
the safe default, since an unrecognised reference means no work we can prove.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class Delivery(StrEnum):
    DELIVERED = "delivered"
    IN_FLIGHT = "in_flight"
    UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True)
class DeliveryVerdict:
    """What happened to the work, and what may be charged for it."""

    status: Delivery
    #: Tokens to actually charge. None means "charge the whole reservation".
    billable_tokens: int | None = None


#: (reference_id, reserved_amount) -> DeliveryVerdict
Resolver = Callable[[str, int], DeliveryVerdict]

_resolvers: dict[str, Resolver] = {}


def register_delivery_resolver(reference_type: str, resolver: Resolver) -> None:
    _resolvers[reference_type] = resolver
    logger.debug("registered delivery resolver for %s", reference_type)


def clear_delivery_resolvers() -> None:
    """Tests only."""
    _resolvers.clear()


def resolve_delivery(reference_type: str, reference_id: str, amount: int) -> DeliveryVerdict:
    resolver = _resolvers.get(reference_type)
    if resolver is None:
        return DeliveryVerdict(Delivery.UNDELIVERABLE)
    try:
        return resolver(reference_id, amount)
    except Exception:
        logger.exception(
            "delivery resolver failed type=%s id=%s", reference_type, reference_id
        )
        return DeliveryVerdict(Delivery.UNDELIVERABLE)
