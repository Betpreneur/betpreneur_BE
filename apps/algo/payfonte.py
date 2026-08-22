"""Compatibility alias for apps.algo.wallet.payfonte."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.wallet.payfonte")
