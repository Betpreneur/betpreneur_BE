"""Compatibility alias for the market-data domain."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.market_data.statpal")
