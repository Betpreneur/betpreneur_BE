from statistics import mean


APPROVE = "approve"
CAUTION = "caution"
REJECT = "reject"


SEVERE_RISK_FLAGS = {
    "estimated_odds",
    "no_real_odds",
    "negative_market_roi",
    "low_market_hit_rate",
    "market_loss_streak",
    "market_recent_losses",
    "market_suppressed",
    "strategy_suppressed",
    "team_news_heavy_absences",
}


MEDIUM_RISK_FLAGS = {
    "best_price_far_above_consensus",
    "draw_boundary_risk",
    "goal_line_boundary",
    "market_recent_low_hit_rate",
    "strategy_cooling",
    "team_news_unavailable",
    "thin_edge",
    "wide_odds_market",
}


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value, low=0, high=100):
    return max(low, min(high, round(value)))


def _verdict(score, reject=False):
    if reject or score < 50:
        return REJECT
    if score < 70:
        return CAUTION
    return APPROVE


def _review(name, score, reasons=None, veto=False):
    score = _clamp(score)
    return {
        "reviewer": name,
        "score": score,
        "verdict": _verdict(score, reject=veto),
        "veto": bool(veto),
        "reasons": list(dict.fromkeys(reasons or [])),
    }


def value_reviewer(candidate):
    ev = candidate.get("ev")
    odds_source = str(candidate.get("odds_source") or "").lower()
    odds_meta = candidate.get("odds_meta") or {}
    score = 50
    reasons = []
    veto = False

    if ev is None:
        reasons.append("missing_expected_value")
        veto = True
    else:
        ev_float = _float(ev)
        score += min(35, ev_float * 350)
        if ev_float <= 0:
            reasons.append("non_positive_expected_value")
            veto = True
        elif ev_float < 0.03:
            reasons.append("thin_expected_value")

    if odds_source == "estimated":
        reasons.append("estimated_odds")
        score -= 25
        veto = True

    bookmaker_count = int(odds_meta.get("bookmaker_count") or 0)
    if bookmaker_count >= 3:
        score += 5
    spread_pct = _float(odds_meta.get("spread_pct"))
    best_vs_average = _float(odds_meta.get("best_vs_average_pct"))
    if spread_pct >= 18:
        reasons.append("wide_bookmaker_spread")
        score -= 10
    if best_vs_average >= 12:
        reasons.append("best_price_far_above_consensus")
        score -= 8

    return _review("value", score, reasons, veto=veto)


def risk_reviewer(candidate):
    flags = set(candidate.get("risk_flags") or [])
    severe = sorted(flags & SEVERE_RISK_FLAGS)
    medium = sorted(flags & MEDIUM_RISK_FLAGS)
    context_flags = sorted(flag for flag in flags if str(flag).startswith("context:"))
    score = 88 - (len(severe) * 22) - (len(medium) * 9) - (len(context_flags) * 4)
    reasons = severe + medium + context_flags
    return _review("risk", score, reasons, veto=bool(severe))


def league_market_reviewer(candidate):
    trust = ((candidate.get("insights") or {}).get("league_trust") or {})
    status = trust.get("status") or ""
    if status == "restricted":
        return _review("league_market", 35, trust.get("reasons") or ["league_market_restricted"], veto=True)
    if status == "probation":
        return _review("league_market", 62, trust.get("reasons") or ["league_market_probation"])
    score = 76
    hit_rate = _float(trust.get("league_hit_rate"))
    roi = _float(trust.get("league_roi"))
    if hit_rate:
        score += min(10, max(-10, hit_rate - 55))
    if roi:
        score += min(8, max(-8, roi / 2))
    return _review("league_market", score, ["league_market_trusted"])


