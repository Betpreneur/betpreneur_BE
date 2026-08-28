"""Diagnostics helpers for prediction outputs."""
from __future__ import annotations

from .contracts import FixturePrediction, PredictionDiagnostics


def merge_diagnostics(*items: PredictionDiagnostics | None) -> PredictionDiagnostics:
    """Merge warning/source metadata from internal model stages."""
    sources: list[str] = []
    warnings: list[str] = []
    data_quality = "unknown"
    metadata = {}
    for item in items:
        if item is None:
            continue
        sources.extend(item.model_sources)
        warnings.extend(item.warnings)
        metadata.update(item.metadata)
        if item.data_quality != "unknown":
            data_quality = item.data_quality
    return PredictionDiagnostics(
        data_quality=data_quality,
        model_sources=tuple(dict.fromkeys(sources)),
        warnings=tuple(dict.fromkeys(warnings)),
        metadata=metadata,
    )


def diagnostics_for_prediction(prediction: FixturePrediction) -> PredictionDiagnostics:
    """Return the top-level diagnostic summary for a fixture prediction."""
    return prediction.diagnostics
