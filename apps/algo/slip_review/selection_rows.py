"""Slip-review selection row persistence helpers."""

from apps.algo.models import SlipSelection
from apps.algo.slip_review.lifecycle import json_safe


def slip_selection_defaults_from_analysis(
    item,
    *,
    settlement_market_for,
    decimal_or_none,
    selection_original_odds,
    float_or_none,
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


def selection_flagged_risky(item):
    """Whether this leg was called out pre-kickoff, frozen at analysis time."""
    return (item or {}).get("verdict") in {"remove", "replace", "caution"}


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
    "persist_slip_selection_progress_result",
    "replace_slip_selection_analysis_rows",
    "selection_flagged_risky",
    "slip_selection_defaults_from_analysis",
    "slip_selection_payload",
]
