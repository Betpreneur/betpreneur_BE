"""Structured logging with a correlation id shared by requests and tasks."""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


class CorrelationIdFilter(logging.Filter):
    """Adds %(correlation_id)s to every record, so a request can be followed
    across the web process and into the workers it enqueued."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id() or "-"
        return True


class CorrelationIdMiddleware:
    """Reads X-Correlation-ID from the request or mints one, and echoes it back."""

    HEADER = "X-Correlation-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(self.HEADER)
        cid = incoming if incoming else new_correlation_id()
        set_correlation_id(cid)
        response = self.get_response(request)
        response[self.HEADER] = cid
        return response
