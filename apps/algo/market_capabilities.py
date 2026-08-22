"""Compatibility alias for apps.algo.markets.capabilities."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.markets.capabilities")
