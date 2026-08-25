"""Payfonte payments, as billing uses them.

The HTTP client lives in integrations/payfonte and knows nothing about a
purchase. Everything here reads settings or turns a TokenPurchase into a
provider payload, which is domain work and belongs on this side of the line.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from betpreneur.integrations.payfonte import (
    PayfonteClient,
    PayfonteConfig,
    PayfonteError,
)

__all__ = [
    "PayfonteError",
    "build_direct_charge_payload",
    "payfonte_client",
    "payfonte_config",
    "payfonte_payment_payload",
]


def payfonte_config() -> PayfonteConfig:
    return PayfonteConfig(
        base_url=str(getattr(settings, "PAYFONTE_BASE_URL", "https://sandbox-api.payfonte.com") or "").rstrip("/"),
        client_id=str(getattr(settings, "PAYFONTE_CLIENT_ID", "") or ""),
        client_secret=str(getattr(settings, "PAYFONTE_CLIENT_SECRET", "") or ""),
        provider=str(getattr(settings, "PAYFONTE_COLLECTION_PROVIDER", "bank-transfer-nigeria") or ""),
        bank_transfer_network=str(getattr(settings, "PAYFONTE_BANK_TRANSFER_NETWORK", "") or ""),
        timeout=int(getattr(settings, "PAYFONTE_TIMEOUT", 30) or 30),
    )

def payfonte_client() -> PayfonteClient:
    return PayfonteClient(payfonte_config())

def build_direct_charge_payload(
    *,
    purchase,
    reference: str,
    webhook_url: str,
    user,
) -> dict[str, Any]:
    config = payfonte_config()
    customer_input: dict[str, Any] = {}
    if config.bank_transfer_network:
        customer_input["network"] = config.bank_transfer_network

    return {
        "provider": config.provider,
        "amount": int(purchase.amount_kobo),
        "reference": reference,
        "webhook": webhook_url,
        "narration": "Betpreneur token purchase",
        "customerInput": customer_input,
        "metadata": {
            "merchantName": "Betpreneur",
            "trafficType": "ECOMMERCE",
            "purchaseId": str(purchase.id),
            "userId": str(getattr(user, "id", "")),
            "packageId": purchase.package_id,
            "tokens": int(purchase.tokens),
        },
    }

def payfonte_payment_payload(purchase) -> dict[str, Any]:
    metadata = dict(purchase.metadata or {})
    payfonte = dict(metadata.get("payfonte") or {})
    direct_charge = dict(payfonte.get("direct_charge") or {})
    data = dict(direct_charge.get("data") or {})
    account = _find_bank_account_payload(data)
    validity_minutes = max(1, int(getattr(settings, "PAYFONTE_VIRTUAL_ACCOUNT_TTL_MINUTES", 30) or 30))
    expires_at = account.get("expires_at") or _fallback_payment_expires_at(purchase, validity_minutes)
    expires_in_seconds = _payment_expires_in_seconds(expires_at)
    return {
        "provider": "payfonte",
        "provider_reference": purchase.provider_reference,
        "status": purchase.status,
        "amount": int(purchase.amount),
        "amount_kobo": int(purchase.amount_kobo),
        "currency": purchase.currency,
        "payfonte_status": data.get("status", ""),
        "checkout_id": data.get("checkoutId") or data.get("checkout_id") or data.get("id") or "",
        "session_id": data.get("sessionId") or data.get("session_id") or "",
        "payment_reference": data.get("reference") or purchase.provider_reference,
        "bank_account": account,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in_seconds,
        "validity_minutes": validity_minutes,
        "instructions": _payment_instructions(account, validity_minutes),
    }

def _payment_instructions(account: dict[str, Any], validity_minutes: int) -> str:
    if not account:
        return f"Transfer the exact amount to the virtual account shown within {validity_minutes} minutes, then tap verify."
    account_number = account.get("account_number") or "the account number"
    bank_name = account.get("bank_name") or "the bank shown"
    account_name = account.get("account_name") or "the account name shown"
    return f"Transfer the exact amount to {account_number} at {bank_name}, account name {account_name}, within {validity_minutes} minutes, then tap verify."

def _fallback_payment_expires_at(purchase, validity_minutes: int) -> str:
    created_at = purchase.created_at or timezone.now()
    return (created_at + timedelta(minutes=validity_minutes)).isoformat()

def _payment_expires_in_seconds(expires_at: str) -> int:
    if not expires_at:
        return 0
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return max(0, int((parsed - timezone.now()).total_seconds()))

def _find_bank_account_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []

    def walk(value):
        if isinstance(value, dict):
            lowered = {str(k).lower(): v for k, v in value.items()}
            if any(k in lowered for k in ("accountnumber", "account_number", "bankname", "bank_name")):
                candidates.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    if not candidates:
        return {}
    account = candidates[0]

    def pick(*keys):
        for key in keys:
            if account.get(key):
                return account[key]
        lower_map = {str(k).lower(): v for k, v in account.items()}
        for key in keys:
            value = lower_map.get(key.lower())
            if value:
                return value
        return ""

    return {
        "bank_name": pick("bankName", "bank_name", "bank"),
        "bank_code": pick("bankCode", "bank_code"),
        "account_number": pick("accountNumber", "account_number", "accountNo", "account_no"),
        "account_name": pick("accountName", "account_name", "name"),
        "expires_at": pick("expiresAt", "expires_at", "expiry", "expiryDate"),
    }