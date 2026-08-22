"""Compatibility alias for apps.algo.advisory.statpal_advisory."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.advisory.statpal_advisory")
