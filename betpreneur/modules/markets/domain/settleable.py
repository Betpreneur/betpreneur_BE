"""Which markets the settlement engine can resolve.

This is vocabulary, not settlement logic: it answers "is this a market we know
how to grade?" from the market name alone. It lives here so both settlement
(which grades) and slips (which warns a user that a leg cannot be tracked) can
ask without either reaching across to the other.
"""
from __future__ import annotations

SETTLEABLE_MARKETS = frozenset({
    "Home Win",
    "Away Win",
    "Draw",
    "Over 1.5",
    "Over 2.5",
    "Over 3.5",
    "Over 4.5",
    "Under 1.5",
    "Under 2.5",
    "Under 3.5",
    "Under 4.5",
    "GG / BTTS Yes",
    "BTTS No",
    "GG + Over 2.5",
    "DC: 1X",
    "DC: X2",
    "DC: 12",
    "Home CS",
    "Away CS",
    "AH Home +0.5",
    "AH Away +0.5",
    "DNB Home",
    "DNB Away",
    "First to Score H",
    "First to Score A",
})
def can_settle_market(market):
    market = str(market or "").strip()
    if not market:
        return False
    if market.startswith("Corners Over ") or market.startswith("Corners Under "):
        try:
            float(market.rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return False
        return True
    return market in SETTLEABLE_MARKETS
