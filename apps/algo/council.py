from statistics import mean
import re


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
    "h2h_draw_pressure",
    "h2h_tight_draw_warning",
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


def _form_rate(form, key):
    form = form or {}
    games = int(form.get("games") or 0)
    if not games:
        return 0.0
    return _float(form.get(key)) / 100 if str(key).endswith("_rate") else _float(form.get(key)) / games


def _team_goal_average(form):
    form = form or {}
    return _float(form.get("avg_scored")) + _float(form.get("avg_conceded"))


def _corner_line(market):
    match = re.search(r"(\d+(?:\.\d+)?)", str(market or ""))
    return _float(match.group(1)) if match else 0.0


def _scoreline_profile(candidate):
    context = candidate.get("fixture_context") or {}
    return context.get("scoreline_profile") or {}


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
    fit = market_fit_reviewer(candidate)
    severe_flags = set(SEVERE_RISK_FLAGS)
    if fit["score"] >= 85:
        severe_flags -= {
            "low_market_hit_rate",
            "market_suppressed",
            "negative_market_roi",
            "strategy_suppressed",
        }
    severe = sorted(flags & severe_flags)
    medium = sorted(flags & MEDIUM_RISK_FLAGS)
    context_flags = sorted(flag for flag in flags if str(flag).startswith("context:"))
    score = 88 - (len(severe) * 22) - (len(medium) * 9) - (len(context_flags) * 4)
    reasons = severe + medium + context_flags
    return _review("risk", score, reasons, veto=bool(severe))


