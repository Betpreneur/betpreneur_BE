"""What billing contributes to modules below it.

identity sits under billing and cannot call in to grant tokens, so it exposes
an extension point and billing fills it. The registration lives here, in the
module that knows what a token is worth.
"""
from __future__ import annotations

import logging

from betpreneur.modules.identity.api import register_verification_contributor

logger = logging.getLogger(__name__)


def signup_grant(user) -> dict:
    """Give a newly verified account its starting balance.

    Deliberately best-effort: the account *is* verified by the time this runs,
    and failing would leave the user unable to retry, since the verification
    code has already been cleared. A miss is self-healing — the nightly refill
    tops any wallet at or below the threshold up to the same cap — so it is
    logged loudly and swallowed rather than surfaced as a verification error.
    """
    from .services.wallet import token_wallet_service

    try:
        return {"tokens": token_wallet_service.grant_signup_tokens(user).to_dict()}
    except Exception:
        logger.exception("Signup token grant failed for user=%s", getattr(user, "id", None))
        return {"tokens": None}


def register() -> None:
    register_verification_contributor("billing.signup_grant", signup_grant)
