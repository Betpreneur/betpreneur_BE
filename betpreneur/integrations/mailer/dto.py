"""Data crossing the mailer boundary. No Django, no domain types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MailerConfig:
    """Everything the mailer needs, passed in rather than read from settings.

    This is what lets the client be constructed in a test with no Django setup
    at all — the pattern every integration in this package follows.
    """

    api_key: str = ""
    from_name: str = "Betpreneur"
    from_email: str = ""

    @property
    def enabled(self) -> bool:
        """False when no key is configured; the client then no-ops loudly."""
        return bool(self.api_key)

    @property
    def sender(self) -> str:
        return f"{self.from_name} <{self.from_email}>"


@dataclass(frozen=True)
class SendResult:
    success: bool
    mocked: bool = False
    response: Any = None
    error: str | None = None
