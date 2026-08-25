"""Confidence tiers.

Which band a confidence score falls into is a pricing judgement; picks merely
stores the result. The string values are the ones already persisted in
algo_pick.tier, so Pick.Tier and this enum must agree — picks builds its
TextChoices from these values.
"""
from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    BANKER = "banker"
    VALUE_GEM = "value_gem"
    WILD_CARD = "wild_card"
    WATCHLIST = "watchlist"


def tier_for_confidence(confidence) -> str:
    """Map a final confidence (0-100) onto a tier."""
    confidence = confidence or 0
    if confidence >= 80:
        return Tier.BANKER
    if 70 <= confidence < 80:
        return Tier.VALUE_GEM
    if 60 <= confidence < 70:
        return Tier.WILD_CARD
    return Tier.WATCHLIST
