"""Smart-randomize ticket helpers for slip review public payloads."""

import math

from apps.algo.markets.api import market_matches
from apps.algo.slip_review.public_formatting import (
    combined_odds,
    float_or_none,
    public_confidence_label,
    public_score,
    public_ticket_label,
    success_percent_display,
)

SMART_RANDOMIZE_MIN_CONFIDENCE = 55.0
SMART_RANDOMIZE_ELIGIBLE_VERDICTS = frozenset({"keep", "caution"})


def smart_randomize_option_values(eligible_count):
    count = int(eligible_count or 0)
    if count < 2:
        return []
    options = list(range(2, count, 2))
    return options or [2]


def smart_randomize_ranking_score(confidence, data_confidence):
    """
    Rank on the claim we are willing to stand behind, not model probability alone.

    This keeps smart-randomize selection aligned with the review verdict by ranking
    on the weaker of model probability and data confidence when both exist.
    """
    confidence = float_or_none(confidence)
    if confidence is None:
        return None
    data_confidence = float_or_none(data_confidence)
    return confidence if data_confidence is None else min(confidence, data_confidence)


def smart_randomize_pick_for_game(game):
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
                "confidence_label": public_confidence_label(user_confidence),
                "data_confidence_score": float_or_none(user_pick.get("data_confidence_score")),
                "ranking_score": smart_randomize_ranking_score(
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
                "confidence_label": public_confidence_label(recommended_confidence),
                "data_confidence_score": float_or_none(recommended_pick.get("data_confidence_score")),
                "ranking_score": smart_randomize_ranking_score(
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


def smart_randomize_candidates(public_payload):
    candidates = []
    excluded = []
    for game in (public_payload or {}).get("games") or []:
        pick = smart_randomize_pick_for_game(game)
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


def smart_randomize_summary(public_payload):
    candidates, _ = smart_randomize_candidates(public_payload)
    options = smart_randomize_option_values(len(candidates))
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


def smart_randomize_ticket(public_payload, requested_games):
    requested = int(requested_games or 0)
    candidates, excluded = smart_randomize_candidates(public_payload)
    options = smart_randomize_option_values(len(candidates))
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
        ticket_confidence = round(
            math.exp(sum(math.log(probability) for probability in probabilities) / len(probabilities)) * 100,
            1,
        )

    odds_values = [float_or_none(item.get("odds")) for item in selected]
    odds_complete = all(value and value > 1 for value in odds_values)
    selected_keys = {(item.get("id"), item.get("match"), item.get("market")) for item in selected}
    return {
        "review_id": (public_payload or {}).get("id"),
        "requested_games": requested,
        "available_options": options,
        "ticket": {
            "total_games": len(selected),
            "confidence_score": public_score(ticket_confidence),
            "confidence_label": public_ticket_label(ticket_confidence),
            "estimated_success_percent": ticket_probability,
            "estimated_success_display": success_percent_display(ticket_probability),
            "estimated_odds": combined_odds(odds_values) if odds_complete else None,
            "odds_complete": odds_complete,
            "label": public_ticket_label(ticket_confidence),
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
                "confidence_score": public_score(item.get("confidence_score")),
                "confidence_label": public_confidence_label(item.get("confidence_score")),
                "data_confidence_score": public_score(item.get("data_confidence_score")),
                "changed_from_user_pick": bool(item.get("changed_from_user_pick")),
            }
            for item in selected
        ],
        "excluded": excluded + [
            {
                "id": item.get("id"),
                "match": item.get("match"),
                "market": item.get("market"),
                "confidence_score": public_score(item.get("confidence_score")),
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


def with_smart_randomize(public_payload):
    payload = dict(public_payload or {})
    if payload.get("status") in {"queued", "importing", "analysing"}:
        return payload
    payload["smart_randomize"] = smart_randomize_summary(payload)
    return payload


__all__ = [
    "SMART_RANDOMIZE_ELIGIBLE_VERDICTS",
    "SMART_RANDOMIZE_MIN_CONFIDENCE",
    "smart_randomize_candidates",
    "smart_randomize_option_values",
    "smart_randomize_pick_for_game",
    "smart_randomize_ranking_score",
    "smart_randomize_summary",
    "smart_randomize_ticket",
    "with_smart_randomize",
]
