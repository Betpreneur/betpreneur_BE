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


def plural(value, singular, plural_text=None):
    return singular if int(value or 0) == 1 else (plural_text or f"{singular}s")


def ticket_health_summary(score, risk_level, remove_count, replace_count, caution_count, unverified_count):
    weak_count = remove_count + replace_count
    if risk_level == "unknown":
        return (
            f"None of these {unverified_count} {plural(unverified_count, 'pick')} could be analysed yet, "
            "so this ticket has not been assessed."
        )
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {plural(replace_count, 'pick')} should be replaced")
    if remove_count:
        parts.append(f"{remove_count} {plural(remove_count, 'pick')} should be avoided")
    if caution_count:
        parts.append(f"{caution_count} {plural(caution_count, 'pick')} need caution")
    if unverified_count:
        parts.append(f"{unverified_count} {plural(unverified_count, 'pick')} need review")
    if parts:
        return "This ticket is risky. " + ", ".join(parts) + "."
    if risk_level == "high":
        return f"This ticket is risky. {weak_count or caution_count} pick(s) need attention."
    if risk_level == "medium":
        return f"This ticket is playable, but {replace_count + caution_count} leg(s) need attention."
    return "This ticket looks healthy from the current Match Checker analysis."


def ticket_health_label(score):
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


def pick_confidence_label(score):
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


def risk_level_from_confidence(score):
    score = float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 70:
        return "medium"
    return "low"


def bettor_verdict_from_confidence(score):
    score = float_or_none(score)
    if score is None:
        return "needs_review"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "playable"
    return "high_risk"


def bettor_verdict_label(code):
    return {
        "strong": "Strong pick",
        "playable": "Playable",
        "high_risk": "High risk",
        "needs_review": "Needs review",
    }.get(str(code or ""), "Needs review")


def bettor_pick_message(verdict_code, *, market="", action=""):
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


def ticket_issue_text(replace_count=0, remove_count=0, caution_count=0, unverified_count=0):
    parts = []
    if replace_count:
        parts.append(f"{replace_count} {plural(replace_count, 'pick')} to replace")
    if remove_count:
        parts.append(f"{remove_count} {plural(remove_count, 'pick')} to avoid")
    if caution_count:
        parts.append(f"{caution_count} {plural(caution_count, 'pick')} to treat carefully")
    if unverified_count:
        parts.append(f"{unverified_count} {plural(unverified_count, 'pick')} needing review")
    return ", ".join(parts)


def public_market_meaning(market_name, *, describe_market, market_matches, market_options):
    descriptor = describe_market(market_name)
    for option in market_options:
        if market_matches(market_name, option.get("value")):
            return option.get("meaning") or descriptor.canonical
    if descriptor.family == "unknown":
        return ""
    return descriptor.canonical


def public_market_pick(
    market,
    *,
    fallback_market="",
    fallback_odds=None,
    describe_market,
    market_matches,
    market_options,
    match_checker_status,
):
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
        "meaning": (market or {}).get("meaning")
        or public_market_meaning(
            market_name,
            describe_market=describe_market,
            market_matches=market_matches,
            market_options=market_options,
        ),
        "confidence": (market or {}).get("final_confidence") or (market or {}).get("confidence"),
        "confidence_score": score,
        "confidence_label": public_confidence_label(score),
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


