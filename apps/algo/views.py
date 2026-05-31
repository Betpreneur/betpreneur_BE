from datetime import timedelta
import csv
import logging

from celery.result import AsyncResult
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AlgoRun, MarketPrediction, Pick, PickBack
from .performance import (
    add_pick,
    confidence_band,
    empty_stats,
    finalize_stats,
    latest_audited_picks,
    odds_band,
)
from .serializers import (
    AlgoRunCreateSerializer,
    AlgoRunSerializer,
    AuditorRunSerializer,
    BackedPicksQuerySerializer,
    BackedPicksResponseSerializer,
    BulkPickBackRequestSerializer,
    BulkPickBackResponseSerializer,
    DailyPicksQuerySerializer,
    DailyPicksResponseSerializer,
    GameAnalysisQuerySerializer,
    GameDetailResponseSerializer,
    GameListResponseSerializer,
    MarketHealthQuerySerializer,
    MarketHealthResponseSerializer,
    PickSerializer,
    PickBackResponseSerializer,
    PickDetailResponseSerializer,
    PublicSummarySerializer,
    RecordResponseSerializer,
    RecordQuerySerializer,
    ResultsUpdateSerializer,
    TaskQueuedSerializer,
    TaskStatusSerializer,
    TopPickResponseSerializer,
)
from .tasks import generate_daily_picks, run_monthly_auditor, settle_daily_results


log = logging.getLogger(__name__)
SETTLED_PICK_STATUSES = [Pick.Status.WIN, Pick.Status.LOSS, Pick.Status.VOID]
PICK_DETAIL_HISTORY_DAYS = 90
PICK_TIER_RANK = {
    Pick.Tier.BANKER: 3,
    Pick.Tier.VALUE_GEM: 2,
    Pick.Tier.WILD_CARD: 1,
}
EXCLUDED_MARKETS = {"DC: 1X", "DC: X2"}


def _effective_pick_tier(pick):
    confidence = pick.confidence or 0
    if confidence >= 80:
        return Pick.Tier.BANKER
    if 70 <= confidence < 80:
        return Pick.Tier.VALUE_GEM
    if 60 <= confidence < 70:
        return Pick.Tier.WILD_CARD
    return pick.tier


def _performance_summary(queryset, window_days):
    if not hasattr(queryset, "aggregate"):
        return _performance_summary_from_picks(queryset, window_days)

    aggregate = queryset.aggregate(
        wins=Count("id", filter=Q(status=Pick.Status.WIN)),
        losses=Count("id", filter=Q(status=Pick.Status.LOSS)),
        voids=Count("id", filter=Q(status=Pick.Status.VOID)),
        pending=Count("id", filter=Q(status=Pick.Status.PENDING)),
        stake=Sum("stake", filter=Q(status__in=[Pick.Status.WIN, Pick.Status.LOSS])),
        pnl=Sum("pnl", filter=Q(status__in=[Pick.Status.WIN, Pick.Status.LOSS])),
    )
    wins = aggregate["wins"] or 0
    losses = aggregate["losses"] or 0
    settled = wins + losses
    stake = float(aggregate["stake"] or 0)
    pnl = float(aggregate["pnl"] or 0)
    return {
        "hit_rate": round((wins / settled) * 100, 1) if settled else 0.0,
        "roi_flat": round((pnl / stake) * 100, 1) if stake else 0.0,
        "picks_logged": queryset.count(),
        "wins": wins,
        "losses": losses,
        "voids": aggregate["voids"] or 0,
        "pending": aggregate["pending"] or 0,
        "window_days": window_days,
    }


def _performance_summary_from_picks(picks, window_days):
    wins = sum(1 for pick in picks if pick.status == Pick.Status.WIN)
    losses = sum(1 for pick in picks if pick.status == Pick.Status.LOSS)
    voids = sum(1 for pick in picks if pick.status == Pick.Status.VOID)
    pending = sum(1 for pick in picks if pick.status == Pick.Status.PENDING)
    settled = wins + losses
    stake = sum(float(pick.stake or 0) for pick in picks if pick.status in [Pick.Status.WIN, Pick.Status.LOSS])
    pnl = sum(float(pick.pnl or 0) for pick in picks if pick.status in [Pick.Status.WIN, Pick.Status.LOSS])
    return {
        "hit_rate": round((wins / settled) * 100, 1) if settled else 0.0,
        "roi_flat": round((pnl / stake) * 100, 1) if stake else 0.0,
        "picks_logged": len(picks),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "pending": pending,
        "window_days": window_days,
    }


def _dedupe_latest_public_picks(picks):
    latest = {}
    for pick in picks:
        key = (
            pick.match_date or pick.run.target_date,
            str(pick.match_id or "").strip(),
            pick.fixture,
            pick.market,
        )
        if key not in latest:
            latest[key] = pick
    return list(latest.values())


def _public_record_pick_payload(pick):
    return {
        "id": pick.id,
        "posted_at": pick.created_at,
        "match_date": pick.match_date or pick.run.target_date,
        "fixture": pick.fixture,
        "home_team": pick.home_team,
        "away_team": pick.away_team,
        "league": pick.league,
        "kickoff": pick.kickoff,
        "tier": pick.tier,
        "market": pick.market,
        "pick": pick.meaning or pick.market,
        "confidence": pick.confidence,
        "odds": pick.odds,
        "stake": pick.stake,
        "status": pick.status,
        "score": pick.score,
        "pnl": pick.pnl,
        "settled_at": pick.settled_at,
    }


def _latest_successful_run(target_date):
    return (
        AlgoRun.objects.filter(target_date=target_date, status=AlgoRun.Status.SUCCESS)
        .prefetch_related("picks")
        .order_by("-created_at")
        .first()
    )


def _top_pick_sort_key(pick):
    return (
        PICK_TIER_RANK.get(_effective_pick_tier(pick), 0),
        pick.confidence or 0,
        float(pick.ev or 0),
        float(pick.odds or 0),
    )


