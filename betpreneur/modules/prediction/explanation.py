"""Model-aware explanation facts.

Stage 19. The engine already produced explanation strings; what it could not do
was let a product reason about them. This turns each line into a typed fact so a
caller can rank, trim and verify it, and — the part that matters — it makes the
"no generic reason" rule an invariant rather than a convention.

The rule, concretely: an explanation either carries at least one fact derived
from a model, or it carries a LIMITATION fact saying why the model could not
speak. It is never allowed to be silent, because silence is what forces a
product to fall back on "rates at 70%".

Rendering lives in modules/explanations, which sits above this one. Prediction
states the facts; it does not write prose and never mentions Banker, Value Gem
or any other product concept.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from .contracts import MarketProbability, ValueAssessment


class FactKind(StrEnum):
    """What a fact is telling the reader.

    The order of these members is the order facts are presented in: what the
    model projects, what went into it, how that compares to the line, what
    history says, what the price implies, and finally what we could not do.
    """

    PROJECTION = "projection"    # "Projected total goals: 3.10."
    COMPONENT = "component"      # "Home average: 1.80 xG."
    COMPARISON = "comparison"    # "Line 2.5 is below the model projection."
    HISTORY = "history"          # "landed in 4 of 6 tracked comparable games."
    PRICE = "price"              # "Model fair odds 1.48, available 1.62."
    LIMITATION = "limitation"    # "No scoreline distribution was available."


#: Facts that come from a model actually running, as opposed to history,
#: price, or an admission that it could not run.
MODEL_KINDS = frozenset({FactKind.PROJECTION, FactKind.COMPONENT, FactKind.COMPARISON})

_ORDER = {kind: index for index, kind in enumerate(FactKind)}


class Priority(IntEnum):
    """Tie-break within a kind. Higher wins when an explanation is trimmed."""

    LOW = 10
    NORMAL = 20
    HIGH = 30


@dataclass(frozen=True, slots=True)
class Fact:
    """One statement about why the model believes what it believes.

    `values` carries the numbers behind the text so a validator can check the
    sentence against the model output instead of trusting the string.
    """

    kind: FactKind
    text: str
    source: str = ""
    values: Mapping[str, float | str] = field(default_factory=dict)
    priority: Priority = Priority.NORMAL

    @property
    def is_model_derived(self) -> bool:
        return self.kind in MODEL_KINDS

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "text": self.text,
            "source": self.source,
            "values": dict(self.values),
        }


@dataclass(frozen=True, slots=True)
class Explanation:
    """An ordered, de-duplicated set of facts for one market."""

    market: str
    facts: tuple[Fact, ...] = ()

    @property
    def has_model_basis(self) -> bool:
        """True when at least one fact came from a model that actually ran."""
        return any(fact.is_model_derived for fact in self.facts)

    @property
    def is_generic_only(self) -> bool:
        """True when nothing here explains anything.

        A product seeing this must not dress the market up with a bare
        percentage — that is the failure mode Stage 19 exists to prevent.
        """
        return not self.has_model_basis

    @property
    def limitations(self) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.kind is FactKind.LIMITATION)

    def of_kind(self, *kinds: FactKind) -> tuple[Fact, ...]:
        wanted = set(kinds)
        return tuple(f for f in self.facts if f.kind in wanted)

    def lines(self, limit: int | None = None) -> tuple[str, ...]:
        """The facts as text, already ordered and trimmed."""
        facts = self.facts if limit is None else self.facts[:limit]
        return tuple(f.text for f in facts)

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "has_model_basis": self.has_model_basis,
            "facts": [f.to_dict() for f in self.facts],
        }


def build_explanation(
    market: str,
    facts: Iterable[Fact],
    *,
    limit: int | None = None,
    no_model_reason: str = "",
) -> Explanation:
    """Order, de-duplicate and enforce the no-generic-reason rule.

    `limit` trims after ordering, so a card asking for three lines gets the
    three most informative ones rather than the first three constructed.
    """
    ordered = sorted(
        _dedupe(facts),
        key=lambda f: (_ORDER[f.kind], -int(f.priority)),
    )
    if not any(f.is_model_derived for f in ordered):
        ordered.append(_limitation_fact(no_model_reason))
        # Re-sort so the limitation sits last rather than wherever it landed.
        ordered.sort(key=lambda f: (_ORDER[f.kind], -int(f.priority)))
    if limit is not None:
        ordered = _trim(ordered, limit)
    return Explanation(market=market, facts=tuple(ordered))


def _dedupe(facts: Iterable[Fact]) -> list[Fact]:
    seen: set[str] = set()
    out: list[Fact] = []
    for fact in facts:
        if not fact or not fact.text:
            continue
        key = " ".join(fact.text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def _trim(facts: list[Fact], limit: int) -> list[Fact]:
    """Trim to `limit`, but never drop the last limitation.

    Losing the limitation would leave an explanation that looks confident and
    is not — the exact thing this module is meant to stop.
    """
    if limit <= 0 or len(facts) <= limit:
        return facts[: max(limit, 0)]
    kept = facts[:limit]
    if any(f.kind is FactKind.LIMITATION for f in facts) and not any(
        f.kind is FactKind.LIMITATION for f in kept
    ):
        kept = kept[: limit - 1] + [next(f for f in facts if f.kind is FactKind.LIMITATION)]
    return kept


def _limitation_fact(reason: str) -> Fact:
    text = reason.strip() or "No model output was available for this market."
    if not text.endswith("."):
        text = f"{text}."
    return Fact(
        kind=FactKind.LIMITATION,
        text=text,
        source="prediction.explanation",
        priority=Priority.HIGH,
    )


# ---------------------------------------------------------------------------
# Building from the contracts
# ---------------------------------------------------------------------------

def explain_market(
    probability: MarketProbability,
    *,
    limit: int | None = None,
) -> Explanation:
    """Explanation for one market probability."""
    facts = [
        f if isinstance(f, Fact) else classify(f, source=probability.model)
        for f in (probability.explanation_facts or ())
    ]
    return build_explanation(
        probability.market,
        facts,
        limit=limit,
        no_model_reason=_reason_from_warnings(probability.warnings, probability.data_quality),
    )


def explain_value(value: ValueAssessment, *, limit: int | None = None) -> Explanation:
    """Explanation for the price side. Always PRICE facts, never a model basis."""
    facts = [
        f if isinstance(f, Fact) else classify(f, source="prediction.value", kind=FactKind.PRICE)
        for f in (value.explanation_facts or ())
    ]
    ordered = sorted(_dedupe(facts), key=lambda f: (_ORDER[f.kind], -int(f.priority)))
    if limit is not None:
        ordered = ordered[:limit]
    return Explanation(market=value.market, facts=tuple(ordered))


_KIND_HINTS: tuple[tuple[FactKind, tuple[str, ...]], ...] = (
    (FactKind.HISTORY, ("landed in", "tracked comparable", "profile:", "history")),
    (FactKind.COMPARISON, ("line ", "is below", "is above", "supporting", "supports")),
    (FactKind.PROJECTION, ("projected", "probability:", "expected")),
    (FactKind.COMPONENT, ("average", "averages", "concedes", "elo ratings", "xg", "used the")),
    (FactKind.PRICE, ("fair odds", "available odds", "edge", "implied")),
)


def classify(text: str, *, source: str = "", kind: FactKind | None = None) -> Fact:
    """Type an existing fact string.

    The model writers produce prose today. Rather than rewrite every one of
    them at once, this reads the sentence and assigns a kind, so the ordering
    and the model-basis rule work immediately. New writers should build Fact
    directly and skip this.
    """
    if kind is not None:
        return Fact(kind=kind, text=text, source=source)
    lowered = text.lower()
    for candidate, hints in _KIND_HINTS:
        if any(hint in lowered for hint in hints):
            return Fact(kind=candidate, text=text, source=source)
    return Fact(kind=FactKind.COMPONENT, text=text, source=source)


_WARNING_REASONS = {
    "scoreline_matrix_missing": "No scoreline distribution was available for this fixture.",
    "count_model_unavailable": "No count model was available for this market.",
    "elo_unavailable": "No team ratings were available for this fixture.",
}


def _reason_from_warnings(warnings: Iterable[str], data_quality: str) -> str:
    for warning in warnings or ():
        reason = _WARNING_REASONS.get(warning)
        if reason:
            return reason
    if data_quality in {"unavailable", "unknown", ""}:
        return "The model had no usable data for this fixture."
    return ""


# ---------------------------------------------------------------------------
# Backwards-compatible string API
# ---------------------------------------------------------------------------

def explanation_facts_for_market(probability: MarketProbability) -> tuple[str, ...]:
    """Expose model facts without adding product recommendation language."""
    return explain_market(probability).lines()


def explanation_facts_for_value(value: ValueAssessment) -> tuple[str, ...]:
    """Expose price-vs-model facts without adding product recommendation language."""
    return explain_value(value).lines()
