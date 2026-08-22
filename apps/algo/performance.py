"""Compatibility alias for apps.algo.settlement.performance."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.settlement.performance")
