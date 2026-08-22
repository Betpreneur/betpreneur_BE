"""Compatibility alias for apps.algo.picks.daily_market_catalog."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.picks.daily_market_catalog")
