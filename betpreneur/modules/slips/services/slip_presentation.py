"""Slip payloads that reference our own picks.

These would be domain functions except that they read picks' presentation
vocabulary, and picks owns tables — so importing its facade pulls Django in.
R5 caught that, correctly: a domain module must be self-contained. They live
here instead, one layer up.
"""
from __future__ import annotations

from betpreneur.modules.markets.api import market_matches
from betpreneur.modules.picks.api import (
    EXCLUDED_MARKETS,
    format_game_form_line,
    normalise_council_review,
)
from betpreneur.modules.pricing.api import (
    allows_broad_replacement,
    broad_fallback_candidate_allowed,
    count_model_line_from_evidence,
    goal_model_line_from_evidence,
    market_family_group,
    market_owned_model_lines,
    period_or_family_line,
    replacement_scope,
    with_match_checker_advisory,
)
from betpreneur.modules.slips.domain.slip_analysis import (
    SMART_RANDOMIZE_MIN_CONFIDENCE,
    _blocked_slip_recommendation_market,
    _clean_public_slip_evidence_text,
    _evidence_is_risk,
    _market_model_conflicted,
    _market_was_assessed,
    _public_confidence_label,
    _public_market_context_line,
    _public_score,
    _rank_replacement_candidates,
    _replacement_candidate_is_eligible,
    _replacement_is_meaningfully_better,
    _replacement_is_supported_fit,
    _select_ranked_replacement,
    _simple_pick_verdict,
    enrich_market_with_team_intelligence,
    is_broad_safe_cross_family_replacement,
)


def _slip_review_market_cache_payload(row):
    payload = dict(row.market_payload or {})
    insights = row.insights or payload.get("insights") or {}
    council_review = normalise_council_review(
        insights,
        fallback_confidence=row.confidence,
        fallback_tier="",
    )
    payload.update(
        {
            "market": row.market,
            "meaning": row.meaning,
            "raw_confidence": row.raw_confidence,
            "confidence": row.confidence,
            "final_confidence": row.final_confidence if row.final_confidence is not None else council_review.get("final_confidence"),
            "council_review": payload.get("council_review") or council_review,
            "odds": float(row.odds) if row.odds is not None else payload.get("odds"),
            "odds_meta": row.odds_meta or payload.get("odds_meta") or {},
            "ev": float(row.ev) if row.ev is not None else payload.get("ev"),
            "odds_source": row.odds_source or payload.get("odds_source", ""),
            "proven": payload.get("proven", False),
            "eligible": row.eligible,
            "risk_flags": row.risk_flags or payload.get("risk_flags") or [],
            "bettor_view": insights.get("bettor_view") or payload.get("bettor_view") or {},
            "analysis_summary": insights.get("summary", payload.get("analysis_summary", "")),
            "analysis_conclusion": insights.get("conclusion", payload.get("analysis_conclusion", "")),
            "positive_evidence": insights.get("positive_evidence") or payload.get("positive_evidence") or [],
            "risk_evidence": insights.get("risk_evidence") or payload.get("risk_evidence") or [],
            "insights": insights,
            "selected": False,
            "selected_pick_id": None,
            "selected_tier": "",
        }
    )
    if row.market_family and "market_family" not in payload:
        payload["market_family"] = row.market_family
    if row.data_quality and "data_quality" not in payload:
        payload["data_quality"] = row.data_quality
    return payload


