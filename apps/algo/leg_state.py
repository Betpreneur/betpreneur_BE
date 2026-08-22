"""Compatibility alias for apps.algo.advisory.leg_state."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.advisory.leg_state")
