"""The daily run.

Orchestrates a run end to end: select fixtures, score them, price the markets,
ask the council, and publish. Extracted from apps/algo/services.py, where it
sat alongside slip importing and fixture search.

Settlement used to live on this class too; it moved to modules/settlement,
which is the only module allowed to write into both picks and slips.
"""
from __future__ import annotations

import gc
import json
import logging
import math
import os
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Count, Q
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    FixtureCache,
    FixtureSearchService,
    SlipReviewMarketCache,
    daily_tracked_league_ids,
    normalize_fixture_text,
    token_side_score,
)
from betpreneur.modules.explanations.api import CAUTION, REJECT
from betpreneur.modules.markets.api import (
    daily_catalog_entry,
    daily_discovery_market_names,
    daily_market_family_payload,
    daily_odds_key_map,
)
from betpreneur.modules.picks.models import (
    AlgoFixture,
    AlgoRun,
    MarketPrediction,
    Pick,
    StrategyReview,
)
from betpreneur.modules.prediction.api import (
    assess_market_value,
    predict_fixture,
    score_recommendation,
)
from betpreneur.modules.pricing.api import (
    assess_all_games_policy,
    assess_calibration_trust,
    assess_league_market_trust,
    assess_recommendation,
    assess_top_picks_policy,
)
from betpreneur.platform.config import temporary_env
from betpreneur.platform.db.json import json_safe

log = logging.getLogger(__name__)


