"""Compatibility alias for apps.algo.settlement.auditor_runner."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.settlement.auditor_runner")
