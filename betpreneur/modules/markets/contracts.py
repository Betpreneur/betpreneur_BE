"""Types that cross the markets boundary.

Every one of these is a frozen dataclass or an enum over plain values, so a
caller can hold one without reaching back into this module's internals.
"""
from __future__ import annotations

from .domain.canonical import (
    CanonicalMarket,
    Period,
    Resolution,
    Settlement,
    Subject,
)
from .domain.capabilities import MarketCapability
from .domain.catalogue import DailyMarketCatalogEntry
from .domain.data_capability import DataCapability
from .domain.evaluation import EvaluatorSpec
from .domain.taxonomy import MarketDescriptor

__all__ = [
    "CanonicalMarket",
    "DailyMarketCatalogEntry",
    "DataCapability",
    "EvaluatorSpec",
    "MarketCapability",
    "MarketDescriptor",
    "Period",
    "Resolution",
    "Settlement",
    "Subject",
]
