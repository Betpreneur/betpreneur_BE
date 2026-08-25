"""Run-once keys for tasks that must not double-apply.

Settlement is the motivating case: grading the same day twice would double
every recorded outcome. The lock is held in the cache with a TTL, so a crashed
worker cannot wedge a key forever.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from django.core.cache import cache

from betpreneur.platform.cache.keys import key

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AlreadyRunning(RuntimeError):
    """Raised when a run-once block is entered while another holds the key."""


@contextmanager
def run_once(name: str, *parts: object, ttl: int = 3600) -> Iterator[None]:
    """Hold an exclusive key for the duration of the block.

        with run_once("settlement", target_date):
            ...

    Raises AlreadyRunning if the key is held. Releases on both success and
    failure, so a failed run can be retried immediately.
    """
    lock = key("lock", name, *parts)
    if not cache.add(lock, "1", timeout=ttl):
        raise AlreadyRunning(f"{lock} is already held")
    try:
        yield
    finally:
        cache.delete(lock)


def once(name: str, *parts: object, ttl: int = 3600) -> Callable[[Callable[[], T]], T | None]:
    """Functional form: returns None when the key is already held."""
    def run(fn: Callable[[], T]) -> T | None:
        try:
            with run_once(name, *parts, ttl=ttl):
                return fn()
        except AlreadyRunning:
            logger.info("skipped, already running name=%s parts=%r", name, parts)
            return None
    return run
