"""Finding a fixture from free text.

Fuzzy team-name matching across providers: acronyms, token coverage, and
alias maps, falling back to fetching from the provider when nothing cached
matches. Extracted from the 5k-line apps/algo/services.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from betpreneur.modules.catalog.domain.text import normalize_fixture_text, parse_match_query
from betpreneur.modules.catalog.models import (
    BookmakerLeagueMap,
    FixtureCache,
    ProviderFixtureMap,
    TeamAliasMap,
)
from betpreneur.modules.catalog.services.runner_env import runner_env
from betpreneur.platform.config import temporary_env
from betpreneur.platform.db.json import json_safe

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


def token_side_score(query, fixture_name):
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

    def __init__(self):
        pass

    def sync_statpal_universe(self, *, start_date=None, days=None):
        """
        Refresh the whole fixture universe in one call.

        StatPal's daily endpoint ignores a date parameter and always returns the same
        rolling window — roughly 1,200 fixtures across 260+ competitions. Asking it once
        therefore covers every date we care about, and carries StatPal's own team ids,
        which are what the corner and card rate profiles need to resolve.
        """
        from betpreneur.modules.catalog.services.provider_client import (
            StatPalConfigurationError,
            StatPalError,
        )
        from betpreneur.modules.catalog.services.statpal_normalize import (
            StatPalDailyMatchProvider,
            normalize_daily_matches,
        )

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
        from betpreneur.modules.catalog.services.provider_client import (
            StatPalConfigurationError,
            StatPalError,
            statpal_client,
        )
        from betpreneur.modules.catalog.services.statpal_normalize import (
            normalize_daily_matches,
            normalize_leagues,
        )

        start_date = start_date or timezone.localdate()
        horizon = start_date + timedelta(days=max(0, int(days)))
        client = statpal_client()

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

        from betpreneur.modules.catalog.services import legacy_runner as algo_runner

        extra_env = {}
        if unrestricted:
            extra_env = {
                "APS_TRACK_ALL_LEAGUES": "True",
                "APS_MAX_FIXTURES": "0",
            }
        with temporary_env(runner_env(extra_env)):
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
        from betpreneur.modules.catalog.services.provider_client import (
            StatPalConfigurationError,
            StatPalError,
        )
        from betpreneur.modules.catalog.services.statpal_normalize import StatPalDailyMatchProvider

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
        from betpreneur.modules.catalog.services import legacy_runner as algo_runner

        with temporary_env(runner_env({"APS_TRACK_ALL_LEAGUES": "True", "APS_MAX_FIXTURES": "0"})):
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
        from betpreneur.modules.catalog.services import legacy_runner as algo_runner

        errors = []
        with temporary_env(runner_env({"APS_TRACK_ALL_LEAGUES": "True", "APS_MAX_FIXTURES": "0"})):
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
            token_side_score(home_query, fixture.home_team_normalized or fixture.home_team) >= 0.70
            and token_side_score(away_query, fixture.away_team_normalized or fixture.away_team) >= 0.70
        )
        reversed_match = (
            token_side_score(home_query, fixture.away_team_normalized or fixture.away_team) >= 0.70
            and token_side_score(away_query, fixture.home_team_normalized or fixture.home_team) >= 0.70
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
                token_side_score(home_query, fixture.home_team_normalized or fixture.home_team)
                + token_side_score(away_query, fixture.away_team_normalized or fixture.away_team)
            ) / 2
            reversed_token = (
                token_side_score(home_query, fixture.away_team_normalized or fixture.away_team)
                + token_side_score(away_query, fixture.home_team_normalized or fixture.home_team)
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