def market_fit_reviewer(candidate):
    market = str(candidate.get("market") or "")
    home = candidate.get("home_recent_form") or {}
    away = candidate.get("away_recent_form") or {}
    context = candidate.get("fixture_context") or {}
    corner_profile = candidate.get("corner_profile") or {}
    goal_model = context.get("goal_model") or {}
    h2h = context.get("h2h") or {}
    expected_total = _float(goal_model.get("expected_total"))
    draw_confidence = _float(goal_model.get("draw_confidence"))
    h2h_games = int(h2h.get("games") or 0)
    h2h_draw_rate = _float(h2h.get("draws")) / h2h_games if h2h_games else 0.0
    h2h_avg_goals = _float(h2h.get("avg_goals"))
    home_games = int(home.get("games") or 0)
    away_games = int(away.get("games") or 0)
    home_draw_rate = _form_rate(home, "draws")
    away_draw_rate = _form_rate(away, "draws")
    home_over25 = _float(home.get("over25_rate"))
    away_over25 = _float(away.get("over25_rate"))
    avg_goal_load = mean([_team_goal_average(home), _team_goal_average(away)])
    score = 58
    reasons = []
    fixture_veto = False

    if min(home_games, away_games) < 5:
        score -= 8
        reasons.append("limited_fixture_form_sample")

    if market == "DC: 12":
        score = 48
        if expected_total >= 2.25:
            score += 12
            reasons.append("decisive_goal_profile")
        if draw_confidence and draw_confidence <= 24:
            score += 18
            reasons.append("low_draw_pressure")
        elif draw_confidence >= 30:
            score -= 22
            reasons.append("high_draw_pressure")
        if home_draw_rate + away_draw_rate <= 0.45:
            score += 10
            reasons.append("low_recent_draw_tendency")
        elif home_draw_rate + away_draw_rate >= 0.7:
            score -= 12
            reasons.append("draw_tendency_conflict")
        if expected_total and expected_total < 2.0:
            score -= 12
            reasons.append("tight_goal_profile_against_dc12")
        if h2h_games >= 6:
            if h2h_draw_rate >= 0.35:
                score -= 24
                reasons.append("h2h_draw_pressure_against_dc12")
                if h2h_avg_goals and h2h_avg_goals <= 1.7:
                    fixture_veto = True
            elif h2h_draw_rate >= 0.22:
                score -= 10
                reasons.append("h2h_draw_warning_against_dc12")
            elif h2h_draw_rate <= 0.12:
                score += 6
                reasons.append("low_h2h_draw_tendency")
            if h2h_avg_goals and h2h_avg_goals <= 1.7:
                score -= 12
                reasons.append("low_h2h_goal_profile_against_dc12")
            elif h2h_avg_goals and h2h_avg_goals <= 2.15 and h2h_draw_rate >= 0.2:
                score -= 6
                reasons.append("tight_h2h_profile_against_dc12")

    elif market.startswith("Under"):
        line = _corner_line(market)
        score = 54
        if line >= 3.5:
            if expected_total and expected_total <= 2.85:
                score += 18
                reasons.append("controlled_goal_projection")
            elif expected_total >= 3.15:
                score -= 18
                reasons.append("goal_projection_too_high_for_under")
            if avg_goal_load <= 3.0:
                score += 10
            if mean([home_over25, away_over25]) <= 45:
                score += 8
        elif line <= 2.5:
            if expected_total and expected_total <= 2.35:
                score += 18
                reasons.append("low_goal_projection")
            elif expected_total >= 2.65:
                score -= 16
                reasons.append("under_line_boundary")

    elif market.startswith("Over") or "BTTS" in market or market.startswith("GG"):
        line = _corner_line(market)
        score = 54
        if line <= 1.5:
            if expected_total >= 1.85:
                score += 18
                reasons.append("over15_goal_projection")
            elif expected_total and expected_total < 1.65:
                score -= 18
                reasons.append("low_goal_projection_against_over")
        elif line >= 2.5:
            if expected_total >= 2.65:
                score += 18
                reasons.append("over25_goal_projection")
            elif expected_total and expected_total < 2.45:
                score -= 18
                reasons.append("goal_projection_too_low_for_over25")
            if mean([home_over25, away_over25]) >= 50:
                score += 8
        if avg_goal_load >= 3.0:
            score += 8

    elif market.startswith("Corners "):
        line = _corner_line(market)
        expected_corners = _float(corner_profile.get("expected_total"))
        games = int(corner_profile.get("games") or 0)
        score = 52
        if games < 6:
            score -= 10
            reasons.append("limited_corner_sample")
        if line and expected_corners:
            edge = expected_corners - line
            if "Over" in market:
                if edge >= 0.75:
                    score += 22
                    reasons.append("corner_projection_clears_line")
                elif edge <= 0.25:
                    score -= 14
                    reasons.append("corner_projection_thin_for_over")
            elif "Under" in market:
                if edge <= -0.75:
                    score += 22
                    reasons.append("corner_projection_below_line")
                elif edge >= -0.25:
                    score -= 14
                    reasons.append("corner_projection_thin_for_under")

    elif market.endswith("Win") or market.startswith("DNB") or market.startswith("AH "):
        score = 54
        home_balance = _float(home.get("avg_scored")) - _float(home.get("avg_conceded"))
        away_balance = _float(away.get("avg_scored")) - _float(away.get("avg_conceded"))
        if "Home" in market or market == "Home Win":
            edge = home_balance - away_balance
        elif "Away" in market or market == "Away Win":
            edge = away_balance - home_balance
        else:
            edge = abs(home_balance - away_balance)
        if edge >= 0.5:
            score += 18
            reasons.append("team_balance_supports_result_market")
        elif edge <= 0.1:
            score -= 10
            reasons.append("team_balance_not_decisive")

    if score >= 82:
        reasons.append("exceptional_fixture_market_fit")
    elif score >= 72:
        reasons.append("strong_fixture_market_fit")
    elif score < 55:
        reasons.append("weak_fixture_market_fit")

    return _review("market_fit", score, reasons, veto=fixture_veto or score < 45)


