"""Compatibility alias for apps.algo.slip_review.market_cache."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.slip_review.market_cache")
