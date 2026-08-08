"""
Deterministic explanations.

The explanation layer is template-first on purpose: the review must be complete and
comprehensible without any language model. An LLM can only ever rephrase what is
written here, and only if it passes validation.

Every sentence is built from a value the model actually produced, which is what makes
the output safe to validate by construction.
"""

from __future__ import annotations

TIER_PHRASING = {
    "very_strong": "is well supported by the model",
    "strong": "is supported by the model",
    "borderline": "is borderline",
    "risky": "is risky",
    "avoid": "is the weakest kind of selection on this ticket",
    "unknown": "could not be assessed",
}

STATE_PHRASING = {
    "unknown_market": "We could not identify this market from the bookmaker's data.",
    "unmatched": "We could not find this fixture.",
    "ambiguous_fixture": "More than one fixture matched this selection.",
    "expired": "This fixture has already started.",
    "insufficient_data": "There is not enough data on this market to assess it.",
    "no_model": "We recognise this market but do not model it yet.",
}


def _percent(value):
    return None if value is None else round(float(value) * 100 if value <= 1 else float(value), 1)


def explain_leg(*, state, assessment_type, family="", tier="", probability=None,
                risk_share=None, evidence=None, availability=None, market="") -> str:
    """One paragraph for a single selection, built only from produced values."""
    evidence = evidence or {}

    if availability and availability.get("status") == "out":
        reason = availability.get("reason") or ""
        who = availability.get("player") or "This player"
        return (
            f"{who} is listed as unavailable{f' ({reason})' if reason else ''}, "
            "so this selection cannot be priced."
        )

    if state != "assessed":
        return STATE_PHRASING.get(state, "This selection could not be assessed.")

    if assessment_type != "quantitative_model":
        return (
            f"{market or 'This selection'} is recognised, but the current assessment is a "
            "supporting signal rather than a modelled probability, so no percentage is shown."
        )

    parts = []
    percent = _percent(probability)
    if percent is not None:
        parts.append(f"The model puts this at about {percent}%.")

    expected_home = evidence.get("expected_goals_home")
    expected_away = evidence.get("expected_goals_away")
    if expected_home is not None and expected_away is not None:
        parts.append(
            f"It expects around {expected_home} goals for the home side and {expected_away} for the away side."
        )

    for key, label in (("expected_corners", "corners"), ("expected_cards", "bookings")):
        if evidence.get(key) is not None:
            parts.append(f"It expects around {evidence[key]} {label} in this match.")

    if tier:
        parts.append(f"On this ticket it {TIER_PHRASING.get(tier, 'has been assessed')}.")

    if risk_share is not None:
        parts.append(f"It carries {round(float(risk_share), 1)}% of the ticket's overall risk.")

    quality = evidence.get("data_quality")
    if quality in {"limited", "poor"}:
        parts.append("The underlying sample is thin, so treat this with more caution than usual.")

    return " ".join(parts) if parts else "This selection has been assessed."


def explain_ticket(*, success_percent=None, assessed_legs=0, excluded_legs=0,
                   killers=None, calibration=None) -> str:
    """A short summary of the ticket as a whole."""
    parts = []
    if success_percent is None:
        parts.append("None of these selections could be assessed, so this ticket has no estimate.")
    else:
        parts.append(f"Across the legs we could model, this ticket lands about {success_percent}% of the time.")

    if excluded_legs:
        parts.append(
            f"{excluded_legs} of the selections could not be included in that estimate."
        )

    killers = killers or []
    if killers:
        share = round(sum(item.get("risk_share_percent") or 0 for item in killers), 1)
        count = len(killers)
        parts.append(
            f"{count} {'selection carries' if count == 1 else 'selections carry'} {share}% of the risk."
        )

    basis = (calibration or {}).get("basis")
    if basis == "prior":
        parts.append(
            "Not enough selections have settled yet to validate these estimates, so a "
            "deliberately conservative prior is used."
        )

    parts.append("These are model estimates, not predictions of what will happen.")
    return " ".join(parts)
