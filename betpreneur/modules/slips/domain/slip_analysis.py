"""Judging a slip, as pure functions.

Everything here takes dictionaries and returns dictionaries: no ORM, no
request, no settings. That is what makes it testable in milliseconds and is
enforced by the R5 import contract.

Extracted from slips/interface/views.py, which had become the same kind of
file the refactor set out to remove — 180 module functions behind 11 view
classes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math

from betpreneur.modules.markets.api import (
    COUNT_MODEL_ENGINE,
    SCORE_MATRIX_ENGINE,
    MarketDescriptor,
    can_settle_market,
    describe_market,
    evaluator_for,
    market_matches,
    market_options,
    normalize_market_text,
)
from betpreneur.modules.pricing.api import (
    SPECIALIST_REPLACEMENT_GROUPS,
    allows_broad_replacement,
    assess_leg,
    effective_market_capability,
    float_or_none,
    line_replacement_preserves_user_thesis,
    market_edge,
    market_family_group,
    market_profile_fit_score,
    market_similarity_score,
    match_checker_alternative_reason,
    match_checker_status,
    normalise_market_name,
    replacement_scope,
    result_replacement_preserves_user_thesis,
    scored_claim,
)

SLIP_REVIEW_MARKET_OPTIONS = market_options()


def _slip_review_billable_selection_count(review):
    summary = review.summary or {}
    if summary.get("analysed_count") is not None:
        return max(0, int(summary.get("analysed_count") or 0))
    return max(0, int((review.submitted_payload or {}).get("selection_count") or review.selections.count() or 0))




def _submitted_market_payload(
    *,
    requested_market,
    market_taxonomy,
    statpal_advisory,
    market_capability,
    odds=None,
):
    market_capability = effective_market_capability(market_capability, statpal_advisory)
    advisory_score, data_confidence, advisory_status, claim_flags = scored_claim(
        (statpal_advisory or {}).get("score"), market_capability
    )
    warnings = list((statpal_advisory or {}).get("warnings") or [])
    warnings.extend((market_capability or {}).get("warnings") or [])
    return {
        "market": requested_market,
        "market_taxonomy": market_taxonomy,
        "market_capability": market_capability or {},
        "confidence": None,
        "final_confidence": None,
        "odds": float_or_none(odds),
        "advisory_score": advisory_score,
        "data_confidence": data_confidence,
        "advisory_status": advisory_status,
        "advisory_basis": (statpal_advisory or {}).get("basis") or "submitted_market_advisory",
        "advisory_warnings": list(dict.fromkeys(warnings))[:10],
        "advisory_evidence": {
            **((statpal_advisory or {}).get("evidence") or {}),
            "market_capability": market_capability or {},
            **claim_flags,
        },
        "statpal_advisory": statpal_advisory or {},
    }


def _generated_market_names_for_family(descriptor):
    family = descriptor.family
    raw_subject = descriptor.subject or descriptor.player or descriptor.raw
    if family in {"corners_total", "team_corners", "corners"}:
        period = descriptor.period or "match"
        is_period_market = period in {"first_half", "second_half"}
        period_prefix = "1H " if period == "first_half" else "2H " if period == "second_half" else ""
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Corners" if descriptor.team == "home" else "Away Team Corners"
            lines = ("0.5", "1.5", "2.5", "3.5", "4.5") if is_period_market else ("2.5", "3.5", "4.5", "5.5", "6.5")
            return [f"{period_prefix}{prefix} {side.title()} {line}" for line in lines for side in ("over", "under")]
        lines = ("1.5", "2.5", "3.5", "4.5", "5.5") if is_period_market else ("7.5", "8.5", "9.5", "10.5", "11.5")
        return [f"{period_prefix}Corners {side.title()} {line}" for line in lines for side in ("over", "under")]
    if family in {"cards_total", "team_cards", "cards"}:
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Cards" if descriptor.team == "home" else "Away Team Cards"
            return [f"{prefix} {side.title()} {line}" for line in ("1.5", "2.5", "3.5") for side in ("over", "under")]
        return [f"Cards {side.title()} {line}" for line in ("2.5", "3.5", "4.5", "5.5") for side in ("over", "under")]
    if family in {"shots_on_target_total", "team_shots_on_target"}:
        if descriptor.team in {"home", "away"}:
            prefix = "Home Team Shots On Target" if descriptor.team == "home" else "Away Team Shots On Target"
            return [f"{prefix} {side.title()} {line}" for line in ("2.5", "3.5", "4.5", "5.5") for side in ("over", "under")]
        return [f"Shots On Target {side.title()} {line}" for line in ("6.5", "7.5", "8.5", "9.5", "10.5", "11.5") for side in ("over", "under")]
    if family == "booking_points":
        return [f"Booking Points {side.title()} {line}" for line in ("35.5", "45.5", "55.5", "65.5") for side in ("over", "under")]
    if family in {"total_goals", "team_total_goals"}:
        if family == "team_total_goals" and descriptor.team in {"home", "away"}:
            prefix = "Home Team" if descriptor.team == "home" else "Away Team"
            return [f"{prefix} {side.title()} {line}" for line in ("1.5", "2.5") for side in ("over", "under")]
        return [f"{side.title()} {line}" for line in ("1.5", "2.5", "3.5", "4.5") for side in ("over", "under")]
    if family in {"result_total_goals", "double_chance_total_goals"}:
        return [
            "Home Win",
            "Draw",
            "Away Win",
            "DC: 1X",
            "DC: X2",
            "DC: 12",
            "Over 1.5",
            "Over 2.5",
            "Under 2.5",
            "Under 3.5",
        ]
    if family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
        return [
            "Home Win",
            "Draw",
            "Away Win",
            "DC: 1X",
            "DC: X2",
            "DC: 12",
            "DNB Home",
            "DNB Away",
        ]
    if family == "btts":
        return ["GG / BTTS Yes", "BTTS No"]
    if family.startswith("player_") and raw_subject:
        subject = str(raw_subject)
        for suffix in (" to score", " player to score", " shots", " shot on target", " shots on target", " to be booked", " assist", " saves"):
            normalized = normalize_market_text(subject)
            if normalized.endswith(normalize_market_text(suffix)):
                subject = subject[: -len(suffix)].strip()
                break
        if subject:
            return [
                f"{subject} To Score",
                f"{subject} Shots Over 1.5",
                f"{subject} Shots On Target Over 1.5",
                f"{subject} To Be Booked",
                f"{subject} Assist",
                f"{subject} Saves Over 2.5",
            ]
    return []


FIXTURE_WIDE_RECOMMENDATION_MARKETS = (
    # Result shape
    "Home Win",
    "Draw",
    "Away Win",
    "DC: 1X",
    "DC: X2",
    "DC: 12",
    "DNB Home",
    "DNB Away",
    # Full-match goals and BTTS
    "Over 1.5",
    "Over 2.5",
    "Over 3.5",
    "Under 1.5",
    "Under 2.5",
    "Under 3.5",
    "Under 4.5",
    "GG / BTTS Yes",
    "BTTS No",
    # Team goals; 0.5 lines stay out of recommendations.
    "Home Team Over 1.5",
    "Away Team Over 1.5",
    "Home Team Under 2.5",
    "Away Team Under 2.5",
    # First-half goals.
    "1H Over 1.5",
    "1H Under 1.5",
    "1H Under 2.5",
    # Count markets; these only survive if the count model can score them.
    "Corners Over 7.5",
    "Corners Over 8.5",
    "Corners Over 9.5",
    "Corners Under 9.5",
    "Corners Under 10.5",
    "Corners Under 11.5",
    "1H Corners Over 2.5",
    "1H Corners Over 3.5",
    "1H Corners Under 4.5",
    "Cards Over 3.5",
    "Cards Over 4.5",
    "Cards Under 5.5",
    "Booking Points Over 35.5",
    "Booking Points Under 55.5",
    "Shots On Target Over 7.5",
    "Shots On Target Under 10.5",
)


COUNT_RECOMMENDATION_FAMILIES = {
    "corners_total",
    "team_corners",
    "cards_total",
    "team_cards",
    "booking_points",
    "shots_on_target_total",
    "team_shots_on_target",
}


def _fixture_supports_count_candidate(descriptor, *, game=None, statpal_context=None):
    if descriptor.family not in COUNT_RECOMMENDATION_FAMILIES:
        return True
    game = game or {}
    if game.get("statpal_home_team_id") and game.get("statpal_away_team_id"):
        return True
    summary = ((((statpal_context or {}).get("snapshots") or {}).get("detailed_stats") or {}).get("summary") or {})
    if descriptor.family in {"corners_total", "team_corners"}:
        return summary.get("home_corners") is not None and summary.get("away_corners") is not None
    if descriptor.family in {"cards_total", "team_cards", "booking_points"}:
        return any(
            summary.get(key) is not None
            for key in ("home_yellow_cards", "away_yellow_cards", "home_red_cards", "away_red_cards", "total_cards", "booking_points")
        )
    if descriptor.family in {"shots_on_target_total", "team_shots_on_target"}:
        return summary.get("home_shots_on_target") is not None and summary.get("away_shots_on_target") is not None
    return False


def _fixture_wide_market_candidates(selected_descriptor, *, game=None, statpal_context=None):
    seen = set()
    candidates = []
    selected_market = {
        "market": selected_descriptor.canonical or selected_descriptor.raw,
        "market_taxonomy": selected_descriptor.to_dict(),
    }
    include_fixture_wide_pool = market_family_group(selected_market) not in SPECIALIST_REPLACEMENT_GROUPS
    candidate_groups = [("statpal_market_family", _generated_market_names_for_family(selected_descriptor))]
    if include_fixture_wide_pool:
        candidate_groups.append(("fixture_wide_market_pool", FIXTURE_WIDE_RECOMMENDATION_MARKETS))

    for source, names in candidate_groups:
        for market_name in names:
            descriptor = describe_market(market_name)
            if not descriptor.recognized:
                continue
            if source == "fixture_wide_market_pool" and not _fixture_supports_count_candidate(
                descriptor,
                game=game,
                statpal_context=statpal_context,
            ):
                continue
            key = normalize_market_text(descriptor.canonical or market_name)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((descriptor, source))
    return candidates


def _bookmaker_recommendation_market_available(market):
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    family = taxonomy.get("family") or ""
    side = str(taxonomy.get("selection") or taxonomy.get("side") or "").lower()
    line = float_or_none(taxonomy.get("line"))
    period = taxonomy.get("period") or "match"
    team = taxonomy.get("team") or ""
    if family == "corners_total" and period == "match" and side == "over" and line is not None and line < 7.5:
        return False
    return not (family == "corners_total" and line is not None and line < 7.5 and not team)


def _market_specificity_score(selected_market, candidate):
    if not selected_market or not candidate:
        return 50
    selected = (selected_market or {}).get("market_taxonomy") or describe_market((selected_market or {}).get("market")).to_dict()
    replacement = (candidate or {}).get("market_taxonomy") or describe_market((candidate or {}).get("market")).to_dict()
    if market_family_group(selected_market) != market_family_group(candidate):
        return 50
    selected_line = float_or_none(selected.get("line"))
    replacement_line = float_or_none(replacement.get("line"))
    selected_side = selected.get("selection") or selected.get("side") or ""
    replacement_side = replacement.get("selection") or replacement.get("side") or ""
    if selected_line is None or replacement_line is None or selected_side != replacement_side:
        return 60
    return round(max(0, min(100, 100 - abs(selected_line - replacement_line) * 18)), 1)


MINIMUM_EV_LIFT = 0.03


SAME_FAMILY_CLOSE_RANKING_MARGIN = 8.0


def _is_early_payout_market(market):
    """SportyBet 1UP/2UP: paid out as soon as the side goes ahead."""
    taxonomy = (market or {}).get("market_taxonomy")
    if not taxonomy:
        taxonomy = describe_market((market or {}).get("market")).to_dict()
    return bool(taxonomy.get("early_payout"))


def _market_expected_value(market):
    """
    Return per unit staked: `p x odds - 1`.

    This is the objective ADR-004 asked for and could not have until alternatives were
    priced. It settles the cross-family comparison on its own: an Under 4.5 at 88% into
    1.10 returns -0.032, while a 43% away win at 2.60 returns +0.118. Probability alone
    always preferred the first.
    """
    odds = float_or_none((market or {}).get("odds"))
    if not odds or odds <= 1:
        return None
    probability = float_or_none((market or {}).get("advisory_score"))
    if probability is None:
        probability = float_or_none((market or {}).get("display_score"))
    if probability is None:
        return None
    return round((probability / 100.0) * odds - 1, 4)


def _rank_replacement_candidates(candidates, *, selected_market=None):
    """
    Rank by edge over the league-average fixture, not by raw probability.

    Markets with no reference (counts, player props) keep their old ordering by falling
    to the bottom of the edge key rather than being treated as zero-edge, which would let
    an unmeasured market outrank a measured negative one.
    """
    def key(market):
        ev = float_or_none(market.get("ev"))
        if ev is None:
            ev = _market_expected_value(market)
        edge = market_edge(market)
        fit = market_profile_fit_score(market)
        similarity = market_similarity_score(selected_market, market) if selected_market else None
        if selected_market:
            return (
                # First: preserve the user's thesis, then rank markets that fit the
                # fixture. Otherwise every replacement drifts to the broadest safe line.
                similarity is not None,
                similarity if similarity is not None else 0,
                fit is not None,
                fit if fit is not None else 0,
                # Then value/edge/probability.
                ev is not None,
                ev if ev is not None else 0,
                edge is not None,
                edge if edge is not None else 0,
                market.get("advisory_score") or 0,
                market.get("final_confidence") or market.get("confidence") or 0,
            )
        return (
            # Value first, where the alternative carries a price.
            ev is not None,
            ev if ev is not None else 0,
            # Otherwise how far above a league-average fixture it sits.
            edge is not None,
            edge if edge is not None else 0,
            market.get("advisory_score") or 0,
            market.get("final_confidence") or market.get("confidence") or 0,
        )

    return sorted(candidates, key=key, reverse=True)


def enrich_market_with_team_intelligence(market, team_intelligence):
    """Attach stored team/league profile fit to a candidate market."""
    if not market or not isinstance(team_intelligence, dict):
        return market
    if not team_intelligence.get("available") and not _team_intelligence_has_league_priors(team_intelligence):
        return market
    profile_fit = _team_intelligence_market_fit(market, team_intelligence)
    if not profile_fit:
        return market
    payload = dict(market)
    evidence = dict(payload.get("advisory_evidence") or {})
    evidence.setdefault("team_intelligence_fit_score", profile_fit["score"])
    evidence.setdefault("team_intelligence_source", profile_fit["source"])
    evidence.setdefault("team_intelligence_profile", profile_fit["profile"])
    payload["advisory_evidence"] = evidence
    payload["team_intelligence_fit_score"] = profile_fit["score"]
    return payload


def analysis_data_fallback_state(team_intelligence=None, statpal_context=None):
    """Describe the data layer slip review should trust for this fixture."""
    team_intelligence = team_intelligence or {}
    statpal_context = statpal_context or {}
    warnings = []

    intelligence_status = team_intelligence.get("status") or "missing"
    if _team_intelligence_is_fresh_enough(team_intelligence):
        primary = "team_intelligence"
    else:
        if intelligence_status == "stale" or _team_intelligence_has_stale_coverage(team_intelligence):
            warnings.append("team_intelligence_stale")
        elif intelligence_status == "missing" or not team_intelligence.get("available"):
            warnings.append("team_intelligence_missing")
        elif team_intelligence.get("missing"):
            warnings.append("team_intelligence_partial")

        if _provider_snapshots_available(statpal_context):
            primary = "provider_snapshots"
        elif _team_intelligence_has_league_priors(team_intelligence):
            primary = "league_priors"
            warnings.append("provider_snapshots_missing")
        else:
            primary = "unavailable"
            warnings.append("provider_snapshots_missing")
            warnings.append("league_priors_missing")

    return {
        "primary": primary,
        "source_order": ["team_intelligence", "provider_snapshots", "league_priors"],
        "team_intelligence_status": intelligence_status,
        "provider_snapshots_available": _provider_snapshots_available(statpal_context),
        "league_priors_available": _team_intelligence_has_league_priors(team_intelligence),
        "warnings": warnings,
    }


def _team_intelligence_is_fresh_enough(team_intelligence):
    if not isinstance(team_intelligence, dict) or not team_intelligence.get("available"):
        return False
    if team_intelligence.get("status") not in {"available", "partial"}:
        return False
    if _team_intelligence_has_stale_coverage(team_intelligence):
        return False
    missing = set(team_intelligence.get("missing") or [])
    return not {"home_team_profile", "away_team_profile"} <= missing


def _team_intelligence_has_stale_coverage(team_intelligence):
    for key in ("home", "away", "league"):
        payload = (team_intelligence or {}).get(key) or {}
        coverage = payload.get("coverage") or {}
        if coverage.get("status") in {"stale", "failed"}:
            return True
    return False


def _team_intelligence_has_league_priors(team_intelligence):
    league = (team_intelligence or {}).get("league") or {}
    coverage = league.get("coverage") or {}
    if coverage.get("status") in {"stale", "failed"}:
        return False
    return bool(league.get("market_profiles"))


def _provider_snapshots_available(statpal_context):
    snapshots = (statpal_context or {}).get("snapshots") or {}
    for snapshot in snapshots.values():
        if isinstance(snapshot, dict) and any(snapshot.get(key) for key in ("payload", "summary", "data", "items")):
            return True
        if snapshot:
            return True
    return False


def _team_intelligence_market_fit(market, team_intelligence):
    taxonomy = (market or {}).get("market_taxonomy") or describe_market((market or {}).get("market")).to_dict()
    family = taxonomy.get("family") or ""
    market_name = (market or {}).get("market") or taxonomy.get("canonical") or ""
    candidates = []
    for label, profile in _team_intelligence_profiles_for_market(taxonomy, team_intelligence):
        for item in profile.get("market_profiles") or []:
            if item.get("market_family") != family:
                continue
            exact = market_matches(market_name, item.get("market"))
            if not exact and item.get("market") != market_name:
                continue
            score = _profile_fit_from_market_profile(item, exact=exact)
            if score is not None:
                candidates.append(
                    {
                        "score": score,
                        "source": f"stored_{label}_team_market_profile",
                        "profile": _compact_team_intelligence_profile(item),
                    }
                )
    league = (team_intelligence or {}).get("league") or {}
    for item in league.get("market_profiles") or []:
        if item.get("market_family") != family:
            continue
        exact = market_matches(market_name, item.get("market"))
        if not exact and item.get("market") != market_name:
            continue
        score = _profile_fit_from_market_profile(item, exact=exact, league=True)
        if score is not None:
            candidates.append(
                {
                    "score": score,
                    "source": "stored_league_market_profile",
                    "profile": _compact_team_intelligence_profile(item),
                }
            )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["score"])


def _team_intelligence_profiles_for_market(taxonomy, team_intelligence):
    family = taxonomy.get("family") or ""
    side = taxonomy.get("side") or taxonomy.get("selection") or ""
    team = taxonomy.get("team") or ""
    if team == "home" or side == "home":
        return [("home", (team_intelligence or {}).get("home") or {})]
    if team == "away" or side == "away":
        return [("away", (team_intelligence or {}).get("away") or {})]
    if family in {"team_total_goals", "team_corners", "team_cards", "team_shots_on_target"}:
        return []
    return [
        ("home", (team_intelligence or {}).get("home") or {}),
        ("away", (team_intelligence or {}).get("away") or {}),
    ]


def _profile_fit_from_market_profile(profile, *, exact: bool, league: bool = False):
    hit_rate = float_or_none(profile.get("hit_rate"))
    confidence = float_or_none(profile.get("confidence"))
    attempts = float_or_none(profile.get("attempts")) or 0
    if hit_rate is None and confidence is None:
        return None
    sample_weight = min(1.0, attempts / (20 if league else 12))
    base = hit_rate if hit_rate is not None else confidence
    if confidence is not None and hit_rate is not None:
        base = (hit_rate * 0.65) + (confidence * 0.35)
    score = (base * (0.75 + sample_weight * 0.25)) if base is not None else None
    if score is None:
        return None
    if not exact:
        score -= 8
    if league:
        score -= 5
    return round(max(0, min(100, score)), 1)


def _compact_team_intelligence_profile(profile):
    return {
        "market_family": profile.get("market_family"),
        "market": profile.get("market"),
        "scope": profile.get("scope"),
        "attempts": profile.get("attempts"),
        "wins": profile.get("wins"),
        "losses": profile.get("losses"),
        "voids": profile.get("voids"),
        "hit_rate": profile.get("hit_rate"),
        "confidence": profile.get("confidence"),
        "data_quality": profile.get("data_quality"),
        "source": "team_intelligence_store",
    }


def _replacement_ranking_score(market, *, selected_market=None):
    probability = float_or_none(market.get("advisory_score"))
    if probability is None:
        probability = float_or_none(market.get("display_score")) or 0
    fit = market_profile_fit_score(market)
    edge = market_edge(market)
    ev = float_or_none(market.get("ev"))
    if ev is None:
        ev = _market_expected_value(market)
    similarity = market_similarity_score(selected_market, market) if selected_market else 0
    specificity = _market_specificity_score(selected_market, market) if selected_market else 50
    score = probability * 0.30
    score += (fit if fit is not None else 50) * 0.38
    score += similarity * 0.14
    score += specificity * 0.45
    if edge is not None:
        score += max(-15, min(15, edge)) * 0.60
    if ev is not None:
        score += max(-0.25, min(0.25, ev)) * 40
    return round(score, 3)


def _select_ranked_replacement(allowed, *, selected_market):
    ranked = sorted(
        allowed,
        key=lambda market: (
            _replacement_ranking_score(market, selected_market=selected_market),
            float_or_none(market.get("advisory_score")) or 0,
        ),
        reverse=True,
    )
    same_family = [market for market in ranked if market.get("replacement_scope") == "comparable_market"]
    cross_family = [market for market in ranked if market.get("replacement_scope") == "broad_fallback"]
    if same_family and cross_family:
        best_same = same_family[0]
        best_cross = cross_family[0]
        same_score = _replacement_ranking_score(best_same, selected_market=selected_market)
        cross_score = _replacement_ranking_score(best_cross, selected_market=selected_market)
        if cross_score < same_score + SAME_FAMILY_CLOSE_RANKING_MARGIN:
            return best_same
        return best_cross
    return ranked[0]


def _blocked_slip_recommendation_market(market):
    market_name = (market or {}).get("market") if isinstance(market, dict) else market
    descriptor = describe_market(market_name)
    if not descriptor.recognized:
        return False
    if descriptor.family in {"asian_handicap", "handicap"}:
        return True
    line = float_or_none(descriptor.line)
    return descriptor.side == "over" and line is not None and abs(line - 0.5) < 0.001


MINIMUM_REPLACEMENT_SCORE = 55


def _market_data_quality(market):
    capability = (market or {}).get("market_capability") or {}
    evidence = (market or {}).get("advisory_evidence") or {}
    evidence_capability = evidence.get("market_capability") if isinstance(evidence.get("market_capability"), dict) else {}
    return str(capability.get("data_quality") or evidence_capability.get("data_quality") or "").lower()


def _market_specific_evidence_exists(market):
    evidence = (market or {}).get("advisory_evidence") or {}
    if not isinstance(evidence, dict):
        return False
    ignored_keys = {
        "market_capability",
        "claim_limited_by_data_quality",
        "data_confidence",
        "market_consensus_percent",
        "bookmaker_count",
        "historical_accuracy",
        "historical_sample",
        "sample_size",
        "similar_market_roi",
        "market_roi",
        "roi",
        "roi_flat",
        "league_market_sample",
        "global_prior",
        "statpal_merge_mode",
        "statpal_adjustment",
        "statpal_basis",
    }
    for key, value in evidence.items():
        if key in ignored_keys or value in (None, "", [], {}):
            continue
        if key == "statpal" and isinstance(value, dict):
            if _market_specific_evidence_exists({"advisory_evidence": value}):
                return True
            continue
        return True
    return False


def _market_model_sanity_passes(market):
    warnings = set((market or {}).get("advisory_warnings") or [])
    evidence = (market or {}).get("advisory_evidence") or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    failed_flags = {
        "model_sanity_check_failed",
        "fixture_model_sanity_failed",
        "extreme_model_market_disagreement",
        "result_model_market_disagreement",
    }
    if warnings.intersection(failed_flags):
        return False
    return not any(bool((evidence or {}).get(flag)) or bool((statpal or {}).get(flag)) for flag in failed_flags)


def _replacement_candidate_is_eligible(market):
    if not market or _blocked_slip_recommendation_market(market):
        return False
    if not _bookmaker_recommendation_market_available(market):
        return False
    probability = float_or_none(market.get("advisory_score"))
    if probability is None:
        probability = float_or_none(market.get("display_score"))
    if probability is None or probability < MINIMUM_REPLACEMENT_SCORE:
        return False
    if _market_data_quality(market) in {"poor", "unsupported"}:
        return False
    if not _market_specific_evidence_exists(market):
        return False
    if not _market_model_sanity_passes(market):
        return False
    return True


def _replacement_is_meaningfully_better(selected_market, replacement_market):
    if not replacement_market or not selected_market:
        return bool(replacement_market)
    if not _replacement_candidate_is_eligible(replacement_market):
        return False
    if market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False
    if not result_replacement_preserves_user_thesis(selected_market, replacement_market):
        return False
    if not line_replacement_preserves_user_thesis(selected_market, replacement_market):
        return False

    if _is_early_payout_market(selected_market):
        # The modelled number is the probability of the *underlying* result. An early
        # payout also wins in games the side led and then failed to see out, so the true
        # chance is higher by an unknown margin -- and the price already reflects that.
        # Comparing a replacement against a floor cannot show it is better, so this pick
        # is left alone rather than swapped on a number we know understates it.
        return False

    selected_score = float_or_none(selected_market.get("advisory_score")) or float(selected_market.get("display_score") or 0)
    replacement_score = float_or_none(replacement_market.get("advisory_score")) or float(replacement_market.get("display_score") or 0)
    scope = replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market)
    minimum_score = 58 if scope == "comparable_market" else 60
    minimum_lift = 4 if scope == "comparable_market" else 6

    # The absolute floor stays: a market we would not stand behind on its own is never a
    # replacement, however well it compares.
    if replacement_score < minimum_score:
        return False

    selected_ev = float_or_none(selected_market.get("ev"))
    if selected_ev is None:
        selected_ev = _market_expected_value(selected_market)
    replacement_ev = float_or_none(replacement_market.get("ev"))
    if replacement_ev is None:
        replacement_ev = _market_expected_value(replacement_market)
    if selected_ev is not None and replacement_ev is not None:
        # Both priced: compare what the bettor actually receives. A swap that raises the
        # probability while collapsing the price is not an improvement, which is what
        # turned a 20.05 ticket into a 3.24 one.
        return replacement_ev >= selected_ev + MINIMUM_EV_LIFT

    selected_edge = market_edge(selected_market)
    replacement_edge = market_edge(replacement_market)
    if selected_edge is not None and replacement_edge is not None:
        # Unpriced, but both measured against the same league: compare like with like.
        return replacement_edge >= selected_edge + minimum_lift

    # No shared reference (counts, player props): fall back to the raw comparison.
    return replacement_score >= selected_score + minimum_lift


def _replacement_is_supported_fit(selected_market, replacement_market):
    if not selected_market or not replacement_market:
        return False
    if not _replacement_candidate_is_eligible(replacement_market):
        return False
    if market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False
    if not _market_was_assessed(replacement_market):
        return False
    replacement_score = float_or_none(replacement_market.get("advisory_score"))
    if replacement_score is None or replacement_score < SMART_RANDOMIZE_MIN_CONFIDENCE:
        return False
    if not result_replacement_preserves_user_thesis(selected_market, replacement_market):
        return False
    if not line_replacement_preserves_user_thesis(selected_market, replacement_market):
        return False
    scope = replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market)
    if scope == "broad_fallback" and not allows_broad_replacement(selected_market):
        return False
    selected_ev = float_or_none(selected_market.get("ev"))
    if selected_ev is None:
        selected_ev = _market_expected_value(selected_market)
    replacement_ev = float_or_none(replacement_market.get("ev"))
    if replacement_ev is None:
        replacement_ev = _market_expected_value(replacement_market)
    if selected_ev is not None and replacement_ev is not None and replacement_ev < selected_ev:
        return False
    fit = market_profile_fit_score(replacement_market)
    if fit is not None and fit < 50:
        return False
    return True




def _market_is_better_for_slip(selected_market, replacement_market):
    if not replacement_market:
        return False
    if market_matches(selected_market.get("market"), replacement_market.get("market")):
        return False
    scope = replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market)
    if scope == "broad_fallback" and not allows_broad_replacement(selected_market):
        return False
    return _replacement_is_meaningfully_better(selected_market, replacement_market)


def _alternative_is_allowed_for_slip(selected_market, replacement_market):
    if not replacement_market or not _market_was_assessed(replacement_market):
        return False
    scope = replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market)
    if scope != "broad_fallback":
        return True
    return allows_broad_replacement(selected_market)


def _reverse_oriented_market(market):
    normalized = normalise_market_name(market)
    reversed_markets = {
        "home win": "Away Win",
        "away win": "Home Win",
        "dnb home": "DNB Away",
        "dnb away": "DNB Home",
        "ah home +0.5": "AH Away +0.5",
        "ah away +0.5": "AH Home +0.5",
        "home cs": "Away CS",
        "away cs": "Home CS",
        "first to score h": "First to Score A",
        "first to score a": "First to Score H",
    }
    return reversed_markets.get(normalized, market)


def _market_for_fixture_orientation(market, candidate):
    if (candidate or {}).get("match_orientation") == "reversed":
        return _reverse_oriented_market(market)
    return market


def _minimal_game_from_candidate(candidate):
    fixture_name = candidate.get("fixture") or " vs ".join(
        item for item in [candidate.get("home_team"), candidate.get("away_team")] if item
    )
    return {
        "fixture": fixture_name,
        "home_team": candidate.get("home_team", ""),
        "away_team": candidate.get("away_team", ""),
        "home_logo": candidate.get("home_logo", ""),
        "away_logo": candidate.get("away_logo", ""),
        "league": candidate.get("league", ""),
        "league_logo": candidate.get("league_logo", ""),
        "country": candidate.get("country", ""),
        "country_flag": candidate.get("country_flag", ""),
        "round": candidate.get("round", ""),
        "kickoff": candidate.get("kickoff", ""),
        "match_id": str(candidate.get("match_id") or ""),
        "match_date": candidate.get("match_date"),
        "statpal_home_team_id": candidate.get("statpal_home_team_id") or "",
        "statpal_away_team_id": candidate.get("statpal_away_team_id") or "",
        "code": candidate.get("code") or candidate.get("league_id") or "",
        "league_id": candidate.get("league_id") or candidate.get("code") or "",
        "hname": candidate.get("hname") or candidate.get("home_team", ""),
        "aname": candidate.get("aname") or candidate.get("away_team", ""),
        "hid": candidate.get("hid") or "",
        "aid": candidate.get("aid") or "",
        "markets": [],
        "market_count": 0,
        "recommendation_status": "no_edge",
        "fixture_context": candidate.get("fixture_context") or {},
        "team_news": candidate.get("team_news") or {},
        "corner_profile": candidate.get("corner_profile") or {},
        "insights": candidate.get("insights") or {},
        "provider_merge": candidate.get("provider_merge") or {},
    }


def _matched_fixture_with_statpal(candidate, game=None, statpal_candidate=None, *, provider_match_id="", provider_competition_id="", home_team_id="", away_team_id=""):
    candidate = candidate or {}
    game = game or {}
    statpal_candidate = statpal_candidate or {}
    return {
        **candidate,
        "match_id": game.get("match_id") or candidate.get("match_id"),
        "fixture": game.get("fixture") or candidate.get("fixture"),
        "home_team": game.get("home_team") or candidate.get("home_team"),
        "away_team": game.get("away_team") or candidate.get("away_team"),
        "league": game.get("league") or candidate.get("league"),
        "country": game.get("country") or candidate.get("country"),
        "kickoff": game.get("kickoff") or candidate.get("kickoff"),
        "statpal_match_id": statpal_candidate.get("match_id") or "",
        "statpal_provider_match_id": provider_match_id or statpal_candidate.get("provider_match_id") or "",
        "statpal_provider_competition_id": provider_competition_id or statpal_candidate.get("provider_competition_id") or "",
        "statpal_home_team_id": home_team_id or statpal_candidate.get("home_team_id") or "",
        "statpal_away_team_id": away_team_id or statpal_candidate.get("away_team_id") or "",
        "statpal_home_team": statpal_candidate.get("home_team") or "",
        "statpal_away_team": statpal_candidate.get("away_team") or "",
        "provider_merge": game.get("provider_merge") or candidate.get("provider_merge") or {},
    }


def _resolved_taxonomy(selection):
    """
    The descriptor the importer already resolved from the bookmaker's market ids.

    Bookmaker imports nest the analysed item under `provider_payload`; the manual path
    puts it at the top level.
    """
    for candidate in (
        selection.get("market_taxonomy"),
        (selection.get("provider_payload") or {}).get("market_taxonomy"),
    ):
        if isinstance(candidate, dict) and candidate.get("family") and candidate.get("recognized"):
            return candidate
    return {}


def _resolved_canonical_market(selection):
    """
    The market identity the importer resolved, carried into the analysis result.

    Without this the public payload reports `resolution: "unresolved"` on every leg,
    because the result is a fresh dict that never inherits what the importer worked out.
    """
    for candidate in (
        selection.get("canonical_market"),
        (selection.get("provider_payload") or {}).get("canonical_market"),
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _descriptor_from_taxonomy(taxonomy):
    """Rebuild a MarketDescriptor from its stored form, tolerating JSON round-tripping."""
    fields = {field.name for field in dataclasses.fields(MarketDescriptor)}
    payload = {key: value for key, value in taxonomy.items() if key in fields}
    payload["data_requirements"] = tuple(payload.get("data_requirements") or ())
    for key in ("raw", "canonical", "code", "family", "category"):
        payload.setdefault(key, "")
    return MarketDescriptor(**payload)


def _market_can_skip_core_on_demand(descriptor):
    if descriptor.family in {"team_shots_on_target"}:
        return True
    spec = evaluator_for(descriptor.family)
    if not spec:
        return False
    return spec.engine in {SCORE_MATRIX_ENGINE, COUNT_MODEL_ENGINE} or descriptor.family.startswith("player_")


def _has_statpal_hydration_identity(candidate=None, statpal_candidate=None, provider_metadata=None):
    candidate = candidate or {}
    statpal_candidate = statpal_candidate or {}
    provider_metadata = provider_metadata or {}
    if str(provider_metadata.get("provider") or "").lower() == "statpal":
        if provider_metadata.get("provider_event_id"):
            return True
    if isinstance(statpal_candidate, dict) and (
        statpal_candidate.get("provider_match_id")
        or statpal_candidate.get("statpal_provider_match_id")
        or str(statpal_candidate.get("match_id") or "").startswith("statpal:")
        or statpal_candidate.get("home_team_id")
        or statpal_candidate.get("away_team_id")
        or statpal_candidate.get("statpal_home_team_id")
        or statpal_candidate.get("statpal_away_team_id")
    ):
        return True
    if isinstance(candidate, dict) and (
        candidate.get("provider_match_id")
        or candidate.get("statpal_provider_match_id")
        or str(candidate.get("match_id") or "").startswith("statpal:")
        or candidate.get("statpal_home_team_id")
        or candidate.get("statpal_away_team_id")
    ):
        return True
    return False


def _should_skip_core_on_demand(descriptor, *, game=None, candidate=None, statpal_candidate=None, provider_metadata=None):
    if not _market_can_skip_core_on_demand(descriptor):
        return False
    if game:
        return True
    return _has_statpal_hydration_identity(candidate, statpal_candidate, provider_metadata)


def _consume_review_force_fresh(review_scoring_context):
    if review_scoring_context is None:
        return True
    if review_scoring_context.get("fixture_universe_synced"):
        return False
    review_scoring_context["fixture_universe_synced"] = True
    return True


_ASSESSMENT_SCORE_KEYS = ("advisory_score", "display_score", "final_confidence", "confidence")


def _market_was_assessed(selected_market) -> bool:
    """
    Whether we actually produced a judgement about this market.

    Matters because the verdict branches below default to `no_edge -> remove`, so a
    market nobody evaluated would come out as "avoid" — a judgement we never made.
    """
    market = selected_market or {}
    return any(float_or_none(market.get(key)) is not None for key in _ASSESSMENT_SCORE_KEYS)


def _market_model_conflicted(market) -> bool:
    warnings = set((market or {}).get("advisory_warnings") or [])
    evidence = (market or {}).get("advisory_evidence") or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    return (
        "result_model_market_disagreement" in warnings
        or bool(evidence.get("result_model_market_disagreement"))
        or bool(statpal.get("result_model_market_disagreement"))
    )


def _manual_verdict(selected_market, replacement_market):
    status_value = selected_market.get("recommendation_status") or "no_edge"
    has_better_market = _market_is_better_for_slip(selected_market, replacement_market)
    has_stat_backed_alternative = _alternative_is_allowed_for_slip(selected_market, replacement_market)
    advisory_score = float_or_none(selected_market.get("advisory_score")) or 0
    advisory_status = selected_market.get("advisory_status") or match_checker_status(advisory_score)

    if not _market_was_assessed(selected_market):
        # Absence of evidence is not evidence of a bad pick. Saying "avoid" here is the
        # same failure as scoring an un-analysed leg zero: it reads as a judgement the
        # user could disagree with, when in fact we simply did not evaluate it.
        return {
            "verdict": "not_assessed",
            "message": "We could not assess this selection, so it has not been judged either way.",
            "better_market_available": False,
            "advisory_score": None,
            "advisory_status": "unknown",
        }

    if (selected_market.get("recommended") or selected_market.get("selected")) and not has_better_market:
        verdict = "keep"
        message = "This selection is strong enough to keep."
    elif advisory_status in {"strong", "playable"}:
        verdict = "replace" if has_better_market else ("keep" if advisory_status == "strong" else "caution")
        message = (
            "A stronger market fits this match better."
            if has_better_market
            else "This selection has enough match-specific support, even if it is not a headline pick."
        )
    elif advisory_status == "caution":
        verdict = "replace" if has_better_market else "caution"
        message = (
            "This selection is fragile; the alternative market has better match-specific support."
            if has_better_market
            else "This selection has some support, but the match and league signals require caution."
        )
    elif advisory_status == "avoid":
        # An assessed leg the model judges weak is a "remove", with or without a
        # replacement. Without this branch it fell through to `no_edge`, which
        # downgraded it to "caution" whenever no alternative was found -- softening
        # the verdict precisely where the evidence was clearest. Failing to find a
        # better market is a statement about the alternatives, not about this pick.
        verdict = "replace" if has_better_market else "remove"
        message = (
            "This selection is weak on the match evidence; the alternative market has better support."
            if has_better_market
            else "This selection is weak on the match evidence, and no stronger replacement was found for this game."
        )
    elif status_value == "watchlist":
        verdict = "replace" if has_better_market else "caution"
        message = (
            "A stronger market fits this match better."
            if has_better_market
            else "This selection is playable for tracking, but it is not strong enough as a recommended pick."
        )
    elif status_value == "no_edge":
        verdict = "replace" if replacement_market else "caution"
        message = (
            "The selected market does not show enough edge; consider the stronger match-specific alternative."
            if has_better_market
            else "The selected market is high risk; use the statistically backed alternative instead."
            if has_stat_backed_alternative
            else "This selection is high risk, but no stronger backed replacement was found for this game."
        )
    else:
        verdict = "caution"
        message = "This selection has some support, but there are enough warnings to treat it carefully."

    return {
        "verdict": verdict,
        "message": message,
        "better_market_available": has_better_market,
        "advisory_score": advisory_score,
        "advisory_status": advisory_status,
    }


def _empty_api_usage():
    return {
        "provider": "statpal",
        "attempted_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "skipped_by_cache": 0,
        "skipped_without_call": 0,
        "snapshot_types_attempted": [],
        "snapshot_types_refreshed": [],
        "snapshot_types_failed": [],
    }


def _merge_api_usage(*usages):
    total = _empty_api_usage()
    for usage in usages:
        usage = usage or {}
        total["attempted_calls"] += int(usage.get("attempted_calls") or 0)
        total["successful_calls"] += int(usage.get("successful_calls") or 0)
        total["failed_calls"] += int(usage.get("failed_calls") or 0)
        total["skipped_by_cache"] += int(usage.get("skipped_by_cache") or 0)
        total["skipped_without_call"] += int(usage.get("skipped_without_call") or 0)
        for key in ("snapshot_types_attempted", "snapshot_types_refreshed", "snapshot_types_failed"):
            total[key].extend(str(value) for value in usage.get(key) or [] if value)
    for key in ("snapshot_types_attempted", "snapshot_types_refreshed", "snapshot_types_failed"):
        total[key] = list(dict.fromkeys(total[key]))
    return total


def _selection_api_usage(item):
    refresh = item.get("statpal_refresh") or {}
    return refresh.get("api_usage") or _empty_api_usage()


def _slip_api_usage(items):
    usage = _merge_api_usage(*(_selection_api_usage(item) for item in items))
    usage["call_budget_note"] = (
        "Counts only StatPal snapshot refresh calls made during this review. "
        "Cache hits and existing mapped fixtures do not spend StatPal calls."
    )
    return usage


def _round_percent(value):
    parsed = float_or_none(value)
    return round(parsed * 100, 1) if parsed is not None else None


def _fair_odds(probability):
    parsed = float_or_none(probability)
    if parsed is None or parsed <= 0:
        return None
    return round(1 / parsed, 2)


def _success_percent_display(value):
    parsed = float_or_none(value)
    if parsed is None:
        return None
    if parsed == 0:
        return "0%"
    if 0 < parsed < 0.01:
        return "<0.01%"
    return f"{round(parsed, 2)}%"


def _implied_probability_from_odds(odds):
    parsed = float_or_none(odds)
    if parsed is None or parsed <= 1:
        return None
    return 1 / parsed


def _probability_gap(model_probability, market_probability):
    if model_probability is None or market_probability is None:
        return None
    return round((model_probability - market_probability) * 100, 1)


def _gap_level(gap_points):
    gap = abs(float_or_none(gap_points) or 0)
    if gap >= 15:
        return "high"
    if gap >= 8:
        return "medium"
    return "low"


def _value_rating(model_probability, offered_odds):
    market_probability = _implied_probability_from_odds(offered_odds)
    gap = _probability_gap(model_probability, market_probability)
    if gap is None:
        return "unknown"
    if gap >= 5:
        return "positive_value"
    if gap <= -5:
        return "poor_value"
    return "near_fair"


def _selection_original_odds(item):
    provider_payload = item.get("provider_payload") or {}
    odds = provider_payload.get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("selection") or {}).get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("leg") or {}).get("odds")
    if odds is None:
        odds = (item.get("selected_market") or {}).get("odds")
    return float_or_none(odds)


def _selection_suggested_odds(item):
    if item.get("verdict") == "replace":
        return float_or_none((item.get("replacement_market") or {}).get("odds"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return _selection_original_odds(item) or float_or_none((item.get("selected_market") or {}).get("odds"))


def _combined_odds(values):
    odds = [value for value in values if value and value > 1]
    if not odds:
        return None
    total = 1.0
    for value in odds:
        total *= value
    return round(total, 2)


def _ticket_health_summary(score, risk_level, remove_count, replace_count, caution_count, unverified_count):
    weak_count = remove_count + replace_count
    if risk_level == "unknown":
        return (
            f"None of these {unverified_count} {_plural(unverified_count, 'pick')} could be analysed yet, "
            "so this ticket has not been assessed."
        )
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {_plural(replace_count, 'pick')} should be replaced")
    if remove_count:
        parts.append(f"{remove_count} {_plural(remove_count, 'pick')} should be avoided")
    if caution_count:
        parts.append(f"{caution_count} {_plural(caution_count, 'pick')} need caution")
    if unverified_count:
        parts.append(f"{unverified_count} {_plural(unverified_count, 'pick')} need review")
    if parts:
        return "This ticket is risky. " + ", ".join(parts) + "."
    if risk_level == "high":
        return f"This ticket is risky. {weak_count or caution_count} pick(s) need attention."
    if risk_level == "medium":
        return f"This ticket is playable, but {replace_count + caution_count} leg(s) need attention."
    return "This ticket looks healthy from the current Match Checker analysis."


def _plural(value, singular, plural=None):
    return singular if int(value or 0) == 1 else (plural or f"{singular}s")


def _ticket_health_label(score):
    score = float_or_none(score)
    if score is None:
        return "Unknown"
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Good"
    if score >= 45:
        return "Risky"
    if score >= 20:
        return "Poor"
    return "Very Poor"


def _pick_confidence_label(score):
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


def _risk_level_from_confidence(score):
    score = float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 70:
        return "medium"
    return "low"


def _bettor_verdict_from_confidence(score):
    score = float_or_none(score)
    if score is None:
        return "needs_review"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "playable"
    return "high_risk"


def _bettor_verdict_label(code):
    return {
        "strong": "Strong pick",
        "playable": "Playable",
        "high_risk": "High risk",
        "needs_review": "Needs review",
    }.get(str(code or ""), "Needs review")


def _bettor_pick_message(verdict_code, *, market="", action=""):
    market = market or "This pick"
    if action == "replace":
        return f"The available statistics support a stronger option than {market}."
    if verdict_code == "strong":
        return "The available statistics strongly support this selection."
    if verdict_code == "playable":
        return "The available statistics give this selection some support, but it still carries risk."
    if verdict_code == "high_risk":
        return f"The available statistics do not strongly support {market}."
    return "There is not enough reliable data to judge this selection confidently."


def _ticket_issue_text(replace_count=0, remove_count=0, caution_count=0, unverified_count=0):
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {_plural(replace_count, 'pick')} to replace")
    if remove_count:
        parts.append(f"{remove_count} {_plural(remove_count, 'pick')} to avoid")
    if caution_count:
        parts.append(f"{caution_count} {_plural(caution_count, 'pick')} to treat carefully")
    if unverified_count:
        parts.append(f"{unverified_count} {_plural(unverified_count, 'pick')} needing review")
    return ", ".join(parts)


def _public_risk_label(value):
    return {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "unknown": "Unknown",
    }.get(str(value or "").lower(), "Unknown")


def _public_action_label(verdict):
    return {
        "keep": "Play",
        "caution": "Consider",
        "replace": "Replace",
        "remove": "Avoid",
        "expired": "Expired",
        "unmatched": "Needs review",
        "unmatched_market": "Needs review",
        "pending_analysis": "Analysing",
        "not_assessed": "Not assessed",
    }.get(str(verdict or "").lower(), "Review")


def _public_verdict_message(verdict, submitted_market=None, pick_status=None):
    market = submitted_market or "This pick"
    if str(verdict or "").lower() == "caution" and str(pick_status or "").lower() == "avoid":
        return f"{market} has low model support; treat it as high risk unless you accept the downside."
    return {
        "keep": f"{market} is playable from the current analysis.",
        "caution": f"{market} is playable, but it carries extra risk.",
        "replace": f"{market} is too risky compared with the suggested alternative.",
        "remove": f"{market} is too risky compared with safer options for this game.",
        "expired": "This event has already started or ended.",
        "unmatched": "We could not confidently match this fixture.",
        "unmatched_market": "We matched the fixture, but not this market.",
        "pending_analysis": "This fixture is still being analysed.",
        "not_assessed": f"We could not assess {market}, so it has not been judged either way.",
    }.get(str(verdict or "").lower(), "This pick needs review.")


def _public_verdict_object(verdict, submitted_market=None, pick_status=None):
    code = str(verdict or "review").lower()
    return {
        "code": code,
        "label": _public_action_label(code),
        "message": _public_verdict_message(code, submitted_market=submitted_market, pick_status=pick_status),
    }


def _public_market_meaning(market_name):
    descriptor = describe_market(market_name)
    for option in SLIP_REVIEW_MARKET_OPTIONS:
        if market_matches(market_name, option.get("value")):
            return option.get("meaning") or descriptor.canonical
    if descriptor.family == "unknown":
        return ""
    return descriptor.canonical


def _public_market_pick(market, *, fallback_market="", fallback_odds=None):
    if not market and not fallback_market:
        return None
    odds_source = (market or {}).get("odds_source", "")
    odds_status = "estimated" if str(odds_source).lower() == "estimated" else "verified" if market else ""
    score = float_or_none((market or {}).get("advisory_score"))
    market_name = (market or {}).get("market") or fallback_market
    payload = {
        "available": bool(market),
        "market": market_name,
        "label": market_name,
        "meaning": (market or {}).get("meaning") or _public_market_meaning(market_name),
        "confidence": (market or {}).get("final_confidence") or (market or {}).get("confidence"),
        "confidence_score": score,
        "confidence_label": _public_confidence_label(score),
        "odds": float_or_none((market or {}).get("odds")) if market else fallback_odds,
        "score": score,
        "decision_score": score,
        "status": match_checker_status(score),
        "odds_status": odds_status,
    }
    if market:
        payload["advisory_evidence"] = (market or {}).get("advisory_evidence") or {}
        payload["market_taxonomy"] = (market or {}).get("market_taxonomy") or describe_market(market_name).to_dict()
        payload["market_capability"] = (market or {}).get("market_capability") or {}
    return payload


def _public_recommendation_strength(pick):
    if not pick:
        return "no_recommendation"
    score = float_or_none(pick.get("score")) or 0
    if score >= 78:
        return "strong_recommendation"
    if score >= 66:
        return "playable"
    if score >= 55:
        return "safer_alternative"
    if score > 0:
        return "caution"
    return "no_recommendation"


def _price_reason_code(price_check):
    """The reason code for how the user's price compares to the reference, if known."""
    if not (price_check or {}).get("available"):
        return ""
    return {
        "positive_edge": "price_edge",
        "near_reference": "price_near_reference",
        "short_price": "price_short",
    }.get(price_check.get("status"), "price_reference")


