"""In-memory Payfonte for tests — no network, deterministic references."""
from __future__ import annotations

from typing import Any

from .client import PayfonteConfig, PayfonteError


class FakePayfonteClient:
    """Records calls and replays canned responses.

        client = FakePayfonteClient(verify={"status": "SUCCESS", "amount": 99000})
        client.verify_payment("ref")          # -> that dict
        client.verifications                  # -> ["ref"]
    """

    def __init__(
        self,
        *,
        charge: dict[str, Any] | None = None,
        verify: dict[str, Any] | None = None,
        fail_with: str | None = None,
    ) -> None:
        self.config = PayfonteConfig(
            base_url="https://fake.payfonte.test",
            client_id="fake",
            client_secret="fake",
            provider="bank-transfer-nigeria",
        )
        self.charges: list[dict[str, Any]] = []
        self.verifications: list[str] = []
        self._charge = charge or {}
        self._verify = verify or {}
        self._fail_with = fail_with

    def direct_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.charges.append(payload)
        if self._fail_with:
            raise PayfonteError(self._fail_with)
        return dict(self._charge)

    def verify_payment(self, reference: str) -> dict[str, Any]:
        self.verifications.append(reference)
        if self._fail_with:
            raise PayfonteError(self._fail_with)
        return dict(self._verify)
