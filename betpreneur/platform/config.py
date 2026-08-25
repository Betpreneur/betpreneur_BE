"""Process-level helpers."""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


@contextmanager
def temporary_env(values: Mapping[str, object]) -> Iterator[None]:
    """Apply environment variables for the duration of a block, then restore.

    The legacy runner is configured entirely through os.environ, so calling it
    means setting variables around the call. Restoring the previous values —
    including unsetting ones that were absent — keeps concurrent work honest.
    """
    previous: dict[str, str | None] = {}
    try:
        for key, value in (values or {}).items():
            previous[key] = os.environ.get(key)
            os.environ[key] = str(value)
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def env_int(name: str, default: int) -> int:
    """Read an integer from the environment, falling back on anything unusable."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)
