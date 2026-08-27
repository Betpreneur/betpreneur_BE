"""Slip review: import, analyse, repair, randomize and stream.

Extracted from the 11k-line apps/algo/views.py.
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core import signing
from django.db import IntegrityError
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from betpreneur.modules.billing.api import (
    InsufficientTokens,
    TokenReservation,
    TokenTransaction,
    insufficient_feature_tokens_payload,
    insufficient_tokens_payload,
    token_wallet_service,
)
from betpreneur.modules.catalog.api import (
    FixtureHydrator,
    FixtureSearchService,
    SlipReviewMarketCache,
    api_response_payload,
    plan_slip_hydration,
    provider_mapping_service,
    team_intelligence_service,
)
from betpreneur.modules.explanations.api import generate as explanation_service
from betpreneur.modules.markets.api import (
    describe_market,
    market_matches,
    normalize_market_text,
)
from betpreneur.modules.picks.api import (
    EXCLUDED_MARKETS,
    AlgoFixture,
    MarketPrediction,
    algo_runner_service,
    decimal_or_none,
    game_detail_payload,
    game_summary_from_fixture,
    market_prediction_payload,
    picks_by_match_for_run,
)
from betpreneur.modules.pricing.api import (
    assess_leg,
    effective_market_capability,
    float_or_none,
    match_checker_status,
    replacement_scope,
    risk_level_for,
    statpal_market_advisory,
    ticket_risk_service,
    with_market_capability,
    with_match_checker_advisory,
    with_statpal_advisory,
)
from betpreneur.modules.scoring.api import capability_for_descriptor
from betpreneur.modules.slips.domain.repair_plan import plan_repair
from betpreneur.modules.slips.domain.slip_analysis import (
    SLIP_REVIEW_MARKET_OPTIONS,
    _bettor_conclusion,
    _bettor_game_summary,
    _bettor_pick_breakdown,
    _blocked_slip_recommendation_market,
    _clean_bettor_evidence_items,
    _clean_deepseek_recommendation_why,
    _clean_public_slip_evidence_text,
    _combined_odds,
    _compact_ai_pick_from_selection,
    _completed_slip_selection_payloads,
    _consume_review_force_fresh,
    _descriptor_from_taxonomy,
    _empty_api_usage,
    _empty_slip_summary,
    _fair_odds,
    _fixture_wide_market_candidates,
    _hit_rate,
    _leg_results_from_persisted_slip_selections,
    _leg_state_counts,
    _manual_verdict,
    _market_can_skip_core_on_demand,
    _market_expected_value,
    _market_for_fixture_orientation,
    _market_was_assessed,
    _matched_fixture_with_statpal,
    _minimal_game_from_candidate,
    _plural,
    _price_reason_code,
    _provider_metadata,
    _public_confidence_label,
    _public_market_meaning,
    _public_market_pick,
    _public_recommendation_strength,
    _public_risk_label,
    _public_score,
    _public_selection_risk,
    _public_slip_review_error_message,
    _public_ticket_killers,
    _public_ticket_label,
    _public_verdict_object,
    _repair_payload,
    _repaired_ticket_confidence_score,
    _replacement_candidate_is_eligible,
    _resolved_canonical_market,
    _resolved_taxonomy,
    _selection_card,
    _selection_flagged_risky,
    _selection_has_analysis,
    _selection_is_unmatched,
    _selection_original_odds,
    _selection_strength_score,
    _selection_suggested_odds,
    _settlement_market_for,
    _should_skip_core_on_demand,
    _simple_pick_verdict,
    _slip_api_usage,
    _slip_review_billable_selection_count,
    _slip_review_booking_code,
    _slip_review_completed_leg_count,
    _slip_selection_payload,
    _smart_randomize_ticket,
    _sportybet_statpal_event,
    _stream_ticket_hash,
    _submitted_market_payload,
    _success_percent_display,
    _ticket_health_label,
    _ticket_health_summary,
    _ticket_issue_text,
    _ticket_killers_message,
    _ticket_risk_level_from_score,
    _with_bettor_view,
    _with_leg_risk,
    _with_smart_randomize,
    _without_blocked_replacement_recommendation,
    _without_remove_recommendation,
    analysis_data_fallback_state,
)
from betpreneur.modules.slips.interface.serializers import (
    BetanoSlipImportRequestSerializer,
    ManualSlipReviewRequestSerializer,
    ManualSlipReviewResponseSerializer,
    SlipRepairRequestSerializer,
    SlipRepairResponseSerializer,
    SlipReviewDetailResponseSerializer,
    SlipReviewEventsQuerySerializer,
    SlipReviewEventsResponseSerializer,
    SlipReviewListResponseSerializer,
    SlipReviewOptionsResponseSerializer,
    SlipReviewRandomizeRequestSerializer,
    SlipReviewRandomizeResponseSerializer,
    SlipReviewRecapQuerySerializer,
    SlipReviewRecapResponseSerializer,
    SlipReviewStreamTokenResponseSerializer,
    SportyBetSlipImportRequestSerializer,
)
from betpreneur.modules.slips.models import (
    SlipLegAnalysisCache,
    SlipRepair,
    SlipReview,
    SlipReviewEvent,
    SlipReviewStreamToken,
    SlipSelection,
)
from betpreneur.modules.slips.services import progress as slip_review_redis
from betpreneur.modules.slips.services.importers import (
    BetanoBetslipImporter,
    SportyBetShareImporter,
)
from betpreneur.modules.slips.services.slip_presentation import (
    _bettor_recommendation,
    _replacement_market_for_slip,
    _slip_review_market_cache_payload,
    _split_bettor_evidence,
    _stats_backed_evidence,
)
from betpreneur.modules.slips.tasks import import_slip_review
from betpreneur.platform.config import env_int as _env_int
from betpreneur.platform.db.json import json_safe

log = logging.getLogger(__name__)

# Leg-analysis cache tuning. Recovered with the pipeline functions below —
# these are read only by them.
SLIP_REVIEW_LEG_CACHE_TTL_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_TTL_SECONDS", 15 * 60)
SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS", 5 * 60)
SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS = _env_int("SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS", 45)
SLIP_REVIEW_STALE_AFTER_SECONDS = _env_int("SLIP_REVIEW_STALE_AFTER_SECONDS", 20 * 60)


SLIP_REVIEW_DEEPSEEK_MAX_GAMES = _env_int("SLIP_REVIEW_DEEPSEEK_MAX_GAMES", 5)


SLIP_REVIEW_STREAM_TICKET_SECONDS = _env_int("SLIP_REVIEW_STREAM_TICKET_SECONDS", 30 * 60)
SLIP_REVIEW_STREAM_TICKET_SALT = "betpreneur.slip-review.stream"




SLIP_REVIEW_VERDICT_OPTIONS = [
    {"value": "keep", "label": "Keep", "description": "Selection is strong enough to stay on the slip."},
    {"value": "caution", "label": "Caution", "description": "Selection has some support but carries warnings."},
    {"value": "replace", "label": "Replace", "description": "A stronger market exists for the same game."},
    {"value": "remove", "label": "Remove", "description": "Selection does not show enough edge."},
    {"value": "unmatched", "label": "Unmatched", "description": "Fixture could not be confidently matched."},
    {"value": "pending_analysis", "label": "Pending Analysis", "description": "Fixture matched but has not been scored yet."},
]


def _slip_review_token_cost(selection_count):
    return max(0, int(selection_count or 0)) * int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1))


def _slip_review_billing_payload(review, *, selection_count, reservation=None, status_value="reserved"):
    token_cost = _slip_review_token_cost(selection_count)
    payload = {
        "status": status_value,
        "token_cost": token_cost,
        "cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        "games": int(selection_count or 0),
    }
    if reservation:
        payload["reservation_id"] = reservation.id
        payload["reservation_status"] = reservation.status
        payload["reservation_expires_at"] = reservation.expires_at.isoformat() if reservation.expires_at else None
    review_payload = dict(review.submitted_payload or {})
    if review_payload.get("token_reservation_id"):
        payload["reservation_id"] = review_payload.get("token_reservation_id")
    return payload


def _store_slip_review_billing(review, billing):
    summary = dict(review.summary or {})
    summary["billing"] = json_safe(billing)
    review.summary = summary


def _reserve_slip_review_tokens(review, selection_count):
    token_cost = _slip_review_token_cost(selection_count)
    if token_cost <= 0:
        billing = _slip_review_billing_payload(review, selection_count=selection_count, status_value="not_required")
        _store_slip_review_billing(review, billing)
        return None

    result = token_wallet_service.reserve_tokens(
        review.user,
        token_cost,
        reference_type="slip_review",
        reference_id=str(review.id),
        metadata={
            "review_id": review.id,
            "source": review.source,
            "selection_count": int(selection_count or 0),
            "cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        },
    )
    submitted_payload = dict(review.submitted_payload or {})
    submitted_payload["token_reservation_id"] = result.reservation.id
    submitted_payload["token_cost"] = token_cost
    submitted_payload["selection_count"] = int(selection_count or 0)
    review.submitted_payload = json_safe(submitted_payload)
    _store_slip_review_billing(
        review,
        _slip_review_billing_payload(
            review,
            selection_count=selection_count,
            reservation=result.reservation,
            status_value="reserved",
        ),
    )
    return result




def _consume_slip_review_token_reservation(review):
    reservation_id = (review.submitted_payload or {}).get("token_reservation_id")
    if not reservation_id:
        return None
    submitted_payload = review.submitted_payload or {}
    reserved_count = int(submitted_payload.get("selection_count") or review.selections.count() or 0)
    billable_count = _slip_review_billable_selection_count(review)
    reserved_tokens = _slip_review_token_cost(reserved_count)
    charged_tokens = _slip_review_token_cost(billable_count)
    refunded_tokens = max(0, reserved_tokens - charged_tokens)
    try:
        result = token_wallet_service.consume_reservation_amount(int(reservation_id), charged_tokens)
        billing = _slip_review_billing_payload(
            review,
            selection_count=reserved_count,
            reservation=result.reservation,
            status_value="consumed",
        )
        billing.update(
            {
                "billable_games": billable_count,
                "charged_tokens": charged_tokens,
                "refunded_tokens": refunded_tokens,
                "non_billable_games": max(0, reserved_count - billable_count),
            }
        )
        _store_slip_review_billing(review, billing)
        return result
    except Exception:
        # Do not fail a delivered review over a billing hiccup -- but do not hide it
        # either. The escrow is still open, so the reservation sweeper reconciles it
        # against the review's status and recognises the tokens rather than refunding
        # a review the user already received.
        log.exception(
            "Slip review token reservation consume failed review=%s reservation=%s "
            "-- left open for reconciliation",
            review.id,
            reservation_id,
        )
        _store_slip_review_billing(
            review,
            {
                **_slip_review_billing_payload(
                    review,
                    selection_count=reserved_count,
                    status_value="consume_failed",
                ),
                "billable_games": billable_count,
                "charged_tokens": charged_tokens,
                "refunded_tokens": refunded_tokens,
                "non_billable_games": max(0, reserved_count - billable_count),
                "reconciliation_pending": True,
            },
        )
        return None


def _release_slip_review_token_reservation(review):
    reservation_id = (review.submitted_payload or {}).get("token_reservation_id")
    if not reservation_id:
        return None
    try:
        result = token_wallet_service.release_reservation(int(reservation_id))
        _store_slip_review_billing(
            review,
            _slip_review_billing_payload(
                review,
                selection_count=(review.submitted_payload or {}).get("selection_count") or review.selections.count(),
                reservation=result.reservation,
                status_value="released",
            ),
        )
        return result
    except ValueError:
        return None
    except TokenReservation.DoesNotExist:
        return None
    except Exception:
        log.exception("Slip review token reservation release failed review=%s reservation=%s", review.id, reservation_id)
        return None
















def _generated_match_checker_markets(
    selected_descriptor,
    *,
    game,
    statpal_context,
    provider_payload=None,
    statpal_payload=None,
):
    generated = []
    fixture = {**(game or {}), "statpal_context": statpal_context or {}}
    for descriptor, generated_source in _fixture_wide_market_candidates(
        selected_descriptor,
        game=game,
        statpal_context=statpal_context,
    ):
        capability = capability_for_descriptor(
            descriptor, fixture=fixture, statpal_context=statpal_context
        )
        advisory = statpal_market_advisory.evaluate_market(
            descriptor,
            fixture=fixture,
            provider_payload=provider_payload or {},
            statpal_payload=statpal_payload,
        )
        if not advisory.get("available") or float_or_none(advisory.get("score")) is None:
            continue
        canonical_market = descriptor.canonical or descriptor.raw
        market = _submitted_market_payload(
            requested_market=canonical_market,
            market_taxonomy=descriptor.to_dict(),
            statpal_advisory=advisory,
            market_capability=capability,
        )
        # Price the alternative. Recommending a swap into a market whose price we do not
        # know is advice nobody can check, and it left the ranking with nothing but raw
        # probability -- which always prefers whichever market has the highest base rate.
        reference = statpal_market_advisory.reference_price(descriptor, fixture=fixture)
        reference_odds = float_or_none(reference.get("odds"))
        market.update(
            {
                "market": canonical_market,
                "meaning": _public_market_meaning(canonical_market),
                "confidence": None,
                "final_confidence": None,
                "odds": reference_odds,
                "odds_source": "statpal_reference" if reference_odds else "unpriced",
                "odds_reference": reference or None,
                "generated": True,
                "generated_source": generated_source,
            }
        )
        market["ev"] = _market_expected_value(market)
        consensus = statpal_market_advisory.devigged_probability(reference)
        if consensus is not None:
            evidence = dict(market.get("advisory_evidence") or {})
            evidence["market_consensus_percent"] = consensus
            evidence["bookmaker_count"] = reference.get("bookmaker_count")
            market["advisory_evidence"] = evidence
        generated.append(market)
    return generated














































def manual_fixture_game(match_id, match_date, request=None):
    target_match_id = str(match_id or "").strip()
    prediction = (
        MarketPrediction.objects.select_related("run", "selected_pick")
        .filter(match_id=target_match_id)
        .order_by("-run__created_at", "-created_at")
        .first()
    )
    if prediction:
        algo_run = prediction.run
        predictions = (
            MarketPrediction.objects.filter(run=algo_run, match_id=target_match_id)
            .select_related("selected_pick")
            .order_by("-confidence", "-ev", "market")
        )
        source_payload = (
            AlgoFixture.objects.filter(run=algo_run, match_id=target_match_id)
            .values_list("source_payload", flat=True)
            .first()
            or {}
        )
        markets = [
            market_prediction_payload(item)
            for item in predictions
            if item.market not in EXCLUDED_MARKETS
        ]
        fixture_summary = {
            "fixture": prediction.fixture,
            "home_team": prediction.home_team,
            "away_team": prediction.away_team,
            "league": prediction.league,
            "kickoff": prediction.kickoff,
            "match_id": prediction.match_id,
            "home_recent_form": prediction.home_recent_form,
            "away_recent_form": prediction.away_recent_form,
            "fixture_context": prediction.fixture_context,
            "team_news": prediction.team_news,
            "markets": markets,
            "source_payload": source_payload,
        }
        game = game_summary_from_fixture(
            fixture_summary,
            picks_by_match_for_run(algo_run),
            request=request,
            include_markets=True,
        )
        if game and game.get("markets"):
            log.info(
                "Slip review market score cache hit match_id=%s run_id=%s markets=%s",
                match_id,
                algo_run.id,
                len(game.get("markets") or []),
            )
            return game

    cache_query = SlipReviewMarketCache.objects.filter(
        cache_scope=SlipReviewMarketCache.Scope.SLIP_REVIEW,
        expires_at__gt=timezone.now(),
    )
    cache_filter = Q(match_id=target_match_id)
    if target_match_id.startswith("statpal:"):
        cache_filter |= Q(provider_match_id=target_match_id.replace("statpal:", "", 1))
    else:
        cache_filter |= Q(provider_match_id=target_match_id)
        cache_filter |= Q(match_id=f"statpal:{target_match_id}")
    if match_date:
        cache_filter &= Q(match_date=match_date)
    cache_rows = list(
        cache_query.filter(cache_filter).order_by("-confidence", "-ev", "market")
    )
    if cache_rows:
        first = cache_rows[0]
        fixture_payload = first.fixture_payload or {}
        markets = [
            _slip_review_market_cache_payload(row)
            for row in cache_rows
            if row.market not in EXCLUDED_MARKETS
        ]
        fixture_summary = {
            **fixture_payload,
            "fixture": first.fixture,
            "home_team": first.home_team,
            "away_team": first.away_team,
            "home_logo": first.home_logo,
            "away_logo": first.away_logo,
            "league": first.league,
            "league_logo": first.league_logo,
            "country": first.country,
            "country_flag": first.country_flag,
            "kickoff": first.kickoff,
            "match_id": first.match_id,
            "provider_match_id": first.provider_match_id,
            "provider_competition_id": first.provider_competition_id,
            "markets": markets,
            "market_count": len(markets),
            "provider_merge": first.provider_merge or {},
        }
        game = game_summary_from_fixture(
            fixture_summary,
            {},
            request=request,
            include_markets=True,
        )
        if game and game.get("markets"):
            game["slip_review_cache"] = {
                "source": "slip_review_market_cache",
                "row_count": len(cache_rows),
                "market_count": len(game.get("markets") or []),
                "cache_version": first.cache_version,
                "expires_at": first.expires_at.isoformat() if first.expires_at else "",
            }
            log.info(
                "Slip review private market cache hit match_id=%s provider_match_id=%s markets=%s cache_version=%s",
                match_id,
                first.provider_match_id,
                len(game.get("markets") or []),
                first.cache_version,
            )
            return game

    payload = game_detail_payload(match_date, match_id, request=request)
    game = payload.get("game")
    if game and game.get("markets"):
        return game
    return None












def _selection_market_descriptor(selection, requested_market):
    """
    Use the identity resolved at import time; only parse text when there is none.

    Re-deriving the descriptor from the canonical string is how period markets were
    being lost: the importer resolves market 60 to `match_result / first_half` and
    writes `1H Home Win`, which text parsing then cannot read back. Trusting the stored
    identity removes the re-derivation rather than teaching the parser one more string
    form.
    """
    taxonomy = _resolved_taxonomy(selection)
    if taxonomy:
        try:
            return _descriptor_from_taxonomy(taxonomy)
        except (TypeError, ValueError) as exc:
            log.info("Falling back to text parsing for %r: %s", requested_market, str(exc)[:200])
    return describe_market(
        requested_market,
        market_name=(taxonomy.get("raw") or (selection.get("market_taxonomy") or {}).get("raw") or ""),
    )
















































































def _public_price_check_from_card(card):
    evidence = card.get("evidence") or {}
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


def _public_why_from_card(card):
    why = []
    codes = []
    evidence = card.get("evidence") or {}
    alternative = card.get("alternative") or {}
    alt_evidence = alternative.get("evidence") or {}
    historical_accuracy = alt_evidence.get("historical_accuracy") or evidence.get("historical_accuracy")
    sample_size = alt_evidence.get("sample_size") or evidence.get("sample_size")
    roi = alt_evidence.get("similar_market_roi") if alt_evidence.get("similar_market_roi") is not None else evidence.get("similar_market_roi")
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
    price_code = _price_reason_code(_public_price_check_from_card(card))
    if price_code:
        codes.append(price_code)
    if alternative.get("reason"):
        why.append(alternative["reason"])
        codes.append("better_alternative")
    statpal_message = (card.get("statpal_advisory") or {}).get("message")
    if statpal_message:
        why.append(statpal_message)
        codes.append("statpal_advisory")
    if not why and card.get("message"):
        why.append(card["message"])
        codes.append("model_message")
    return why[:4], list(dict.fromkeys(codes))[:6]
















def _public_selection_card(item):
    card = _selection_card(item)
    selected_market = item.get("selected_market") or {}
    replacement_market = item.get("replacement_market") or {}
    verdict = item.get("verdict")
    if replacement_market and _blocked_slip_recommendation_market(replacement_market):
        replacement_market = {}
    if replacement_market and not _replacement_candidate_is_eligible(replacement_market):
        replacement_market = {}
        if verdict == "replace":
            verdict = "caution"
            item = {**item, "no_replacement_available": True}
    ai_pick = None
    if verdict == "replace" and replacement_market:
        ai_pick = _public_market_pick(replacement_market)
    elif verdict in {"keep", "caution"}:
        selected_score = float_or_none(selected_market.get("advisory_score"))
        if selected_score is not None and selected_score >= 55:
            ai_pick = _public_market_pick(selected_market, fallback_market=item.get("submitted_market"), fallback_odds=_selection_original_odds(item))
    if ai_pick:
        ai_pick["recommendation_strength"] = _public_recommendation_strength(ai_pick)
        if verdict == "replace" and replacement_market:
            ai_pick["replacement_scope"] = replacement_market.get("replacement_scope") or replacement_scope(selected_market, replacement_market)
    if verdict != "replace":
        card = {**card, "alternative": None}
    why, reason_codes = _public_why_from_card(card)
    if verdict == "replace" and replacement_market and ai_pick:
        why = _stats_backed_evidence(
            {"user_pick": _public_market_pick(selected_market, fallback_market=item.get("submitted_market"), fallback_odds=_selection_original_odds(item))},
            market_payload=ai_pick,
            include_context=True,
            owned_market_only=True,
        )
        if not why:
            why = [f"{ai_pick.get('market')} has the strongest supported profile among eligible alternatives for this fixture."]
        # The `why` is rewritten to describe the alternative, but the reason codes also
        # summarise the user's own pick -- including how its price compares. Dropping them
        # wholesale left `reason_codes` contradicting the `price_check` in the same
        # payload, which still reported a positive edge or a short price.
        rebuilt = [
            code
            for code in ("market_specific_evidence", "replacement_market_fit")
            if code not in (item.get("reason_codes") or [])
        ] or ["replacement_market_fit"]
        price_code = _price_reason_code(_public_price_check_from_card(card))
        if price_code and price_code not in rebuilt:
            rebuilt.append(price_code)
        reason_codes = rebuilt
    price_check = _public_price_check_from_card(card)
    your_pick = {
        "market": item.get("submitted_market"),
        "label": item.get("submitted_market"),
        "meaning": _public_market_meaning(item.get("submitted_market")),
        "confidence": card.get("confidence"),
        "odds": card.get("odds"),
        "score": card.get("advisory_score"),
        "decision_score": card.get("advisory_score"),
        "status": card.get("advisory_status") or match_checker_status(float_or_none(card.get("advisory_score"))),
    }
    capability = item.get("market_capability") or selected_market.get("market_capability") or {}
    if capability:
        your_pick["support_level"] = capability.get("support_level")
        your_pick["data_quality"] = capability.get("data_quality")
        your_pick["confidence_cap"] = capability.get("confidence_cap")
    risk_level = _public_selection_risk(verdict, your_pick)
    statpal_context = item.get("statpal_context") or card.get("statpal_context") or {}
    statpal_coverage = statpal_context.get("market_snapshot_coverage") or {}
    statpal_plan = statpal_context.get("market_snapshot_plan") or {}
    statpal_snapshot_types = sorted((statpal_context.get("snapshots") or {}).keys())
    statpal_hydration_source = statpal_context.get("hydration_source") or ("statpal_context" if statpal_snapshot_types else "")
    statpal_snapshot_cache_status = statpal_context.get("snapshot_cache_status") or ("hit" if statpal_snapshot_types else "")
    technical_ref = {
        "status": item.get("status"),
        "match_resolution_score": card.get("match_resolution_score"),
        "kickoff": (item.get("matched_fixture") or {}).get("kickoff_utc")
        or (item.get("matched_fixture") or {}).get("kickoff")
        or "",
        "market_recognized": (item.get("market_taxonomy") or {}).get("recognized"),
        "market_core_supported": (item.get("market_taxonomy") or {}).get("core_supported"),
        "market_support_level": capability.get("support_level") if capability else "",
        "market_data_quality": capability.get("data_quality") if capability else "",
        "market_confidence_cap": capability.get("confidence_cap") if capability else None,
        "market_capability_warnings": capability.get("warnings") or [],
        "statpal_snapshot_types": statpal_snapshot_types,
        "statpal_hydration_source": statpal_hydration_source,
        "statpal_snapshot_cache_status": statpal_snapshot_cache_status,
        "statpal_required_snapshot_types": statpal_coverage.get("required") or statpal_plan.get("snapshot_types") or [],
        "statpal_missing_snapshot_types": statpal_coverage.get("missing") or statpal_plan.get("missing_snapshot_types") or [],
        "statpal_stale_snapshot_types": statpal_plan.get("stale_snapshot_types") or [],
        "statpal_snapshot_coverage_percent": statpal_coverage.get("coverage_percent") if statpal_coverage else statpal_plan.get("coverage_percent"),
        "provider_merge": (item.get("matched_fixture") or {}).get("provider_merge") or item.get("provider_merge") or {},
        "blocked_recommendation_markets": item.get("blocked_recommendation_markets") or [],
        "no_replacement_available": bool(item.get("no_replacement_available")),
        "has_technical_details": True,
    }
    leg_assessment = assess_leg(item)
    canonical_market = item.get("canonical_market") or {}
    if card.get("match_id"):
        technical_ref["match_id"] = card.get("match_id")
    if item.get("status") == "matched_unscored":
        on_demand = item.get("on_demand_analysis") or {}
        technical_ref["analysis_status"] = on_demand.get("status") or "not_started"
        technical_ref["analysis_error"] = on_demand.get("error") or ""
        technical_ref["analysis_run_id"] = on_demand.get("run_id")
    return {
        "id": card.get("match_id") or item.get("match"),
        "match": card.get("fixture") or card.get("match"),
        "match_id": card.get("match_id", ""),
        "your_pick": your_pick,
        "verdict": _public_verdict_object(verdict, submitted_market=item.get("submitted_market"), pick_status=your_pick.get("status")),
        "risk_level": risk_level,
        "risk": _public_risk_label(risk_level),
        "ai_pick": ai_pick,
        "price_check": price_check,
        "why": why,
        "reason_codes": reason_codes,
        "home_recent_form": item.get("home_recent_form") or {},
        "away_recent_form": item.get("away_recent_form") or {},
        "corner_profile": item.get("corner_profile") or {},
        "fixture_context": item.get("fixture_context") or {},
        "evidence_payload": selected_market.get("advisory_evidence") or {},
        "state": str(leg_assessment.state),
        "assessment": {
            "type": leg_assessment.assessment_type,
            "may_publish_probability": leg_assessment.may_publish_probability,
            "market_family": leg_assessment.family,
            "message": leg_assessment.message,
        },
        "market_identity": {
            "resolution": canonical_market.get("resolution") or "unresolved",
            "provider_market_text": item.get("provider_market_text") or item.get("submitted_market") or "",
            "period": canonical_market.get("period") or "",
            "subject": canonical_market.get("subject") or "",
        },
        "technical_ref": technical_ref,
    }


def _with_explanation(card):
    """Attach a plain-language explanation built only from values the model produced."""
    card["explanation"] = explanation_service.explain_leg(card).to_dict()
    return card


































































def _build_bettor_public_payload(review, technical_public, *, enhance=False):
    technical_public = technical_public or {}
    ticket_summary = technical_public.get("ticket_summary") or {}
    user_ticket = ticket_summary.get("user_ticket") or {}
    ai_ticket = ticket_summary.get("ai_ticket") or {}
    improvement = ticket_summary.get("improvement") or {}
    breakdown = ticket_summary.get("pick_breakdown") or {}
    games = []
    recommended_picks = []
    for selection in technical_public.get("selections") or []:
        user_pick = selection.get("user_pick") or selection.get("your_pick") or {}
        recommendation = _bettor_recommendation(selection)
        positive_evidence, risk_evidence = _split_bettor_evidence(selection)
        match = selection.get("match") or ""
        selected_pick = recommendation.get("pick")
        changed = recommendation.get("action") == "replace"
        games.append(
            {
                "id": selection.get("id"),
                "match": match,
                "kickoff": (selection.get("technical_ref") or {}).get("kickoff")
                or (selection.get("matched_fixture") or {}).get("kickoff_utc")
                or "",
                "user_pick": {
                    "market": user_pick.get("market"),
                    "odds": user_pick.get("odds"),
                    "confidence_score": _public_score(user_pick.get("confidence_score")),
                    "confidence_label": _public_confidence_label(user_pick.get("confidence_score")),
                    "data_confidence_score": _public_score(user_pick.get("data_confidence_score")),
                    "verdict": _simple_pick_verdict(selection),
                    "summary": _bettor_game_summary(selection),
                },
                "analysis": {
                    "positive_evidence": positive_evidence,
                    "risk_evidence": risk_evidence,
                    "conclusion": _bettor_conclusion(selection),
                },
                "recommendation": recommendation,
            }
        )
        if selected_pick:
            recommended_picks.append(
                {
                    "match": match,
                    "market": (selected_pick or {}).get("market"),
                    "confidence_score": (selected_pick or {}).get("confidence_score"),
                    "confidence_label": (selected_pick or {}).get("confidence_label"),
                    "action": recommendation.get("action"),
                    "included_in_estimate": (selected_pick or {}).get("confidence_score") is not None,
                    "changed": changed,
                }
            )
        else:
            simple_verdict = _simple_pick_verdict(selection)
            recommended_picks.append(
                {
                    "match": match,
                    "market": user_pick.get("market"),
                    "confidence_score": None,
                    "confidence_label": "Unknown",
                    "action": "no_replacement" if simple_verdict == "risky" else "review" if simple_verdict == "review" else recommendation.get("action"),
                    "included_in_estimate": False,
                    "changed": False,
                }
            )

    changes = int(improvement.get("picks_changed") or 0)
    high_risk_count = int(breakdown.get("high_risk") or 0)
    review_count = int(breakdown.get("needs_review") or 0)
    risky_count = high_risk_count
    needs_word = "needs" if changes == 1 else "need"
    verdict_title = f"{changes} {_plural(changes, 'pick')} {needs_word} changing" if changes else "No forced changes"
    if changes:
        extra = f" {review_count} {_plural(review_count, 'pick')} still needs review." if review_count == 1 else (
            f" {review_count} {_plural(review_count, 'pick')} still need review." if review_count else ""
        )
        verdict_message = (
            f"{changes} {_plural(changes, 'selection')} "
            f"{'has' if changes == 1 else 'have'} weak statistical support. "
            f"We found {'a stronger alternative' if changes == 1 else 'stronger alternatives'} for "
            f"{'it' if changes == 1 else 'them'}.{extra}"
        )
    elif risky_count or review_count:
        total_attention = risky_count + review_count
        verdict_message = (
            f"{total_attention} {_plural(total_attention, 'selection')} need caution or review, but no stronger replacement "
            "was found with enough statistical support."
        )
    else:
        verdict_message = "Your selections are supported by the available match data."

    payload = {
        "id": review.id,
        "source": review.source,
        "status": _public_slip_review_status(review.status),
        "ticket": {
            "total_games": ticket_summary.get("total_legs") or len(games),
            "original_odds": user_ticket.get("combined_odds"),
            "user_picks": {
                "confidence_score": _public_score(user_ticket.get("overall_confidence_score")),
                "label": _public_ticket_label(user_ticket.get("overall_confidence_score")),
                "estimated_success_percent": user_ticket.get("estimated_success_percent"),
                "estimated_success_display": _success_percent_display(user_ticket.get("estimated_success_percent")),
                "summary": {
                    "strong": int(breakdown.get("strong") or 0),
                    "playable": int(breakdown.get("playable") or 0),
                    "risky": risky_count,
                    "review": review_count,
                },
            },
            "recommended_picks": {
                "confidence_score": _public_score(ai_ticket.get("overall_confidence_score")),
                "label": _public_ticket_label(ai_ticket.get("overall_confidence_score")),
                "estimated_success_percent": ai_ticket.get("estimated_success_percent"),
                "estimated_success_display": _success_percent_display(ai_ticket.get("estimated_success_percent")),
                "estimated_odds": ai_ticket.get("combined_odds"),
                "changes": changes,
            },
            "verdict": {"title": verdict_title, "message": verdict_message},
        },
        "games": games,
        "recommended_ticket": {
            "confidence_score": _public_score(ai_ticket.get("overall_confidence_score")),
            "confidence_label": _public_ticket_label(ai_ticket.get("overall_confidence_score")),
            "estimated_success_percent": ai_ticket.get("estimated_success_percent"),
            "estimated_success_display": _success_percent_display(ai_ticket.get("estimated_success_percent")),
            "estimated_odds": ai_ticket.get("combined_odds"),
            "picks": recommended_picks,
        },
        "disclaimer": (
            "Confidence scores are statistical estimates based on available match data and do not guarantee an outcome."
        ),
    }
    if enhance and review.source == SlipReview.Source.SPORTYBET:
        game_count = len(games)
        if game_count <= SLIP_REVIEW_DEEPSEEK_MAX_GAMES:
            payload = _enhance_bettor_public_with_deepseek(payload)
        else:
            log.info(
                "DeepSeek SportyBet public analysis skipped review=%s games=%s max_games=%s reason=large_slip_llm_limit",
                review.id,
                game_count,
                SLIP_REVIEW_DEEPSEEK_MAX_GAMES,
            )
    return _with_smart_randomize(payload)


def _enhance_bettor_public_with_deepseek(payload):
    try:
        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        if not algo_runner.llm_reasoning_enabled():
            return payload
        games = payload.get("games") or []
        compact_games = [
            {
                "index": index,
                "match": game.get("match"),
                "user_pick": game.get("user_pick"),
                "analysis": game.get("analysis"),
                "recommendation": game.get("recommendation"),
            }
            for index, game in enumerate(games)
        ]
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        deepseek_payload = {
            "model": model,
            "temperature": 0.15,
            "top_p": 0.85,
            "max_tokens": max(1800, min(7000, 850 * len(compact_games))),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write simple football betslip analysis for bettors. Use only the supplied facts. "
                        "Do not promise a win. Do not invent team form, injuries, odds, xG, lineups, or H2H. "
                        "Keep the user's markets, scores, verdicts, and recommendation actions unchanged. "
                        "Do not introduce Over 0.5, 1H Over 0.5, 2H Over 0.5, team Over 0.5, "
                        "or player shots/SOT Over 0.5 as replacement recommendations unless it is the user's submitted pick. "
                        "Do not use StatPal reference-price wording as evidence. Bettors need football stats, "
                        "Do not mention StatPal, API-Football, snapshots, provider context, or internal source names. "
                        "such as expected goals, recent form, first-half/second-half profile, corners, cards, "
                        "shots, or the market-specific confidence already supplied. "
                        "Return strict valid JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Rewrite each game into bettor-facing analysis. For each game return: index, "
                        "user_pick_summary, positive_evidence, risk_evidence, conclusion, recommendation_why. "
                        "Evidence arrays must only rephrase supplied football/statistical evidence and must be short bullet strings. "
                        "Each evidence item should include an actual stat when supplied, such as confidence %, expected goals, "
                        "recent W-D-L, goals scored/conceded, corner totals, card totals, shot volume, or period-specific context. "
                        "Never write bullets like 'Your price is close to the StatPal reference'. "
                        "Shape: {\"games\":[{\"index\":0,\"user_pick_summary\":\"...\","
                        "\"positive_evidence\":[\"...\"],\"risk_evidence\":[\"...\"],"
                        "\"conclusion\":\"...\",\"recommendation_why\":[\"...\"]}]}.\n"
                        f"Data:\n{json.dumps(compact_games, ensure_ascii=True)}"
                    ),
                },
            ],
        }
        content = algo_runner._deepseek_chat_completion(deepseek_payload, retries=1) or ""
        parsed = algo_runner._parse_llm_json(content)
        items = parsed.get("games") if isinstance(parsed, dict) else []
        updates = {int(item.get("index")): item for item in items or [] if isinstance(item, dict) and str(item.get("index", "")).isdigit()}
        for index, game in enumerate(games):
            update = updates.get(index)
            if not update:
                continue
            if update.get("user_pick_summary"):
                summary_text = _clean_public_slip_evidence_text(update["user_pick_summary"])
                if summary_text:
                    game["user_pick"]["summary"] = summary_text[:320]
            if isinstance(update.get("positive_evidence"), list):
                cleaned = _clean_bettor_evidence_items(update["positive_evidence"])
                if cleaned:
                    game["analysis"]["positive_evidence"] = cleaned
            if isinstance(update.get("risk_evidence"), list):
                game["analysis"]["risk_evidence"] = _clean_bettor_evidence_items(update["risk_evidence"])
            if update.get("conclusion"):
                conclusion = _clean_public_slip_evidence_text(update["conclusion"])
                if conclusion:
                    game["analysis"]["conclusion"] = conclusion[:360]
            if isinstance(update.get("recommendation_why"), list):
                cleaned = _clean_deepseek_recommendation_why(game, update["recommendation_why"])
                if cleaned:
                    game["recommendation"]["why"] = cleaned
        log.info("DeepSeek SportyBet public analysis enhanced %s/%s games", len(updates), len(games))
    except Exception as exc:
        log.warning("DeepSeek SportyBet public analysis skipped: %s", exc)
    return payload




def _slip_intelligence(results):
    enriched = []
    for item in results:
        copy = _without_remove_recommendation(dict(item))
        copy = _without_blocked_replacement_recommendation(copy)
        copy["selection_score"] = _selection_strength_score(copy)
        enriched.append(copy)

    analysed = [item for item in enriched if _selection_has_analysis(item)]
    # Ticket health is the geometric mean of the calibrated leg probabilities, so it
    # measures leg quality independently of leg count. See pricing.services.ticket_risk.
    ticket_risk = ticket_risk_service.assess(enriched)
    overall_score = ticket_risk.health_percent

    remove_items = [item for item in enriched if item.get("verdict") == "remove"]
    replace_items = [item for item in enriched if item.get("verdict") == "replace"]
    caution_items = [item for item in enriched if item.get("verdict") == "caution"]
    keep_items = [item for item in enriched if item.get("verdict") == "keep"]
    expired_items = [item for item in enriched if item.get("status") == "expired"]
    pending_items = [item for item in enriched if item.get("status") == "matched_unscored"]
    not_assessed_items = [item for item in enriched if item.get("verdict") == "not_assessed"]
    unverified_items = [
        item for item in enriched
        if (not _selection_has_analysis(item) or item.get("verdict") == "not_assessed")
        and item.get("status") != "expired"
    ]

    risk_level = risk_level_for(ticket_risk)

    strongest = sorted(
        [item for item in analysed if item.get("verdict") in {"keep", "caution"}],
        key=lambda item: item.get("selection_score") or 0,
        reverse=True,
    )[:3]
    weakest = sorted(
        [item for item in analysed if item.get("verdict") in {"remove", "replace"}],
        key=lambda item: item.get("selection_score") or 0,
    )[:3]
    original_combined = _combined_odds(_selection_original_odds(item) for item in enriched)
    suggested_combined = _combined_odds(_selection_suggested_odds(item) for item in enriched)
    original_success = ticket_risk.success_percent
    optimized_success = ticket_risk.repaired_success_percent
    optimized_leg_count = ticket_risk.assessed_legs
    improvement = (
        round(optimized_success - original_success, 2)
        if optimized_success is not None and original_success is not None
        else None
    )
    api_usage = _slip_api_usage(enriched)

    if remove_items:
        verdict = f"Remove {len(remove_items)} leg(s) before trusting this slip."
    elif replace_items:
        verdict = f"Replace {len(replace_items)} leg(s) with stronger markets."
    elif caution_items:
        verdict = "Playable, but treat the caution legs carefully."
    elif keep_items and len(keep_items) == len(analysed) and not unverified_items:
        verdict = "This slip is clean from the current model view."
    else:
        verdict = "Some selections still need verification before this slip is reliable."

    ticket_health = {
        "score": overall_score,
        "max_score": 100,
        "label": _ticket_health_label(overall_score),
        "risk_level": risk_level,
        "summary": _ticket_health_summary(
            overall_score,
            risk_level,
            len(remove_items),
            len(replace_items),
            len(caution_items),
            len(unverified_items),
        ),
    }
    original_ticket = {
        "legs": len(analysed),
        "estimated_success": original_success,
        "combined_odds": original_combined,
        "fair_odds": _fair_odds((original_success or 0) / 100) if original_success is not None else None,
    }
    optimized_ticket = {
        "legs": optimized_leg_count,
        "estimated_success": optimized_success,
        "combined_odds": suggested_combined,
        "fair_odds": _fair_odds((optimized_success or 0) / 100) if optimized_success is not None else None,
    }
    repaired_confidence_score = _repaired_ticket_confidence_score(ticket_risk)
    confidence_change = (
        round(repaired_confidence_score - overall_score, 1)
        if repaired_confidence_score is not None and overall_score is not None
        else None
    )
    improvement_text = f"+{improvement} percentage points" if improvement is not None and improvement > 0 else (
        f"{improvement} percentage points" if improvement is not None else ""
    )
    # A leg is only genuinely tracked when the settler can find it after kickoff: it
    # needs a resolved fixture date and a market this engine can settle. Anything else
    # must not be reported as tracked.
    trackable_items = [
        item for item in enriched
        if _settlement_market_for(item) and (item.get("matched_fixture") or {}).get("match_date")
    ]
    untracked_items = []
    for item in enriched:
        reasons = []
        if not (item.get("matched_fixture") or {}).get("match_date"):
            reasons.append("missing_fixture_date")
        if not _settlement_market_for(item):
            reasons.append("unsupported_settlement_market")
        if reasons:
            untracked_items.append(
                {
                    "id": (item.get("matched_fixture") or {}).get("match_id") or item.get("match"),
                    "match": item.get("match"),
                    "market": item.get("submitted_market"),
                    "reasons": reasons,
                }
            )
    flagged_risky_items = [item for item in enriched if _selection_flagged_risky(item)]
    learning_tracking = {
        "status": "tracking" if trackable_items else "not_tracked",
        "tracked_selections": len(trackable_items),
        "untracked_selections": len(enriched) - len(trackable_items),
        "flagged_risky_selections": len(flagged_risky_items),
        "outcome_tracking": "pending_settlement" if trackable_items else "unavailable",
        "reason": (
            ""
            if trackable_items
            else "No leg has both a resolved fixture date and a market the settlement engine supports."
        ),
    }

    public_selections = [
        _with_explanation(_with_bettor_view(_with_leg_risk(_public_selection_card(item), leg)))
        for item, leg in zip(enriched, ticket_risk.legs)
    ]
    bettor_breakdown = _bettor_pick_breakdown(public_selections)
    public_ticket_killers = _public_ticket_killers(ticket_risk)
    recommended_change_ids = [
        selection.get("id")
        for selection in public_selections
        if (selection.get("verdict") or {}).get("code") in {"replace", "remove"}
    ]
    ticket_impact = {
        "message": (
            # Only claim an improvement when one was actually measured; a null
            # increase alongside "improves the success rate" is not a claim we can make.
            f"Changing {len(replace_items) + len(remove_items)} risky {_plural(len(replace_items) + len(remove_items), 'pick')} improves the estimated ticket success rate."
            if (remove_items or replace_items) and improvement is not None
            else f"{len(replace_items) + len(remove_items)} {_plural(len(replace_items) + len(remove_items), 'pick')} could be changed, but the effect on this ticket could not be estimated."
            if remove_items or replace_items
            else f"None of these {len(enriched)} {_plural(len(enriched), 'selection')} could be analysed, so no risk assessment was possible."
            if not analysed
            else "No major risky picks were found in the analysed selections."
        ),
        "picks_changed": len(replace_items) + len(remove_items),
        "estimated_success_increase_points": improvement,
        "original_odds": original_combined,
        "optimized_odds": suggested_combined,
    }
    verdict_code = "review"
    verdict_label = "Review ticket"
    verdict_message = verdict
    issue_text = _ticket_issue_text(
        replace_count=len(replace_items),
        remove_count=len(remove_items),
        caution_count=len(caution_items),
        unverified_count=len(unverified_items),
    )
    if remove_items and replace_items:
        verdict_code = "replace_or_remove"
        change_count = len(remove_items) + len(replace_items)
        verdict_label = f"Change {change_count} {_plural(change_count, 'pick')}"
        verdict_message = f"This ticket has {issue_text}."
    elif remove_items:
        verdict_code = "avoid_risky_picks"
        verdict_label = f"Avoid {len(remove_items)} {_plural(len(remove_items), 'pick')}"
        verdict_message = f"This ticket has {issue_text}."
    elif replace_items:
        verdict_code = "replace_picks" if len(replace_items) != len(analysed) else "replace_all"
        verdict_label = (
            f"Replace {len(replace_items)} {_plural(len(replace_items), 'pick')}"
            if len(replace_items) != len(analysed)
            else f"Replace all {len(replace_items)} {_plural(len(replace_items), 'pick')}"
        )
        verdict_message = (
            "Every submitted pick has a safer or stronger alternative."
            if len(replace_items) == len(analysed)
            else f"This ticket has {issue_text}."
        )
    elif caution_items:
        verdict_code = "play_with_caution"
        verdict_label = "Play with caution"
        verdict_message = f"This ticket has {issue_text}."
    elif keep_items and len(keep_items) == len(analysed) and not unverified_items:
        verdict_code = "playable"
        verdict_label = "Playable"
        verdict_message = "This ticket looks clean from the current analysis."

    public_review = {
        "contract_version": "match_checker_public_v2",
        "response_mode": "public",
        "ticket": {
            "title": "Slip Review",
            "total_legs": len(enriched),
            "analysed_legs": len(analysed),
            "pending_analysis_legs": len(pending_items),
            "unmatched_legs": len([item for item in enriched if _selection_is_unmatched(item)]),
            "expired_legs": len(expired_items),
            "estimated_success_percent": ticket_risk.success_percent,
            "estimated_success_display": _success_percent_display(ticket_risk.success_percent),
            "risk_tiers": ticket_risk.tier_counts,
            "assessed_legs_in_estimate": ticket_risk.assessed_legs,
            "legs_excluded_from_estimate": ticket_risk.unassessed_legs,
        },
        "correlation": ticket_risk.correlation,
        "explanation": {},
        "leg_states": _leg_state_counts(enriched),
        "ticket_health": ticket_health,
        "ticket_summary": {
            "total_legs": len(enriched),
            "pick_breakdown": bettor_breakdown,
            "user_ticket": {
                "overall_confidence_score": overall_score,
                "estimated_success_percent": original_success,
                "estimated_success_display": _success_percent_display(original_success),
                "risk_level": risk_level,
                "label": _ticket_health_label(overall_score),
                "combined_odds": original_combined,
                "model_fair_odds": original_ticket["fair_odds"],
            },
            "ai_ticket": {
                "overall_confidence_score": repaired_confidence_score,
                "estimated_success_percent": optimized_success,
                "estimated_success_display": _success_percent_display(optimized_success),
                "risk_level": _ticket_risk_level_from_score(repaired_confidence_score),
                "label": "Improved" if confidence_change is not None and confidence_change > 0 else _ticket_health_label(repaired_confidence_score),
                "combined_odds": suggested_combined,
                "model_fair_odds": optimized_ticket["fair_odds"],
            },
            "improvement": {
                "confidence_score_change": confidence_change,
                "success_probability_change": improvement,
                "picks_changed": len(replace_items) + len(remove_items),
            },
        },
        "ticket_killers": {
            "selections": public_ticket_killers,
            "message": _ticket_killers_message(ticket_risk),
            "combined_risk_share_percent": round(
                sum(killer["risk_share_percent"] for killer in public_ticket_killers), 1
            ) if public_ticket_killers else None,
        },
        "calibration": {
            **ticket_risk.calibration.to_dict(),
            "disclaimer": (
                "Estimated success rates are model estimates, not guarantees. "
                + (
                    "They are calibrated against selections that have already settled."
                    if ticket_risk.calibration.basis != "prior"
                    else "Not enough selections have settled yet to validate these estimates, "
                         "so a deliberately conservative prior is used."
                )
            ),
        },
        "verdict": {
            "code": verdict_code,
            "label": verdict_label,
            "message": verdict_message,
        },
        "comparison": {
            "original": {
                "legs": original_ticket["legs"],
                "combined_odds": original_ticket["combined_odds"],
                "model_fair_odds": original_ticket["fair_odds"],
                "model_estimated_success_percent": original_ticket["estimated_success"],
            },
            "repaired": {
                "legs": optimized_ticket["legs"],
                "combined_odds": optimized_ticket["combined_odds"],
                "model_fair_odds": optimized_ticket["fair_odds"],
                "model_estimated_success_percent": optimized_ticket["estimated_success"],
            },
            "optimized": {
                "legs": optimized_ticket["legs"],
                "combined_odds": optimized_ticket["combined_odds"],
                "model_fair_odds": optimized_ticket["fair_odds"],
                "model_estimated_success_percent": optimized_ticket["estimated_success"],
            },
            "success_increase_percentage_points": improvement,
            "picks_changed": len(replace_items) + len(remove_items),
        },
        "improvement": {
            "original_success_percent": original_success,
            "repaired_success_percent": optimized_success,
            "optimized_success_percent": optimized_success,
            "increase_percentage_points": improvement,
            "label": improvement_text,
        },
        "ticket_impact": ticket_impact,
        "recommended_change_ids": recommended_change_ids,
        "counts": {
            "keep": len(keep_items),
            "caution": len(caution_items),
            "replace": len(replace_items),
            "remove": len(remove_items),
            "pending_analysis": len(pending_items),
            "not_assessed": len(not_assessed_items),
            "unmatched": len([item for item in enriched if _selection_is_unmatched(item)]),
            "expired": len(expired_items),
        },
        "selections": public_selections,
        "tracking": {
            "enabled": bool(trackable_items),
            "status": learning_tracking["outcome_tracking"],
            "tracked_selections": len(trackable_items),
            "untracked_selections": len(untracked_items),
            "untracked": untracked_items,
            "flagged_risky_selections": len(flagged_risky_items),
        },
    }
    # Built last, so it can summarise the finished payload rather than a partial one.
    public_review["explanation"] = explanation_service.explain_ticket(public_review).to_dict()

    return enriched, {
        "overall_score": overall_score,
        "health_score": overall_score,
        "risk_level": risk_level,
        "verdict": verdict,
        "summary": ticket_health["summary"],
        "public": public_review,
        "ticket_health": ticket_health,
        "original_ticket": original_ticket,
        "optimized_ticket": optimized_ticket,
        "improvement": improvement_text,
        "improvement_percent": improvement,
        "learning_tracking": learning_tracking,
        "api_usage": api_usage,
        "original_combined_odds": original_combined,
        "suggested_combined_odds": suggested_combined,
        "strongest_legs": [_selection_card(item) for item in strongest],
        "weakest_legs": [_selection_card(item) for item in weakest],
        "legs_to_keep": [_selection_card(item) for item in keep_items],
        "legs_to_caution": [_selection_card(item) for item in caution_items],
        "legs_to_replace": [_selection_card(item) for item in replace_items],
        "legs_to_remove": [_selection_card(item) for item in remove_items],
        "expired_legs": [_selection_card(item) for item in expired_items],
        "unverified_legs": [_selection_card(item) for item in unverified_items],
    }


def _manual_review_summary(results):
    enriched, intelligence = _slip_intelligence(results)
    return {
        "count": len(enriched),
        "analysed_count": sum(1 for item in enriched if _selection_has_analysis(item)),
        "keep_count": sum(1 for item in enriched if item.get("verdict") == "keep"),
        "caution_count": sum(1 for item in enriched if item.get("verdict") == "caution"),
        "replace_count": sum(1 for item in enriched if item.get("verdict") == "replace"),
        "remove_count": sum(1 for item in enriched if item.get("verdict") == "remove"),
        "expired_count": sum(1 for item in enriched if item.get("status") == "expired"),
        "unmatched_count": sum(1 for item in enriched if _selection_is_unmatched(item)),
        "pending_analysis_count": sum(1 for item in enriched if item.get("status") == "matched_unscored"),
        "not_assessed_count": sum(1 for item in enriched if item.get("verdict") == "not_assessed"),
        "health_score": intelligence.get("health_score", 0),
        "risk_level": intelligence.get("risk_level", ""),
        "ticket_health": intelligence.get("ticket_health", {}),
        "original_ticket": intelligence.get("original_ticket", {}),
        "optimized_ticket": intelligence.get("optimized_ticket", {}),
        "improvement": intelligence.get("improvement", ""),
        "improvement_percent": intelligence.get("improvement_percent"),
        "learning_tracking": intelligence.get("learning_tracking", {}),
        "api_usage": intelligence.get("api_usage", _empty_api_usage()),
        "public": intelligence.get("public", {}),
        "intelligence": intelligence,
    }


def _review_status_from_summary(summary):
    count = int(summary.get("count") or 0)
    analysed_count = int(summary.get("analysed_count") or 0)
    pending_count = int(summary.get("pending_analysis_count") or 0)
    not_assessed_count = int(summary.get("not_assessed_count") or 0)
    reviewable_count = max(0, count - int(summary.get("expired_count") or 0))
    if count and analysed_count == reviewable_count:
        return SlipReview.Status.COMPLETED
    if analysed_count:
        return SlipReview.Status.PARTIAL
    if pending_count:
        return SlipReview.Status.UNANALYSED
    if not_assessed_count:
        # The review ran to completion and concluded it could not assess anything.
        # That is a finding, not a crash, and must not be reported as a failure.
        return SlipReview.Status.UNANALYSED
    return SlipReview.Status.FAILED






def _log_slip_review_debug(review, summary):
    public = (summary or {}).get("public") or {}
    ticket_summary = public.get("ticket_summary") or {}
    user_ticket = ticket_summary.get("user_ticket") or {}
    ai_ticket = ticket_summary.get("ai_ticket") or {}
    improvement = ticket_summary.get("improvement") or {}
    explanation = public.get("explanation") or {}
    tracking = public.get("tracking") or {}
    correlation = public.get("correlation") or {}
    counts = public.get("counts") or {}
    verdict = public.get("verdict") or {}
    log.info(
        (
            "Slip review public summary review=%s status=%s source=%s total_legs=%s analysed=%s "
            "user_conf=%s user_success=%s user_odds=%s ai_conf=%s ai_success=%s ai_odds=%s "
            "confidence_delta=%s success_delta=%s picks_changed=%s verdict=%s counts=%s "
            "correlation=%s tracking=%s explanation_ok=%s explanation_reasons=%s"
        ),
        review.id,
        review.status,
        review.source,
        ticket_summary.get("total_legs"),
        (summary or {}).get("analysed_count"),
        user_ticket.get("overall_confidence_score"),
        user_ticket.get("estimated_success_percent"),
        user_ticket.get("combined_odds"),
        ai_ticket.get("overall_confidence_score"),
        ai_ticket.get("estimated_success_percent"),
        ai_ticket.get("combined_odds"),
        improvement.get("confidence_score_change"),
        improvement.get("success_probability_change"),
        improvement.get("picks_changed"),
        verdict.get("code"),
        counts,
        correlation,
        {
            "status": tracking.get("status"),
            "tracked": tracking.get("tracked_selections"),
            "untracked": tracking.get("untracked_selections"),
            "flagged_risky": tracking.get("flagged_risky_selections"),
        },
        (explanation.get("validation") or {}).get("ok"),
        (explanation.get("validation") or {}).get("reasons") or [],
    )

    untracked_by_id = {
        str(item.get("id") or ""): item.get("reasons") or []
        for item in tracking.get("untracked") or []
    }
    for index, selection in enumerate(public.get("selections") or [], start=1):
        user_pick = selection.get("user_pick") or selection.get("your_pick") or {}
        ai_pick = selection.get("ai_pick") or {}
        comparison = selection.get("comparison") or {}
        technical = selection.get("technical_ref") or {}
        market_consensus = selection.get("market_consensus") or {}
        assessment = selection.get("assessment") or {}
        selection_id = str(selection.get("id") or "")
        log.info(
            (
                "Slip review leg debug review=%s leg=%s id=%s match=%r market=%r state=%s family=%s "
                "verdict=%s risk=%s user_conf=%s user_label=%s user_prob=%s data_conf=%s "
                "ai_available=%s ai_market=%r ai_conf=%s ai_label=%s ai_prob=%s "
                "confidence_gain=%s ticket_lift=%s value=%s market_gap=%s disagreement=%s "
                "price_status=%s statpal_source=%s statpal_cache=%s statpal_coverage=%s "
                "statpal_required=%s statpal_missing=%s statpal_stale=%s statpal_snapshots=%s "
                "provider_merge=%s "
                "blocked_recommendations=%s warnings=%s tracking_reasons=%s bettor_verdict=%s recommendation=%s "
                "evidence_count=%s reason_codes=%s"
            ),
            review.id,
            index,
            selection_id,
            selection.get("match"),
            user_pick.get("market"),
            selection.get("state"),
            assessment.get("market_family"),
            (selection.get("verdict") or {}).get("code"),
            selection.get("risk_level"),
            user_pick.get("confidence_score"),
            user_pick.get("confidence_label"),
            user_pick.get("model_probability_percent"),
            user_pick.get("data_confidence_score"),
            ai_pick.get("available"),
            ai_pick.get("market"),
            ai_pick.get("confidence_score"),
            ai_pick.get("confidence_label"),
            ai_pick.get("model_probability_percent"),
            comparison.get("confidence_gain"),
            comparison.get("ticket_success_lift"),
            user_pick.get("value_rating"),
            market_consensus.get("probability_gap_points"),
            market_consensus.get("disagreement_level"),
            (selection.get("price_check") or {}).get("status"),
            technical.get("statpal_hydration_source"),
            technical.get("statpal_snapshot_cache_status"),
            technical.get("statpal_snapshot_coverage_percent"),
            technical.get("statpal_required_snapshot_types") or [],
            technical.get("statpal_missing_snapshot_types") or [],
            technical.get("statpal_stale_snapshot_types") or [],
            technical.get("statpal_snapshot_types") or [],
            technical.get("provider_merge") or {},
            technical.get("blocked_recommendation_markets") or [],
            technical.get("market_capability_warnings") or [],
            untracked_by_id.get(selection_id, []),
            user_pick.get("verdict"),
            (selection.get("recommendation") or {}).get("action"),
            len(selection.get("evidence") or []),
            selection.get("reason_codes") or [],
        )


def _populate_slip_review(review, results):
    safe_results = json_safe(results)
    safe_results, _ = _slip_intelligence(safe_results)
    summary = _manual_review_summary(safe_results)
    review.status = _review_status_from_summary(summary)
    summary["bettor_public"] = _build_bettor_public_payload(
        review,
        (summary.get("public") or {}),
        enhance=True,
    )
    review.summary = summary
    review.save(update_fields=["status", "summary", "updated_at"])
    _log_slip_review_debug(review, summary)
    review.selections.all().delete()
    rows = []
    for index, item in enumerate(safe_results, start=1):
        matched = item.get("matched_fixture") or {}
        rows.append(
            SlipSelection(
                review=review,
                order=index,
                submitted_match=item.get("match", ""),
                submitted_market=item.get("submitted_market", ""),
                status=item.get("status", ""),
                verdict=item.get("verdict", ""),
                message=item.get("message", ""),
                match_id=matched.get("match_id") or "",
                match_date=matched.get("match_date") or None,
                fixture=matched.get("fixture") or "",
                home_team=matched.get("home_team") or "",
                away_team=matched.get("away_team") or "",
                league=matched.get("league") or "",
                country=matched.get("country") or "",
                kickoff=matched.get("kickoff") or "",
                selected_market=item.get("selected_market") or {},
                best_market=item.get("best_market") or {},
                recommended_market=item.get("recommended_market") or {},
                possible_matches=item.get("possible_matches") or [],
                analysis_payload=item,
                settlement_market=_settlement_market_for(item),
                odds=decimal_or_none(_selection_original_odds(item)),
                flagged_risky=_selection_flagged_risky(item),
                advisory_score=float_or_none(
                    item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score")
                ),
            )
        )
    SlipSelection.objects.bulk_create(rows, batch_size=100)
    return summary, safe_results




def _slip_review_progress(*, phase, total=0, completed=0, message="", **extra):
    total = max(0, int(total or 0))
    completed = max(0, min(int(completed or 0), total)) if total else max(0, int(completed or 0))
    percent = round((completed / total) * 100, 1) if total else (100.0 if phase in {"completed", "failed"} else 0.0)
    progress = {
        "phase": str(phase or ""),
        "total": total,
        "completed": completed,
        "percent": percent,
        "message": str(message or ""),
        "updated_at": timezone.now().isoformat(),
    }
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            progress[key] = json_safe(value)
    return progress


def _public_slip_review_status(value):
    return SlipReview.Status.COMPLETED if value == SlipReview.Status.PARTIAL else value


def _public_slip_review_progress(progress):
    progress = dict(progress or {})
    if progress.get("final_status"):
        progress["final_status"] = _public_slip_review_status(progress.get("final_status"))
    return progress


def _publish_slip_review_event(review, event_type, payload=None):
    payload = json_safe(payload or {})
    public_payload = dict(payload)
    if public_payload.get("status"):
        public_payload["status"] = _public_slip_review_status(public_payload["status"])
    if isinstance(public_payload.get("progress"), dict) and public_payload["progress"].get("final_status"):
        public_payload["progress"] = _public_slip_review_progress(public_payload["progress"])
    try:
        event = SlipReviewEvent.objects.create(
            review=review,
            event_type=str(event_type or ""),
            payload=public_payload,
        )
        log.info(
            "Slip review event review=%s event=%s event_id=%s payload=%s",
            review.id,
            event_type,
            event.id,
            payload,
        )
        event_payload = {
            "type": "slip_review.event",
            "id": event.id,
            "review_id": review.id,
            "event_type": event.event_type,
            "payload": event.payload or {},
            "created_at": event.created_at.isoformat() if event.created_at else "",
        }
        slip_review_redis.push_event(
            review.id,
            event_payload,
            snapshot={
                "type": "slip_review.snapshot",
                "review_id": review.id,
                "status": _public_slip_review_status(review.status),
                "progress": _public_slip_review_progress((review.summary or {}).get("progress") or {}),
                "latest_event_id": event.id,
                "updated_at": review.updated_at.isoformat() if review.updated_at else "",
            },
        )
        if getattr(settings, "ENABLE_WEBSOCKETS", False):
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer

                channel_layer = get_channel_layer()
                if channel_layer:
                    async_to_sync(channel_layer.group_send)(
                        f"slip_review_{review.id}",
                        {
                            "type": "slip_review.event",
                            "payload": event_payload,
                        },
                    )
            except Exception:
                log.exception(
                    "Slip review websocket publish failed review=%s event=%s event_id=%s",
                    review.id,
                    event_type,
                    event.id,
                )
        return event
    except Exception:
        log.exception("Slip review event publish failed review=%s event=%s", getattr(review, "id", None), event_type)
        return None


def _set_slip_review_progress(review, *, phase, total=0, completed=0, message="", status=None, save=True, **extra):
    summary = dict(review.summary or {})
    summary["progress"] = _slip_review_progress(
        phase=phase,
        total=total,
        completed=completed,
        message=message,
        **extra,
    )
    review.summary = summary
    if status:
        review.status = status
    public_progress = _public_slip_review_progress(summary["progress"])
    slip_review_redis.store_snapshot(
        review.id,
        {
            "type": "slip_review.snapshot",
            "review_id": review.id,
            "status": _public_slip_review_status(review.status),
            "progress": public_progress,
            "latest_event_id": None,
            "updated_at": timezone.now().isoformat(),
        },
    )
    if save:
        fields = ["summary", "updated_at"]
        if status:
            fields.insert(0, "status")
        review.save(update_fields=fields)
        _publish_slip_review_event(
            review,
            "review.progress",
            {
                "status": _public_slip_review_status(review.status),
                "progress": public_progress,
            },
        )
    return summary["progress"]


def _create_queued_slip_review(user, *, source, submitted_payload):
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.QUEUED,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary={
            **_empty_slip_summary("Slip import queued."),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        },
    )






def _active_slip_review_public_payload(review, *, summary, progress, latest_event_id):
    completed_payloads = _completed_slip_selection_payloads(review)
    if not completed_payloads:
        return {
            "id": review.id,
            "source": review.source,
            "status": _public_slip_review_status(review.status),
            "created_at": review.created_at,
            "updated_at": review.updated_at,
            "progress": _public_slip_review_progress(progress),
            "latest_event_id": latest_event_id,
        }

    partial_summary = _manual_review_summary(completed_payloads)
    payload = _build_bettor_public_payload(
        review,
        partial_summary.get("public") or {},
        enhance=False,
    )
    payload["status"] = _public_slip_review_status(review.status)
    payload["progress"] = _public_slip_review_progress(progress)
    payload["latest_event_id"] = latest_event_id
    payload["created_at"] = review.created_at
    payload["updated_at"] = review.updated_at
    payload["partial"] = True
    payload["completed_games"] = len(completed_payloads)
    total = int((progress or {}).get("total") or 0)
    if total:
        payload.setdefault("ticket", {})["total_games"] = total
    payload = _with_smart_randomize(payload)
    return payload






def _compact_slip_review_list_payload(review, *, include_picks=True, pick_limit=None, use_summary=True):
    summary = (review.summary or {}) if use_summary else {}
    public_payload = summary.get("bettor_public") or {}
    if not public_payload and summary.get("public"):
        public_payload = _build_bettor_public_payload(review, summary.get("public") or {})

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
                    "confidence_score": _public_score(selection.advisory_score),
                    "verdict": selection.verdict or selection.status or "review",
                },
                "ai_pick": _compact_ai_pick_from_selection(selection),
            }
            for selection in selections
        ]

    payload = {
        "id": review.id,
        "number_of_games": int(number_of_games or 0),
        "status": _public_slip_review_status(review.status),
        "source": review.source,
        "booking_code": _slip_review_booking_code(review),
        "title": review.title,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
        "picks": picks,
    }
    if include_picks:
        payload["picks_returned"] = len(picks)
        payload["has_more_picks"] = truncated
    return payload


def _slip_review_payload(review, *, include_selections=True, public_only=False):
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
            progress = (summary or {}).get("progress") or _slip_review_progress(
                phase=review.status,
                message=f"Slip review is {review.status}.",
            )
            return api_response_payload(
                _active_slip_review_public_payload(
                    review,
                    summary=summary,
                    progress=progress,
                    latest_event_id=latest_event_id,
                )
            )
        bettor_payload = summary.get("bettor_public") or _build_bettor_public_payload(
            review,
            public_payload,
            enhance=False,
        )
        if bettor_payload.get("status") != review.status:
            bettor_payload = _build_bettor_public_payload(
                review,
                public_payload,
                enhance=False,
            )
        bettor_payload = {**bettor_payload, "status": _public_slip_review_status(bettor_payload.get("status"))}
        bettor_payload = _with_smart_randomize(bettor_payload)
        return api_response_payload(bettor_payload)
    payload = {
        "id": review.id,
        "source": review.source,
        "status": _public_slip_review_status(review.status),
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
            _slip_selection_payload(selection)
            for selection in review.selections.all().order_by("order", "id")
        ]
    return api_response_payload(payload)


def _provider_match_date(selection):
    provider_payload = selection.get("provider_payload") or {}
    kickoff_ms = provider_payload.get("kickoff_ms")
    if kickoff_ms in (None, ""):
        nested = provider_payload.get("provider_payload") or {}
        kickoff_ms = ((nested.get("outcome") or {}).get("estimateStartTime"))
        if kickoff_ms in (None, ""):
            kickoff_ms = ((nested.get("leg") or {}).get("eventStartTime"))
    try:
        if kickoff_ms in (None, ""):
            return None
        return datetime.fromtimestamp(float(kickoff_ms) / 1000, tz=timezone.get_current_timezone()).date()
    except (TypeError, ValueError, OSError):
        return None


def _provider_kickoff_datetime(selection):
    provider_payload = selection.get("provider_payload") or {}
    kickoff_ms = provider_payload.get("kickoff_ms")
    if kickoff_ms in (None, ""):
        nested = provider_payload.get("provider_payload") or {}
        kickoff_ms = ((nested.get("outcome") or {}).get("estimateStartTime"))
        if kickoff_ms in (None, ""):
            kickoff_ms = ((nested.get("leg") or {}).get("eventStartTime"))
    try:
        if kickoff_ms in (None, ""):
            return None
        return datetime.fromtimestamp(float(kickoff_ms) / 1000, tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OSError):
        return None


def _provider_event_status(selection):
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    status = str(outcome.get("status") if outcome.get("status") is not None else "").strip()
    match_status = str(outcome.get("matchStatus") or "").strip().lower()
    return status, match_status






def _try_sportybet_statpal_mapping(selection, *, provider_date, resolver_trace):
    provider_payload = selection.get("provider_payload") or {}
    provider_event_id = str(provider_payload.get("provider_event_id") or "").strip()
    if str(selection.get("provider") or "").lower() != "sportybet" or not provider_event_id:
        return None

    search_service = FixtureSearchService()
    sync_result = {}

    try:
        result = provider_mapping_service.match_sportybet_to_statpal(_sportybet_statpal_event(selection))
    except Exception as exc:
        result = {"matched": False, "reason": "sportybet_statpal_mapping_error", "error": str(exc)}

    if not result.get("matched") and provider_date:
        try:
            sync_result = search_service.sync_statpal_daily(provider_date)
        except Exception as exc:
            sync_result = {"synced": 0, "errors": [str(exc)]}
        try:
            result = provider_mapping_service.match_sportybet_to_statpal(_sportybet_statpal_event(selection))
        except Exception as exc:
            result = {"matched": False, "reason": "sportybet_statpal_mapping_error_after_sync", "error": str(exc)}

    resolver_trace.append(
        {
            "strategy": "sportybet_statpal_mapping",
            "synced": sync_result.get("synced", 0),
            "sync_errors": sync_result.get("errors", []),
            "matched": bool(result.get("matched")),
            "reason": result.get("reason", ""),
            "candidate_match_id": ((result.get("candidate") or {}).get("match_id") if isinstance(result.get("candidate"), dict) else ""),
            "candidate_score": ((result.get("candidate") or {}).get("match_score") if isinstance(result.get("candidate"), dict) else None),
        }
    )
    return result


def _selection_expiry(selection):
    status, match_status = _provider_event_status(selection)
    terminal_statuses = {"ended", "finished", "cancelled", "canceled", "postponed", "abandoned"}
    if status in {"3", "4", "5"} or match_status in terminal_statuses:
        return {
            "expired": True,
            "reason": "provider_event_not_reviewable",
            "message": "This event has already ended or is not available for pre-match review.",
        }
    kickoff_at = _provider_kickoff_datetime(selection)
    if kickoff_at and kickoff_at <= timezone.now():
        return {
            "expired": True,
            "reason": "kickoff_already_passed",
            "message": "This event has already started and cannot be reviewed as a pre-match selection.",
        }
    return {"expired": False}


def _analyse_manual_selection(
    selection,
    *,
    days,
    request=None,
    force_fresh=False,
    hydration_cache=None,
    review_scoring_context=None,
    allow_on_demand_scoring=True,
):
    match_text = selection.get("match", "")
    requested_market = selection.get("market", "")
    market_descriptor = _selection_market_descriptor(selection, requested_market)
    market_taxonomy = market_descriptor.to_dict()
    provider_date = _provider_match_date(selection)
    provider_kickoff = _provider_kickoff_datetime(selection)
    provider_metadata = _provider_metadata(selection)
    expiry = _selection_expiry(selection)
    resolver_trace = [
        {
            "strategy": "provider_metadata",
            "provider_date": provider_date.isoformat() if provider_date else "",
            "provider_kickoff": provider_kickoff.isoformat() if provider_kickoff else "",
            "competition": provider_metadata.get("competition") or "",
            "provider_event_id": provider_metadata.get("provider_event_id") or "",
            "provider_competition_id": provider_metadata.get("provider_competition_id") or "",
            "expired": expiry.get("expired", False),
            "expiry_reason": expiry.get("reason", ""),
        }
    ]
    if expiry.get("expired"):
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "expired",
            "verdict": "expired",
            "message": expiry.get("message"),
            "fixture_resolution": {
                "status": "expired",
                "attempts": resolver_trace,
            },
            "possible_matches": [],
        }

    search_service = FixtureSearchService()
    statpal_candidate = {}
    provider_fixture = search_service.get_provider_fixture(
        provider=provider_metadata.get("provider"),
        provider_event_id=provider_metadata.get("provider_event_id"),
    )
    if provider_fixture and (provider_fixture.get("fixture") or {}).get("source") == "statpal":
        statpal_candidate = statpal_candidate or provider_fixture.get("fixture") or {}
        resolver_trace.append(
            {
                "strategy": "provider_fixture_map_statpal_context",
                "mapping_id": provider_fixture.get("mapping_id"),
                "candidate_match_ids": [provider_fixture["fixture"].get("match_id")],
            }
        )
        provider_fixture = None
    if provider_fixture:
        candidates = [provider_fixture["fixture"]]
        resolver_trace.append(
            {
                "strategy": "provider_fixture_map",
                "mapping_id": provider_fixture.get("mapping_id"),
                "candidate_count": 1,
                "candidate_match_ids": [provider_fixture["fixture"].get("match_id")],
            }
        )
    else:
        statpal_mapping_result = _try_sportybet_statpal_mapping(selection, provider_date=provider_date, resolver_trace=resolver_trace)
        statpal_candidate = (statpal_mapping_result or {}).get("candidate") if isinstance(statpal_mapping_result, dict) else {}
        search = search_service.search(match_text, days=days, limit=5)
        candidates = search.get("results") or []
        resolver_trace.append(
            {
                "strategy": "local_or_default_window",
                "candidate_count": len(candidates),
                "refreshed": search.get("refreshed", False),
                "refresh_errors": search.get("refresh_errors", []),
                "candidate_match_ids": [candidate.get("match_id") for candidate in candidates],
            }
        )
    best_score = float((candidates[0] if candidates else {}).get("match_score") or 0)
    if not provider_fixture and provider_date and best_score < 70:
        search = search_service.search(
            match_text,
            start_date=max(provider_date - timedelta(days=2), timezone.localdate()),
            days=4,
            limit=5,
            refresh=True,
            unrestricted=True,
        )
        candidates = search.get("results") or candidates
        resolver_trace.append(
            {
                "strategy": "provider_date_unrestricted_refresh",
                "candidate_count": len(search.get("results") or []),
                "refreshed": search.get("refreshed", False),
                "refresh_errors": search.get("refresh_errors", []),
                "candidate_match_ids": [candidate.get("match_id") for candidate in (search.get("results") or [])],
            }
        )
        best_score = float((candidates[0] if candidates else {}).get("match_score") or 0)
    if not provider_fixture and provider_date and best_score < 70:
        provider_search = search_service.search_provider_fixture(
            match_text,
            provider_date=provider_date,
            competition=provider_metadata.get("competition") or "",
            provider=provider_metadata.get("provider") or "",
            provider_competition_id=provider_metadata.get("provider_competition_id") or "",
            limit=5,
        )
        candidates = provider_search.get("results") or candidates
        resolver_trace.extend(provider_search.get("trace") or [])
    if not candidates:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "unmatched",
            "verdict": "unmatched",
            "message": "We could not find this fixture in the upcoming fixture cache or API-Football search window.",
            "fixture_resolution": {
                "status": "unmatched",
                "attempts": resolver_trace,
            },
            "possible_matches": [],
        }

    candidate = candidates[0]
    if float(candidate.get("match_score") or 0) < 70:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "ambiguous_match",
            "verdict": "unmatched",
            "message": "We found possible fixtures, but none were clear enough to analyse automatically.",
            "fixture_resolution": {
                "status": "ambiguous_match",
                "attempts": resolver_trace,
            },
            "possible_matches": candidates,
        }
    if not statpal_candidate:
        statpal_candidate = search_service.find_statpal_fixture_context(candidate)
        if statpal_candidate:
            resolver_trace.append(
                {
                    "strategy": "statpal_context_from_resolved_fixture",
                    "candidate_match_id": statpal_candidate.get("match_id"),
                    "provider_match_id": statpal_candidate.get("provider_match_id") or statpal_candidate.get("statpal_provider_match_id"),
                    "candidate_score": statpal_candidate.get("match_score"),
                }
            )
    if str(provider_metadata.get("provider") or "").lower() != "sportybet" or provider_fixture:
        search_service.learn_resolution(
            provider_metadata=provider_metadata,
            candidate=candidate,
            confidence=candidate.get("match_score"),
            method="provider_fixture_map" if provider_fixture else "team_date_league",
        )

    on_demand = None
    skip_core_on_demand = _market_can_skip_core_on_demand(market_descriptor)
    if skip_core_on_demand:
        game = manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
        if not game:
            if _should_skip_core_on_demand(
                market_descriptor,
                game=game,
                candidate=candidate,
                statpal_candidate=statpal_candidate,
                provider_metadata=provider_metadata,
            ):
                game = _minimal_game_from_candidate(candidate)
                on_demand = {
                    "status": "skipped",
                    "reason": "market_served_by_match_checker_advisory",
                    "market_family": market_descriptor.family,
                }
            else:
                effective_force_fresh = _consume_review_force_fresh(review_scoring_context)
                on_demand = algo_runner_service.score_cached_fixture_on_demand(
                    candidate["match_id"],
                    match_date=candidate.get("match_date"),
                    reason="slip_review_market_context",
                    force=effective_force_fresh,
                )
                game = manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
                if not game:
                    game = _minimal_game_from_candidate(candidate)
    elif force_fresh:
        effective_force_fresh = _consume_review_force_fresh(review_scoring_context)
        on_demand = algo_runner_service.score_cached_fixture_on_demand(
            candidate["match_id"],
            match_date=candidate.get("match_date"),
            reason="slip_review",
            force=effective_force_fresh,
        )
        game = manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
    else:
        game = manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)
        if not game and allow_on_demand_scoring:
            on_demand = algo_runner_service.score_cached_fixture_on_demand(
                candidate["match_id"],
                match_date=candidate.get("match_date"),
                reason="slip_review",
            )
            game = manual_fixture_game(candidate["match_id"], candidate["match_date"], request=request)

    if not game:
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "market_taxonomy": market_taxonomy,
            "status": "matched_unscored",
            "verdict": "pending_analysis",
            "message": "Fixture matched, but on-demand analysis could not produce market predictions yet.",
            "matched_fixture": candidate,
            "possible_matches": candidates,
            "on_demand_analysis": on_demand,
            "fixture_resolution": {
                "status": "matched_unscored",
                "attempts": resolver_trace,
            },
        }

    markets = game.get("markets") or []
    statpal_provider_match_id = ""
    statpal_provider_competition_id = provider_metadata.get("provider_competition_id") or ""
    if str(provider_metadata.get("provider") or "").lower() == "statpal":
        statpal_provider_match_id = provider_metadata.get("provider_event_id") or ""
    elif isinstance(statpal_candidate, dict):
        statpal_provider_match_id = statpal_candidate.get("provider_match_id") or str(statpal_candidate.get("match_id") or "").replace("statpal:", "", 1)
        statpal_provider_competition_id = statpal_candidate.get("provider_competition_id") or statpal_provider_competition_id
    statpal_home_team_id = (statpal_candidate.get("home_team_id") if isinstance(statpal_candidate, dict) else "") or game.get("statpal_home_team_id") or ""
    statpal_away_team_id = (statpal_candidate.get("away_team_id") if isinstance(statpal_candidate, dict) else "") or game.get("statpal_away_team_id") or ""
    scoring_game = {
        **game,
        "statpal_provider_match_id": statpal_provider_match_id,
        "statpal_provider_competition_id": statpal_provider_competition_id,
        "statpal_home_team_id": statpal_home_team_id,
        "statpal_away_team_id": statpal_away_team_id,
        "provider_merge": game.get("provider_merge") or {},
    }
    team_intelligence = team_intelligence_service.for_fixture(scoring_game)
    scoring_game["team_intelligence"] = team_intelligence
    matched_fixture_payload = _matched_fixture_with_statpal(
        candidate,
        scoring_game,
        statpal_candidate,
        provider_match_id=statpal_provider_match_id,
        provider_competition_id=statpal_provider_competition_id,
        home_team_id=statpal_home_team_id,
        away_team_id=statpal_away_team_id,
    )
    hydrator = hydration_cache or FixtureHydrator()
    statpal_bundle = hydrator.bundle_for(
        market_descriptor,
        match_id=(statpal_candidate.get("match_id") if isinstance(statpal_candidate, dict) and statpal_candidate.get("match_id") else candidate.get("match_id")),
        provider_match_id=statpal_provider_match_id,
        provider_competition_id=statpal_provider_competition_id,
        home_team_id=statpal_home_team_id,
        away_team_id=statpal_away_team_id,
    )
    statpal_refresh = statpal_bundle.get("refreshed") or {}
    statpal_context = statpal_bundle.get("context") or {}
    analysis_data_source = analysis_data_fallback_state(team_intelligence, statpal_context)
    statpal_context = {
        **statpal_context,
        "team_intelligence": team_intelligence,
        "analysis_data_source": analysis_data_source,
    }
    scoring_game["statpal_context"] = statpal_context
    scoring_game["analysis_data_source"] = analysis_data_source

    # Snapshot coverage is only the right yardstick for the StatPal advisory path;
    # matrix- and count-model markets are judged on the data that actually serves them.
    market_capability = capability_for_descriptor(
        market_descriptor, fixture=scoring_game, statpal_context=statpal_context
    )
    statpal_advisory = statpal_market_advisory.evaluate_market(
        market_descriptor,
        fixture=scoring_game,
        provider_payload=selection.get("provider_payload") or {},
        statpal_payload=selection.get("statpal_payload"),
    )
    market_capability = effective_market_capability(market_capability, statpal_advisory)
    generated_markets = _generated_match_checker_markets(
        market_descriptor,
        game=scoring_game,
        statpal_context=statpal_context,
        provider_payload=selection.get("provider_payload") or {},
        statpal_payload=selection.get("statpal_payload"),
    )
    canonical_requested_market = market_descriptor.canonical
    analysis_market = _market_for_fixture_orientation(canonical_requested_market, candidate)
    selected_market = next((market for market in markets if market_matches(analysis_market, market.get("market"))), None)
    blocked_recommendation_markets = []
    if not selected_market:
        submitted_market = _submitted_market_payload(
            requested_market=requested_market,
            market_taxonomy=market_taxonomy,
            statpal_advisory=statpal_advisory,
            market_capability=market_capability,
            odds=selection.get("odds"),
        )
        replacement_market = _replacement_market_for_slip(
            scoring_game,
            selected_market=submitted_market,
            generated_markets=generated_markets,
            allow_safer_fallback=True,
            blocked_markets_out=blocked_recommendation_markets,
        )
        verdict = _manual_verdict(submitted_market, replacement_market)
        # A market priced by a fitted model is not "not found" merely because the core
        # algo did not enumerate it. Since most families are now served by the score
        # matrix or the count models, that list is often empty by design — reporting
        # those legs as unmatched hid perfectly good assessments.
        model_served = _market_was_assessed(submitted_market)
        resolution_status = "analysed" if model_served else "market_not_found"
        return {
            "match": match_text,
            "submitted_market": requested_market,
            "provider_market_text": selection.get("provider_market_text") or requested_market,
            "canonical_market": _resolved_canonical_market(selection),
            "market_taxonomy": market_taxonomy,
            "analysis_market": analysis_market,
            "fixture_orientation": candidate.get("match_orientation", ""),
            "status": resolution_status,
            **verdict,
            "matched_fixture": matched_fixture_payload,
            "provider_merge": matched_fixture_payload.get("provider_merge") or {},
            "available_markets": [market.get("market") for market in markets],
            "selected_market": submitted_market,
            "best_market": game.get("best_market"),
            "recommended_market": game.get("recommended_market"),
            "replacement_market": replacement_market,
            "blocked_recommendation_markets": blocked_recommendation_markets,
            "generated_markets": generated_markets,
            "fixture_resolution": {
                "status": resolution_status,
                "attempts": resolver_trace,
            },
            "statpal_refresh": statpal_refresh,
            "statpal_context": statpal_context,
            "team_intelligence": team_intelligence,
            "analysis_data_source": analysis_data_source,
            "statpal_advisory": statpal_advisory,
            "market_capability": market_capability,
        }

    best_market = game.get("best_market") or game.get("top_market")
    recommended_market = game.get("recommended_market")
    selected_market = with_match_checker_advisory(selected_market)
    if selected_market:
        selected_market["market_taxonomy"] = market_taxonomy
        selected_market = with_statpal_advisory(selected_market, statpal_advisory)
        selected_market = with_market_capability(selected_market, market_capability)
    best_market = with_match_checker_advisory(best_market)
    recommended_market = with_match_checker_advisory(recommended_market)
    replacement_market = _replacement_market_for_slip(
        scoring_game,
        selected_market=selected_market,
        generated_markets=generated_markets,
        allow_safer_fallback=True,
        blocked_markets_out=blocked_recommendation_markets,
    )
    verdict = _manual_verdict(selected_market, replacement_market)
    return {
        "match": match_text,
        "submitted_market": requested_market,
        "provider_market_text": selection.get("provider_market_text") or requested_market,
        "canonical_market": _resolved_canonical_market(selection),
        "market_taxonomy": market_taxonomy,
        "analysis_market": analysis_market,
        "fixture_orientation": candidate.get("match_orientation", ""),
        "status": "analysed",
        **verdict,
        "matched_fixture": matched_fixture_payload,
        "provider_merge": matched_fixture_payload.get("provider_merge") or {},
        "selected_market": selected_market,
        "best_market": best_market,
        "recommended_market": recommended_market,
        "replacement_market": replacement_market,
        "blocked_recommendation_markets": blocked_recommendation_markets,
        "generated_markets": generated_markets,
        "statpal_refresh": statpal_refresh,
        "statpal_context": statpal_context,
        "team_intelligence": team_intelligence,
        "analysis_data_source": analysis_data_source,
        "statpal_advisory": statpal_advisory,
        "market_capability": market_capability,
        "possible_matches": candidates,
        "on_demand_analysis": on_demand,
        "fixture_resolution": {
            "status": "matched",
            "attempts": resolver_trace,
        },
    }


def fail_slip_review_import(review_id, message, *, error_code="failed", error_payload=None, release_tokens=True):
    review = SlipReview.objects.get(id=review_id)
    if release_tokens:
        _release_slip_review_token_reservation(review)
    review.status = SlipReview.Status.FAILED
    review.summary = _empty_slip_summary(
        message,
        task_id=(review.summary or {}).get("task_id", ""),
        error=message,
    )
    review.summary["error_code"] = error_code
    if error_payload:
        review.summary["error_payload"] = json_safe(error_payload)
    review.summary["progress"] = _slip_review_progress(
        phase="failed",
        message=message,
        error_code=error_code,
    )
    review.save(update_fields=["status", "summary", "updated_at"])
    _publish_slip_review_event(
        review,
        "review.failed",
        {
            "status": review.status,
            "error": message,
            "error_code": error_code,
            "error_payload": json_safe(error_payload or {}),
            "progress": review.summary.get("progress") or {},
        },
    )
    return api_response_payload(
        {
            "review_id": review.id,
            "status": _public_slip_review_status(review.status),
            "error": message,
            "error_code": error_code,
            "error_payload": json_safe(error_payload or {}),
            **(review.summary or {}),
        }
    )


class ManualSlipReviewView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ManualSlipReviewResponseSerializer

    @extend_schema(
        summary="Review manual match predictions",
        description=(
            "Authenticated user endpoint. Accepts manually typed matches and selected markets, matches each fixture "
            "against the upcoming fixture cache/API-Football fallback, and reviews the selected market using existing "
            "scored market analysis when available."
        ),
        tags=["Slip Reviews"],
        request=ManualSlipReviewRequestSerializer,
        responses={200: ManualSlipReviewResponseSerializer},
    )
    def post(self, request):
        serializer = ManualSlipReviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data.get("days", 3)
        selections = serializer.validated_data["selections"]
        review = _create_queued_slip_review(
            request.user,
            source=SlipReview.Source.MANUAL,
            submitted_payload=serializer.validated_data,
        )
        try:
            _reserve_slip_review_tokens(review, len(selections))
            _set_slip_review_progress(
                review,
                phase="analysing_legs",
                total=len(selections),
                completed=0,
                message=f"Analysing {len(selections)} selections.",
                status=SlipReview.Status.ANALYSING,
            )
            review_scoring_context = {"fixture_universe_synced": False}
            results = [
                _analyse_manual_selection(
                    selection,
                    days=days,
                    request=request,
                    force_fresh=True,
                    review_scoring_context=review_scoring_context,
                )
                for selection in selections
            ]
            summary, safe_results = _populate_slip_review(review, results)
            _consume_slip_review_token_reservation(review)
            review.save(update_fields=["summary", "submitted_payload", "updated_at"])
        except InsufficientTokens as exc:
            error_payload = insufficient_tokens_payload(exc, review_id=review.id, selection_count=len(selections))
            fail_payload = fail_slip_review_import(
                review.id,
                error_payload["message"],
                error_code="insufficient_tokens",
                error_payload=error_payload,
                release_tokens=False,
            )
            return Response(fail_payload, status=status.HTTP_402_PAYMENT_REQUIRED)
        except Exception:
            _release_slip_review_token_reservation(review)
            raise
        return Response(
            api_response_payload({
                "id": review.id,
                "source": review.source,
                "status": _public_slip_review_status(review.status),
                "public": summary.get("public", {}),
                **summary,
                "selections": safe_results,
            })
        )


class SportyBetSlipImportView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        summary="Import SportyBet slip",
        description=(
            "Authenticated user endpoint. Accepts a SportyBet share URL/code or raw share payload, imports the booked "
            "football selections asynchronously, matches them against cached fixtures, analyses each selected market, "
            "and saves the review. Returns a queued review immediately; poll the review detail endpoint until the "
            "status becomes completed or failed."
        ),
        tags=["Slip Reviews"],
        request=SportyBetSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = SportyBetSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = _create_queued_slip_review(
            request.user,
            source=SlipReview.Source.SPORTYBET,
            submitted_payload=data,
        )
        task = import_slip_review.apply_async(args=[review.id], queue=settings.SLIP_REVIEW_IMPORT_QUEUE)
        review.summary = {
            **_empty_slip_summary("Slip import queued.", task_id=task.id),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        }
        review.save(update_fields=["summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.queued",
            {
                "status": review.status,
                "task_id": task.id,
                "progress": review.summary.get("progress") or {},
            },
        )
        return Response(_slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)


class BetanoSlipImportView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        summary="Import Betano slip",
        description=(
            "Authenticated user endpoint. Accepts a Betano booking URL/code, opens it with the backend browser "
            "importer, captures the getbetslip payload, imports the booked football selections, matches them against "
            "cached fixtures, analyses each selected market, and saves the review asynchronously. A raw getbetslip "
            "payload can also be supplied as a fallback. Returns a queued review immediately; poll the review detail "
            "endpoint until the status becomes completed or failed."
        ),
        tags=["Slip Reviews"],
        request=BetanoSlipImportRequestSerializer,
        responses={202: SlipReviewDetailResponseSerializer},
    )
    def post(self, request):
        serializer = BetanoSlipImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = _create_queued_slip_review(
            request.user,
            source=SlipReview.Source.BETANO,
            submitted_payload=data,
        )
        task = import_slip_review.apply_async(args=[review.id], queue=settings.SLIP_REVIEW_IMPORT_QUEUE)
        review.summary = {
            **_empty_slip_summary("Slip import queued.", task_id=task.id),
            "progress": _slip_review_progress(
                phase="queued",
                message="Slip import queued.",
            ),
        }
        review.save(update_fields=["summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.queued",
            {
                "status": review.status,
                "task_id": task.id,
                "progress": review.summary.get("progress") or {},
            },
        )
        return Response(_slip_review_payload(review, include_selections=True), status=status.HTTP_202_ACCEPTED)




def slip_recap_payload(user, *, days):
    since = timezone.localdate() - timedelta(days=days)
    selections = list(
        SlipSelection.objects.filter(
            review__user=user,
            match_date__gte=since,
        ).only("outcome", "flagged_risky", "review_id")
    )

    wins = [item for item in selections if item.outcome == SlipSelection.Outcome.WIN]
    losses = [item for item in selections if item.outcome == SlipSelection.Outcome.LOSS]
    void = [item for item in selections if item.outcome == SlipSelection.Outcome.VOID]
    unsettleable = [item for item in selections if item.outcome == SlipSelection.Outcome.UNSETTLEABLE]
    pending = [item for item in selections if item.outcome == SlipSelection.Outcome.PENDING]

    flagged_wins = [item for item in wins if item.flagged_risky]
    flagged_losses = [item for item in losses if item.flagged_risky]
    unflagged_wins = [item for item in wins if not item.flagged_risky]
    unflagged_losses = [item for item in losses if not item.flagged_risky]

    ticket_count = len({item.review_id for item in selections})
    settled_count = len(wins) + len(losses)

    if not settled_count:
        message = "None of your selections in this window have been settled yet."
    else:
        message = (
            f"You submitted {ticket_count} {_plural(ticket_count, 'ticket')}. "
            f"{len(wins)} of {settled_count} settled {_plural(settled_count, 'selection')} were correct."
        )
        if losses:
            message += (
                f" {len(flagged_losses)} of the {len(losses)} that failed "
                f"{'was' if len(flagged_losses) == 1 else 'were'} flagged as risky before kickoff."
            )

    return {
        "contract_version": "match_checker_public_v2",
        "window": {"days": days, "from": since.isoformat(), "to": timezone.localdate().isoformat()},
        "tickets": ticket_count,
        "selections": {
            "total": len(selections),
            "settled": settled_count,
            "correct": len(wins),
            "failed": len(losses),
            "void": len(void),
            "unsettleable": len(unsettleable),
            "awaiting_result": len(pending),
        },
        "flagged": {
            "flagged_before_kickoff": len(flagged_wins) + len(flagged_losses),
            "failed_and_flagged": len(flagged_losses),
            "failed_and_not_flagged": len(unflagged_losses),
            "flagged_hit_rate_percent": _hit_rate(len(flagged_wins), len(flagged_losses)),
            "unflagged_hit_rate_percent": _hit_rate(len(unflagged_wins), len(unflagged_losses)),
        },
        "message": message,
    }




class SlipRepairView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipRepairResponseSerializer

    @extend_schema(
        summary="Repair a slip",
        description=(
            "Authenticated user endpoint. Builds a revised version of a reviewed slip by "
            "replacing or dropping selections the model cannot defend. Send `decisions` to "
            "accept or reject individual changes; omit it to apply every recommended change. "
            "A repaired ticket is an evidence-based alternative, not a guarantee, and it "
            "usually carries lower combined odds than the original."
        ),
        tags=["Slip Reviews"],
        request=SlipRepairRequestSerializer,
        responses={201: SlipRepairResponseSerializer},
    )
    def post(self, request, review_id):
        review = SlipReview.objects.filter(id=review_id, user=request.user).first()
        if review is None:
            return Response({"detail": "Slip review not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = SlipRepairRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submitted = serializer.validated_data.get("decisions") or []
        decisions = {item["index"]: item["action"] for item in submitted}

        items = [selection.analysis_payload or {} for selection in review.selections.all()]
        if not items:
            return Response(
                {"detail": "This review has no analysed selections to repair."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ticket_risk = ticket_risk_service.assess(items)
        plan = plan_repair(items, ticket_risk, decisions=decisions)
        repair = SlipRepair.objects.create(
            review=review,
            mode=SlipRepair.Mode.CUSTOM if decisions else SlipRepair.Mode.RECOMMENDED,
            original_legs=plan.original_legs,
            original_combined_odds=plan.original_combined_odds,
            original_success_percent=plan.original_success_percent,
            revised_legs=plan.revised_legs,
            revised_combined_odds=plan.revised_combined_odds,
            revised_success_percent=plan.revised_success_percent,
            changes=[decision.to_dict() for decision in plan.decisions],
        )
        return Response(_repair_payload(review, plan, repair), status=status.HTTP_201_CREATED)


class SlipReviewRandomizeView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewRandomizeResponseSerializer

    @extend_schema(
        summary="Build a smart randomized slip ticket",
        description=(
            "Authenticated user endpoint. After a slip review has completed, send the number of games "
            "the user wants in the generated ticket. The backend deterministically returns the strongest "
            "analysed picks from that slip; there are no modes or frontend-side ranking rules."
        ),
        tags=["Slip Reviews"],
        request=SlipReviewRandomizeRequestSerializer,
        responses={200: SlipReviewRandomizeResponseSerializer},
    )
    def post(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.prefetch_related("selections"),
            id=review_id,
            user=request.user,
        )
        if review.status in {
            SlipReview.Status.QUEUED,
            SlipReview.Status.IMPORTING,
            SlipReview.Status.ANALYSING,
        }:
            return Response(
                {
                    "detail": "Slip review is still being analysed.",
                    "status": _public_slip_review_status(review.status),
                    "progress": _public_slip_review_progress((review.summary or {}).get("progress") or {}),
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = SlipReviewRandomizeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        public_payload = _slip_review_payload(review, include_selections=True, public_only=True)
        ticket, error = _smart_randomize_ticket(public_payload, serializer.validated_data["games"])
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        token_cost = int(getattr(settings, "SLIP_REVIEW_RANDOMIZE_TOKEN_COST", 5))
        try:
            charge = token_wallet_service.charge_tokens(
                request.user,
                token_cost,
                reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE,
                reference_type="slip_review_randomize",
                reference_id=str(review.id),
                metadata={
                    "review_id": review.id,
                    "requested_games": serializer.validated_data["games"],
                    "source": review.source,
                },
            )
        except InsufficientTokens as exc:
            return Response(
                insufficient_feature_tokens_payload(
                    exc,
                    feature="smart_randomize",
                    token_cost=token_cost,
                    review_id=review.id,
                ),
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        ticket["billing"] = {
            "status": "charged",
            "token_cost": token_cost,
            "transaction_id": charge.transaction.id if charge.transaction else None,
            "wallet": charge.balance_after,
        }
        return Response(api_response_payload(ticket))


class SlipReviewRecapView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewRecapResponseSerializer

    @extend_schema(
        summary="Slip review recap",
        description=(
            "Authenticated user endpoint. Returns settled outcomes for the current user's slip selections over a "
            "recent window, including how many failed selections had been flagged as risky before kickoff. "
            "Selections whose market the settlement engine cannot resolve are reported separately as "
            "`unsettleable` and are excluded from hit rates."
        ),
        tags=["Slip Reviews"],
        parameters=[SlipReviewRecapQuerySerializer],
        responses={200: SlipReviewRecapResponseSerializer},
    )
    def get(self, request):
        query = SlipReviewRecapQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data.get("days") or 1
        return Response(slip_recap_payload(request.user, days=days))


class SlipReviewListView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewListResponseSerializer

    @extend_schema(
        operation_id="algo_slip_reviews_list",
        summary="List slip reviews",
        description=(
            "Authenticated user endpoint. Returns compact previous manual/bookmaker slip reviews for the current user: "
            "number of games, status, and each game's user pick vs AI pick."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewListResponseSerializer},
    )
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        view_mode = (request.query_params.get("view") or "compact").strip().lower()
        explicit_include_picks = str(
            request.query_params.get("include_picks", "")
        ).strip().lower() in {"1", "true", "yes"}
        include_picks = view_mode in {"compact", "full", "legacy"} or explicit_include_picks
        try:
            pick_limit = int(request.query_params.get("pick_limit", 2))
        except (TypeError, ValueError):
            pick_limit = 2
        pick_limit = max(0, min(pick_limit, 20))
        if include_picks and pick_limit == 0:
            include_picks = False

        selected_fields = ["id", "source", "status", "title", "submitted_payload", "created_at", "updated_at"]
        use_summary = include_picks and pick_limit is None
        if use_summary:
            selected_fields.append("summary")
        reviews_qs = (
            SlipReview.objects.filter(user=request.user)
            .annotate(selection_count=Count("selections"))
            .only(*selected_fields)
            .order_by("-created_at")
        )
        if include_picks and pick_limit is None:
            reviews_qs = reviews_qs.prefetch_related("selections")
        elif include_picks:
            reviews_qs = reviews_qs.prefetch_related(
                Prefetch(
                    "selections",
                    queryset=SlipSelection.objects.only(
                        "id",
                        "review_id",
                        "order",
                        "submitted_match",
                        "submitted_market",
                        "odds",
                        "status",
                        "verdict",
                        "advisory_score",
                        "analysis_payload",
                    ).order_by("order", "id"),
                    to_attr="preview_selections",
                )
            )
        reviews = list(reviews_qs[:limit])
        return Response(
            {
                "count": len(reviews),
                "reviews": [
                    _compact_slip_review_list_payload(
                        review,
                        include_picks=include_picks or pick_limit > 0,
                        pick_limit=pick_limit if pick_limit > 0 else None,
                        use_summary=use_summary,
                    )
                    for review in reviews
                ],
            }
        )


class SlipReviewOptionsView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewOptionsResponseSerializer

    @extend_schema(
        summary="Slip review frontend options",
        description="Authenticated user endpoint. Returns stable market dropdown options, verdict labels, source labels, and request limits for slip review screens.",
        tags=["Slip Reviews"],
        responses={200: SlipReviewOptionsResponseSerializer},
    )
    def get(self, request):
        return Response(
            {
                "markets": SLIP_REVIEW_MARKET_OPTIONS,
                "verdicts": SLIP_REVIEW_VERDICT_OPTIONS,
                "sources": [
                    {"value": "manual", "label": "Manual"},
                    {"value": "sportybet", "label": "SportyBet"},
                    {"value": "betano", "label": "Betano"},
                ],
                "limits": {
                    "manual_max_selections": 30,
                    "search_max_days": 14,
                    "search_default_days": 3,
                    "fixture_search_limit": 25,
                },
            }
        )


class SlipReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewDetailResponseSerializer

    @extend_schema(
        operation_id="algo_slip_reviews_retrieve",
        summary="Slip review detail",
        description=(
            "Authenticated user endpoint. Returns one previous slip review. Use `?view=public` for the "
            "frontend-ready bettor response; omit it for the full technical/internal payload."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewDetailResponseSerializer},
    )
    def get(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.prefetch_related("selections"),
            id=review_id,
            user=request.user,
        )
        public_only = str(request.query_params.get("view", "")).lower() == "public"
        return Response(_slip_review_payload(review, include_selections=True, public_only=public_only))


def _slip_review_event_payload(event):
    payload = dict(event.payload or {})
    if payload.get("status"):
        payload["status"] = _public_slip_review_status(payload["status"])
    if isinstance(payload.get("progress"), dict):
        payload["progress"] = _public_slip_review_progress(payload["progress"])
    return {
        "id": event.id,
        "review_id": event.review_id,
        "event_type": event.event_type,
        "payload": payload,
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }


def _public_slip_review_stream_event(event):
    event = dict(event or {})
    payload = dict(event.get("payload") or {})
    if payload.get("status"):
        payload["status"] = _public_slip_review_status(payload["status"])
    if isinstance(payload.get("progress"), dict):
        payload["progress"] = _public_slip_review_progress(payload["progress"])
    event["payload"] = payload
    return event


class SlipReviewEventsView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewEventsResponseSerializer

    @extend_schema(
        summary="Slip review realtime events",
        description=(
            "Authenticated user endpoint. Returns only slip-review events newer than `after_id`, plus the current "
            "progress snapshot. This is the HTTP fallback/reconnect path for the websocket stream."
        ),
        tags=["Slip Reviews"],
        parameters=[SlipReviewEventsQuerySerializer],
        responses={200: SlipReviewEventsResponseSerializer},
    )
    def get(self, request, review_id):
        query = SlipReviewEventsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        after_id = query.validated_data.get("after_id") or 0
        limit = query.validated_data.get("limit") or 100
        review = get_object_or_404(
            SlipReview.objects.only("id", "status", "summary", "updated_at"),
            id=review_id,
            user=request.user,
        )
        redis_snapshot = slip_review_redis.get_snapshot(review.id) or {}
        redis_events = slip_review_redis.get_events_after(review.id, after_id=after_id, limit=limit)
        # An empty list means Redis is reachable, not that this review has no events. The
        # stream is ephemeral, so a review whose events have expired -- or one populated by
        # a path that never pushed to Redis -- would otherwise report no progress at all
        # while its `SlipReviewEvent` rows sit in the database. Redis is authoritative only
        # when it actually holds something for this review.
        redis_knows_review = bool(redis_events) or bool(redis_snapshot)
        if redis_events is not None and redis_knows_review:
            events_payload = [_public_slip_review_stream_event(event) for event in redis_events]
            latest_event_id = (redis_snapshot or {}).get("latest_event_id")
            if latest_event_id is None and events_payload:
                latest_event_id = events_payload[-1].get("id")
        else:
            events = list(
                SlipReviewEvent.objects.filter(review=review, id__gt=after_id)
                .order_by("id")[:limit]
            )
            latest_event_id = (
                SlipReviewEvent.objects.filter(review=review).order_by("-id").values_list("id", flat=True).first()
            )
            events_payload = [_slip_review_event_payload(event) for event in events]
        payload = {
            "review_id": review.id,
            "status": _public_slip_review_status(review.status),
            "progress": _public_slip_review_progress(
                (redis_snapshot or {}).get("progress") or (review.summary or {}).get("progress") or {}
            ),
            "latest_event_id": latest_event_id,
            "events": events_payload,
        }
        response = Response(api_response_payload(payload))
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Authorization, Cookie"
        return response




class SlipReviewStreamTokenView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SlipReviewStreamTokenResponseSerializer

    @extend_schema(
        summary="Create slip review websocket ticket",
        description=(
            "Authenticated user endpoint. Mints a short-lived, review-scoped websocket ticket so the frontend "
            "does not put the real JWT access token in the websocket URL."
        ),
        tags=["Slip Reviews"],
        responses={200: SlipReviewStreamTokenResponseSerializer},
    )
    def post(self, request, review_id):
        review = get_object_or_404(
            SlipReview.objects.only("id", "user_id"),
            id=review_id,
            user=request.user,
        )
        now = timezone.now()
        expires_at = now + timedelta(seconds=max(60, SLIP_REVIEW_STREAM_TICKET_SECONDS))
        ticket = signing.dumps(
            {"user_id": request.user.id, "review_id": review.id},
            salt=SLIP_REVIEW_STREAM_TICKET_SALT,
        )
        SlipReviewStreamToken.objects.filter(expires_at__lt=now).delete()
        SlipReviewStreamToken.objects.create(
            review=review,
            user=request.user,
            token_hash=_stream_ticket_hash(ticket),
            expires_at=expires_at,
        )
        ws_path = f"/ws/slip-reviews/{review.id}/?ticket={ticket}"
        scheme = "wss" if request.is_secure() else "ws"
        ws_url = f"{scheme}://{request.get_host()}{ws_path}"
        return Response(
            api_response_payload(
                {
                    "ticket": ticket,
                    "expires_in": max(60, SLIP_REVIEW_STREAM_TICKET_SECONDS),
                    "expires_at": expires_at,
                    "ws_path": ws_path,
                    "ws_url": ws_url,
                }
            )
        )


# ---------------------------------------------------------------------------
# Slip-review pipeline.
#
# These run inside the Celery tasks rather than serving a request. They are
# here because the tasks import them from this module; they belong in
# services/ and move there once the task bodies are properly thinned.
# ---------------------------------------------------------------------------





def _streamed_slip_review_game_payload(review, index, result):
    try:
        summary = _manual_review_summary([result or {}])
        public_payload = _build_bettor_public_payload(
            review,
            summary.get("public") or {},
            enhance=False,
        )
        game = (public_payload.get("games") or [None])[0]
        recommended_pick = (public_payload.get("recommended_ticket") or {}).get("picks") or []
        return json_safe(
            {
                "index": index,
                "order": index + 1,
                "game": game,
                "recommended_pick": recommended_pick[0] if recommended_pick else None,
            }
        )
    except Exception:
        log.exception(
            "Slip review streamed game payload failed review=%s leg=%s",
            getattr(review, "id", None),
            index + 1,
        )
        return {
            "index": index,
            "order": index + 1,
            "game": None,
            "recommended_pick": None,
        }


def _slip_selection_defaults_from_analysis(item):
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
        "settlement_market": _settlement_market_for(item),
        "odds": decimal_or_none(_selection_original_odds(item)),
        "flagged_risky": _selection_flagged_risky(item),
        "advisory_score": float_or_none(
            item.get("advisory_score") or (item.get("selected_market") or {}).get("advisory_score")
        ),
    }


def _initial_slip_selection_payload(selection):
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


def _initialize_slip_selection_progress_rows(review, selections):
    review.selections.all().delete()
    rows = []
    for index, selection in enumerate(selections, start=1):
        payload = _initial_slip_selection_payload(selection)
        defaults = _slip_selection_defaults_from_analysis(payload)
        rows.append(SlipSelection(review=review, order=index, **defaults))
    if rows:
        SlipSelection.objects.bulk_create(rows, batch_size=100)


def _persist_slip_selection_progress_result(review, index, result):
    defaults = _slip_selection_defaults_from_analysis(result)
    updated = SlipSelection.objects.filter(review=review, order=index + 1).update(**defaults)
    if not updated:
        SlipSelection.objects.create(review=review, order=index + 1, **defaults)


def _create_slip_review(user, *, source, submitted_payload, results):
    review = SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.ANALYSING,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary=_empty_slip_summary("Slip analysis started."),
    )
    summary, safe_results = _populate_slip_review(review, results)
    return review, summary, safe_results




def _create_failed_slip_review(user, *, source, submitted_payload, error):
    summary = _empty_slip_summary("Slip import failed.", error=error)
    return SlipReview.objects.create(
        user=user,
        source=source,
        status=SlipReview.Status.FAILED,
        title=f"{source.title()} review",
        submitted_payload=json_safe(submitted_payload),
        summary=summary,
    )




def _slip_review_leg_failure_result(index, selection, message, *, error_code="analysis_failed"):
    provider_payload = json_safe((selection or {}).get("provider_payload") or {})
    public_message = _public_slip_review_error_message(error_code)
    return {
        "match": (selection or {}).get("match", ""),
        "submitted_market": (selection or {}).get("market", ""),
        "market_taxonomy": _selection_market_descriptor(selection or {}, (selection or {}).get("market", "")).to_dict(),
        "status": "analysis_failed",
        "verdict": "not_assessed",
        "message": public_message,
        "provider": (selection or {}).get("provider", ""),
        "provider_payload": provider_payload,
        "fixture_resolution": {
            "status": "analysis_failed",
            "attempts": [
                {
                    "strategy": "celery_leg_task",
                    "error_code": error_code,
                    "index": index,
                }
            ],
        },
        "possible_matches": [],
    }


def _slip_leg_analysis_cache_key(selection):
    selection = selection or {}
    provider_payload = selection.get("provider_payload") or {}
    provider_metadata = _provider_metadata(selection)
    descriptor = _selection_market_descriptor(selection, selection.get("market", ""))
    market_key = (
        getattr(descriptor, "code", "")
        or getattr(descriptor, "canonical", "")
        or selection.get("market")
        or ""
    )
    raw_key = {
        "provider": str(selection.get("provider") or provider_metadata.get("provider") or "").lower(),
        "provider_event_id": provider_metadata.get("provider_event_id") or "",
        "provider_competition_id": provider_metadata.get("provider_competition_id") or "",
        "provider_date": _provider_match_date(selection).isoformat() if _provider_match_date(selection) else "",
        "match": normalize_market_text(selection.get("match") or ""),
        "market": normalize_market_text(market_key),
        "odds": str(provider_payload.get("odds") or provider_payload.get("displayOdds") or ""),
        "market_id": str(provider_payload.get("marketId") or provider_payload.get("market_id") or ""),
        "outcome_id": str(provider_payload.get("outcomeId") or provider_payload.get("outcome_id") or ""),
        "specifier": str(provider_payload.get("specifier") or ""),
    }
    encoded = json.dumps(raw_key, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), raw_key


def _cached_slip_leg_payload(cached, cache_key, *, status="hit"):
    payload = dict(cached.payload or {})
    payload["analysis_cache"] = {
        "status": status,
        "cache_key": cache_key,
        "updated_at": cached.updated_at.isoformat() if cached.updated_at else "",
        "expires_at": cached.expires_at.isoformat() if cached.expires_at else "",
    }
    return payload


def _get_or_lock_slip_leg_analysis_cache(selection):
    cache_key, raw_key = _slip_leg_analysis_cache_key(selection)
    now = timezone.now()
    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if (
        cached
        and cached.status == SlipLegAnalysisCache.Status.READY
        and cached.expires_at > now
        and cached.payload
    ):
        return _cached_slip_leg_payload(cached, cache_key), cache_key, raw_key, False

    lock_until = now + timedelta(seconds=max(30, SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS))
    expires_at = now + timedelta(seconds=max(60, SLIP_REVIEW_LEG_CACHE_TTL_SECONDS))
    if not cached:
        try:
            SlipLegAnalysisCache.objects.create(
                cache_key=cache_key,
                status=SlipLegAnalysisCache.Status.PROCESSING,
                source=raw_key.get("provider") or "",
                provider_event_id=raw_key.get("provider_event_id") or "",
                match_text=(selection or {}).get("match") or "",
                market_text=(selection or {}).get("market") or "",
                payload={},
                expires_at=expires_at,
                lock_expires_at=lock_until,
            )
            return None, cache_key, raw_key, True
        except IntegrityError:
            cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()

    now = timezone.now()
    if cached and cached.status == SlipLegAnalysisCache.Status.PROCESSING and cached.lock_expires_at and cached.lock_expires_at > now:
        deadline = time.monotonic() + max(0, SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS)
        while time.monotonic() < deadline:
            time.sleep(1)
            cached.refresh_from_db()
            if cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
                return _cached_slip_leg_payload(cached, cache_key, status="wait_hit"), cache_key, raw_key, False

    updated = SlipLegAnalysisCache.objects.filter(
        cache_key=cache_key,
    ).filter(
        Q(lock_expires_at__lte=timezone.now()) | Q(lock_expires_at__isnull=True) | Q(status__in=[
            SlipLegAnalysisCache.Status.READY,
            SlipLegAnalysisCache.Status.FAILED,
        ])
    ).update(
        status=SlipLegAnalysisCache.Status.PROCESSING,
        lock_expires_at=lock_until,
        expires_at=expires_at,
    )
    if updated:
        return None, cache_key, raw_key, True

    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if cached and cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
        return _cached_slip_leg_payload(cached, cache_key, status="late_hit"), cache_key, raw_key, False
    return None, cache_key, raw_key, True


def _store_slip_leg_analysis_cache(selection, result, *, cache_key=None, raw_key=None):
    result = json_safe(result or {})
    if result.get("status") not in {"analysed", "market_not_found", "insufficient_data"}:
        return
    cache_key = cache_key or _slip_leg_analysis_cache_key(selection)[0]
    raw_key = raw_key or _slip_leg_analysis_cache_key(selection)[1]
    matched = result.get("matched_fixture") or {}
    expires_at = timezone.now() + timedelta(seconds=max(60, SLIP_REVIEW_LEG_CACHE_TTL_SECONDS))
    SlipLegAnalysisCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "status": SlipLegAnalysisCache.Status.READY,
            "source": raw_key.get("provider") or "",
            "provider_event_id": raw_key.get("provider_event_id") or "",
            "match_text": result.get("match") or (selection or {}).get("match") or "",
            "market_text": result.get("submitted_market") or (selection or {}).get("market") or "",
            "match_id": matched.get("match_id") or "",
            "payload": result,
            "expires_at": expires_at,
            "lock_expires_at": None,
        },
    )


def _mark_slip_leg_analysis_cache_failed(selection, *, cache_key=None):
    cache_key = cache_key or _slip_leg_analysis_cache_key(selection)[0]
    SlipLegAnalysisCache.objects.filter(cache_key=cache_key).update(
        status=SlipLegAnalysisCache.Status.FAILED,
        lock_expires_at=None,
        expires_at=timezone.now() + timedelta(seconds=60),
    )


def process_slip_review_leg_failure(review_id, index, selection, message, *, error_code="analysis_failed"):
    review = SlipReview.objects.get(id=review_id)
    _mark_slip_leg_analysis_cache_failed(selection)
    result = _slip_review_leg_failure_result(index, selection, message, error_code=error_code)
    _persist_slip_selection_progress_result(review, index, result)
    total = review.selections.count()
    completed = _slip_review_completed_leg_count(review)
    _set_slip_review_progress(
        review,
        phase="analysing_legs",
        total=total,
        completed=completed,
        message=f"Analysed {completed} of {total} selections.",
        last_completed_match=result.get("match") or (selection or {}).get("match"),
        last_error=result.get("message") or _public_slip_review_error_message(error_code),
    )
    _publish_slip_review_event(
        review,
        "leg.failed",
        {
            "index": index,
            "order": index + 1,
            "match": result.get("match") or (selection or {}).get("match"),
            "market": result.get("submitted_market") or (selection or {}).get("market"),
            "game": _streamed_slip_review_game_payload(review, index, result).get("game"),
            "error": result.get("message") or _public_slip_review_error_message(error_code),
            "error_code": error_code,
            "completed": completed,
            "total": total,
        },
    )
    log.warning(
        "Slip review leg failed review=%s leg=%s match=%r market=%r error_code=%s error=%s",
        review_id,
        index + 1,
        (selection or {}).get("match"),
        (selection or {}).get("market"),
        error_code,
        message,
    )
    return {"review_id": review_id, "index": index, "status": "failed", "result": result, "error": result.get("message", "")}


def process_slip_review_leg_analysis(review_id, index, selection, *, days=3):
    review = SlipReview.objects.get(id=review_id)
    SlipSelection.objects.filter(review=review, order=index + 1).update(
        status="analysing",
        message="Analysing this selection.",
        analysis_payload={
            **_initial_slip_selection_payload(selection or {}),
            "status": "analysing",
            "message": "Analysing this selection.",
        },
    )
    total = review.selections.count()
    _publish_slip_review_event(
        review,
        "leg.started",
        {
            "index": index,
            "order": index + 1,
            "match": (selection or {}).get("match"),
            "market": (selection or {}).get("market"),
            "completed": _slip_review_completed_leg_count(review),
            "total": total,
        },
    )
    cached, cache_key, raw_key, owns_cache_lock = _get_or_lock_slip_leg_analysis_cache(selection)
    if cached:
        cached["provider"] = review.source
        cached["provider_payload"] = json_safe((selection or {}).get("provider_payload") or {})
        _persist_slip_selection_progress_result(review, index, cached)
        total = review.selections.count()
        completed = _slip_review_completed_leg_count(review)
        _set_slip_review_progress(
            review,
            phase="analysing_legs",
            total=total,
            completed=completed,
            message=f"Analysed {completed} of {total} selections.",
            last_completed_match=cached.get("match") or (selection or {}).get("match"),
            cache_status="hit",
        )
        _publish_slip_review_event(
            review,
            "leg.completed",
            {
                "index": index,
                "order": index + 1,
                "match": cached.get("match") or (selection or {}).get("match"),
                "market": cached.get("submitted_market") or (selection or {}).get("market"),
                "status": cached.get("status"),
                "verdict": cached.get("verdict"),
                "cache_status": (cached.get("analysis_cache") or {}).get("status") or "hit",
                **_streamed_slip_review_game_payload(review, index, cached),
                "completed": completed,
                "total": total,
            },
        )
        log.info(
            "Slip review leg cache hit review=%s leg=%s match=%r market=%r cache_key=%s",
            review_id,
            index + 1,
            (selection or {}).get("match"),
            (selection or {}).get("market"),
            cache_key,
        )
        return {
            "review_id": review_id,
            "index": index,
            "status": "cache_hit",
            "result": cached,
            "hydration": {
                "calls_used": 0,
                "served_from_cache": 1,
                "served_from_snapshot_cache": 0,
                "snapshot_cache_misses": 0,
                "served_by_model": 0,
                "fixtures_hydrated": 0,
                "budget_exhausted": False,
            },
        }
    hydrator = FixtureHydrator()
    try:
        result = _analyse_manual_selection(
            selection or {},
            days=days,
            request=None,
            force_fresh=True,
            hydration_cache=hydrator,
            review_scoring_context={"fixture_universe_synced": index > 0},
            allow_on_demand_scoring=True,
        )
    except Exception:
        if owns_cache_lock:
            _mark_slip_leg_analysis_cache_failed(selection, cache_key=cache_key)
        raise
    result["provider"] = review.source
    result["provider_payload"] = json_safe((selection or {}).get("provider_payload") or {})
    result["analysis_cache"] = {"status": "miss", "cache_key": cache_key}
    if owns_cache_lock:
        _store_slip_leg_analysis_cache(selection, result, cache_key=cache_key, raw_key=raw_key)
    _persist_slip_selection_progress_result(review, index, result)
    total = review.selections.count()
    completed = _slip_review_completed_leg_count(review)
    _set_slip_review_progress(
        review,
        phase="analysing_legs",
        total=total,
        completed=completed,
        message=f"Analysed {completed} of {total} selections.",
        last_completed_match=result.get("match") or (selection or {}).get("match"),
    )
    _publish_slip_review_event(
        review,
        "leg.completed",
        {
            "index": index,
            "order": index + 1,
            "match": result.get("match") or (selection or {}).get("match"),
            "market": result.get("submitted_market") or (selection or {}).get("market"),
            "status": result.get("status"),
            "verdict": result.get("verdict"),
            "cache_status": "miss",
            **_streamed_slip_review_game_payload(review, index, result),
            "completed": completed,
            "total": total,
        },
    )
    log.info(
        "Slip review leg analysed review=%s leg=%s match=%r status=%s hydration=%s",
        review_id,
        index + 1,
        result.get("match") or (selection or {}).get("match"),
        result.get("status"),
        hydrator.stats.to_dict(),
    )
    return {
        "review_id": review_id,
        "index": index,
        "status": "analysed",
        "result": result,
        "hydration": hydrator.stats.to_dict(),
    }


def process_slip_review_import(review_id):
    review = SlipReview.objects.get(id=review_id)
    payload = review.submitted_payload or {}
    review.summary = {
        **(review.summary or {}),
        **_empty_slip_summary("Importing slip selections.", task_id=(review.summary or {}).get("task_id", "")),
    }
    _set_slip_review_progress(
        review,
        phase="importing",
        message="Importing slip selections.",
        status=SlipReview.Status.IMPORTING,
    )

    try:
        if review.source == SlipReview.Source.SPORTYBET:
            imported = SportyBetShareImporter().import_share(
                url=payload.get("url"),
                code=payload.get("code"),
                payload=payload.get("payload"),
            )
        elif review.source == SlipReview.Source.BETANO:
            imported = BetanoBetslipImporter().import_betslip(
                url=payload.get("url"),
                code=payload.get("code"),
                payload=payload.get("payload"),
            )
        else:
            raise ValueError(f"Unsupported async slip source: {review.source}")

        review.summary = {
            **(review.summary or {}),
            **_empty_slip_summary("Analysing imported selections.", task_id=(review.summary or {}).get("task_id", "")),
        }

        selections = [
            {
                "match": item.get("match", ""),
                "market": item.get("market", ""),
                "provider": review.source,
                "provider_payload": item,
            }
            for item in imported.get("selections") or []
            if item.get("match") and item.get("market")
        ]
        if not selections:
            raise ValueError("No supported football selections were found in this slip.")
        _reserve_slip_review_tokens(review, len(selections))
        _initialize_slip_selection_progress_rows(review, selections)
        _set_slip_review_progress(
            review,
            phase="analysing_legs",
            total=len(selections),
            completed=0,
            message=f"Imported {len(selections)} selections. Analysing each leg.",
            status=SlipReview.Status.ANALYSING,
        )
        plan = plan_slip_hydration(selections)
        log.info(
            "Slip hydration plan review=%s legs=%s fixtures=%s needing_snapshots=%s served_by_model=%s estimated_snapshot_calls=%s fanout=True",
            review.id,
            plan["legs"],
            plan["distinct_fixtures"],
            plan["fixtures_needing_snapshots"],
            plan["fixtures_served_by_model"],
            plan.get("estimated_snapshot_calls"),
        )
        final_payload = json_safe(
            {
                **(review.submitted_payload or payload),
                "provider_code": imported.get("share_code") or imported.get("booking_code") or "",
                "selection_count": imported.get("selection_count", 0),
                "fanout_analysis": True,
            }
        )
        review.submitted_payload = final_payload
        review.save(update_fields=["submitted_payload", "summary", "updated_at"])

        from celery import chord as celery_chord

        from betpreneur.modules.slips.tasks import (
            analyse_slip_review_leg,
            finalize_slip_review_import,
        )

        workflow = celery_chord(
            [
                analyse_slip_review_leg.s(review.id, index, json_safe(selection), payload.get("days", 3))
                .set(queue=settings.SLIP_REVIEW_LEG_QUEUE)
                for index, selection in enumerate(selections)
            ]
        )(finalize_slip_review_import.s(review.id).set(queue=settings.SLIP_REVIEW_FINALIZE_QUEUE))
        _publish_slip_review_event(
            review,
            "review.fanout_queued",
            {
                "total": len(selections),
                "fanout_task_id": getattr(workflow, "id", ""),
            },
        )
        log.info(
            "Slip review fanout queued review=%s legs=%s chord_task_id=%s",
            review.id,
            len(selections),
            getattr(workflow, "id", ""),
        )
        return api_response_payload(
            {
                "review_id": review.id,
                "status": _public_slip_review_status(review.status),
                "fanout_task_id": getattr(workflow, "id", ""),
                **(review.summary or {}),
            }
        )
    except InsufficientTokens as exc:
        selection_count = len(locals().get("selections") or [])
        error_payload = insufficient_tokens_payload(exc, review_id=review.id, selection_count=selection_count)
        log.info(
            "Slip review insufficient tokens review=%s required=%s available=%s selections=%s",
            review.id,
            error_payload["required_tokens"],
            error_payload["available_tokens"],
            selection_count,
        )
        return fail_slip_review_import(
            review.id,
            error_payload["message"],
            error_code="insufficient_tokens",
            error_payload=error_payload,
            release_tokens=False,
        )
    except Exception:
        log.exception("Slip review import failed review=%s", review.id)
        _release_slip_review_token_reservation(review)
        public_error = _public_slip_review_error_message("failed")
        review.status = SlipReview.Status.FAILED
        review.summary = _empty_slip_summary("Slip import failed.", task_id=(review.summary or {}).get("task_id", ""), error=public_error)
        review.summary["progress"] = _slip_review_progress(
            phase="failed",
            message="Slip review failed.",
            error=public_error,
        )
        review.save(update_fields=["status", "summary", "updated_at"])
        _publish_slip_review_event(
            review,
            "review.failed",
            {
                "status": review.status,
                "error": public_error,
                "progress": review.summary.get("progress") or {},
            },
        )
        raise


def finalize_slip_review_import_results(review_id, leg_results):
    review = SlipReview.objects.get(id=review_id)
    payload = review.submitted_payload or {}
    ordered = sorted(
        [item for item in leg_results or [] if isinstance(item, dict)],
        key=lambda item: int(item.get("index") or 0),
    )
    results = [item.get("result") for item in ordered if isinstance(item.get("result"), dict)]
    hydration_totals = {
        "calls_used": 0,
        "served_from_cache": 0,
        "served_from_snapshot_cache": 0,
        "snapshot_cache_misses": 0,
        "served_by_model": 0,
        "fixtures_hydrated": 0,
        "budget_exhausted": False,
    }
    for item in ordered:
        stats = item.get("hydration") or {}
        for key in (
            "calls_used",
            "served_from_cache",
            "served_from_snapshot_cache",
            "snapshot_cache_misses",
            "served_by_model",
            "fixtures_hydrated",
        ):
            hydration_totals[key] += int(stats.get(key) or 0)
        hydration_totals["budget_exhausted"] = bool(hydration_totals["budget_exhausted"] or stats.get("budget_exhausted"))

    log.info("Slip hydration done review=%s %s", review.id, hydration_totals)
    if not results:
        raise ValueError("No supported football selections were found in this slip.")

    summary, safe_results = _populate_slip_review(review, results)
    summary["progress"] = _slip_review_progress(
        phase="completed",
        total=len(results),
        completed=len(results),
        message="Slip review completed.",
        final_status=_public_slip_review_status(review.status),
    )
    review.summary = summary
    _consume_slip_review_token_reservation(review)
    final_payload = json_safe({**payload, "fanout_analysis": True})
    review.submitted_payload = final_payload
    final_updated_at = timezone.now()
    SlipReview.objects.filter(id=review.id).update(
        status=review.status,
        summary=review.summary,
        submitted_payload=final_payload,
        updated_at=final_updated_at,
    )
    review.updated_at = final_updated_at
    log.info(
        "Slip review final persisted review=%s status=%s fanout=True selections=%s",
        review.id,
        review.status,
        len(results),
    )
    _publish_slip_review_event(
        review,
        "review.completed",
        {
            "status": _public_slip_review_status(review.status),
            "total": len(results),
            "completed": len(results),
            "progress": summary.get("progress") or {},
        },
    )
    return api_response_payload({"review_id": review.id, "status": _public_slip_review_status(review.status), **summary})




def recover_stale_slip_reviews(*, stale_after_seconds=None, limit=25):
    stale_after_seconds = int(stale_after_seconds or SLIP_REVIEW_STALE_AFTER_SECONDS)
    cutoff = timezone.now() - timedelta(seconds=max(60, stale_after_seconds))
    candidates = list(
        SlipReview.objects.filter(
            status__in=[
                SlipReview.Status.QUEUED,
                SlipReview.Status.IMPORTING,
                SlipReview.Status.ANALYSING,
            ],
            updated_at__lt=cutoff,
        )
        .prefetch_related("selections")
        .order_by("updated_at")[: max(1, int(limit or 25))]
    )
    recovered = failed = skipped = 0
    results = []
    for review in candidates:
        progress = (review.summary or {}).get("progress") or {}
        total = review.selections.count() or int(progress.get("total") or 0)
        persisted_leg_results = _leg_results_from_persisted_slip_selections(review)
        completed = len(persisted_leg_results)
        try:
            if persisted_leg_results:
                finalize_slip_review_import_results(review.id, persisted_leg_results)
                recovered += 1
                outcome = "finalized_from_persisted_legs"
            else:
                fail_slip_review_import(
                    review.id,
                    "Slip review did not finish in time. Please retry the slip review.",
                    error_code="stale_review_timeout",
                )
                failed += 1
                outcome = "failed_stale_without_completed_legs"
            results.append(
                {
                    "review_id": review.id,
                    "previous_status": review.status,
                    "outcome": outcome,
                    "completed": completed,
                    "total": total,
                }
            )
            log.warning(
                "Slip review stale recovery review=%s outcome=%s completed=%s total=%s stale_after_seconds=%s",
                review.id,
                outcome,
                completed,
                total,
                stale_after_seconds,
            )
        except Exception as exc:
            skipped += 1
            results.append(
                {
                    "review_id": review.id,
                    "previous_status": review.status,
                    "outcome": "recovery_failed",
                    "error": str(exc)[:300],
                    "completed": completed,
                    "total": total,
                }
            )
            log.exception("Slip review stale recovery failed review=%s", review.id)
    return {
        "considered": len(candidates),
        "recovered": recovered,
        "failed": failed,
        "skipped": skipped,
        "stale_after_seconds": stale_after_seconds,
        "results": results,
    }
