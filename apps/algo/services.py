import os
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import AlgoRun, MarketPrediction, Pick, StrategyReview


@contextmanager
def temporary_env(values):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AlgoRunnerService:
    """
    Transitional service boundary around the legacy single-file runner.

    The API and database now depend on this service, not on algo_runner.py
    directly. Next we can move fixture fetching, scoring, selection, reports,
    and integrations out of algo_runner.py one module at a time.
    """

    def create_run(self, *, user=None, target_date=None) -> AlgoRun:
        if target_date is None:
            target_date = timezone.localdate() + timedelta(days=1)
        return AlgoRun.objects.create(target_date=target_date, triggered_by=user)

    def _runner_env(self, extra=None):
        grind_algo_settings = getattr(settings, "GRIND_ALGO", {})
        env = {
            key: value
            for key, value in grind_algo_settings.items()
            if value not in (None, "")
        }
        if "APS_KEY" in env and "API_SPORTS_KEY" not in env:
            env["API_SPORTS_KEY"] = env["APS_KEY"]
        if extra:
            env.update(extra)
        return env

    def _persist_selected_picks(self, algo_run: AlgoRun, result):
        selected_picks = result.get("selected_picks") or []
        if not selected_picks:
            return

        algo_run.picks.all().delete()
        picks = []
        for item in selected_picks:
            picks.append(
                Pick.objects.create(
                    run=algo_run,
                    match_date=item.get("match_date") or algo_run.target_date,
                    fixture=item.get("fixture", ""),
                    home_team=item.get("home_team", ""),
                    away_team=item.get("away_team", ""),
                    league=item.get("league", ""),
                    kickoff=item.get("kickoff", ""),
                    match_id=item.get("match_id", ""),
                    tier=item.get("tier", Pick.Tier.BANKER),
                    market=item.get("market", ""),
                    meaning=item.get("meaning", ""),
                    reasoning=item.get("reasoning", ""),
                    model_verdict=item.get("model_verdict", ""),
                    home_recent_form=item.get("home_recent_form", {}),
                    away_recent_form=item.get("away_recent_form", {}),
                    risk_flags=item.get("risk_flags", []),
                    insights=item.get("insights", {}),
                    confidence=item.get("confidence") or 0,
                    odds=item.get("odds") or 0,
                    ev=item.get("ev") or 0,
                    stake=item.get("stake"),
                    source=item.get("source", ""),
                )
            )
        return picks

    def _prediction_rejection_reason(self, market, published=False):
        if published or market.get("selected"):
            return ""
        flags = market.get("risk_flags") or []
        if not market.get("eligible"):
            if flags:
                return ", ".join(str(flag) for flag in flags[:4])
            return "below_publish_gate"
        return "not_top_pick"

    def _persist_market_predictions(self, algo_run: AlgoRun, result):
        MarketPrediction.objects.filter(run=algo_run).delete()
        fixture_summaries = result.get("fixture_summaries") or []
        if not fixture_summaries:
            return 0

        selected_lookup = {
            (
                str(pick.match_id or "").strip(),
                pick.market,
            ): pick
            for pick in algo_run.picks.all()
        }
        rows = []
        for fixture in fixture_summaries:
            match_id = str(fixture.get("match_id") or "").strip()
            for market in fixture.get("markets") or []:
                selected_pick = selected_lookup.get((match_id, market.get("market", "")))
                published = bool(selected_pick)
                rows.append(
                    MarketPrediction(
                        run=algo_run,
                        selected_pick=selected_pick,
                        match_date=algo_run.target_date,
                        fixture=fixture.get("fixture", ""),
                        home_team=fixture.get("home_team", ""),
                        away_team=fixture.get("away_team", ""),
                        league=fixture.get("league", ""),
                        kickoff=fixture.get("kickoff", ""),
                        match_id=match_id,
                        market=market.get("market", ""),
                        meaning=market.get("meaning", ""),
                        raw_confidence=market.get("raw_confidence") or market.get("confidence") or 0,
                        confidence=market.get("confidence") or 0,
                        odds=market.get("odds") or 0,
                        ev=market.get("ev"),
                        odds_source=market.get("odds_source", ""),
                        odds_meta=market.get("odds_meta") or {},
                        eligible=bool(market.get("eligible")),
                        published=published,
                        rejection_reason=self._prediction_rejection_reason(market, published),
                        risk_flags=market.get("risk_flags") or [],
                        insights=market.get("insights") or {},
                        home_recent_form=fixture.get("home_recent_form") or {},
                        away_recent_form=fixture.get("away_recent_form") or {},
                        fixture_context=fixture.get("fixture_context") or {},
                        team_news=fixture.get("team_news") or {},
                    )
                )
        MarketPrediction.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        result["internal_prediction_count"] = len(rows)
        return len(rows)

    def _sync_settled_picks(self, result):
        settled_picks = result.get("settled_picks") or []
        settled_at = timezone.now()
        updated = 0
        for item in settled_picks:
            rows = Pick.objects.filter(
                match_date=item.get("match_date"),
                fixture=item.get("fixture", ""),
                market=item.get("market", ""),
            )
            update_count = rows.update(
                status=item.get("status", Pick.Status.PENDING),
                score=item.get("score", ""),
                result=item.get("result", ""),
                pnl=item.get("pnl"),
                stake=item.get("stake"),
                settled_at=settled_at,
            )
            updated += update_count
        if settled_picks:
            result["database_updated_count"] = updated

    def _performance_profile(self):
        predictions = (
            MarketPrediction.objects.filter(status__in=[MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS])
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")
        )
        if predictions.exists():
            return self._performance_profile_from_predictions(predictions)

        picks = (
            Pick.objects.filter(status__in=[Pick.Status.WIN, Pick.Status.LOSS])
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")
        )
        return self._performance_profile_from_picks(picks)

    def _strategy_action_for_stats(self, stats):
        count = int(stats.get("count") or 0)
        if count < 5:
            return ""
        state = stats.get("state") or "active"
        hit_rate = float(stats.get("hit_rate") or 0)
        roi_flat = float(stats.get("roi_flat") or 0)
        loss_streak = int(stats.get("loss_streak") or 0)
        recent_5_losses = int(stats.get("recent_5_losses") or 0)
        if state == "suppressed" or loss_streak >= 3 or recent_5_losses >= 4:
            return "suppress"
        if state == "cooling" or roi_flat < -8 or hit_rate < 45:
            return "cool"
        if state == "recovered" and hit_rate >= 60 and roi_flat >= 0:
            return "promote"
        return ""

    def _strategy_profile(self, target_date):
        performance = self._performance_profile()
        market_actions = {}
        league_market_actions = {}
        league_warnings = set()

        for market, stats in (performance.get("markets") or {}).items():
            action = self._strategy_action_for_stats(stats)
            if action:
                market_actions[market] = {"action": action, **stats}

        league_bad_counts = defaultdict(int)
        for key, stats in (performance.get("league_markets") or {}).items():
            action = self._strategy_action_for_stats(stats)
            if not action:
                continue
            league_market_actions[key] = {"action": action, **stats}
            league = str(key).split("::", 1)[0]
            if action in {"suppress", "cool"}:
                league_bad_counts[league] += 1

        for league, count in league_bad_counts.items():
            if count >= 2:
                league_warnings.add(league)

        markets_suppressed = sorted(
            market for market, item in market_actions.items() if item.get("action") == "suppress"
        )
        markets_cooling = sorted(
            market for market, item in market_actions.items() if item.get("action") == "cool"
        )
        markets_promoted = sorted(
            market for market, item in market_actions.items() if item.get("action") == "promote"
        )
        reasons = []
        if markets_suppressed:
            reasons.append(f"Suppressing weak markets: {', '.join(markets_suppressed[:6])}.")
        if markets_cooling:
            reasons.append(f"Cooling markets under watch: {', '.join(markets_cooling[:6])}.")
        if markets_promoted:
            reasons.append(f"Promoting recovered markets: {', '.join(markets_promoted[:6])}.")
        if league_warnings:
            reasons.append(f"League warnings active: {', '.join(sorted(league_warnings)[:6])}.")
        reason = " ".join(reasons) or "No major market restrictions; using adaptive market memory."

        profile = {
            "date": target_date.isoformat(),
            "markets": market_actions,
            "league_markets": league_market_actions,
            "league_warnings": sorted(league_warnings),
            "daily_policy": "adaptive_market_memory",
            "reason": reason,
        }
        StrategyReview.objects.update_or_create(
            target_date=target_date,
            defaults={
                "profile": profile,
                "markets_suppressed": markets_suppressed,
                "markets_cooling": markets_cooling,
                "markets_promoted": markets_promoted,
                "league_market_actions": league_market_actions,
                "league_warnings": sorted(league_warnings),
                "daily_policy": "adaptive_market_memory",
                "reason": reason,
            },
        )
        return profile

    def _empty_market_stats(self):
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "stake": 0.0,
            "pnl": 0.0,
            "confidence_total": 0.0,
            "published_count": 0,
            "internal_count": 0,
            "recent_statuses": [],
        }

    def _finalize_market_stats(self, group):
        payload = {}
        for key, stats in group.items():
            settled = stats["wins"] + stats["losses"]
            stake = stats["stake"]
            recent_statuses = stats["recent_statuses"]
            loss_streak = 0
            for status in recent_statuses:
                if status != Pick.Status.LOSS:
                    break
                loss_streak += 1
            recent_5 = recent_statuses[:5]
            recent_10 = recent_statuses[:10]
            recent_5_losses = sum(1 for status in recent_5 if status == Pick.Status.LOSS)
            recent_10_wins = sum(1 for status in recent_10 if status == Pick.Status.WIN)
            hit_rate = round((stats["wins"] / settled) * 100, 1) if settled else 0.0
            roi_flat = round((stats["pnl"] / stake) * 100, 1) if stake else 0.0
            recent_10_hit_rate = round((recent_10_wins / len(recent_10)) * 100, 1) if recent_10 else 0.0
            state = "active"
            if loss_streak >= 3 or recent_5_losses >= 4 or (len(recent_10) >= 5 and recent_10_hit_rate < 35):
                state = "suppressed"
            elif loss_streak >= 2 or recent_5_losses >= 3 or (len(recent_10) >= 5 and recent_10_hit_rate < 45):
                state = "cooling"
            elif len(recent_10) >= 5 and recent_10_hit_rate >= 60 and roi_flat >= 0:
                state = "recovered"
            payload[key] = {
                "count": stats["count"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "hit_rate": hit_rate,
                "roi_flat": roi_flat,
                "avg_confidence": round(stats["confidence_total"] / stats["count"], 1) if stats["count"] else 0.0,
                "published_count": stats["published_count"],
                "internal_count": stats["internal_count"],
                "recent_count": len(recent_statuses),
                "loss_streak": loss_streak,
                "recent_5_losses": recent_5_losses,
                "recent_10_hit_rate": recent_10_hit_rate,
                "state": state,
            }
        return payload

    def _performance_profile_from_predictions(self, predictions):
        latest = {}
        for prediction in predictions:
            key = (
                prediction.match_date or prediction.run.target_date,
                str(prediction.match_id or "").strip(),
                prediction.fixture,
                prediction.market,
            )
            if key not in latest:
                latest[key] = prediction

        older_picks = (
            Pick.objects.filter(status__in=[Pick.Status.WIN, Pick.Status.LOSS])
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")
        )
        for pick in older_picks:
            key = (
                pick.match_date or pick.run.target_date,
                str(pick.match_id or "").strip(),
                pick.fixture,
                pick.market,
            )
            if key not in latest:
                latest[key] = pick

        market_stats = defaultdict(self._empty_market_stats)
        league_market_stats = defaultdict(self._empty_market_stats)

        for record in latest.values():
            keys = [record.market, f"{record.league}::{record.market}"]
            stat_groups = [market_stats[keys[0]], league_market_stats[keys[1]]]
            for stats in stat_groups:
                stats["count"] += 1
                if record.status == Pick.Status.WIN:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                if isinstance(record, MarketPrediction):
                    stats["stake"] += 1000.0
                    stats["pnl"] += float(record.pnl_simulated or 0)
                    is_published = record.published
                else:
                    stats["stake"] += float(record.stake or 0)
                    stats["pnl"] += float(record.pnl or 0)
                    is_published = True
                stats["confidence_total"] += float(record.confidence or 0)
                if is_published:
                    stats["published_count"] += 1
                else:
                    stats["internal_count"] += 1
                if len(stats["recent_statuses"]) < 10:
                    stats["recent_statuses"].append(record.status)

        return {
            "markets": self._finalize_market_stats(market_stats),
            "league_markets": self._finalize_market_stats(league_market_stats),
        }

    def _performance_profile_from_picks(self, picks):
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

        market_stats = defaultdict(self._empty_market_stats)
        league_market_stats = defaultdict(self._empty_market_stats)

        for pick in latest.values():
            keys = [pick.market, f"{pick.league}::{pick.market}"]
            stat_groups = [market_stats[keys[0]], league_market_stats[keys[1]]]
            for stats in stat_groups:
                stats["count"] += 1
                if pick.status == Pick.Status.WIN:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                stats["stake"] += float(pick.stake or 0)
                stats["pnl"] += float(pick.pnl or 0)
                stats["confidence_total"] += float(pick.confidence or 0)
                stats["published_count"] += 1
                if len(stats["recent_statuses"]) < 10:
                    stats["recent_statuses"].append(pick.status)

        return {
            "markets": self._finalize_market_stats(market_stats),
            "league_markets": self._finalize_market_stats(league_market_stats),
        }

    def _api_football_headers(self):
        api_key = self._runner_env().get("APS_KEY")
        if not api_key:
            raise RuntimeError("APS_KEY is not configured")
        return {"x-apisports-key": api_key}

    def _api_football_get(self, path, params=None):
        response = requests.get(
            f"https://v3.football.api-sports.io{path}",
            headers=self._api_football_headers(),
            params=params or {},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("response", [])

    def _first_scorer(self, fixture_id):
        events = self._api_football_get("/fixtures/events", {"fixture": fixture_id})
        for event in events:
            if str(event.get("type")).title() == "Goal" and "Missed" not in str(event.get("detail", "")):
                return (event.get("team") or {}).get("name")
        return None

    def _fixture_corner_total(self, fixture_id):
        if not hasattr(self, "_corner_total_cache"):
            self._corner_total_cache = {}
        if fixture_id in self._corner_total_cache:
            return self._corner_total_cache[fixture_id]
        stats = self._api_football_get("/fixtures/statistics", {"fixture": fixture_id})
        total = 0
        found = False
        for team_stats in stats:
            for item in team_stats.get("statistics", []) or []:
                if str(item.get("type", "")).lower() == "corner kicks":
                    try:
                        total += int(item.get("value") or 0)
                        found = True
                    except (TypeError, ValueError):
                        continue
        value = total if found else None
        self._corner_total_cache[fixture_id] = value
        return value

    def _check_market(self, pick, home_goals, away_goals, home_team=None, away_team=None, first_scorer=None):
        market = pick.market
        if market.startswith("Corners Over ") or market.startswith("Corners Under "):
            corner_total = self._fixture_corner_total(pick.match_id)
            if corner_total is None:
                return None
            try:
                line = float(market.rsplit(" ", 1)[-1])
            except (TypeError, ValueError):
                return None
            if market.startswith("Corners Over "):
                return corner_total > line
            return corner_total < line

        if market == "DNB Home":
            if home_goals == away_goals:
                return None
            return home_goals > away_goals
        if market == "DNB Away":
            if home_goals == away_goals:
                return None
            return away_goals > home_goals

        if market == "First to Score H":
            if home_goals == 0 and away_goals == 0:
                return False
            if home_goals > 0 and away_goals == 0:
                return True
            if away_goals > 0 and home_goals == 0:
                return False
            return first_scorer == home_team if first_scorer else None

        if market == "First to Score A":
            if home_goals == 0 and away_goals == 0:
                return False
            if away_goals > 0 and home_goals == 0:
                return True
            if home_goals > 0 and away_goals == 0:
                return False
            return first_scorer == away_team if first_scorer else None

        total = home_goals + away_goals
        checks = {
            "Home Win": home_goals > away_goals,
            "Away Win": away_goals > home_goals,
            "Draw": home_goals == away_goals,
            "Over 1.5": total >= 2,
            "Over 2.5": total >= 3,
            "Over 3.5": total >= 4,
            "Under 1.5": total <= 1,
            "Under 2.5": total <= 2,
            "Under 3.5": total <= 3,
            "GG / BTTS Yes": home_goals > 0 and away_goals > 0,
            "GG + Over 2.5": home_goals > 0 and away_goals > 0 and total >= 3,
            "DC: 1X": home_goals >= away_goals,
            "DC: X2": away_goals >= home_goals,
            "DC: 12": home_goals != away_goals,
            "Home CS": away_goals == 0,
            "Away CS": home_goals == 0,
            "AH Home +0.5": home_goals >= away_goals,
            "AH Away +0.5": away_goals >= home_goals,
        }
        return checks.get(market)

    def _settle_database_picks(self, target_date):
        fixtures = self._api_football_get(
            "/fixtures",
            {"date": target_date.isoformat(), "timezone": "Africa/Lagos"},
        )
        fixture_map = {
            str((fixture.get("fixture") or {}).get("id")): fixture
            for fixture in fixtures
            if ((fixture.get("fixture") or {}).get("status") or {}).get("short") in {"FT", "AET", "PEN"}
        }

        picks = Pick.objects.filter(
            Q(match_date=target_date)
            | Q(match_date__isnull=True, run__target_date=target_date),
            status=Pick.Status.PENDING,
        )
        predictions = MarketPrediction.objects.filter(
            match_date=target_date,
            status=MarketPrediction.Status.PENDING,
        )
        updated = 0
        predictions_updated = 0
        total_pnl = 0
        settled = []
        settled_predictions = []
        first_scorer_cache = {}

        for pick in picks:
            fixture = fixture_map.get(str(pick.match_id))
            if not fixture:
                continue

            goals = fixture.get("goals") or {}
            home_goals = goals.get("home")
            away_goals = goals.get("away")
            if home_goals is None or away_goals is None:
                continue

            teams = fixture.get("teams") or {}
            home_team = (teams.get("home") or {}).get("name")
            away_team = (teams.get("away") or {}).get("name")
            first_scorer = None
            if "First to Score" in pick.market:
                if pick.match_id not in first_scorer_cache:
                    first_scorer_cache[pick.match_id] = self._first_scorer(pick.match_id)
                first_scorer = first_scorer_cache[pick.match_id]

            won = self._check_market(pick, home_goals, away_goals, home_team, away_team, first_scorer)
            stake = pick.stake or Decimal("0")
            if won is None:
                pick.status = Pick.Status.VOID
                pick.pnl = Decimal("0")
            elif won:
                pick.status = Pick.Status.WIN
                pick.pnl = Decimal(str(round(float(stake) * (float(pick.odds) - 1), 2)))
            else:
                pick.status = Pick.Status.LOSS
                pick.pnl = -stake

            pick.score = f"{home_goals}-{away_goals}"
            if pick.market.startswith("Corners "):
                corner_total = self._fixture_corner_total(pick.match_id)
                pick.result = f"{corner_total} corners" if corner_total is not None else pick.score
            else:
                pick.result = pick.score
            pick.settled_at = timezone.now()
            pick.save(update_fields=["status", "pnl", "score", "result", "settled_at"])

            updated += 1
            total_pnl = round(total_pnl + float(pick.pnl or 0), 2)
            settled.append({
                "id": pick.id,
                "fixture": pick.fixture,
                "market": pick.market,
                "status": pick.status,
                "score": pick.score,
                "pnl": float(pick.pnl or 0),
            })

        for prediction in predictions:
            fixture = fixture_map.get(str(prediction.match_id))
            if not fixture:
                continue

            goals = fixture.get("goals") or {}
            home_goals = goals.get("home")
            away_goals = goals.get("away")
            if home_goals is None or away_goals is None:
                continue

            teams = fixture.get("teams") or {}
            home_team = (teams.get("home") or {}).get("name")
            away_team = (teams.get("away") or {}).get("name")
            first_scorer = None
            if "First to Score" in prediction.market:
                if prediction.match_id not in first_scorer_cache:
                    first_scorer_cache[prediction.match_id] = self._first_scorer(prediction.match_id)
                first_scorer = first_scorer_cache[prediction.match_id]

            won = self._check_market(prediction, home_goals, away_goals, home_team, away_team, first_scorer)
            stake = Decimal("1000")
            if won is None:
                prediction.status = MarketPrediction.Status.VOID
                prediction.pnl_simulated = Decimal("0")
            elif won:
                prediction.status = MarketPrediction.Status.WIN
                prediction.pnl_simulated = Decimal(str(round(float(stake) * (float(prediction.odds) - 1), 2)))
            else:
                prediction.status = MarketPrediction.Status.LOSS
                prediction.pnl_simulated = -stake

            prediction.score = f"{home_goals}-{away_goals}"
            if prediction.market.startswith("Corners "):
                corner_total = self._fixture_corner_total(prediction.match_id)
                prediction.result = f"{corner_total} corners" if corner_total is not None else prediction.score
            else:
                prediction.result = prediction.score
            prediction.settled_at = timezone.now()
            prediction.save(update_fields=["status", "pnl_simulated", "score", "result", "settled_at"])

            predictions_updated += 1
            settled_predictions.append({
                "id": prediction.id,
                "fixture": prediction.fixture,
                "market": prediction.market,
                "published": prediction.published,
                "status": prediction.status,
                "score": prediction.score,
                "pnl_simulated": float(prediction.pnl_simulated or 0),
            })

        return {
            "status": "success",
            "date": target_date.isoformat(),
            "updated_count": updated,
            "database_updated_count": updated,
            "internal_predictions_updated_count": predictions_updated,
            "total_pnl": total_pnl,
            "settled_picks": settled,
            "settled_internal_predictions": settled_predictions,
        }

    def run(self, algo_run: AlgoRun) -> AlgoRun:
        algo_run.status = AlgoRun.Status.RUNNING
        algo_run.started_at = timezone.now()
        algo_run.save(update_fields=["status", "started_at", "updated_at"])

        env = self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(self._performance_profile()),
            "ALGO_STRATEGY_PROFILE": json.dumps(self._strategy_profile(algo_run.target_date)),
        })
        try:
            with temporary_env(env):
                from .grindalgo.algo_runner import run_daily_algo

                result = run_daily_algo()

            status = result.get("status", AlgoRun.Status.SUCCESS)
            algo_run.status = status if status in AlgoRun.Status.values else AlgoRun.Status.SUCCESS
            algo_run.fd_fixtures = result.get("fd_fixtures", 0)
            algo_run.aps_fixtures = result.get("aps_fixtures", 0)
            algo_run.total_scored = result.get("total_scored", 0)
            algo_run.picks_count = result.get("picks_count", 0)
            algo_run.bankers = result.get("bankers", 0)
            algo_run.value_gems = result.get("value_gems", 0)
            algo_run.wild_cards = result.get("wild_cards", 0)
            algo_run.bankroll = result.get("bankroll")
            algo_run.result = result
            self._persist_selected_picks(algo_run, result)
            self._persist_market_predictions(algo_run, result)
        except Exception as exc:
            algo_run.status = AlgoRun.Status.FAILED
            algo_run.error = str(exc)
        finally:
            algo_run.finished_at = timezone.now()
            algo_run.save()

        return algo_run

    def update_results(self, *, target_date=None):
        if target_date is not None:
            settle_date = target_date
        else:
            settle_date = timezone.localdate() - timedelta(days=1)
        return self._settle_database_picks(settle_date)

    def run_auditor(self, *, from_date=None, to_date=None):
        env = self._runner_env()
        if from_date is not None:
            env["AUDITOR_FROM"] = from_date.isoformat()
        if to_date is not None:
            env["AUDITOR_TO"] = to_date.isoformat()
        with temporary_env(env):
            from .grindalgo.auditor_runner import run_auditor

            return run_auditor()


algo_runner_service = AlgoRunnerService()
