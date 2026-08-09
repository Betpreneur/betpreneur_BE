"""
Corner, card and shots-on-target markets, modelled from cached team rates.

Replaces the previous corners/cards evaluators that published constants when the
expected-count inputs were missing. Team rate profiles remain the main source, while a
fresh StatPal detailed-stats snapshot can now fill the immediate fixture values from
the league match-stats endpoint.

As with the score matrix, no data means decline. A constant is worse than an absence
because it is indistinguishable from a real answer.
"""

from __future__ import annotations

from dataclasses import replace

from ..scoring import counts
from ..scoring.rate_profiles import team_rate_profile_service

CORNER_FAMILIES = {
    "corners_total", "team_corners", "corner_range", "team_corner_range",
    "corners_result", "corner_handicap",
}
RANGE_FAMILIES = {"corner_range", "team_corner_range"}
RESULT_FAMILIES = {"corners_result"}
HANDICAP_FAMILIES = {"corner_handicap"}
CARD_RESULT_FAMILIES = {"cards_result"}
CARD_FAMILIES = {"cards_total", "team_cards", "booking_points", "cards", "cards_result"}
SOT_FAMILIES = {"shots_on_target_total", "team_shots_on_target"}

# Booking points are scored 10 per yellow and 25 per red on the standard scale; the
# line arrives in points, so convert it to an equivalent booking count.
BOOKING_POINTS_PER_CARD = 10.0
PERIOD_EXPECTATION_FACTORS = {
    "1st_half": 0.45,
    "first_half": 0.45,
    "2nd_half": 0.55,
    "second_half": 0.55,
}


def _line(descriptor):
    try:
        return float(descriptor.line)
    except (TypeError, ValueError):
        return None


def _profiles(fixture):
    fixture = fixture or {}
    home = team_rate_profile_service.profile_for(
        team_id=str(fixture.get("statpal_home_team_id") or ""),
        team_name=fixture.get("hname") or fixture.get("home_team") or "",
    )
    away = team_rate_profile_service.profile_for(
        team_id=str(fixture.get("statpal_away_team_id") or ""),
        team_name=fixture.get("aname") or fixture.get("away_team") or "",
    )
    return home, away


def _detailed_summary(fixture):
    snapshots = (((fixture or {}).get("statpal_context") or {}).get("snapshots") or {})
    return ((snapshots.get("detailed_stats") or {}).get("summary") or {})


def _summary_number(summary, *keys):
    for key in keys:
        try:
            value = summary.get(key)
            if value not in (None, "", "-"):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _kind_label(kind: str) -> str:
    return {
        "corners": "corner",
        "cards": "card",
        "shots_on_target": "shots-on-target",
    }.get(kind, kind)


def _statpal_forecast(descriptor, fixture, *, kind: str, team: str = ""):
    summary = _detailed_summary(fixture)
    if not summary:
        return None
    if kind == "corners":
        home = _summary_number(summary, "home_corners")
        away = _summary_number(summary, "away_corners")
    elif kind == "shots_on_target":
        home = _summary_number(summary, "home_shots_on_target", "home_sot")
        away = _summary_number(summary, "away_shots_on_target", "away_sot")
    else:
        home_yellows = _summary_number(summary, "home_yellow_cards")
        home_reds = _summary_number(summary, "home_red_cards")
        away_yellows = _summary_number(summary, "away_yellow_cards")
        away_reds = _summary_number(summary, "away_red_cards")
        home = (home_yellows or 0) + (home_reds or 0) if home_yellows is not None or home_reds is not None else None
        away = (away_yellows or 0) + (away_reds or 0) if away_yellows is not None or away_reds is not None else None
        if descriptor.family == "booking_points":
            booking_points = _summary_number(summary, "booking_points")
            if booking_points is not None:
                return counts.CountForecast(
                    expected=round(max(0.1, booking_points / BOOKING_POINTS_PER_CARD), 3),
                    sources=("statpal_detailed_stats",),
                    matches=counts.SHRINKAGE_MATCHES,
                )
        total_cards = _summary_number(summary, "total_cards")
        if total_cards is not None and not team:
            return counts.CountForecast(
                expected=round(max(0.1, total_cards), 3),
                sources=("statpal_detailed_stats",),
                matches=counts.SHRINKAGE_MATCHES,
            )

    if team == "home" and home is not None:
        expected = home
    elif team == "away" and away is not None:
        expected = away
    elif not team and home is not None and away is not None:
        expected = home + away
    else:
        return None
    return counts.CountForecast(
        expected=round(max(0.1, expected), 3),
        sources=("statpal_detailed_stats",),
        matches=counts.SHRINKAGE_MATCHES,
    )


