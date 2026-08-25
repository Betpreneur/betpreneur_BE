"""Payfonte payment provider — HTTP transport only.

Takes a PayfonteConfig rather than reading django settings, so the client can
be constructed in a test with no Django at all. Building a charge payload from
a purchase, and reading settings, both belong to billing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


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

class PayfonteClient:
    def __init__(self, config: PayfonteConfig, session: requests.Session | None = None):
        self.config = config
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