def scoreline_pattern_reviewer(candidate):
    market = str(candidate.get("market") or "")
    profile = _scoreline_profile(candidate)
    h2h_games = int(profile.get("h2h_games") or 0)
    games = int(profile.get("games") or 0)
    low_total_rate = _float(profile.get("low_total_rate"))
    high_total_rate = _float(profile.get("high_total_rate"))
    btts_rate = _float(profile.get("btts_rate"))
    draw_rate = _float(profile.get("draw_rate"))
    h2h_low_total_rate = _float(profile.get("h2h_low_total_rate"))
    h2h_draw_rate = _float(profile.get("h2h_draw_rate"))
    h2h_btts_rate = _float(profile.get("h2h_btts_rate"))
    low_cluster = bool(profile.get("low_score_cluster"))
    high_cluster = bool(profile.get("high_score_cluster"))
    draw_cluster = bool(profile.get("draw_cluster"))
    btts_cluster = bool(profile.get("btts_cluster"))
    tight_margin_cluster = bool(profile.get("tight_margin_cluster"))
    score = 60
    reasons = []
    veto = False

    if games < 8 and h2h_games < 3:
        score -= 8
        reasons.append("limited_scoreline_pattern_sample")

    if market == "DC: 12":
        if draw_cluster:
            score -= 24
            reasons.append("scoreline_draw_cluster_against_dc12")
            if h2h_games >= 6 and h2h_draw_rate >= 35:
                veto = True
        elif draw_rate <= 18 and (h2h_games < 4 or h2h_draw_rate <= 18):
            score += 10
            reasons.append("scorelines_show_low_draw_pattern")
        if low_cluster:
            score -= 10
            reasons.append("low_scoreline_cluster_increases_draw_risk")
        if tight_margin_cluster:
            score -= 6
            reasons.append("tight_scoreline_margins")

    elif market.startswith("Under"):
        line = _corner_line(market)
        if line >= 3.5:
            if low_cluster or low_total_rate >= 58:
                score += 18
                reasons.append("scorelines_support_controlled_total")
            if high_cluster or high_total_rate >= 62:
                score -= 18
                reasons.append("scorelines_conflict_with_under")
            if h2h_games >= 4 and h2h_low_total_rate >= 65:
                score += 8
                reasons.append("h2h_scorelines_support_under")
        elif line <= 2.5:
            if low_cluster and low_total_rate >= 65:
                score += 16
                reasons.append("scorelines_support_low_total")
            if high_total_rate >= 45:
                score -= 14
                reasons.append("scorelines_too_open_for_low_under")

    elif market.startswith("Over") or "BTTS" in market or market.startswith("GG"):
        line = _corner_line(market)
        if "BTTS" in market or market.startswith("GG"):
            if btts_cluster or btts_rate >= 58:
                score += 16
                reasons.append("scorelines_support_btts")
            if btts_rate <= 35 and (h2h_games < 4 or h2h_btts_rate <= 35):
                score -= 16
                reasons.append("scorelines_conflict_with_btts")
        elif line <= 1.5:
            if low_total_rate <= 35 or high_total_rate >= 45:
                score += 14
                reasons.append("scorelines_support_over15")
            if low_cluster and low_total_rate >= 75:
                score -= 12
                reasons.append("scorelines_tight_for_over15")
        elif line >= 2.5:
            if high_cluster or high_total_rate >= 58:
                score += 18
                reasons.append("scorelines_support_over25")
            if low_cluster or low_total_rate >= 62:
                score -= 18
                reasons.append("scorelines_conflict_with_over25")

    elif market.endswith("Win") or market.startswith("DNB") or market.startswith("AH "):
        if draw_cluster:
            score -= 10
            reasons.append("scoreline_draw_pattern_against_result_market")
        if tight_margin_cluster:
            score -= 6
            reasons.append("scoreline_margin_risk")
        if high_cluster and not draw_cluster:
            score += 6
            reasons.append("scorelines_show_decisive_profile")

    if score >= 76:
        reasons.append("scoreline_pattern_supports_market")
    elif score < 50:
        reasons.append("scoreline_pattern_conflict")

    return _review("scoreline_pattern", score, reasons, veto=veto or score < 40)


def league_market_reviewer(candidate):
    trust = ((candidate.get("insights") or {}).get("league_trust") or {})
    status = trust.get("status") or ""
    fit = market_fit_reviewer(candidate)
    exceptional_fit = fit["score"] >= 85
    if status == "restricted" and not exceptional_fit:
        return _review("league_market", 35, trust.get("reasons") or ["league_market_restricted"], veto=True)
    if status == "restricted":
        return _review(
            "league_market",
            58,
            [*(trust.get("reasons") or ["league_market_restricted"]), "exceptional_fixture_fit_review"],
        )
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

    fit = market_fit_reviewer(candidate)
    exceptional_fit = fit["score"] >= 85
    if status == "restricted" and not exceptional_fit:
        return _review("market_history", 38, trust.get("reasons") or ["market_history_restricted"], veto=True)
    if status == "restricted":
        score = 54
        reasons.extend(trust.get("reasons") or ["market_history_restricted"])
        reasons.append("exceptional_fixture_fit_overrides_market_memory")
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
    if exceptional_fit and score < 55:
        score = 55

    severe_history = market_hit_rate < 40 or market_roi < -15
    return _review("market_history", score, reasons, veto=severe_history and not exceptional_fit)


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
        market_fit_reviewer(candidate),
        scoreline_pattern_reviewer(candidate),
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
