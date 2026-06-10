from django.conf import settings


HARD_STOP_FLAGS = {
    "best_price_far_above_consensus",
    "below_dc12_value_threshold",
    "below_market_threshold",
    "draw_boundary_risk",
    "estimated_odds",
    "goal_line_boundary",
    "low_market_hit_rate",
    "market_loss_streak",
    "market_recent_losses",
    "market_recent_low_hit_rate",
    "market_suppressed",
    "negative_market_roi",
    "no_real_odds",
    "strategy_suppressed",
    "strategy_cooling",
    "team_news_heavy_absences",
    "thin_edge",
    "wide_odds_market",
}

BLOCKED_PICK_COUNTRIES = set()

BLOCKED_PICK_LEAGUES = set()


def _setting(name, default=None):
    return getattr(settings, "GRIND_ALGO", {}).get(name, default)


def _bool_setting(name, default=False):
    value = _setting(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float_setting(name, default):
    try:
        return float(_setting(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int_setting(name, default):
    try:
        return int(_setting(name, default))
    except (TypeError, ValueError):
        return int(default)


def _normalise_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _value(candidate, name, default=None):
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _reviewer_score(insights, reviewer_name):
    review = (insights.get("council_review") or {})
    for item in review.get("reviewers") or []:
        if item.get("reviewer") == reviewer_name:
            try:
                return float(item.get("score") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def assess_league_market_trust(league_stats=None, market_stats=None):
    league_stats = league_stats or {}
    market_stats = market_stats or {}
    min_sample = _int_setting("ALGO_LEAGUE_MARKET_MIN_SAMPLE", 8)
    league_count = int(league_stats.get("count") or 0)
    market_count = int(market_stats.get("count") or 0)
    league_hit_rate = float(league_stats.get("hit_rate") or 0)
    market_hit_rate = float(market_stats.get("hit_rate") or 0)
    league_roi = float(league_stats.get("roi_flat") or 0)
    market_roi = float(market_stats.get("roi_flat") or 0)
    league_state = str(league_stats.get("state") or "")
    market_state = str(market_stats.get("state") or "")

    reasons = []
    if league_count < min_sample:
        status = "probation"
        reasons.append("limited_league_market_sample")
    elif league_state == "suppressed" or league_hit_rate < 45 or league_roi < -8:
        status = "restricted"
        reasons.append("weak_league_market_record")
    elif league_state == "cooling" or league_hit_rate < 52 or league_roi < 0:
        status = "probation"
        reasons.append("league_market_under_watch")
    else:
        status = "trusted"

    if market_count >= min_sample:
        if market_state == "suppressed" or market_hit_rate < 45 or market_roi < -8:
            status = "restricted"
            reasons.append("weak_overall_market_record")
        elif status == "trusted" and (market_state == "cooling" or market_hit_rate < 52 or market_roi < 0):
            status = "probation"
            reasons.append("overall_market_under_watch")

    return {
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "league_sample": league_count,
        "market_sample": market_count,
        "league_hit_rate": league_hit_rate,
        "market_hit_rate": market_hit_rate,
        "league_roi": league_roi,
        "market_roi": market_roi,
    }


def assess_calibration_trust(band_stats=None):
    band_stats = band_stats or {}
    min_sample = _int_setting("ALGO_CONFIDENCE_BAND_MIN_SAMPLE", 20)
    count = int(band_stats.get("count") or 0)
    hit_rate = float(band_stats.get("hit_rate") or 0)
    roi_flat = float(band_stats.get("roi_flat") or 0)
    state = str(band_stats.get("state") or "")
    avg_confidence = float(band_stats.get("avg_confidence") or 0)

    reasons = []
    if count < min_sample:
        status = "probation"
        reasons.append("limited_confidence_band_sample")
    elif state == "suppressed" or hit_rate < 48 or roi_flat < -10:
        status = "restricted"
        reasons.append("weak_confidence_band_record")
    elif state == "cooling" or hit_rate < 55 or roi_flat < -2:
        status = "probation"
        reasons.append("confidence_band_under_watch")
    else:
        status = "trusted"

    return {
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "sample": count,
        "hit_rate": hit_rate,
        "roi": roi_flat,
        "avg_confidence": avg_confidence,
    }


def assess_recommendation(candidate):
    confidence = float(_value(candidate, "confidence", 0) or 0)
    ev = _value(candidate, "ev")
    ev = float(ev) if ev is not None else None
    odds_source = str(_value(candidate, "odds_source", "") or "").lower()
    league = _normalise_text(_value(candidate, "league", ""))
    country = _normalise_text(_value(candidate, "country", ""))
    risk_flags = {str(flag) for flag in (_value(candidate, "risk_flags", []) or [])}
    insights = _value(candidate, "insights", {}) or {}
    league_trust = insights.get("league_trust") or {}
    calibration_trust = insights.get("calibration_trust") or {}
    trust_status = league_trust.get("status") or ""
    calibration_status = calibration_trust.get("status") or ""
    market_fit_score = _reviewer_score(insights, "market_fit")
    exceptional_fixture_fit = market_fit_score >= 85
    eligible = bool(_value(candidate, "eligible", False))
    min_confidence = _float_setting("ALGO_PUBLISH_MIN_CONFIDENCE", 70)
    min_ev = _float_setting("ALGO_PUBLISH_MIN_EV", 0.03)
    probation_confidence_extra = _float_setting("ALGO_PROBATION_CONFIDENCE_EXTRA", 5)
    probation_ev_extra = _float_setting("ALGO_PROBATION_EV_EXTRA", 0.03)
    calibration_confidence_extra = _float_setting("ALGO_CALIBRATION_CONFIDENCE_EXTRA", 3)
    calibration_ev_extra = _float_setting("ALGO_CALIBRATION_EV_EXTRA", 0.02)
    allow_wild_cards = _bool_setting("ALGO_PUBLISH_WILD_CARDS", False)

    reasons = []
    if country and country in BLOCKED_PICK_COUNTRIES:
        reasons.append("blocked_country")
    if league and any(item in league for item in BLOCKED_PICK_LEAGUES):
        reasons.append("blocked_league")
    if not eligible and not exceptional_fixture_fit:
        reasons.append("below_publish_gate")
    elif not eligible:
        if confidence < min_confidence + 8:
            reasons.append("exceptional_fit_needs_higher_confidence")
        if ev is None or ev < min_ev + 0.06:
            reasons.append("exceptional_fit_needs_stronger_ev")
    hard_flags = set(risk_flags & HARD_STOP_FLAGS)
    if exceptional_fixture_fit:
        hard_flags -= {
            "low_market_hit_rate",
            "market_recent_low_hit_rate",
            "market_suppressed",
            "negative_market_roi",
            "strategy_suppressed",
            "strategy_cooling",
        }
    reasons.extend(sorted(hard_flags))
    if odds_source == "estimated":
        reasons.append("estimated_odds")
    if ev is None:
        reasons.append("unpriced_market")
    elif ev < min_ev:
        reasons.append("insufficient_expected_value")
    if confidence < min_confidence:
        reasons.append("below_publish_confidence")
    if confidence < 70 and not allow_wild_cards:
        reasons.append("wild_cards_paused")
    if trust_status == "restricted" and not exceptional_fixture_fit:
        reasons.extend(league_trust.get("reasons") or ["league_market_restricted"])
    elif trust_status == "restricted":
        if confidence < min_confidence + 8:
            reasons.append("restricted_market_needs_exceptional_confidence")
        if ev is None or ev < min_ev + 0.06:
            reasons.append("restricted_market_needs_exceptional_ev")
    elif trust_status == "probation":
        if confidence < min_confidence + probation_confidence_extra:
            reasons.append("probation_needs_higher_confidence")
        if ev is None or ev < min_ev + probation_ev_extra:
            reasons.append("probation_needs_stronger_ev")
    if calibration_status == "restricted":
        reasons.extend(calibration_trust.get("reasons") or ["confidence_band_restricted"])
    elif calibration_status == "probation":
        if confidence < min_confidence + calibration_confidence_extra:
            reasons.append("calibration_needs_higher_confidence")
        if ev is None or ev < min_ev + calibration_ev_extra:
            reasons.append("calibration_needs_stronger_ev")

    reasons = list(dict.fromkeys(reasons))
    recommended = not reasons
    if recommended and confidence >= 80:
        status = "strong"
    elif recommended:
        status = "playable"
    elif eligible and confidence >= 60 and ev is not None and ev > 0:
        status = "watchlist"
    else:
        status = "no_edge"

    return {
        "recommended": recommended,
        "recommendation_status": status,
        "recommendation_reasons": reasons,
    }
