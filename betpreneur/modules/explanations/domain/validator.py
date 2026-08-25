"""
Guardrail for generated explanations.

An explanation may only restate evidence the model actually produced. Two failure modes
matter, and both are rejections rather than warnings:

* **Invented numbers.** A fluent sentence containing a statistic nobody computed is
  indistinguishable from a real one. Every numeric token must appear in the supplied
  evidence.
* **Certainty language.** Football outcomes carry irreducible uncertainty. "Guaranteed",
  "sure thing" and "cannot lose" are the vocabulary this product exists to avoid, and
  they must never reach a user regardless of how the text was produced.

Validation failure falls back to the deterministic template. Nothing unvalidated ships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

BANNED_PHRASES = (
    "guarantee",
    "guaranteed",
    "sure bet",
    "sure thing",
    "sure odds",
    "certain win",
    "cannot lose",
    "can't lose",
    "no risk",
    "risk free",
    "risk-free",
    "banker",
    "lock of the day",
    "will definitely",
    "100% certain",
    "easy money",
)

# Small integers appear in ordinary prose ("both teams", "one of three") and carry no
# statistical claim, so they are not treated as evidence-bearing figures.
_ALLOWED_BARE = {"0", "1", "2", "3"}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {"ok": self.ok, "reasons": list(self.reasons)}


def _evidence_numbers(evidence) -> set[str]:
    """Every number the model actually produced, in the forms it might be written."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(_canonical(node))
            found.add(_canonical(round(float(node), 1)))
            found.add(_canonical(round(float(node))))
            found.add(_canonical(round(float(node) * 100, 1)))
            found.add(_canonical(round(float(node) * 100)))
        elif isinstance(node, str):
            for match in _NUMBER.finditer(node):
                found.add(_canonical(match.group()))

    walk(evidence)
    return {value for value in found if value}


def _canonical(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.10g}"


def validate(text: str, evidence) -> ValidationResult:
    """Whether this explanation may be shown to a user."""
    reasons: list[str] = []
    lowered = str(text or "").lower()

    if not lowered.strip():
        return ValidationResult(False, ("empty_explanation",))

    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            reasons.append(f"certainty_language:{phrase}")

    allowed = _evidence_numbers(evidence) | _ALLOWED_BARE
    for match in _NUMBER.finditer(lowered):
        token = _canonical(match.group())
        if token not in allowed:
            reasons.append(f"unsupported_number:{match.group()}")

    return ValidationResult(not reasons, tuple(dict.fromkeys(reasons)))
