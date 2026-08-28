"""Reporting and audits.

Read-only aggregation over everything below: performance windows, the
public record, market health, and the monthly strategy audit.

This module is the only importable surface. Nothing outside the module
may reach into analytics.models, .services, .domain, .interface or .tasks —
the R2 import contract enforces that.
"""
from __future__ import annotations

from .auditor import run_auditor
from .models import StrategyActionOutcome
from .services.model_health import (
    Availability,
    Metric,
    ModelHealthReport,
    ModelHealthService,
    model_health_service,
)
from .services.public_dataset import (
    Provenance,
    PublicDataset,
    PublicDatasetService,
    public_dataset_service,
)
from .services.strategy_memory import evaluate_strategy_memory

__all__ = [
    "Availability",
    "Metric",
    "ModelHealthReport",
    "ModelHealthService",
    "Provenance",
    "PublicDataset",
    "PublicDatasetService",
    "StrategyActionOutcome",
    "evaluate_strategy_memory",
    "model_health_service",
    "public_dataset_service",
    "run_auditor",
]
