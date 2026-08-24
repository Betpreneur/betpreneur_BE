"""Slip-review selection row persistence helpers."""

from apps.algo.models import SlipSelection
from apps.algo.slip_review.lifecycle import json_safe
from apps.algo.slip_review.public_formatting import float_or_none


def slip_selection_defaults_from_analysis(
    item,
    *,
    settlement_market_for,
    decimal_or_none,
):
    item = json_safe(item or {})
    matched = item.get("matched_fixture") or {}
    return {
        "submitted_match": item.get("match", ""),
        "submitted_market": item.get("submitted_market") or item.get("market", ""),
        "status": item.get("status", ""),
        "verdict": item.get("verdict", ""),
        "message": item.get("message", ""),
        "match_id": matched.get("match_id") or "",
        "match_date": matched.get("match_date") or None,
        "fixture": matched.get("fixture") or "",
        "home_team": matched.get("home_team") or "",
        "away_team": matched.get("away_team") or "",
        "league": matched.get("league") or "",
        "country": matched.get("country") or "",
        "kickoff": matched.get("kickoff") or "",
        "selected_market": item.get("selected_market") or {},
        "best_market": item.get("best_market") or {},
        "recommended_market": item.get("recommended_market") or {},
        "possible_matches": item.get("possible_matches") or [],
        "analysis_payload": item,
        "settlement_market": settlement_market_for(item),
        "odds": decimal_or_none(selection_original_odds(item)),
        "flagged_risky": selection_flagged_risky(item),
        "advisory_score": float_or_none(
            item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score")
        ),
    }


def selection_original_odds(item):
    provider_payload = (item or {}).get("provider_payload") or {}
    odds = provider_payload.get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("selection") or {}).get("odds")
    if odds is None:
        odds = ((provider_payload.get("provider_payload") or {}).get("leg") or {}).get("odds")
    if odds is None:
        odds = ((item or {}).get("selected_market") or {}).get("odds")
    return float_or_none(odds)