def _public_selection_risk(verdict, pick):
    score = float_or_none((pick or {}).get("score"))
    status_value = str((pick or {}).get("status") or "").lower()
    if verdict in {"replace", "remove"}:
        return "high"
    if verdict in {"unmatched", "unmatched_market", "pending_analysis", "expired", "not_assessed"}:
        return "unknown"
    if status_value == "avoid" or (score is not None and score < 55):
        return "high"
    if verdict == "caution" or status_value == "caution" or (score is not None and score < 66):
        return "medium"
    if score is None:
        # No score means no opinion. Reporting "low" here would imply safety we never
        # established, which is a worse error than implying danger.
        return "unknown"
    return "low"


def _selection_has_analysis(item):
    if item.get("status") == "analysed":
        return True
    if item.get("status") == "market_not_found":
        selected_market = item.get("selected_market") or {}
        return bool(item.get("replacement_market")) or float_or_none(selected_market.get("advisory_score")) is not None
    return False


def _selection_is_unmatched(item):
    return item.get("status") in {"unmatched", "ambiguous_match"}


def _selection_strength_score(item):
    if not _selection_has_analysis(item):
        return None
    market = item.get("selected_market") or {}
    advisory_score = float_or_none(item.get("advisory_score") or market.get("advisory_score"))
    final_confidence = float_or_none(market.get("final_confidence") or market.get("confidence")) or 0
    display_score = float_or_none(market.get("display_score")) or final_confidence
    verdict_bonus = {
        "keep": 12,
        "caution": -4,
        "replace": -18,
        "remove": -35,
    }.get(item.get("verdict"), -20)
    risk_penalty = min(len(market.get("risk_flags") or []) * 2.5, 18)
    base_score = advisory_score if advisory_score is not None else (final_confidence * 0.6 + display_score * 0.25)
    score = base_score + verdict_bonus - risk_penalty
    return round(max(0, min(100, score)), 1)