def _replacement_market_for_slip(
    game,
    selected_market=None,
    generated_markets=None,
    *,
    allow_safer_fallback=False,
    blocked_markets_out=None,
):
    markets = [
        with_match_checker_advisory(market)
        for market in (game.get("markets") or [])
        if market.get("market") not in EXCLUDED_MARKETS
    ]
    markets.extend(generated_markets or [])
    team_intelligence = (game or {}).get("team_intelligence") or {}
    markets = [enrich_market_with_team_intelligence(market, team_intelligence) for market in markets]
    allowed_markets = []
    blocked_markets = []
    for market in markets:
        if not market:
            continue
        if _blocked_slip_recommendation_market(market):
            blocked_markets.append(market.get("market"))
            continue
        allowed_markets.append(market)
    if blocked_markets_out is not None:
        blocked_markets_out.extend(name for name in dict.fromkeys(blocked_markets) if name)
    markets = allowed_markets
    if selected_market:
        selected_name = selected_market.get("market")
        markets = [market for market in markets if not market_matches(selected_name, market.get("market"))]
    candidates = [
        market
        for market in markets
        if _market_was_assessed(market) and _replacement_candidate_is_eligible(market)
    ]
    if selected_market:
        meaningful_candidates = [
            market
            for market in candidates
            if not is_broad_safe_cross_family_replacement(selected_market, market)
        ]
        if meaningful_candidates:
            candidates = meaningful_candidates
    if not candidates:
        return None
    if selected_market:
        allowed = []
        selected_group = market_family_group(selected_market)
        conflict_blocks_broad = _market_model_conflicted(selected_market) and selected_group == "result"
        for market in candidates:
            scope = replacement_scope(selected_market, market)
            if scope == "broad_fallback" and conflict_blocks_broad:
                continue
            if scope == "broad_fallback" and (not allow_safer_fallback or not allows_broad_replacement(selected_market)):
                continue
            if scope == "broad_fallback" and not broad_fallback_candidate_allowed(selected_market, market):
                continue
            market["replacement_scope"] = scope
            if _replacement_is_meaningfully_better(selected_market, market):
                allowed.append(market)
        if not allowed:
            supported = []
            for market in candidates:
                scope = market.get("replacement_scope") or replacement_scope(selected_market, market)
                if scope == "broad_fallback" and conflict_blocks_broad:
                    continue
                if scope == "broad_fallback" and (not allow_safer_fallback or not allows_broad_replacement(selected_market)):
                    continue
                if scope == "broad_fallback" and not broad_fallback_candidate_allowed(selected_market, market):
                    continue
                market["replacement_scope"] = scope
                if _replacement_is_supported_fit(selected_market, market):
                    supported.append(market)
            if supported:
                allowed = supported
        if not allowed:
            return None
        replacement = _select_ranked_replacement(allowed, selected_market=selected_market)
        if replacement.get("replacement_scope") == "broad_fallback":
            replacement["recommendation_strength"] = "safer_alternative"
        elif not _replacement_is_meaningfully_better(selected_market, replacement):
            replacement["recommendation_strength"] = "best_fit_alternative"
        return replacement
    replacement = _rank_replacement_candidates(candidates)[0]
    if selected_market:
        replacement["replacement_scope"] = replacement_scope(selected_market, replacement)
    return replacement


def _stat_line_from_form(label, form):
    if not isinstance(form, dict) or not int(form.get("games") or 0):
        return ""
    return format_game_form_line(label, form) + "."


def _team_intelligence_profile_line(evidence, *, market_payload=None):
    profile = (evidence or {}).get("team_intelligence_profile")
    if not isinstance(profile, dict):
        return ""
    attempts = profile.get("attempts")
    wins = profile.get("wins")
    hit_rate = profile.get("hit_rate")
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 0
    try:
        wins = int(wins)
    except (TypeError, ValueError):
        wins = None
    try:
        hit_rate = round(float(hit_rate), 1)
    except (TypeError, ValueError):
        hit_rate = None
    if attempts <= 0:
        return ""
    market = profile.get("market") or (market_payload or {}).get("market") or "this market"
    scope = str(profile.get("scope") or "all").replace("_", " ").lower()
    source = str((evidence or {}).get("team_intelligence_source") or "")
    if "league" in source:
        label = "league profile"
    elif scope in {"home", "away"}:
        label = f"{scope} team profile"
    else:
        label = "team profile"
    if wins is not None:
        rate = f" ({hit_rate}%)" if hit_rate is not None else ""
        return f"Stored {label} hit {market} in {wins} of {attempts} tracked matches{rate}."
    if hit_rate is not None:
        return f"Stored {label} shows a {hit_rate}% hit rate for {market} across {attempts} tracked matches."
    return ""


