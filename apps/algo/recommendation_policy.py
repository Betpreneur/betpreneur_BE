"""Compatibility alias for apps.algo.picks.recommendation_policy."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.picks.recommendation_policy")