def selection_suggested_odds(item):
    item = item or {}
    if item.get("verdict") == "replace":
        return float_or_none((item.get("replacement_market") or {}).get("odds"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return selection_original_odds(item) or float_or_none((item.get("selected_market") or {}).get("odds"))


def optimized_leg_score(item):
    item = item or {}
    if item.get("verdict") == "replace":
        return float_or_none((item.get("replacement_market") or {}).get("advisory_score"))
    if item.get("status") != "analysed":
        return None
    if item.get("verdict") == "remove":
        return None
    return float_or_none(item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score"))


def selection_has_analysis(item):
    item = item or {}
    if item.get("status") == "analysed":
        return True
    if item.get("status") == "market_not_found":
        selected_market = item.get("selected_market") or {}
        return bool(item.get("replacement_market")) or float_or_none(selected_market.get("advisory_score")) is not None
    return False


def selection_is_unmatched(item):
    return (item or {}).get("status") in {"unmatched", "ambiguous_match"}


def selection_strength_score(item):
    item = item or {}
    if not selection_has_analysis(item):
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


def selection_card(item, *, alternative_reason, replacement_scope):
    item = item or {}
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
            "reason": alternative_reason(item.get("submitted_market"), replacement_market),
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
        "odds": selection_original_odds(item),
        "suggested_market": replacement_market.get("market") if item.get("verdict") == "replace" else item.get("submitted_market"),
        "suggested_odds": selection_suggested_odds(item),
        "suggested_advisory_score": replacement_market.get("advisory_score") if replacement_market else None,
        "suggested_advisory_status": replacement_market.get("advisory_status") if replacement_market else "",
        "alternative": alternative,
        "message": item.get("message", ""),
        "why_risky": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:4],
        "warnings": (selected_market.get("advisory_warnings") or selected_market.get("risk_flags") or [])[:6],
        "statpal_advisory": item.get("statpal_advisory") or selected_market.get("statpal_advisory") or {},
        "statpal_context": item.get("statpal_context") or {},
    }


def selection_flagged_risky(item):
    """Whether this leg was called out pre-kickoff, frozen at analysis time."""
    return (item or {}).get("verdict") in {"remove", "replace", "caution"}


def settlement_market_for(item, *, market_for_fixture_orientation, can_settle_market):
    """
    Canonical, orientation-corrected market used to settle this leg after kickoff.

    Returns "" when the market cannot be resolved from a finished fixture, which the
    settler records as ``unsettleable`` rather than a void.
    """
    market = item.get("analysis_market")
    if not market:
        canonical = (item.get("market_taxonomy") or {}).get("canonical") or ""
        if canonical:
            market = market_for_fixture_orientation(canonical, item.get("matched_fixture") or {})
    market = str(market or "").strip()
    return market if can_settle_market(market) else ""


def initial_slip_selection_payload(selection):
    provider_payload = json_safe(selection.get("provider_payload") or {})
    market = selection.get("market", "")
    return {
        "match": selection.get("match", ""),
        "market": market,
        "submitted_market": market,
        "status": "queued",
        "verdict": "",
        "message": "Waiting for analysis.",
        "provider": selection.get("provider", ""),
        "provider_payload": provider_payload,
    }


def initialize_slip_selection_progress_rows(review, selections, *, defaults_builder):
    review.selections.all().delete()
    rows = []
    for index, selection in enumerate(selections, start=1):
        payload = initial_slip_selection_payload(selection)
        defaults = defaults_builder(payload)
        rows.append(SlipSelection(review=review, order=index, **defaults))
    if rows:
        SlipSelection.objects.bulk_create(rows, batch_size=100)


def persist_slip_selection_progress_result(review, index, result, *, defaults_builder):
    defaults = defaults_builder(result)
    updated = SlipSelection.objects.filter(review=review, order=index + 1).update(**defaults)
    if not updated:
        SlipSelection.objects.create(review=review, order=index + 1, **defaults)


def replace_slip_selection_analysis_rows(review, results, *, defaults_builder):
    review.selections.all().delete()
    rows = []
    for index, item in enumerate(results or [], start=1):
        rows.append(SlipSelection(review=review, order=index, **defaults_builder(item)))
    if rows:
        SlipSelection.objects.bulk_create(rows, batch_size=100)


def mark_slip_selection_analysing(review, index, selection):
    SlipSelection.objects.filter(review=review, order=index + 1).update(
        status="analysing",
        message="Analysing this selection.",
        analysis_payload={
            **initial_slip_selection_payload(selection or {}),
            "status": "analysing",
            "message": "Analysing this selection.",
        },
    )


def slip_selection_payload(selection):
    payload = dict(selection.analysis_payload or {})
    payload.setdefault("match", selection.submitted_match)
    payload.setdefault("submitted_market", selection.submitted_market)
    payload.setdefault("status", selection.status)
    payload.setdefault("verdict", selection.verdict)
    payload.setdefault("message", selection.message)
    return payload


def completed_slip_selection_payloads(review, *, selection_has_analysis):
    selections = list(getattr(review, "_prefetched_objects_cache", {}).get("selections") or [])
    if not selections:
        selections = list(review.selections.all().order_by("order", "id"))
    completed = []
    for selection in selections:
        payload = slip_selection_payload(selection)
        if selection_has_analysis(payload):
            completed.append(payload)
    return completed


def leg_results_from_persisted_slip_selections(review):
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


__all__ = [
    "completed_slip_selection_payloads",
    "initial_slip_selection_payload",
    "initialize_slip_selection_progress_rows",
    "leg_results_from_persisted_slip_selections",
    "mark_slip_selection_analysing",
    "optimized_leg_score",
    "persist_slip_selection_progress_result",
    "replace_slip_selection_analysis_rows",
    "selection_flagged_risky",
    "selection_card",
    "selection_has_analysis",
    "selection_is_unmatched",
    "selection_original_odds",
    "selection_strength_score",
    "selection_suggested_odds",
    "settlement_market_for",
    "slip_selection_defaults_from_analysis",
    "slip_selection_payload",
]