def _pick_identity_key(pick):
    return (
        pick.match_date or pick.run.target_date,
        str(pick.match_id or "").strip(),
        pick.fixture,
        pick.market,
    )


def _fixture_summary_for_pick(pick):
    for item in (pick.run.result or {}).get("fixture_summaries", []):
        if str(item.get("match_id")) == str(pick.match_id):
            return item
    return {}


def _market_sort_value(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("eligible") else 0,
        market.get("confidence") or 0,
        market.get("ev") if market.get("ev") is not None else -999,
        market.get("odds") or 0,
    )


def _game_market_rank(market):
    return (
        1 if market.get("selected") else 0,
        1 if market.get("eligible") else 0,
        market.get("confidence") or 0,
        market.get("ev") if market.get("ev") is not None else -999,
        market.get("odds") or 0,
    )


def _fixture_group_sort_key(item):
    return (
        (item.get("country") or "World").lower(),
        (item.get("league") or item.get("competition") or "").lower(),
        item.get("kickoff") or "",
        (item.get("fixture") or "").lower(),
    )


def _group_by_country_and_league(items, item_label):
    countries = {}
    for item in items:
        country = item.get("country") or "World"
        league = item.get("league") or "Other"
        country_bucket = countries.setdefault(country, {})
        country_bucket.setdefault(league, []).append(item)

    grouped = []
    for country in sorted(countries):
        leagues = []
        for league in sorted(countries[country]):
            league_items = sorted(countries[country][league], key=_fixture_group_sort_key)
            leagues.append({
                "league": league,
                "competition": league,
                "count": len(league_items),
                item_label: league_items,
            })
        grouped.append({
            "country": country,
            "count": sum(league["count"] for league in leagues),
            "leagues": leagues,
        })
    return grouped


def _tier_for_confidence(confidence):
    confidence = confidence or 0
    if confidence >= 80:
        return Pick.Tier.BANKER
    if 70 <= confidence < 80:
        return Pick.Tier.VALUE_GEM
    if 60 <= confidence < 70:
        return Pick.Tier.WILD_CARD
    return "watchlist"


def _normalise_fixture_markets(item, picks_by_match):
    markets = []
    match_picks = picks_by_match.get(str(item.get("match_id") or ""), [])
    pick_by_market = {pick.market: pick for pick in match_picks}
    for market in item.get("markets") or []:
        if market.get("market") in EXCLUDED_MARKETS:
            continue
        payload = dict(market)
        payload["suggested_tier"] = _tier_for_confidence(payload.get("confidence"))
        selected_pick = pick_by_market.get(payload.get("market"))
        if selected_pick:
            payload["selected"] = True
            payload["selected_pick_id"] = selected_pick.id
            payload["selected_tier"] = _effective_pick_tier(selected_pick)
        else:
            payload.setdefault("selected", False)
            payload.setdefault("selected_pick_id", None)
            payload.setdefault("selected_tier", "")
        markets.append(payload)
    return sorted(markets, key=_game_market_rank, reverse=True)


def _game_summary_from_fixture(item, picks_by_match, request=None, include_markets=False):
    match_id = str(item.get("match_id") or "")
    markets = _normalise_fixture_markets(item, picks_by_match)
    match_picks = sorted(picks_by_match.get(match_id, []), key=_top_pick_sort_key, reverse=True)
    pick_data = PickSerializer(match_picks, many=True, context={"request": request}).data
    top_market = markets[0] if markets else None
    official_pick = pick_data[0] if pick_data else None

    payload = {
        "fixture": item.get("fixture", ""),
        "home_team": item.get("home_team", ""),
        "away_team": item.get("away_team", ""),
        "home_logo": item.get("home_logo", ""),
        "away_logo": item.get("away_logo", ""),
        "teams": {
            "home": {
                "name": item.get("home_team", ""),
                "logo": item.get("home_logo", ""),
            },
            "away": {
                "name": item.get("away_team", ""),
                "logo": item.get("away_logo", ""),
            },
        },
        "league": item.get("league", ""),
        "league_logo": item.get("league_logo", ""),
        "competition_logo": item.get("league_logo", ""),
        "country": item.get("country", ""),
        "country_flag": item.get("country_flag", ""),
        "competition": item.get("league", ""),
        "competition_info": {
            "name": item.get("league", ""),
            "logo": item.get("league_logo", ""),
            "country": item.get("country", ""),
            "country_flag": item.get("country_flag", ""),
        },
        "round": item.get("round", ""),
        "league_type": item.get("league_type", ""),
        "kickoff": item.get("kickoff", ""),
        "match_id": match_id,
        "published": bool(match_picks),
        "official_pick_count": len(match_picks),
        "official_pick": official_pick,
        "official_picks": pick_data,
        "top_market": top_market,
        "market_count": item.get("market_count", len(markets)),
        "eligible_market_count": sum(1 for market in markets if market.get("eligible")),
        "markets_70_plus": item.get("markets_70_plus", 0),
        "markets_65_plus": item.get("markets_65_plus", 0),
        "home_recent_form": _recent_form_payload(item.get("home_recent_form", {})),
        "away_recent_form": _recent_form_payload(item.get("away_recent_form", {})),
        "fixture_context": item.get("fixture_context", {}),
        "team_news": item.get("team_news", {}),
        "corner_profile": item.get("corner_profile", {}),
        "insights": item.get("insights", {}),
    }
    if include_markets:
        payload["markets"] = markets
        payload["model_summary"] = {
            "pre_match_strategy": (item.get("insights") or {}).get("pre_match_strategy", ""),
            "key_signals": (item.get("insights") or {}).get("key_signals", []),
            "risk_warnings": (item.get("insights") or {}).get("risk_warnings", []),
            "top_market": top_market,
        }
    return payload


