"""
Corner and card markets, modelled from cached team rates.

Replaces the previous corners/cards evaluators, which read their inputs from the
match-stats endpoint. That endpoint carries no corner data and no aggregated card
counts, so the model never fired: every corners market returned a flat 52 and every
cards market a flat 50, **ignoring the line entirely** — `Over 9.5` and `Over 12.5`
scored identically, while being reported as quantitative assessments.

As with the score matrix, no data means decline. A constant is worse than an absence
because it is indistinguishable from a real answer.
"""

from __future__ import annotations

from ..scoring import counts
from ..scoring.rate_profiles import team_rate_profile_service

CORNER_FAMILIES = {"corners_total", "team_corners"}
CARD_FAMILIES = {"cards_total", "team_cards", "booking_points", "cards"}

# Booking points are scored 10 per yellow and 25 per red on the standard scale; the
# line arrives in points, so convert it to an equivalent booking count.
BOOKING_POINTS_PER_CARD = 10.0


def _line(descriptor):
    try:
        return float(descriptor.line)
    except (TypeError, ValueError):
        return None


def _profiles(fixture):
    fixture = fixture or {}
    home = team_rate_profile_service.profile_for(
        team_id=str(fixture.get("statpal_home_team_id") or fixture.get("hid") or ""),
        team_name=fixture.get("hname") or fixture.get("home_team") or "",
    )
    away = team_rate_profile_service.profile_for(
        team_id=str(fixture.get("statpal_away_team_id") or fixture.get("aid") or ""),
        team_name=fixture.get("aname") or fixture.get("away_team") or "",
    )
    return home, away


def _forecast(descriptor, home_profile, away_profile):
    family = descriptor.family
    team = descriptor.team if descriptor.team in {"home", "away"} else ""

    if family in CORNER_FAMILIES:
        if family == "team_corners" and team:
            profile = home_profile if team == "home" else away_profile
            return counts.expected_team_corners(profile, side=team), "corners"
        return counts.expected_corners(home_profile, away_profile), "corners"

    if family in CARD_FAMILIES:
        if family == "team_cards" and team:
            profile = home_profile if team == "home" else away_profile
            return counts.expected_team_cards(profile, side=team), "cards"
        return counts.expected_cards(home_profile, away_profile), "cards"

    return None, ""


def evaluate(descriptor, *, fixture=None, **_ignored) -> dict:
    line = _line(descriptor)
    if line is None:
        return {
            "available": False, "score": None, "status": "unsupported",
            "basis": "count_market_no_line",
            "evidence": {"market_family": descriptor.family},
            "warnings": ["missing_line"],
            "message": "This market has no line to evaluate.",
        }

    home_profile, away_profile = _profiles(fixture)
    forecast, kind = _forecast(descriptor, home_profile, away_profile)
    if forecast is None:
        return {
            "available": False, "score": None, "status": "unsupported",
            "basis": "count_market_unsupported_family",
            "evidence": {"market_family": descriptor.family},
            "warnings": ["family_not_supported"],
            "message": "This market family is not handled by the count model.",
        }

    if not forecast.sources:
        # No team profile at all: decline rather than publish the league average.
        return {
            "available": False, "score": None, "status": "needs_data",
            "basis": "count_market_no_team_rates",
            "evidence": {"market_family": descriptor.family, "line": line},
            "warnings": ["no_team_rate_profile"],
            "message": "No corner or card history is available for these teams yet.",
        }

    effective_line = line
    if descriptor.family == "booking_points":
        effective_line = line / BOOKING_POINTS_PER_CARD

    side = (descriptor.selection or descriptor.side or "over").lower()
    win, push = counts.poisson_over_under(forecast.expected, effective_line, side)
    live = 1.0 - push
    probability = win / live if live > 1e-9 else 0.0

    warnings = []
    if forecast.thin:
        warnings.append("thin_team_sample")
    if len(forecast.sources) < 2 and descriptor.family in {"corners_total", "cards_total"}:
        warnings.append("one_sided_team_rates")

    return {
        "available": True,
        "score": round(probability * 100, 1),
        "probability": round(probability, 6),
        "push_probability": round(push, 6),
        "status": "modelled",
        "basis": f"{kind}_count_model",
        "evidence": {
            "market_family": descriptor.family,
            "line": line,
            "effective_line": round(effective_line, 3),
            f"expected_{kind}": forecast.expected,
            "sources": list(forecast.sources),
            "sample_matches": forecast.matches,
        },
        "warnings": warnings,
        "message": (
            f"Expected {forecast.expected} {kind} against a line of {line}."
        ),
    }
