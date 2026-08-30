"""Fixture and team text normalisation.

Pure — no Django, enforced by the R5 contract. Every provider spells a team a
little differently; this is the single place that decides what "the same name"
means, and resolution.py builds on it.
"""
from __future__ import annotations

import re
import unicodedata


def normalize_fixture_text(value) -> str:
    """Fold a fixture or team name to a comparable form.

    Strips accents, lowercases, expands "&", and reduces anything that is not
    alphanumeric to single spaces.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_referee_name(value) -> str:
    """Fold provider referee names to a comparable key.

    StatPal often appends the country, e.g. ``Michael Salisbury, England``.
    The country is useful for display but noisy for historical matching.
    """
    text = normalize_fixture_text(str(value or "").split(",", 1)[0])
    return text


def parse_match_query(value) -> tuple[str, str, str]:
    """Split a free-text search into (home, away, normalized_whole).

    Home and away come back empty when the text is not a two-sided fixture.
    """
    text = str(value or "").strip()
    normalized = normalize_fixture_text(text)
    parts = re.split(r"\s+(?:vs|v|versus)\s+|\s+-\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        home = normalize_fixture_text(parts[0])
        away = normalize_fixture_text(parts[1])
        if home and away:
            return home, away, normalized
    return "", "", normalized
