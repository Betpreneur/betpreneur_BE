"""Public slip-review response payload assembly."""

from apps.algo.models import SlipReview
from apps.algo.slip_review.lifecycle import slip_review_progress
from apps.algo.slip_review.payloads import public_slip_review_progress, public_slip_review_status
from apps.algo.slip_review.selection_rows import completed_slip_selection_payloads, slip_selection_payload


def active_slip_review_public_payload(
    review,
    *,
    summary,
    progress,
    latest_event_id,
    selection_has_analysis,
    manual_review_summary,
    build_bettor_public_payload,
    with_smart_randomize,
):
    completed_payloads = completed_slip_selection_payloads(
        review,
        selection_has_analysis=selection_has_analysis,
    )
    if not completed_payloads:
        return {
            "id": review.id,
            "source": review.source,
            "status": public_slip_review_status(review.status),
            "created_at": review.created_at,
            "updated_at": review.updated_at,
            "progress": public_slip_review_progress(progress),
            "latest_event_id": latest_event_id,
        }

    partial_summary = manual_review_summary(completed_payloads)
    payload = build_bettor_public_payload(
        review,
        partial_summary.get("public") or {},
        enhance=False,
    )
    payload["status"] = public_slip_review_status(review.status)
    payload["progress"] = public_slip_review_progress(progress)
    payload["latest_event_id"] = latest_event_id
    payload["created_at"] = review.created_at
    payload["updated_at"] = review.updated_at
    payload["partial"] = True
    payload["completed_games"] = len(completed_payloads)
    total = int((progress or {}).get("total") or 0)
    if total:
        payload.setdefault("ticket", {})["total_games"] = total
    return with_smart_randomize(payload)


def compact_ai_pick_from_selection(selection, *, public_score):
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
        "confidence_score": public_score(confidence),
        "action": action,
    }


def slip_review_booking_code(review):
    payload = review.submitted_payload or {}
    for key in ("provider_code", "share_code", "booking_code", "code"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def compact_slip_review_list_payload(
    review,
    *,
    include_picks=True,
    pick_limit=None,
    use_summary=True,
    build_bettor_public_payload,
    public_score,
):
    summary = (review.summary or {}) if use_summary else {}
    public_payload = summary.get("bettor_public") or {}
    if not public_payload and summary.get("public"):
        public_payload = build_bettor_public_payload(review, summary.get("public") or {})

    ticket = public_payload.get("ticket") or {}
    number_of_games = (
        ticket.get("total_games")
        or getattr(review, "selection_count", None)
        or summary.get("count")
        or summary.get("total_legs")
    )
    games = public_payload.get("games") or []
    if not number_of_games:
        number_of_games = len(games)

    picks = []
    truncated = False
    if include_picks and games:
        selected_games = games
        if pick_limit is not None:
            selected_games = games[:pick_limit]
            truncated = len(games) > len(selected_games)
        picks = [
            {
                "match": game.get("match"),
                "your_pick": {
                    "market": (game.get("user_pick") or {}).get("market"),
                    "odds": (game.get("user_pick") or {}).get("odds"),
                    "confidence_score": (game.get("user_pick") or {}).get("confidence_score"),
                    "verdict": (game.get("user_pick") or {}).get("verdict"),
                },
                "ai_pick": {
                    "market": ((game.get("recommendation") or {}).get("pick") or {}).get("market"),
                    "confidence_score": ((game.get("recommendation") or {}).get("pick") or {}).get("confidence_score"),
                    "action": (game.get("recommendation") or {}).get("action"),
                },
            }
            for game in selected_games
        ]
    elif include_picks:
        selections = list(getattr(review, "preview_selections", []))
        if not selections:
            selections_qs = review.selections.all().order_by("order", "id")
            if pick_limit is not None:
                selections_qs = selections_qs[:pick_limit]
            selections = list(selections_qs)
        elif pick_limit is not None:
            selections = selections[:pick_limit]
        if not number_of_games:
            number_of_games = len(selections)
        if pick_limit is not None and number_of_games:
            truncated = int(number_of_games) > len(selections)
        picks = [
            {
                "match": selection.submitted_match,
                "your_pick": {
                    "market": selection.submitted_market,
                    "odds": float(selection.odds) if selection.odds is not None else None,
                    "confidence_score": public_score(selection.advisory_score),
                    "verdict": selection.verdict or selection.status or "review",
                },
                "ai_pick": compact_ai_pick_from_selection(selection, public_score=public_score),
            }
            for selection in selections
        ]

    payload = {
        "id": review.id,
        "number_of_games": int(number_of_games or 0),
        "status": public_slip_review_status(review.status),
        "source": review.source,
        "booking_code": slip_review_booking_code(review),
        "title": review.title,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
        "picks": picks,
    }
    if include_picks:
        payload["picks_returned"] = len(picks)
        payload["has_more_picks"] = truncated
    return payload


def slip_review_payload(
    review,
    *,
    include_selections=True,
    public_only=False,
    api_response_payload,
    build_bettor_public_payload,
    with_smart_randomize,
    selection_has_analysis,
    manual_review_summary,
):
    summary = review.summary or {}
    public_payload = summary.get("public") or (summary.get("intelligence") or {}).get("public", {})
    latest_event_id = (
        review.events.order_by("-id").values_list("id", flat=True).first()
        if hasattr(review, "events")
        else None
    )
    if public_only:
        if review.status in {
            SlipReview.Status.QUEUED,
            SlipReview.Status.IMPORTING,
            SlipReview.Status.ANALYSING,
        }:
            progress = (summary or {}).get("progress") or slip_review_progress(
                phase=review.status,
                message=f"Slip review is {review.status}.",
            )
            return api_response_payload(
                active_slip_review_public_payload(
                    review,
                    summary=summary,
                    progress=progress,
                    latest_event_id=latest_event_id,
                    selection_has_analysis=selection_has_analysis,
                    manual_review_summary=manual_review_summary,
                    build_bettor_public_payload=build_bettor_public_payload,
                    with_smart_randomize=with_smart_randomize,
                )
            )
        bettor_payload = summary.get("bettor_public") or build_bettor_public_payload(
            review,
            public_payload,
            enhance=False,
        )
        if bettor_payload.get("status") != review.status:
            bettor_payload = build_bettor_public_payload(
                review,
                public_payload,
                enhance=False,
            )
        bettor_payload = {**bettor_payload, "status": public_slip_review_status(bettor_payload.get("status"))}
        bettor_payload = with_smart_randomize(bettor_payload)
        return api_response_payload(bettor_payload)
    payload = {
        "id": review.id,
        "source": review.source,
        "status": public_slip_review_status(review.status),
        "title": review.title,
        "summary": summary,
        "public": public_payload,
        "intelligence": summary.get("intelligence", {}),
        "created_at": review.created_at,
        "updated_at": review.updated_at,
        "latest_event_id": latest_event_id,
    }
    if include_selections:
        payload["selections"] = [
            slip_selection_payload(selection)
            for selection in review.selections.all().order_by("order", "id")
        ]
    return api_response_payload(payload)


__all__ = [
    "active_slip_review_public_payload",
    "compact_ai_pick_from_selection",
    "compact_slip_review_list_payload",
    "slip_review_booking_code",
    "slip_review_payload",
]