def with_bettor_view(card):
    card = card or {}
    user_pick = card.get("user_pick") or {}
    ai_pick = card.get("ai_pick") or {}
    verdict_code = (card.get("verdict") or {}).get("code")
    action = "replace" if ai_pick.get("available") and verdict_code == "replace" else (
        "keep" if verdict_code in {"keep", "caution"} else "review"
    )
    user_verdict = bettor_verdict_from_confidence(user_pick.get("confidence_score"))
    user_pick.update(
        {
            "verdict": "replace" if action == "replace" else user_verdict,
            "verdict_label": bettor_verdict_label(user_verdict),
            "message": bettor_pick_message(user_verdict, market=user_pick.get("market"), action=action),
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


def with_leg_risk(card, leg):
    """Attach the calibrated risk view of a leg to its public card."""
    card = card or {}
    tier_label = "High risk" if leg.tier == "avoid" else leg.tier_label
    probability_percent = round_percent(leg.probability)
    repair_probability_percent = round_percent(leg.repair_probability)
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
        # `capped_by_data_quality` means "the claim was held back", not "the number
        # was truncated" -- the probability below is reported as modelled.
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
        else (
            leg.data_confidence_percent
            if leg.data_confidence_percent is not None
            else (your_pick.get("confidence_cap") or your_pick.get("confidence"))
        )
    )
    offered_probability = implied_probability_from_odds(your_pick.get("odds"))
    price_check = card.get("price_check") or {}
    reference_probability = implied_probability_from_odds(price_check.get("reference_odds"))
    disagreement_gap = probability_gap(leg.probability, reference_probability)
    pick_confidence_score = probability_percent
    your_pick.update(
        {
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "fair_odds": fair_odds(leg.probability),
            "confidence_score": pick_confidence_score,
            "confidence_label": pick_confidence_label(pick_confidence_score),
            "data_confidence_score": data_confidence_score,
            "decision_score": your_pick.get("decision_score", your_pick.get("score")),
            "risk_score": round((1 - leg.probability) * 100, 1) if leg.probability is not None else None,
            "risk_level": risk_level_from_confidence(pick_confidence_score),
            "market_implied_probability": offered_probability,
            "market_implied_probability_percent": round_percent(offered_probability),
            "value_rating": value_rating(leg.probability, your_pick.get("odds")),
        }
    )
    card["your_pick"] = your_pick
    ai_same_as_user = bool(card.get("ai_pick")) and leg.repair_probability is None
    card["user_pick"] = {
        "market": your_pick.get("market"),
        "odds": your_pick.get("odds"),
        "confidence_score": pick_confidence_score,
        "confidence_label": pick_confidence_label(pick_confidence_score),
        "risk_level": risk_level_from_confidence(pick_confidence_score),
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
                "fair_odds": fair_odds(ai_probability),
                "available": True,
                "confidence_score": ai_confidence_score,
                "confidence_label": pick_confidence_label(ai_confidence_score),
                "data_confidence_score": ai_data_confidence_score,
                "decision_score": card["ai_pick"].get("decision_score", card["ai_pick"].get("score")),
                "risk_level": risk_level_from_confidence(ai_confidence_score),
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
            "implied_probability_percent": round_percent(reference_probability),
            "model_probability": leg.probability,
            "model_probability_percent": probability_percent,
            "probability_gap_points": disagreement_gap,
            "disagreement_level": gap_level(disagreement_gap),
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


def with_explanation(card, *, explain_leg):
    """Attach a plain-language explanation built only from values the model produced."""
    card = card or {}
    card["explanation"] = explain_leg(card).to_dict()
    return card


def leg_state_counts(items, *, assess_leg):
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


def public_ticket_killers(ticket_risk):
    selections = []
    for killer in ticket_risk.killers:
        copy = dict(killer)
        if copy.get("tier") == "avoid":
            copy["tier_label"] = "High risk"
        selections.append(copy)
    return selections


def ticket_risk_level_from_score(score):
    score = float_or_none(score)
    if score is None:
        return "unknown"
    if score < 55:
        return "high"
    if score < 65:
        return "medium"
    return "low"


def public_risk_label(value):
    return {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "unknown": "Unknown",
    }.get(str(value or "").lower(), "Unknown")


def public_action_label(verdict):
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


def public_verdict_message(verdict, submitted_market=None, pick_status=None):
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


def public_verdict_object(verdict, submitted_market=None, pick_status=None):
    code = str(verdict or "review").lower()
    return {
        "code": code,
        "label": public_action_label(code),
        "message": public_verdict_message(code, submitted_market=submitted_market, pick_status=pick_status),
    }


def public_recommendation_strength(pick):
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


def price_reason_code(price_check):
    """The reason code for how the user's price compares to the reference, if known."""
    if not (price_check or {}).get("available"):
        return ""
    return {
        "positive_edge": "price_edge",
        "near_reference": "price_near_reference",
        "short_price": "price_short",
    }.get(price_check.get("status"), "price_reference")


def public_price_check_from_card(card):
    evidence = (card or {}).get("evidence") or {}
    statpal_evidence = evidence.get("statpal") or {}
    odds_value = evidence.get("odds_value") or statpal_evidence.get("odds_value") or {}
    if not odds_value:
        return {
            "available": False,
            "status": "unknown",
            "message": "No StatPal reference price was available for this selection.",
        }

    edge = float_or_none(odds_value.get("value_edge_pct"))
    offered = float_or_none(odds_value.get("offered_odds"))
    reference = float_or_none(odds_value.get("statpal_reference_odds"))
    reference_min = float_or_none(odds_value.get("statpal_reference_min_odds"))
    reference_max = float_or_none(odds_value.get("statpal_reference_max_odds"))
    reference_spread = float_or_none(odds_value.get("statpal_reference_spread_pct"))
    bookmaker_count = float_or_none(odds_value.get("statpal_reference_bookmaker_count"))
    reliability = odds_value.get("reference_reliability") or ""
    market = odds_value.get("matched_market") or ""
    outcome = odds_value.get("matched_outcome") or ""
    bookmaker = odds_value.get("bookmaker") or ""
    reliability_note = ""
    if reliability == "thin":
        reliability_note = " The reference is based on one bookmaker, so treat it as a light signal."
    elif reliability == "wide":
        reliability_note = " Bookmaker prices disagree, so treat the edge cautiously."
    elif reliability == "volatile":
        reliability_note = " Bookmaker prices disagree sharply, so the edge is unreliable."
    if edge is None:
        status = "matched"
        message = "A StatPal reference price was matched for this selection."
    elif edge >= 5:
        status = "positive_edge"
        message = f"Your price is about {round(edge, 1)}% better than the StatPal reference."
    elif edge <= -5:
        status = "short_price"
        message = f"Your price is about {abs(round(edge, 1))}% shorter than the StatPal reference."
    else:
        status = "near_reference"
        message = "Your price is close to the StatPal reference."
    message = f"{message}{reliability_note}"
    return {
        "available": True,
        "status": status,
        "message": message,
        "offered_odds": offered,
        "reference_odds": reference,
        "reference_min_odds": reference_min,
        "reference_max_odds": reference_max,
        "reference_spread_percent": reference_spread,
        "reference_bookmaker_count": int(bookmaker_count) if bookmaker_count is not None else None,
        "reference_method": odds_value.get("reference_method") or "",
        "reference_reliability": reliability,
        "edge_percent": round(edge, 1) if edge is not None else None,
        "matched_market": market,
        "matched_outcome": outcome,
        "bookmaker": bookmaker,
    }


def public_why_from_card(card):
    why = []
    codes = []
    evidence = (card or {}).get("evidence") or {}
    alternative = (card or {}).get("alternative") or {}
    alt_evidence = alternative.get("evidence") or {}
    historical_accuracy = alt_evidence.get("historical_accuracy") or evidence.get("historical_accuracy")
    sample_size = alt_evidence.get("sample_size") or evidence.get("sample_size")
    roi = (
        alt_evidence.get("similar_market_roi")
        if alt_evidence.get("similar_market_roi") is not None
        else evidence.get("similar_market_roi")
    )
    league_trust = alt_evidence.get("league_trust") or evidence.get("league_trust")
    if historical_accuracy is not None:
        sample_text = f" across {int(sample_size)} tracked results" if sample_size else ""
        why.append(f"Similar selections won {round(float(historical_accuracy), 1)}%{sample_text}.")
        codes.append("historical_accuracy")
    if sample_size:
        codes.append("historical_sample")
    if roi is not None:
        why.append(f"Similar markets have returned {round(float(roi), 1)}% ROI.")
        codes.append("market_roi")
    if league_trust == "trusted":
        why.append("This market has reliable history in similar league conditions.")
        codes.append("trusted_league_market")
    elif league_trust in {"probation", "restricted"}:
        why.append("There is limited competition-specific history, so some caution remains.")
        codes.append("limited_league_sample")
    price_code = price_reason_code(public_price_check_from_card(card))
    if price_code:
        codes.append(price_code)
    if alternative.get("reason"):
        why.append(alternative["reason"])
        codes.append("better_alternative")
    statpal_message = ((card or {}).get("statpal_advisory") or {}).get("message")
    if statpal_message:
        why.append(statpal_message)
        codes.append("statpal_advisory")
    if not why and (card or {}).get("message"):
        why.append(card["message"])
        codes.append("model_message")
    return why[:4], list(dict.fromkeys(codes))[:6]


def public_selection_risk(verdict, pick):
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
        return "unknown"
    return "low"


def public_score(value):
    value = float_or_none(value)
    return int(round(value)) if value is not None else None


def public_confidence_label(score):
    return pick_confidence_label(score)


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
    "bettor_pick_message",
    "bettor_verdict_from_confidence",
    "bettor_verdict_label",
    "combined_odds",
    "fair_odds",
    "float_or_none",
    "gap_level",
    "implied_probability_from_odds",
    "leg_state_counts",
    "pick_confidence_label",
    "plural",
    "probability_gap",
    "price_reason_code",
    "public_action_label",
    "public_confidence_label",
    "public_market_meaning",
    "public_market_pick",
    "public_price_check_from_card",
    "public_recommendation_strength",
    "public_risk_label",
    "public_selection_risk",
    "public_score",
    "public_ticket_label",
    "public_ticket_killers",
    "public_verdict_message",
    "public_verdict_object",
    "public_why_from_card",
    "round_percent",
    "risk_level_from_confidence",
    "success_percent_display",
    "ticket_health_label",
    "ticket_health_summary",
    "ticket_issue_text",
    "ticket_risk_level_from_score",
    "value_rating",
    "with_bettor_view",
    "with_explanation",
    "with_leg_risk",
]
