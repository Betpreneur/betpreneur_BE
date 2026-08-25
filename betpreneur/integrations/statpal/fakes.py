"""In-memory StatPal for tests — canned payloads, no network."""
from __future__ import annotations

from typing import Any

from .client import StatPalConfig, StatPalError


class FakeStatPalClient:
    """Replays payloads keyed by path and records every call.

        client = FakeStatPalClient({"soccer/leagues": {"leagues": [...]}})
        client.get("soccer/leagues")
        client.calls  # -> [("soccer/leagues", {...})]
    """

    def __init__(
        self,
        payloads: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
        fail_with: str | None = None,
    ) -> None:
        self.config = StatPalConfig(
            access_key="fake",
            base_url="https://fake.statpal.test/api/v2",
            usage_base_url="https://fake.statpal.test/api",
            timeout=5,
            enabled=enabled,
        )
        self._payloads = payloads or {}
        self._fail_with = fail_with
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        if self._fail_with:
            raise StatPalError(self._fail_with)
        return dict(self._payloads.get(path) or {})

    def soccer_endpoint(self, endpoint_name: str, params=None, **path_params):
        return self.get(endpoint_name.lower(), params)
