"""Compatibility alias for apps.algo.slip_review.ticket_risk."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("apps.algo.slip_review.ticket_risk")