def _selection_card(item):
    matched = item.get("matched_fixture") or {}
    selected_market = item.get("selected_market") or {}
    replacement_market = item.get("replacement_market") or {}
    action = item.get("verdict")
    leg_score = item.get("selection_score")
    if leg_score is None:
        risk_level = "unknown"
    elif leg_score < 45 or action == "remove":
        risk_level = "high"
    elif leg_score < 65 or action in {"replace", "caution"}:
        risk_level = "medium"
    else:
        risk_level = "low"
    alternative = None
    if replacement_market:
        alternative = {
            "market": replacement_market.get("market"),
            "confidence": replacement_market.get("final_confidence") or replacement_market.get("confidence"),
            "advisory_score": replacement_market.get("advisory_score"),
            "risk_level": (
                "low"
                if (replacement_market.get("advisory_score") or 0) >= 78
                else "medium"
                if (replacement_market.get("advisory_score") or 0) >= 55
                else "high"
            ),
            "odds": float_or_none(replacement_market.get("odds")),
            "ev": float_or_none(replacement_market.get("ev")),
            "reason": match_checker_alternative_reason(item.get("submitted_market"), replacement_market),
            "replacement_scope": replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market),
            "evidence": replacement_market.get("advisory_evidence") or {},
            "warnings": replacement_market.get("advisory_warnings") or [],
        }
    return {
        "match": item.get("match"),
        "fixture": matched.get("fixture") or item.get("match"),
        "match_id": matched.get("match_id", ""),
        "submitted_market": item.get("submitted_market"),
        "verdict": item.get("verdict"),
        "recommended_action": action,
        "no_replacement_available": bool(item.get("no_replacement_available")),
        "status": item.get("status"),
        "score": item.get("selection_score"),
        "submitted_pick_score": item.get("selection_score"),
        "leg_score": leg_score,
        "risk_level": risk_level,
        "advisory_score": item.get("advisory_score") or selected_market.get("advisory_score"),
        "advisory_status": item.get("advisory_status") or selected_market.get("advisory_status"),
        "advisory_basis": selected_market.get("advisory_basis"),
        "evidence": selected_market.get("advisory_evidence") or {},
        "match_resolution_score": (matched.get("match_score") if matched else None),
        "confidence": selected_market.get("final_confidence") or selected_market.get("confidence"),
        "odds": _selection_original_odds(item),
        "suggested_market": replacement_market.get("market") if item.get("verdict") == "replace" else item.get("submitted_market"),
        "suggested_odds": _selection_suggested_odds(item),
        "suggested_advisory_score": replacement_market.get("advisory_score") if replacement_market else None,
        "suggested_advisory_status": replacement_market.get("advisory_status") if replacement_market else "",
        "alternative": alternative,
        "message": item.get("message", ""),
        "why_risky": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:4],
        "warnings": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:6],
        "statpal_advisory": item.get("statpal_advisory") or selected_market.get("statpal_advisory") or {},
        "statpal_context": item.get("statpal_context") or {},
    }


