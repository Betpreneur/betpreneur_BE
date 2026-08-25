"""Typed identifiers.

These are NewTypes, so they cost nothing at runtime but stop a provider's
event id being passed where an internal match id belongs — a mistake this
codebase currently has no way to catch.
"""
from __future__ import annotations

from typing import NewType

MatchId = NewType("MatchId", str)
"""Our canonical id for a fixture, stable across providers."""

ProviderMatchId = NewType("ProviderMatchId", str)
"""A single provider's id for a fixture. Resolve via catalog before use."""

TeamId = NewType("TeamId", str)
PlayerId = NewType("PlayerId", str)
LeagueId = NewType("LeagueId", str)
