"""Compatibility alias for apps.algo.picks.grindalgo.algo_runner."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.picks.grindalgo.algo_runner")