def _without_remove_recommendation(item):
    if item.get("verdict") != "remove":
        return item
    copy = dict(item)
    selected_market = copy.get("selected_market") or {}
    replacement_market = copy.get("replacement_market") or {}
    if replacement_market and _replacement_is_meaningfully_better(selected_market, replacement_market):
        copy["verdict"] = "replace"
        copy["message"] = (
            copy.get("message")
            or "This selection is high risk; use the statistically backed alternative instead."
        )
    else:
        copy["verdict"] = "caution"
        copy["no_replacement_available"] = True
        copy["message"] = (
            copy.get("message")
            or "This selection is high risk, but no stronger backed replacement was found for this game."
        )
    return copy


def _without_blocked_replacement_recommendation(item):
    replacement_market = (item or {}).get("replacement_market") or {}
    if not replacement_market or not _blocked_slip_recommendation_market(replacement_market):
        return item
    copy = dict(item)
    blocked = list(copy.get("blocked_recommendation_markets") or [])
    if replacement_market.get("market"):
        blocked.append(replacement_market.get("market"))
    copy["blocked_recommendation_markets"] = list(dict.fromkeys(blocked))
    copy["replacement_market"] = None
    if copy.get("verdict") == "replace":
        copy["verdict"] = "caution"
        copy["better_market_available"] = False
        copy["no_replacement_available"] = True
        copy["message"] = (
            "This selection is risky, but no stronger backed replacement was found for this game."
        )
    return copy


