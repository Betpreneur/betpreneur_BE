"""In-memory mailer for tests. Wired in by config/settings/test.py."""
from __future__ import annotations

from dataclasses import dataclass

from .dto import SendResult


@dataclass
class SentEmail:
    to: str
    subject: str
    html: str


class FakeMailer:
    """Records sends instead of making them.

        mailer = FakeMailer()
        ...
        assert mailer.sent[0].to == "user@example.com"
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[SentEmail] = []
        self._fail = fail

    def send(self, *, to: str, subject: str, html: str) -> SendResult:
        self.sent.append(SentEmail(to=to, subject=subject, html=html))
        if self._fail:
            return SendResult(success=False, error="fake failure")
        return SendResult(success=True, mocked=True)

    def last_to(self, address: str) -> SentEmail | None:
        for email in reversed(self.sent):
            if email.to == address:
                return email
        return None
