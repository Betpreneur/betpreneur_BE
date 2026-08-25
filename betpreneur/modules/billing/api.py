"""Wallets, tokens and purchases — the public surface of billing.

billing owns every token in the system: the balance, the ledger, escrow for
work in flight, and money coming in. It knows nothing about what the tokens
buy. A module that charges for something registers a delivery resolver so
billing can tell a finished job from an abandoned one.
"""
from __future__ import annotations

from .models import TokenPurchase, TokenReservation, TokenTransaction, TokenWallet
from .services.delivery import (
    Delivery,
    DeliveryVerdict,
    clear_delivery_resolvers,
    register_delivery_resolver,
    resolve_delivery,
)
from .services.errors import (
    insufficient_feature_tokens_payload,
    insufficient_tokens_payload,
)
from .services.payments import (
    PayfonteError,
    build_direct_charge_payload,
    payfonte_client,
    payfonte_config,
    payfonte_payment_payload,
)
from .services.wallet import (
    InsufficientTokens,
    TokenGrantResult,
    TokenOperationResult,
    TokenPurchaseResult,
    TokenRefillResult,
    TokenWalletService,
    token_package_by_id,
    token_package_catalogue,
    token_wallet_service,
    token_wallet_snapshot,
)

__all__ = [
    "Delivery",
    "DeliveryVerdict",
    "InsufficientTokens",
    "PayfonteError",
    "TokenGrantResult",
    "TokenOperationResult",
    "TokenPurchase",
    "TokenPurchaseResult",
    "TokenRefillResult",
    "TokenReservation",
    "TokenTransaction",
    "TokenWallet",
    "TokenWalletService",
    "annotations",
    "build_direct_charge_payload",
    "clear_delivery_resolvers",
    "insufficient_feature_tokens_payload",
    "insufficient_tokens_payload",
    "payfonte_client",
    "payfonte_config",
    "payfonte_payment_payload",
    "register_delivery_resolver",
    "resolve_delivery",
    "token_package_by_id",
    "token_package_catalogue",
    "token_wallet_service",
    "token_wallet_snapshot",
]