def _with_bettor_view(card):
    user_pick = card.get("user_pick") or {}
    ai_pick = card.get("ai_pick") or {}
    action = "replace" if ai_pick.get("available") and (card.get("verdict") or {}).get("code") == "replace" else (
        "keep" if (card.get("verdict") or {}).get("code") in {"keep", "caution"} else "review"
    )
    user_verdict = _bettor_verdict_from_confidence(user_pick.get("confidence_score"))
    user_pick.update(
        {
            "verdict": "replace" if action == "replace" else user_verdict,
            "verdict_label": _bettor_verdict_label(user_verdict),
            "message": _bettor_pick_message(user_verdict, market=user_pick.get("market"), action=action),
        }
    )
    card["user_pick"] = user_pick

    evidence = list(dict.fromkeys(card.get("why") or []))[:5]
    card["evidence"] = evidence
    card["our_view"] = user_pick["message"]

    if action == "replace":
        recommendation_why = []
        if ai_pick.get("market"):
            recommendation_why.append(
                f"{ai_pick.get('market')} has stronger statistical support than the original selection."
            )
        if card.get("comparison", {}).get("confidence_gain") is not None:
            recommendation_why.append(
                f"It improves this leg's confidence by {card['comparison']['confidence_gain']} points."
            )
        recommendation_why.extend(evidence[:2])
        card["recommendation"] = {
            "action": "replace",
            "market": ai_pick.get("market"),
            "confidence": ai_pick.get("confidence_score"),
            "confidence_label": ai_pick.get("confidence_label"),
            "risk_level": ai_pick.get("risk_level"),
            "message": "Use the stronger backed alternative for this fixture.",
            "why": list(dict.fromkeys(recommendation_why))[:4],
        }
    elif action == "keep":
        card["recommendation"] = {
            "action": "keep",
            "market": user_pick.get("market"),
            "confidence": user_pick.get("confidence_score"),
            "confidence_label": user_pick.get("confidence_label"),
            "risk_level": user_pick.get("risk_level"),
            "message": "Keep this selection, but respect the stated risk level.",
            "why": evidence[:4],
        }
    else:
        card["recommendation"] = {
            "action": "review",
            "market": user_pick.get("market"),
            "confidence": user_pick.get("confidence_score"),
            "confidence_label": user_pick.get("confidence_label"),
            "risk_level": user_pick.get("risk_level"),
            "message": "Do not treat this as supported until more reliable match data is available.",
            "why": evidence[:4],
        }
    return card


