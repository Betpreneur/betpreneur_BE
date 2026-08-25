"""Reporting and audits.

Read-only aggregation over everything below: performance windows, the
public record, market health, and the monthly strategy audit.

This module is the only importable surface. Nothing outside the module
may reach into analytics.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""
from __future__ import annotations

from .auditor import run_auditor

__all__ = [
    "run_auditor",
]
