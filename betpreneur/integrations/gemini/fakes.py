"""In-memory Gemini for tests — canned verdicts, no network, no API key.

The real client posts to generateContent and parses a JSON verdict back. These
fakes stand in for that call so a test can drive the council without a key and
without waiting on an LLM.
"""
from __future__ import annotations

from typing import Any


class FakeGeminiAnalyst:
    """Replays a canned analyst verdict and records what it was asked.

        analyst = FakeGeminiAnalyst(verdict={"bankers": [...]})
        analyst.call_shege_analyst(candidates)
        analyst.calls           # -> [[{...candidate...}]]

    Passing verdict=None reproduces the real client's behaviour when the key is
    missing or the model returns nothing usable.
    """

    def __init__(
        self,
        verdict: dict[str, Any] | None = None,
        *,
        fail_with: Exception | None = None,
    ) -> None:
        self._verdict = verdict
        self._fail_with = fail_with
        self.calls: list[list[dict[str, Any]]] = []

    def call_shege_analyst(self, candidates):
        self.calls.append(list(candidates or []))
        if self._fail_with is not None:
            raise self._fail_with
        if not candidates:
            return None
        return self._verdict


def no_candidates_verdict() -> None:
    """What the real client returns when there is nothing to analyse."""
    return None


def rejecting_verdict() -> dict[str, Any]:
    """A verdict that promotes nothing — useful for gating tests."""
    return {"bankers": [], "value_gems": [], "wild_cards": []}