def _leg_state_counts(items):
    """
    Where every leg stopped, and how it was assessed.

    `heuristic` legs are deliberately excluded from the ticket probability: their score
    is a constant plus context nudges, not a modelled probability. Reporting the split
    is what stops that exclusion looking like a silent gap.
    """
    states = {}
    assessments = {}
    for item in items:
        assessment = assess_leg(item)
        states[str(assessment.state)] = states.get(str(assessment.state), 0) + 1
        assessments[assessment.assessment_type] = assessments.get(assessment.assessment_type, 0) + 1
    return {"by_state": states, "by_assessment_type": assessments}


def _with_leg_risk(card, leg):
    """Attach the calibrated risk view of a leg to its public card."""
    tier_label = "High risk" if leg.tier == "avoid" else leg.tier_label
    probability_percent = _round_percent(leg.probability)
    repair_probability_percent = _round_percent(leg.repair_probability)
    selection_lift = (
        round(repair_probability_percent - probability_percent, 1)
        if repair_probability_percent is not None and probability_percent is not None
        else None
    )
    card["risk_tier"] = {
        "code": leg.tier,
        "label": tier_label,
        "estimated_success_percent": probability_percent,
        "risk_share_percent": leg.risk_share_percent,
        # `capped_by_data_quality` now means "the claim was held back", not "the
        # number was truncated" -- the probability below is reported as modelled.
        "capped_by_data_quality": leg.capped_by_data_quality,
        "data_confidence_percent": leg.data_confidence_percent,
    }
    card["repair"] = {
        "available": leg.repair_probability is not None,
        "estimated_success_percent": repair_probability_percent,
        "selection_lift_points": selection_lift,
        "ticket_lift_points": leg.repair_lift_points,
        "drop_lift_points": leg.drop_lift_points,
    }
    your_pick = card.get("your_pick") or {}
    data_confidence_score = float_or_none(
        your_pick.get("data_confidence")
        if your_pick.get("data_confidence") is not None
        else (leg.data_confidence_percent if leg.data_confidence_percent is not None else (your_pick.get("confidence_cap") or your_pick.get("confidence")))
    )
    offered_probability = _implied_probability_from_odds(your_pick.get("odds"))
    price_check = card.get("price_check") or {}
    reference_probability = _implied_probability_from_odds(price_check.get("reference_odds"))
    disagreement_gap = _probability_gap(leg.probability, reference_probability)
    pick_confidence_score = probability_percent
    your_pick.update(
        {
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "fair_odds": _fair_odds(leg.probability),
            "confidence_score": pick_confidence_score,
            "confidence_label": _pick_confidence_label(pick_confidence_score),
            "data_confidence_score": data_confidence_score,
            "decision_score": your_pick.get("decision_score", your_pick.get("score")),
            "risk_score": round((1 - leg.probability) * 100, 1) if leg.probability is not None else None,
            "risk_level": _risk_level_from_confidence(pick_confidence_score),
            "market_implied_probability": offered_probability,
            "market_implied_probability_percent": _round_percent(offered_probability),
            "value_rating": _value_rating(leg.probability, your_pick.get("odds")),
        }
    )
    card["your_pick"] = your_pick
    ai_same_as_user = bool(card.get("ai_pick")) and leg.repair_probability is None
    card["user_pick"] = {
        "market": your_pick.get("market"),
        "odds": your_pick.get("odds"),
        "confidence_score": pick_confidence_score,
        "confidence_label": _pick_confidence_label(pick_confidence_score),
        "risk_level": _risk_level_from_confidence(pick_confidence_score),
        "model_probability_percent": probability_percent,
        "data_confidence_score": data_confidence_score,
        "verdict": (card.get("verdict") or {}).get("code"),
    }
    if card.get("ai_pick"):
        ai_data_confidence_score = float_or_none(
            card["ai_pick"].get("confidence") or data_confidence_score
        )
        ai_probability = leg.repair_probability if leg.repair_probability is not None else leg.probability
        ai_confidence_score = repair_probability_percent if repair_probability_percent is not None else probability_percent
        card["ai_pick"].update(
            {
                "model_probability": ai_probability,
                "model_probability_percent": ai_confidence_score,
                "fair_odds": _fair_odds(ai_probability),
                "available": True,
                "confidence_score": ai_confidence_score,
                "confidence_label": _pick_confidence_label(ai_confidence_score),
                "data_confidence_score": ai_data_confidence_score,
                "decision_score": card["ai_pick"].get("decision_score", card["ai_pick"].get("score")),
                "risk_level": _risk_level_from_confidence(ai_confidence_score),
                "selection_lift_points": selection_lift,
            }
        )
    else:
        card["ai_pick"] = {"available": False}
    card["comparison"] = {
        "confidence_gain": 0.0 if ai_same_as_user and selection_lift is None else selection_lift,
        "selection_probability_lift": 0.0 if ai_same_as_user and selection_lift is None else selection_lift,
        "ticket_success_lift": leg.repair_lift_points,
    }
    if reference_probability is not None:
        card["market_consensus"] = {
            "reference_odds": price_check.get("reference_odds"),
            "implied_probability": reference_probability,
            "implied_probability_percent": _round_percent(reference_probability),
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "probability_gap_points": disagreement_gap,
            "disagreement_level": _gap_level(disagreement_gap),
        }
        if abs(disagreement_gap or 0) >= 15:
            card.setdefault("reason_codes", [])
            if "model_market_disagreement" not in card["reason_codes"]:
                card["reason_codes"].append("model_market_disagreement")
            card.setdefault("why", [])
            card["why"].append(
                "The model and market consensus disagree strongly, so treat this verdict with extra caution."
            )
    return card


