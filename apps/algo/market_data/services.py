"""Public service surface for provider data and fixture resolution.

The implementation still lives in the legacy service module during the Stage 2
transition. Consumers should import from here so the implementation can move
without another cross-codebase import churn.
"""

from apps.algo.services import (
    FixtureSearchService,
    json_safe,
    normalize_fixture_text,
    parse_match_query,
)

__all__ = [
    "FixtureSearchService",
    "json_safe",
    "normalize_fixture_text",
    "parse_match_query",
]

