"""Slip-review market descriptor recovery helpers."""

import dataclasses
import logging

from apps.algo.markets.api import MarketDescriptor, describe_market


log = logging.getLogger(__name__)


def resolved_taxonomy(selection):
    """
    Return the descriptor identity resolved by the bookmaker importer.

    Bookmaker imports nest the analysed item under `provider_payload`; the manual
    path puts it at the top level.
    """
    selection = selection or {}
    for candidate in (
        selection.get("market_taxonomy"),
        (selection.get("provider_payload") or {}).get("market_taxonomy"),
    ):
        if isinstance(candidate, dict) and candidate.get("family") and candidate.get("recognized"):
            return candidate
    return {}


def resolved_canonical_market(selection):
    """
    Return the canonical market identity carried from the importer, if present.
    """
    selection = selection or {}
    for candidate in (
        selection.get("canonical_market"),
        (selection.get("provider_payload") or {}).get("canonical_market"),
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def descriptor_from_taxonomy(taxonomy):
    """Rebuild a MarketDescriptor from its stored form, tolerating JSON round-tripping."""
    fields = {field.name for field in dataclasses.fields(MarketDescriptor)}
    payload = {key: value for key, value in (taxonomy or {}).items() if key in fields}
    payload["data_requirements"] = tuple(payload.get("data_requirements") or ())
    for key in ("raw", "canonical", "code", "family", "category"):
        payload.setdefault(key, "")
    return MarketDescriptor(**payload)


def selection_market_descriptor(selection, requested_market):
    """
    Use the identity resolved at import time; only parse text when there is none.
    """
    selection = selection or {}
    taxonomy = resolved_taxonomy(selection)
    if taxonomy:
        try:
            return descriptor_from_taxonomy(taxonomy)
        except (TypeError, ValueError) as exc:
            log.info("Falling back to text parsing for %r: %s", requested_market, str(exc)[:200])
    return describe_market(
        requested_market,
        market_name=(taxonomy.get("raw") or (selection.get("market_taxonomy") or {}).get("raw") or ""),
    )


__all__ = [
    "descriptor_from_taxonomy",
    "resolved_canonical_market",
    "resolved_taxonomy",
    "selection_market_descriptor",
]
