"""Extension point for what happens when an email is verified.

/api/auth/verify-email/ returns a "tokens" field describing the signup grant —
a billing concern in an identity endpoint. Billing sits *above* identity, so
identity cannot call it. Instead identity states the extension point and
billing fills it in, which keeps the layer order intact while the response
body stays exactly as clients already receive it.

Contributors run synchronously and return a dict merged into the response, so
this is not the event bus: the caller needs the result.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: user -> fields to merge into the verify-email response
Contributor = Callable[[object], dict]

_contributors: dict[str, Contributor] = {}


def register_verification_contributor(name: str, fn: Contributor) -> None:
    _contributors[name] = fn


def clear_verification_contributors() -> None:
    """Tests only."""
    _contributors.clear()


def run_verification_contributors(user) -> dict:
    """Collect every contributor's fields. A failure is logged, never raised —
    the account is already verified and the request must not fail after that."""
    fields: dict = {}
    for name, fn in _contributors.items():
        try:
            fields.update(fn(user) or {})
        except Exception:
            logger.exception("verification contributor failed name=%s", name)
    return fields