def market_history_reviewer(candidate):
    trust = ((candidate.get("insights") or {}).get("league_trust") or {})
    status = trust.get("status") or ""
    market_hit_rate = _float(trust.get("market_hit_rate"))
    market_roi = _float(trust.get("market_roi"))
    market_sample = int(trust.get("market_sample") or 0)
    score = 66
    reasons = []

    if status == "restricted":
        return _review("market_history", 38, trust.get("reasons") or ["market_history_restricted"], veto=True)
    if market_sample < 8:
        reasons.append("limited_market_history")
        score -= 8
    if market_hit_rate:
        score += min(14, max(-14, market_hit_rate - 55))
    if market_roi:
        score += min(10, max(-10, market_roi / 2))
    if market_hit_rate and market_hit_rate < 45:
        reasons.append("weak_market_hit_rate")
    if market_roi < -8:
        reasons.append("negative_market_roi")

    return _review("market_history", score, reasons, veto=market_hit_rate < 40 or market_roi < -15)


def calibration_reviewer(candidate):
    calibration = ((candidate.get("insights") or {}).get("calibration_trust") or {})
    status = calibration.get("status") or ""
    raw_confidence = _float(candidate.get("confidence"))
    avg_confidence = _float(calibration.get("avg_confidence"), raw_confidence)
    hit_rate = _float(calibration.get("hit_rate"))
    score = raw_confidence
    reasons = []

    if status == "restricted":
        return _review("calibration", 40, calibration.get("reasons") or ["confidence_band_restricted"], veto=True)
    if status == "probation":
        reasons.extend(calibration.get("reasons") or ["confidence_band_probation"])
        score -= 8
    if hit_rate:
        score = mean([score, hit_rate])
        if raw_confidence - hit_rate >= 8:
            reasons.append("historical_band_under_raw_confidence")
    elif avg_confidence:
        score = mean([score, avg_confidence])

    return _review("calibration", score, reasons)


def market_behaviour_reviewer(candidate):
    odds_meta = candidate.get("odds_meta") or {}
    flags = set(candidate.get("risk_flags") or [])
    score = 72
    reasons = []
    if "wide_odds_market" in flags or _float(odds_meta.get("spread_pct")) >= 18:
        reasons.append("wide_bookmaker_spread")
        score -= 12
    if "best_price_far_above_consensus" in flags or _float(odds_meta.get("best_vs_average_pct")) >= 12:
        reasons.append("best_price_far_above_consensus")
        score -= 10
    if int(odds_meta.get("bookmaker_count") or 0) >= 3:
        score += 5
    return _review("market_behaviour", score, reasons)


def council_review(candidate):
    reviewers = [
        value_reviewer(candidate),
        risk_reviewer(candidate),
        league_market_reviewer(candidate),
        market_history_reviewer(candidate),
        calibration_reviewer(candidate),
        market_behaviour_reviewer(candidate),
    ]
    scores = [item["score"] for item in reviewers]
    vetoes = [item for item in reviewers if item["veto"] or item["verdict"] == REJECT]
    consensus_score = _clamp(mean(scores) if scores else 0)
    disagreement_score = _clamp((max(scores) - min(scores)) if scores else 0)
    raw_confidence = _float(candidate.get("confidence"))
    final_confidence = _clamp(mean([raw_confidence, consensus_score]) - max(0, disagreement_score - 25) * 0.25)

    if vetoes:
        decision = REJECT
    elif consensus_score >= 75 and disagreement_score <= 25 and final_confidence >= 70:
        decision = APPROVE
    elif consensus_score >= 60 and disagreement_score <= 40 and final_confidence >= 60:
        decision = CAUTION
    else:
        decision = REJECT

    if decision == REJECT:
        tier = ""
    elif final_confidence >= 80 and consensus_score >= 75 and disagreement_score <= 25:
        tier = "banker"
    elif final_confidence >= 70:
        tier = "value_gem"
    elif final_confidence >= 60:
        tier = "wild_card"
    else:
        tier = ""

    reasons = []
    for item in reviewers:
        reasons.extend(item.get("reasons") or [])
    if vetoes:
        reasons.extend(f"{item['reviewer']}_veto" for item in vetoes)
    if disagreement_score > 40:
        reasons.append("high_reviewer_disagreement")

    return {
        "raw_confidence": round(raw_confidence, 2),
        "final_confidence": final_confidence,
        "consensus_score": consensus_score,
        "disagreement_score": disagreement_score,
        "decision": decision,
        "tier": tier,
        "reviewers": reviewers,
        "reasons": list(dict.fromkeys(reasons)),
    }
