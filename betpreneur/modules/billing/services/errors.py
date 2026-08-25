"""Error payloads for billing failures.

Both the token endpoints and any feature that charges tokens need to render an
InsufficientTokens the same way, so the shape lives here on billing's public
surface rather than inside one caller's views.
"""
from __future__ import annotations

from django.conf import settings


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
