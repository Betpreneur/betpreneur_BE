"""One error envelope for the whole API.

Every failure leaves the service in the same shape, so clients need one
branch rather than one per endpoint:

    {"error": {"code": "insufficient_tokens", "message": "...", "detail": {...}}}
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base for errors a client is allowed to see.

    Anything not deriving from this is a bug and is reported as a 500 with no
    internal detail leaked.
    """

    code = "error"
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


def error_payload(code: str, message: str, detail: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER. Renders DomainError; defers to DRF otherwise."""
    if isinstance(exc, DomainError):
        return Response(
            error_payload(exc.code, exc.message, exc.detail),
            status=exc.status_code,
        )
    return drf_exception_handler(exc, context)