def _forecast(descriptor, home_profile, away_profile):
    family = descriptor.family
    team = descriptor.team if descriptor.team in {"home", "away"} else ""

    if family in CORNER_FAMILIES:
        if family in {"team_corners", "team_corner_range"} and team:
            profile = home_profile if team == "home" else away_profile
            return counts.expected_team_corners(profile, side=team), "corners"
        return counts.expected_corners(home_profile, away_profile), "corners"

    if family in CARD_FAMILIES:
        if family == "team_cards" and team:
            profile = home_profile if team == "home" else away_profile
            return counts.expected_team_cards(profile, side=team), "cards"
        return counts.expected_cards(home_profile, away_profile), "cards"

    if family in SOT_FAMILIES:
        if family == "team_shots_on_target" and team:
            profile = home_profile if team == "home" else away_profile
            return counts.expected_team_shots_on_target(profile, side=team), "shots_on_target"
        return counts.expected_shots_on_target(home_profile, away_profile), "shots_on_target"

    return None, ""


def _period_adjusted(forecast, descriptor):
    factor = PERIOD_EXPECTATION_FACTORS.get(str(getattr(descriptor, "period", "") or "").lower(), 1.0)
    if factor == 1.0:
        return forecast, factor
    return replace(forecast, expected=round(max(0.1, forecast.expected * factor), 3)), factor


def _team_corner_forecasts(descriptor, home_profile, away_profile, *, fixture=None):
    home = counts.expected_team_corners(home_profile, side="home")
    away = counts.expected_team_corners(away_profile, side="away")
    if not home.sources:
        home = _statpal_forecast(descriptor, fixture, kind="corners", team="home") or home
    if not away.sources:
        away = _statpal_forecast(descriptor, fixture, kind="corners", team="away") or away
    home, factor = _period_adjusted(home, descriptor)
    away, _ = _period_adjusted(away, descriptor)
    return home, away, factor


def _team_card_forecasts(descriptor, home_profile, away_profile, *, fixture=None):
    home = counts.expected_team_cards(home_profile, side="home")
    away = counts.expected_team_cards(away_profile, side="away")
    if not home.sources:
        home = _statpal_forecast(descriptor, fixture, kind="cards", team="home") or home
    if not away.sources:
        away = _statpal_forecast(descriptor, fixture, kind="cards", team="away") or away
    home, factor = _period_adjusted(home, descriptor)
    away, _ = _period_adjusted(away, descriptor)
    return home, away, factor


