"""How well can we actually evaluate a market for a given fixture?

This sits in scoring rather than catalog because the answer depends on whether
a fitted model exists and how good it is — knowledge that lives here. Catalog
is below scoring and cannot ask this itself, so anything needing a capability
verdict calls scoring.
"""
from __future__ import annotations

from betpreneur.modules.markets.api import SCORE_MATRIX_ENGINE

#: Confidence ceiling per model data-quality band. A model fitted on thin data
#: must not be able to present as a strong read.
MODEL_CONFIDENCE_CAPS = {"strong": 85, "medium": 75, "limited": 62, "poor": 0}


def model_backed_capability(family: str, data_quality: str) -> dict:
    """
    Capability payload for a market served by the fitted score model.

    The capability layer was written assuming StatPal snapshots were the only data
    source; a matrix-derived market would otherwise be scored as having zero coverage
    and dropped. Its data quality comes from the league fit instead.
    """
    quality = str(data_quality or "poor").lower()
    cap = MODEL_CONFIDENCE_CAPS.get(quality, 0)
    warnings = []
    if quality in {"limited", "poor"}:
        warnings.append("thin_league_sample")
    if quality == "poor":
        warnings.append("no_expected_goals_available")
    return {
        "market": {"family": family},
        "support_level": "full" if quality in {"strong", "medium"} else "medium",
        "data_quality": quality,
        "confidence_cap": cap,
        "scoreable": quality != "poor",
        "required_snapshots": [],
        "available_snapshots": [],
        "missing_snapshots": [],
        "coverage_percent": 100.0 if quality != "poor" else 0.0,
        "warnings": warnings,
        "reason": f"Served by the fitted league score model at {quality} data quality.",
    }


def _detailed_stats_supports_count_market(family: str, statpal_context) -> bool:
    summary = ((((statpal_context or {}).get("snapshots") or {}).get("detailed_stats") or {}).get("summary") or {})
    if not summary:
        return False
    if family in {"corners_total", "team_corners", "corner_range", "team_corner_range", "corners_result", "corner_handicap"}:
        return summary.get("home_corners") is not None and summary.get("away_corners") is not None
    if family in {"cards_total", "team_cards", "booking_points", "cards", "cards_result"}:
        return any(
            summary.get(key) is not None
            for key in ("home_yellow_cards", "away_yellow_cards", "home_red_cards", "away_red_cards", "total_cards", "booking_points")
        )
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        return summary.get("home_shots_on_target") is not None and summary.get("away_shots_on_target") is not None
    return False


def capability_for_descriptor(descriptor, *, fixture=None, statpal_context=None):
    """
    Capability for a market, routed by whichever engine actually serves it.

    Snapshot coverage is only the right yardstick for the StatPal advisory path. Judging
    a matrix- or count-model market that way caps it at the coverage of snapshots it
    never needed, which silently prevents it from ever being recommended.
    """
    from betpreneur.modules.markets.api import (
        COUNT_MODEL_ENGINE,
        evaluator_for,
        market_capability_service,
    )

    spec = evaluator_for(getattr(descriptor, "family", ""))
    if spec is None:
        return market_capability_service.assess(descriptor, statpal_context=statpal_context or {}).to_dict()

    if spec.engine == SCORE_MATRIX_ENGINE:
        from betpreneur.modules.scoring.services.service import score_model_service

        game = fixture or {}
        rates = score_model_service.rates_for_fixture(
            league_id=game.get("statpal_provider_competition_id") or game.get("code") or game.get("league_id") or "",
            home_team_name=game.get("hname") or game.get("home_team") or "",
            away_team_name=game.get("aname") or game.get("away_team") or "",
        )
        return model_backed_capability(descriptor.family, rates.data_quality if rates.usable else "poor")

    if spec.engine == COUNT_MODEL_ENGINE:
        from betpreneur.modules.scoring.services.rate_profiles import team_rate_profile_service

        game = fixture or {}
        context = statpal_context or game.get("statpal_context") or {}
        home = team_rate_profile_service.profile_for(
            team_id=str(game.get("statpal_home_team_id") or ""), team_name=game.get("hname") or game.get("home_team") or ""
        )
        away = team_rate_profile_service.profile_for(
            team_id=str(game.get("statpal_away_team_id") or ""), team_name=game.get("aname") or game.get("away_team") or ""
        )
        available = [profile for profile in (home, away) if profile is not None]
        if not available and _detailed_stats_supports_count_market(descriptor.family, context):
            quality = "limited"
        elif not available:
            quality = "poor"
        elif len(available) == 2 and min(profile.matches for profile in available) >= 8:
            quality = "medium"
        else:
            quality = "limited"
        return model_backed_capability(descriptor.family, quality)

    return market_capability_service.assess(descriptor, statpal_context=statpal_context or {}).to_dict()
