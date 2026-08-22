"""Compatibility alias for apps.algo.slip_review.repair."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.slip_review.repair")
