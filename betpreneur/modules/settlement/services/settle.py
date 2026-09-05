"""Grading picks and slip legs against final results.

Settlement sits above both picks and slips because it writes outcomes into
each. It is the only module allowed to import both — they are peers and must
never reach across to one another, so anything that needs to touch both lands
here.

Extracted from AlgoRunnerService, where settling a slip leg and settling a
daily pick shared the same finished-fixture lookup but lived among 90 other
methods about generating picks.
"""
from __future__ import annotations

import gc
import logging
from datetime import timedelta
from decimal import Decimal

import requests
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    FixtureCache,
    ProviderFixtureMap,
    SlipReviewMarketCache,
    StatPalFixtureSnapshot,
    runner_env,
)
from betpreneur.modules.markets.api import can_settle_market
from betpreneur.modules.picks.api import MarketPrediction, Pick
from betpreneur.modules.prediction.api import record_team_match_feedback
from betpreneur.modules.slips.api import SlipSelection

from ..models import SettlementRun
from .recording import recorded

log = logging.getLogger(__name__)

INTERNAL_SETTLEMENT_BATCH_SIZE = 250


class SettlementService:
    """Reads final results once, then grades both products against them."""


    def __init__(self) -> None:
        self._corner_total_cache: dict = {}

    def _runner_env(self, extra=None):
        return runner_env(extra)

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


    def _check_market(
        self,
        pick,
        home_goals,
        away_goals,
        home_team=None,
        away_team=None,
        first_scorer=None,
        actual_stats=None,
    ):
        market = pick.market
        total = home_goals + away_goals
        goal_line_result = self._goal_line_result(market, total)
        if goal_line_result is not None:
            return goal_line_result

        team_goal_line_result = self._team_goal_line_result(market, home_goals, away_goals)
        if team_goal_line_result is not None:
            return team_goal_line_result

        stat_line_result = self._stat_line_result(market, actual_stats or {})
        if stat_line_result is not None:
            return stat_line_result

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

        checks = {
            "Home Win": home_goals > away_goals,
            "Away Win": away_goals > home_goals,
            "Draw": home_goals == away_goals,
            "GG / BTTS Yes": home_goals > 0 and away_goals > 0,
            "BTTS No": home_goals == 0 or away_goals == 0,
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

    def _market_result_text(self, market, match_id, score, actual_stats=None):
        stat_value = self._market_stat_value(market, actual_stats or {})
        stat_name = self._market_stat_label(market)
        if stat_value is None and market.startswith("Corners "):
            stat_value = self._fixture_corner_total(match_id)
            stat_name = "corners"
        if stat_value is None or not stat_name:
            return score
        return f"{self._format_stat_value(stat_value)} {stat_name}"

    @classmethod
    def _stat_line_result(cls, market, actual_stats):
        stat_value = cls._market_stat_value(market, actual_stats)
        if stat_value is None:
            return None
        parsed = cls._parse_stat_line_market(market)
        if not parsed:
            return None
        _side, _stat, direction, line = parsed
        return stat_value > line if direction == "Over" else stat_value < line

    @classmethod
    def _market_stat_value(cls, market, actual_stats):
        parsed = cls._parse_stat_line_market(market)
        if not parsed:
            return None
        side, stat, _direction, _line = parsed
        if side:
            return cls._side_stat_value(actual_stats, side, stat)
        return cls._total_stat_value(actual_stats, stat)

    @staticmethod
    def _parse_stat_line_market(market):
        text = str(market or "").strip()
        prefixes = (
            ("Home Team Shots On Target ", "home", "shots_on_target"),
            ("Away Team Shots On Target ", "away", "shots_on_target"),
            ("Shots On Target ", None, "shots_on_target"),
            ("Home Team Corners ", "home", "corners"),
            ("Away Team Corners ", "away", "corners"),
            ("Corners ", None, "corners"),
            ("Home Team Cards ", "home", "cards"),
            ("Away Team Cards ", "away", "cards"),
            ("Cards ", None, "cards"),
        )
        for prefix, side, stat in prefixes:
            if not text.startswith(prefix):
                continue
            parts = text[len(prefix):].split()
            if len(parts) != 2 or parts[0] not in {"Over", "Under"}:
                return None
            try:
                line = float(parts[1])
            except (TypeError, ValueError):
                return None
            return side, stat, parts[0], line
        return None

    @classmethod
    def _total_stat_value(cls, actual_stats, stat):
        home = cls._side_stat_value(actual_stats, "home", stat)
        away = cls._side_stat_value(actual_stats, "away", stat)
        if home is None or away is None:
            return None
        return home + away

    @classmethod
    def _side_stat_value(cls, actual_stats, side, stat):
        if not isinstance(actual_stats, dict):
            return None
        side_stats = actual_stats.get(side) if isinstance(actual_stats.get(side), dict) else {}
        if stat == "cards":
            return cls._cards_total(side_stats)
        value = side_stats.get(stat)
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _market_stat_label(market):
        parsed = SettlementService._parse_stat_line_market(market)
        if not parsed:
            return ""
        side, stat, _direction, _line = parsed
        labels = {
            "cards": "cards",
            "corners": "corners",
            "shots_on_target": "shots on target",
        }
        prefix = f"{side} team " if side else ""
        return f"{prefix}{labels.get(stat, stat)}"

    @staticmethod
    def _format_stat_value(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return str(int(number))
        return str(round(number, 2))

    @staticmethod
    def _goal_line_result(market, total_goals):
        parts = str(market or "").strip().split()
        if len(parts) != 2 or parts[0] not in {"Over", "Under"}:
            return None
        try:
            line = float(parts[1])
        except (TypeError, ValueError):
            return None
        if line not in {1.5, 2.5, 3.5, 4.5}:
            return None
        return total_goals > line if parts[0] == "Over" else total_goals < line

    @staticmethod
    def _team_goal_line_result(market, home_goals, away_goals):
        parts = str(market or "").strip().split()
        if len(parts) != 4 or parts[:2] not in (["Home", "Team"], ["Away", "Team"]):
            return None
        if parts[2] not in {"Over", "Under"}:
            return None
        try:
            line = float(parts[3])
        except (TypeError, ValueError):
            return None
        if line not in {0.5, 1.5, 2.5}:
            return None
        goals = home_goals if parts[0] == "Home" else away_goals
        return goals > line if parts[2] == "Over" else goals < line

    def _finished_fixture_map(self, target_date, match_ids=None):
        fixture_map = {}
        wanted_keys = {str(match_id or "").strip() for match_id in (match_ids or []) if str(match_id or "").strip()}
        wanted_provider_ids = {
            key.replace("statpal:", "", 1)
            for key in wanted_keys
            if key.startswith("statpal:") and key.replace("statpal:", "", 1)
        }

        def add_fixture(keys, fixture):
            normalized_keys = {str(key or "").strip() for key in keys if str(key or "").strip()}
            if wanted_keys and not normalized_keys.intersection(wanted_keys | wanted_provider_ids):
                return
            for key in keys:
                key = str(key or "").strip()
                if key:
                    fixture_map.setdefault(key, fixture)

        try:
            fixtures = self._api_football_get(
                "/fixtures",
                {"date": target_date.isoformat(), "timezone": "Africa/Lagos"},
            )
        except Exception as exc:
            log.warning("API-Football result lookup failed date=%s error=%s", target_date, exc)
            fixtures = []

        for fixture in fixtures or []:
            fixture_id = (fixture.get("fixture") or {}).get("id")
            if ((fixture.get("fixture") or {}).get("status") or {}).get("short") not in {"FT", "AET", "PEN"}:
                continue
            keys = [fixture_id]
            if wanted_keys and str(fixture_id or "") not in wanted_keys:
                mapped_keys = set(
                    ProviderFixtureMap.objects.filter(api_fixture_id=str(fixture_id), active=True)
                    .values_list("provider_event_id", flat=True)
                )
                if not mapped_keys.intersection(wanted_keys | wanted_provider_ids):
                    continue
            for mapping in ProviderFixtureMap.objects.filter(api_fixture_id=str(fixture_id), active=True):
                keys.extend([mapping.api_fixture_id, mapping.provider_event_id, f"{mapping.provider}:{mapping.provider_event_id}"])
                if mapping.provider == "statpal":
                    keys.append(f"statpal:{mapping.provider_event_id}")
            add_fixture(keys, fixture)

        cached_query = FixtureCache.objects.filter(match_date=target_date, source="statpal")
        if wanted_keys:
            cached_query = cached_query.filter(Q(match_id__in=wanted_keys) | Q(match_id__in=wanted_provider_ids))
        for cached in cached_query.only("match_id", "home_team", "away_team", "api_payload").iterator(chunk_size=100):
            fixture = self._statpal_cached_finished_fixture(cached)
            if not fixture:
                continue
            payload = cached.api_payload or {}
            provider_match_id = str(
                payload.get("provider_match_id")
                or payload.get("statpal_provider_match_id")
                or payload.get("main_id")
                or str(cached.match_id).replace("statpal:", "", 1)
                or ""
            )
            keys = [cached.match_id, provider_match_id, f"statpal:{provider_match_id}" if provider_match_id else ""]
            mapped = ProviderFixtureMap.objects.filter(
                provider="statpal",
                provider_event_id=provider_match_id,
                active=True,
            ).first()
            if mapped:
                keys.extend([mapped.api_fixture_id, mapped.provider_event_id, f"statpal:{mapped.provider_event_id}"])
            add_fixture(keys, fixture)

        slip_cache_query = SlipReviewMarketCache.objects.filter(match_date=target_date).exclude(provider_match_id="")
        if wanted_keys:
            slip_cache_query = slip_cache_query.filter(
                Q(match_id__in=wanted_keys)
                | Q(match_id__in=wanted_provider_ids)
                | Q(provider_match_id__in=wanted_keys)
                | Q(provider_match_id__in=wanted_provider_ids)
            )
        for row in slip_cache_query.only(
            "match_id",
            "provider_match_id",
            "home_team",
            "away_team",
            "fixture_payload",
        ).iterator(chunk_size=100):
            fixture = self._statpal_payload_finished_fixture(
                row.fixture_payload or {},
                match_id=row.match_id,
                home_team=row.home_team,
                away_team=row.away_team,
            )
            if fixture:
                add_fixture([row.match_id, row.provider_match_id, f"statpal:{row.provider_match_id}"], fixture)

        return fixture_map

    @staticmethod
    def _status_is_finished(status):
        normalized = str(status or "").strip().lower().replace("_", " ")
        return normalized in {
            "ft",
            "aet",
            "pen",
            "finished",
            "full time",
            "fulltime",
            "after extra time",
            "after penalties",
            "complete",
            "completed",
            "ended",
        }

    @staticmethod
    def _settlement_int_or_none(value):
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _statpal_cached_finished_fixture(self, cached):
        return self._statpal_payload_finished_fixture(
            cached.api_payload or {},
            match_id=cached.match_id,
            home_team=cached.home_team,
            away_team=cached.away_team,
        )

    def _statpal_payload_finished_fixture(self, payload, *, match_id="", home_team="", away_team=""):
        payload = payload if isinstance(payload, dict) else {}
        fixture_payload = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
        home_payload = payload.get("home") if isinstance(payload.get("home"), dict) else {}
        away_payload = payload.get("away") if isinstance(payload.get("away"), dict) else {}
        status = payload.get("status") or fixture_payload.get("status")
        if isinstance(status, dict):
            status = status.get("short") or status.get("long") or status.get("status")
        home_goals = self._settlement_int_or_none(
            payload.get("home_goals")
            if payload.get("home_goals") not in (None, "")
            else payload.get("ft_home_goals")
        )
        away_goals = self._settlement_int_or_none(
            payload.get("away_goals")
            if payload.get("away_goals") not in (None, "")
            else payload.get("ft_away_goals")
        )
        if home_goals is None or away_goals is None:
            goals = payload.get("goals") if isinstance(payload.get("goals"), dict) else {}
            home_goals = self._settlement_int_or_none(goals.get("home"))
            away_goals = self._settlement_int_or_none(goals.get("away"))
        if home_goals is None or away_goals is None:
            return None
        if status and not self._status_is_finished(status):
            return None

        provider_match_id = str(
            payload.get("provider_match_id")
            or payload.get("statpal_provider_match_id")
            or payload.get("main_id")
            or str(match_id).replace("statpal:", "", 1)
            or ""
        )
        home = (
            home_team
            or payload.get("home_name")
            or payload.get("hname")
            or home_payload.get("name")
        )
        away = (
            away_team
            or payload.get("away_name")
            or payload.get("aname")
            or away_payload.get("name")
        )
        return {
            "fixture": {
                "id": match_id or f"statpal:{provider_match_id}" if provider_match_id else match_id,
                "status": {"short": "FT", "long": str(status or "finished")},
            },
            "goals": {"home": home_goals, "away": away_goals},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "actual_stats": self._statpal_actual_stats(payload),
            "provider": "statpal",
            "provider_match_id": provider_match_id,
        }

    def _statpal_actual_stats(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        team_stats = payload.get("team_stats") if isinstance(payload.get("team_stats"), dict) else {}
        return {
            "home": self._statpal_side_actuals(payload, team_stats, "home"),
            "away": self._statpal_side_actuals(payload, team_stats, "away"),
            "referee_name": self._statpal_referee_name(payload),
        }

    def _statpal_side_actuals(self, payload, team_stats, side):
        stats = team_stats.get(side) if isinstance(team_stats.get(side), dict) else {}
        return {
            "corners": self._statpal_nested_float(stats, "corners", "total"),
            "expected_goals": self._statpal_nested_float(stats, "expected_goals", "total"),
            "fouls": self._statpal_nested_float(stats, "fouls", "total"),
            "yellow_cards": self._statpal_event_count(payload, side, "yellowcards"),
            "red_cards": self._statpal_event_count(payload, side, "redcards"),
            "shots_on_target": self._statpal_nested_float(stats, "shots_on_goal", "total")
            or self._statpal_nested_float(stats, "shots_on_target", "total"),
        }

    @staticmethod
    def _statpal_nested_float(stats, group, key):
        group_payload = stats.get(group) if isinstance(stats, dict) else None
        if not isinstance(group_payload, dict):
            return None
        try:
            value = group_payload.get(key)
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _statpal_referee_name(payload):
        referee = payload.get("referee")
        if isinstance(referee, dict) and referee.get("name"):
            return str(referee.get("name") or "")
        match_info = payload.get("match_info") if isinstance(payload.get("match_info"), dict) else {}
        referee = match_info.get("referee")
        if isinstance(referee, dict):
            return str(referee.get("name") or "")
        return str(referee or "")

    @staticmethod
    def _statpal_event_count(payload, side, event_name):
        event_summary = payload.get("event_summary") if isinstance(payload.get("event_summary"), dict) else {}
        side_events = event_summary.get(side) if isinstance(event_summary.get(side), dict) else {}
        if event_name not in side_events:
            return None
        events = side_events.get(event_name)
        if events in ("", None):
            return 0
        if isinstance(events, dict):
            events = events.get("event")
        if events in ("", None):
            return 0
        if isinstance(events, list):
            return len(events)
        if isinstance(events, dict):
            return 1
        return None

    @staticmethod
    def _fixture_goals(fixture):
        goals = fixture.get("goals") or {}
        return goals.get("home"), goals.get("away")

    @staticmethod
    def _fixture_team_names(fixture):
        teams = fixture.get("teams") or {}
        return (teams.get("home") or {}).get("name"), (teams.get("away") or {}).get("name")

    def _settle_database_picks(self, target_date):
        self._corner_total_cache.clear()
        picks = Pick.objects.filter(
            Q(match_date=target_date)
            | Q(match_date__isnull=True, run__target_date=target_date),
            status=Pick.Status.PENDING,
        )
        predictions = MarketPrediction.objects.filter(
            match_date=target_date,
            status=MarketPrediction.Status.PENDING,
        ).only(
            "id",
            "match_date",
            "fixture",
            "match_id",
            "market",
            "odds",
            "published",
            "status",
            "pnl_simulated",
            "score",
            "result",
            "settled_at",
        ).order_by("id")
        pending_match_ids = set(
            picks.exclude(match_id="").values_list("match_id", flat=True).distinct()
        )
        pending_match_ids.update(
            predictions.exclude(match_id="").values_list("match_id", flat=True).distinct()
        )
        if not pending_match_ids:
            return {
                "status": "success",
                "date": target_date.isoformat(),
                "updated_count": 0,
                "database_updated_count": 0,
                "internal_predictions_updated_count": 0,
                "internal_prediction_status_counts": {"win": 0, "loss": 0, "void": 0},
                "total_pnl": 0,
                "settled_picks": [],
                "settled_internal_predictions": [],
                "settled_picks_sampled": False,
                "settled_internal_predictions_sampled": False,
            }
        fixture_map = self._finished_fixture_map(target_date, pending_match_ids)
        updated = 0
        predictions_updated = 0
        total_pnl = 0
        settled_sample = []
        settled_predictions_sample = []
        prediction_status_counts = {"win": 0, "loss": 0, "void": 0}
        first_scorer_cache = {}
        feedback_recorded = set()
        prediction_updates = []
        settlement_now = timezone.now()

        def flush_prediction_updates():
            if not prediction_updates:
                return
            MarketPrediction.objects.bulk_update(
                prediction_updates,
                ["status", "pnl_simulated", "score", "result", "settled_at"],
                batch_size=INTERNAL_SETTLEMENT_BATCH_SIZE,
            )
            prediction_updates.clear()
            close_old_connections()
            gc.collect()

        def record_feedback_once(match_id, fixture):
            key = str(match_id or "")
            if not key or key in feedback_recorded:
                return
            self._record_team_match_feedback(
                fixture,
                target_date=target_date,
                match_id=key,
            )
            feedback_recorded.add(key)

        for pick in picks:
            fixture = fixture_map.get(str(pick.match_id))
            if not fixture:
                continue

            goals = fixture.get("goals") or {}
            home_goals = goals.get("home")
            away_goals = goals.get("away")
            if home_goals is None or away_goals is None:
                continue
            record_feedback_once(pick.match_id, fixture)

            teams = fixture.get("teams") or {}
            home_team = (teams.get("home") or {}).get("name")
            away_team = (teams.get("away") or {}).get("name")
            actual_stats = fixture.get("actual_stats") if isinstance(fixture.get("actual_stats"), dict) else {}
            first_scorer = None
            if "First to Score" in pick.market:
                if pick.match_id not in first_scorer_cache:
                    first_scorer_cache[pick.match_id] = self._first_scorer(pick.match_id)
                first_scorer = first_scorer_cache[pick.match_id]

            won = self._check_market(
                pick,
                home_goals,
                away_goals,
                home_team,
                away_team,
                first_scorer,
                actual_stats,
            )
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
            pick.result = self._market_result_text(pick.market, pick.match_id, pick.score, actual_stats)
            pick.settled_at = timezone.now()
            pick.save(update_fields=["status", "pnl", "score", "result", "settled_at"])

            updated += 1
            total_pnl = round(total_pnl + float(pick.pnl or 0), 2)
            if len(settled_sample) < 100:
                settled_sample.append({
                    "id": pick.id,
                    "fixture": pick.fixture,
                    "market": pick.market,
                    "status": pick.status,
                    "score": pick.score,
                    "pnl": float(pick.pnl or 0),
                })

        last_prediction_id = 0
        while True:
            prediction_batch = list(
                predictions.filter(id__gt=last_prediction_id)[:INTERNAL_SETTLEMENT_BATCH_SIZE]
            )
            if not prediction_batch:
                break

            for prediction in prediction_batch:
                last_prediction_id = prediction.id
                fixture = fixture_map.get(str(prediction.match_id))
                if not fixture:
                    continue

                goals = fixture.get("goals") or {}
                home_goals = goals.get("home")
                away_goals = goals.get("away")
                if home_goals is None or away_goals is None:
                    continue
                record_feedback_once(prediction.match_id, fixture)

                teams = fixture.get("teams") or {}
                home_team = (teams.get("home") or {}).get("name")
                away_team = (teams.get("away") or {}).get("name")
                actual_stats = fixture.get("actual_stats") if isinstance(fixture.get("actual_stats"), dict) else {}
                first_scorer = None
                if "First to Score" in prediction.market:
                    if prediction.match_id not in first_scorer_cache:
                        first_scorer_cache[prediction.match_id] = self._first_scorer(prediction.match_id)
                    first_scorer = first_scorer_cache[prediction.match_id]

                won = self._check_market(
                    prediction,
                    home_goals,
                    away_goals,
                    home_team,
                    away_team,
                    first_scorer,
                    actual_stats,
                )
                stake = Decimal("1000")
                if won is None:
                    prediction.status = MarketPrediction.Status.VOID
                    prediction.pnl_simulated = Decimal("0")
                    prediction_status_counts["void"] += 1
                elif won:
                    prediction.status = MarketPrediction.Status.WIN
                    prediction.pnl_simulated = Decimal(str(round(float(stake) * (float(prediction.odds) - 1), 2)))
                    prediction_status_counts["win"] += 1
                else:
                    prediction.status = MarketPrediction.Status.LOSS
                    prediction.pnl_simulated = -stake
                    prediction_status_counts["loss"] += 1

                prediction.score = f"{home_goals}-{away_goals}"
                prediction.result = self._market_result_text(
                    prediction.market,
                    prediction.match_id,
                    prediction.score,
                    actual_stats,
                )
                prediction.settled_at = settlement_now
                prediction_updates.append(prediction)

                predictions_updated += 1
                if len(settled_predictions_sample) < 100:
                    settled_predictions_sample.append({
                        "id": prediction.id,
                        "fixture": prediction.fixture,
                        "market": prediction.market,
                        "published": prediction.published,
                        "status": prediction.status,
                        "score": prediction.score,
                        "pnl_simulated": float(prediction.pnl_simulated or 0),
                    })

            flush_prediction_updates()

        flush_prediction_updates()

        return {
            "status": "success",
            "date": target_date.isoformat(),
            "updated_count": updated,
            "database_updated_count": updated,
            "internal_predictions_updated_count": predictions_updated,
            "internal_prediction_status_counts": prediction_status_counts,
            "total_pnl": total_pnl,
            "settled_picks": settled_sample,
            "settled_internal_predictions": settled_predictions_sample,
            "settled_picks_sampled": updated > len(settled_sample),
            "settled_internal_predictions_sampled": predictions_updated > len(settled_predictions_sample),
        }

    def _record_team_match_feedback(self, fixture, *, target_date, match_id):
        goals = fixture.get("goals") or {}
        home_goals = goals.get("home")
        away_goals = goals.get("away")
        if home_goals is None or away_goals is None:
            return
        teams = fixture.get("teams") or {}
        home_team = (teams.get("home") or {}).get("name") or ""
        away_team = (teams.get("away") or {}).get("name") or ""
        if not home_team or not away_team:
            return

        actual_stats = fixture.get("actual_stats") or {}
        if not self._actual_stats_have_counts(actual_stats):
            detailed_stats = self._statpal_detailed_actual_stats(match_id, fixture.get("provider_match_id"))
            if detailed_stats:
                actual_stats = detailed_stats
        prediction_snapshot = self._prediction_feedback_snapshot(target_date=target_date, match_id=match_id)
        fixture_id = str((fixture.get("fixture") or {}).get("id") or match_id or "")
        provider_match_id = str(fixture.get("provider_match_id") or "").strip()
        source = str(fixture.get("provider") or "settlement")

        home_result, away_result = self._team_results(home_goals, away_goals)
        sides = (
            ("home", home_team, away_team, home_goals, away_goals, home_result),
            ("away", away_team, home_team, away_goals, home_goals, away_result),
        )
        for side, team, opponent, goals_for, goals_against, result in sides:
            opponent_side = "away" if side == "home" else "home"
            side_actuals = actual_stats.get(side) if isinstance(actual_stats.get(side), dict) else {}
            opponent_actuals = (
                actual_stats.get(opponent_side) if isinstance(actual_stats.get(opponent_side), dict) else {}
            )
            record_team_match_feedback(
                fixture_id=fixture_id,
                provider_match_id=provider_match_id,
                fixture_name=f"{home_team} vs {away_team}",
                match_date=target_date,
                team_name=team,
                opponent_name=opponent,
                side=side,
                actual_result=result,
                goals_for=goals_for,
                goals_against=goals_against,
                corners_for=side_actuals.get("corners"),
                corners_against=opponent_actuals.get("corners"),
                cards_for=self._cards_total(side_actuals),
                cards_against=self._cards_total(opponent_actuals),
                shots_on_target_for=side_actuals.get("shots_on_target"),
                shots_on_target_against=opponent_actuals.get("shots_on_target"),
                referee_name=str(actual_stats.get("referee_name") or ""),
                source=source,
                prediction_snapshot=prediction_snapshot,
                actual_stats={
                    "team": side_actuals,
                    "opponent": opponent_actuals,
                    "score": f"{home_goals}-{away_goals}",
                },
            )

    @staticmethod
    def _team_results(home_goals, away_goals):
        if home_goals == away_goals:
            return "draw", "draw"
        if home_goals > away_goals:
            return "win", "loss"
        return "loss", "win"

    @staticmethod
    def _cards_total(actuals):
        yellow = actuals.get("yellow_cards")
        red = actuals.get("red_cards")
        if yellow is None and red is None:
            return None
        return float(yellow or 0) + float(red or 0)

    @staticmethod
    def _actual_stats_have_counts(actual_stats):
        if not isinstance(actual_stats, dict):
            return False
        for side in ("home", "away"):
            side_stats = actual_stats.get(side) if isinstance(actual_stats.get(side), dict) else {}
            if side_stats.get("corners") is not None or side_stats.get("yellow_cards") is not None:
                return True
        return False

    def _statpal_detailed_actual_stats(self, match_id, provider_match_id=""):
        keys = {
            str(match_id or "").strip(),
            str(provider_match_id or "").strip(),
            f"statpal:{str(provider_match_id or '').strip()}" if provider_match_id else "",
        }
        keys = {key for key in keys if key}
        if not keys:
            return {}
        snapshot = (
            StatPalFixtureSnapshot.objects.filter(
                snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
                status="available",
            )
            .filter(Q(match_id__in=keys) | Q(provider_match_id__in=keys))
            .order_by("-updated_at")
            .first()
        )
        if not snapshot:
            return {}
        return self._statpal_actual_stats(snapshot.payload or {})

    @staticmethod
    def _prediction_feedback_snapshot(*, target_date, match_id):
        predictions = (
            MarketPrediction.objects.filter(match_date=target_date, match_id=match_id)
            .only(
                "market",
                "confidence",
                "raw_confidence",
                "odds",
                "odds_source",
                "eligible",
                "published",
                "insights",
            )
            .order_by("-published", "-eligible", "-confidence", "market")[:40]
        )
        markets = []
        for prediction in predictions:
            insights = prediction.insights or {}
            taxonomy = insights.get("market_taxonomy") or {}
            markets.append({
                "market": prediction.market,
                "market_family": taxonomy.get("family") or insights.get("market_family") or "",
                "confidence": prediction.confidence,
                "raw_confidence": prediction.raw_confidence,
                "raw_probability": insights.get("raw_probability"),
                "calibrated_probability": insights.get("calibrated_probability"),
                "odds": float(prediction.odds) if prediction.odds is not None else None,
                "odds_source": prediction.odds_source,
                "eligible": prediction.eligible,
                "published": prediction.published,
                "model_sources": insights.get("model_sources") or [],
                "data_status": insights.get("data_status", ""),
            })
        return {"markets": markets, "market_count": len(predictions)}

    def update_results(self, *, target_date=None):
        if target_date is not None:
            settle_date = target_date
        else:
            settle_date = timezone.localdate() - timedelta(days=1)
        return recorded(
            SettlementRun.Scope.PICKS,
            settle_date,
            lambda: self._settle_database_picks(settle_date),
        )

    def update_recent_results(self, *, days=7, end_date=None):
        try:
            window_days = max(1, min(int(days or 7), 60))
        except (TypeError, ValueError):
            window_days = 7
        last_date = end_date or (timezone.localdate() - timedelta(days=1))
        first_date = last_date - timedelta(days=window_days - 1)
        results = []
        current = first_date
        while current <= last_date:
            results.append(self.update_results(target_date=current))
            close_old_connections()
            gc.collect()
            current += timedelta(days=1)
        return {
            "status": "success",
            "start_date": first_date.isoformat(),
            "end_date": last_date.isoformat(),
            "days": window_days,
            "updated_count": sum(int(item.get("updated_count") or 0) for item in results),
            "internal_predictions_updated_count": sum(
                int(item.get("internal_predictions_updated_count") or 0) for item in results
            ),
            "results": results,
        }

    def settle_slip_selections(self, *, target_date=None):
        """
        Resolve user slip legs against finished fixtures.

        Unlike Pick settlement, users submit arbitrary bookmaker markets, so a market
        this engine cannot resolve is recorded as ``unsettleable`` instead of being
        silently counted as a void. Only settled legs feed the accuracy stats.
        """
        settle_date = target_date or (timezone.localdate() - timedelta(days=1))
        return recorded(
            SettlementRun.Scope.SLIPS,
            settle_date,
            lambda: self._settle_slip_selections(settle_date),
        )

    def _settle_slip_selections(self, settle_date):
        selections = list(
            SlipSelection.objects.filter(
                match_date=settle_date,
                outcome=SlipSelection.Outcome.PENDING,
            )
        )
        if not selections:
            return {
                "target_date": settle_date.isoformat(),
                "considered": 0,
                "settled": 0,
                "wins": 0,
                "losses": 0,
                "void": 0,
                "unsettleable": 0,
                "awaiting_result": 0,
                "flagged_risky_losses": 0,
            }

        fixture_map = self._finished_fixture_map(settle_date)
        first_scorer_cache = {}
        counts = {"win": 0, "loss": 0, "void": 0, "unsettleable": 0}
        awaiting = 0
        flagged_risky_losses = 0
        settled_rows = []

        for selection in selections:
            if not can_settle_market(selection.market):
                selection.outcome = SlipSelection.Outcome.UNSETTLEABLE
                selection.result = "Market not supported by the settlement engine."
                selection.settled_at = timezone.now()
                settled_rows.append(selection)
                counts["unsettleable"] += 1
                continue

            fixture = fixture_map.get(str(selection.match_id))
            if not fixture:
                awaiting += 1
                continue

            home_goals, away_goals = self._fixture_goals(fixture)
            if home_goals is None or away_goals is None:
                awaiting += 1
                continue

            home_team, away_team = self._fixture_team_names(fixture)
            first_scorer = None
            if "First to Score" in selection.market:
                if selection.match_id not in first_scorer_cache:
                    first_scorer_cache[selection.match_id] = self._first_scorer(selection.match_id)
                first_scorer = first_scorer_cache[selection.match_id]

            won = self._check_market(selection, home_goals, away_goals, home_team, away_team, first_scorer)
            if won is None:
                selection.outcome = SlipSelection.Outcome.VOID
                counts["void"] += 1
            elif won:
                selection.outcome = SlipSelection.Outcome.WIN
                counts["win"] += 1
            else:
                selection.outcome = SlipSelection.Outcome.LOSS
                counts["loss"] += 1
                if selection.flagged_risky:
                    flagged_risky_losses += 1

            selection.score = f"{home_goals}-{away_goals}"
            if selection.market.startswith("Corners "):
                corner_total = self._fixture_corner_total(selection.match_id)
                selection.result = f"{corner_total} corners" if corner_total is not None else selection.score
            else:
                selection.result = selection.score
            selection.settled_at = timezone.now()
            settled_rows.append(selection)

        if settled_rows:
            SlipSelection.objects.bulk_update(
                settled_rows,
                ["outcome", "score", "result", "settled_at"],
                batch_size=200,
            )

        return {
            "target_date": settle_date.isoformat(),
            "considered": len(selections),
            "settled": len(settled_rows),
            "wins": counts["win"],
            "losses": counts["loss"],
            "void": counts["void"],
            "unsettleable": counts["unsettleable"],
            "awaiting_result": awaiting,
            "flagged_risky_losses": flagged_risky_losses,
        }

settlement_service = SettlementService()
