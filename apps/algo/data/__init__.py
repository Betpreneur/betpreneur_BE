"""Compatibility alias for the scoring-owned data package."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.scoring.data")