def evaluate(descriptor, *, fixture=None, **_ignored) -> dict:
    line = _line(descriptor)
    is_range_market = descriptor.family in RANGE_FAMILIES
    is_result_market = descriptor.family in RESULT_FAMILIES
    is_card_result_market = descriptor.family in CARD_RESULT_FAMILIES
    is_handicap_market = descriptor.family in HANDICAP_FAMILIES
    if line is None and not is_range_market and not is_result_market and not is_card_result_market:
        return {
            "available": False, "score": None, "status": "unsupported",
            "basis": "count_market_no_line",
            "evidence": {"market_family": descriptor.family},
            "warnings": ["missing_line"],
            "message": "This market has no line to evaluate.",
        }

    home_profile, away_profile = _profiles(fixture)

    if is_result_market or is_card_result_market or is_handicap_market:
        if is_card_result_market:
            home_forecast, away_forecast, period_factor = _team_card_forecasts(
                descriptor, home_profile, away_profile, fixture=fixture
            )
            kind = "cards"
        else:
            home_forecast, away_forecast, period_factor = _team_corner_forecasts(
                descriptor, home_profile, away_profile, fixture=fixture
            )
            kind = "corners"
        if not home_forecast.sources and not away_forecast.sources:
            return {
                "available": False, "score": None, "status": "needs_data",
                "basis": "count_market_no_team_rates",
                "evidence": {"market_family": descriptor.family},
                "warnings": ["no_team_rate_profile"],
                "message": f"No {_kind_label(kind)} history is available for these teams yet.",
            }
        side = (descriptor.selection or descriptor.side or "").lower()
        if is_handicap_market:
            probabilities = counts.poisson_handicap(home_forecast.expected, away_forecast.expected, line)
            push = probabilities["push"]
            live = 1.0 - push
            raw_probability = probabilities.get(side)
            probability = raw_probability / live if raw_probability is not None and live > 1e-9 else None
            basis = "corners_handicap_count_model"
            invalid_basis = "count_market_invalid_handicap_side"
            invalid_warning = "invalid_handicap_side"
        else:
            probabilities = counts.poisson_three_way(home_forecast.expected, away_forecast.expected)
            push = 0.0
            probability = probabilities.get(side)
            basis = f"{kind}_result_count_model"
            invalid_basis = "count_market_invalid_result_side"
            invalid_warning = "invalid_result_side"
        if probability is None:
            return {
                "available": False, "score": None, "status": "unsupported",
                "basis": invalid_basis,
                "evidence": {"market_family": descriptor.family, "side": side},
                "warnings": [invalid_warning],
                "message": f"This {_kind_label(kind)} count market has an invalid side.",
            }

        warnings = []
        if home_forecast.thin or away_forecast.thin:
            warnings.append("thin_team_sample")
        if len(home_forecast.sources) + len(away_forecast.sources) < 2:
            warnings.append("one_sided_team_rates")
        if period_factor != 1.0:
            warnings.append("period_expectation_scaled")

        return {
            "available": True,
            "score": round(probability * 100, 1),
            "probability": round(probability, 6),
            "push_probability": round(push, 6),
            "status": "modelled",
            "basis": basis,
            "evidence": {
                "market_family": descriptor.family,
                "side": side,
                "line": line,
                f"expected_home_{kind}": home_forecast.expected,
                f"expected_away_{kind}": away_forecast.expected,
                "period_factor": period_factor,
                "home_probability": round(probabilities["home"], 6),
                "draw_probability": round(probabilities.get("draw", 0.0), 6),
                "away_probability": round(probabilities["away"], 6),
                "push_probability": round(push, 6),
                "sources": list(home_forecast.sources + away_forecast.sources),
                "sample_matches": min(home_forecast.matches, away_forecast.matches),
            },
            "warnings": warnings,
            "message": (
                f"Expected {_kind_label(kind)} counts: home {home_forecast.expected}, away {away_forecast.expected}."
            ),
        }

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
        team = descriptor.team if descriptor.team in {"home", "away"} and descriptor.family in {"team_corners", "team_cards", "team_shots_on_target"} else ""
        statpal_forecast = _statpal_forecast(descriptor, fixture, kind=kind, team=team)
        if statpal_forecast is not None:
            forecast = statpal_forecast

    if not forecast.sources:
        # No team profile at all: decline rather than publish the league average.
        return {
            "available": False, "score": None, "status": "needs_data",
            "basis": "count_market_no_team_rates",
            "evidence": {"market_family": descriptor.family, "line": line},
            "warnings": ["no_team_rate_profile"],
            "message": f"No {_kind_label(kind)} history is available for these teams yet.",
        }

    forecast, period_factor = _period_adjusted(forecast, descriptor)

    if is_range_market:
        bucket = descriptor.selection or descriptor.side
        probability, parsed_range = counts.poisson_range(forecast.expected, bucket)
        lower, upper = parsed_range
        if probability is None:
            return {
                "available": False, "score": None, "status": "unsupported",
                "basis": "count_market_invalid_range",
                "evidence": {"market_family": descriptor.family, "range": bucket},
                "warnings": ["invalid_range"],
                "message": "This range market has an invalid bucket.",
            }

        warnings = []
        if forecast.thin:
            warnings.append("thin_team_sample")
        if len(forecast.sources) < 2 and descriptor.family == "corner_range":
            warnings.append("one_sided_team_rates")
        if period_factor != 1.0:
            warnings.append("period_expectation_scaled")

        return {
            "available": True,
            "score": round(probability * 100, 1),
            "probability": round(probability, 6),
            "push_probability": 0.0,
            "status": "modelled",
            "basis": f"{kind}_range_count_model",
            "evidence": {
                "market_family": descriptor.family,
                "range": bucket,
                "range_lower": lower,
                "range_upper": upper,
                f"expected_{kind}": forecast.expected,
                "period_factor": period_factor,
                "sources": list(forecast.sources),
                "sample_matches": forecast.matches,
            },
            "warnings": warnings,
            "message": (
                f"Expected {forecast.expected} {_kind_label(kind)} events; selected range is {bucket}."
            ),
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
    if len(forecast.sources) < 2 and descriptor.family in {"corners_total", "cards_total", "shots_on_target_total"}:
        warnings.append("one_sided_team_rates")
    if period_factor != 1.0:
        warnings.append("period_expectation_scaled")

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
            "period_factor": period_factor,
            "sources": list(forecast.sources),
            "sample_matches": forecast.matches,
        },
        "warnings": warnings,
        "message": (
            f"Expected {forecast.expected} {_kind_label(kind)} events against a line of {line}."
        ),
    }
