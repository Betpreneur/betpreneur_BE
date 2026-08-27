"""Scoring and ranking a market.

The judgement layer: how good a price is, how well a market fits the model,
which alternative genuinely improves on a user's pick, and what evidence backs
any of it. Pure — no ORM, no settings, no request. Extracted from the 11k-line
apps/algo/views.py, where none of it could be tested on its own.
"""
from __future__ import annotations

from betpreneur.modules.markets.api import (
    canonical_market_name,
    describe_market,
)

from .tiers import tier_for_confidence  # noqa: F401  (re-exported for callers)

MATCH_CHECKER_MEMORY_FLAGS = {
    "market_suppressed",
    "strategy_suppressed",
    "market_cooling",
    "strategy_cooling",
    "market_loss_streak",
    "market_recent_losses",
    "limited_market_history",
}


MATCH_CHECKER_SERIOUS_FLAGS = {
    "best_price_far_above_consensus",
    "wide_odds_market",
    "goal_line_boundary",
    "under35_blowout_risk",
    "nordic_under_volatility",
    "draw_boundary_risk",
}


def market_sort_value(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("eligible") else 0,
        market.get("confidence") or 0,
        market.get("ev") if market.get("ev") is not None else -999,
        market.get("odds") or 0,
    )


def _market_reviewer_score(market, reviewer_name):
    review = market.get("council_review") or {}
    for item in review.get("reviewers") or []:
        if item.get("reviewer") == reviewer_name:
            try:
                return float(item.get("score") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _bounded_ev_score(ev):
    if ev is None:
        return -12.0
    try:
        return max(-12.0, min(14.0, float(ev) * 35.0))
    except (TypeError, ValueError):
        return -12.0


def market_decision_rank(market):
    decision = str((market.get("council_review") or {}).get("decision") or "")
    if market.get("recommended"):
        return 4
    return {
        "approve": 3,
        "caution": 2,
        "not_reviewed": 1,
        "reject": 0,
    }.get(decision, 1)




def normalise_market_name(value):
    return " ".join(str(value or "").strip().lower().split())


def _canonical_market_name(value):
    return canonical_market_name(value)




def _match_checker_risk_penalty(risk_flags):
    flags = set(risk_flags or [])
    penalty = 0.0
    penalty += len(flags & MATCH_CHECKER_MEMORY_FLAGS) * 3.0
    penalty += len(flags & MATCH_CHECKER_SERIOUS_FLAGS) * 6.0
    penalty += max(0, len(flags) - 8) * 1.25
    return min(penalty, 34.0)




def match_checker_status(score):
    if score is None:
        return "unknown"
    if score >= 78:
        return "strong"
    if score >= 66:
        return "playable"
    if score >= 55:
        return "caution"
    return "avoid"


def _match_checker_warnings(market):
    flags = list(market.get("risk_flags") or [])
    warnings = []
    for flag in flags:
        if flag in MATCH_CHECKER_MEMORY_FLAGS or flag in MATCH_CHECKER_SERIOUS_FLAGS:
            warnings.append(flag)
    for flag in flags:
        if flag not in warnings:
            warnings.append(flag)
        if len(warnings) >= 6:
            break
    return warnings[:6]


def _match_checker_evidence(market):
    insights = market.get("insights") or {}
    league_trust = insights.get("league_trust") or {}
    calibration_trust = insights.get("calibration_trust") or {}
    review = market.get("council_review") or {}
    reviewers = review.get("reviewers") or []
    market_fit = _market_reviewer_score(market, "market_fit")
    scoreline_fit = _market_reviewer_score(market, "scoreline_pattern")
    sample_size = (
        league_trust.get("market_sample")
        or calibration_trust.get("sample")
        or league_trust.get("league_sample")
        or 0
    )
    historical_accuracy = (
        league_trust.get("market_hit_rate")
        or calibration_trust.get("hit_rate")
        or league_trust.get("league_hit_rate")
    )
    similar_market_roi = (
        league_trust.get("market_roi")
        if league_trust.get("market_roi") is not None
        else calibration_trust.get("roi")
    )
    return {
        "historical_accuracy": float_or_none(historical_accuracy),
        "similar_market_roi": float_or_none(similar_market_roi),
        "sample_size": int(sample_size or 0),
        "league_trust": league_trust.get("status", ""),
        "confidence_calibration": calibration_trust.get("status", ""),
        "market_fit_score": round(float(market_fit), 1) if market_fit is not None else None,
        "scoreline_fit_score": round(float(scoreline_fit), 1) if scoreline_fit is not None else None,
        "reviewer_count": len(reviewers),
    }


def match_checker_alternative_reason(submitted_market, alternative):
    alt_market = alternative.get("market") or "the suggested market"
    submitted = submitted_market or "the submitted market"
    evidence = alternative.get("advisory_evidence") or {}
    scoreline_fit = evidence.get("scoreline_fit_score")
    market_fit = evidence.get("market_fit_score")
    confidence = alternative.get("final_confidence") or alternative.get("confidence")
    scope = alternative.get("replacement_scope")
    if scope == "comparable_market":
        return f"{alt_market} is the stronger comparable market for this same selection type."
    if scope == "broad_fallback":
        return f"{alt_market} is a broader fallback because {submitted} is weak or lacks reliable scoring data."
    if scoreline_fit and scoreline_fit >= 70:
        return f"{alt_market} fits the match scoreline pattern better than {submitted}."
    if market_fit and market_fit >= 70:
        return f"{alt_market} has stronger market fit for this fixture than {submitted}."
    if confidence:
        return f"{alt_market} carries stronger match-specific confidence than {submitted}."
    return f"{alt_market} is the safer alternative from this match analysis."




def with_statpal_advisory(market, statpal_advisory):
    if not market:
        return None
    if not statpal_advisory or not statpal_advisory.get("available"):
        return market
    score = float_or_none(statpal_advisory.get("score"))
    if score is None:
        return market
    payload = dict(market)
    review = payload.get("council_review") or {}
    has_core_confidence = any(
        float_or_none(value) is not None
        for value in (
            review.get("final_confidence"),
            review.get("consensus_score"),
            payload.get("final_confidence"),
            payload.get("confidence"),
            payload.get("raw_confidence"),
        )
    )
    current_score = float_or_none(payload.get("advisory_score"))
    if not has_core_confidence or current_score is None:
        adjustment = 0.0
        adjusted_score = round(max(0, min(100, score)), 1)
        merge_mode = "primary"
    else:
        adjustment = max(-6.0, min(6.0, (score - 55.0) * 0.20))
        adjusted_score = round(max(0, min(100, current_score + adjustment)), 1)
        merge_mode = "adjustment"
    payload["advisory_score"] = adjusted_score
    payload["advisory_status"] = match_checker_status(adjusted_score)
    payload["statpal_advisory"] = statpal_advisory
    payload["advisory_basis"] = f"{payload.get('advisory_basis') or 'match_specific_analysis'}+statpal_context"
    warnings = list(payload.get("advisory_warnings") or [])
    warnings.extend(statpal_advisory.get("warnings") or [])
    payload["advisory_warnings"] = list(dict.fromkeys(warnings))[:8]
    evidence = dict(payload.get("advisory_evidence") or {})
    evidence["statpal_score"] = score
    evidence["statpal_adjustment"] = adjustment
    evidence["statpal_merge_mode"] = merge_mode
    evidence["statpal_basis"] = statpal_advisory.get("basis")
    evidence["statpal"] = statpal_advisory.get("evidence") or {}
    payload["advisory_evidence"] = evidence
    return payload


def _cap_advisory_score(score, market_capability):
    """
    The modelled probability, unchanged by data quality.

    Truncating the estimate at the data-quality ceiling collapsed distinct
    probabilities onto one number: with a cap of 75, six in ten markets landed on
    exactly 75; with a cap of 62, nineteen in twenty landed on 62. That is where the
    clustering came from, and it conflated two different questions.

    "How likely is this?" and "how much do we trust that estimate?" are now reported
    separately -- see `_data_confidence`. Data quality still constrains what we *claim*
    (the tier and the verdict), it just no longer rewrites the number.
    """
    parsed = float_or_none(score)
    if parsed is None:
        return None
    return round(max(0, min(100, parsed)), 1)


def _data_confidence(market_capability):
    """How much evidence stands behind the estimate, on the same 0-100 scale."""
    cap = float_or_none((market_capability or {}).get("confidence_cap"))
    return None if cap is None else round(max(0, min(100, cap)), 1)


def scored_claim(score, market_capability):
    """
    Split "how likely is this?" from "how much do we trust it?".

    Returns (probability, data_confidence, status, evidence_flags). The probability
    is reported as modelled; thin evidence is expressed by holding the *status*
    back, never by rewriting the number. Both the submitted-market path and the
    direct-analysis path go through here so the two cannot drift apart.
    """
    probability = _cap_advisory_score(score, market_capability)
    if probability is None:
        return None, _data_confidence(market_capability), "needs_data", {}
    confidence = _data_confidence(market_capability)
    status = match_checker_status(
        min(probability, confidence) if confidence is not None else probability
    )
    flags = {"data_confidence": confidence}
    if confidence is not None and probability > confidence:
        flags["claim_limited_by_data_quality"] = True
    return probability, confidence, status, flags


def _statpal_advisory_scored(statpal_advisory):
    return bool((statpal_advisory or {}).get("available")) and float_or_none((statpal_advisory or {}).get("score")) is not None


def effective_market_capability(market_capability, statpal_advisory):
    capability = dict(market_capability or {})
    if not _statpal_advisory_scored(statpal_advisory):
        return capability

    quality = str(capability.get("data_quality") or "").lower()
    cap = float_or_none(capability.get("confidence_cap"))
    if quality not in {"poor", "unsupported"} and (cap is None or cap > 0):
        return capability

    warnings = [
        warning
        for warning in (capability.get("warnings") or [])
        if warning not in {"no_expected_goals_available", "data_quality_poor", "data_quality_unsupported"}
    ]
    return {
        **capability,
        "support_level": capability.get("support_level") or "medium",
        "data_quality": "medium",
        "confidence_cap": max(cap or 0, 75),
        "scoreable": True,
        "coverage_percent": max(float_or_none(capability.get("coverage_percent")) or 0, 60.0),
        "warnings": list(dict.fromkeys(warnings)),
        "reason": "Scored by StatPal fallback context after the fitted model lacked enough fixture-specific inputs.",
    }


def with_market_capability(market, market_capability):
    if not market:
        return None
    payload = dict(market)
    market_capability = effective_market_capability(market_capability, payload.get("statpal_advisory"))
    payload["market_capability"] = market_capability or {}
    scored, confidence, status, flags = scored_claim(
        payload.get("advisory_score"), market_capability
    )
    if scored is not None:
        payload["advisory_score"] = scored
        payload["data_confidence"] = confidence
        payload["advisory_status"] = status
        payload["advisory_evidence"] = {
            **(payload.get("advisory_evidence") or {}),
            "market_capability": market_capability or {},
            **flags,
        }
    warnings = list(payload.get("advisory_warnings") or [])
    warnings.extend((market_capability or {}).get("warnings") or [])
    data_quality = (market_capability or {}).get("data_quality")
    if data_quality in {"limited", "poor", "unsupported"}:
        warnings.append(f"data_quality_{data_quality}")
    payload["advisory_warnings"] = list(dict.fromkeys(warnings))[:10]
    return payload


def market_family_group(market):
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    family = taxonomy.get("family") or ""
    if family in {
        "total_goals",
        "team_total_goals",
        "btts",
        "clean_sheet",
        "first_to_score",
        "last_to_score",
        "result_total_goals",
        "double_chance_total_goals",
        "total_btts",
        "result_btts",
        "both_halves_total_goals",
    }:
        return "goals"
    if family in {"corners_total", "team_corners", "corners"}:
        return "corners"
    if family in {"cards_total", "team_cards", "booking_points", "cards"}:
        return "cards"
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        return "shots_on_target"
    if str(family).startswith("player_"):
        return "player"
    if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
        return "result"
    return family or "unknown"


def replacement_scope(selected_market, candidate):
    selected_group = market_family_group(selected_market)
    candidate_group = market_family_group(candidate)
    if selected_group == candidate_group:
        return "comparable_market"
    return "broad_fallback"


def _result_market_side(market):
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    return taxonomy.get("family") or "", taxonomy.get("side") or "", float_or_none(taxonomy.get("line"))


RESULT_THESIS_FAMILIES = frozenset(
    {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}
)


def result_thesis_side(market):
    """
    Which team a result selection is backing, or "" when it backs neither.

    `DC: 12` returns "" deliberately: it wins when *either* side wins, so it does not
    carry a direction and cannot stand in for one.
    """
    family, side, _line = _result_market_side(market)
    if family == "match_result":
        return side if side in {"home", "away", "draw"} else ""
    if family == "double_chance":
        return {"home_or_draw": "home", "draw_or_away": "away"}.get(side, "")
    if family in {"draw_no_bet", "asian_handicap", "handicap"}:
        return side if side in {"home", "away"} else ""
    return ""


def result_replacement_preserves_user_thesis(selected_market, replacement_market):
    """
    A replacement may change the market, never the team being backed.

    This used to run only when the *user's* pick was a `match_result`, so a double chance
    was unguarded: someone who backed `DC: 1X` (home or draw) was told to switch to
    `DC: X2` (draw or away) -- the opposite team -- in three fixtures of a single slip.
    """
    selected_family, selected_side, _ = _result_market_side(selected_market)
    candidate_family, candidate_side, candidate_line = _result_market_side(replacement_market)
    if selected_family not in RESULT_THESIS_FAMILIES:
        return True
    if candidate_family not in RESULT_THESIS_FAMILIES:
        return True

    if selected_family == "match_result" and selected_side == "draw":
        return candidate_family == "match_result" and candidate_side == "draw"

    selected_lean = result_thesis_side(selected_market)
    if not selected_lean:
        # No direction to preserve (`DC: 12`): only the same market qualifies.
        return candidate_family == selected_family and candidate_side == selected_side

    if result_thesis_side(replacement_market) != selected_lean:
        return False
    if candidate_family in {"asian_handicap", "handicap"}:
        # A negative line asks the side to win by more, which is a harder bet than the
        # one the user made, not a safer expression of it.
        return candidate_line is None or candidate_line >= 0
    return True


def line_replacement_preserves_user_thesis(selected_market, replacement_market):
    selected = (selected_market or {}).get("market_taxonomy") or describe_market((selected_market or {}).get("market")).to_dict()
    candidate = (replacement_market or {}).get("market_taxonomy") or describe_market((replacement_market or {}).get("market")).to_dict()
    selected_family = selected.get("family") or ""
    candidate_family = candidate.get("family") or ""
    selected_group = market_family_group(selected_market)
    candidate_group = market_family_group(replacement_market)
    guarded_groups = {"corners"}
    if selected_group not in guarded_groups or candidate_group != selected_group:
        if selected_group != "goals" or candidate_group != "goals":
            return True
        selected_side = selected.get("selection") or selected.get("side") or ""
        candidate_side = candidate.get("selection") or candidate.get("side") or ""
        if selected_family == "team_total_goals":
            return (
                candidate_family == "team_total_goals"
                and (selected.get("period") or "") == (candidate.get("period") or "")
                and (selected.get("team") or "") == (candidate.get("team") or "")
                and (not selected_side or not candidate_side or selected_side == candidate_side)
            )
        if selected_family == "total_goals":
            return (
                candidate_family == "total_goals"
                and (selected.get("period") or "") == (candidate.get("period") or "")
                and (not selected_side or not candidate_side or selected_side == candidate_side)
            )
        if selected_family in {"btts", "total_btts", "result_btts"}:
            return candidate_side != "under"
        return True
    if selected_family != candidate_family:
        return False
    if (selected.get("period") or "") != (candidate.get("period") or ""):
        return False
    if (selected.get("team") or "") != (candidate.get("team") or ""):
        return False
    selected_side = selected.get("selection") or selected.get("side") or ""
    candidate_side = candidate.get("selection") or candidate.get("side") or ""
    if selected_side and candidate_side and selected_side != candidate_side:
        return False
    return True


def broad_fallback_candidate_allowed(selected_market, candidate):
    if not selected_market or not candidate:
        return True
    candidate_group = market_family_group(candidate)
    if candidate_group in {"cards", "shots_on_target"}:
        return False
    taxonomy = (candidate or {}).get("market_taxonomy") or describe_market((candidate or {}).get("market")).to_dict()
    side = str(taxonomy.get("selection") or taxonomy.get("side") or "").lower()
    line = float_or_none(taxonomy.get("line"))
    family = taxonomy.get("family") or ""
    if side == "under":
        if family == "total_goals" and line is not None and line >= 4.5:
            return False
        if family == "team_total_goals" and line is not None and line >= 2.5:
            return False
        if candidate_group == "corners" and line is not None and line >= 10.5:
            return False
    return True


SPECIALIST_REPLACEMENT_GROUPS = frozenset({"player", "corners", "cards", "shots_on_target", "unknown"})


def allows_broad_replacement(selected_market):
    """
    Whether this pick may be replaced by a market from a different family.

    Previously `group not in {"unknown"}`, which allowed it for everything -- the guard
    existed in name only, and only the candidate-selection path enforced anything. The
    verdict path let a player pick be "improved" into a goals total.
    """
    return market_family_group(selected_market) not in SPECIALIST_REPLACEMENT_GROUPS


def market_edge(market):
    """
    Points by which this market beats a league-average fixture, or None.

    Raw probabilities are not comparable across families: a double chance covers two of
    three outcomes and sits near 70% almost everywhere, an Under 4.5 near 88%, a home win
    near 40%. Ranking on the raw number therefore prefers whichever market has the highest
    base rate, regardless of whether this fixture is any good for it -- which is how a
    thirteen-leg slip came back with eight legs "improved" into double chances and unders
    and its odds cut from 20.05 to 3.24.
    """
    evidence = (market or {}).get("advisory_evidence") or {}
    edge = float_or_none(evidence.get("edge_points"))
    if edge is None:
        statpal = (market or {}).get("statpal_advisory") or {}
        edge = float_or_none(((statpal.get("evidence") or {})).get("edge_points"))
    return edge


def _fit_from_line(expected, line, side, *, scale):
    expected = float_or_none(expected)
    line = float_or_none(line)
    side = str(side or "").lower()
    if expected is None or line is None or not side:
        return None
    margin = expected - line if side == "over" else line - expected
    return round(max(0, min(100, 50 + (margin / max(scale, 0.1)) * 100)), 1)


def _fit_from_probability(probability, *, neutral=50):
    probability = float_or_none(probability)
    if probability is None:
        return None
    # Treat the model probability as the family fit when there is no natural line.
    # Neutral is kept for future calibration, but today probabilities are already 0-100.
    return round(max(0, min(100, probability if probability >= neutral else probability)), 1)


def _first_non_null(*values):
    for value in values:
        parsed = float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _result_fit_from_payload(payload, side):
    side = str(side or "").lower()
    if side == "home":
        return _fit_from_probability(_first_non_null(payload.get("home_win_probability"), payload.get("home_probability")))
    if side == "away":
        return _fit_from_probability(_first_non_null(payload.get("away_win_probability"), payload.get("away_probability")))
    if side == "draw":
        return _fit_from_probability(payload.get("draw_probability"))
    if side == "home_or_draw":
        home = _first_non_null(payload.get("home_win_probability"), payload.get("home_probability"))
        draw = float_or_none(payload.get("draw_probability"))
        if home is not None and draw is not None:
            return _fit_from_probability(home + draw)
    if side == "draw_or_away":
        draw = float_or_none(payload.get("draw_probability"))
        away = _first_non_null(payload.get("away_win_probability"), payload.get("away_probability"))
        if draw is not None and away is not None:
            return _fit_from_probability(draw + away)
    if side == "home_or_away":
        home = _first_non_null(payload.get("home_win_probability"), payload.get("home_probability"))
        away = _first_non_null(payload.get("away_win_probability"), payload.get("away_probability"))
        if home is not None and away is not None:
            return _fit_from_probability(home + away)
    return None


def market_profile_fit_score(market):
    """
    How well the candidate matches the fixture shape behind its own model evidence.

    Probability alone rewards broad markets. A market with an 11.5 corner under can have
    a high hit rate in almost every fixture, but that does not mean it is the market this
    fixture is specifically pointing toward. This score answers the first question:
    "does the profile actually lean toward this side of this line?"
    """
    evidence = (market or {}).get("advisory_evidence") or {}
    intelligence_fit = float_or_none(evidence.get("team_intelligence_fit_score"))
    if intelligence_fit is not None:
        return round(max(0, min(100, intelligence_fit)), 1)
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    family = taxonomy.get("family") or ""
    side = str(taxonomy.get("selection") or taxonomy.get("side") or "").lower()
    line = float_or_none(taxonomy.get("line"))
    for payload in (evidence, statpal):
        if line is None:
            line = float_or_none(payload.get("line"))
        if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
            fit = _result_fit_from_payload(payload, side)
            if fit is not None:
                return fit
        if family in {"btts", "total_btts", "result_btts"}:
            probability = _first_non_null(
                payload.get("btts_probability"),
                payload.get("btts_yes_probability"),
                payload.get("estimated_probability"),
            )
            if side in {"no", "btts_no"} and probability is not None:
                probability = 100 - probability
            fit = _fit_from_probability(probability)
            if fit is not None:
                return fit
        expected = None
        if family in {"corners_total", "team_corners"}:
            expected = _first_non_null(
                payload.get("expected_total_corners"),
                payload.get("expected_corners"),
                payload.get("home_expected_corners") if taxonomy.get("team") == "home" else None,
                payload.get("away_expected_corners") if taxonomy.get("team") == "away" else None,
            )
            scale = 4.0 if taxonomy.get("period") == "first_half" else 10.0
        elif family in {"cards_total", "team_cards", "booking_points"}:
            expected = _first_non_null(
                payload.get("expected_total_cards")
                if family != "booking_points"
                else None,
                payload.get("expected_cards") if family != "booking_points" else None,
                payload.get("expected_booking_points"),
                payload.get("booking_points"),
                payload.get("total_cards"),
            )
            intensity = _first_non_null(
                payload.get("referee_cards_per_game"),
                payload.get("referee_card_average"),
                payload.get("expected_fouls"),
                payload.get("match_intensity_score"),
            )
            if expected is not None and intensity is not None and side == "over":
                expected += min(1.0, max(0.0, (intensity - 4.0) * 0.2))
            scale = 25.0 if family == "booking_points" else 4.0
        elif family in {"shots_on_target_total", "team_shots_on_target"}:
            expected = _first_non_null(
                payload.get("expected_shots_on_target")
                or payload.get("expected_total_shots_on_target"),
                payload.get("home_expected_shots_on_target") if taxonomy.get("team") == "home" else None,
                payload.get("away_expected_shots_on_target") if taxonomy.get("team") == "away" else None,
            )
            scale = 8.0
        elif family in {"total_goals", "team_total_goals"}:
            expected = _first_non_null(
                payload.get("first_half_expected_goals") if taxonomy.get("period") == "first_half" else None,
                payload.get("second_half_expected_goals") if taxonomy.get("period") == "second_half" else None,
                payload.get("expected_goals")
                or payload.get("expected_total_goals"),
                payload.get("expected_total"),
                payload.get("expected_team_goals"),
            )
            scale = 1.2 if taxonomy.get("period") == "first_half" else 2.5
        elif family in {"first_to_score", "last_to_score"}:
            fit = _result_fit_from_payload(payload, side)
            if fit is not None:
                return fit
        else:
            continue
        fit = _fit_from_line(expected, line, side, scale=scale)
        if fit is not None:
            return fit
    return None


def market_similarity_score(selected_market, candidate):
    if not selected_market or not candidate:
        return 0
    selected = (selected_market or {}).get("market_taxonomy") or describe_market((selected_market or {}).get("market")).to_dict()
    replacement = (candidate or {}).get("market_taxonomy") or describe_market((candidate or {}).get("market")).to_dict()
    score = 0
    if market_family_group(selected_market) == market_family_group(candidate):
        score += 30
    if (selected.get("family") or "") == (replacement.get("family") or ""):
        score += 25
    if (selected.get("period") or "") == (replacement.get("period") or ""):
        score += 15
    if (selected.get("team") or "") == (replacement.get("team") or ""):
        score += 10
    selected_side = selected.get("selection") or selected.get("side") or ""
    replacement_side = replacement.get("selection") or replacement.get("side") or ""
    if selected_side and selected_side == replacement_side:
        score += 15
    selected_line = float_or_none(selected.get("line"))
    replacement_line = float_or_none(replacement.get("line"))
    if selected_line is not None and replacement_line is not None:
        score += max(0, 20 - min(20, abs(selected_line - replacement_line) * 8))
    return round(max(0, min(100, score)), 1)


def float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def goal_model_line_from_evidence(evidence):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    candidates = [evidence, statpal]
    for payload in candidates:
        home_xg = float_or_none(
            payload.get("home_expected_goals")
            or payload.get("home_xg")
            or payload.get("expected_home_goals")
            or payload.get("first_half_expected_home_goals")
        )
        away_xg = float_or_none(
            payload.get("away_expected_goals")
            or payload.get("away_xg")
            or payload.get("expected_away_goals")
            or payload.get("first_half_expected_away_goals")
        )
        total_xg = float_or_none(
            payload.get("expected_goals")
            or payload.get("expected_total")
            or payload.get("total_expected_goals")
        )
        if home_xg is not None and away_xg is not None:
            return f"Expected goals: home {round(home_xg, 2)}, away {round(away_xg, 2)}."
        if total_xg is not None:
            return f"Expected goals sit around {round(total_xg, 2)}."
    return ""


def _result_model_line_from_evidence(evidence):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    for payload in (evidence, statpal):
        home = float_or_none(payload.get("home_win_probability") or payload.get("home_probability"))
        draw = float_or_none(payload.get("draw_probability"))
        away = float_or_none(payload.get("away_win_probability") or payload.get("away_probability"))
        if home is not None or draw is not None or away is not None:
            parts = []
            if home is not None:
                parts.append(f"home {round(home, 1)}%")
            if draw is not None:
                parts.append(f"draw {round(draw, 1)}%")
            if away is not None:
                parts.append(f"away {round(away, 1)}%")
            return f"Result probabilities: {', '.join(parts)}."
    return ""


def _btts_model_line_from_evidence(evidence):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    for payload in (evidence, statpal):
        probability = _first_non_null(
            payload.get("btts_probability"),
            payload.get("btts_yes_probability"),
            payload.get("estimated_probability"),
        )
        if probability is not None:
            return f"BTTS probability sits around {round(probability, 1)}%."
    return ""


def count_model_line_from_evidence(evidence, *, market_payload=None):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    taxonomy = (market_payload or {}).get("market_taxonomy") or describe_market((market_payload or {}).get("market")).to_dict()
    taxonomy_side = str(taxonomy.get("selection") or taxonomy.get("side") or "").strip().title()
    for payload in (evidence, statpal):
        line = float_or_none(payload.get("line"))
        side = taxonomy_side or str(payload.get("selection") or payload.get("side") or "").strip().title()
        if not side:
            side = "Over"
        corners = float_or_none(
            payload.get("expected_total_corners")
            or payload.get("expected_corners")
        )
        if corners is not None and line is not None:
            return f"Expected {round(corners, 3)} corner events against a line of {line} for {side}."
        cards = float_or_none(
            payload.get("expected_total_cards")
            or payload.get("expected_cards")
            or payload.get("expected_booking_points")
        )
        if cards is not None and line is not None:
            unit = "booking points" if payload.get("market_family") == "booking_points" else "cards"
            return f"Expected {round(cards, 3)} {unit} against a line of {line} for {side}."
        shots = float_or_none(
            payload.get("expected_shots_on_target")
            or payload.get("expected_total_shots_on_target")
        )
        if shots is not None and line is not None:
            return f"Expected {round(shots, 3)} shots on target against a line of {line} for {side}."
    return ""


def market_owned_model_lines(market_payload, evidence):
    market_payload = market_payload or {}
    market_name = market_payload.get("market") or ""
    taxonomy = market_payload.get("market_taxonomy") or describe_market(market_name).to_dict()
    family = taxonomy.get("family") or ""
    period = taxonomy.get("period") or ""
    group = market_family_group({"market": market_name, "market_taxonomy": taxonomy})
    lines = []

    if group == "goals":
        if family in {"btts", "total_btts", "result_btts"}:
            line = _btts_model_line_from_evidence(evidence)
            if line:
                lines.append(line)
        goal_line = goal_model_line_from_evidence(evidence)
        if goal_line:
            lines.append(goal_line)
    elif group in {"corners", "cards", "shots_on_target"}:
        line = count_model_line_from_evidence(evidence, market_payload=market_payload)
        if line:
            lines.append(line)
    elif group == "result":
        line = _result_model_line_from_evidence(evidence)
        if line:
            lines.append(line)
        goal_line = goal_model_line_from_evidence(evidence)
        if goal_line:
            lines.append(goal_line)

    if period == "first_half":
        period_line = period_or_family_line({}, market_payload)
        if period_line:
            lines.append(period_line)
    return list(dict.fromkeys(lines))


def period_or_family_line(selection, market_payload=None):
    market_payload = market_payload or {}
    user_pick = (selection or {}).get("user_pick") or {}
    market = str(market_payload.get("market") or user_pick.get("market") or "")
    assessment = (selection or {}).get("assessment") or {}
    family = str(assessment.get("market_family") or "")
    technical = (selection or {}).get("technical_ref") or {}
    technical.get("statpal_snapshot_types") or []
    if "1H" in market or "First Half" in market or "first_half" in str((selection or {}).get("market_identity") or {}):
        return "This is a first-half market, so the pick depends on early match control rather than full-time strength."
    if "2H" in market or "Second Half" in market:
        return "This is a second-half market, so match state and second-half scoring profile matter most."
    if "corner" in family or "Corner" in market:
        return "This corner market should be judged from team corner volume and corner concessions, not win/loss form."
    if "card" in family or "Card" in market:
        return "This card market should be judged from fouls, cards, referee tendency and match intensity."
    if "shots_on_target" in family or "Shots On Target" in market:
        return "This shots-on-target market should be judged from attacking shot volume and defensive shot allowance."
    return ""
