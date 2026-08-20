from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings


class PayfonteError(Exception):
    pass


@dataclass(frozen=True)
class PayfonteConfig:
    base_url: str
    client_id: str
    client_secret: str
    provider: str
    bank_transfer_network: str = ""
    timeout: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def payfonte_config() -> PayfonteConfig:
    return PayfonteConfig(
        base_url=str(getattr(settings, "PAYFONTE_BASE_URL", "https://sandbox-api.payfonte.com") or "").rstrip("/"),
        client_id=str(getattr(settings, "PAYFONTE_CLIENT_ID", "") or ""),
        client_secret=str(getattr(settings, "PAYFONTE_CLIENT_SECRET", "") or ""),
        provider=str(getattr(settings, "PAYFONTE_COLLECTION_PROVIDER", "bank-transfer-nigeria") or ""),
        bank_transfer_network=str(getattr(settings, "PAYFONTE_BANK_TRANSFER_NETWORK", "") or ""),
        timeout=int(getattr(settings, "PAYFONTE_TIMEOUT", 30) or 30),
    )


class PayfonteClient:
    def __init__(self, config: PayfonteConfig | None = None, session: requests.Session | None = None):
        self.config = config or payfonte_config()
        self.session = session or requests.Session()

    def direct_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request("post", "/payments/v1/payments/direct-charge", json=payload)
        return dict(response.get("data") or response)

    def verify_payment(self, reference: str) -> dict[str, Any]:
        response = self._request("get", f"/payments/v1/payments/verify/{reference}")
        return dict(response.get("data") or response)

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if not self.config.configured:
            raise PayfonteError("Payfonte credentials are not configured.")
        url = f"{self.config.base_url}{path}"
        headers = {
            "client-id": self.config.client_id,
            "client-secret": self.config.client_secret,
            "Content-Type": "application/json",
        }
        try:
            response = self.session.request(
                method.upper(),
                url,
                headers=headers,
                timeout=self.config.timeout,
                **kwargs,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = ""
            try:
                body = response.text[:500]
            except Exception:
                body = ""
            raise PayfonteError(f"Payfonte request failed status={response.status_code} body={body}") from exc
        except requests.RequestException as exc:
            raise PayfonteError(f"Payfonte request failed: {exc}") from exc

        try:
            return response.json()
        except ValueError as exc:
            raise PayfonteError("Payfonte returned a non-JSON response.") from exc


def payfonte_client() -> PayfonteClient:
    return PayfonteClient()


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
        "instructions": _payment_instructions(account),
    }


def _payment_instructions(account: dict[str, Any]) -> str:
    if not account:
        return "Transfer the exact amount to the virtual account shown by Payfonte, then tap verify."
    account_number = account.get("account_number") or "the account number"
    bank_name = account.get("bank_name") or "the bank shown"
    account_name = account.get("account_name") or "the account name shown"
    return f"Transfer the exact amount to {account_number} at {bank_name}, account name {account_name}, then tap verify."


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
            if key in account and account[key]:
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