def _public_ticket_killers(ticket_risk):
    selections = []
    for killer in ticket_risk.killers:
        copy = dict(killer)
        if copy.get("tier") == "avoid":
            copy["tier_label"] = "High risk"
        selections.append(copy)
    return selections


def _ticket_risk_level_from_score(score):
    score = float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 65:
        return "medium"
    return "low"


def _repaired_ticket_confidence_score(ticket_risk):
    probabilities = []
    for leg in ticket_risk.legs:
        probability = leg.repair_probability if leg.repair_probability is not None else leg.probability
        if probability is not None:
            probabilities.append(probability)
    if not probabilities:
        return None
    return round(math.exp(sum(math.log(probability) for probability in probabilities) / len(probabilities)) * 100, 1)


def _bettor_pick_breakdown(selections):
    breakdown = {"strong": 0, "playable": 0, "high_risk": 0, "needs_review": 0}
    for selection in selections or []:
        verdict = _simple_pick_verdict(selection)
        confidence = float_or_none((selection.get("user_pick") or {}).get("confidence_score"))
        if verdict == "review":
            code = "needs_review"
        elif verdict == "risky":
            code = "high_risk"
        elif confidence is not None and confidence >= 70:
            code = "strong"
        else:
            code = "playable"
        breakdown[code] = breakdown.get(code, 0) + 1
    return breakdown


def _public_score(value):
    value = float_or_none(value)
    return int(round(value)) if value is not None else None


def _public_confidence_label(score):
    return _pick_confidence_label(score)


def _public_ticket_label(score):
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


def _simple_pick_verdict(selection):
    raw_verdict = (selection or {}).get("verdict") or {}
    verdict = raw_verdict.get("code") if isinstance(raw_verdict, dict) else raw_verdict
    verdict = str(verdict or "").strip().lower()
    confidence = float_or_none(((selection or {}).get("user_pick") or {}).get("confidence_score"))
    if verdict in {"replace", "remove"}:
        return "risky"
    if verdict == "keep":
        return "keep"
    if verdict == "caution":
        return "risky" if confidence is not None and confidence < 55 else "caution"
    if verdict in {"expired", "not_assessed", "unmatched", "unmatched_market", "pending_analysis"}:
        return "review"
    return "risky" if confidence is not None and confidence < 55 else "caution"


def _evidence_is_risk(text):
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "risk",
            "weak",
            "only",
            "limited",
            "not enough",
            "disagree",
            "shorter",
            "thin",
            "close to",
            "caution",
            "unsupported",
            "poor",
        )
    )


def _public_market_context_line(selection, market_payload=None):
    market_payload = market_payload or {}
    user_market = ((selection or {}).get("user_pick") or {}).get("market") or ""
    market_name = market_payload.get("market") or user_market
    confidence = _public_score(
        market_payload.get("model_probability_percent")
        or market_payload.get("confidence_score")
        or market_payload.get("score")
        or market_payload.get("decision_score")
        or ((selection or {}).get("user_pick") or {}).get("confidence_score")
    )
    odds = market_payload.get("odds")
    if odds is None and (not market_payload.get("market") or market_matches(market_name, user_market)):
        odds = ((selection or {}).get("user_pick") or {}).get("odds")
    ev = market_payload.get("ev")
    parts = []
    if confidence is not None:
        parts.append(f"{confidence}% confidence")
    if odds is not None:
        parts.append(f"{odds} odds")
    if ev is not None:
        parts.append(f"{float(ev):+.3f} expected value")
    if not parts:
        return ""
    sentence = f"{market_name} rates at {parts[0]}"
    if len(parts) > 1:
        sentence += f" with {', '.join(parts[1:])}"
    return sentence + "."




def _clean_public_slip_evidence_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "broader fallback" in lowered:
        return ""
    if "statpal context available" in lowered:
        return ""
    if "statpal" in lowered and any(marker in lowered for marker in ("context", "snapshot", "available")):
        return ""
    if "snapshot" in lowered and any(marker in lowered for marker in ("available", "missing", "required")):
        return ""
    text = text.replace("StatPal-backed expected goals", "expected goals")
    text = text.replace("StatPal-backed", "")
    text = text.replace("StatPal", "").strip()
    text = text.replace("_", " ")
    return " ".join(text.split())




def _clean_bettor_evidence_items(items, *, limit=4):
    cleaned = []
    for item in items or []:
        text = _clean_public_slip_evidence_text(item)
        lowered = text.lower()
        if not text:
            continue
        if "statpal reference" in lowered or "reference price" in lowered or "your price is" in lowered:
            continue
        cleaned.append(text[:240])
    return list(dict.fromkeys(cleaned))[:limit]


def _text_mentions_blocked_slip_recommendation_market(text):
    lowered = normalize_market_text(text)
    blocked_markets = (
        "over 0.5",
        "1h over 0.5",
        "2h over 0.5",
        "home team over 0.5",
        "away team over 0.5",
        "shots over 0.5",
        "shots on target over 0.5",
    )
    return any(market in lowered for market in blocked_markets)


def _clean_deepseek_recommendation_why(game, items):
    user_market = ((game or {}).get("user_pick") or {}).get("market")
    recommendation = (game or {}).get("recommendation") or {}
    recommendation_market = (recommendation.get("pick") or {}).get("market")
    user_submitted_blocked_market = _blocked_slip_recommendation_market({"market": user_market})
    recommended_market_is_user_market = market_matches(user_market, recommendation_market)
    allow_blocked_text = user_submitted_blocked_market and recommended_market_is_user_market
    cleaned = []
    for item in _clean_bettor_evidence_items(items):
        if not allow_blocked_text and _text_mentions_blocked_slip_recommendation_market(item):
            continue
        cleaned.append(item)
    return cleaned




def _bettor_game_summary(selection):
    user_pick = (selection or {}).get("user_pick") or {}
    market = user_pick.get("market") or "this selection"
    verdict = _simple_pick_verdict(selection)
    if verdict == "keep":
        return f"The statistics support keeping {market}."
    if verdict == "caution":
        return f"{market} is playable, but it is not one of the safest legs on this ticket."
    if verdict == "risky":
        return f"The statistics do not strongly support {market}."
    return f"We need stronger match data before judging {market}."


def _bettor_conclusion(selection):
    user_pick = (selection or {}).get("user_pick") or {}
    market = user_pick.get("market") or "this selection"
    verdict = _simple_pick_verdict(selection)
    if verdict == "keep":
        return f"The available evidence supports keeping {market}."
    if verdict == "caution":
        return f"{market} is playable, but it carries enough risk to treat carefully."
    if verdict == "risky":
        return f"{market} carries too much risk based on the available match evidence."
    return f"{market} has not been backed by enough reliable match evidence yet."




SMART_RANDOMIZE_MIN_CONFIDENCE = 55.0


SMART_RANDOMIZE_ELIGIBLE_VERDICTS = frozenset({"keep", "caution"})


def _smart_randomize_option_values(eligible_count):
    count = int(eligible_count or 0)
    if count < 2:
        return []
    options = list(range(2, count, 2))
    return options or [2]


def _smart_randomize_ranking_score(confidence, data_confidence):
    """
    The claim we are willing to stand behind, which is what selection must rank on.

    `confidence_score` is the modelled probability and is reported unchanged (ADR-005).
    Ranking on it alone would promote an 88%-estimate-on-58-points-of-evidence over an
    80%-estimate-on-92-points -- and put a leg the review labels `caution` at the top of
    a ticket sold as "the strongest analysed picks". Gating on the same
    `min(probability, confidence)` the status already uses keeps the two consistent.
    """
    confidence = float_or_none(confidence)
    if confidence is None:
        return None
    data_confidence = float_or_none(data_confidence)
    return confidence if data_confidence is None else min(confidence, data_confidence)


def _smart_randomize_pick_for_game(game):
    user_pick = (game or {}).get("user_pick") or {}
    recommendation = (game or {}).get("recommendation") or {}
    recommended_pick = recommendation.get("pick") or {}
    candidates = []

    user_confidence = float_or_none(user_pick.get("confidence_score"))
    user_verdict = str(user_pick.get("verdict") or "").lower()
    if user_confidence is not None and user_verdict in SMART_RANDOMIZE_ELIGIBLE_VERDICTS:
        candidates.append(
            {
                "source": "user_pick",
                "action": recommendation.get("action") or user_verdict or "keep",
                "market": user_pick.get("market"),
                "odds": user_pick.get("odds"),
                "confidence_score": user_confidence,
                "confidence_label": _public_confidence_label(user_confidence),
                "data_confidence_score": float_or_none(user_pick.get("data_confidence_score")),
                "ranking_score": _smart_randomize_ranking_score(
                    user_confidence, user_pick.get("data_confidence_score")
                ),
                "changed_from_user_pick": False,
            }
        )

    recommended_confidence = float_or_none(recommended_pick.get("confidence_score"))
    if recommended_pick and recommended_confidence is not None:
        action = recommendation.get("action") or "recommend"
        changed = action == "replace" and not market_matches(
            recommended_pick.get("market"),
            user_pick.get("market"),
        )
        candidates.append(
            {
                "source": "ai_pick" if changed else "user_pick",
                "action": action,
                "market": recommended_pick.get("market"),
                "odds": recommended_pick.get("odds"),
                "confidence_score": recommended_confidence,
                "confidence_label": _public_confidence_label(recommended_confidence),
                "data_confidence_score": float_or_none(recommended_pick.get("data_confidence_score")),
                "ranking_score": _smart_randomize_ranking_score(
                    recommended_confidence, recommended_pick.get("data_confidence_score")
                ),
                "changed_from_user_pick": changed,
            }
        )

    candidates = [item for item in candidates if item.get("ranking_score") is not None]
    if not candidates:
        return None
    pick = max(candidates, key=lambda item: (item["ranking_score"], 1 if item["source"] == "ai_pick" else 0))
    if pick["ranking_score"] < SMART_RANDOMIZE_MIN_CONFIDENCE:
        return None
    return {
        "id": (game or {}).get("id"),
        "match": (game or {}).get("match"),
        "kickoff": (game or {}).get("kickoff"),
        **pick,
    }


def _smart_randomize_candidates(public_payload):
    candidates = []
    excluded = []
    for game in (public_payload or {}).get("games") or []:
        pick = _smart_randomize_pick_for_game(game)
        if pick:
            candidates.append(pick)
        else:
            excluded.append(
                {
                    "id": (game or {}).get("id"),
                    "match": (game or {}).get("match"),
                    "reason": "No analysed pick reached the minimum confidence for a generated ticket.",
                }
            )
    return sorted(
        candidates,
        key=lambda item: (item.get("ranking_score") or 0, item.get("match") or ""),
        reverse=True,
    ), excluded


