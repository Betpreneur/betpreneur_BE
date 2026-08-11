import os
import json
import gc
import logging
import math
import re
import time
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from difflib import SequenceMatcher
from decimal import Decimal

import requests
from django.conf import settings
from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .models import (
    AlgoFixture,
    AlgoRun,
    BookmakerLeagueMap,
    FixtureCache,
    MarketPrediction,
    Pick,
    ProviderFixtureMap,
    SlipSelection,
    StrategyReview,
    TeamAliasMap,
)
from .recommendation_policy import (
    assess_calibration_trust,
    assess_league_market_trust,
    assess_recommendation,
)
from .council import CAUTION, REJECT, council_review
from .market_taxonomy import describe_market
from .normalize.bridge import descriptor_from_canonical
from .normalize.canonical import Resolution as MarketResolution
from .normalize.sportybet import resolve as resolve_sportybet_market


log = logging.getLogger(__name__)


class BookmakerImportError(ValueError):
    pass


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


def normalize_fixture_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def parse_match_query(value):
    text = str(value or "").strip()
    normalized = normalize_fixture_text(text)
    parts = re.split(r"\s+(?:vs|v|versus)\s+|\s+-\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        home = normalize_fixture_text(parts[0])
        away = normalize_fixture_text(parts[1])
        if home and away:
            return home, away, normalized
    return "", "", normalized


def json_safe(value):
    return json.loads(json.dumps(value, default=str))


TEAM_NAME_ALIASES = {
    "turun palloseura": {"tps"},
    "tps": {"turun", "palloseura"},
    "ekenas idrottsforening": {"eif", "ekenas"},
    "eif": {"ekenas", "idrottsforening"},
}
_team_alias_cache = {}


def _mapped_team_alias_tokens(normalized):
    if not normalized:
        return set()
    if normalized in _team_alias_cache:
        return _team_alias_cache[normalized]
    try:
        rows = TeamAliasMap.objects.filter(active=True).filter(
            Q(alias_normalized=normalized) | Q(canonical_normalized=normalized)
        )[:20]
        tokens = set()
        for row in rows:
            tokens.update(token for token in normalize_fixture_text(row.alias).split() if len(token) > 1)
            tokens.update(token for token in normalize_fixture_text(row.canonical_name).split() if len(token) > 1)
    except Exception:
        tokens = set()
    _team_alias_cache[normalized] = tokens
    return tokens


def _team_acronyms(value):
    normalized = normalize_fixture_text(value)
    tokens = [token for token in normalized.split() if len(token) > 1]
    if len(tokens) < 2:
        return set()
    acronyms = {"".join(token[0] for token in tokens)}
    meaningful_tokens = [token for token in tokens if token not in {"fc", "cf", "sc", "afc", "club", "the"}]
    if len(meaningful_tokens) >= 2:
        acronyms.add("".join(token[0] for token in meaningful_tokens))
    return {item for item in acronyms if len(item) >= 2}


def _team_tokens(value):
    stopwords = {"fc", "cf", "sc", "afc", "bk", "club", "the"}
    normalized = normalize_fixture_text(value)
    tokens = [token for token in normalized.split() if len(token) > 2]
    meaningful = [token for token in tokens if token not in stopwords]
    result = set(meaningful or tokens)
    result.update(TEAM_NAME_ALIASES.get(normalized, set()))
    result.update(_mapped_team_alias_tokens(normalized))
    return result


def _acronym_match_score(query, fixture_name):
    query_acronyms = _team_acronyms(query)
    fixture_acronyms = _team_acronyms(fixture_name)
    query_tokens = _team_tokens(query)
    fixture_tokens = _team_tokens(fixture_name)
    for acronym in query_acronyms:
        if acronym in fixture_tokens or any(token.startswith(acronym) for token in fixture_tokens):
            return 0.94
    for acronym in fixture_acronyms:
        if acronym in query_tokens or any(token.startswith(acronym) for token in query_tokens):
            return 0.94
    return 0.0


def _soft_token_coverage(query_tokens, fixture_tokens):
    if not query_tokens or not fixture_tokens:
        return 0.0
    matched = 0
    for query_token in query_tokens:
        best = max(
            SequenceMatcher(None, query_token, fixture_token).ratio()
            for fixture_token in fixture_tokens
        )
        if best >= 0.78:
            matched += 1
    return matched / max(min(len(query_tokens), len(fixture_tokens)), 1)


def _token_side_score(query, fixture_name):
    query_tokens = _team_tokens(query)
    fixture_tokens = _team_tokens(fixture_name)
    if not query_tokens or not fixture_tokens:
        return 0.0
    overlap = query_tokens & fixture_tokens
    if not overlap:
        acronym_score = _acronym_match_score(query, fixture_name)
        soft_coverage = _soft_token_coverage(query_tokens, fixture_tokens)
        if soft_coverage:
            return max(acronym_score, min(0.96, 0.70 + (soft_coverage * 0.26)))
        return acronym_score
    coverage = max(
        len(overlap) / max(len(query_tokens), 1),
        len(overlap) / max(len(fixture_tokens), 1),
    )
    return min(1.0, 0.72 + (coverage * 0.28))


class FixtureSearchService:
    DEFAULT_DAYS = 3
    MAX_DAYS = 14
    PROVIDER_LEAGUE_IDS = {
        "world cup": 1,
        "fifa world cup": 1,
        "uefa champions league": 2,
        "uefa europa league": 3,
        "uefa europa conference league": 848,
        "club friendly games": 667,
        "friendlies clubs": 667,
        "chinese super league": 169,
        "super league": 169,
    }

    def __init__(self, runner_service=None):
        self.runner_service = runner_service or AlgoRunnerService()

    def sync_statpal_universe(self, *, start_date=None, days=None):
        """
        Refresh the whole fixture universe in one call.

        StatPal's daily endpoint ignores a date parameter and always returns the same
        rolling window — roughly 1,200 fixtures across 260+ competitions. Asking it once
        therefore covers every date we care about, and carries StatPal's own team ids,
        which are what the corner and card rate profiles need to resolve.
        """
        from .statpal import StatPalConfigurationError, StatPalError
        from .statpal_provider import StatPalDailyMatchProvider, normalize_daily_matches

        start_date = start_date or timezone.localdate()
        days = min(int(days or self.DEFAULT_DAYS), self.MAX_DAYS)
        horizon = start_date + timedelta(days=days)

        try:
            payload = StatPalDailyMatchProvider().client.soccer_daily_matches()
        except (StatPalConfigurationError, StatPalError) as exc:
            return {"synced": 0, "errors": [{"provider": "statpal", "error": str(exc)}]}
        except Exception as exc:
            return {"synced": 0, "errors": [{"provider": "statpal", "error": str(exc)}]}

        grouped = defaultdict(list)
        for fixture in normalize_daily_matches(payload, target_date=start_date):
            match_date = fixture.get("date") or start_date
            if start_date <= match_date <= horizon:
                grouped[match_date].append(fixture)

        synced = 0
        for match_date, group in grouped.items():
            synced += self._upsert_fixtures(group, match_date)
        return {
            "synced": synced,
            "errors": [],
            "source": "statpal",
            "dates": sorted(item.isoformat() for item in grouped),
        }

    def sync_statpal_horizon(self, *, start_date=None, days=3, league_ids=None):
        """
        Cache every fixture in every league across the Match Checker's horizon.

        The daily endpoint only really covers today, so a slip booked for the weekend
        would find no fixture to resolve against. A league's own match list runs months
        ahead, so one call per league covers the whole window.

        Cost is one request per league — around a thousand — which is a couple of
        percent of the daily quota, so this runs on a schedule rather than per review.
        """
        from .statpal import StatPalClient, StatPalConfigurationError, StatPalError
        from .statpal_provider import normalize_daily_matches, normalize_leagues

        start_date = start_date or timezone.localdate()
        horizon = start_date + timedelta(days=max(0, int(days)))
        client = StatPalClient()

        if league_ids is None:
            try:
                payload = client.soccer_leagues()
            except (StatPalConfigurationError, StatPalError) as exc:
                return {"synced": 0, "leagues": 0, "errors": [{"provider": "statpal", "error": str(exc)}]}
            league_ids = [league["provider_league_id"] for league in normalize_leagues(payload)]

        grouped = defaultdict(list)
        calls = failures = 0
        errors = []
        for league_id in league_ids:
            try:
                payload = client.soccer_league_matches(league_id)
                calls += 1
            except Exception as exc:
                failures += 1
                if len(errors) < 20:
                    errors.append({"league_id": league_id, "error": str(exc)[:200]})
                continue
            for fixture in normalize_daily_matches(payload, target_date=start_date):
                match_date = fixture.get("date")
                if match_date and start_date <= match_date <= horizon:
                    grouped[match_date].append(fixture)

        synced = 0
        for match_date, group in grouped.items():
            synced += self._upsert_fixtures(group, match_date)
        return {
            "synced": synced,
            "leagues": len(league_ids),
            "api_calls": calls,
            "failed_leagues": failures,
            "dates": sorted(item.isoformat() for item in grouped),
            "errors": errors,
        }

    def sync_upcoming(self, *, start_date=None, days=None, unrestricted=False):
        """
        Refresh the fixture universe, preferring StatPal's single-call window.

        The previous behaviour looped one API-Football request per date and ran on every
        leg of a slip, costing roughly 85 seconds per pass. API-Football is now the
        fallback for when StatPal returns nothing.
        """
        start_date = start_date or timezone.localdate()
        days = min(int(days or self.DEFAULT_DAYS), self.MAX_DAYS)

        statpal_result = self.sync_statpal_universe(start_date=start_date, days=days)
        if statpal_result.get("synced"):
            return statpal_result

        total = 0
        errors = list(statpal_result.get("errors") or [])

        from .grindalgo import algo_runner

        extra_env = {}
        if unrestricted:
            extra_env = {
                "APS_TRACK_ALL_LEAGUES": "True",
                "APS_MAX_FIXTURES": "0",
            }
        with temporary_env(self.runner_service._runner_env(extra_env)):
            for offset in range(days + 1):
                target_date = start_date + timedelta(days=offset)
                try:
                    fixtures = algo_runner.fetch_aps_fixtures(target_date.isoformat())
                except Exception as exc:
                    errors.append({"date": target_date.isoformat(), "error": str(exc)})
                    continue
                total += self._upsert_fixtures(fixtures, target_date)
                statpal_result = self.sync_statpal_daily(target_date=target_date)
                total += statpal_result.get("synced", 0)
                errors.extend(statpal_result.get("errors", []))
        return {"synced": total, "errors": errors}

    def sync_statpal_daily(self, *, target_date):
        from .statpal import StatPalConfigurationError, StatPalError
        from .statpal_provider import StatPalDailyMatchProvider

        try:
            fixtures = StatPalDailyMatchProvider().fixtures_for_date(target_date)
        except StatPalConfigurationError:
            return {"synced": 0, "errors": []}
        except StatPalError as exc:
            return {"synced": 0, "errors": [{"date": target_date.isoformat(), "provider": "statpal", "error": str(exc)}]}
        except Exception as exc:
            return {"synced": 0, "errors": [{"date": target_date.isoformat(), "provider": "statpal", "error": str(exc)}]}
        return {"synced": self._upsert_fixtures(fixtures, target_date), "errors": []}

    def _attach_statpal_fixture_context(self, fixtures, target_date):
        self.sync_statpal_daily(target_date=target_date)
        statpal_rows = list(
            FixtureCache.objects.filter(match_date=target_date, source="statpal")
            .only("match_id", "home_team_normalized", "away_team_normalized", "api_payload")
        )
        if not statpal_rows:
            return fixtures
        by_pair = {
            (row.home_team_normalized, row.away_team_normalized): row
            for row in statpal_rows
            if row.home_team_normalized and row.away_team_normalized
        }
        enriched = []
        for fixture in fixtures:
            item = dict(fixture)
            key = (
                normalize_fixture_text(item.get("hname") or item.get("home_team")),
                normalize_fixture_text(item.get("aname") or item.get("away_team")),
            )
            row = by_pair.get(key)
            if row:
                payload = row.api_payload or {}
                home_team_id = payload.get("provider_home_team_id") or payload.get("hid") or ""
                away_team_id = payload.get("provider_away_team_id") or payload.get("aid") or ""
                item["statpal_match_id"] = row.match_id
                item["statpal_provider_match_id"] = payload.get("provider_match_id") or row.match_id.replace("statpal:", "", 1)
                item["statpal_provider_competition_id"] = payload.get("provider_competition_id") or payload.get("code") or ""
                item["statpal_home_team_id"] = str(home_team_id or "")
                item["statpal_away_team_id"] = str(away_team_id or "")
                item["statpal_payload"] = payload
            enriched.append(item)
        return enriched

    def _upsert_fixtures(self, fixtures, target_date):
        count = 0
        for item in fixtures:
            match_id = str(item.get("match_id") or item.get("aps_id") or "").strip()
            if not match_id:
                continue
            home_team = item.get("hname") or item.get("home_team") or ""
            away_team = item.get("aname") or item.get("away_team") or ""
            fixture = item.get("fixture") or f"{home_team} vs {away_team}".strip()
            kickoff_utc = parse_datetime(str(item.get("kickoff_utc") or "")) if item.get("kickoff_utc") else None
            home_norm = normalize_fixture_text(home_team)
            away_norm = normalize_fixture_text(away_team)
            # Keep the provider's own identifiers on the cached row. They are the join
            # key for the fitted goal models and the team rate profiles, and storing only
            # the raw provider payload loses them — leaving a fixture that resolves
            # perfectly but cannot be priced.
            payload = json_safe(item.get("api_payload") or item)
            if isinstance(payload, dict):
                league_id = (
                    item.get("provider_competition_id")
                    or item.get("code")
                    or ((payload.get("_league") or {}).get("id") if isinstance(payload.get("_league"), dict) else "")
                )
                home_id = item.get("hid") or (payload.get("home") or {}).get("id")
                away_id = item.get("aid") or (payload.get("away") or {}).get("id")
                payload["provider_competition_id"] = str(league_id or "")
                payload["provider_match_id"] = str(
                    item.get("provider_match_id") or payload.get("main_id") or ""
                )
                payload["provider_home_team_id"] = str(home_id or "")
                payload["provider_away_team_id"] = str(away_id or "")
            FixtureCache.objects.update_or_create(
                match_id=match_id,
                defaults={
                    "match_date": item.get("date") or target_date,
                    "fixture": fixture,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_team_normalized": home_norm,
                    "away_team_normalized": away_norm,
                    "fixture_normalized": normalize_fixture_text(f"{home_team} vs {away_team}"),
                    "home_logo": item.get("home_logo") or "",
                    "away_logo": item.get("away_logo") or "",
                    "league": item.get("league") or "",
                    "league_logo": item.get("league_logo") or "",
                    "country": item.get("country") or "",
                    "country_flag": item.get("country_flag") or "",
                    "round": item.get("round") or "",
                    "league_type": item.get("league_type") or "",
                    "kickoff": item.get("kickoff") or "",
                    "kickoff_utc": kickoff_utc,
                    "api_payload": payload,
                    "source": item.get("source") or "api_football",
                },
            )
            count += 1
        return count

    def search(self, query, *, start_date=None, days=None, limit=10, refresh=False, unrestricted=False):
        start_date = start_date or timezone.localdate()
        days = min(int(days or self.DEFAULT_DAYS), self.MAX_DAYS)
        limit = max(1, min(int(limit or 10), 25))
        refreshed = False
        refresh_errors = []
        candidates = self._search_cached(query, start_date=start_date, days=days, limit=limit)
        if refresh or not candidates:
            refresh_result = self.sync_upcoming(start_date=start_date, days=days, unrestricted=unrestricted)
            refresh_errors = refresh_result.get("errors", [])
            refreshed = True
            candidates = self._search_cached(query, start_date=start_date, days=days, limit=limit)
        return {"refreshed": refreshed, "refresh_errors": refresh_errors, "results": candidates}

    def search_provider_fixture(
        self,
        query,
        *,
        provider_date,
        competition="",
        provider="",
        provider_competition_id="",
        limit=10,
    ):
        if not provider_date:
            return {"refreshed": False, "refresh_errors": [], "results": [], "trace": []}

        start_date = max(provider_date - timedelta(days=1), timezone.localdate())
        days = 2
        errors = []
        attempts = []
        total = 0
        provider_match_ids = []
        from .grindalgo import algo_runner

        with temporary_env(self.runner_service._runner_env({"APS_TRACK_ALL_LEAGUES": "True", "APS_MAX_FIXTURES": "0"})):
            for offset in range(days + 1):
                target_date = start_date + timedelta(days=offset)
                params_list = [{"date": target_date.isoformat(), "timezone": "Africa/Lagos"}]
                league_id = self._provider_league_id(
                    competition,
                    provider=provider,
                    provider_competition_id=provider_competition_id,
                )
                if league_id:
                    params_list.insert(
                        0,
                        {
                            "date": target_date.isoformat(),
                            "league": league_id,
                            "season": provider_date.year,
                            "timezone": "Africa/Lagos",
                        },
                    )
                for params in params_list:
                    try:
                        fixtures = algo_runner.aps_get("/fixtures", params)
                    except Exception as exc:
                        errors.append({"date": target_date.isoformat(), "params": params, "error": str(exc)})
                        attempts.append(
                            {
                                "strategy": "api_provider_lookup",
                                "parameters": params,
                                "error": str(exc),
                                "api_result_count": 0,
                                "cached_count": 0,
                            }
                        )
                        continue
                    cached_count = self._upsert_api_fixtures(fixtures, target_date)
                    total += cached_count
                    provider_match_ids.extend(
                        str((fixture.get("fixture") or {}).get("id"))
                        for fixture in (fixtures or [])
                        if (fixture.get("fixture") or {}).get("id")
                    )
                    attempts.append(
                        {
                            "strategy": "api_provider_lookup",
                            "parameters": params,
                            "api_result_count": len(fixtures or []),
                            "cached_count": cached_count,
                            "fixture_ids": [
                                ((fixture.get("fixture") or {}).get("id"))
                                for fixture in (fixtures or [])[:25]
                            ],
                            "fixtures": [
                                {
                                    "id": ((fixture.get("fixture") or {}).get("id")),
                                    "home": (((fixture.get("teams") or {}).get("home") or {}).get("name")),
                                    "away": (((fixture.get("teams") or {}).get("away") or {}).get("name")),
                                    "league": ((fixture.get("league") or {}).get("name")),
                                }
                                for fixture in (fixtures or [])[:10]
                            ],
                        }
                    )

        candidates = self._search_cached(
            query,
            start_date=start_date,
            days=days,
            limit=limit,
            use_token_filter=False,
            match_ids=provider_match_ids,
        )
        attempts.append(
            {
                "strategy": "cache_after_provider_lookup",
                "start_date": start_date.isoformat(),
                "days": days,
                "candidate_count": len(candidates),
                "candidate_match_ids": [candidate.get("match_id") for candidate in candidates],
            }
        )
        return {
            "refreshed": True,
            "refresh_errors": errors,
            "synced": total,
            "results": candidates,
            "trace": attempts,
        }

    def get_provider_fixture(self, *, provider="", provider_event_id=""):
        provider = str(provider or "").strip()
        provider_event_id = str(provider_event_id or "").strip()
        if not provider or not provider_event_id:
            return None
        mapping = (
            ProviderFixtureMap.objects.filter(
                provider=provider,
                provider_event_id=provider_event_id,
                active=True,
            )
            .order_by("-verified_at", "-updated_at")
            .first()
        )
        if not mapping:
            return None
        cached = FixtureCache.objects.filter(match_id=str(mapping.api_fixture_id)).first()
        if not cached:
            self.fetch_fixture_by_id(mapping.api_fixture_id)
            cached = FixtureCache.objects.filter(match_id=str(mapping.api_fixture_id)).first()
        if not cached:
            return None
        return {
            "mapping_id": mapping.id,
            "mapping_confidence": float(mapping.confidence or 0),
            "fixture": self._serialize_fixture(cached, float(mapping.confidence or 100), "direct"),
        }

    def find_statpal_fixture_context(self, candidate, *, minimum_score=70):
        candidate = candidate or {}
        match_date = candidate.get("match_date")
        if not match_date:
            return None
        home_query = normalize_fixture_text(candidate.get("home_team") or candidate.get("hname") or "")
        away_query = normalize_fixture_text(candidate.get("away_team") or candidate.get("aname") or "")
        normalized_query = normalize_fixture_text(
            candidate.get("fixture")
            or " vs ".join(
                item
                for item in [
                    candidate.get("home_team") or candidate.get("hname"),
                    candidate.get("away_team") or candidate.get("aname"),
                ]
                if item
            )
        )
        if not normalized_query:
            return None

        best = None
        queryset = FixtureCache.objects.filter(match_date=match_date, source="statpal")
        for fixture in queryset[:2000]:
            if home_query and away_query and not self._has_team_token_overlap(fixture, home_query, away_query):
                continue
            score, orientation = self._match_score_and_orientation(fixture, home_query, away_query, normalized_query)
            if score >= minimum_score and (best is None or score > best[0]):
                best = (score, orientation, fixture)
        if not best:
            return None
        score, orientation, fixture = best
        return self._serialize_fixture(fixture, score, orientation)

    def fetch_fixture_by_id(self, fixture_id):
        fixture_id = str(fixture_id or "").strip()
        if not fixture_id:
            return {"synced": 0, "errors": ["fixture_id_required"]}
        from .grindalgo import algo_runner

        errors = []
        with temporary_env(self.runner_service._runner_env({"APS_TRACK_ALL_LEAGUES": "True", "APS_MAX_FIXTURES": "0"})):
            try:
                fixtures = algo_runner.aps_get("/fixtures", {"id": fixture_id, "timezone": "Africa/Lagos"})
            except Exception as exc:
                errors.append(str(exc))
                fixtures = []
        synced = 0
        for item in fixtures or []:
            fixture = item.get("fixture") or {}
            fixture_date = parse_datetime(str(fixture.get("date") or ""))
            target_date = fixture_date.date() if fixture_date else timezone.localdate()
            synced += self._upsert_api_fixtures([item], target_date)
        return {"synced": synced, "errors": errors}

    def learn_resolution(self, *, provider_metadata, candidate, confidence=None, method="team_date_league"):
        provider = str((provider_metadata or {}).get("provider") or "").strip()
        event_id = str((provider_metadata or {}).get("provider_event_id") or "").strip()
        if not provider or not event_id or not candidate:
            return
        score = float(confidence if confidence is not None else candidate.get("match_score") or 0)
        if score < 90:
            return
        now = timezone.now()
        provider_competition_id = str((provider_metadata or {}).get("provider_competition_id") or "")
        provider_competition_name = str((provider_metadata or {}).get("competition") or "")
        api_league_id = self._int_or_none(candidate.get("league_id"))
        api_home_id = self._int_or_none(candidate.get("home_team_id"))
        api_away_id = self._int_or_none(candidate.get("away_team_id"))
        ProviderFixtureMap.objects.update_or_create(
            provider=provider,
            provider_event_id=event_id,
            defaults={
                "provider_competition_id": provider_competition_id,
                "provider_competition_name": provider_competition_name,
                "api_fixture_id": str(candidate.get("match_id") or ""),
                "api_league_id": api_league_id,
                "api_league_name": str(candidate.get("league") or ""),
                "provider_home_team": str((provider_metadata or {}).get("home_team") or ""),
                "provider_away_team": str((provider_metadata or {}).get("away_team") or ""),
                "api_home_team": str(candidate.get("home_team") or ""),
                "api_away_team": str(candidate.get("away_team") or ""),
                "kickoff_at": candidate.get("kickoff_utc") or None,
                "confidence": score,
                "resolution_method": method,
                "active": True,
                "payload": json_safe({"provider": provider_metadata, "candidate": candidate}),
                "verified_at": now,
            },
        )
        self._learn_league_map(
            provider=provider,
            provider_competition_id=provider_competition_id,
            provider_competition_name=provider_competition_name,
            candidate=candidate,
            confidence=score,
            now=now,
        )
        self._learn_team_alias(
            provider=provider,
            alias=(provider_metadata or {}).get("home_team"),
            canonical=candidate.get("home_team"),
            api_team_id=api_home_id,
            country=candidate.get("country"),
            confidence=score,
            now=now,
        )
        self._learn_team_alias(
            provider=provider,
            alias=(provider_metadata or {}).get("away_team"),
            canonical=candidate.get("away_team"),
            api_team_id=api_away_id,
            country=candidate.get("country"),
            confidence=score,
            now=now,
        )

    def _provider_league_id(self, competition, *, provider="", provider_competition_id=""):
        mapped = self._mapped_provider_league_id(
            provider=provider,
            provider_competition_id=provider_competition_id,
            competition=competition,
        )
        if mapped:
            return mapped
        normalized = normalize_fixture_text(competition)
        if not normalized:
            return None
        if normalized in self.PROVIDER_LEAGUE_IDS:
            return self.PROVIDER_LEAGUE_IDS[normalized]
        for name, league_id in self.PROVIDER_LEAGUE_IDS.items():
            if name in normalized or normalized in name:
                return league_id
        return None

    def _mapped_provider_league_id(self, *, provider="", provider_competition_id="", competition=""):
        normalized = normalize_fixture_text(competition)
        query = BookmakerLeagueMap.objects.filter(active=True)
        if provider:
            query = query.filter(provider=provider)
        mapped = None
        if provider_competition_id:
            mapped = query.filter(provider_competition_id=str(provider_competition_id)).order_by("-confidence").first()
        if not mapped and normalized:
            mapped = query.filter(provider_competition_normalized=normalized).order_by("-confidence").first()
        return mapped.api_league_id if mapped else None

    def _learn_league_map(self, *, provider, provider_competition_id, provider_competition_name, candidate, confidence, now):
        api_league_id = self._int_or_none(candidate.get("league_id"))
        if not provider or not provider_competition_name or not api_league_id:
            return
        BookmakerLeagueMap.objects.update_or_create(
            provider=provider,
            provider_competition_id=provider_competition_id or "",
            provider_competition_normalized=normalize_fixture_text(provider_competition_name),
            defaults={
                "provider_competition_name": provider_competition_name,
                "api_league_id": api_league_id,
                "api_league_name": str(candidate.get("league") or ""),
                "country": str(candidate.get("country") or ""),
                "current_api_season": self._int_or_none(candidate.get("season")),
                "confidence": confidence,
                "active": True,
                "source": "auto",
                "last_verified_at": now,
            },
        )

    def _learn_team_alias(self, *, provider, alias, canonical, api_team_id=None, country="", confidence=100, now=None):
        alias = str(alias or "").strip()
        canonical = str(canonical or "").strip()
        if not alias or not canonical:
            return
        alias_normalized = normalize_fixture_text(alias)
        canonical_normalized = normalize_fixture_text(canonical)
        if not alias_normalized or not canonical_normalized or alias_normalized == canonical_normalized:
            return
        TeamAliasMap.objects.update_or_create(
            provider=provider or "",
            alias_normalized=alias_normalized,
            canonical_normalized=canonical_normalized,
            defaults={
                "alias": alias,
                "canonical_name": canonical,
                "api_team_id": api_team_id,
                "country": str(country or ""),
                "confidence": confidence,
                "active": True,
                "source": "auto",
                "last_seen_at": now or timezone.now(),
            },
        )

    def _int_or_none(self, value):
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _upsert_api_fixtures(self, fixtures, target_date):
        items = []
        for item in fixtures or []:
            fixture = item.get("fixture") or {}
            teams = item.get("teams") or {}
            league = item.get("league") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            match_id = fixture.get("id")
            if not match_id or not home.get("name") or not away.get("name"):
                continue
            status = (fixture.get("status") or {}).get("short", "")
            if status in ("FT", "AET", "PEN", "CANC", "ABD"):
                continue
            items.append(
                {
                    "fixture": f"{home.get('name')} vs {away.get('name')}",
                    "hname": home.get("name") or "",
                    "aname": away.get("name") or "",
                    "home_logo": home.get("logo") or "",
                    "away_logo": away.get("logo") or "",
                    "hid": home.get("id"),
                    "aid": away.get("id"),
                    "league": league.get("name") or "",
                    "league_logo": league.get("logo") or "",
                    "country": league.get("country") or "",
                    "country_flag": league.get("flag") or "",
                    "round": league.get("round") or "",
                    "league_type": league.get("type") or "",
                    "code": str(league.get("id") or ""),
                    "kickoff": "",
                    "kickoff_utc": fixture.get("date") or "",
                    "match_id": match_id,
                    "source": "aps_provider_lookup",
                    "aps_id": match_id,
                    "date": target_date.isoformat(),
                    "season": league.get("season"),
                }
            )
        return self._upsert_fixtures(items, target_date)

    def _search_cached(self, query, *, start_date, days, limit, use_token_filter=True, match_ids=None):
        home_query, away_query, normalized_query = parse_match_query(query)
        end_date = start_date + timedelta(days=days)
        queryset = FixtureCache.objects.filter(match_date__range=(start_date, end_date))
        if match_ids:
            queryset = queryset.filter(match_id__in=[str(item) for item in match_ids])
        if normalized_query and use_token_filter:
            tokens = [token for token in normalized_query.split() if len(token) > 1]
            token_filter = Q()
            for token in tokens[:6]:
                token_filter |= Q(fixture_normalized__icontains=token)
                token_filter |= Q(home_team_normalized__icontains=token)
                token_filter |= Q(away_team_normalized__icontains=token)
            if token_filter:
                queryset = queryset.filter(token_filter)

        scored = []
        max_candidates = 2000 if not use_token_filter or match_ids else 500
        for fixture in queryset[:max_candidates]:
            if home_query and away_query and not self._has_team_token_overlap(fixture, home_query, away_query):
                continue
            score, orientation = self._match_score_and_orientation(fixture, home_query, away_query, normalized_query)
            if score >= 35:
                scored.append((score, orientation, fixture))
        # The same fixture is often cached from more than one provider. When they match
        # equally well, prefer the row that carries the identifiers our models join on —
        # otherwise resolution succeeds and pricing then fails, because the winning row
        # has no league or team id to look a fitted model up by.
        scored.sort(
            key=lambda item: (item[0], self._pricing_rank(item[2]), item[2].match_date),
            reverse=True,
        )
        return [self._serialize_fixture(fixture, score, orientation) for score, orientation, fixture in scored[:limit]]

    @staticmethod
    def _pricing_rank(fixture) -> int:
        payload = fixture.api_payload or {}
        return 1 if payload.get("provider_competition_id") else 0

    def _has_team_token_overlap(self, fixture, home_query, away_query):
        if not _team_tokens(home_query) or not _team_tokens(away_query):
            return True
        direct = (
            _token_side_score(home_query, fixture.home_team_normalized or fixture.home_team) >= 0.70
            and _token_side_score(away_query, fixture.away_team_normalized or fixture.away_team) >= 0.70
        )
        reversed_match = (
            _token_side_score(home_query, fixture.away_team_normalized or fixture.away_team) >= 0.70
            and _token_side_score(away_query, fixture.home_team_normalized or fixture.home_team) >= 0.70
        )
        return direct or reversed_match

    def _match_score(self, fixture, home_query, away_query, normalized_query):
        score, _orientation = self._match_score_and_orientation(fixture, home_query, away_query, normalized_query)
        return score

    def _match_score_and_orientation(self, fixture, home_query, away_query, normalized_query):
        fixture_norm = fixture.fixture_normalized or normalize_fixture_text(fixture.fixture)
        if home_query and away_query:
            direct_fuzzy = (
                SequenceMatcher(None, home_query, fixture.home_team_normalized).ratio()
                + SequenceMatcher(None, away_query, fixture.away_team_normalized).ratio()
            ) / 2
            reversed_fuzzy = (
                SequenceMatcher(None, home_query, fixture.away_team_normalized).ratio()
                + SequenceMatcher(None, away_query, fixture.home_team_normalized).ratio()
            ) / 2
            direct_token = (
                _token_side_score(home_query, fixture.home_team_normalized or fixture.home_team)
                + _token_side_score(away_query, fixture.away_team_normalized or fixture.away_team)
            ) / 2
            reversed_token = (
                _token_side_score(home_query, fixture.away_team_normalized or fixture.away_team)
                + _token_side_score(away_query, fixture.home_team_normalized or fixture.home_team)
            ) / 2
            direct = max(direct_fuzzy, direct_token)
            reversed_match = max(reversed_fuzzy, reversed_token)
            if reversed_match > direct:
                return round(reversed_match * 100, 2), "reversed"
            return round(direct * 100, 2), "direct"
        return round(SequenceMatcher(None, normalized_query, fixture_norm).ratio() * 100, 2), "unknown"

    def _serialize_fixture(self, fixture, score, orientation="unknown"):
        payload = fixture.api_payload or {}
        # The provider's own ids are how a resolved fixture reaches its fitted goal model
        # and its team rate profiles. API-Football rows carry `hid`/`code`; StatPal rows
        # carry the normalised `provider_*` keys, so read both.
        home_team_id = payload.get("provider_home_team_id") or payload.get("hid") or payload.get("home_team_id")
        away_team_id = payload.get("provider_away_team_id") or payload.get("aid") or payload.get("away_team_id")
        league_id = payload.get("provider_competition_id") or payload.get("code") or payload.get("league_id")
        provider_match_id = payload.get("provider_match_id") or payload.get("main_id") or ""
        statpal_home_team_id = payload.get("statpal_home_team_id") or (home_team_id if fixture.source == "statpal" else "")
        statpal_away_team_id = payload.get("statpal_away_team_id") or (away_team_id if fixture.source == "statpal" else "")
        statpal_provider_competition_id = payload.get("statpal_provider_competition_id") or (league_id if fixture.source == "statpal" else "")
        statpal_provider_match_id = payload.get("statpal_provider_match_id") or (provider_match_id if fixture.source == "statpal" else "")
        return {
            "match_id": fixture.match_id,
            "match_date": fixture.match_date,
            "fixture": fixture.fixture,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "statpal_home_team_id": statpal_home_team_id,
            "statpal_away_team_id": statpal_away_team_id,
            "statpal_provider_match_id": statpal_provider_match_id,
            "statpal_provider_competition_id": statpal_provider_competition_id,
            "provider_match_id": provider_match_id,
            "hid": home_team_id,
            "aid": away_team_id,
            "code": league_id,
            "source": fixture.source,
            "home_logo": fixture.home_logo,
            "away_logo": fixture.away_logo,
            "league": fixture.league,
            "league_id": league_id,
            "season": payload.get("season"),
            "league_logo": fixture.league_logo,
            "country": fixture.country,
            "country_flag": fixture.country_flag,
            "round": fixture.round,
            "league_type": fixture.league_type,
            "kickoff": fixture.kickoff,
            "kickoff_utc": fixture.kickoff_utc,
            "match_score": score,
            "match_orientation": orientation,
        }


class SportyBetShareImporter:
    SHARE_ENDPOINT = "https://www.sportybet.com/api/ng/orders/share/{code}"

    def extract_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"shareCode=([A-Za-z0-9_-]+)", text)
        if match:
            return match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{4,32}", text):
            return text
        return ""

    def fetch_share(self, code):
        try:
            return self._fetch_share_http(code)
        except BookmakerImportError as exc:
            log.warning("SportyBet direct import failed for code=%s; trying browser fallback: %s", code, exc)
            return self._fetch_share_with_browser(code)

    def _fetch_share_http(self, code):
        response = requests.get(
            self.SHARE_ENDPOINT.format(code=code),
            params={"_t": int(time.time() * 1000)},
            headers={
                "Accept": "*/*",
                "Accept-Language": "en",
                "Connection": "keep-alive",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"https://www.sportybet.com/ng/?shareCode={code}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "same-origin",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "clientid": "web",
                "operid": "2",
                "platform": "web",
            },
            timeout=20,
        )
        log.info(
            "SportyBet HTTP import response code=%s status=%s content_type=%s bytes=%s",
            code,
            response.status_code,
            response.headers.get("content-type", ""),
            len(response.content or b""),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = response.status_code
            body_preview = (response.text or "")[:250].replace("\n", " ")
            if 400 <= status_code < 500:
                raise BookmakerImportError(
                    f"SportyBet rejected the share-code request with HTTP {status_code}. "
                    f"The provider may be blocking server-side import. Response: {body_preview}"
                ) from exc
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "")
            body_preview = (response.text or "")[:250].replace("\n", " ")
            raise BookmakerImportError(
                "SportyBet did not return JSON for this share code. "
                f"content_type={content_type!r}, status={response.status_code}, response={body_preview!r}"
            ) from exc
        self._log_payload_shape("http", code, payload)
        return payload

    def _fetch_share_with_browser(self, code):
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install project dependencies and Chromium browser runtime to enable SportyBet browser import."
            ) from exc

        timeout_ms = int(os.environ.get("SPORTYBET_IMPORT_TIMEOUT_MS", "30000") or 30000)
        url = f"https://www.sportybet.com/ng/?shareCode={code}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                locale="en-US",
                timezone_id="Africa/Lagos",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1500)
                attempts = page.evaluate(
                    """async (code) => {
                        const paths = [
                            `/orders/share/${code}?_t=${Date.now()}`,
                            `/api/ng/orders/share/${code}?_t=${Date.now()}`,
                            `https://www.sportybet.com/api/ng/orders/share/${code}?_t=${Date.now()}`
                        ];
                        const credentialModes = ["omit", "include"];
                        const results = [];
                        for (const path of paths) {
                            for (const credentials of credentialModes) {
                                try {
                                    const response = await fetch(path, {
                                        method: "GET",
                                        credentials,
                                        headers: {
                                            clientid: "web",
                                            operid: "2",
                                            platform: "web",
                                            Accept: "*/*",
                                            "Accept-Language": "en"
                                        }
                                    });
                                    const text = await response.text();
                                    results.push({
                                        url: path,
                                        credentials,
                                        ok: response.ok,
                                        status: response.status,
                                        contentType: response.headers.get("content-type") || "",
                                        text
                                    });
                                    try {
                                        JSON.parse(text);
                                        return results;
                                    } catch (error) {}
                                } catch (error) {
                                    results.push({
                                        url: path,
                                        credentials,
                                        ok: false,
                                        status: 0,
                                        contentType: "",
                                        text: String(error)
                                    });
                                }
                            }
                        }
                        return results;
                    }""",
                    code,
                )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Timed out while loading SportyBet share page.") from exc
            finally:
                context.close()
                browser.close()

        attempts = attempts or []
        log.info("SportyBet browser import attempts code=%s attempts=%s", code, len(attempts))
        for payload in attempts:
            text = (payload or {}).get("text") or ""
            log.info(
                "SportyBet browser attempt code=%s url=%s credentials=%s status=%s content_type=%s bytes=%s",
                code,
                (payload or {}).get("url", ""),
                (payload or {}).get("credentials", ""),
                (payload or {}).get("status"),
                (payload or {}).get("contentType", ""),
                len(text),
            )
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not (payload or {}).get("ok"):
                raise BookmakerImportError(
                    f"SportyBet browser import returned JSON but failed with HTTP {(payload or {}).get('status')}. "
                    f"Response: {text[:250].replace(chr(10), ' ')}"
                )
            self._log_payload_shape("browser", code, data)
            return data

        last_payload = attempts[-1] if attempts else {}
        last_text = (last_payload or {}).get("text") or ""
        try:
            json.loads(last_text)
        except ValueError as exc:
            raise BookmakerImportError(
                "SportyBet browser import did not return JSON. "
                f"attempts={len(attempts)}, status={(last_payload or {}).get('status')}, "
                f"content_type={(last_payload or {}).get('contentType')!r}, "
                f"url={(last_payload or {}).get('url')!r}, "
                f"response={last_text[:250].replace(chr(10), ' ')!r}"
            ) from exc
        raise BookmakerImportError("SportyBet browser import did not return a usable response.")

    def import_share(self, *, code=None, url=None, payload=None):
        share_code = self.extract_code(code or url)
        if payload is None:
            if not share_code:
                raise ValueError("SportyBet share code or URL is required.")
            payload = self.fetch_share(share_code)
        else:
            share_code = share_code or str((payload.get("data") or {}).get("shareCode") or "").strip()

        data = payload.get("data") or {}
        ticket = data.get("ticket") or {}
        outcomes = ticket.get("outcomes") or data.get("outcomes") or []
        log.info(
            "SportyBet parse start code=%s biz_code=%s available=%s ticket_keys=%s selections=%s outcomes=%s data_keys=%s",
            share_code,
            payload.get("bizCode"),
            payload.get("isAvailable"),
            sorted(ticket.keys()),
            len(ticket.get("selections") or []),
            len(outcomes),
            sorted(data.keys()),
        )
        outcomes_by_event = self._merge_outcomes_by_event(outcomes)
        selections = []
        for item in ticket.get("selections") or []:
            event_id = str(item.get("eventId") or "")
            outcome = outcomes_by_event.get(event_id) or {}
            normalized = self._selection_from_item(item, outcome)
            if normalized:
                selections.append(normalized)
        if not selections:
            for outcome in outcomes:
                normalized = self._selection_from_outcome(outcome)
                if normalized:
                    selections.append(normalized)
        log.info(
            "SportyBet parsed selections code=%s count=%s markets=%s",
            share_code,
            len(selections),
            [
                {
                    "match": item.get("match"),
                    "market": item.get("market"),
                    "odds": item.get("odds"),
                }
                for item in selections[:20]
            ],
        )
        return {
            "provider": "sportybet",
            "share_code": share_code,
            "selection_count": len(selections),
            "selections": selections,
            "raw": payload,
        }

    def _log_payload_shape(self, source, code, payload):
        data = payload.get("data") if isinstance(payload, dict) else {}
        ticket = data.get("ticket") if isinstance(data, dict) else {}
        outcomes = (ticket or {}).get("outcomes") or (data or {}).get("outcomes") or []
        log.info(
            "SportyBet %s payload shape code=%s top_keys=%s data_keys=%s ticket_keys=%s selections=%s outcomes=%s",
            source,
            code,
            sorted(payload.keys()) if isinstance(payload, dict) else [],
            sorted(data.keys()) if isinstance(data, dict) else [],
            sorted(ticket.keys()) if isinstance(ticket, dict) else [],
            len((ticket or {}).get("selections") or []),
            len(outcomes),
        )
        if os.environ.get("SPORTYBET_IMPORT_DEBUG_PAYLOAD", "").lower() in {"1", "true", "yes"}:
            log.info("SportyBet %s raw payload code=%s payload=%s", source, code, json.dumps(payload, default=str)[:8000])

    def _selection_from_item(self, item, outcome):
        home = outcome.get("homeTeamName") or ""
        away = outcome.get("awayTeamName") or ""
        if not home or not away:
            return None
        market = self._market_name(item, outcome)
        if not market:
            return None
        canonical, market_descriptor = self._resolve_market_identity(item, outcome, fallback_text=market)
        tournament = (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", "")
        return {
            "provider_event_id": item.get("eventId") or outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market_descriptor.canonical or market,
            "provider_market_text": market,
            "provider_market_guide": self._market_guide(item, outcome),
            "canonical_market": canonical.to_dict(),
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": tournament,
            "kickoff_ms": outcome.get("estimateStartTime"),
            "odds": self._selection_odds(item, outcome),
            "provider_payload": {"selection": item, "outcome": outcome},
        }

    def _selection_from_outcome(self, outcome):
        home = outcome.get("homeTeamName") or ""
        away = outcome.get("awayTeamName") or ""
        markets = outcome.get("markets") or []
        market = self._market_name({}, {"markets": markets})
        if not home or not away or not market:
            return None
        canonical, market_descriptor = self._resolve_market_identity({}, outcome, fallback_text=market)
        return {
            "provider_event_id": outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market_descriptor.canonical or market,
            "provider_market_text": market,
            "provider_market_guide": self._market_guide({}, outcome),
            "canonical_market": canonical.to_dict(),
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", ""),
            "kickoff_ms": outcome.get("estimateStartTime"),
            "odds": None,
            "provider_payload": {"outcome": outcome},
        }

    @staticmethod
    def _merge_outcomes_by_event(outcomes):
        """
        Merge SportyBet's per-selection outcome entries into one entry per fixture.

        SportyBet emits a separate `outcomes` element for every selection, each carrying
        only that selection's market and only the chosen outcome within it. Indexing them
        into a plain dict keyed on eventId therefore keeps just the last one, and every
        other leg on the same fixture loses its market. Merging markets by
        (id, specifier) — and outcomes within them by id — keeps all legs resolvable,
        which matters for same-match multis and bet builders.
        """
        merged = {}
        market_index = {}
        for item in outcomes:
            event_id = str(item.get("eventId") or "")
            if not event_id:
                continue
            base = merged.get(event_id)
            if base is None:
                base = {key: value for key, value in item.items() if key != "markets"}
                base["markets"] = []
                merged[event_id] = base
                market_index[event_id] = {}
            for market in item.get("markets") or []:
                key = (str(market.get("id") or ""), str(market.get("specifier") or ""))
                existing = market_index[event_id].get(key)
                if existing is None:
                    existing = dict(market)
                    existing["outcomes"] = list(market.get("outcomes") or [])
                    base["markets"].append(existing)
                    market_index[event_id][key] = existing
                    continue
                seen = {str(entry.get("id") or "") for entry in existing["outcomes"]}
                for entry in market.get("outcomes") or []:
                    if str(entry.get("id") or "") not in seen:
                        existing["outcomes"].append(entry)
                        seen.add(str(entry.get("id") or ""))
        return merged

    def _resolve_market_identity(self, item, outcome, *, fallback_text=""):
        """
        Resolve the market from the bookmaker's ids, falling back to text only when the
        market id is unknown to us.

        `Over 2.5` has been observed meaning match goals, home-team goals, bookings and
        shots on target on the same feed, so the display string is not an identity. The
        fallback is flagged via the canonical market's `resolution`, never silently
        presented as if it were a confident identification.
        """
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        market = next(
            (
                candidate
                for candidate in outcome.get("markets") or []
                if str(candidate.get("id") or "") == market_id
                and (not specifier or str(candidate.get("specifier") or "") == specifier)
            ),
            None,
        ) or (outcome.get("markets") or [{}])[0]
        outcome_id = str(item.get("outcomeId") or "")
        selected = next(
            (
                candidate
                for candidate in market.get("outcomes") or []
                if str(candidate.get("id") or "") == outcome_id
            ),
            None,
        ) or (market.get("outcomes") or [{}])[0]

        canonical = resolve_sportybet_market(
            market_id=market_id or market.get("id"),
            outcome_id=outcome_id or selected.get("id"),
            specifier=specifier or market.get("specifier") or "",
            market_label=market.get("name") or market.get("desc") or "",
            outcome_label=selected.get("desc") or "",
        )
        if canonical.resolution == MarketResolution.MAPPED:
            return canonical, descriptor_from_canonical(canonical, raw=fallback_text)

        log.info(
            "SportyBet unmapped market id=%s specifier=%s outcome=%s label=%r",
            market_id,
            specifier,
            outcome_id,
            market.get("desc") or market.get("name") or "",
        )
        return canonical, describe_market(fallback_text)

    def _market_guide(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        market = next(
            (
                market
                for market in outcome.get("markets") or []
                if (not market_id or str(market.get("id") or "") == market_id)
                and (not specifier or str(market.get("specifier") or "") == specifier)
            ),
            None,
        ) or (outcome.get("markets") or [{}])[0]
        return str(market.get("marketGuide") or "")

    def _market_name(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        outcome_id = str(item.get("outcomeId") or "")
        market = next(
            (
                market
                for market in outcome.get("markets") or []
                if str(market.get("id") or "") == market_id
                and (not specifier or str(market.get("specifier") or "") == specifier)
            ),
            None,
        )
        if market:
            outcome_name = self._outcome_name(market, outcome_id)
            if outcome_name:
                return self._canonical_market(market.get("name") or market.get("desc"), outcome_name, market.get("specifier") or specifier)
        return self._canonical_market("", self._fallback_outcome_name(outcome_id), specifier)

    def _outcome_name(self, market, outcome_id):
        for outcome in market.get("outcomes") or []:
            if str(outcome.get("id") or "") == str(outcome_id):
                return str(outcome.get("desc") or outcome.get("name") or "")
        return ""

    def _fallback_outcome_name(self, outcome_id):
        return {
            "1": "Home",
            "2": "Draw",
            "3": "Away",
            "12": "Over",
            "13": "Under",
        }.get(str(outcome_id), "")

    def _canonical_market(self, market_name, outcome_name, specifier):
        market_text = str(market_name or "").strip().lower()
        outcome_text = str(outcome_name or "").strip()
        if outcome_text and any(token in market_text for token in ("goalscorer", "goal scorer", "player to score")):
            return f"{outcome_text} To Score"
        if outcome_text and any(token in market_text for token in ("player shots", "shots on target", "shots on goal", "player shot")):
            if "target" in market_text or "on goal" in market_text:
                # The specifier is machine syntax (`total=7.5`); it must not reach a label.
                return f"{outcome_text} Shots On Target".strip()
            return f"{outcome_text} Shots".strip()
        if outcome_text and any(token in market_text for token in ("player to be booked", "player card", "to be booked")):
            return f"{outcome_text} To Be Booked"
        descriptor = describe_market(
            outcome_name or market_name,
            market_name=market_name,
            outcome_name=outcome_name,
            specifier=specifier,
        )
        canonical = descriptor.canonical if descriptor.recognized else outcome_name.strip()
        if canonical:
            if "1up" in market_text:
                return f"{canonical} 1UP"
            if "2up" in market_text:
                return f"{canonical} 2UP"
            if "never down" in market_text:
                return f"{canonical} Never Down"
        return canonical

    def _selection_odds(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        outcome_id = str(item.get("outcomeId") or "")
        for market in outcome.get("markets") or []:
            if str(market.get("id") or "") != market_id:
                continue
            for market_outcome in market.get("outcomes") or []:
                if str(market_outcome.get("id") or "") == outcome_id:
                    return market_outcome.get("odds")
        return item.get("odds")


class BetanoBetslipImporter:
    def extract_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"/bookingcode/([A-Za-z0-9_-]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", text):
            return text
        return ""

    def import_betslip(self, *, code=None, url=None, payload=None):
        booking_code = self.extract_code(code or url)
        if payload is None:
            if not url and booking_code:
                url = f"https://www.betano.ng/bookingcode/{booking_code}"
            if not url:
                raise ValueError("Betano booking URL or code is required.")
            payload = self.fetch_betslip_payload(url)

        legs = self._legs_from_payload(payload)
        selections = []
        for leg in legs:
            normalized = self._selection_from_leg(leg)
            if normalized:
                selections.append(normalized)
        return {
            "provider": "betano",
            "booking_code": booking_code,
            "selection_count": len(selections),
            "selections": selections,
            "raw": payload,
        }

    def fetch_betslip_payload(self, url):
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install project dependencies and Chromium browser runtime to enable Betano link import."
            ) from exc

        timeout_ms = int(os.environ.get("BETANO_IMPORT_TIMEOUT_MS", "30000") or 30000)
        target_payload = None
        request_payload = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                locale="en-US",
                timezone_id="Africa/Lagos",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            def capture_request(request):
                nonlocal request_payload
                if "/api/betslip/v3/getbetslip" not in request.url:
                    return
                try:
                    post_data_json = getattr(request, "post_data_json", None)
                    request_payload = post_data_json() if callable(post_data_json) else post_data_json
                except Exception:
                    request_payload = None

            page.on("request", capture_request)
            try:
                with page.expect_response(
                    lambda response: "/api/betslip/v3/getbetslip" in response.url,
                    timeout=timeout_ms,
                ) as response_info:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                response = response_info.value
                try:
                    target_payload = response.json()
                except Exception as exc:
                    content_type = response.headers.get("content-type", "")
                    try:
                        body_preview = (response.text() or "")[:250].replace("\n", " ")
                    except Exception:
                        body_preview = ""
                    raise BookmakerImportError(
                        "Betano getbetslip response was captured, but it was not valid JSON. "
                        f"content_type={content_type!r}, status={response.status}, response={body_preview!r}"
                    ) from exc
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Timed out while waiting for Betano getbetslip response.") from exc
            finally:
                context.close()
                browser.close()

        if target_payload:
            return target_payload
        if request_payload:
            return request_payload
        raise RuntimeError("Betano getbetslip payload was not captured.")

    def _legs_from_payload(self, payload):
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("legs"), list):
            return data.get("legs") or []
        betslip = payload.get("betslip") if isinstance(payload, dict) else None
        if isinstance(betslip, dict) and isinstance(betslip.get("legs"), list):
            return betslip.get("legs") or []
        if isinstance(payload.get("legs"), list):
            return payload.get("legs") or []
        return []

    def _selection_from_leg(self, leg):
        if str(leg.get("sportId") or "").upper() not in {"", "FOOT"}:
            return None
        participants = leg.get("participants") or []
        home = (participants[0] or {}).get("name", "") if len(participants) > 0 else ""
        away = (participants[1] or {}).get("name", "") if len(participants) > 1 else ""
        if not home or not away:
            home, away = self._teams_from_event_name(leg.get("eventName"))
        market = self._canonical_market(leg)
        if not home or not away or not market:
            return None
        market_descriptor = describe_market(market)
        return {
            "provider_event_id": str(leg.get("eventId") or ""),
            "match": f"{home} vs {away}",
            "market": market,
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": str(leg.get("league") or leg.get("leagueName") or ""),
            "kickoff_ms": leg.get("eventStartTime"),
            "odds": leg.get("odds"),
            "provider_payload": {"leg": leg},
        }

    def _teams_from_event_name(self, value):
        text = str(value or "")
        if " - " in text:
            home, away = text.split(" - ", 1)
            return home.strip(), away.strip()
        if " vs " in text.lower():
            parts = re.split(r"\s+vs\s+", text, maxsplit=1, flags=re.IGNORECASE)
            return parts[0].strip(), parts[1].strip()
        return "", ""

    def _canonical_market(self, leg):
        description = str(leg.get("description") or "")
        market = str(leg.get("market") or "")
        market_sort = str(leg.get("marketSort") or "")
        event_home, event_away = self._teams_from_event_name(leg.get("eventName"))
        outcome = description
        if market_sort in {"MRES", "MR12"}:
            if description == event_home:
                outcome = "Home"
            elif description == event_away:
                outcome = "Away"
        descriptor = describe_market(
            description,
            market_name=f"{market} {market_sort}",
            outcome_name=outcome,
        )
        return descriptor.canonical if descriptor.recognized else description.strip()


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
        raw = str(os.environ.get("STATPAL_TRACKED_LEAGUES") or "").strip()
        if not raw:
            return set()
        league_ids = set()
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

    def _statpal_cached_runner_fixtures(self, target_date):
        queryset = FixtureCache.objects.filter(match_date=target_date, source="statpal")
        tracked = self._statpal_tracked_league_ids()
        rows = list(queryset.order_by("country", "league", "kickoff", "fixture"))
        if tracked and not self._statpal_track_all_leagues():
            rows = [row for row in rows if self._statpal_fixture_league_id(row) in tracked]
        return [self._cached_fixture_runner_payload(row) for row in rows]

    def _daily_runner_fixtures(self, target_date):
        from .grindalgo import algo_runner

        statpal_errors = []
        if self._statpal_primary_daily_enabled():
            fixtures = self._statpal_cached_runner_fixtures(target_date)
            if not fixtures:
                try:
                    sync_result = FixtureSearchService(runner_service=self).sync_statpal_universe(
                        start_date=target_date,
                        days=0,
                    )
                    statpal_errors = sync_result.get("errors") or []
                    fixtures = self._statpal_cached_runner_fixtures(target_date)
                except Exception as exc:
                    statpal_errors = [{"provider": "statpal", "error": str(exc)}]
            if fixtures:
                return {
                    "fixtures": fixtures,
                    "source": "statpal",
                    "fallback_used": False,
                    "errors": statpal_errors,
                }

        fixtures = algo_runner.fetch_aps_fixtures(target_date.isoformat())
        fixtures = self._attach_statpal_fixture_context(fixtures, target_date)
        return {
            "fixtures": fixtures,
            "source": "api_football",
            "fallback_used": self._statpal_primary_daily_enabled(),
            "errors": statpal_errors,
        }

    def _hydrate_statpal_scoring_context(self, fixture):
        provider_match_id = str(fixture.get("statpal_provider_match_id") or "").strip()
        provider_competition_id = str(fixture.get("statpal_provider_competition_id") or fixture.get("code") or "").strip()
        match_id = str(fixture.get("match_id") or fixture.get("aps_id") or "").strip()
        if not provider_match_id and not match_id:
            return fixture

        from .statpal_snapshots import statpal_snapshot_service

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
        except Exception as exc:
            refresh = {"errors": [{"provider": "statpal", "error": str(exc)}]}
            context = {"available": False, "snapshots": {}}

        enriched = dict(fixture)
        enriched["statpal_refresh"] = refresh
        enriched["statpal_context"] = context
        return enriched

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
            from .statpal_daily_build import StatPalDailyBuildService

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
        from .market_capabilities import MarketCapabilityService

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
            "reasoning": "",
            "model_verdict": "",
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
        from .daily_market_catalog import daily_evaluation_route

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
        insights["council_review"] = council_review(candidate)
        return candidate

    def _select_prediction_ids(self, algo_run):
        max_daily = max(1, self._runner_env_int("ALGO_MAX_DAILY_PICKS", 15))
        optimization_mode = self._daily_optimization_mode()
        performance = (algo_run.result or {}).get("performance_profile") or self._performance_profile()

        predictions = list(
            MarketPrediction.objects.filter(run=algo_run)
            .exclude(market__in=["DC: 1X", "DC: X2"])
            .exclude(ev__isnull=True)
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
            if not assess_recommendation(candidate)["recommended"]:
                return False
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
        for prediction in MarketPrediction.objects.filter(run=algo_run, published=False):
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
        if updates:
            MarketPrediction.objects.bulk_update(updates, ["rejection_reason", "insights"], batch_size=500)

    def _publish_selected_predictions(self, algo_run, bankroll, use_llm=False):
        MarketPrediction.objects.filter(run=algo_run, published=True).update(
            published=False,
            selected_pick=None,
        )
        self._refresh_recommendation_rejections(algo_run)
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

        from .grindalgo import algo_runner

        reason_candidates = [self._candidate_dict_for_reasoning(payload) for payload in payloads]
        for payload, candidate in zip(payloads, reason_candidates):
            payload["reasoning"] = algo_runner.pick_reasoning(candidate)
            payload["model_verdict"] = algo_runner.pick_verdict(candidate)
        if use_llm:
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

        from .grindalgo import algo_runner

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
                from .grindalgo import algo_runner

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
                from .grindalgo import algo_runner

                algo_runner.clear_runtime_caches()
                source_payload = dict(fixture.source_payload or {})
                source_payload = self._hydrate_statpal_scoring_context(source_payload)
                scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(source_payload)
                summary = algo_runner.serialize_fixture_summaries(
                    [scored_fixture],
                    [confs],
                    [real_odds],
                )[0]
                summary["source_payload"] = scored_fixture
                summary = self._enrich_fixture_statpal_diagnostics(summary)
                self._persist_fixture_summary(algo_run, summary)
                market_count = self._persist_fixture_market_predictions(algo_run, summary)
                family_counts = self._market_family_counts(summary.get("markets") or [])
                statpal_family_coverage = (summary.get("insights") or {}).get("statpal_market_family_coverage") or {}
                log.info(
                    "Daily fixture scored run=%s match_id=%s markets=%s market_families=%s statpal_family_coverage=%s",
                    algo_run.id,
                    summary.get("match_id"),
                    market_count,
                    family_counts,
                    statpal_family_coverage,
                )
                algo_runner.clear_runtime_caches()
            return {
                "fixture_id": fixture.id,
                "status": "scored",
                "market_count": market_count,
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

    def publish_fanout_run(self, algo_run: AlgoRun):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)
        algo_run.refresh_from_db()
        bankroll = algo_run.bankroll or 10000
        picks = self._publish_selected_predictions(algo_run, bankroll)
        scored_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.SCORED).count()
        failed_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.FAILED).count()
        predictions = list(MarketPrediction.objects.filter(run=algo_run).only("market", "insights"))
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
            "market_family_counts": self._prediction_market_family_counts(predictions),
            "statpal_market_family_coverage": self._prediction_statpal_family_coverage(predictions),
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
                from .grindalgo import algo_runner

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
                        scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(fixture)
                        summary = algo_runner.serialize_fixture_summaries(
                            [scored_fixture],
                            [confs],
                            [real_odds],
                        )[0]
                        summary["source_payload"] = scored_fixture
                        summary = self._enrich_fixture_statpal_diagnostics(summary)
                        for family, count in self._market_family_counts(summary.get("markets") or []).items():
                            market_family_counts[family] += count
                        self._merge_market_family_statpal_coverage(
                            statpal_family_coverage,
                            (summary.get("insights") or {}).get("statpal_market_family_coverage") or {},
                        )
                        self._persist_fixture_summary(algo_run, summary)
                        market_count += self._persist_fixture_market_predictions(algo_run, summary)
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

    # Markets that _check_market can resolve from a finished fixture. Kept next to the
    # resolver so the two stay in step; anything outside this set is reported as
    # unsettleable rather than silently treated as a void (push).
    SETTLEABLE_MARKETS = frozenset({
        "Home Win",
        "Away Win",
        "Draw",
        "Over 1.5",
        "Over 2.5",
        "Over 3.5",
        "Under 1.5",
        "Under 2.5",
        "Under 3.5",
        "GG / BTTS Yes",
        "GG + Over 2.5",
        "DC: 1X",
        "DC: X2",
        "DC: 12",
        "Home CS",
        "Away CS",
        "AH Home +0.5",
        "AH Away +0.5",
        "DNB Home",
        "DNB Away",
        "First to Score H",
        "First to Score A",
    })

    @classmethod
    def can_settle_market(cls, market):
        market = str(market or "").strip()
        if not market:
            return False
        if market.startswith("Corners Over ") or market.startswith("Corners Under "):
            try:
                float(market.rsplit(" ", 1)[-1])
            except (TypeError, ValueError):
                return False
            return True
        return market in cls.SETTLEABLE_MARKETS

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

    def _finished_fixture_map(self, target_date):
        fixtures = self._api_football_get(
            "/fixtures",
            {"date": target_date.isoformat(), "timezone": "Africa/Lagos"},
        )
        return {
            str((fixture.get("fixture") or {}).get("id")): fixture
            for fixture in fixtures
            if ((fixture.get("fixture") or {}).get("status") or {}).get("short") in {"FT", "AET", "PEN"}
        }

    @staticmethod
    def _fixture_goals(fixture):
        goals = fixture.get("goals") or {}
        return goals.get("home"), goals.get("away")

    @staticmethod
    def _fixture_team_names(fixture):
        teams = fixture.get("teams") or {}
        return (teams.get("home") or {}).get("name"), (teams.get("away") or {}).get("name")

    def _settle_database_picks(self, target_date):
        fixture_map = self._finished_fixture_map(target_date)

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

        performance_profile = self._performance_profile()
        strategy_profile = self._strategy_profile(algo_run.target_date, performance_profile)
        env = self._runner_env({
            "OVERRIDE_DATE": algo_run.target_date.isoformat(),
            "ALGO_PERFORMANCE_PROFILE": json.dumps(performance_profile),
            "ALGO_STRATEGY_PROFILE": json.dumps(strategy_profile),
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

    def update_results(self, *, target_date=None):
        if target_date is not None:
            settle_date = target_date
        else:
            settle_date = timezone.localdate() - timedelta(days=1)
        return self._settle_database_picks(settle_date)

    def settle_slip_selections(self, *, target_date=None):
        """
        Resolve user slip legs against finished fixtures.

        Unlike Pick settlement, users submit arbitrary bookmaker markets, so a market
        this engine cannot resolve is recorded as ``unsettleable`` instead of being
        silently counted as a void. Only settled legs feed the accuracy stats.
        """
        settle_date = target_date or (timezone.localdate() - timedelta(days=1))
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
            if not self.can_settle_market(selection.market):
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
