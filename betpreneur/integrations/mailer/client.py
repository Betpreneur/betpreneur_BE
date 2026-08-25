"""Resend transport.

Only transport lives here. Choosing what to say, rendering it, and recording
that it was said all belong to the module that asked for the send.
"""
from __future__ import annotations

import logging

import resend

from .dto import MailerConfig, SendResult

logger = logging.getLogger(__name__)


class ResendMailer:
    def __init__(self, config: MailerConfig) -> None:
        self._config = config

    def send(self, *, to: str, subject: str, html: str) -> SendResult:
        """Send one email. Never raises — delivery is best-effort."""
        if not self._config.enabled:
            logger.warning("RESEND_API_KEY not set. Would send email to %s: %s", to, subject)
            return SendResult(success=True, mocked=True)

        try:
            resend.api_key = self._config.api_key
            response = resend.Emails.send(
                {
                    "from": self._config.sender,
                    "to": to,
                    "subject": subject,
                    "html": html,
                }
            )
            return SendResult(success=True, response=response)
        except Exception as exc:
            logger.error("Failed to send email to %s: %s", to, exc)
            return SendResult(success=False, error=str(exc))