class AlgoRunnerService:
    """
    Transitional service boundary around the legacy single-file runner.

    The API and database now depend on this service, not on algo_runner.py
    directly. Next we can move fixture fetching, scoring, selection, reports,
    and integrations out of algo_runner.py one module at a time.
    """

    def create_run(self, *, user=None, target_date=None) -> AlgoRun:
        if target_date is None:
            target_date = timezone.localdate()
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

    def _runner_env_int(self, name, default):
        value = self._runner_env().get(name, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _limit_fixtures(self, fixtures):
        try:
            max_fixtures = int(os.environ.get("APS_MAX_FIXTURES", "0") or 0)
        except (TypeError, ValueError):
            max_fixtures = 0
        if max_fixtures > 0 and len(fixtures) > max_fixtures:
            return fixtures[:max_fixtures]
        return fixtures

    def _text(self, value):
        return "" if value is None else str(value)

    def _cached_fixture_runner_payload(self, cached: FixtureCache):
        payload = dict(cached.api_payload or {})
        raw_fixture = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
        raw_teams = payload.get("teams") if isinstance(payload.get("teams"), dict) else {}
        raw_league = payload.get("league") if isinstance(payload.get("league"), dict) else {}
        raw_home = raw_teams.get("home") if isinstance(raw_teams.get("home"), dict) else {}
        raw_away = raw_teams.get("away") if isinstance(raw_teams.get("away"), dict) else {}

        if raw_fixture and raw_home and raw_away:
            payload = {
                "fixture": f"{raw_home.get('name') or cached.home_team} vs {raw_away.get('name') or cached.away_team}",
                "hname": raw_home.get("name") or cached.home_team,
                "aname": raw_away.get("name") or cached.away_team,
                "home_logo": raw_home.get("logo") or cached.home_logo,
                "away_logo": raw_away.get("logo") or cached.away_logo,
                "hid": raw_home.get("id"),
                "aid": raw_away.get("id"),
                "league": raw_league.get("name") or cached.league,
                "league_logo": raw_league.get("logo") or cached.league_logo,
                "country": raw_league.get("country") or cached.country,
                "country_flag": raw_league.get("flag") or cached.country_flag,
                "round": raw_league.get("round") or cached.round,
                "league_type": raw_league.get("type") or cached.league_type,
                "code": str(raw_league.get("id") or ""),
                "kickoff": cached.kickoff,
                "kickoff_utc": raw_fixture.get("date") or (cached.kickoff_utc.isoformat() if cached.kickoff_utc else ""),
                "match_id": raw_fixture.get("id") or cached.match_id,
                "source": "aps_provider_lookup",
                "aps_id": raw_fixture.get("id") or cached.match_id,
                "date": cached.match_date,
                "season": raw_league.get("season"),
            }

        payload.setdefault("fixture", cached.fixture)
        payload.setdefault("hname", cached.home_team)
        payload.setdefault("aname", cached.away_team)
        payload.setdefault("home_team", cached.home_team)
        payload.setdefault("away_team", cached.away_team)
        payload.setdefault("home_logo", cached.home_logo)
        payload.setdefault("away_logo", cached.away_logo)
        payload.setdefault("league", cached.league)
        payload.setdefault("league_logo", cached.league_logo)
        payload.setdefault("country", cached.country)
        payload.setdefault("country_flag", cached.country_flag)
        payload.setdefault("round", cached.round)
        payload.setdefault("league_type", cached.league_type)
        payload.setdefault("kickoff", cached.kickoff)
        payload.setdefault("kickoff_utc", cached.kickoff_utc.isoformat() if cached.kickoff_utc else "")
        payload.setdefault("match_id", cached.match_id)
        payload.setdefault("date", cached.match_date)
        if cached.source == "statpal":
            provider_match_id = str(
                payload.get("provider_match_id")
                or payload.get("main_id")
                or str(cached.match_id).replace("statpal:", "", 1)
                or ""
            )
            provider_competition_id = str(
                payload.get("provider_competition_id")
                or payload.get("statpal_provider_competition_id")
                or payload.get("code")
                or ""
            )
            home_team_id = str(payload.get("provider_home_team_id") or payload.get("statpal_home_team_id") or payload.get("hid") or "")
            away_team_id = str(payload.get("provider_away_team_id") or payload.get("statpal_away_team_id") or payload.get("aid") or "")
            payload["source"] = payload.get("source") or "statpal_daily_cache"
            payload["statpal_match_id"] = cached.match_id
            payload["statpal_provider_match_id"] = provider_match_id
            payload["statpal_provider_competition_id"] = provider_competition_id
            payload["statpal_home_team_id"] = home_team_id
            payload["statpal_away_team_id"] = away_team_id
            payload.setdefault("provider_match_id", provider_match_id)
            payload.setdefault("provider_competition_id", provider_competition_id)
            payload.setdefault("code", provider_competition_id)
            payload.setdefault("hid", home_team_id)
            payload.setdefault("aid", away_team_id)
        elif str(cached.match_id).isdigit():
            payload.setdefault("aps_id", cached.match_id)
        if payload.get("aps_id") and not payload.get("match_id"):
            payload["match_id"] = payload["aps_id"]
        return json_safe(payload)

    def _statpal_primary_daily_enabled(self):
        return self._runner_env_bool("ALGO_DAILY_USE_STATPAL_FIXTURES", True)

    def _statpal_track_all_leagues(self):
        return self._runner_env_bool("STATPAL_TRACK_ALL_LEAGUES", False)

    def _statpal_tracked_league_ids(self):
        league_ids = set(daily_tracked_league_ids("statpal"))
        raw = str(os.environ.get("STATPAL_TRACKED_LEAGUES") or "").strip()
        if not raw:
            return league_ids
        for item in raw.replace("\n", ",").split(","):
            value = item.strip()
            if not value:
                continue
            if "|" in value:
                value = value.split("|", 1)[0].strip()
            if ":" in value:
                value = value.split(":", 1)[0].strip()
            league_ids.add(value)
        return league_ids

    def _statpal_fixture_league_id(self, fixture):
        payload = fixture.api_payload or {}
        raw_league = payload.get("league") if isinstance(payload.get("league"), dict) else {}
        return str(
            payload.get("statpal_provider_competition_id")
            or payload.get("provider_competition_id")
            or payload.get("code")
            or raw_league.get("id")
            or ""
        ).strip()

    def _api_football_tracked_league_ids(self):
        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        return {str(league_id) for league_id in algo_runner.tracked_leagues()}

    def _api_football_fixture_league_id(self, fixture):
        payload = fixture.api_payload or {}
        raw_league = payload.get("league") if isinstance(payload.get("league"), dict) else {}
        return str(
            payload.get("api_football_league_id")
            or payload.get("provider_competition_id")
            or payload.get("league_id")
            or payload.get("code")
            or raw_league.get("id")
            or ""
        ).strip()

    def _api_enrichment_rows(self, target_date):
        rows = list(
            FixtureCache.objects.filter(match_date=target_date)
            .exclude(source="statpal")
            .only(
                "match_id",
                "match_date",
                "fixture",
                "home_team",
                "away_team",
                "home_team_normalized",
                "away_team_normalized",
                "fixture_normalized",
                "home_logo",
                "away_logo",
                "league",
                "league_logo",
                "country",
                "country_flag",
                "round",
                "league_type",
                "kickoff",
                "kickoff_utc",
                "api_payload",
                "source",
            )
        )
        tracked = self._api_football_tracked_league_ids()
        if tracked:
            rows = [row for row in rows if self._api_football_fixture_league_id(row) in tracked]
        return rows

    def _api_enrichment_match(self, fixture, api_rows):
        home_query = normalize_fixture_text(fixture.get("hname") or fixture.get("home_team") or "")
        away_query = normalize_fixture_text(fixture.get("aname") or fixture.get("away_team") or "")
        normalized_query = normalize_fixture_text(fixture.get("fixture") or "")
        best = None
        for row in api_rows:
            if home_query and away_query:
                direct = (
                    token_side_score(home_query, row.home_team_normalized or row.home_team)
                    + token_side_score(away_query, row.away_team_normalized or row.away_team)
                ) / 2
                reversed_match = (
                    token_side_score(home_query, row.away_team_normalized or row.away_team)
                    + token_side_score(away_query, row.home_team_normalized or row.home_team)
                ) / 2
                orientation = "reversed" if reversed_match > direct else "direct"
                score = max(direct, reversed_match) * 100
            else:
                orientation = "unknown"
                score = SequenceMatcher(None, normalized_query, row.fixture_normalized or normalize_fixture_text(row.fixture)).ratio() * 100
            if score >= 82 and (best is None or score > best[0]):
                best = (round(score, 2), orientation, row)
        return best

    def _merge_api_football_enrichment(self, fixture, api_row, *, score=0, orientation="unknown"):
        item = dict(fixture or {})
        if not api_row:
            item.setdefault("provider_merge", {})["api_football"] = {"matched": False}
            return item
        payload = api_row.api_payload or {}
        api_home_id = payload.get("provider_home_team_id") or payload.get("hid") or payload.get("home_team_id")
        api_away_id = payload.get("provider_away_team_id") or payload.get("aid") or payload.get("away_team_id")
        api_league_id = payload.get("provider_competition_id") or payload.get("code") or payload.get("league_id")
        api_provider_match_id = payload.get("provider_match_id") or payload.get("main_id") or api_row.match_id

        def fill(key, value):
            if item.get(key) in (None, "") and value not in (None, ""):
                item[key] = value

        fill("home_logo", api_row.home_logo)
        fill("away_logo", api_row.away_logo)
        fill("league_logo", api_row.league_logo)
        fill("country_flag", api_row.country_flag)
        fill("round", api_row.round)
        fill("league_type", api_row.league_type)
        fill("kickoff_utc", api_row.kickoff_utc.isoformat() if api_row.kickoff_utc else "")
        fill("country", api_row.country)
        item["api_football_fixture_id"] = str(api_row.match_id or "")
        item["api_football_provider_match_id"] = str(api_provider_match_id or "")
        item["api_football_league_id"] = str(api_league_id or "")
        item["api_football_home_team_id"] = api_home_id
        item["api_football_away_team_id"] = api_away_id
        item["api_football_source"] = api_row.source
        item["aps_id"] = str(api_row.match_id or "")
        provider_merge = dict(item.get("provider_merge") or {})
        provider_merge["api_football"] = {
            "matched": True,
            "match_id": str(api_row.match_id or ""),
            "league_id": str(api_league_id or ""),
            "home_team_id": str(api_home_id or ""),
            "away_team_id": str(api_away_id or ""),
            "score": score,
            "orientation": orientation,
            "used_for": ["metadata", "odds", "team_form", "prediction_context"],
        }
        provider_merge["primary"] = "statpal"
        item["provider_merge"] = provider_merge
        return item

    def _merge_statpal_and_api_football_fixtures(self, fixtures, target_date):
        api_rows = self._api_enrichment_rows(target_date)
        enriched = []
        matched_api_match_ids = set()
        matched = 0
        for fixture in fixtures or []:
            match = self._api_enrichment_match(fixture, api_rows)
            if match:
                score, orientation, row = match
                enriched.append(self._merge_api_football_enrichment(fixture, row, score=score, orientation=orientation))
                matched_api_match_ids.add(str(row.match_id or ""))
                matched += 1
            else:
                enriched.append(self._merge_api_football_enrichment(fixture, None))
        unmatched_api_count = sum(
            1 for row in api_rows if str(row.match_id or "") not in matched_api_match_ids
        )
        for row in api_rows:
            if str(row.match_id or "") in matched_api_match_ids:
                continue
            payload = self._cached_fixture_runner_payload(row)
            provider_merge = dict(payload.get("provider_merge") or {})
            provider_merge.setdefault("primary", payload.get("source") or row.source or "api_football")
            provider_merge.setdefault("api_football", {
                "matched": False,
                "match_id": str(row.match_id or ""),
                "used_for": ["fixture_source", "odds", "team_form", "prediction_context"],
            })
            payload["provider_merge"] = provider_merge
            enriched.append(payload)
        log.info(
            "Daily StatPal/API-Football provider merge date=%s statpal_fixtures=%s api_candidates=%s matched=%s api_added=%s",
            target_date,
            len(fixtures or []),
            len(api_rows),
            matched,
            unmatched_api_count,
        )
        return enriched

    def _enrich_fixture_for_cross_provider_scoring(self, fixture, target_date):
        item = dict(fixture or {})
        if not item:
            return item

        search_service = FixtureSearchService()
        is_statpal_fixture = str(item.get("source") or "").startswith("statpal") or str(item.get("match_id") or "").startswith("statpal:")
        if is_statpal_fixture:
            api_rows = self._api_enrichment_rows(target_date)
            if not api_rows:
                self._sync_api_football_enrichment_cache(target_date)
                api_rows = self._api_enrichment_rows(target_date)
            match = self._api_enrichment_match(item, api_rows)
            if match:
                score, orientation, row = match
                item = self._merge_api_football_enrichment(item, row, score=score, orientation=orientation)
            else:
                item = self._merge_api_football_enrichment(item, None)
            return item

        candidate = {
            **item,
            "match_date": target_date,
            "home_team": item.get("home_team") or item.get("hname") or "",
            "away_team": item.get("away_team") or item.get("aname") or "",
        }
        statpal = search_service.find_statpal_fixture_context(candidate)
        if not statpal:
            attached = search_service._attach_statpal_fixture_context([item], target_date)
            item = dict((attached or [item])[0] or item)
            if not item.get("statpal_provider_match_id"):
                return item
            statpal = item

        item["statpal_match_id"] = statpal.get("statpal_match_id") or statpal.get("match_id") or item.get("statpal_match_id") or ""
        item["statpal_provider_match_id"] = (
            statpal.get("statpal_provider_match_id")
            or statpal.get("provider_match_id")
            or str(statpal.get("match_id") or "").replace("statpal:", "", 1)
            or item.get("statpal_provider_match_id")
            or ""
        )
        item["statpal_provider_competition_id"] = (
            statpal.get("statpal_provider_competition_id")
            or statpal.get("provider_competition_id")
            or statpal.get("code")
            or item.get("statpal_provider_competition_id")
            or ""
        )
        item["statpal_home_team_id"] = str(
            statpal.get("statpal_home_team_id")
            or statpal.get("home_team_id")
            or statpal.get("hid")
            or item.get("statpal_home_team_id")
            or ""
        )
        item["statpal_away_team_id"] = str(
            statpal.get("statpal_away_team_id")
            or statpal.get("away_team_id")
            or statpal.get("aid")
            or item.get("statpal_away_team_id")
            or ""
        )
        provider_merge = dict(item.get("provider_merge") or {})
        provider_merge["primary"] = item.get("source") or "api_football"
        provider_merge["statpal"] = {
            "matched": True,
            "match_id": str(item.get("statpal_match_id") or ""),
            "provider_match_id": str(item.get("statpal_provider_match_id") or ""),
            "league_id": str(item.get("statpal_provider_competition_id") or ""),
            "home_team_id": str(item.get("statpal_home_team_id") or ""),
            "away_team_id": str(item.get("statpal_away_team_id") or ""),
            "score": statpal.get("match_score"),
            "orientation": statpal.get("match_orientation") or "direct",
            "used_for": ["snapshots", "market_context", "team_news", "odds_context"],
        }
        item["provider_merge"] = provider_merge
        return item

    def _sync_api_football_enrichment_cache(self, target_date):
        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        try:
            with temporary_env(self._runner_env({"APS_TRACK_ALL_LEAGUES": "False", "APS_MAX_FIXTURES": "0"})):
                fixtures = algo_runner.fetch_aps_fixtures(target_date.isoformat())
            synced = FixtureSearchService()._upsert_fixtures(fixtures, target_date)
            return {"synced": synced, "errors": []}
        except Exception as exc:
            return {"synced": 0, "errors": [{"provider": "api_football", "error": str(exc)}]}

    def _statpal_cached_runner_fixtures(self, target_date):
        queryset = FixtureCache.objects.filter(match_date=target_date, source="statpal")
        tracked = self._statpal_tracked_league_ids()
        rows = list(queryset.order_by("country", "league", "kickoff", "fixture"))
        if tracked and not self._statpal_track_all_leagues():
            rows = [row for row in rows if self._statpal_fixture_league_id(row) in tracked]
        return [self._cached_fixture_runner_payload(row) for row in rows]

    def _daily_runner_fixtures(self, target_date):
        statpal_errors = []
        if self._statpal_primary_daily_enabled():
            fixtures = self._statpal_cached_runner_fixtures(target_date)
            if not fixtures:
                try:
                    sync_result = FixtureSearchService().sync_statpal_universe(
                        start_date=target_date,
                        days=0,
                    )
                    statpal_errors = sync_result.get("errors") or []
                    fixtures = self._statpal_cached_runner_fixtures(target_date)
                except Exception as exc:
                    statpal_errors = [{"provider": "statpal", "error": str(exc)}]
            if fixtures:
                api_sync = self._sync_api_football_enrichment_cache(target_date)
                statpal_errors.extend(api_sync.get("errors") or [])
                fixtures = self._merge_statpal_and_api_football_fixtures(fixtures, target_date)
                return {
                    "fixtures": fixtures,
                    "source": "statpal_api_football",
                    "fallback_used": False,
                    "errors": statpal_errors,
                }

        return {
            "fixtures": self._api_football_fallback_runner_fixtures(target_date),
            "source": "api_football",
            "fallback_used": self._statpal_primary_daily_enabled(),
            "errors": statpal_errors,
        }

    def _api_football_fallback_runner_fixtures(self, target_date):
        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        with temporary_env(self._runner_env({"APS_TRACK_ALL_LEAGUES": "False", "APS_MAX_FIXTURES": "0"})):
            fixtures = algo_runner.fetch_aps_fixtures(target_date.isoformat())
        return self._attach_statpal_fixture_context(fixtures, target_date)

    def _hydrate_statpal_scoring_context(self, fixture):
        provider_match_id = str(fixture.get("statpal_provider_match_id") or "").strip()
        provider_competition_id = str(fixture.get("statpal_provider_competition_id") or fixture.get("code") or "").strip()
        match_id = str(fixture.get("match_id") or fixture.get("aps_id") or "").strip()
        if not provider_match_id and not match_id:
            return fixture

        from betpreneur.modules.catalog.api import StatPalFixtureSnapshot, statpal_snapshot_service

        try:
            refresh = statpal_snapshot_service.refresh_fixture_snapshots(
                match_id=match_id,
                provider_match_id=provider_match_id,
                provider_competition_id=provider_competition_id,
            )
            home_team_id = str(fixture.get("statpal_home_team_id") or "").strip()
            away_team_id = str(fixture.get("statpal_away_team_id") or "").strip()
            if home_team_id or away_team_id:
                team_refresh = statpal_snapshot_service.refresh_fixture_team_stats(
                    match_id=match_id,
                    provider_match_id=provider_match_id,
                    provider_competition_id=provider_competition_id,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                )
                refresh = {**refresh, "team_stats_refresh": team_refresh}
            context = statpal_snapshot_service.fixture_context(
                match_id=match_id,
                provider_match_id=provider_match_id,
            )
            if provider_competition_id:
                league_rows = (
                    StatPalFixtureSnapshot.objects.filter(
                        status="available",
                        provider_competition_id=provider_competition_id,
                        snapshot_type__in=[
                            StatPalFixtureSnapshot.SnapshotType.LEAGUE_STANDINGS,
                            StatPalFixtureSnapshot.SnapshotType.LEAGUE_STATS,
                        ],
                    )
                    .order_by("snapshot_type", "-fetched_at", "-updated_at")
                )
                snapshots = dict(context.get("snapshots") or {})
                for row in league_rows:
                    if row.snapshot_type in snapshots:
                        continue
                    snapshots[row.snapshot_type] = statpal_snapshot_service._snapshot_context(row)
                context = {**context, "available": bool(snapshots), "snapshots": snapshots}
        except Exception as exc:
            refresh = {"errors": [{"provider": "statpal", "error": str(exc)}]}
            context = {"available": False, "snapshots": {}}

        enriched = dict(fixture)
        enriched["statpal_refresh"] = refresh
        enriched["statpal_context"] = context
        enriched["team_news"] = self._team_news_for_prediction_fixture(enriched, context)
        return enriched

    def _team_news_for_prediction_fixture(self, fixture, statpal_context):
        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        team_news = algo_runner._statpal_team_news(statpal_context)
        if team_news.get("available"):
            return team_news

        aps_id = self._text(fixture.get("aps_id") or fixture.get("api_football_fixture_id"))
        if not aps_id:
            return team_news
        try:
            fallback = algo_runner.fetch_fixture_team_news(
                aps_id,
                fixture.get("api_football_home_team_id") or fixture.get("hid"),
                fixture.get("api_football_away_team_id") or fixture.get("aid"),
            )
        except Exception as exc:
            log.warning(
                "Daily prediction team news fetch failed match_id=%s aps_id=%s error=%s",
                fixture.get("match_id"),
                aps_id,
                exc,
            )
            return team_news
        if fallback.get("available"):
            fallback.setdefault("flags", []).append("api_football_team_news_fallback")
            fallback["flags"] = sorted(dict.fromkeys(fallback.get("flags") or []))
            return fallback
        return team_news

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
            fixture_context = {
                **(fixture.get("fixture_context") or {}),
                "country": fixture.get("country", ""),
                "league": fixture.get("league", ""),
            }
            for market in fixture.get("markets") or []:
                selected_pick = selected_lookup.get((match_id, market.get("market", "")))
                published = bool(selected_pick)
                rows.append(
                    MarketPrediction(
                        run=algo_run,
                        selected_pick=selected_pick,
                        match_date=algo_run.target_date,
                        fixture=self._text(fixture.get("fixture", "")),
                        home_team=self._text(fixture.get("home_team", "")),
                        away_team=self._text(fixture.get("away_team", "")),
                        league=self._text(fixture.get("league", "")),
                        kickoff=self._text(fixture.get("kickoff", "")),
                        match_id=match_id,
                        market=market.get("market", ""),
                        meaning=market.get("meaning", ""),
                        raw_confidence=market.get("raw_confidence") or market.get("confidence") or 0,
                        confidence=market.get("confidence") or 0,
                        odds=market.get("odds") or 0,
                        ev=market.get("ev"),
                        odds_source=market.get("odds_source", ""),
                        odds_meta=json_safe(market.get("odds_meta") or {}),
                        eligible=bool(market.get("eligible")),
                        published=published,
                        rejection_reason=self._prediction_rejection_reason(market, published),
                        risk_flags=json_safe(market.get("risk_flags") or []),
                        insights=json_safe(market.get("insights") or {}),
                        home_recent_form=json_safe(fixture.get("home_recent_form") or {}),
                        away_recent_form=json_safe(fixture.get("away_recent_form") or {}),
                        fixture_context=json_safe(fixture_context),
                        team_news=json_safe(fixture.get("team_news") or {}),
                    )
                )
        MarketPrediction.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        result["internal_prediction_count"] = len(rows)
        return len(rows)

    def _persist_fixtures(self, algo_run: AlgoRun, result):
        AlgoFixture.objects.filter(run=algo_run).delete()
        fixture_summaries = result.get("fixture_summaries") or []
        if not fixture_summaries:
            return 0

        rows = []
        for fixture in fixture_summaries:
            rows.append(
                AlgoFixture(
                    run=algo_run,
                    match_date=algo_run.target_date,
                    fixture=self._text(fixture.get("fixture", "")),
                    home_team=self._text(fixture.get("home_team", "")),
                    away_team=self._text(fixture.get("away_team", "")),
                    home_logo=self._text(fixture.get("home_logo", "")),
                    away_logo=self._text(fixture.get("away_logo", "")),
                    league=self._text(fixture.get("league", "")),
                    league_logo=self._text(fixture.get("league_logo", "")),
                    country=self._text(fixture.get("country", "")),
                    country_flag=self._text(fixture.get("country_flag", "")),
                    round=self._text(fixture.get("round", "")),
                    league_type=self._text(fixture.get("league_type", "")),
                    kickoff=self._text(fixture.get("kickoff", "")),
                    match_id=str(fixture.get("match_id") or ""),
                    market_count=fixture.get("market_count", 0),
                    markets_70_plus=fixture.get("markets_70_plus", 0),
                    markets_65_plus=fixture.get("markets_65_plus", 0),
                    home_recent_form=json_safe(fixture.get("home_recent_form") or {}),
                    away_recent_form=json_safe(fixture.get("away_recent_form") or {}),
                    fixture_context=json_safe(fixture.get("fixture_context") or {}),
                    team_news=json_safe(fixture.get("team_news") or {}),
                    corner_profile=json_safe(fixture.get("corner_profile") or {}),
                    insights=json_safe(fixture.get("insights") or {}),
                    source_payload=json_safe(fixture.get("source_payload") or {}),
                    status=AlgoFixture.Status.SCORED,
                )
            )
        AlgoFixture.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        result["fixture_count"] = len(rows)
        return len(rows)

    def _fixture_defaults(self, algo_run, fixture):
        return {
            "match_date": algo_run.target_date,
            "fixture": self._text(fixture.get("fixture", "")),
            "home_team": self._text(fixture.get("home_team", "")),
            "away_team": self._text(fixture.get("away_team", "")),
            "home_logo": self._text(fixture.get("home_logo", "")),
            "away_logo": self._text(fixture.get("away_logo", "")),
            "league": self._text(fixture.get("league", "")),
            "league_logo": self._text(fixture.get("league_logo", "")),
            "country": self._text(fixture.get("country", "")),
            "country_flag": self._text(fixture.get("country_flag", "")),
            "round": self._text(fixture.get("round", "")),
            "league_type": self._text(fixture.get("league_type", "")),
            "kickoff": self._text(fixture.get("kickoff", "")),
            "market_count": fixture.get("market_count", 0),
            "markets_70_plus": fixture.get("markets_70_plus", 0),
            "markets_65_plus": fixture.get("markets_65_plus", 0),
            "home_recent_form": json_safe(fixture.get("home_recent_form") or {}),
            "away_recent_form": json_safe(fixture.get("away_recent_form") or {}),
            "fixture_context": json_safe(fixture.get("fixture_context") or {}),
            "team_news": json_safe(fixture.get("team_news") or {}),
            "corner_profile": json_safe(fixture.get("corner_profile") or {}),
            "insights": json_safe(fixture.get("insights") or {}),
            "source_payload": json_safe(fixture.get("source_payload") or {}),
            "status": AlgoFixture.Status.SCORED,
            "error": "",
        }

    def _persist_fixture_summary(self, algo_run: AlgoRun, fixture):
        match_id = str(fixture.get("match_id") or "")
        obj, _created = AlgoFixture.objects.update_or_create(
            run=algo_run,
            match_id=match_id,
            defaults=self._fixture_defaults(algo_run, fixture),
        )
        return obj

    def _persist_fixture_market_predictions(self, algo_run: AlgoRun, fixture):
        match_id = str(fixture.get("match_id") or "")
        MarketPrediction.objects.filter(run=algo_run, match_id=match_id).delete()
        rows = []
        fixture_context = {
            **(fixture.get("fixture_context") or {}),
            "country": fixture.get("country", ""),
            "league": fixture.get("league", ""),
        }
        for market in fixture.get("markets") or []:
            rows.append(
                MarketPrediction(
                    run=algo_run,
                    match_date=algo_run.target_date,
                    fixture=self._text(fixture.get("fixture", "")),
                    home_team=self._text(fixture.get("home_team", "")),
                    away_team=self._text(fixture.get("away_team", "")),
                    league=self._text(fixture.get("league", "")),
                    kickoff=self._text(fixture.get("kickoff", "")),
                    match_id=match_id,
                    market=market.get("market", ""),
                    meaning=market.get("meaning", ""),
                    raw_confidence=market.get("raw_confidence") or market.get("confidence") or 0,
                    confidence=market.get("confidence") or 0,
                    odds=market.get("odds") or 0,
                    ev=market.get("ev"),
                    odds_source=market.get("odds_source", ""),
                    odds_meta=json_safe(market.get("odds_meta") or {}),
                    eligible=bool(market.get("eligible")),
                    published=False,
                    rejection_reason=self._prediction_rejection_reason(market, False),
                    risk_flags=json_safe(market.get("risk_flags") or []),
                    insights=json_safe(market.get("insights") or {}),
                    home_recent_form=json_safe(fixture.get("home_recent_form") or {}),
                    away_recent_form=json_safe(fixture.get("away_recent_form") or {}),
                    fixture_context=json_safe(fixture_context),
                    team_news=json_safe(fixture.get("team_news") or {}),
                )
            )
        if rows:
            MarketPrediction.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        return len(rows)

    def _write_slip_review_market_cache(self, algo_run: AlgoRun, fixture, *, source="merged"):
        if not getattr(settings, "SLIP_REVIEW_MARKET_CACHE_WRITE_ENABLED", True):
            return {"enabled": False, "cached": 0}
        try:
            from betpreneur.modules.catalog.api import SlipReviewMarketCacheWriter

            payload = dict(fixture or {})
            payload.setdefault("match_date", algo_run.target_date)
            summary = SlipReviewMarketCacheWriter().upsert_fixture_markets(payload, source=source)
            summary["enabled"] = True
            return summary
        except Exception as exc:
            log.warning(
                "Slip review market cache write failed run=%s match_id=%s error=%s",
                getattr(algo_run, "id", None),
                (fixture or {}).get("match_id"),
                exc,
            )
            return {
                "enabled": True,
                "cached": 0,
                "error": str(exc)[:500],
            }

    def _prediction_fixture_payload(self, fixture):
        payload = dict(fixture or {})
        home = self._text(payload.get("home_team") or payload.get("hname"))
        away = self._text(payload.get("away_team") or payload.get("aname"))
        fixture_name = self._text(payload.get("fixture") or f"{home} vs {away}".strip())
        match_id = self._text(payload.get("match_id") or payload.get("aps_id") or payload.get("id"))
        payload.update(
            {
                "fixture": fixture_name,
                "home_team": home,
                "away_team": away,
                "hname": self._text(payload.get("hname") or home),
                "aname": self._text(payload.get("aname") or away),
                "match_id": match_id,
            }
        )
        return payload

    def _remember_prediction_odd(self, odds, key, value, *, source="statpal"):
        try:
            odd = float(value)
        except (TypeError, ValueError):
            return
        if odd <= 1:
            return
        odds.setdefault("_samples", {}).setdefault(key, []).append(odd)
        current = odds.get(key)
        if current is None or odd > float(current or 0):
            odds[key] = odd

    def _finalize_prediction_odds_meta(self, odds, *, source):
        samples = odds.pop("_samples", {})
        meta = {}
        for key, values in samples.items():
            values = [float(value) for value in values if value]
            if not values:
                continue
            average = sum(values) / len(values)
            best = max(values)
            worst = min(values)
            meta[key] = {
                "bookmaker_count": len(values),
                "best": round(best, 3),
                "worst": round(worst, 3),
                "average": round(average, 3),
                "spread_pct": round(((best - worst) / average) * 100, 1) if average else 0.0,
                "best_vs_average_pct": round(((best - average) / average) * 100, 1) if average else 0.0,
                "source": source,
            }
        if meta:
            odds["_meta"] = meta
        return odds

    def _statpal_prematch_odds(self, fixture):
        statpal_context = fixture.get("statpal_context") or {}
        if not statpal_context:
            statpal_context = ((fixture.get("fixture_context") or {}).get("statpal") or {})
        snapshot = ((statpal_context.get("snapshots") or {}).get("prematch_odds") or {})
        if snapshot:
            payload = snapshot.get("payload") or {}
            summary = snapshot.get("summary") or {}
            return {**payload, **summary}
        prematch = statpal_context.get("prematch_odds") or {}
        return prematch if isinstance(prematch, dict) else {}

    def _remember_statpal_summary_odds(self, odds, prematch):
        odds_map = prematch.get("odds_map") if isinstance(prematch, dict) else {}
        if isinstance(odds_map, dict):
            for odds_key, value in odds_map.items():
                self._remember_prediction_odd(odds, str(odds_key), value, source="statpal")

        mapping = {
            "home_odds": "hw",
            "draw_odds": "d",
            "away_odds": "aw",
            "over15_odds": "o15",
            "under15_odds": "u15",
            "over25_odds": "o25",
            "under25_odds": "u25",
            "over35_odds": "o35",
            "under35_odds": "u35",
            "over45_odds": "o45",
            "under45_odds": "u45",
            "btts_yes_odds": "btts_yes",
            "btts_no_odds": "btts_no",
            "double_chance_1x_odds": "1x",
            "double_chance_12_odds": "12",
            "double_chance_x2_odds": "x2",
        }
        for source_key, odds_key in mapping.items():
            self._remember_prediction_odd(odds, odds_key, prematch.get(source_key), source="statpal")

        alias_mapping = {
            "home": "hw",
            "draw": "d",
            "away": "aw",
            "over15": "o15",
            "under15": "u15",
            "over25": "o25",
            "under25": "u25",
            "over35": "o35",
            "under35": "u35",
            "over45": "o45",
            "under45": "u45",
            "1x": "1x",
            "12": "12",
            "x2": "x2",
        }
        for source_key, odds_key in alias_mapping.items():
            self._remember_prediction_odd(odds, odds_key, prematch.get(source_key), source="statpal")

    def _remember_statpal_market_odds(self, odds, payload):
        if not isinstance(payload, dict):
            return

        def clean(value):
            return normalize_fixture_text(value or "")

        def as_list(value):
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
            if isinstance(value, dict):
                return [value]
            return []

        def odds_items(container):
            return as_list(container.get("odds") or container.get("odd"))

        def total_items(bookmaker):
            return as_list(bookmaker.get("totals") or bookmaker.get("total"))

        def remember_items(mapping, bookmaker):
            for odd in odds_items(bookmaker):
                if not isinstance(odd, dict):
                    continue
                odds_key = mapping.get(clean(odd.get("name")))
                if odds_key:
                    self._remember_prediction_odd(odds, odds_key, odd.get("value"), source="statpal")

        def market_label_for_total(market_name):
            if "shot" in market_name and "target" in market_name:
                if "home" in market_name:
                    return "Home Team Shots On Target"
                if "away" in market_name:
                    return "Away Team Shots On Target"
                return "Shots On Target"
            if "booking point" in market_name:
                return "Booking Points"
            if "card" in market_name or "booking" in market_name or "yellow" in market_name:
                if "home" in market_name:
                    return "Home Team Cards"
                if "away" in market_name:
                    return "Away Team Cards"
                return "Cards"
            if "corner" in market_name:
                if "home" in market_name:
                    return "Home Team Corners"
                if "away" in market_name:
                    return "Away Team Corners"
                return "Corners"
            if market_name in {"total - home", "home total", "home team total"}:
                return "Home Team"
            if market_name in {"total - away", "away total", "away team total"}:
                return "Away Team"
            if "home" in market_name and "goal" in market_name:
                return "Home Team"
            if "away" in market_name and "goal" in market_name:
                return "Away Team"
            return ""

        def remember_total(prefix, total, *, market_label=""):
            if not isinstance(total, dict):
                return
            line = total.get("line") if total.get("line") is not None else total.get("name")
            try:
                line_value = float(line)
                line_text = f"{line_value:g}".replace(".", "")
                display_line = f"{line_value:g}"
            except (TypeError, ValueError):
                return
            for odd in odds_items(total):
                if not isinstance(odd, dict):
                    continue
                odd_name = clean(odd.get("name"))
                if odd_name == "over":
                    if prefix or not market_label:
                        self._remember_prediction_odd(
                            odds, f"{prefix}o{line_text}", odd.get("value"), source="statpal"
                        )
                    if market_label:
                        self._remember_prediction_odd(
                            odds,
                            f"{market_label} Over {display_line}",
                            odd.get("value"),
                            source="statpal",
                        )
                elif odd_name == "under":
                    if prefix or not market_label:
                        self._remember_prediction_odd(
                            odds, f"{prefix}u{line_text}", odd.get("value"), source="statpal"
                        )
                    if market_label:
                        self._remember_prediction_odd(
                            odds,
                            f"{market_label} Under {display_line}",
                            odd.get("value"),
                            source="statpal",
                        )

        for market in as_list(payload.get("markets")):
            if not isinstance(market, dict):
                continue
            market_name = clean(market.get("name"))
            for bookmaker in as_list(market.get("bookmakers") or market.get("bookmaker")):
                if not isinstance(bookmaker, dict):
                    continue
                if market_name in {"1x2", "1 x 2", "match winner", "fulltime result"}:
                    remember_items({"home": "hw", "draw": "d", "away": "aw"}, bookmaker)
                elif market_name in {"both teams to score", "both teams score", "btts"}:
                    remember_items({"yes": "btts_yes", "no": "btts_no"}, bookmaker)
                elif market_name == "double chance":
                    remember_items(
                        {
                            "home/draw": "1x",
                            "home draw": "1x",
                            "1x": "1x",
                            "home/away": "12",
                            "home away": "12",
                            "12": "12",
                            "draw/away": "x2",
                            "draw away": "x2",
                            "x2": "x2",
                        },
                        bookmaker,
                    )
                elif market_name in {"over/under", "over under", "totals"}:
                    for total in total_items(bookmaker):
                        remember_total("", total)
                else:
                    market_label = market_label_for_total(market_name)
                    if market_label:
                        for total in total_items(bookmaker):
                            remember_total("", total, market_label=market_label)

    def _statpal_prediction_odds(self, fixture):
        prematch = self._statpal_prematch_odds(fixture)
        odds = {}
        self._remember_statpal_summary_odds(odds, prematch)
        self._remember_statpal_market_odds(odds, prematch)
        return self._finalize_prediction_odds_meta(odds, source="statpal")

    def _daily_prediction_real_odds(self, fixture):
        real_odds = {}
        aps_id = self._text(fixture.get("aps_id") or fixture.get("api_football_fixture_id"))
        if aps_id:
            try:
                from betpreneur.modules.catalog.api import legacy_runner as algo_runner

                real_odds = dict(algo_runner.get_api_football_odds(aps_id) or {})
            except Exception as exc:
                log.warning(
                    "Daily prediction odds fetch failed match_id=%s aps_id=%s error=%s",
                    fixture.get("match_id"),
                    aps_id,
                    exc,
                )
        statpal_odds = self._statpal_prediction_odds(fixture)
        if statpal_odds:
            meta = {
                **(real_odds.get("_meta") or {}),
                **(statpal_odds.get("_meta") or {}),
            }
            real_odds = {
                **{key: value for key, value in real_odds.items() if key != "_meta"},
                **{key: value for key, value in statpal_odds.items() if key != "_meta"},
            }
            if meta:
                real_odds["_meta"] = meta
        return real_odds

    def _daily_prediction_markets(self, real_odds):
        markets = list(daily_discovery_market_names())
        for key in sorted(real_odds or {}):
            if str(key).startswith("_"):
                continue
            entry = daily_catalog_entry(str(key))
            if entry and entry.enabled and entry.publish_enabled and entry.market not in markets:
                markets.append(entry.market)
        return tuple(markets)

    def _prediction_market_real_odd(self, real_odds, market):
        odds_key = daily_odds_key_map().get(market)
        if odds_key and real_odds.get(odds_key):
            return real_odds.get(odds_key), odds_key
        if real_odds.get(market):
            return real_odds.get(market), market
        return None, odds_key or market

    def _prediction_estimated_odds(self, probability):
        if probability.fair_odds:
            return round(float(probability.fair_odds) * 1.05, 2)
        if probability.effective_probability:
            return round(1 / max(float(probability.effective_probability), 0.05) * 1.05, 2)
        return 0

    def _prediction_sample_size(self, probability):
        metadata = probability.diagnostics.metadata or {}
        for key in ("calibration_sample_count", "sample_count", "league_sample_count"):
            value = metadata.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _prediction_market_fit_score(self, probability):
        support = str((probability.diagnostics.metadata or {}).get("market_support_level") or "").lower()
        return {
            "full": 82,
            "strong": 82,
            "medium": 68,
            "partial": 58,
            "weak": 46,
            "unsupported": 20,
        }.get(support)

    def _prediction_odds_quality_flags(self, odds_meta):
        odds_meta = odds_meta or {}
        flags = []
        try:
            spread_pct = float(odds_meta.get("spread_pct") or 0)
        except (TypeError, ValueError):
            spread_pct = 0.0
        try:
            best_vs_average_pct = float(odds_meta.get("best_vs_average_pct") or 0)
        except (TypeError, ValueError):
            best_vs_average_pct = 0.0
        if spread_pct >= 80:
            flags.append("wide_odds_market")
        if best_vs_average_pct >= 35:
            flags.append("best_price_far_above_consensus")
        return flags

    def _prediction_policy_flags(self, probability, value, recommendation, top_picks, *, odds_meta=None):
        flags = [
            *list(probability.warnings or ()),
            *list(value.pricing_warnings or ()),
            *list(recommendation.warnings or ()),
            *list(top_picks.reasons or ()),
            *list(top_picks.warnings or ()),
            *self._prediction_odds_quality_flags(odds_meta),
        ]
        return list(dict.fromkeys(str(flag) for flag in flags if flag))

    def _prediction_all_games_eligible(self, probability, value, all_games):
        if not self._prediction_analysis_available(probability, all_games):
            return False
        pricing_warnings = {str(item) for item in (value.pricing_warnings or ())}
        if not value.available_odds or "available_odds_missing" in pricing_warnings:
            return False
        return True

    def _prediction_analysis_available(self, probability, all_games):
        warnings = {str(item) for item in (probability.warnings or ())}
        if "fixture_not_found" in warnings or probability.effective_probability is None:
            return False
        both_team_profiles_missing = {"home_team_profile", "away_team_profile"}.issubset(warnings)
        model = str(probability.model or "")
        if model == "elo_result" and both_team_profiles_missing:
            return False
        if model == "poisson_goals":
            if {
                "goal_model_unavailable",
                "using_feature_derived_expected_goals",
            }.issubset(warnings):
                return False
            return bool((all_games.data_confidence or 0) > 0)
        if "count" in model:
            if "count_model_unavailable" in warnings:
                return False
            if probability.data_quality in {"missing", "poor", "unavailable"}:
                return False
            return bool((all_games.data_confidence or 0) > 0)
        if both_team_profiles_missing:
            return False
        if probability.data_quality in {"missing", "poor", "unavailable"}:
            return False
        return bool((all_games.data_confidence or 0) > 0)

    def _prediction_data_status(self, probability, value, top_picks, analysis_available):
        if not analysis_available:
            return "insufficient_data"
        if top_picks.publishable:
            return "top_pick_ready"
        if not value.available_odds:
            return "modelled_no_real_odds"
        if top_picks.reasons or top_picks.warnings:
            return "modelled_watchlist"
        return "modelled"

    def _prediction_market_insights(
        self,
        probability,
        value,
        recommendation,
        all_games,
        top_picks,
        *,
        all_games_eligible=False,
        analysis_available=False,
    ):
        family_payload = daily_market_family_payload(probability.market)
        positive = list(dict.fromkeys([*probability.explanation_facts, *value.explanation_facts]))
        risk = [
            *list(probability.warnings or ()),
            *list(value.pricing_warnings or ()),
            *list(recommendation.warnings or ()),
            *list(top_picks.reasons or ()),
        ]
        confidence = int(round(probability.confidence_score or 0))
        source = ", ".join(probability.model_sources or (probability.model,)) or "prediction"
        if top_picks.publishable:
            conclusion = f"{probability.market} passes the Top Picks exposure policy."
        elif probability.effective_probability is not None:
            conclusion = f"{probability.market} is modelled, but product policy needs stronger price or reliability support."
        else:
            conclusion = f"{probability.market} does not have enough model support yet."
        data_status = self._prediction_data_status(probability, value, top_picks, analysis_available)
        policy_decision = "approve" if top_picks.publishable else "caution" if all_games_eligible else "reject"
        policy_tier = top_picks.tier if all_games_eligible else ""
        return {
            **family_payload,
            "prediction_engine": "prediction.api.predict_fixture",
            "value_engine": "prediction.api.assess_market_value",
            "recommendation_engine": "prediction.api.score_recommendation",
            "product_policy_engine": "pricing.product_policies",
            "raw_probability": probability.raw_probability,
            "calibrated_probability": probability.calibrated_probability,
            "fair_odds": probability.fair_odds,
            "model": probability.model,
            "model_sources": list(probability.model_sources or ()),
            "data_quality": probability.data_quality,
            "all_games_policy": all_games.to_dict(),
            "top_picks_policy": top_picks.to_dict(),
            "value_assessment": value.to_dict(),
            "recommendation_score": recommendation.to_dict(),
            "council_review": {
                "decision": policy_decision,
                "tier": policy_tier,
                "raw_confidence": confidence,
                "final_confidence": confidence,
                "consensus_score": recommendation.recommendation_score,
                "disagreement_score": None,
                "reasons": list(dict.fromkeys([*list(top_picks.reasons or ()), *list(top_picks.warnings or ())])),
                "reviewers": ["prediction_policy"],
            },
            "analysis_available": analysis_available,
            "data_status": data_status,
            "daily_evaluation_route": {
                "family": family_payload.get("market_family"),
                "assessment_type": family_payload.get("assessment_type"),
                "engine": family_payload.get("evaluation_engine"),
                "publishes_probability": family_payload.get("publishes_probability"),
                "required_capabilities": family_payload.get("required_capabilities") or [],
                "optional_capabilities": family_payload.get("optional_capabilities") or [],
            },
            "evidence": {
                "positive": positive,
                "risk": list(dict.fromkeys(str(item) for item in risk if item)),
                "model_sources": list(probability.model_sources or ()),
            },
            "bettor_view": {
                "summary": f"{probability.market} has {confidence}% calibrated model confidence from {source}.",
                "conclusion": conclusion,
                "pricing_warning": value.pricing_warning,
                "tier": top_picks.tier,
            },
            "summary": f"{probability.market} has {confidence}% calibrated model confidence.",
            "conclusion": conclusion,
            "positive_evidence": positive,
            "risk_evidence": list(dict.fromkeys(str(item) for item in risk if item)),
        }

    def _prediction_market_payload(self, probability, real_odds):
        real_odd, odds_key = self._prediction_market_real_odd(real_odds, probability.market)
        odds_meta = ((real_odds.get("_meta") or {}).get(odds_key) if odds_key else {}) or {}
        odds_source = odds_meta.get("source") or ("api_football" if real_odd else "estimated")
        value = assess_market_value(
            probability,
            available_odds=float(real_odd) if real_odd else None,
            odds_source=odds_source if real_odd else "",
            estimated_odds=not bool(real_odd),
            sample_size=self._prediction_sample_size(probability),
            context={
                "data_quality": probability.data_quality,
                "market": probability.market,
            },
        )
        recommendation = score_recommendation(
            probability,
            value,
            market_fit_score=self._prediction_market_fit_score(probability),
            context={
                "market": probability.market,
                "data_quality": probability.data_quality,
                "sample_size": self._prediction_sample_size(probability),
            },
        )
        all_games = assess_all_games_policy(probability)
        top_picks = assess_top_picks_policy(probability, value, recommendation)
        odds = float(real_odd) if real_odd else self._prediction_estimated_odds(probability)
        confidence = int(round(probability.confidence_score or 0))
        analysis_available = self._prediction_analysis_available(probability, all_games)
        all_games_eligible = self._prediction_all_games_eligible(probability, value, all_games)
        insights = self._prediction_market_insights(
            probability,
            value,
            recommendation,
            all_games,
            top_picks,
            all_games_eligible=all_games_eligible,
            analysis_available=analysis_available,
        )
        entry = daily_catalog_entry(probability.market)
        family_payload = daily_market_family_payload(probability.market)
        return {
            "market": probability.market,
            "meaning": entry.meaning if entry else "",
            **family_payload,
            "daily_evaluation_route": insights["daily_evaluation_route"],
            "raw_confidence": int(round((probability.raw_probability or 0) * 100)),
            "confidence": confidence,
            "odds": odds,
            "odds_meta": odds_meta,
            "ev": value.ev,
            "odds_source": odds_source,
            "proven": bool(entry and entry.proven),
            "eligible": all_games_eligible,
            "analysis_available": analysis_available,
            "data_status": insights["data_status"],
            "risk_flags": self._prediction_policy_flags(
                probability,
                value,
                recommendation,
                top_picks,
                odds_meta=odds_meta,
            ),
            "council_review": insights["council_review"],
            "bettor_view": insights["bettor_view"],
            "analysis_summary": insights["summary"],
            "analysis_conclusion": insights["conclusion"],
            "positive_evidence": insights["positive_evidence"],
            "risk_evidence": insights["risk_evidence"],
            "insights": insights,
        }

    def _prediction_recent_form_payload(self, prediction, side):
        features = ((prediction.features.features or {}).get(side) or {}) if prediction.features else {}
        recent = features.get("recent_form") or {}
        form = (recent.get("all") or {}).get("10") or (recent.get("all") or {}).get("5") or {}
        season = features.get("season_profile") or {}
        matches = self._prediction_number(form.get("matches"))
        if matches is None:
            matches = self._prediction_number(season.get("matches_played"))
        if matches is None:
            return {"games": 0, "wins": 0, "draws": 0, "losses": 0, "scope": "overall", "form": ""}
        wins = int(self._prediction_number(form.get("wins")) or 0)
        draws = int(self._prediction_number(form.get("draws")) or 0)
        losses = int(self._prediction_number(form.get("losses")) or max(0, matches - wins - draws))
        avg_scored = self._prediction_number(form.get("goals_for_per_match"))
        if avg_scored is None:
            avg_scored = self._prediction_recent_average(form.get("goals_for"), matches, ceiling=6.0)
        if avg_scored is None:
            avg_scored = self._prediction_rate(season.get("goals_for"), season.get("matches_played"))
        avg_conceded = self._prediction_number(form.get("goals_against_per_match"))
        if avg_conceded is None:
            avg_conceded = self._prediction_recent_average(form.get("goals_against"), matches, ceiling=6.0)
        if avg_conceded is None:
            avg_conceded = self._prediction_rate(season.get("goals_against"), season.get("matches_played"))
        return {
            "games": int(matches),
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "scope": form.get("scope") or "stored_team_intelligence",
            "form": form.get("form") or "",
            "avg_scored": avg_scored or 0,
            "avg_conceded": avg_conceded or 0,
            "source": season.get("source") or "stored_team_intelligence",
            "data_quality": season.get("data_quality") or features.get("coverage", {}).get("status") or "",
        }

    def _prediction_team_intelligence_payload(self, prediction, side):
        features = ((prediction.features.features or {}).get(side) or {}) if prediction.features else {}
        return {
            "strength": features.get("strength") or {},
            "season_profile": features.get("season_profile") or {},
            "recent_form": features.get("recent_form") or {},
            "market_profiles_by_family": features.get("market_profiles_by_family") or {},
            "coverage": features.get("coverage") or {},
        }

    def _prediction_corner_profile_payload(self, prediction):
        counts = prediction.counts
        if not counts:
            return {}
        corners = (counts.expected_team_counts or {}).get("corners") or {}
        sources = ((counts.diagnostics.metadata or {}).get("sources") or {}).get("corners") or []
        features = prediction.features.features if prediction.features else {}
        home = features.get("home") or {}
        away = features.get("away") or {}
        home_season = home.get("season_profile") or {}
        away_season = away.get("season_profile") or {}
        home_matches = home_season.get("matches_played")
        away_matches = away_season.get("matches_played")
        home_avg_for = self._prediction_plausible_rate(home_season.get("corners_for"), home_matches, ceiling=20.0)
        away_avg_for = self._prediction_plausible_rate(away_season.get("corners_for"), away_matches, ceiling=20.0)
        home_avg_against = self._prediction_plausible_rate(
            home_season.get("corners_against"),
            home_matches,
            ceiling=20.0,
        )
        away_avg_against = self._prediction_plausible_rate(
            away_season.get("corners_against"),
            away_matches,
            ceiling=20.0,
        )
        home_expected = self._prediction_number(corners.get("home"))
        away_expected = self._prediction_number(corners.get("away"))
        return {
            "expected_total": counts.expected_total_corners,
            "games": int(min(
                self._prediction_number(home_matches) or 0,
                self._prediction_number(away_matches) or 0,
            )),
            "home": {
                "avg_for": home_avg_for or home_expected,
                "avg_against": home_avg_against,
                "avg_total": round((home_avg_for or 0) + (home_avg_against or 0), 2)
                if home_avg_for is not None and home_avg_against is not None
                else None,
                "expected_for": home_expected,
                "opponent_avg_against": away_avg_against,
            },
            "away": {
                "avg_for": away_avg_for or away_expected,
                "avg_against": away_avg_against,
                "avg_total": round((away_avg_for or 0) + (away_avg_against or 0), 2)
                if away_avg_for is not None and away_avg_against is not None
                else None,
                "expected_for": away_expected,
                "opponent_avg_against": home_avg_against,
            },
            "sources": list(sources),
            "data_quality": counts.diagnostics.data_quality,
            "warnings": list(counts.diagnostics.warnings or ()),
        }

    @staticmethod
    def _prediction_number(value):
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _prediction_rate(self, total, matches):
        total_value = self._prediction_number(total)
        match_count = self._prediction_number(matches)
        if total_value is None or not match_count:
            return 0
        return round(total_value / match_count, 2)

    def _prediction_plausible_rate(self, total, matches, *, ceiling):
        total_value = self._prediction_number(total)
        match_count = self._prediction_number(matches)
        if total_value is None or not match_count:
            return None
        rate = total_value / match_count
        if rate < 0 or rate > ceiling:
            return None
        return round(rate, 2)

    def _prediction_recent_average(self, value, matches, *, ceiling):
        total_value = self._prediction_number(value)
        if total_value is None:
            return None
        match_count = self._prediction_number(matches)
        if match_count and total_value > ceiling:
            return round(total_value / match_count, 2)
        return round(total_value, 2)

    def _score_fixture_with_prediction_engine(self, fixture):
        source_payload = self._prediction_fixture_payload(fixture)
        real_odds = self._daily_prediction_real_odds(source_payload)
        markets = self._daily_prediction_markets(real_odds)
        prediction = predict_fixture(
            source_payload.get("match_id") or source_payload.get("fixture"),
            fixture=source_payload,
            markets=markets,
        )
        market_payloads = [
            self._prediction_market_payload(probability, real_odds)
            for probability in prediction.market_probabilities
            if probability.confidence_score is not None
        ]
        fixture_context = dict(source_payload.get("fixture_context") or {})
        if source_payload.get("statpal_context"):
            fixture_context["statpal"] = source_payload.get("statpal_context") or {}
        fixture_context["prediction_features"] = {
            "league_key": prediction.features.league_key if prediction.features else "",
            "season": prediction.features.season if prediction.features else "",
            "home": self._prediction_team_intelligence_payload(prediction, "home"),
            "away": self._prediction_team_intelligence_payload(prediction, "away"),
            "league": ((prediction.features.features or {}).get("league") or {}) if prediction.features else {},
            "data_freshness": ((prediction.features.features or {}).get("data_freshness") or {}) if prediction.features else {},
            "provider_quality": ((prediction.features.features or {}).get("provider_quality") or {}) if prediction.features else {},
        }
        home_recent_form = source_payload.get("home_recent_form") or self._prediction_recent_form_payload(prediction, "home")
        away_recent_form = source_payload.get("away_recent_form") or self._prediction_recent_form_payload(prediction, "away")
        corner_profile = source_payload.get("corner_profile") or self._prediction_corner_profile_payload(prediction)
        insights = {
            "prediction_engine": "prediction.api.predict_fixture",
            "prediction_diagnostics": prediction.diagnostics.to_dict(),
            "model_sources": list(prediction.diagnostics.model_sources or ()),
            "data_quality": prediction.diagnostics.data_quality,
            "odds_markets": sorted(str(key) for key in real_odds if key != "_meta"),
            "all_games_policy": "pricing.assess_all_games_policy",
            "top_picks_policy": "pricing.assess_top_picks_policy",
        }
        return {
            "fixture": source_payload.get("fixture", ""),
            "home_team": source_payload.get("home_team", ""),
            "away_team": source_payload.get("away_team", ""),
            "home_logo": source_payload.get("home_logo", ""),
            "away_logo": source_payload.get("away_logo", ""),
            "league": source_payload.get("league", ""),
            "league_logo": source_payload.get("league_logo", ""),
            "country": source_payload.get("country", ""),
            "country_flag": source_payload.get("country_flag", ""),
            "round": source_payload.get("round", ""),
            "league_type": source_payload.get("league_type", ""),
            "kickoff": source_payload.get("kickoff") or source_payload.get("kickoff_utc") or "",
            "match_id": source_payload.get("match_id", ""),
            "home_recent_form": home_recent_form,
            "away_recent_form": away_recent_form,
            "fixture_context": fixture_context,
            "team_news": source_payload.get("team_news") or {},
            "corner_profile": corner_profile,
            "market_count": len(market_payloads),
            "markets_70_plus": sum(1 for market in market_payloads if (market.get("confidence") or 0) >= 70),
            "markets_65_plus": sum(1 for market in market_payloads if (market.get("confidence") or 0) >= 65),
            "markets": market_payloads,
            "insights": insights,
            "source_payload": source_payload,
        }

    def _market_family_counts(self, markets):
        counts = defaultdict(int)
        for market in markets or []:
            family = (
                market.get("market_family")
                or (market.get("insights") or {}).get("market_family")
                or "unknown"
            )
            counts[str(family or "unknown")] += 1
        return dict(sorted(counts.items()))

    def _fixture_statpal_coverage(self, fixture):
        source_payload = fixture.get("source_payload") if isinstance(fixture.get("source_payload"), dict) else {}
        fixture_payload = {**source_payload, **fixture}
        try:
            from betpreneur.modules.catalog.api import StatPalDailyBuildService

            coverage = StatPalDailyBuildService().coverage_for_fixture(
                fixture_payload,
                include_optional=True,
            )
        except Exception as exc:
            statpal_context = ((fixture.get("fixture_context") or {}).get("statpal") or {})
            snapshots = sorted((statpal_context.get("snapshots") or {}).keys())
            return {
                "status": "error",
                "coverage_percent": 0.0,
                "present_snapshot_types": snapshots,
                "missing_snapshot_types": [],
                "stale_snapshot_types": [],
                "required_snapshot_types": [],
                "usable_field_count": 0,
                "error": str(exc),
            }

        snapshots = coverage.get("snapshots") or {}
        return {
            "status": coverage.get("status", "unknown"),
            "coverage_percent": coverage.get("coverage_percent", 0.0),
            "present_snapshot_types": sorted(key for key, item in snapshots.items() if item.get("present")),
            "missing_snapshot_types": coverage.get("missing_snapshot_types") or [],
            "stale_snapshot_types": coverage.get("stale_snapshot_types") or [],
            "required_snapshot_types": coverage.get("required_snapshot_types") or [],
            "optional_snapshot_types": coverage.get("optional_snapshot_types") or [],
            "usable_field_count": coverage.get("usable_field_count", 0),
            "identity": coverage.get("identity") or {},
            "endpoint_failures": coverage.get("endpoint_failures") or [],
        }

    def _market_statpal_diagnostics(self, market, statpal_context):
        from betpreneur.modules.markets.api import MarketCapabilityService

        capability = MarketCapabilityService().assess(
            market.get("market", ""),
            statpal_context=statpal_context or {},
        )
        return {
            "support_level": capability.support_level,
            "data_quality": capability.data_quality,
            "confidence_cap": capability.confidence_cap,
            "scoreable": capability.scoreable,
            "required_snapshot_types": capability.required_snapshots,
            "available_snapshot_types": capability.available_snapshots,
            "missing_snapshot_types": capability.missing_snapshots,
            "coverage_percent": capability.coverage_percent,
            "warnings": capability.warnings,
            "reason": capability.reason,
        }

    def _market_family_statpal_coverage(self, markets):
        grouped = {}
        for market in markets or []:
            insights = market.get("insights") or {}
            family = str(market.get("market_family") or insights.get("market_family") or "unknown")
            diagnostics = insights.get("statpal_market_coverage") or {}
            row = grouped.setdefault(
                family,
                {
                    "markets": 0,
                    "scoreable": 0,
                    "full": 0,
                    "partial": 0,
                    "missing": 0,
                    "coverage_total": 0.0,
                    "missing_snapshot_types": set(),
                    "warnings": set(),
                },
            )
            coverage = float(diagnostics.get("coverage_percent") or 0)
            row["markets"] += 1
            row["coverage_total"] += coverage
            if diagnostics.get("scoreable"):
                row["scoreable"] += 1
            if coverage >= 80:
                row["full"] += 1
            elif coverage > 0:
                row["partial"] += 1
            else:
                row["missing"] += 1
            row["missing_snapshot_types"].update(diagnostics.get("missing_snapshot_types") or [])
            row["warnings"].update(diagnostics.get("warnings") or [])

        result = {}
        for family, row in grouped.items():
            markets = row["markets"] or 1
            result[family] = {
                "markets": row["markets"],
                "scoreable": row["scoreable"],
                "full": row["full"],
                "partial": row["partial"],
                "missing": row["missing"],
                "average_coverage_percent": round(row["coverage_total"] / markets, 1),
                "missing_snapshot_types": sorted(row["missing_snapshot_types"]),
                "warnings": sorted(row["warnings"]),
            }
        return dict(sorted(result.items()))

    def _merge_market_family_statpal_coverage(self, aggregate, family_coverage):
        for family, item in (family_coverage or {}).items():
            row = aggregate.setdefault(
                family,
                {
                    "markets": 0,
                    "scoreable": 0,
                    "full": 0,
                    "partial": 0,
                    "missing": 0,
                    "coverage_total": 0.0,
                    "missing_snapshot_types": set(),
                    "warnings": set(),
                },
            )
            markets = int(item.get("markets") or 0)
            row["markets"] += markets
            row["scoreable"] += int(item.get("scoreable") or 0)
            row["full"] += int(item.get("full") or 0)
            row["partial"] += int(item.get("partial") or 0)
            row["missing"] += int(item.get("missing") or 0)
            row["coverage_total"] += float(item.get("average_coverage_percent") or 0) * markets
            row["missing_snapshot_types"].update(item.get("missing_snapshot_types") or [])
            row["warnings"].update(item.get("warnings") or [])

    def _finalize_market_family_statpal_coverage(self, aggregate):
        result = {}
        for family, row in (aggregate or {}).items():
            markets = row["markets"] or 1
            result[family] = {
                "markets": row["markets"],
                "scoreable": row["scoreable"],
                "full": row["full"],
                "partial": row["partial"],
                "missing": row["missing"],
                "average_coverage_percent": round(row["coverage_total"] / markets, 1),
                "missing_snapshot_types": sorted(row["missing_snapshot_types"]),
                "warnings": sorted(row["warnings"]),
            }
        return dict(sorted(result.items()))

    def _prediction_market_family_counts(self, predictions):
        counts = defaultdict(int)
        for prediction in predictions or []:
            insights = prediction.insights or {}
            route = insights.get("daily_evaluation_route") or {}
            family = route.get("family") or insights.get("market_family") or "unknown"
            counts[str(family or "unknown")] += 1
        return dict(sorted(counts.items()))

    def _prediction_statpal_family_coverage(self, predictions):
        markets = []
        for prediction in predictions or []:
            insights = prediction.insights or {}
            markets.append({
                "market": prediction.market,
                "market_family": insights.get("market_family") or (insights.get("daily_evaluation_route") or {}).get("family"),
                "insights": insights,
            })
        return self._market_family_statpal_coverage(markets)

    def _prediction_summary_metrics(self, algo_run):
        family_counts = defaultdict(int)
        statpal_coverage = {}
        queryset = (
            MarketPrediction.objects.filter(run=algo_run)
            .only("market", "insights")
            .order_by("id")
        )
        for prediction in queryset.iterator(chunk_size=500):
            insights = prediction.insights or {}
            route = insights.get("daily_evaluation_route") or {}
            family = route.get("family") or insights.get("market_family") or "unknown"
            family_counts[str(family or "unknown")] += 1
            self._merge_market_family_statpal_coverage(
                statpal_coverage,
                self._market_family_statpal_coverage([
                    {
                        "market": prediction.market,
                        "market_family": family,
                        "insights": insights,
                    }
                ]),
            )
        return {
            "market_family_counts": dict(sorted(family_counts.items())),
            "statpal_market_family_coverage": self._finalize_market_family_statpal_coverage(statpal_coverage),
        }

    def _enrich_fixture_statpal_diagnostics(self, fixture):
        enriched = dict(fixture)
        insights = dict(enriched.get("insights") or {})
        fixture_context = enriched.get("fixture_context") or {}
        statpal_context = fixture_context.get("statpal") or {}
        fixture_coverage = self._fixture_statpal_coverage(enriched)
        markets = []
        for market in enriched.get("markets") or []:
            market_payload = dict(market)
            market_insights = dict(market_payload.get("insights") or {})
            diagnostics = self._market_statpal_diagnostics(market_payload, statpal_context)
            market_insights["statpal_market_coverage"] = diagnostics
            market_payload["insights"] = market_insights
            markets.append(market_payload)
        family_coverage = self._market_family_statpal_coverage(markets)
        insights["statpal_fixture_coverage"] = fixture_coverage
        insights["statpal_market_family_coverage"] = family_coverage
        enriched["markets"] = markets
        enriched["insights"] = insights
        return enriched

    def _selected_pick_payload_from_prediction(self, prediction, tier, bankroll):
        risk_flags = list(prediction.risk_flags or [])
        insights = dict(prediction.insights or {})
        council = insights.get("council_review") or {}
        final_confidence = council.get("final_confidence") or prediction.confidence
        insights["published_tier"] = tier
        insights["optimization_profile"] = self._prediction_optimization_profile(prediction)
        profile = {
            Pick.Tier.BANKER: "reliability",
            Pick.Tier.VALUE_GEM: "mispriced_value",
            Pick.Tier.WILD_CARD: "high_upside",
        }.get(tier, "")
        if profile:
            risk_flags.append(f"profile:{profile}")
        return {
            "match_date": prediction.match_date,
            "fixture": prediction.fixture,
            "home_team": prediction.home_team,
            "away_team": prediction.away_team,
            "league": prediction.league,
            "kickoff": prediction.kickoff,
            "match_id": prediction.match_id,
            "tier": tier,
            "market": prediction.market,
            "meaning": prediction.meaning,
            "market_family": insights.get("market_family", ""),
            "market_category": insights.get("market_category", ""),
            "assessment_type": insights.get("assessment_type", ""),
            "evaluation_engine": insights.get("evaluation_engine", ""),
            "daily_evaluation_route": insights.get("daily_evaluation_route") or {},
            "market_identity": insights.get("market_identity") or {},
            "market_support_level": insights.get("market_support_level", ""),
            "optimization_profile": insights.get("optimization_profile") or {},
            "evidence": insights.get("evidence") or {},
            "bettor_view": insights.get("bettor_view") or {},
            "positive_evidence": insights.get("positive_evidence") or [],
            "risk_evidence": insights.get("risk_evidence") or [],
            "analysis_summary": insights.get("summary", ""),
            "analysis_conclusion": insights.get("conclusion", ""),
            "reasoning": self._prediction_reasoning_text(insights),
            "model_verdict": self._prediction_verdict_text(insights),
            "home_recent_form": prediction.home_recent_form or {},
            "away_recent_form": prediction.away_recent_form or {},
            "risk_flags": risk_flags,
            "insights": insights,
            "confidence": prediction.confidence,
            "final_confidence": final_confidence,
            "odds": prediction.odds,
            "ev": prediction.ev,
            "stake": round(max(100, float(bankroll or 10000) * 0.10), 2),
            "source": "APS",
        }

    def _prediction_reasoning_text(self, insights):
        parts = []
        summary = insights.get("summary") or ((insights.get("bettor_view") or {}).get("summary"))
        if summary:
            parts.append(str(summary))
        parts.extend([str(item) for item in (insights.get("positive_evidence") or []) if item][:3])
        conclusion = insights.get("conclusion") or ((insights.get("bettor_view") or {}).get("conclusion"))
        if conclusion and conclusion not in parts:
            parts.append(str(conclusion))
        return " ".join(" ".join(parts).split())

    def _prediction_verdict_text(self, insights):
        conclusion = insights.get("conclusion") or ((insights.get("bettor_view") or {}).get("conclusion"))
        return " ".join(str(conclusion or "").split())

    def _candidate_dict_for_reasoning(self, payload):
        return {
            "fixture": payload.get("fixture", ""),
            "home_team": payload.get("home_team", ""),
            "away_team": payload.get("away_team", ""),
            "league": payload.get("league", ""),
            "kickoff": payload.get("kickoff", ""),
            "match_id": payload.get("match_id", ""),
            "tier": payload.get("tier", ""),
            "market": payload.get("market", ""),
            "meaning": payload.get("meaning", ""),
            "market_family": payload.get("market_family", ""),
            "market_category": payload.get("market_category", ""),
            "assessment_type": payload.get("assessment_type", ""),
            "evaluation_engine": payload.get("evaluation_engine", ""),
            "daily_evaluation_route": payload.get("daily_evaluation_route") or {},
            "market_identity": payload.get("market_identity") or {},
            "market_support_level": payload.get("market_support_level", ""),
            "optimization_profile": payload.get("optimization_profile") or {},
            "evidence": payload.get("evidence") or {},
            "bettor_view": payload.get("bettor_view") or {},
            "positive_evidence": payload.get("positive_evidence") or [],
            "risk_evidence": payload.get("risk_evidence") or [],
            "analysis_summary": payload.get("analysis_summary", ""),
            "analysis_conclusion": payload.get("analysis_conclusion", ""),
            "conf": payload.get("final_confidence") or payload.get("confidence") or 0,
            "raw_confidence": payload.get("confidence") or 0,
            "final_confidence": payload.get("final_confidence") or payload.get("confidence") or 0,
            "odds": float(payload.get("odds") or 0),
            "ev": float(payload.get("ev") or 0),
            "home_recent_form": payload.get("home_recent_form") or {},
            "away_recent_form": payload.get("away_recent_form") or {},
            "fixture_context": payload.get("fixture_context") or {},
            "corner_profile": payload.get("corner_profile") or {},
            "team_news": payload.get("team_news") or {},
            "risk_flags": payload.get("risk_flags") or [],
            "selection_profile": next(
                (
                    str(flag).split(":", 1)[1]
                    for flag in payload.get("risk_flags") or []
                    if str(flag).startswith("profile:")
                ),
                "",
            ),
        }

    def _prediction_tier(self, prediction):
        policy = ((prediction.insights or {}).get("top_picks_policy") or {})
        policy_tier = str(policy.get("tier") or "").strip()
        if policy.get("publishable") and policy_tier in {
            Pick.Tier.BANKER,
            Pick.Tier.VALUE_GEM,
            Pick.Tier.WILD_CARD,
        }:
            return policy_tier
        if prediction.confidence >= 80:
            return Pick.Tier.BANKER
        if prediction.confidence >= 70:
            return Pick.Tier.VALUE_GEM
        if prediction.confidence >= 60:
            return Pick.Tier.WILD_CARD
        return None

    def _runner_env_bool(self, name, default=False):
        value = self._runner_env().get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _tier_after_council(self, prediction, candidate):
        base_tier = self._prediction_tier(prediction)
        if not base_tier:
            return None
        review = (candidate.get("insights") or {}).get("council_review") or {}
        decision = review.get("decision")
        council_tier = review.get("tier") or ""
        if decision == REJECT or not council_tier:
            return None
        if council_tier == Pick.Tier.WILD_CARD and not self._runner_env_bool("ALGO_PUBLISH_WILD_CARDS", False):
            return None
        if council_tier in {Pick.Tier.BANKER, Pick.Tier.VALUE_GEM, Pick.Tier.WILD_CARD}:
            return council_tier
        return None

    def _council_rejection_reason(self, candidate):
        review = (candidate.get("insights") or {}).get("council_review") or {}
        decision = review.get("decision") or "unknown"
        reasons = review.get("reasons") or []
        if decision == CAUTION:
            return "council_caution_downgraded_below_publish_tier"
        if reasons:
            return ", ".join([f"council_{decision}", *reasons][:4])
        return f"council_{decision}"

    def _market_daily_limit(self, market, max_daily):
        if market == "DC: 12":
            return max(0, self._runner_env_int("ALGO_MAX_DAILY_DC12_PICKS", 0))
        return max(0, self._runner_env_int("ALGO_MAX_DAILY_SAME_MARKET_PICKS", 0))

    def _prediction_market_family(self, prediction):
        insights = prediction.insights or {}
        route = insights.get("daily_evaluation_route") or {}
        family = route.get("family") or insights.get("market_family")
        if family:
            return str(family)
        from betpreneur.modules.markets.api import daily_evaluation_route

        return str(daily_evaluation_route(prediction.market).get("family") or "unknown")

    def _family_daily_limit(self, family, max_daily):
        raw_key = f"ALGO_MAX_DAILY_FAMILY_{str(family or '').upper()}_PICKS"
        specific = self._runner_env_int(raw_key, -1)
        if specific >= 0:
            return specific
        configured = self._runner_env_int("ALGO_MAX_DAILY_SAME_MARKET_FAMILY_PICKS", -1)
        if configured >= 0:
            return configured
        return max(2, math.ceil(max_daily * 0.40))

    def _allow_family_overflow(self):
        return self._runner_env_bool("ALGO_ALLOW_MARKET_FAMILY_OVERFLOW", True)

    def _prediction_can_overflow_family(self, prediction):
        return int(prediction.confidence or 0) >= self._runner_env_int(
            "ALGO_MARKET_FAMILY_OVERFLOW_MIN_CONFIDENCE",
            80,
        )

    def _daily_optimization_mode(self):
        mode = str(self._runner_env().get("ALGO_DAILY_OPTIMIZATION_MODE", "balanced") or "balanced").strip().lower()
        return mode if mode in {"safer", "balanced", "value"} else "balanced"

    def _prediction_value_score(self, prediction):
        try:
            ev = float(prediction.ev or 0)
        except (TypeError, ValueError):
            ev = 0.0
        try:
            odds = float(prediction.odds or 0)
        except (TypeError, ValueError):
            odds = 0.0
        return round((ev * 100) + min(odds, 6.0), 2)

    def _prediction_optimization_profile(self, prediction):
        confidence = float(prediction.confidence or 0)
        ev = float(prediction.ev or 0)
        value_score = self._prediction_value_score(prediction)
        risk_flags = {str(flag) for flag in (prediction.risk_flags or [])}
        severe_risk = bool(risk_flags & {
            "goal_line_boundary",
            "draw_boundary_risk",
            "under35_blowout_risk",
            "nordic_under_volatility",
            "wide_odds_market",
            "best_price_far_above_consensus",
        })
        if confidence >= self._runner_env_int("ALGO_SAFER_MODE_MIN_CONFIDENCE", 78) and not severe_risk:
            mode = "safer"
            label = "Safer"
        elif ev >= float(self._runner_env().get("ALGO_VALUE_MODE_MIN_EV", 0.08) or 0.08):
            mode = "value"
            label = "Value"
        else:
            mode = "balanced"
            label = "Balanced"
        return {
            "mode": mode,
            "label": label,
            "confidence_score": round(confidence, 1),
            "value_score": value_score,
            "expected_value": round(ev, 3),
        }

    def _prediction_matches_optimization_mode(self, prediction, mode):
        if mode == "balanced":
            return True
        profile = self._prediction_optimization_profile(prediction)
        return profile["mode"] == mode

    def _prediction_mode_rank(self, prediction):
        mode = self._daily_optimization_mode()
        confidence = float(prediction.confidence or 0)
        ev = float(prediction.ev or 0)
        if mode == "safer":
            return (confidence, ev, -float(prediction.odds or 0))
        if mode == "value":
            return (ev, self._prediction_value_score(prediction), confidence)
        return self._prediction_rank(prediction)

    def _pick_family_counts(self, picks):
        counts = defaultdict(int)
        for pick in picks or []:
            insights = pick.insights or {}
            route = insights.get("daily_evaluation_route") or {}
            family = route.get("family") or insights.get("market_family") or "unknown"
            counts[str(family or "unknown")] += 1
        return dict(sorted(counts.items()))

    def _pick_optimization_counts(self, picks):
        counts = defaultdict(int)
        for pick in picks or []:
            profile = (pick.insights or {}).get("optimization_profile") or {}
            mode = profile.get("mode") or "balanced"
            counts[str(mode or "balanced")] += 1
        return dict(sorted(counts.items()))

    def _prediction_reviewer_score(self, prediction, reviewer_name):
        review = ((prediction.insights or {}).get("council_review") or {})
        for item in review.get("reviewers") or []:
            if not isinstance(item, dict):
                continue
            if item.get("reviewer") == reviewer_name:
                try:
                    return float(item.get("score") or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _bounded_ev_score(self, ev):
        try:
            return max(-12.0, min(14.0, float(ev) * 35.0))
        except (TypeError, ValueError):
            return -12.0

    def _prediction_rank(self, prediction):
        ev = float(prediction.ev or 0)
        odds = float(prediction.odds or 0)
        raw_confidence = float(prediction.confidence or 0)
        review = ((prediction.insights or {}).get("council_review") or {})
        final_confidence = float(review.get("final_confidence") or raw_confidence)
        consensus = float(review.get("consensus_score") or final_confidence)
        disagreement = float(review.get("disagreement_score") or 0)
        market_fit = self._prediction_reviewer_score(prediction, "market_fit") or consensus
        scoreline_fit = self._prediction_reviewer_score(prediction, "scoreline_pattern") or consensus
        value_score = self._prediction_reviewer_score(prediction, "value") or consensus
        decision = str(review.get("decision") or "")
        decision_score = {"approve": 2, "caution": 1, "reject": -2}.get(decision, 0)
        risk_flags = set(prediction.risk_flags or [])
        council_score = (
            consensus * 0.34
            + market_fit * 0.22
            + scoreline_fit * 0.14
            + final_confidence * 0.18
            + value_score * 0.10
            + self._bounded_ev_score(ev)
            - disagreement * 0.45
        )
        if decision == "reject":
            council_score -= 70.0
        if "market_suppressed" in risk_flags or "strategy_suppressed" in risk_flags:
            council_score -= 35.0
        if "market_loss_streak" in risk_flags or "market_recent_losses" in risk_flags:
            council_score -= 22.0
        if "best_price_far_above_consensus" in risk_flags:
            council_score -= 18.0
        if "wide_odds_market" in risk_flags:
            council_score -= 12.0
        if "goal_line_boundary" in risk_flags:
            council_score -= 24.0
        if "under35_blowout_risk" in risk_flags:
            council_score -= 28.0
        if "nordic_under_volatility" in risk_flags:
            council_score -= 16.0
        return (
            decision_score,
            council_score,
            final_confidence,
            consensus,
            market_fit,
            scoreline_fit,
            ev,
            raw_confidence,
            -odds,
        )

    def _league_trust_for_prediction(self, prediction, performance=None):
        performance = performance or self._performance_profile()
        market_stats = (performance.get("markets") or {}).get(prediction.market) or {}
        league_market_key = f"{prediction.league}::{prediction.market}"
        league_market_stats = (performance.get("league_markets") or {}).get(league_market_key) or {}
        return assess_league_market_trust(league_market_stats, market_stats)

    def _confidence_band(self, confidence):
        confidence = int(confidence or 0)
        if confidence >= 80:
            return "80+"
        if confidence >= 75:
            return "75-79"
        if confidence >= 70:
            return "70-74"
        if confidence >= 65:
            return "65-69"
        return "Below 65"

    def _calibration_trust_for_prediction(self, prediction, performance=None):
        performance = performance or self._performance_profile()
        band = self._confidence_band(prediction.confidence)
        band_stats = (performance.get("confidence_bands") or {}).get(band) or {}
        return assess_calibration_trust(band_stats)

    def _recommendation_candidate(self, prediction, performance=None):
        insights = dict(prediction.insights or {})
        insights["league_trust"] = self._league_trust_for_prediction(prediction, performance)
        insights["calibration_trust"] = self._calibration_trust_for_prediction(prediction, performance)
        insights["optimization_profile"] = self._prediction_optimization_profile(prediction)
        if not insights.get("council_review"):
            top_policy = insights.get("top_picks_policy") or {}
            confidence = prediction.confidence
            insights["council_review"] = {
                "decision": "approve" if top_policy.get("publishable") else "reject",
                "tier": top_policy.get("tier", ""),
                "raw_confidence": confidence,
                "final_confidence": confidence,
                "consensus_score": ((insights.get("recommendation_score") or {}).get("recommendation_score")),
                "disagreement_score": None,
                "reasons": [
                    *list(top_policy.get("reasons") or ()),
                    *list(top_policy.get("warnings") or ()),
                ],
                "reviewers": ["prediction_policy"],
            }
        candidate = {
            "confidence": prediction.confidence,
            "ev": prediction.ev,
            "odds_meta": prediction.odds_meta or {},
            "odds_source": prediction.odds_source,
            "league": prediction.league,
            "country": (prediction.fixture_context or {}).get("country", ""),
            "risk_flags": prediction.risk_flags or [],
            "eligible": prediction.eligible,
            "market": prediction.market,
            "home_recent_form": prediction.home_recent_form or {},
            "away_recent_form": prediction.away_recent_form or {},
            "fixture_context": prediction.fixture_context or {},
            "corner_profile": (prediction.insights or {}).get("corner_profile") or {},
            "insights": insights,
        }
        return candidate

    def _select_prediction_ids(self, algo_run):
        max_daily = max(1, self._runner_env_int("ALGO_MAX_DAILY_PICKS", 15))
        optimization_mode = self._daily_optimization_mode()
        performance = (algo_run.result or {}).get("performance_profile") or self._performance_profile()

        predictions = list(
            MarketPrediction.objects.filter(run=algo_run)
            .filter(eligible=True, ev__isnull=False, insights__top_picks_policy__publishable=True)
            .exclude(market__in=["DC: 1X", "DC: X2"])
            .only(
                "id",
                "match_id",
                "market",
                "confidence",
                "ev",
                "odds",
                "odds_source",
                "odds_meta",
                "eligible",
                "risk_flags",
                "insights",
                "home_recent_form",
                "away_recent_form",
                "fixture_context",
                "league",
            )
            .order_by("-confidence", "-ev", "odds")
        )
        predictions.sort(key=self._prediction_mode_rank, reverse=True)

        buckets = {
            Pick.Tier.BANKER: [],
            Pick.Tier.VALUE_GEM: [],
            Pick.Tier.WILD_CARD: [],
        }
        used_matches = set()
        market_counts = defaultdict(int)
        family_counts = defaultdict(int)
        family_limit_rejections = set()

        def add_prediction(prediction, *, enforce_family_limit=True, enforce_mode=True):
            product_policy = ((prediction.insights or {}).get("top_picks_policy") or {})
            if not prediction.eligible or not product_policy.get("publishable"):
                return False
            if prediction.match_id in used_matches:
                return False
            if enforce_mode and not self._prediction_matches_optimization_mode(prediction, optimization_mode):
                return False
            family = self._prediction_market_family(prediction)
            family_limit = self._family_daily_limit(family, max_daily)
            if enforce_family_limit and family_limit and family_counts[family] >= family_limit:
                family_limit_rejections.add(prediction.id)
                return False
            candidate = self._recommendation_candidate(prediction, performance)
            tier = self._tier_after_council(prediction, candidate)
            if not tier:
                return False
            insights = dict(prediction.insights or {})
            insights["selection_family"] = family
            insights["selection_family_limit"] = family_limit
            insights["selection_family_count_before_pick"] = family_counts[family]
            insights["selection_optimization_mode"] = optimization_mode
            insights["optimization_profile"] = self._prediction_optimization_profile(prediction)
            if prediction.insights != insights:
                prediction.insights = insights
                prediction.save(update_fields=["insights"])
            buckets[tier].append(prediction.id)
            used_matches.add(prediction.match_id)
            market_counts[prediction.market] += 1
            family_counts[family] += 1
            return True

        def selected_count():
            return sum(len(items) for items in buckets.values())

        for prediction in predictions:
            if selected_count() >= max_daily:
                break
            market_limit = self._market_daily_limit(prediction.market, max_daily)
            if market_limit and market_counts[prediction.market] >= market_limit:
                continue
            add_prediction(prediction)

        if selected_count() < max_daily and self._allow_family_overflow():
            for prediction in predictions:
                if selected_count() >= max_daily:
                    break
                if prediction.id not in family_limit_rejections:
                    continue
                if not self._prediction_can_overflow_family(prediction):
                    continue
                market_limit = self._market_daily_limit(prediction.market, max_daily)
                if market_limit and market_counts[prediction.market] >= market_limit:
                    continue
                add_prediction(prediction, enforce_family_limit=False)

        if selected_count() < max_daily and optimization_mode != "balanced":
            for prediction in predictions:
                if selected_count() >= max_daily:
                    break
                market_limit = self._market_daily_limit(prediction.market, max_daily)
                if market_limit and market_counts[prediction.market] >= market_limit:
                    continue
                add_prediction(prediction, enforce_mode=False)

        return buckets

    def _refresh_recommendation_rejections(self, algo_run):
        updates = []
        performance = (algo_run.result or {}).get("performance_profile") or self._performance_profile()
        queryset = (
            MarketPrediction.objects.filter(run=algo_run, published=False)
            .only(
                "id",
                "market",
                "confidence",
                "ev",
                "odds",
                "odds_source",
                "odds_meta",
                "eligible",
                "risk_flags",
                "insights",
                "home_recent_form",
                "away_recent_form",
                "fixture_context",
                "league",
                "rejection_reason",
            )
            .order_by("id")
        )
        for prediction in queryset.iterator(chunk_size=500):
            candidate = self._recommendation_candidate(prediction, performance)
            assessment = assess_recommendation(candidate)
            council_tier = self._tier_after_council(prediction, candidate)
            if not assessment["recommended"]:
                rejection_reason = ", ".join(assessment["recommendation_reasons"][:4])
            elif not council_tier:
                rejection_reason = self._council_rejection_reason(candidate)
            else:
                rejection_reason = "not_top_pick"
            insights = dict(prediction.insights or {})
            changed = False
            if insights.get("league_trust") != candidate["insights"].get("league_trust"):
                insights["league_trust"] = candidate["insights"].get("league_trust")
                prediction.insights = insights
                changed = True
            if insights.get("calibration_trust") != candidate["insights"].get("calibration_trust"):
                insights["calibration_trust"] = candidate["insights"].get("calibration_trust")
                prediction.insights = insights
                changed = True
            if insights.get("council_review") != candidate["insights"].get("council_review"):
                insights["council_review"] = candidate["insights"].get("council_review")
                prediction.insights = insights
                changed = True
            if prediction.rejection_reason != rejection_reason:
                prediction.rejection_reason = rejection_reason
                changed = True
            if changed:
                updates.append(prediction)
            if len(updates) >= 500:
                MarketPrediction.objects.bulk_update(updates, ["rejection_reason", "insights"], batch_size=500)
                updates = []
        if updates:
            MarketPrediction.objects.bulk_update(updates, ["rejection_reason", "insights"], batch_size=500)

    def _publish_selected_predictions(self, algo_run, bankroll, use_llm=False):
        MarketPrediction.objects.filter(run=algo_run, published=True).update(
            published=False,
            selected_pick=None,
        )
        selected_ids = self._select_prediction_ids(algo_run)
        flat_ids = [pk for ids in selected_ids.values() for pk in ids]
        log.info(
            "Top-pick family-aware selection run=%s selected=%s buckets=%s",
            algo_run.id,
            len(flat_ids),
            {tier: len(ids) for tier, ids in selected_ids.items()},
        )
        if not flat_ids:
            Pick.objects.filter(run=algo_run).delete()
            return []

        predictions = {
            prediction.id: prediction
            for prediction in MarketPrediction.objects.filter(id__in=flat_ids).order_by("-confidence", "-ev")
        }
        payloads = []
        for tier in (Pick.Tier.BANKER, Pick.Tier.VALUE_GEM, Pick.Tier.WILD_CARD):
            for prediction_id in selected_ids[tier]:
                prediction = predictions.get(prediction_id)
                if prediction:
                    payloads.append(self._selected_pick_payload_from_prediction(prediction, tier, bankroll))

        if use_llm:
            from betpreneur.modules.catalog.api import legacy_runner as algo_runner

            reason_candidates = [self._candidate_dict_for_reasoning(payload) for payload in payloads]
            with temporary_env(self._runner_env()):
                algo_runner.enhance_pick_explanations_with_llm(reason_candidates)
            for payload, candidate in zip(payloads, reason_candidates):
                payload["reasoning"] = candidate.get("reasoning", payload["reasoning"])
                payload["model_verdict"] = candidate.get("model_verdict", payload["model_verdict"])
                payload["insights"] = candidate.get("insights") or payload.get("insights") or {}
                payload["bettor_view"] = (payload.get("insights") or {}).get("bettor_view") or payload.get("bettor_view") or {}
                payload["evidence"] = (payload.get("insights") or {}).get("evidence") or payload.get("evidence") or {}
                payload["positive_evidence"] = (
                    (payload.get("insights") or {}).get("positive_evidence")
                    or payload.get("positive_evidence")
                    or []
                )
                payload["risk_evidence"] = (
                    (payload.get("insights") or {}).get("risk_evidence")
                    or payload.get("risk_evidence")
                    or []
                )
                payload["analysis_summary"] = (
                    (payload.get("insights") or {}).get("summary")
                    or payload.get("analysis_summary")
                    or ""
                )
                payload["analysis_conclusion"] = (
                    (payload.get("insights") or {}).get("conclusion")
                    or payload.get("analysis_conclusion")
                    or ""
                )

        picks = self._persist_selected_picks(algo_run, {"selected_picks": payloads}) or []
        pick_lookup = {(str(pick.match_id or ""), pick.market): pick for pick in picks}
        updates = []
        for prediction in predictions.values():
            pick = pick_lookup.get((str(prediction.match_id or ""), prediction.market))
            if not pick:
                continue
            prediction.published = True
            prediction.selected_pick = pick
            prediction.rejection_reason = ""
            updates.append(prediction)
        if updates:
            MarketPrediction.objects.bulk_update(
                updates,
                ["published", "selected_pick", "rejection_reason"],
                batch_size=500,
            )
        log.info(
            "Top-pick published counts run=%s market_families=%s optimization_modes=%s",
            algo_run.id,
            self._pick_family_counts(picks),
            self._pick_optimization_counts(picks),
        )
        return picks

    def explain_picks_for_run(self, algo_run):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)
        picks = list(algo_run.picks.order_by("tier", "-confidence", "-ev"))
        if not picks:
            return {"run_id": algo_run.id, "updated": 0, "total": 0}

        from betpreneur.modules.catalog.api import legacy_runner as algo_runner

        candidates = []
        for pick in picks:
            council = ((pick.insights or {}).get("council_review") or {})
            final_confidence = council.get("final_confidence") or pick.confidence
            candidates.append({
                "fixture": pick.fixture,
                "home_team": pick.home_team,
                "away_team": pick.away_team,
                "league": pick.league,
                "kickoff": pick.kickoff,
                "match_id": pick.match_id,
                "tier": pick.tier,
                "market": pick.market,
                "meaning": pick.meaning,
                "conf": final_confidence,
                "raw_confidence": pick.confidence,
                "final_confidence": final_confidence,
                "odds": float(pick.odds or 0),
                "ev": float(pick.ev or 0),
                "home_recent_form": pick.home_recent_form or {},
                "away_recent_form": pick.away_recent_form or {},
                "risk_flags": pick.risk_flags or [],
                "insights": pick.insights or {},
                "reasoning": pick.reasoning,
                "model_verdict": pick.model_verdict,
            })
        with temporary_env(self._runner_env()):
            algo_runner.enhance_pick_explanations_with_llm(candidates)
        updated = 0
        for pick, candidate in zip(picks, candidates):
            reasoning = candidate.get("reasoning") or pick.reasoning
            verdict = candidate.get("model_verdict") or pick.model_verdict
            insights = candidate.get("insights") or pick.insights or {}
            changed = (
                reasoning != pick.reasoning
                or verdict != pick.model_verdict
                or insights != (pick.insights or {})
            )
            if changed:
                pick.reasoning = reasoning
                pick.model_verdict = verdict
                pick.insights = insights
                pick.save(update_fields=["reasoning", "model_verdict", "insights"])
                updated += 1
        log.info(
            "DeepSeek top-pick explanation task run=%s updated=%s total=%s structured_fields=%s",
            algo_run.id,
            updated,
            len(picks),
            ["bettor_view", "summary", "positive_evidence", "risk_evidence", "conclusion"],
        )
        return {"run_id": algo_run.id, "updated": updated, "total": len(picks)}

    def explain_game_headlines_for_run(self, algo_run):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)

        from betpreneur.modules.catalog.api import legacy_runner as algo_runner
        from betpreneur.modules.picks.services.presentation import _game_market_rank, market_prediction_payload

        selected_by_match = {}
        queryset = (
            MarketPrediction.objects.filter(run=algo_run, eligible=True)
            .select_related("selected_pick")
            .only(
                "id",
                "match_id",
                "fixture",
                "home_team",
                "away_team",
                "league",
                "kickoff",
                "market",
                "meaning",
                "raw_confidence",
                "confidence",
                "odds",
                "ev",
                "odds_source",
                "odds_meta",
                "eligible",
                "published",
                "selected_pick",
                "selected_pick_id",
                "risk_flags",
                "insights",
                "home_recent_form",
                "away_recent_form",
                "fixture_context",
            )
            .order_by("match_id", "-confidence", "-ev", "market")
        )
        for prediction in queryset.iterator(chunk_size=500):
            payload = market_prediction_payload(prediction)
            current = selected_by_match.get(prediction.match_id)
            if current and _game_market_rank(current[1]) >= _game_market_rank(payload):
                continue
            selected_by_match[prediction.match_id] = (prediction, payload)

        if not selected_by_match:
            return {"run_id": algo_run.id, "updated": 0, "total": 0}

        fixture_context_by_match = {
            fixture.match_id: {
                "team_news": fixture.team_news or {},
                "corner_profile": fixture.corner_profile or {},
                "fixture_context": fixture.fixture_context or {},
            }
            for fixture in AlgoFixture.objects.filter(run=algo_run, match_id__in=selected_by_match.keys()).only(
                "match_id",
                "team_news",
                "corner_profile",
                "fixture_context",
            )
        }
        predictions = []
        candidates = []
        for prediction, payload in selected_by_match.values():
            context = fixture_context_by_match.get(prediction.match_id) or {}
            payload.update(context)
            payload["tier"] = "game_analysis"
            predictions.append(prediction)
            candidates.append(self._candidate_dict_for_reasoning(payload))

        with temporary_env(self._runner_env()):
            algo_runner.enhance_pick_explanations_with_llm(candidates)

        updates = []
        for prediction, candidate in zip(predictions, candidates):
            reasoning = candidate.get("reasoning") or ""
            verdict = candidate.get("model_verdict") or ""
            if not reasoning and not verdict and not candidate.get("insights"):
                continue
            insights = dict(candidate.get("insights") or prediction.insights or {})
            current_public_analysis = insights.get("public_analysis") or {}
            public_analysis = dict(current_public_analysis) if isinstance(current_public_analysis, dict) else {}
            if reasoning:
                public_analysis["reasoning"] = reasoning
            if verdict:
                public_analysis["model_verdict"] = verdict
            if public_analysis:
                insights["public_analysis"] = public_analysis
            if insights != (prediction.insights or {}):
                prediction.insights = insights
                updates.append(prediction)
            if len(updates) >= 500:
                MarketPrediction.objects.bulk_update(updates, ["insights"], batch_size=500)
                updates = []
        if updates:
            MarketPrediction.objects.bulk_update(updates, ["insights"], batch_size=500)
        updated = len([candidate for candidate in candidates if candidate.get("reasoning") or candidate.get("model_verdict")])
        log.info(
            "DeepSeek game-headline explanation task run=%s updated=%s total=%s",
            algo_run.id,
            updated,
            len(candidates),
        )
        return {"run_id": algo_run.id, "updated": updated, "total": len(candidates)}

    def _persist_failed_fixture(self, algo_run, fixture, error):
        match_id = str(fixture.get("match_id") or fixture.get("aps_id") or "")
        AlgoFixture.objects.update_or_create(
            run=algo_run,
            match_id=match_id,
            defaults={
                "match_date": algo_run.target_date,
                "fixture": self._text(fixture.get("fixture", "")),
                "home_team": self._text(fixture.get("hname", "")),
                "away_team": self._text(fixture.get("aname", "")),
                "home_logo": self._text(fixture.get("home_logo", "")),
                "away_logo": self._text(fixture.get("away_logo", "")),
                "league": self._text(fixture.get("league", "")),
                "league_logo": self._text(fixture.get("league_logo", "")),
                "country": self._text(fixture.get("country", "")),
                "country_flag": self._text(fixture.get("country_flag", "")),
                "round": self._text(fixture.get("round", "")),
                "league_type": self._text(fixture.get("league_type", "")),
                "kickoff": self._text(fixture.get("kickoff", "")),
                "source_payload": json_safe(fixture),
                "status": AlgoFixture.Status.FAILED,
                "error": str(error)[:2000],
            },
        )

    def _run_storage_payload(self):
        return {
            "fixtures": "algo_algofixture",
            "markets": "algo_marketprediction",
            "picks": "algo_pick",
        }

    def _pipeline_profiles(self, target_date):
        performance_profile = self._performance_profile()
        strategy_profile = self._strategy_profile(target_date, performance_profile)
        return performance_profile, strategy_profile

    def _pipeline_env(self, algo_run):
        result = algo_run.result or {}
        performance_profile = result.get("performance_profile") or self._performance_profile()
        strategy_profile = result.get("strategy_profile") or self._strategy_profile(
            algo_run.target_date,
            performance_profile,
        )
        return self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
            "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
        })

    def prepare_fanout_run(self, algo_run: AlgoRun):
        algo_run.status = AlgoRun.Status.RUNNING
        algo_run.started_at = timezone.now()
        performance_profile, strategy_profile = self._pipeline_profiles(algo_run.target_date)
        env = self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
            "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
        })
        try:
            with temporary_env(env):
                from betpreneur.modules.catalog.api import legacy_runner as algo_runner

                bankroll = algo_runner.get_bankroll(None)
                fixture_bundle = self._daily_runner_fixtures(algo_run.target_date)
                fixtures = self._limit_fixtures(fixture_bundle.get("fixtures") or [])

            log.info(
                "Daily fixture source run=%s source=%s fixtures=%s fallback=%s errors=%s",
                algo_run.id,
                fixture_bundle.get("source"),
                len(fixtures),
                fixture_bundle.get("fallback_used"),
                fixture_bundle.get("errors") or [],
            )

            Pick.objects.filter(run=algo_run).delete()
            AlgoFixture.objects.filter(run=algo_run).delete()
            MarketPrediction.objects.filter(run=algo_run).delete()

            rows = []
            for fixture in fixtures:
                match_id = str(fixture.get("match_id") or fixture.get("aps_id") or "")
                rows.append(
                    AlgoFixture(
                        run=algo_run,
                        match_date=algo_run.target_date,
                        fixture=self._text(fixture.get("fixture", "")),
                        home_team=self._text(fixture.get("hname", "")),
                        away_team=self._text(fixture.get("aname", "")),
                        home_logo=self._text(fixture.get("home_logo", "")),
                        away_logo=self._text(fixture.get("away_logo", "")),
                        league=self._text(fixture.get("league", "")),
                        league_logo=self._text(fixture.get("league_logo", "")),
                        country=self._text(fixture.get("country", "")),
                        country_flag=self._text(fixture.get("country_flag", "")),
                        round=self._text(fixture.get("round", "")),
                        league_type=self._text(fixture.get("league_type", "")),
                        kickoff=self._text(fixture.get("kickoff", "")),
                        match_id=match_id,
                        source_payload=json_safe(fixture),
                        status=AlgoFixture.Status.PENDING,
                    )
                )
            AlgoFixture.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)

            algo_run.fd_fixtures = 0
            algo_run.aps_fixtures = len(rows)
            algo_run.bankroll = bankroll
            if rows:
                algo_run.result = {
                    "status": AlgoRun.Status.RUNNING,
                    "date": algo_run.target_date.isoformat(),
                    "publish_policy": "celery_fanout_pipeline",
                    "fixture_source": fixture_bundle.get("source"),
                    "statpal_primary_enabled": self._statpal_primary_daily_enabled(),
                    "statpal_fallback_used": fixture_bundle.get("fallback_used"),
                    "statpal_fixture_errors": fixture_bundle.get("errors") or [],
                    "strategy_profile": strategy_profile,
                    "performance_profile": performance_profile,
                    "storage": self._run_storage_payload(),
                }
            else:
                algo_run.status = AlgoRun.Status.REST_DAY
                algo_run.finished_at = timezone.now()
                algo_run.result = {
                    "status": AlgoRun.Status.REST_DAY,
                    "date": algo_run.target_date.isoformat(),
                    "picks_count": 0,
                    "fixture_source": fixture_bundle.get("source"),
                    "statpal_primary_enabled": self._statpal_primary_daily_enabled(),
                    "statpal_fallback_used": fixture_bundle.get("fallback_used"),
                    "statpal_fixture_errors": fixture_bundle.get("errors") or [],
                    "strategy_profile": strategy_profile,
                    "storage": self._run_storage_payload(),
                }
            algo_run.save()
            return list(
                AlgoFixture.objects.filter(run=algo_run)
                .order_by("id")
                .values_list("id", flat=True)
            )
        except Exception as exc:
            algo_run.status = AlgoRun.Status.FAILED
            algo_run.error = str(exc)
            algo_run.finished_at = timezone.now()
            algo_run.save()
            return []

    def score_fixture_for_run(self, fixture_id):
        try:
            fixture = AlgoFixture.objects.select_related("run").get(id=fixture_id)
        except AlgoFixture.DoesNotExist:
            return {
                "fixture_id": fixture_id,
                "status": "skipped",
                "error": "fixture_not_found",
            }
        algo_run = fixture.run
        if algo_run.status not in {AlgoRun.Status.RUNNING, AlgoRun.Status.PENDING}:
            return {"fixture_id": fixture.id, "status": "skipped", "run_status": algo_run.status}

        try:
            with temporary_env(self._pipeline_env(algo_run)):
                source_payload = dict(fixture.source_payload or {})
                source_payload = self._hydrate_statpal_scoring_context(source_payload)
                summary = self._score_fixture_with_prediction_engine(source_payload)
                summary = self._enrich_fixture_statpal_diagnostics(summary)
                self._persist_fixture_summary(algo_run, summary)
                market_count = self._persist_fixture_market_predictions(algo_run, summary)
                cache_source = (
                    "on_demand"
                    if (algo_run.result or {}).get("publish_policy") == "on_demand_fixture_analysis"
                    else "merged"
                )
                slip_cache = self._write_slip_review_market_cache(algo_run, summary, source=cache_source)
                family_counts = self._market_family_counts(summary.get("markets") or [])
                statpal_family_coverage = (summary.get("insights") or {}).get("statpal_market_family_coverage") or {}
                log.info(
                    "Daily fixture scored run=%s match_id=%s markets=%s slip_cache=%s market_families=%s provider_merge=%s statpal_family_coverage=%s",
                    algo_run.id,
                    summary.get("match_id"),
                    market_count,
                    slip_cache,
                    family_counts,
                    ((summary.get("source_payload") or {}).get("provider_merge") or {}),
                    statpal_family_coverage,
                )
            return {
                "fixture_id": fixture.id,
                "status": "scored",
                "market_count": market_count,
                "slip_review_market_cache": slip_cache,
                "market_families": family_counts,
                "statpal_market_family_coverage": statpal_family_coverage,
            }
        except Exception as exc:
            fixture.status = AlgoFixture.Status.FAILED
            fixture.error = str(exc)[:2000]
            fixture.save(update_fields=["status", "error", "updated_at"])
            return {"fixture_id": fixture.id, "status": "failed", "error": str(exc)}

    def score_cached_fixture_on_demand(self, match_id, *, match_date=None, reason="on_demand", force=False):
        match_id = str(match_id or "").strip()
        if not match_id:
            return {"status": "failed", "error": "match_id_required"}
        existing = MarketPrediction.objects.filter(match_id=match_id).order_by("-run__created_at", "-created_at").first()
        if existing and not force:
            return {
                "status": "already_scored",
                "match_id": match_id,
                "run_id": existing.run_id,
            }

        fixture_query = FixtureCache.objects.filter(match_id=match_id)
        if match_date:
            fixture_query = fixture_query.filter(match_date=match_date)
        cached = fixture_query.order_by("match_date", "-updated_at").first()
        if not cached:
            return {"status": "failed", "match_id": match_id, "error": "fixture_cache_not_found"}

        target_date = cached.match_date
        performance_profile, strategy_profile = self._pipeline_profiles(target_date)
        algo_run = AlgoRun.objects.create(
            target_date=target_date,
            status=AlgoRun.Status.RUNNING,
            started_at=timezone.now(),
            fd_fixtures=0,
            aps_fixtures=1,
            result={
                "status": AlgoRun.Status.RUNNING,
                "date": target_date.isoformat(),
                "publish_policy": "on_demand_fixture_analysis",
                "reason": reason,
                "strategy_profile": strategy_profile,
                "performance_profile": performance_profile,
                "storage": self._run_storage_payload(),
            },
        )
        source_payload = self._cached_fixture_runner_payload(cached)
        source_payload = self._enrich_fixture_for_cross_provider_scoring(source_payload, target_date)
        log.info(
            "On-demand provider merge match_id=%s target_date=%s reason=%s provider_merge=%s",
            match_id,
            target_date,
            reason,
            source_payload.get("provider_merge") or {},
        )
        fixture = AlgoFixture.objects.create(
            run=algo_run,
            match_date=target_date,
            fixture=cached.fixture,
            home_team=cached.home_team,
            away_team=cached.away_team,
            home_logo=cached.home_logo,
            away_logo=cached.away_logo,
            league=cached.league,
            league_logo=cached.league_logo,
            country=cached.country,
            country_flag=cached.country_flag,
            round=cached.round,
            league_type=cached.league_type,
            kickoff=cached.kickoff,
            match_id=match_id,
            source_payload=json_safe(source_payload),
            status=AlgoFixture.Status.PENDING,
        )
        result = self.score_fixture_for_run(fixture.id)
        fixture.refresh_from_db()
        scored_count = 1 if result.get("status") == "scored" else 0
        market_count = int(result.get("market_count") or 0)
        log.info(
            "On-demand fixture scoring result match_id=%s run_id=%s fixture_id=%s status=%s market_count=%s error=%s",
            match_id,
            algo_run.id,
            fixture.id,
            result.get("status"),
            market_count,
            str(result.get("error") or "")[:500],
        )
        algo_run.refresh_from_db()
        algo_run.status = AlgoRun.Status.SUCCESS if scored_count else AlgoRun.Status.NO_DATA
        algo_run.total_scored = scored_count
        algo_run.finished_at = timezone.now()
        run_result = dict(algo_run.result or {})
        run_result.update({
            "status": algo_run.status,
            "total_scored": scored_count,
            "market_count": market_count,
            "on_demand_match_id": match_id,
            "on_demand_fixture_id": fixture.id,
            "statpal_market_family_coverage": result.get("statpal_market_family_coverage") or {},
        })
        if result.get("error"):
            algo_run.error = str(result.get("error"))[:2000]
            run_result["error"] = algo_run.error
        algo_run.result = run_result
        algo_run.save(update_fields=["status", "total_scored", "finished_at", "error", "result", "updated_at"])
        return {
            **result,
            "run_id": algo_run.id,
            "match_id": match_id,
            "target_date": target_date.isoformat(),
        }

    def _slip_review_cache_fresh(self, match_id):
        return SlipReviewMarketCache.objects.filter(
            cache_scope=SlipReviewMarketCache.Scope.SLIP_REVIEW,
            match_id=str(match_id or ""),
            expires_at__gt=timezone.now(),
        ).exists()

    def score_fixture_for_slip_review_market_cache(self, cached: FixtureCache, *, force=False):
        close_old_connections()
        match_id = str(cached.match_id or "").strip()
        if not match_id:
            return {"status": "failed", "error": "match_id_required"}
        try:
            if not force and self._slip_review_cache_fresh(match_id):
                return {
                    "status": "cached",
                    "match_id": match_id,
                    "fixture": cached.fixture,
                }
        finally:
            close_old_connections()

        target_date = cached.match_date
        performance_profile, strategy_profile = self._pipeline_profiles(target_date)
        try:
            close_old_connections()
            with temporary_env(self._runner_env({
                "OVERRIDE_DATE": target_date.isoformat(),
                "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
                "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
            })):
                from betpreneur.modules.catalog.api import SlipReviewMarketCacheWriter

                source_payload = self._cached_fixture_runner_payload(cached)
                source_payload = self._enrich_fixture_for_cross_provider_scoring(source_payload, target_date)
                source_payload = self._hydrate_statpal_scoring_context(source_payload)
                summary = self._score_fixture_with_prediction_engine(source_payload)
                summary["match_date"] = target_date
                summary = self._enrich_fixture_statpal_diagnostics(summary)
                cache_summary = SlipReviewMarketCacheWriter().upsert_fixture_markets(
                    summary,
                    source=SlipReviewMarketCache.Source.MERGED,
                )
        except Exception as exc:
            close_old_connections()
            log.warning(
                "Slip review private fixture scoring failed match_id=%s fixture=%r error=%s",
                match_id,
                cached.fixture,
                exc,
            )
            return {
                "status": "failed",
                "match_id": match_id,
                "fixture": cached.fixture,
                "error": str(exc)[:1000],
            }
        finally:
            close_old_connections()

        markets = summary.get("markets") or []
        return {
            "status": "scored",
            "match_id": match_id,
            "fixture": summary.get("fixture") or cached.fixture,
            "market_count": len(markets),
            "cache": cache_summary,
            "market_families": self._market_family_counts(markets),
            "provider_merge": ((summary.get("source_payload") or {}).get("provider_merge") or {}),
        }

    def build_slip_review_market_cache(
        self,
        *,
        start_date=None,
        days=None,
        sync_fixtures=True,
        force=False,
        max_fixtures=None,
    ):
        start_date = start_date or timezone.localdate()
        days = int(days if days is not None else getattr(settings, "SLIP_REVIEW_MARKET_CACHE_BUILD_DAYS", 3))
        days = max(0, days)
        configured_limit = getattr(settings, "SLIP_REVIEW_MARKET_CACHE_BUILD_MAX_FIXTURES", 0)
        max_fixtures = int(max_fixtures if max_fixtures is not None else configured_limit or 0)
        end_date = start_date + timedelta(days=days)

        sync_result = {}
        if sync_fixtures:
            sync_result = FixtureSearchService().sync_statpal_horizon(
                start_date=start_date,
                days=days,
                league_ids=None,
            )

        queryset = (
            FixtureCache.objects.filter(
                match_date__range=(start_date, end_date),
                source="statpal",
            )
            .order_by("match_date", "country", "league", "kickoff", "fixture")
        )
        total_available = queryset.count()
        if max_fixtures > 0:
            queryset = queryset[:max_fixtures]
        fixtures = list(queryset)
        close_old_connections()

        scored = cached = failed = 0
        market_count = 0
        errors = []
        family_counts = defaultdict(int)
        started_at = timezone.now()
        considered_limit = total_available if max_fixtures <= 0 else min(total_available, max_fixtures)
        for index, fixture in enumerate(fixtures, start=1):
            result = self.score_fixture_for_slip_review_market_cache(fixture, force=force)
            close_old_connections()
            status_value = result.get("status")
            if status_value == "scored":
                scored += 1
                market_count += int(result.get("market_count") or 0)
                for family, count in (result.get("market_families") or {}).items():
                    family_counts[family] += int(count or 0)
            elif status_value == "cached":
                cached += 1
            else:
                failed += 1
                if len(errors) < 25:
                    errors.append({
                        "match_id": result.get("match_id"),
                        "fixture": result.get("fixture"),
                        "error": result.get("error", "unknown_error"),
                    })
            if index % 25 == 0:
                log.info(
                    "Slip review private cache build progress fixtures=%s/%s scored=%s cached=%s failed=%s markets=%s",
                    index,
                    considered_limit,
                    scored,
                    cached,
                    failed,
                    market_count,
                )

        result = {
            "status": "completed",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days": days,
            "sync": sync_result,
            "fixtures_available": total_available,
            "fixtures_considered": scored + cached + failed,
            "fixtures_scored": scored,
            "fixtures_served_from_cache": cached,
            "fixtures_failed": failed,
            "market_count": market_count,
            "market_family_counts": dict(sorted(family_counts.items())),
            "force": bool(force),
            "max_fixtures": max_fixtures,
            "duration_seconds": round((timezone.now() - started_at).total_seconds(), 3),
            "errors": errors,
        }
        log.info("Slip review private market cache build done %s", result)
        return result

    def cleanup_slip_review_market_cache(self, *, grace_seconds=None, limit=None):
        grace_seconds = int(
            grace_seconds
            if grace_seconds is not None
            else getattr(settings, "SLIP_REVIEW_MARKET_CACHE_CLEANUP_GRACE_SECONDS", 0)
        )
        limit = int(
            limit
            if limit is not None
            else getattr(settings, "SLIP_REVIEW_MARKET_CACHE_CLEANUP_LIMIT", 0)
        )
        cutoff = timezone.now() - timedelta(seconds=max(0, grace_seconds))
        queryset = SlipReviewMarketCache.objects.filter(
            cache_scope=SlipReviewMarketCache.Scope.SLIP_REVIEW,
            expires_at__lte=cutoff,
        )
        expired_count = queryset.count()
        if limit > 0:
            ids = list(queryset.order_by("expires_at").values_list("id", flat=True)[:limit])
            deleted, _ = SlipReviewMarketCache.objects.filter(id__in=ids).delete()
        else:
            deleted, _ = queryset.delete()
        remaining_expired = max(0, expired_count - deleted)
        result = {
            "status": "completed",
            "cutoff": cutoff.isoformat(),
            "grace_seconds": grace_seconds,
            "limit": limit,
            "expired_count": expired_count,
            "deleted": deleted,
            "remaining_expired": remaining_expired,
        }
        log.info("Slip review private market cache cleanup done %s", result)
        return result

    def publish_fanout_run(self, algo_run: AlgoRun):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)
        algo_run.refresh_from_db()
        bankroll = algo_run.bankroll or 10000
        picks = self._publish_selected_predictions(algo_run, bankroll)
        scored_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.SCORED).count()
        failed_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.FAILED).count()
        prediction_metrics = self._prediction_summary_metrics(algo_run)
        aggregate = MarketPrediction.objects.filter(run=algo_run).aggregate(
            market_count=Count("id"),
            markets_70_plus=Count("id", filter=Q(confidence__gte=70)),
            markets_65_plus=Count("id", filter=Q(confidence__gte=65)),
        )
        algo_run.status = AlgoRun.Status.SUCCESS if scored_count else AlgoRun.Status.NO_DATA
        algo_run.total_scored = scored_count
        algo_run.picks_count = len(picks)
        algo_run.bankers = sum(1 for pick in picks if pick.tier == Pick.Tier.BANKER)
        algo_run.value_gems = sum(1 for pick in picks if pick.tier == Pick.Tier.VALUE_GEM)
        algo_run.wild_cards = sum(1 for pick in picks if pick.tier == Pick.Tier.WILD_CARD)
        result = algo_run.result or {}
        algo_run.result = {
            "status": algo_run.status,
            "date": algo_run.target_date.isoformat(),
            "fd_fixtures": 0,
            "aps_fixtures": algo_run.aps_fixtures,
            "total_scored": scored_count,
            "failed_fixtures": failed_count,
            "picks_count": len(picks),
            "no_bet": len(picks) == 0,
            "publish_policy": "strict_accuracy_gate",
            "strategy_profile": result.get("strategy_profile", {}),
            "performance_profile": result.get("performance_profile", {}),
            "market_count": aggregate.get("market_count") or 0,
            "market_family_counts": prediction_metrics["market_family_counts"],
            "statpal_market_family_coverage": prediction_metrics["statpal_market_family_coverage"],
            "top_pick_market_family_counts": self._pick_family_counts(picks),
            "top_pick_optimization_counts": self._pick_optimization_counts(picks),
            "daily_optimization_mode": self._daily_optimization_mode(),
            "markets_70_plus": aggregate.get("markets_70_plus") or 0,
            "markets_65_plus": aggregate.get("markets_65_plus") or 0,
            "fixture_count": scored_count,
            "bankers": algo_run.bankers,
            "value_gems": algo_run.value_gems,
            "wild_cards": algo_run.wild_cards,
            "bankroll": float(bankroll),
            "storage": self._run_storage_payload(),
        }
        log.info(
            "Daily run published run=%s market_families=%s statpal_family_coverage=%s",
            algo_run.id,
            algo_run.result.get("market_family_counts") or {},
            algo_run.result.get("statpal_market_family_coverage") or {},
        )
        algo_run.finished_at = timezone.now()
        algo_run.save()
        return algo_run

    def recover_fanout_run(self, algo_run, *, rescore_failed=False):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)
        rescored = []
        if rescore_failed:
            fixture_ids = list(
                AlgoFixture.objects.filter(
                    run=algo_run,
                    status__in=[AlgoFixture.Status.PENDING, AlgoFixture.Status.FAILED],
                ).values_list("id", flat=True)
            )
            for fixture_id in fixture_ids:
                rescored.append(self.score_fixture_for_run(fixture_id))
        published = self.publish_fanout_run(algo_run)
        return {
            "run_id": published.id,
            "target_date": published.target_date.isoformat(),
            "status": published.status,
            "rescored": rescored,
            "picks_count": published.picks_count,
            "bankers": published.bankers,
            "value_gems": published.value_gems,
            "wild_cards": published.wild_cards,
        }

    def run_staged(self, algo_run: AlgoRun) -> AlgoRun:
        algo_run.status = AlgoRun.Status.RUNNING
        algo_run.started_at = timezone.now()
        algo_run.save(update_fields=["status", "started_at", "updated_at"])

        performance_profile = self._performance_profile()
        strategy_profile = self._strategy_profile(algo_run.target_date, performance_profile)
        env = self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
            "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
        })

        try:
            with temporary_env(env):
                from betpreneur.modules.catalog.api import legacy_runner as algo_runner

                algo_runner.clear_runtime_caches()
                algo_runner.log_memory("staged_start")
                bankroll = algo_runner.get_bankroll(None)
                fixture_bundle = self._daily_runner_fixtures(algo_run.target_date)
                fixtures = self._limit_fixtures(fixture_bundle.get("fixtures") or [])

                log.info(
                    "Daily fixture source run=%s source=%s fixtures=%s fallback=%s errors=%s",
                    algo_run.id,
                    fixture_bundle.get("source"),
                    len(fixtures),
                    fixture_bundle.get("fallback_used"),
                    fixture_bundle.get("errors") or [],
                )

                algo_run.aps_fixtures = len(fixtures)
                algo_run.fd_fixtures = 0
                algo_run.bankroll = bankroll
                algo_run.save(update_fields=["aps_fixtures", "fd_fixtures", "bankroll", "updated_at"])

                Pick.objects.filter(run=algo_run).delete()
                AlgoFixture.objects.filter(run=algo_run).delete()
                MarketPrediction.objects.filter(run=algo_run).delete()

                if not fixtures:
                    algo_run.status = AlgoRun.Status.REST_DAY
                    algo_run.result = {
                        "status": AlgoRun.Status.REST_DAY,
                        "date": algo_run.target_date.isoformat(),
                        "picks_count": 0,
                        "fixture_source": fixture_bundle.get("source"),
                        "statpal_primary_enabled": self._statpal_primary_daily_enabled(),
                        "statpal_fallback_used": fixture_bundle.get("fallback_used"),
                        "statpal_fixture_errors": fixture_bundle.get("errors") or [],
                        "strategy_profile": strategy_profile,
                        "storage": {
                            "fixtures": "algo_algofixture",
                            "markets": "algo_marketprediction",
                            "picks": "algo_pick",
                        },
                    }
                    return algo_run

                scored_count = 0
                market_count = 0
                markets_70_plus = 0
                markets_65_plus = 0
                market_family_counts = defaultdict(int)
                statpal_family_coverage = {}
                for index, fixture in enumerate(fixtures, start=1):
                    try:
                        fixture = self._hydrate_statpal_scoring_context(fixture)
                        summary = self._score_fixture_with_prediction_engine(fixture)
                        summary = self._enrich_fixture_statpal_diagnostics(summary)
                        for family, count in self._market_family_counts(summary.get("markets") or []).items():
                            market_family_counts[family] += count
                        self._merge_market_family_statpal_coverage(
                            statpal_family_coverage,
                            (summary.get("insights") or {}).get("statpal_market_family_coverage") or {},
                        )
                        self._persist_fixture_summary(algo_run, summary)
                        market_count += self._persist_fixture_market_predictions(algo_run, summary)
                        self._write_slip_review_market_cache(algo_run, summary, source="merged")
                        markets_70_plus += summary.get("markets_70_plus", 0)
                        markets_65_plus += summary.get("markets_65_plus", 0)
                        scored_count += 1
                        if index % 10 == 0 or index == len(fixtures):
                            algo_runner.log_memory(f"staged_scored_{index}_of_{len(fixtures)}")
                            log.info(
                                "Daily scoring progress run=%s fixtures=%s/%s markets=%s market_families=%s statpal_family_coverage=%s",
                                algo_run.id,
                                index,
                                len(fixtures),
                                market_count,
                                dict(sorted(market_family_counts.items())),
                                self._finalize_market_family_statpal_coverage(statpal_family_coverage),
                            )
                    except Exception as exc:
                        self._persist_failed_fixture(algo_run, fixture, exc)

                picks = self._publish_selected_predictions(algo_run, bankroll)
                algo_runner.clear_runtime_caches()
                algo_runner.log_memory("staged_end")

            algo_run.status = AlgoRun.Status.SUCCESS if scored_count else AlgoRun.Status.NO_DATA
            algo_run.total_scored = scored_count
            algo_run.picks_count = len(picks)
            algo_run.bankers = sum(1 for pick in picks if pick.tier == Pick.Tier.BANKER)
            algo_run.value_gems = sum(1 for pick in picks if pick.tier == Pick.Tier.VALUE_GEM)
            algo_run.wild_cards = sum(1 for pick in picks if pick.tier == Pick.Tier.WILD_CARD)
            algo_run.result = {
                "status": algo_run.status,
                "date": algo_run.target_date.isoformat(),
                "fd_fixtures": 0,
                "aps_fixtures": len(fixtures),
                "total_scored": scored_count,
                "picks_count": len(picks),
                "no_bet": len(picks) == 0,
                "publish_policy": "staged_db_pipeline",
                "strategy_profile": strategy_profile,
                "performance_profile": performance_profile,
                "fixture_source": fixture_bundle.get("source"),
                "statpal_primary_enabled": self._statpal_primary_daily_enabled(),
                "statpal_fallback_used": fixture_bundle.get("fallback_used"),
                "statpal_fixture_errors": fixture_bundle.get("errors") or [],
                "market_count": market_count,
                "market_family_counts": dict(sorted(market_family_counts.items())),
                "statpal_market_family_coverage": self._finalize_market_family_statpal_coverage(statpal_family_coverage),
                "top_pick_market_family_counts": self._pick_family_counts(picks),
                "top_pick_optimization_counts": self._pick_optimization_counts(picks),
                "daily_optimization_mode": self._daily_optimization_mode(),
                "markets_70_plus": markets_70_plus,
                "markets_65_plus": markets_65_plus,
                "fixture_count": scored_count,
                "bankers": algo_run.bankers,
                "value_gems": algo_run.value_gems,
                "wild_cards": algo_run.wild_cards,
                "bankroll": bankroll,
                "storage": {
                    "fixtures": "algo_algofixture",
                    "markets": "algo_marketprediction",
                    "picks": "algo_pick",
                },
            }
        except Exception as exc:
            algo_run.status = AlgoRun.Status.FAILED
            algo_run.error = str(exc)
        finally:
            algo_run.finished_at = timezone.now()
            algo_run.save()

        return algo_run

    def _slim_result_payload(self, result):
        slim = dict(result or {})
        slim.pop("fixture_summaries", None)
        slim.pop("selected_picks", None)
        slim.pop("settled_picks", None)
        slim.pop("settled_internal_predictions", None)
        slim["storage"] = {
            "fixtures": "algo_algofixture",
            "markets": "algo_marketprediction",
            "picks": "algo_pick",
        }
        return slim

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

    def _performance_window_days(self):
        raw = self._runner_env().get("ALGO_PERFORMANCE_PROFILE_DAYS", 90)
        try:
            return max(7, min(int(raw), 365))
        except (TypeError, ValueError):
            return 90

    def _performance_max_records(self):
        raw = self._runner_env().get("ALGO_PERFORMANCE_MAX_RECORDS", 20000)
        try:
            return max(1000, min(int(raw), 100000))
        except (TypeError, ValueError):
            return 20000

    def _performance_profile(self):
        since = timezone.localdate() - timedelta(days=self._performance_window_days())
        max_records = self._performance_max_records()
        predictions = (
            MarketPrediction.objects.filter(
                match_date__gte=since,
                status__in=[MarketPrediction.Status.WIN, MarketPrediction.Status.LOSS],
            )
            .only(
                "id",
                "run_id",
                "match_date",
                "match_id",
                "fixture",
                "market",
                "league",
                "status",
                "confidence",
                "published",
                "pnl_simulated",
                "created_at",
                "run__target_date",
            )
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")[:max_records]
        )
        if predictions.exists():
            return self._performance_profile_from_predictions(predictions, since, max_records)

        picks = (
            Pick.objects.filter(status__in=[Pick.Status.WIN, Pick.Status.LOSS])
            .filter(Q(match_date__gte=since) | Q(match_date__isnull=True, run__target_date__gte=since))
            .only(
                "id",
                "run_id",
                "match_date",
                "match_id",
                "fixture",
                "market",
                "league",
                "status",
                "confidence",
                "stake",
                "pnl",
                "created_at",
                "run__target_date",
            )
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")[:max_records]
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

    def _strategy_profile(self, target_date, performance=None):
        performance = performance or self._performance_profile()
        market_actions = {}
        league_market_actions = {}
        confidence_band_actions = {}
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

        for band, stats in (performance.get("confidence_bands") or {}).items():
            action = self._strategy_action_for_stats(stats)
            if action:
                confidence_band_actions[band] = {"action": action, **stats}

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
        if confidence_band_actions:
            reasons.append(f"Confidence calibration watch: {', '.join(sorted(confidence_band_actions)[:6])}.")
        reason = " ".join(reasons) or "No major market restrictions; using adaptive market memory."

        profile = {
            "date": target_date.isoformat(),
            "markets": market_actions,
            "league_markets": league_market_actions,
            "confidence_bands": confidence_band_actions,
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

    def _performance_profile_from_predictions(self, predictions, since, max_records):
        latest = {}
        for prediction in predictions.iterator(chunk_size=1000):
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
            .filter(Q(match_date__gte=since) | Q(match_date__isnull=True, run__target_date__gte=since))
            .only(
                "id",
                "run_id",
                "match_date",
                "match_id",
                "fixture",
                "market",
                "league",
                "status",
                "confidence",
                "stake",
                "pnl",
                "created_at",
                "run__target_date",
            )
            .select_related("run")
            .order_by("-match_date", "-run__target_date", "-created_at", "-id")[:max_records]
        )
        for pick in older_picks.iterator(chunk_size=1000):
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
        confidence_band_stats = defaultdict(self._empty_market_stats)

        for record in latest.values():
            keys = [
                record.market,
                f"{record.league}::{record.market}",
                self._confidence_band(record.confidence),
            ]
            stat_groups = [
                market_stats[keys[0]],
                league_market_stats[keys[1]],
                confidence_band_stats[keys[2]],
            ]
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
            "confidence_bands": self._finalize_market_stats(confidence_band_stats),
        }

    def _performance_profile_from_picks(self, picks):
        latest = {}
        for pick in picks.iterator(chunk_size=1000):
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
        confidence_band_stats = defaultdict(self._empty_market_stats)

        for pick in latest.values():
            keys = [
                pick.market,
                f"{pick.league}::{pick.market}",
                self._confidence_band(pick.confidence),
            ]
            stat_groups = [
                market_stats[keys[0]],
                league_market_stats[keys[1]],
                confidence_band_stats[keys[2]],
            ]
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
            "confidence_bands": self._finalize_market_stats(confidence_band_stats),
        }





    # Markets that _check_market can resolve from a finished fixture. Kept next to the
    # resolver so the two stay in step; anything outside this set is reported as
    # unsettleable rather than silently treated as a void (push).











    def run(self, algo_run: AlgoRun) -> AlgoRun:
        algo_run.status = AlgoRun.Status.RUNNING
        algo_run.started_at = timezone.now()
        algo_run.save(update_fields=["status", "started_at", "updated_at"])

        performance_profile = self._performance_profile()
        strategy_profile = self._strategy_profile(algo_run.target_date, performance_profile)
        env = self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
            "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
        })
        try:
            with temporary_env(env):
                from betpreneur.modules.catalog.api import run_daily_algo

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
            if result.get("fixture_summaries"):
                result["fixture_summaries"] = [
                    self._enrich_fixture_statpal_diagnostics(fixture)
                    for fixture in result.get("fixture_summaries") or []
                ]
                aggregate = {}
                for fixture in result["fixture_summaries"]:
                    self._merge_market_family_statpal_coverage(
                        aggregate,
                        (fixture.get("insights") or {}).get("statpal_market_family_coverage") or {},
                    )
                result["statpal_market_family_coverage"] = self._finalize_market_family_statpal_coverage(aggregate)
            self._persist_selected_picks(algo_run, result)
            self._persist_fixtures(algo_run, result)
            self._persist_market_predictions(algo_run, result)
            slim_result = self._slim_result_payload(result)
            result.clear()
            result.update(slim_result)
            gc.collect()
            algo_run.result = result
        except Exception as exc:
            algo_run.status = AlgoRun.Status.FAILED
            algo_run.error = str(exc)
        finally:
            algo_run.finished_at = timezone.now()
            algo_run.save()

        return algo_run




algo_runner_service = AlgoRunnerService()