def _picks_by_match(algo_run):
    grouped = {}
    for pick in sorted(
        [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
        key=_top_pick_sort_key,
        reverse=True,
    ):
        grouped.setdefault(str(pick.match_id or ""), []).append(pick)
    return grouped


def _all_games_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "summary": {
                "game_count": 0,
                "published_game_count": 0,
                "market_count": 0,
                "eligible_market_count": 0,
                "top_pick_count": 0,
            },
            "games": [],
        }

    picks_by_match = _picks_by_match(algo_run)
    fixture_summaries = (algo_run.result or {}).get("fixture_summaries", [])
    games = [
        _game_summary_from_fixture(item, picks_by_match, request=request)
        for item in fixture_summaries
    ]

    games.sort(
        key=lambda game: (
            (game.get("country") or "World").lower(),
            (game.get("league") or "").lower(),
            game.get("kickoff") or "",
            0 if game.get("published") else 1,
            -((game.get("top_market") or {}).get("confidence") or 0),
            game.get("fixture") or "",
        ),
    )

    return {
        "date": target_date,
        "published": bool(games),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "game_count": len(games),
            "published_game_count": sum(1 for game in games if game.get("published")),
            "market_count": (algo_run.result or {}).get("market_count", sum(game.get("market_count", 0) for game in games)),
            "eligible_market_count": sum(game.get("eligible_market_count", 0) for game in games),
            "top_pick_count": sum(len(items) for items in picks_by_match.values()),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "games": games,
        "grouped_games": _group_by_country_and_league(games, "games"),
    }


def _game_detail_payload(target_date, match_id, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "run_id": None,
            "posted_at": None,
            "game": None,
        }

    target_match_id = str(match_id)
    picks_by_match = _picks_by_match(algo_run)
    fixture_summary = next(
        (
            item
            for item in (algo_run.result or {}).get("fixture_summaries", [])
            if str(item.get("match_id") or "") == target_match_id
        ),
        None,
    )
    if not fixture_summary:
        match_picks = picks_by_match.get(target_match_id, [])
        if not match_picks:
            return {
                "date": target_date,
                "published": False,
                "run_id": algo_run.id,
                "posted_at": algo_run.created_at,
                "game": None,
            }
        pick = match_picks[0]
        fixture_summary = {
            "fixture": pick.fixture,
            "home_team": pick.home_team,
            "away_team": pick.away_team,
            "home_logo": fixture_summary.get("home_logo", ""),
            "away_logo": fixture_summary.get("away_logo", ""),
            "teams": {
                "home": {
                    "name": pick.home_team,
                    "logo": fixture_summary.get("home_logo", ""),
                },
                "away": {
                    "name": pick.away_team,
                    "logo": fixture_summary.get("away_logo", ""),
                },
            },
            "league": pick.league,
            "kickoff": pick.kickoff,
            "match_id": pick.match_id,
            "home_recent_form": pick.home_recent_form,
            "away_recent_form": pick.away_recent_form,
            "markets": [],
        }

    game = _game_summary_from_fixture(
        fixture_summary,
        picks_by_match,
        request=request,
        include_markets=True,
    )
    return {
        "date": target_date,
        "published": game.get("published", False),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "game": game,
    }


def _markets_for_pick_detail(pick, fixture_summary):
    markets = []
    selected_market = None
    for market in fixture_summary.get("markets") or []:
        if market.get("market") in EXCLUDED_MARKETS:
            continue
        payload = dict(market)
        is_selected = payload.get("market") == pick.market
        payload["selected"] = is_selected
        payload["selected_pick_id"] = pick.id if is_selected else payload.get("selected_pick_id")
        payload["selected_tier"] = pick.tier if is_selected else payload.get("selected_tier", "")
        markets.append(payload)
        if is_selected:
            selected_market = payload

    if selected_market is None:
        selected_market = {
            "market": pick.market,
            "meaning": pick.meaning,
            "confidence": pick.confidence,
            "raw_confidence": pick.confidence,
            "odds": float(pick.odds),
            "ev": float(pick.ev),
            "odds_source": pick.source,
            "proven": False,
            "eligible": True,
            "risk_flags": pick.risk_flags,
            "selected": True,
            "selected_pick_id": pick.id,
            "selected_tier": pick.tier,
        }
        markets.append(selected_market)

    ranked = sorted(markets, key=_market_sort_value, reverse=True)
    alternatives = [market for market in ranked if not market.get("selected")][:10]
    return selected_market, alternatives


def _stats_for_picks(picks):
    stats = empty_stats()
    for pick in picks:
        add_pick(stats, pick)
    return finalize_stats(stats)


def _form_has_games(form):
    return int((form or {}).get("games") or 0) > 0


def _recent_form_payload(form):
    form = dict(form or {})
    wins = int(form.get("wins") or 0)
    draws = int(form.get("draws") or 0)
    games = int(form.get("games") or 0)
    if "losses" not in form:
        form["losses"] = max(0, games - wins - draws)
    form.setdefault("draws", draws)
    form.setdefault("scope", "overall")
    form.setdefault("form", "")
    return form


def _fresh_recent_forms_for_pick(pick, fixture_summary=None):
    fixture_summary = fixture_summary or {}
    home_form = pick.home_recent_form or fixture_summary.get("home_recent_form") or {}
    away_form = pick.away_recent_form or fixture_summary.get("away_recent_form") or {}

    if not _form_has_games(home_form):
        home_form = fixture_summary.get("home_recent_form") or home_form
    if not _form_has_games(away_form):
        away_form = fixture_summary.get("away_recent_form") or away_form

    needs_home = not _form_has_games(home_form)
    needs_away = not _form_has_games(away_form)
    if (needs_home or needs_away) and pick.match_id:
        try:
            from .grindalgo.algo_runner import (
                aps_get,
                fetch_team_recent_form,
                recent_form_summary,
            )

            matches = aps_get("/fixtures", {"id": pick.match_id}, timeout=12)
            match = matches[0] if matches else {}
            teams = match.get("teams", {}) or {}
            home_id = (teams.get("home") or {}).get("id")
            away_id = (teams.get("away") or {}).get("id")
            if needs_home and home_id:
                fresh = fetch_team_recent_form(home_id)
                if fresh:
                    home_form = recent_form_summary(fresh)
            if needs_away and away_id:
                fresh = fetch_team_recent_form(away_id)
                if fresh:
                    away_form = recent_form_summary(fresh)
        except Exception as exc:
            log.warning("Could not refresh recent form for pick %s: %s", pick.id, exc)

    update_fields = []
    if _form_has_games(home_form) and home_form != pick.home_recent_form:
        pick.home_recent_form = home_form
        update_fields.append("home_recent_form")
    if _form_has_games(away_form) and away_form != pick.away_recent_form:
        pick.away_recent_form = away_form
        update_fields.append("away_recent_form")
    if update_fields:
        Pick.objects.filter(id=pick.id).update(
            **{field: getattr(pick, field) for field in update_fields}
        )
    return _recent_form_payload(home_form), _recent_form_payload(away_form)


