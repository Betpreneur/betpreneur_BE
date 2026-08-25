"""Import each module's handlers.py once, at app-ready.

Subscriptions live in the subscribing module and are wired by importing it.
Doing that here — rather than at module import time — keeps the ordering
predictable and gives one place to see what is listening.
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Modules whose handlers.py should be loaded. Order does not matter; handlers
# must not depend on each other.
SUBSCRIBERS: tuple[str, ...] = (
    "betpreneur.modules.billing.handlers",
    "betpreneur.modules.settlement.handlers",
    "betpreneur.modules.analytics.handlers",
)


def load_handlers() -> None:
    for path in SUBSCRIBERS:
        try:
            importlib.import_module(path)
        except ModuleNotFoundError:
            # Expected while modules are still being built out.
            logger.debug("no handlers yet for %s", path)
