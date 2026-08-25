"""Make arbitrary values safe to put in a JSONField.

Lifted verbatim from apps/algo/services.py. The behaviour is deliberately
*not* improved: `default=str` means a Decimal lands as the string "1.50" and a
datetime as "2026-08-11 00:49:00+00:00" (space-separated, not ISO 'T'). Those
representations are already stored in production JSONFields and served to
clients, so changing them here would silently alter API payloads.

If a nicer encoding is ever wanted, it needs a migration for the stored rows
and a version bump on the affected endpoints — not a quiet edit to this file.
"""
from __future__ import annotations

import json
from typing import Any


def json_safe(value: Any) -> Any:
    """Round-trip through JSON, stringifying anything json cannot encode."""
    return json.loads(json.dumps(value, default=str))