def _pick_performance_slices(pick, days=PICK_DETAIL_HISTORY_DAYS):
    history = [
        item
        for item in latest_audited_picks(days=days)
        if _pick_identity_key(item) != _pick_identity_key(pick)
    ]
    pick_confidence_band = confidence_band(pick.confidence)
    pick_odds_band = odds_band(pick.odds)
    league_market = [
        item for item in history
        if item.league == pick.league and item.market == pick.market
    ]
    return {
        "days": days,
        "overall": _stats_for_picks(history),
        "same_market": _stats_for_picks([item for item in history if item.market == pick.market]),
        "same_league": _stats_for_picks([item for item in history if item.league == pick.league]),
        "same_league_market": _stats_for_picks(league_market),
        "same_tier": _stats_for_picks([item for item in history if item.tier == pick.tier]),
        "same_confidence_band": {
            "label": pick_confidence_band,
            **_stats_for_picks([item for item in history if confidence_band(item.confidence) == pick_confidence_band]),
        },
        "same_odds_band": {
            "label": pick_odds_band,
            **_stats_for_picks([item for item in history if odds_band(item.odds) == pick_odds_band]),
        },
    }


def _pick_detail_payload(pick, request=None):
    fixture_summary = _fixture_summary_for_pick(pick)
    selected_market, alternatives = _markets_for_pick_detail(pick, fixture_summary)
    home_form, away_form = _fresh_recent_forms_for_pick(pick, fixture_summary)
    run_picks = sorted(list(pick.run.picks.all()), key=_top_pick_sort_key, reverse=True)
    rank_on_day = next((index + 1 for index, item in enumerate(run_picks) if item.id == pick.id), None)
    pick_data = PickSerializer(pick, context={"request": request}).data
    effective_tier = _effective_pick_tier(pick)
    selected_market["selected_tier"] = effective_tier

    return {
        "date": pick.match_date or pick.run.target_date,
        "published": True,
        "run_id": pick.run_id,
        "posted_at": pick.created_at,
        "pick": pick_data,
        "fixture": {
            "fixture": pick.fixture,
            "home_team": pick.home_team,
            "away_team": pick.away_team,
            "home_logo": fixture_summary.get("home_logo", ""),
            "away_logo": fixture_summary.get("away_logo", ""),
            "teams": {
                "home": {
                    "name": pick.home_team,
                    "logo": fixture_summary.get("home_logo", ""),
                },
                "away": {
                    "name": pick.away_team,
                    "logo": fixture_summary.get("away_logo", ""),
                },
            },
            "league": pick.league,
            "league_logo": fixture_summary.get("league_logo", ""),
            "competition_logo": fixture_summary.get("league_logo", ""),
            "country": fixture_summary.get("country", ""),
            "country_flag": fixture_summary.get("country_flag", ""),
            "competition": pick.league,
            "competition_info": {
                "name": pick.league,
                "logo": fixture_summary.get("league_logo", ""),
                "country": fixture_summary.get("country", ""),
                "country_flag": fixture_summary.get("country_flag", ""),
            },
            "kickoff": pick.kickoff,
            "match_id": pick.match_id,
            "market_count": fixture_summary.get("market_count", len(fixture_summary.get("markets") or [])),
            "markets_70_plus": fixture_summary.get("markets_70_plus", 0),
            "markets_65_plus": fixture_summary.get("markets_65_plus", 0),
            "home_recent_form": home_form,
            "away_recent_form": away_form,
            "fixture_context": fixture_summary.get("fixture_context", {}),
            "team_news": fixture_summary.get("team_news", {}),
            "insights": fixture_summary.get("insights", {}),
            "corner_profile": fixture_summary.get("corner_profile", {}),
        },
        "market": {
            "selected": selected_market,
            "alternatives": alternatives,
            "eligible_count": sum(1 for market in fixture_summary.get("markets") or [] if market.get("eligible")),
        },
        "selection": {
            "rank_on_day": rank_on_day,
            "total_picks_on_day": len(run_picks),
            "is_top_pick": rank_on_day == 1,
            "tier": effective_tier,
            "selection_profile": pick_data.get("selection_profile", ""),
            "risk_level": pick_data.get("risk_level", ""),
            "confidence_band": confidence_band(pick.confidence),
            "odds_band": odds_band(pick.odds),
            "confidence": pick.confidence,
            "odds": float(pick.odds),
            "ev": float(pick.ev),
            "stake": float(pick.stake) if pick.stake is not None else None,
            "status": pick.status,
        },
        "model_summary": {
            "meaning": pick.meaning,
            "reasoning": pick.reasoning,
            "model_verdict": pick_data.get("model_verdict", pick.model_verdict),
            "risk_flags": pick.risk_flags,
            "insights": pick_data.get("insights", {}),
            "home_recent_form": home_form,
            "away_recent_form": away_form,
        },
        "performance": _pick_performance_slices(pick),
    }


