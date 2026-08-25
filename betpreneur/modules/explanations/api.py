"""Human-readable reasoning — the public surface of explanations.

Takes a verdict and the evidence behind it and produces something a user can
read: an LLM council review, a rendered template, and the validator that stops
a claim the evidence does not support from reaching anyone.
"""
from __future__ import annotations

from .services import generate
from .services.council import CAUTION, REJECT, council_review

__all__ = ["CAUTION", "REJECT", "council_review", "generate"]
