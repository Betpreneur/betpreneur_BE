"""Public markets API.

This layer is intentionally model-free. It owns market identity, canonical
names, capabilities, and bookmaker-to-canonical normalization.
"""

from .capabilities import (
    FULL,
    MEDIUM,
    UNSUPPORTED,
    WEAK,
    MarketCapability,
    market_capability_service,
)
from .taxonomy import (
    MarketDescriptor,
    canonical_market_name,
    describe_market,
    market_matches,
    market_options,
    normalize_market_text,
)
from .normalize.bridge import descriptor_from_canonical
from .normalize.canonical import Period, Resolution, Settlement, Subject
from .normalize.sportybet import resolve as resolve_sportybet_market

__all__ = [
    "FULL",
    "MEDIUM",
    "UNSUPPORTED",
    "WEAK",
    "MarketCapability",
    "MarketDescriptor",
    "Period",
    "Resolution",
    "Settlement",
    "Subject",
    "canonical_market_name",
    "describe_market",
    "descriptor_from_canonical",
    "market_capability_service",
    "market_matches",
    "market_options",
    "normalize_market_text",
    "resolve_sportybet_market",
]