def _daily_picks_payload(target_date, request=None):
    algo_run = _latest_successful_run(target_date)
    if not algo_run:
        return {
            "date": target_date,
            "published": False,
            "no_bet": False,
            "message": "Picks have not been published for this date.",
            "run_id": None,
            "posted_at": None,
            "summary": {
                "fixture_count": 0,
                "market_count": 0,
                "selected_pick_count": 0,
                "picks_70_plus": 0,
                "picks_65_plus": 0,
                "markets_70_plus": 0,
                "markets_65_plus": 0,
            },
            "fixtures": [],
        }

    picks = sorted(
        [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
        key=_top_pick_sort_key,
        reverse=True,
    )
    backed_ids = set()
    if request and request.user.is_authenticated:
        backed_ids = set(
            PickBack.objects.filter(user=request.user, pick__in=picks)
            .values_list("pick_id", flat=True)
        )

    fixture_summaries = {
        str(item.get("match_id")): item
        for item in (algo_run.result or {}).get("fixture_summaries", [])
    }
    fixtures = {}
    for item in fixture_summaries.values():
        markets = [
            market
            for market in item.get("markets") or []
            if market.get("market") not in EXCLUDED_MARKETS
        ]
        fixtures[item.get("match_id")] = {
            "fixture": item.get("fixture", ""),
            "home_team": item.get("home_team", ""),
            "away_team": item.get("away_team", ""),
            "home_logo": item.get("home_logo", ""),
            "away_logo": item.get("away_logo", ""),
            "teams": {
                "home": {
                    "name": item.get("home_team", ""),
                    "logo": item.get("home_logo", ""),
                },
                "away": {
                    "name": item.get("away_team", ""),
                    "logo": item.get("away_logo", ""),
                },
            },
            "league": item.get("league", ""),
            "league_logo": item.get("league_logo", ""),
            "competition_logo": item.get("league_logo", ""),
            "country": item.get("country", ""),
            "country_flag": item.get("country_flag", ""),
            "round": item.get("round", ""),
            "league_type": item.get("league_type", ""),
            "competition": item.get("league", ""),
            "competition_info": {
                "name": item.get("league", ""),
                "logo": item.get("league_logo", ""),
                "country": item.get("country", ""),
                "country_flag": item.get("country_flag", ""),
            },
            "kickoff": item.get("kickoff", ""),
            "match_id": item.get("match_id", ""),
            "market_count": item.get("market_count", 0),
            "markets_70_plus": item.get("markets_70_plus", 0),
            "markets_65_plus": item.get("markets_65_plus", 0),
            "home_recent_form": _recent_form_payload(item.get("home_recent_form", {})),
            "away_recent_form": _recent_form_payload(item.get("away_recent_form", {})),
            "fixture_context": item.get("fixture_context", {}),
            "team_news": item.get("team_news", {}),
            "insights": item.get("insights", {}),
            "corner_profile": item.get("corner_profile", {}),
            "markets": markets,
            "picks": [],
        }

    for pick in picks:
        key = str(pick.match_id)
        if key not in fixtures:
            fixtures[key] = {
                "fixture": pick.fixture,
                "home_team": pick.home_team,
                "away_team": pick.away_team,
                "home_logo": "",
                "away_logo": "",
                "teams": {
                    "home": {"name": pick.home_team, "logo": ""},
                    "away": {"name": pick.away_team, "logo": ""},
                },
                "league": pick.league,
                "league_logo": "",
                "competition_logo": "",
                "country": "",
                "country_flag": "",
                "round": "",
                "league_type": "",
                "competition": pick.league,
                "competition_info": {
                    "name": pick.league,
                    "logo": "",
                    "country": "",
                    "country_flag": "",
                },
                "kickoff": pick.kickoff,
                "match_id": pick.match_id,
                "market_count": 0,
                "markets_70_plus": 0,
                "markets_65_plus": 0,
                "home_recent_form": _recent_form_payload(pick.home_recent_form),
                "away_recent_form": _recent_form_payload(pick.away_recent_form),
                "fixture_context": {},
                "team_news": {},
                "insights": {},
                "corner_profile": {},
                "markets": [],
                "picks": [],
            }
        fixtures[key]["home_recent_form"] = _recent_form_payload(
            fixtures[key].get("home_recent_form") or pick.home_recent_form
        )
        fixtures[key]["away_recent_form"] = _recent_form_payload(
            fixtures[key].get("away_recent_form") or pick.away_recent_form
        )
        data = PickSerializer(pick, context={"request": request}).data
        data["backed_by_me"] = pick.id in backed_ids
        data["backed_count"] = pick.backs.count()
        fixtures[key]["picks"].append(data)
        for market in fixtures[key]["markets"]:
            if market.get("market") == pick.market:
                market["selected"] = True
                market["selected_pick_id"] = pick.id
                market["selected_tier"] = data["tier"]

    for fixture in fixtures.values():
        for market in fixture["markets"]:
            market.setdefault("selected", False)
            market.setdefault("selected_pick_id", None)
            market.setdefault("selected_tier", "")

    published_fixtures = sorted(
        [fixture for fixture in fixtures.values() if fixture["picks"]],
        key=lambda fixture: (
            (fixture.get("country") or "World").lower(),
            (fixture.get("league") or "").lower(),
            fixture.get("kickoff") or "",
            -_top_pick_sort_key(
                next(pick for pick in picks if str(pick.match_id) == str(fixture["match_id"]))
            )[1],
        ),
    )
    top_pick = picks[0] if picks else None

    return {
        "date": target_date,
        "published": bool(picks),
        "no_bet": not bool(picks),
        "message": (
            "Published picks are available."
            if picks
            else "No bet today. The model scored the fixtures but did not find an edge strong enough to publish."
        ),
        "run_id": algo_run.id,
        "posted_at": algo_run.created_at,
        "summary": {
            "fixture_count": len(published_fixtures),
            "market_count": (algo_run.result or {}).get("market_count", 0),
            "selected_pick_count": len(picks),
            "top_pick_id": top_pick.id if top_pick else None,
            "banker_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.BANKER),
            "value_gem_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.VALUE_GEM),
            "wild_card_count": sum(1 for pick in picks if _effective_pick_tier(pick) == Pick.Tier.WILD_CARD),
            "picks_70_plus": sum(1 for pick in picks if pick.confidence >= 70),
            "picks_65_plus": sum(1 for pick in picks if pick.confidence >= 65),
            "markets_70_plus": (algo_run.result or {}).get("markets_70_plus", 0),
            "markets_65_plus": (algo_run.result or {}).get("markets_65_plus", 0),
        },
        "strategy": (algo_run.result or {}).get("strategy_profile", {}),
        "fixtures": published_fixtures,
        "grouped_fixtures": _group_by_country_and_league(published_fixtures, "fixtures"),
    }


@extend_schema_view(
    list=extend_schema(
        summary="List algo runs",
        description="Internal staff endpoint. Lists algorithm execution records.",
        tags=["Admin Algo"],
    ),
    retrieve=extend_schema(
        summary="Get algo run",
        description="Internal staff endpoint. Gets a specific algorithm execution record by ID.",
        tags=["Admin Algo"],
    ),
    create=extend_schema(
        summary="Queue manual algo run",
        description="""
        Internal staff endpoint. Queues the betting algorithm for a target date.
        
        **Optional payload:**
        ```json
        {
          "target_date": "2026-05-04"
        }
        ```
        
        If no target_date is provided, runs for today.
        """,
        tags=["Admin Algo"],
        request=AlgoRunCreateSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Generate picks for a date",
                value={"target_date": "2026-05-19"},
                request_only=True,
            )
        ],
    ),
)
class AlgoRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AlgoRun.objects.prefetch_related("picks").all()
    serializer_class = AlgoRunSerializer
    permission_classes = [IsAdminUser]

    def create(self, request):
        serializer = AlgoRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("target_date")
        task = generate_daily_picks.delay(target_date.isoformat() if target_date else None)
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Algo run queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Update algo results",
        description="Internal staff endpoint. Queues settlement for the target date. If omitted, settles yesterday in WAT.",
        tags=["Admin Algo"],
        request=ResultsUpdateSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Settle a date",
                value={"target_date": "2026-05-18"},
                request_only=True,
            )
        ],
    )
    @action(detail=False, methods=["post"], url_path="update-results")
    def update_results(self, request):
        serializer = ResultsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("target_date")
        task = settle_daily_results.delay(target_date.isoformat() if target_date else None)
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Results settlement queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Run algo auditor",
        description="Internal staff endpoint. Queues the monthly auditor report for an optional date range.",
        tags=["Admin Algo"],
        request=AuditorRunSerializer,
        responses={202: TaskQueuedSerializer},
        examples=[
            OpenApiExample(
                "Audit date range",
                value={"from_date": "2026-04-01", "to_date": "2026-04-30"},
                request_only=True,
            )
        ],
    )
    @action(detail=False, methods=["post"], url_path="run-auditor")
    def run_auditor(self, request):
        serializer = AuditorRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from_date = serializer.validated_data.get("from_date")
        to_date = serializer.validated_data.get("to_date")
        task = run_monthly_auditor.delay(
            from_date.isoformat() if from_date else None,
            to_date.isoformat() if to_date else None,
        )
        return Response(
            {
                "task_id": task.id,
                "status": "queued",
                "message": "Auditor run queued. Poll the task status endpoint for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PublicSummaryView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = PublicSummarySerializer

    @extend_schema(
        summary="Public audited performance summary",
        description="Returns headline stats for the public proof/landing page.",
        tags=["Public Record"],
        responses={200: PublicSummarySerializer},
    )
    def get(self, request):
        window_days = 90
        since = timezone.localdate() - timedelta(days=window_days)
        today = timezone.localdate()
        picks = Pick.objects.filter(
            status__in=SETTLED_PICK_STATUSES,
        ).filter(
            Q(match_date__gte=since, match_date__lte=today)
            | Q(match_date__isnull=True, run__target_date__gte=since, run__target_date__lte=today)
        ).select_related("run").order_by(
            "-match_date",
            "-run__target_date",
            "-created_at",
            "-id",
        )
        return Response(_performance_summary(_dedupe_latest_public_picks(picks), window_days))


class DailyPicksView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Daily picks",
        description="Authenticated user endpoint. Returns the published picks for a matchday. Defaults to today in WAT.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: DailyPicksResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        return Response(_daily_picks_payload(target_date, request))


class TopPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TopPickResponseSerializer

    @extend_schema(
        summary="Top picks of the day",
        description="Authenticated user endpoint. Returns the high-value published picks for the requested matchday, ranked by tier, confidence, EV, and odds.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={200: TopPickResponseSerializer},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date)
        picks = []
        if algo_run:
            picks = sorted(
                [pick for pick in algo_run.picks.all() if pick.market not in EXCLUDED_MARKETS],
                key=_top_pick_sort_key,
                reverse=True,
            )
        picks_data = PickSerializer(picks, many=True, context={"request": request}).data
        top_pick = picks_data[0] if picks_data else None
        return Response(
            {
                "date": target_date,
                "published": bool(picks_data),
                "count": len(picks_data),
                "pick": top_pick,
                "top_pick": top_pick,
                "picks": picks_data,
            }
        )


class GamesView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameListResponseSerializer

    @extend_schema(
        summary="All covered games",
        description="Authenticated user endpoint. Returns every fixture scored for the covered leagues on a matchday, including each game's best available market and any official published pick.",
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        responses={200: GameListResponseSerializer},
    )
    def get(self, request):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        return Response(_all_games_payload(target_date, request))


class GameDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GameDetailResponseSerializer

    @extend_schema(
        summary="Game analysis detail",
        description="Authenticated user endpoint. Returns full model context for one scored fixture, including all markets, fixture context, forms, team news, insights, and official picks if published.",
        tags=["Games"],
        parameters=[GameAnalysisQuerySerializer],
        responses={200: GameDetailResponseSerializer},
    )
    def get(self, request, match_id):
        query = GameAnalysisQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        payload = _game_detail_payload(target_date, match_id, request)
        if payload["game"] is None:
            return Response(payload, status=status.HTTP_404_NOT_FOUND)
        return Response(payload)


class PickDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PickDetailResponseSerializer

    @extend_schema(
        summary="Pick detail",
        description="Authenticated user endpoint. Returns one published pick with fixture context, market context, model summary, and historical performance slices.",
        tags=["Picks"],
        responses={200: PickDetailResponseSerializer},
    )
    def get(self, request, pick_id):
        pick = get_object_or_404(
            Pick.objects.select_related("run").prefetch_related("backs", "run__picks"),
            id=pick_id,
        )
        return Response(_pick_detail_payload(pick, request))


class DailyPicksDownloadView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DailyPicksResponseSerializer

    @extend_schema(
        summary="Download daily picks",
        description="Authenticated user endpoint. Downloads the daily picks as CSV.",
        tags=["Picks"],
        parameters=[DailyPicksQuerySerializer],
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
    )
    def get(self, request):
        query = DailyPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        algo_run = _latest_successful_run(target_date)
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="betpreneur_picks_{target_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(["date", "fixture", "league", "kickoff", "tier", "market", "confidence", "odds", "ev", "status"])
        if algo_run:
            for pick in algo_run.picks.all().order_by("kickoff", "-confidence"):
                if pick.market in EXCLUDED_MARKETS:
                    continue
                writer.writerow([
                    pick.match_date,
                    pick.fixture,
                    pick.league,
                    pick.kickoff,
                    pick.tier,
                    pick.market,
                    pick.confidence,
                    pick.odds,
                    pick.ev,
                    pick.status,
                ])
        return response


def _market_health_state(loss_streak, recent_5_losses, recent_10_hit_rate, recent_10_count, roi_flat):
    if loss_streak >= 3 or recent_5_losses >= 4 or (recent_10_count >= 5 and recent_10_hit_rate < 35):
        return "suppressed"
    if loss_streak >= 2 or recent_5_losses >= 3 or (recent_10_count >= 5 and recent_10_hit_rate < 45):
        return "cooling"
    if recent_10_count >= 5 and recent_10_hit_rate >= 60 and roi_flat >= 0:
        return "recovered"
    return "active"


class MarketHealthView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = MarketHealthResponseSerializer

    @extend_schema(
        summary="Internal market health",
        description="Staff endpoint. Shows market performance used to suppress, watch, or restore markets.",
        tags=["Admin Algo"],
        parameters=[MarketHealthQuerySerializer],
        responses={200: MarketHealthResponseSerializer},
    )
    def get(self, request):
        query = MarketHealthQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data.get("days", 90)
        scope = query.validated_data.get("scope", "all")
        market_filter = query.validated_data.get("market", "")
        since = timezone.localdate() - timedelta(days=days)

        qs = MarketPrediction.objects.filter(
            match_date__gte=since,
            status__in=[MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS],
        ).order_by("-match_date", "-created_at", "-id")
        if market_filter:
            qs = qs.filter(market__iexact=market_filter)
        if scope == "published":
            qs = qs.filter(published=True)
        elif scope == "internal":
            qs = qs.filter(published=False)

        latest = {}
        for prediction in qs:
            key = (
                prediction.match_date,
                str(prediction.match_id or "").strip(),
                prediction.fixture,
                prediction.market,
            )
            if key not in latest:
                latest[key] = prediction

        grouped = {}
        for prediction in latest.values():
            item = grouped.setdefault(
                prediction.market,
                {
                    "market": prediction.market,
                    "count": 0,
                    "wins": 0,
                    "losses": 0,
                    "published_count": 0,
                    "internal_count": 0,
                    "stake": 0.0,
                    "pnl": 0.0,
                    "confidence_total": 0.0,
                    "recent_statuses": [],
                },
            )
            item["count"] += 1
            if prediction.status == MarketPrediction.Status.WIN:
                item["wins"] += 1
            else:
                item["losses"] += 1
            if prediction.published:
                item["published_count"] += 1
            else:
                item["internal_count"] += 1
            item["stake"] += 1000.0
            item["pnl"] += float(prediction.pnl_simulated or 0)
            item["confidence_total"] += float(prediction.confidence or 0)
            if len(item["recent_statuses"]) < 10:
                item["recent_statuses"].append(prediction.status)

        markets = []
        for item in grouped.values():
            recent = item.pop("recent_statuses")
            loss_streak = 0
            for status_value in recent:
                if status_value != MarketPrediction.Status.LOSS:
                    break
                loss_streak += 1
            recent_5_losses = sum(1 for status_value in recent[:5] if status_value == MarketPrediction.Status.LOSS)
            recent_10 = recent[:10]
            recent_10_wins = sum(1 for status_value in recent_10 if status_value == MarketPrediction.Status.WIN)
            hit_rate = round((item["wins"] / item["count"]) * 100, 1) if item["count"] else 0.0
            roi_flat = round((item["pnl"] / item["stake"]) * 100, 1) if item["stake"] else 0.0
            recent_10_hit_rate = round((recent_10_wins / len(recent_10)) * 100, 1) if recent_10 else 0.0
            item.update({
                "hit_rate": hit_rate,
                "roi_flat": roi_flat,
                "avg_confidence": round(item["confidence_total"] / item["count"], 1) if item["count"] else 0.0,
                "loss_streak": loss_streak,
                "recent_5_losses": recent_5_losses,
                "recent_10_hit_rate": recent_10_hit_rate,
                "state": _market_health_state(loss_streak, recent_5_losses, recent_10_hit_rate, len(recent_10), roi_flat),
            })
            item.pop("stake", None)
            item.pop("confidence_total", None)
            markets.append(item)

        state_rank = {"suppressed": 3, "cooling": 2, "active": 1, "recovered": 0}
        markets.sort(
            key=lambda item: (
                state_rank.get(item["state"], 0),
                item["loss_streak"],
                item["recent_5_losses"],
                -item["hit_rate"],
            ),
            reverse=True,
        )
        return Response({
            "days": days,
            "scope": scope,
            "count": len(markets),
            "markets": markets,
        })


class BackPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PickBackResponseSerializer

    @extend_schema(
        summary="Back a pick",
        description="Authenticated user endpoint. Marks that the user backed this pick. No request body is required.",
        tags=["Picks"],
        request=None,
        responses={200: PickBackResponseSerializer, 201: PickBackResponseSerializer},
    )
    def post(self, request, pick_id):
        pick = get_object_or_404(Pick, id=pick_id)
        backed, created = PickBack.objects.get_or_create(pick=pick, user=request.user)
        return Response(
            {
                "pick_id": pick.id,
                "backed": True,
                "created": created,
                "backed_count": pick.backs.count(),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class BulkBackPickView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BulkPickBackResponseSerializer

    @extend_schema(
        summary="Back multiple picks",
        description="Authenticated user endpoint. Marks multiple published picks as backed by the current user.",
        tags=["Picks"],
        request=BulkPickBackRequestSerializer,
        responses={200: BulkPickBackResponseSerializer, 201: BulkPickBackResponseSerializer},
    )
    def post(self, request):
        serializer = BulkPickBackRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pick_ids = serializer.validated_data["pick_ids"]

        picks = list(
            Pick.objects.select_related("run")
            .prefetch_related("backs")
            .filter(id__in=pick_ids)
            .exclude(market__in=EXCLUDED_MARKETS)
        )
        picks_by_id = {pick.id: pick for pick in picks}
        missing_pick_ids = [pick_id for pick_id in pick_ids if pick_id not in picks_by_id]

        existing_ids = set(
            PickBack.objects.filter(user=request.user, pick_id__in=picks_by_id)
            .values_list("pick_id", flat=True)
        )
        created_ids = []
        results = []
        for pick_id in pick_ids:
            pick = picks_by_id.get(pick_id)
            if not pick:
                results.append({
                    "pick_id": pick_id,
                    "backed": False,
                    "created": False,
                    "error": "not_found",
                })
                continue
            if pick_id in existing_ids:
                results.append({
                    "pick_id": pick_id,
                    "backed": True,
                    "created": False,
                    "backed_count": pick.backs.count(),
                })
                continue
            PickBack.objects.create(user=request.user, pick=pick)
            created_ids.append(pick_id)
            results.append({
                "pick_id": pick_id,
                "backed": True,
                "created": True,
                "backed_count": pick.backs.count() + 1,
            })

        backed_picks = sorted(
            picks,
            key=lambda pick: (
                pick.match_date or pick.run.target_date,
                pick.kickoff or "",
                -(pick.confidence or 0),
            ),
        )
        payload = {
            "requested_count": len(pick_ids),
            "backed_count": len(picks),
            "created_count": len(created_ids),
            "already_backed_count": len(existing_ids),
            "missing_pick_ids": missing_pick_ids,
            "results": results,
            "picks": PickSerializer(backed_picks, many=True, context={"request": request}).data,
        }
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created_ids else status.HTTP_200_OK,
        )


class BackedPicksView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BackedPicksResponseSerializer

    @extend_schema(
        summary="List user backed picks",
        description="Authenticated user endpoint. Returns picks backed by the current user, with optional match date filtering.",
        tags=["Picks"],
        parameters=[BackedPicksQuerySerializer],
        responses={200: BackedPicksResponseSerializer},
    )
    def get(self, request):
        query = BackedPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date")

        picks = (
            Pick.objects.select_related("run")
            .prefetch_related("backs")
            .filter(backs__user=request.user)
            .exclude(market__in=EXCLUDED_MARKETS)
        )
        if target_date:
            picks = picks.filter(match_date=target_date)
        picks = picks.distinct().order_by("-match_date", "kickoff", "-confidence", "-ev")

        return Response(
            {
                "date": target_date,
                "count": picks.count(),
                "picks": PickSerializer(picks, many=True, context={"request": request}).data,
            }
        )


class PublicRecordView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = RecordResponseSerializer

    @extend_schema(
        summary="Public audited pick record",
        description="Returns a deduplicated public audit table for the requested window. Each record is the latest posted copy for a date, fixture and market.",
        tags=["Public Record"],
        parameters=[RecordQuerySerializer],
        responses={200: RecordResponseSerializer},
    )
    def get(self, request):
        query = RecordQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        window_days = query.validated_data["days"]
        since = timezone.localdate() - timedelta(days=window_days)
        today = timezone.localdate()
        picks_queryset = Pick.objects.filter(
            status__in=SETTLED_PICK_STATUSES,
        ).filter(
            Q(match_date__gte=since, match_date__lte=today)
            | Q(match_date__isnull=True, run__target_date__gte=since, run__target_date__lte=today)
        ).select_related("run").order_by(
            "-match_date",
            "-run__target_date",
            "-created_at",
            "-id",
        )
        picks = _dedupe_latest_public_picks(picks_queryset)
        return Response(
            {
                "summary": _performance_summary(picks, window_days),
                "records": [_public_record_pick_payload(pick) for pick in picks],
            }
        )


class TaskStatusView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        summary="Get background task status",
        description="Internal staff endpoint. Returns Celery task status and result/error when available.",
        tags=["Admin Algo"],
        responses={200: TaskStatusSerializer},
    )
    def get(self, request, task_id):
        task = AsyncResult(task_id)
        payload = {
            "task_id": task_id,
            "status": task.status.lower(),
            "result": None,
            "error": "",
        }
        if task.successful():
            payload["result"] = task.result
        elif task.failed():
            payload["error"] = str(task.result)
        return Response(payload, status=status.HTTP_200_OK)