def _h2h_evidence_line(evidence):
    evidence = evidence or {}
    statpal = evidence.get("statpal") if isinstance(evidence.get("statpal"), dict) else {}
    h2h = evidence.get("h2h") if isinstance(evidence.get("h2h"), dict) else statpal.get("h2h") if isinstance(statpal.get("h2h"), dict) else {}
    for payload in (h2h, evidence, statpal):
        if not isinstance(payload, dict):
            continue
        games = payload.get("h2h_games") or payload.get("games")
        try:
            games = int(games)
        except (TypeError, ValueError):
            games = 0
        if games <= 0:
            continue
        avg_goals = payload.get("h2h_avg_goals") or payload.get("avg_goals")
        try:
            avg_goals = round(float(avg_goals), 2)
        except (TypeError, ValueError):
            avg_goals = None
        if avg_goals is not None:
            return f"Head-to-head sample: {games} meetings averaged {avg_goals} goals."
        return f"Head-to-head sample includes {games} tracked meetings."
    return ""


def _stats_backed_evidence(selection, *, market_payload=None, include_context=True, owned_market_only=False):
    market_payload = market_payload or {}
    evidence = []
    context_line = _public_market_context_line(selection, market_payload)
    if include_context and context_line:
        evidence.append(context_line)

    selected_evidence = (
        market_payload.get("advisory_evidence")
        or (selection or {}).get("evidence_payload")
        or {}
    )
    if owned_market_only:
        evidence.extend(market_owned_model_lines(market_payload, selected_evidence))
        profile_line = _team_intelligence_profile_line(selected_evidence, market_payload=market_payload)
        profile_source = str(selected_evidence.get("team_intelligence_source") or "")
        if profile_line and ("stored_league" not in profile_source or not evidence):
            evidence.append(profile_line)
        h2h_line = _h2h_evidence_line(selected_evidence)
        if h2h_line:
            evidence.append(h2h_line)
    else:
        for label, form in (
            ("Home", (selection or {}).get("home_recent_form")),
            ("Away", (selection or {}).get("away_recent_form")),
        ):
            line = _stat_line_from_form(label, form)
            if line:
                evidence.append(line)

        count_line = count_model_line_from_evidence(selected_evidence, market_payload=market_payload)
        if count_line:
            evidence.append(count_line)
        goal_line = goal_model_line_from_evidence(selected_evidence)
        if goal_line:
            evidence.append(goal_line)

    user_market = ((selection or {}).get("user_pick") or {}).get("market")
    payload_market = market_payload.get("market")
    include_selected_raw_evidence = not payload_market or market_matches(user_market, payload_market)
    if include_selected_raw_evidence and not owned_market_only:
        raw_evidence = list((selection or {}).get("evidence") or (selection or {}).get("why") or [])
        for item in raw_evidence:
            text = _clean_public_slip_evidence_text(item)
            lowered = text.lower()
            if not text:
                continue
            if "statpal reference" in lowered or "your price is" in lowered or "reference price" in lowered:
                continue
            evidence.append(text)

    period_line = period_or_family_line(selection, market_payload)
    if period_line:
        evidence.append(period_line)

    return list(dict.fromkeys(evidence))[:5]


def _split_bettor_evidence(selection):
    raw = _stats_backed_evidence(selection, market_payload=(selection or {}).get("your_pick") or {})
    verdict = _simple_pick_verdict(selection)
    positive = [item for item in raw if not _evidence_is_risk(item)]
    risky = [item for item in raw if _evidence_is_risk(item)]
    user_pick = (selection or {}).get("user_pick") or {}
    probability = user_pick.get("confidence_score")
    market = user_pick.get("market") or "this selection"
    if probability is not None:
        support_line = f"The model gives {market} about {_public_score(probability)}% support."
        if verdict in {"risky", "review"}:
            risky.append(support_line)
        else:
            positive.append(support_line)
    if verdict == "risky" and not risky:
        risky = raw[:3] or [f"The available statistics do not strongly support {market}."]
    if verdict in {"keep", "caution"} and not positive:
        positive = raw[:3] or [f"The available statistics give {market} some support."]
    return list(dict.fromkeys(positive))[:4], list(dict.fromkeys(risky))[:4]


