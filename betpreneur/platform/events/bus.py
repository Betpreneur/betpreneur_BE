"""A synchronous, in-process event bus.

Deliberately small. It exists to invert one dependency — letting `analytics`
react to `billing` without `billing` importing `analytics` — not to become a
message broker. Anything needing durability, retries or ordering should be a
Celery task that a handler enqueues.

Delivery is deferred to transaction commit by default, so a handler never
observes a write that later rolled back.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import TypeVar

from django.conf import settings
from django.db import transaction

from .base import DomainEvent

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=DomainEvent)
Handler = Callable[[DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None:
        """Register a handler. Called from a module's handlers.py at app-ready."""
        self._handlers[event_type].append(handler)  # type: ignore[arg-type]
        logger.debug("subscribed %s to %s", handler.__qualname__, event_type.name())

    def publish(self, event: DomainEvent, *, immediate: bool | None = None) -> None:
        """Publish an event to every subscriber.

        By default delivery waits for the surrounding transaction to commit.
        Pass immediate=True only when publishing outside a write, or set
        EVENT_BUS_IMMEDIATE=True (test settings do) to bypass the wait.
        """
        if immediate is None:
            immediate = getattr(settings, "EVENT_BUS_IMMEDIATE", False)
        if immediate:
            self._dispatch(event)
        else:
            transaction.on_commit(lambda: self._dispatch(event))

    def _dispatch(self, event: DomainEvent) -> None:
        for handler in self._handlers[type(event)]:
            try:
                handler(event)
            except Exception:
                # A subscriber must never break its publisher. Events are
                # notifications; if a handler matters that much, the caller
                # should be invoking it directly through an api.py instead.
                logger.exception(
                    "event handler failed event=%s handler=%s",
                    event.name(),
                    handler.__qualname__,
                )

    def clear(self) -> None:
        """Drop every subscription. Tests only."""
        self._handlers.clear()


bus = EventBus()
