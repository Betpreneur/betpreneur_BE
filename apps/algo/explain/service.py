"""
Explanation orchestration.

Order of operations, and the reason for it:

1. Build the deterministic explanation. This always succeeds and is always safe.
2. If a language model is configured *and* enabled, ask it to rephrase — passing only
   the structured evidence, never free rein.
3. Validate the result. Any invented number or certainty phrase means we discard it.

The template is the product; the model is a presentation layer over it. A review is
complete without ever calling one, which is what keeps the explanation off the critical
path and immune to provider outages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from . import templates
from .validator import validate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You rewrite football bet assessments for a general audience. "
    "Use only the facts in the supplied evidence. "
    "Do not invent or infer any number that is not present. "
    "Never promise or guarantee an outcome. "
    "Two or three short sentences, plain language."
)


@dataclass(frozen=True)
class Explanation:
    text: str
    source: str            # "template" | "model"
    validation: dict

    def to_dict(self):
        return {"text": self.text, "source": self.source, "validation": self.validation}


def _llm_enabled() -> bool:
    return bool(getattr(settings, "EXPLANATION_LLM_ENABLED", False))


def _rephrase(text: str, evidence) -> str | None:
    """Ask the configured model to rephrase. Returns None if unavailable or it errors."""
    try:
        from ..grindalgo.gemini_analyst import rephrase_explanation
    except Exception:
        return None
    try:
        return rephrase_explanation(system=SYSTEM_PROMPT, text=text, evidence=evidence)
    except Exception as exc:
        log.info("Explanation rephrase failed: %s", str(exc)[:200])
        return None


def explain_leg(card, *, evidence=None) -> Explanation:
    """Explanation for one public selection card."""
    assessment = card.get("assessment") or {}
    risk_tier = card.get("risk_tier") or {}
    technical = card.get("technical_ref") or {}
    evidence = evidence or {**technical, **(risk_tier or {})}

    baseline = templates.explain_leg(
        state=card.get("state") or "",
        assessment_type=assessment.get("type") or "",
        family=assessment.get("market_family") or "",
        tier=risk_tier.get("code") or "",
        probability=risk_tier.get("estimated_success_percent"),
        risk_share=risk_tier.get("risk_share_percent"),
        evidence=evidence,
        availability=card.get("availability"),
        market=(card.get("your_pick") or {}).get("market") or "",
    )
    return _finalise(baseline, evidence)


def explain_ticket(public) -> Explanation:
    """Explanation for the ticket as a whole."""
    ticket = public.get("ticket") or {}
    killers = (public.get("ticket_killers") or {}).get("selections") or []
    baseline = templates.explain_ticket(
        success_percent=ticket.get("estimated_success_percent"),
        assessed_legs=ticket.get("assessed_legs_in_estimate") or 0,
        excluded_legs=ticket.get("legs_excluded_from_estimate") or 0,
        killers=killers,
        calibration=public.get("calibration") or {},
    )
    evidence = {
        "ticket": ticket,
        "killers": killers,
        "ticket_killers": public.get("ticket_killers") or {},
        "calibration": public.get("calibration") or {},
    }
    return _finalise(baseline, evidence)


def _finalise(baseline: str, evidence) -> Explanation:
    baseline_check = validate(baseline, evidence)
    if not baseline_check.ok:
        # The template is built from produced values, so this means a value went missing
        # from the evidence rather than that the text is unsafe. Log and ship it.
        log.warning("Template explanation failed validation: %s", baseline_check.reasons)

    if not _llm_enabled():
        return Explanation(baseline, "template", baseline_check.to_dict())

    rephrased = _rephrase(baseline, evidence)
    if not rephrased:
        return Explanation(baseline, "template", baseline_check.to_dict())

    check = validate(rephrased, evidence)
    if not check.ok:
        log.info("Discarded model explanation: %s", check.reasons)
        return Explanation(baseline, "template", check.to_dict())
    return Explanation(rephrased, "model", check.to_dict())
