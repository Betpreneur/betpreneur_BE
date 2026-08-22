"""Compatibility alias for apps.algo.scoring.data.planner."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.scoring.data.planner")
