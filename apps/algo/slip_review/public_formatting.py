"""Public slip-review display formatting helpers."""


def float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def success_percent_display(value):
    parsed = float_or_none(value)
    if parsed is None:
        return None
    if parsed == 0:
        return "0%"
    if 0 < parsed < 0.01:
        return "<0.01%"
    return f"{round(parsed, 2)}%"


def round_percent(value):
    parsed = float_or_none(value)
    return round(parsed * 100, 1) if parsed is not None else None


def fair_odds(probability):
    parsed = float_or_none(probability)
    if parsed is None or parsed <= 0:
        return None
    return round(1 / parsed, 2)


def implied_probability_from_odds(odds):
    parsed = float_or_none(odds)
    if parsed is None or parsed <= 1:
        return None
    return 1 / parsed


def probability_gap(model_probability, market_probability):
    if model_probability is None or market_probability is None:
        return None
    return round((model_probability - market_probability) * 100, 1)


def gap_level(gap_points):
    gap = abs(float_or_none(gap_points) or 0)
    if gap >= 15:
        return "high"
    if gap >= 8:
        return "medium"
    return "low"


def value_rating(model_probability, offered_odds):
    market_probability = implied_probability_from_odds(offered_odds)
    gap = probability_gap(model_probability, market_probability)
    if gap is None:
        return "unknown"
    if gap >= 5:
        return "positive_value"
    if gap <= -5:
        return "poor_value"
    return "near_fair"


def combined_odds(values):
    odds = [value for value in values if value and value > 1]
    if not odds:
        return None
    total = 1.0
    for value in odds:
        total *= value
    return round(total, 2)


def public_score(value):
    value = float_or_none(value)
    return int(round(value)) if value is not None else None


def public_confidence_label(score):
    score = float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 90:
        return "Exceptional"
    if score >= 80:
        return "Very Strong"
    if score >= 70:
        return "Strong"
    if score >= 60:
        return "Moderate"
    if score >= 50:
        return "Borderline"
    if score >= 40:
        return "Low"
    return "Very Low"


def public_ticket_label(score):
    score = float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 75:
        return "Strong"
    if score >= 65:
        return "Good"
    if score >= 55:
        return "Playable"
    if score >= 40:
        return "Risky"
    return "Poor"


__all__ = [
    "combined_odds",
    "fair_odds",
    "float_or_none",
    "gap_level",
    "implied_probability_from_odds",
    "probability_gap",
    "public_confidence_label",
    "public_score",
    "public_ticket_label",
    "round_percent",
    "success_percent_display",
    "value_rating",
]
