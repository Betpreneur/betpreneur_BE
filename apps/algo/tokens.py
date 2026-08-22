"""Compatibility alias for apps.algo.wallet.tokens."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.wallet.tokens")