def _smart_randomize_summary(public_payload):
    candidates, _ = _smart_randomize_candidates(public_payload)
    options = _smart_randomize_option_values(len(candidates))
    return {
        "available": bool(options),
        "options": options,
        "eligible_games": len(candidates),
        "min_confidence_score": SMART_RANDOMIZE_MIN_CONFIDENCE,
        "message": (
            "Build a smaller ticket from the strongest analysed picks in this slip."
            if options
            else "Not enough analysed picks reached the minimum confidence for smart randomize."
        ),
    }


def _smart_randomize_ticket(public_payload, requested_games):
    requested = int(requested_games or 0)
    candidates, excluded = _smart_randomize_candidates(public_payload)
    options = _smart_randomize_option_values(len(candidates))
    if requested not in options:
        return None, {
            "detail": "Choose one of the available smart randomize options.",
            "available_options": options,
            "eligible_games": len(candidates),
        }

    selected = candidates[:requested]
    probabilities = [
        max(1.0, min(95.0, float(item["confidence_score"]))) / 100.0
        for item in selected
        if item.get("confidence_score") is not None
    ]
    ticket_probability = None
    ticket_confidence = None
    if probabilities:
        total = 1.0
        for probability in probabilities:
            total *= probability
        ticket_probability = round(total * 100, 2) if total * 100 >= 0.01 else float(f"{total * 100:.4g}")
        ticket_confidence = round(math.exp(sum(math.log(probability) for probability in probabilities) / len(probabilities)) * 100, 1)

    odds_values = [float_or_none(item.get("odds")) for item in selected]
    odds_complete = all(value and value > 1 for value in odds_values)
    selected_keys = {(item.get("id"), item.get("match"), item.get("market")) for item in selected}
    return {
        "review_id": (public_payload or {}).get("id"),
        "requested_games": requested,
        "available_options": options,
        "ticket": {
            "total_games": len(selected),
            "confidence_score": _public_score(ticket_confidence),
            "confidence_label": _public_ticket_label(ticket_confidence),
            "estimated_success_percent": ticket_probability,
            "estimated_success_display": _success_percent_display(ticket_probability),
            "estimated_odds": _combined_odds(odds_values) if odds_complete else None,
            "odds_complete": odds_complete,
            "label": _public_ticket_label(ticket_confidence),
        },
        "picks": [
            {
                "id": item.get("id"),
                "match": item.get("match"),
                "kickoff": item.get("kickoff"),
                "market": item.get("market"),
                "odds": item.get("odds"),
                "source": item.get("source"),
                "action": item.get("action"),
                "confidence_score": _public_score(item.get("confidence_score")),
                "confidence_label": _public_confidence_label(item.get("confidence_score")),
                "data_confidence_score": _public_score(item.get("data_confidence_score")),
                "changed_from_user_pick": bool(item.get("changed_from_user_pick")),
            }
            for item in selected
        ],
        "excluded": excluded + [
            {
                "id": item.get("id"),
                "match": item.get("match"),
                "market": item.get("market"),
                "confidence_score": _public_score(item.get("confidence_score")),
                "reason": "Lower confidence than the selected smart-randomize picks.",
            }
            for item in candidates
            if (item.get("id"), item.get("match"), item.get("market")) not in selected_keys
        ],
        "disclaimer": (
            "Smart randomize selects the strongest analysed picks from this slip. "
            "Confidence scores are statistical estimates and do not guarantee an outcome."
        ),
    }, None


def _with_smart_randomize(public_payload):
    payload = dict(public_payload or {})
    if payload.get("status") in {"queued", "importing", "analysing"}:
        return payload
    payload["smart_randomize"] = _smart_randomize_summary(payload)
    return payload


def _ticket_killers_message(ticket_risk):
    killers = ticket_risk.killers
    if not killers:
        if not ticket_risk.assessed_legs:
            return "No selection could be assessed, so no risk ranking is available."
        return "No single selection dominates this ticket's risk."
    share = round(sum(killer["risk_share_percent"] for killer in killers), 1)
    count = len(killers)
    lift = sum(killer["drop_lift_points"] or 0 for killer in killers)
    message = (
        f"{count} {_plural(count, 'selection')} {'carries' if count == 1 else 'carry'} "
        f"{share}% of this ticket's risk."
    )
    if lift > 0:
        message += f" Changing {'it' if count == 1 else 'them'} to safer backed alternatives would raise the estimated success rate by about {round(lift, 2)} percentage points."
    return message


def _settlement_market_for(item):
    """
    Canonical, orientation-corrected market used to settle this leg after kickoff.

    Returns "" when the market cannot be resolved from a finished fixture, which the
    settler records as ``unsettleable`` rather than a void.
    """
    market = item.get("analysis_market")
    if not market:
        canonical = (item.get("market_taxonomy") or {}).get("canonical") or ""
        if canonical:
            market = _market_for_fixture_orientation(canonical, item.get("matched_fixture") or {})
    market = str(market or "").strip()
    return market if can_settle_market(market) else ""


def _selection_flagged_risky(item):
    """Whether this leg was called out pre-kickoff, frozen at analysis time."""
    return item.get("verdict") in {"remove", "replace", "caution"}


def _empty_slip_summary(verdict, *, task_id="", error=""):
    summary = {
        "count": 0,
        "analysed_count": 0,
        "keep_count": 0,
        "caution_count": 0,
        "replace_count": 0,
        "remove_count": 0,
        "expired_count": 0,
        "unmatched_count": 0,
        "pending_analysis_count": 0,
        "api_usage": _empty_api_usage(),
        "intelligence": {
            "overall_score": 0,
            "risk_level": "medium" if not error else "high",
            "verdict": verdict,
            "api_usage": _empty_api_usage(),
            "original_combined_odds": None,
            "suggested_combined_odds": None,
            "strongest_legs": [],
            "weakest_legs": [],
            "legs_to_keep": [],
            "legs_to_caution": [],
            "legs_to_replace": [],
            "legs_to_remove": [],
            "expired_legs": [],
            "unverified_legs": [],
        },
    }
    if task_id:
        summary["task_id"] = task_id
    if error:
        summary["error"] = str(error)
    return summary


def _slip_selection_payload(selection):
    payload = dict(selection.analysis_payload or {})
    payload.setdefault("match", selection.submitted_match)
    payload.setdefault("submitted_market", selection.submitted_market)
    payload.setdefault("status", selection.status)
    payload.setdefault("verdict", selection.verdict)
    payload.setdefault("message", selection.message)
    return payload


def _completed_slip_selection_payloads(review):
    selections = list(getattr(review, "_prefetched_objects_cache", {}).get("selections") or [])
    if not selections:
        selections = list(review.selections.all().order_by("order", "id"))
    completed = []
    for selection in selections:
        payload = _slip_selection_payload(selection)
        if _selection_has_analysis(payload):
            completed.append(payload)
    return completed


def _compact_ai_pick_from_selection(selection):
    payload = selection.analysis_payload or {}
    replacement = payload.get("replacement_market") or {}
    selected = payload.get("selected_market") or {}
    recommended = payload.get("recommended_market") or {}
    if replacement:
        source = replacement
        action = "replace"
    elif recommended:
        source = recommended
        action = payload.get("verdict") or "recommend"
    elif selected:
        source = selected
        action = payload.get("verdict") or "keep"
    else:
        source = {}
        action = "review"
    confidence = (
        source.get("confidence_score")
        or source.get("advisory_score")
        or source.get("final_confidence")
        or source.get("confidence")
    )
    return {
        "market": source.get("market") or selection.submitted_market,
        "confidence_score": _public_score(confidence),
        "action": action,
    }


def _slip_review_booking_code(review):
    payload = review.submitted_payload or {}
    for key in ("provider_code", "share_code", "booking_code", "code"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _provider_metadata(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    sport = outcome.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    provider_competition_id = str(tournament.get("id") or "")
    return {
        "provider": selection.get("provider") or provider_payload.get("provider") or "",
        "provider_event_id": provider_payload.get("provider_event_id") or outcome.get("eventId") or "",
        "provider_competition_id": provider_competition_id,
        "competition": provider_payload.get("competition") or tournament.get("name") or "",
        "home_team": provider_payload.get("home_team") or outcome.get("homeTeamName") or "",
        "away_team": provider_payload.get("away_team") or outcome.get("awayTeamName") or "",
    }


def _sportybet_statpal_event(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    event = dict(outcome) if isinstance(outcome, dict) else {}
    event.setdefault("eventId", provider_payload.get("provider_event_id") or "")
    event.setdefault("homeTeamName", provider_payload.get("home_team") or "")
    event.setdefault("awayTeamName", provider_payload.get("away_team") or "")
    event.setdefault("estimateStartTime", provider_payload.get("kickoff_ms") or "")
    if provider_payload.get("competition") and not event.get("sport"):
        event["sport"] = {"category": {"tournament": {"name": provider_payload.get("competition")}}}
    return event


def _hit_rate(wins, losses):
    settled = wins + losses
    return round((wins / settled) * 100, 1) if settled else None


def _repair_payload(review, plan, repair):
    return {
        "repair_id": repair.id,
        "review_id": review.id,
        "mode": repair.mode,
        "original": {
            "legs": plan.original_legs,
            "combined_odds": plan.original_combined_odds,
            "estimated_success_percent": plan.original_success_percent,
        },
        "revised": {
            "legs": plan.revised_legs,
            "combined_odds": plan.revised_combined_odds,
            "estimated_success_percent": plan.revised_success_percent,
        },
        "changes": plan.changes,
        "decisions": [decision.to_dict() for decision in plan.decisions],
        "disclosure": plan.disclosure,
    }


def _stream_ticket_hash(ticket):
    return hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()


def _combined_probability(scores):
    probabilities = [
        max(1.0, min(95.0, float(score))) / 100.0
        for score in scores
        if score is not None
    ]
    if not probabilities:
        return None
    total = 1.0
    for probability in probabilities:
        total *= probability
    return round(total * 100, 1)


def _optimized_leg_score(item):
    if item.get("verdict") == "replace":
        return float_or_none((item.get("replacement_market") or {}).get("advisory_score"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return float_or_none(item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score"))


def _public_slip_review_error_message(error_code="analysis_failed"):
    messages = {
        "soft_time_limit_exceeded": "This selection took too long to analyse. Please retry in a moment.",
        "analysis_failed": "We could not analyse this selection right now. Please retry in a moment.",
        "failed": "Slip review failed. Please retry in a moment.",
    }
    return messages.get(str(error_code or ""), messages["analysis_failed"])


def _slip_review_completed_leg_count(review):
    return review.selections.exclude(status__in=["queued", "analysing", ""]).count()


def _leg_results_from_persisted_slip_selections(review):
    leg_results = []
    for selection in review.selections.order_by("order"):
        payload = selection.analysis_payload or {}
        if payload.get("status") in {"queued", "analysing", ""}:
            continue
        leg_results.append(
            {
                "review_id": review.id,
                "index": max(0, int(selection.order or 1) - 1),
                "status": payload.get("status") or selection.status or "",
                "result": payload,
                "hydration": {},
            }
        )
    return leg_results