def _bettor_recommendation(selection):
    recommendation = (selection or {}).get("recommendation") or {}
    user_pick = (selection or {}).get("user_pick") or {}
    ai_pick = (selection or {}).get("ai_pick") or {}
    recommendation_pick = recommendation.get("pick") or {}
    simple_verdict = _simple_pick_verdict(selection)
    action = recommendation.get("action") or "review"
    replacement_market = ai_pick.get("market") or recommendation_pick.get("market") or recommendation.get("market")
    replacement_score_source = (
        ai_pick.get("confidence_score")
        if ai_pick.get("confidence_score") is not None
        else (
            ai_pick.get("score")
            if ai_pick.get("score") is not None
            else ai_pick.get("decision_score")
        )
        if ai_pick.get("score") is not None or ai_pick.get("decision_score") is not None
        else (
            recommendation_pick.get("confidence_score")
            if recommendation_pick.get("confidence_score") is not None
            else recommendation.get("confidence")
        )
    )
    replacement_score = _public_score(replacement_score_source)
    effective_ai_pick = dict(ai_pick or {})
    if action == "replace" and replacement_market:
        effective_ai_pick.setdefault("available", True)
        effective_ai_pick.setdefault("market", replacement_market)
        if effective_ai_pick.get("confidence_score") is None and replacement_score is not None:
            effective_ai_pick["confidence_score"] = replacement_score
        if effective_ai_pick.get("confidence_label") is None and replacement_score is not None:
            effective_ai_pick["confidence_label"] = _public_confidence_label(replacement_score)
        if effective_ai_pick.get("data_confidence_score") is None and recommendation_pick.get("data_confidence_score") is not None:
            effective_ai_pick["data_confidence_score"] = recommendation_pick.get("data_confidence_score")
        if effective_ai_pick.get("odds") is None and recommendation_pick.get("odds") is not None:
            effective_ai_pick["odds"] = recommendation_pick.get("odds")
    if action == "replace" and effective_ai_pick.get("available") and replacement_market and replacement_score is not None:
        pick = {
            "market": replacement_market,
            "odds": effective_ai_pick.get("odds"),
            "confidence_score": replacement_score,
            "confidence_label": _public_confidence_label(replacement_score),
            "data_confidence_score": _public_score(effective_ai_pick.get("data_confidence_score")),
        }
    elif action == "replace":
        pick = None
        action = "no_replacement"
    elif action in {"keep", "caution"} or simple_verdict in {"keep", "caution"}:
        action = "keep" if simple_verdict == "keep" else "caution"
        user_score = _public_score(user_pick.get("confidence_score"))
        if user_score is not None and user_score >= SMART_RANDOMIZE_MIN_CONFIDENCE:
            pick = {
                "market": user_pick.get("market"),
                "odds": user_pick.get("odds"),
                "confidence_score": user_score,
                "confidence_label": _public_confidence_label(user_pick.get("confidence_score")),
                "data_confidence_score": _public_score(user_pick.get("data_confidence_score")),
            }
        else:
            pick = None
    else:
        pick = None
    if pick is None:
        technical_ref = (selection or {}).get("technical_ref") or {}
        if technical_ref.get("no_replacement_available") or simple_verdict == "risky":
            action = "no_replacement"
        elif simple_verdict == "review":
            action = "review"
    why = _stats_backed_evidence(
        selection,
        market_payload=effective_ai_pick if action == "replace" else ((selection or {}).get("ai_pick") or user_pick),
        include_context=True,
        owned_market_only=(
            action == "replace"
            and effective_ai_pick.get("available")
            and not market_matches(user_pick.get("market"), effective_ai_pick.get("market"))
        ),
    )
    if not why and action == "replace":
        why = list(dict.fromkeys(recommendation.get("why") or []))[:4]
    if not why and action != "replace":
        why = list(dict.fromkeys(recommendation.get("why") or []))[:4]
    if not why:
        if action == "replace":
            why = ["This alternative has stronger statistical support than the original selection."]
        elif action == "keep":
            why = ["Your original selection already fits the statistical profile of the match."]
        elif action == "caution":
            why = ["There is not enough evidence for a stronger replacement to be recommended confidently."]
        elif action == "no_replacement":
            why = ["The original pick is risky, but no statistically supported replacement was found."]
        else:
            why = ["No confident recommendation is available from the current match data."]
    if action == "no_replacement":
        why = [
            item
            for item in why
            if "broader fallback" not in str(item or "").lower()
            and "stronger comparable market" not in str(item or "").lower()
        ]
        why = [
            "The original pick is risky, but no statistically supported replacement was found.",
            *why,
        ]
    return {"action": action, "pick": pick, "why": why}
