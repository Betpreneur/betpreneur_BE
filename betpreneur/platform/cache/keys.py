"""Namespaced cache keys, so no two modules can collide on one."""
from __future__ import annotations


def key(module: str, *parts: object) -> str:
    """build a key: key("slips", "review", 12) -> 'betpreneur:slips:review:12'"""
    tail = ":".join(str(p) for p in parts)
    return f"betpreneur:{module}:{tail}" if tail else f"betpreneur:{module}"
