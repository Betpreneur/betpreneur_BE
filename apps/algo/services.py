import os
import json
import gc
import re
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

from .models import AlgoFixture, AlgoRun, FixtureCache, MarketPrediction, Pick, StrategyReview
from .recommendation_policy import (
    assess_calibration_trust,
    assess_league_market_trust,
    assess_recommendation,
)
from .council import CAUTION, REJECT, council_review


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


class FixtureSearchService:
    DEFAULT_DAYS = 3
    MAX_DAYS = 14

    def __init__(self, runner_service=None):
        self.runner_service = runner_service or AlgoRunnerService()

    def sync_upcoming(self, *, start_date=None, days=None):
        start_date = start_date or timezone.localdate()
        days = min(int(days or self.DEFAULT_DAYS), self.MAX_DAYS)
        total = 0
        errors = []

        from .grindalgo import algo_runner

        with temporary_env(self.runner_service._runner_env()):
            for offset in range(days + 1):
                target_date = start_date + timedelta(days=offset)
                try:
                    fixtures = algo_runner.fetch_aps_fixtures(target_date.isoformat())
                except Exception as exc:
                    errors.append({"date": target_date.isoformat(), "error": str(exc)})
                    continue
                total += self._upsert_fixtures(fixtures, target_date)
        return {"synced": total, "errors": errors}

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
                    "api_payload": item,
                },
            )
            count += 1
        return count

    def search(self, query, *, start_date=None, days=None, limit=10, refresh=False):
        start_date = start_date or timezone.localdate()
        days = min(int(days or self.DEFAULT_DAYS), self.MAX_DAYS)
        limit = max(1, min(int(limit or 10), 25))
        refreshed = False
        refresh_errors = []
        candidates = self._search_cached(query, start_date=start_date, days=days, limit=limit)
        if refresh or not candidates:
            refresh_result = self.sync_upcoming(start_date=start_date, days=days)
            refresh_errors = refresh_result.get("errors", [])
            refreshed = True
            candidates = self._search_cached(query, start_date=start_date, days=days, limit=limit)
        return {"refreshed": refreshed, "refresh_errors": refresh_errors, "results": candidates}

    def _search_cached(self, query, *, start_date, days, limit):
        home_query, away_query, normalized_query = parse_match_query(query)
        end_date = start_date + timedelta(days=days)
        queryset = FixtureCache.objects.filter(match_date__range=(start_date, end_date))
        if normalized_query:
            tokens = [token for token in normalized_query.split() if len(token) > 1]
            token_filter = Q()
            for token in tokens[:6]:
                token_filter |= Q(fixture_normalized__icontains=token)
                token_filter |= Q(home_team_normalized__icontains=token)
                token_filter |= Q(away_team_normalized__icontains=token)
            if token_filter:
                queryset = queryset.filter(token_filter)

        scored = []
        for fixture in queryset[:500]:
            score = self._match_score(fixture, home_query, away_query, normalized_query)
            if score >= 35:
                scored.append((score, fixture))
        scored.sort(key=lambda item: (item[0], item[1].match_date), reverse=True)
        return [self._serialize_fixture(fixture, score) for score, fixture in scored[:limit]]

    def _match_score(self, fixture, home_query, away_query, normalized_query):
        fixture_norm = fixture.fixture_normalized or normalize_fixture_text(fixture.fixture)
        if home_query and away_query:
            direct = (
                SequenceMatcher(None, home_query, fixture.home_team_normalized).ratio()
                + SequenceMatcher(None, away_query, fixture.away_team_normalized).ratio()
            ) / 2
            reversed_match = (
                SequenceMatcher(None, home_query, fixture.away_team_normalized).ratio()
                + SequenceMatcher(None, away_query, fixture.home_team_normalized).ratio()
            ) / 2
            return round(max(direct, reversed_match) * 100, 2)
        return round(SequenceMatcher(None, normalized_query, fixture_norm).ratio() * 100, 2)

    def _serialize_fixture(self, fixture, score):
        return {
            "match_id": fixture.match_id,
            "match_date": fixture.match_date,
            "fixture": fixture.fixture,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "home_logo": fixture.home_logo,
            "away_logo": fixture.away_logo,
            "league": fixture.league,
            "league_logo": fixture.league_logo,
            "country": fixture.country,
            "country_flag": fixture.country_flag,
            "round": fixture.round,
            "league_type": fixture.league_type,
            "kickoff": fixture.kickoff,
            "kickoff_utc": fixture.kickoff_utc,
            "match_score": score,
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
        response = requests.get(
            self.SHARE_ENDPOINT.format(code=code),
            headers={
                "Accept": "*/*",
                "Accept-Language": "en",
                "Referer": f"https://www.sportybet.com/ng/?shareCode={code}",
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
        response.raise_for_status()
        return response.json()

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
        outcomes_by_event = {
            str(item.get("eventId") or ""): item
            for item in ticket.get("outcomes") or []
            if item.get("eventId")
        }
        selections = []
        for item in ticket.get("selections") or []:
            event_id = str(item.get("eventId") or "")
            outcome = outcomes_by_event.get(event_id) or {}
            normalized = self._selection_from_item(item, outcome)
            if normalized:
                selections.append(normalized)
        if not selections:
            for outcome in ticket.get("outcomes") or []:
                normalized = self._selection_from_outcome(outcome)
                if normalized:
                    selections.append(normalized)
        return {
            "provider": "sportybet",
            "share_code": share_code,
            "selection_count": len(selections),
            "selections": selections,
            "raw": payload,
        }

    def _selection_from_item(self, item, outcome):
        home = outcome.get("homeTeamName") or ""
        away = outcome.get("awayTeamName") or ""
        if not home or not away:
            return None
        market = self._market_name(item, outcome)
        if not market:
            return None
        tournament = (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", "")
        return {
            "provider_event_id": item.get("eventId") or outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market,
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
        return {
            "provider_event_id": outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market,
            "home_team": home,
            "away_team": away,
            "competition": (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", ""),
            "kickoff_ms": outcome.get("estimateStartTime"),
            "odds": None,
            "provider_payload": {"outcome": outcome},
        }

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
        market_text = normalize_fixture_text(market_name)
        outcome_text = normalize_fixture_text(outcome_name)
        total_match = re.search(r"total=([0-9.]+)", str(specifier or ""))
        line = total_match.group(1) if total_match else ""
        if "over under" in market_text or "total goals" in market_text or line:
            if "under" in outcome_text:
                return f"Under {line}" if line else "Under"
            if "over" in outcome_text:
                return f"Over {line}" if line else "Over"
        if "match result" in market_text or outcome_text in {"home", "away", "draw"}:
            if outcome_text == "home":
                return "Home Win"
            if outcome_text == "away":
                return "Away Win"
            if outcome_text == "draw":
                return "Draw"
        if "both teams" in market_text and "yes" in outcome_text:
            return "GG / BTTS Yes"
        return outcome_name.strip()

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
                target_payload = response.json()
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
        return {
            "provider_event_id": str(leg.get("eventId") or ""),
            "match": f"{home} vs {away}",
            "market": market,
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
        text = normalize_fixture_text(f"{market} {description} {market_sort}")
        total_match = re.search(r"(over|under)\s+([0-9]+(?:\.[0-9]+)?)", description, flags=re.IGNORECASE)
        if total_match:
            side = total_match.group(1).title()
            line = total_match.group(2)
            return f"{side} {line}"
        if "over under" in text or "total goals" in text:
            line_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", description)
            if "under" in text and line_match:
                return f"Under {line_match.group(1)}"
            if "over" in text and line_match:
                return f"Over {line_match.group(1)}"
        if "both teams" in text and "yes" in text:
            return "GG / BTTS Yes"
        if "match result" in text or market_sort in {"MRES", "MR12"}:
            event_home, event_away = self._teams_from_event_name(leg.get("eventName"))
            if description == event_home:
                return "Home Win"
            if description == event_away:
                return "Away Win"
            if normalize_fixture_text(description) == "draw":
                return "Draw"
        return description.strip()


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
                        odds_meta=market.get("odds_meta") or {},
                        eligible=bool(market.get("eligible")),
                        published=published,
                        rejection_reason=self._prediction_rejection_reason(market, published),
                        risk_flags=market.get("risk_flags") or [],
                        insights=market.get("insights") or {},
                        home_recent_form=fixture.get("home_recent_form") or {},
                        away_recent_form=fixture.get("away_recent_form") or {},
                        fixture_context=fixture_context,
                        team_news=fixture.get("team_news") or {},
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
                    home_recent_form=fixture.get("home_recent_form") or {},
                    away_recent_form=fixture.get("away_recent_form") or {},
                    fixture_context=fixture.get("fixture_context") or {},
                    team_news=fixture.get("team_news") or {},
                    corner_profile=fixture.get("corner_profile") or {},
                    insights=fixture.get("insights") or {},
                    source_payload=fixture.get("source_payload") or {},
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
            "home_recent_form": fixture.get("home_recent_form") or {},
            "away_recent_form": fixture.get("away_recent_form") or {},
            "fixture_context": fixture.get("fixture_context") or {},
            "team_news": fixture.get("team_news") or {},
            "corner_profile": fixture.get("corner_profile") or {},
            "insights": fixture.get("insights") or {},
            "source_payload": fixture.get("source_payload") or {},
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
                    odds_meta=market.get("odds_meta") or {},
                    eligible=bool(market.get("eligible")),
                    published=False,
                    rejection_reason=self._prediction_rejection_reason(market, False),
                    risk_flags=market.get("risk_flags") or [],
                    insights=market.get("insights") or {},
                    home_recent_form=fixture.get("home_recent_form") or {},
                    away_recent_form=fixture.get("away_recent_form") or {},
                    fixture_context=fixture_context,
                    team_news=fixture.get("team_news") or {},
                )
            )
        if rows:
            MarketPrediction.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        return len(rows)

    def _selected_pick_payload_from_prediction(self, prediction, tier, bankroll):
        risk_flags = list(prediction.risk_flags or [])
        insights = dict(prediction.insights or {})
        council = insights.get("council_review") or {}
        final_confidence = council.get("final_confidence") or prediction.confidence
        insights["published_tier"] = tier
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
        performance = (algo_run.result or {}).get("performance_profile") or self._performance_profile()

        predictions = list(
            MarketPrediction.objects.filter(run=algo_run)
            .exclude(market__in=["DC: 1X", "DC: X2"])
            .exclude(ev__isnull=True)
            .order_by("-confidence", "-ev", "odds")
        )
        predictions.sort(key=self._prediction_rank, reverse=True)

        buckets = {
            Pick.Tier.BANKER: [],
            Pick.Tier.VALUE_GEM: [],
            Pick.Tier.WILD_CARD: [],
        }
        used_matches = set()
        market_counts = defaultdict(int)

        def add_prediction(prediction):
            if prediction.match_id in used_matches:
                return False
            candidate = self._recommendation_candidate(prediction, performance)
            if not assess_recommendation(candidate)["recommended"]:
                return False
            tier = self._tier_after_council(prediction, candidate)
            if not tier:
                return False
            buckets[tier].append(prediction.id)
            used_matches.add(prediction.match_id)
            market_counts[prediction.market] += 1
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
            algo_runner.enhance_pick_explanations_with_llm(reason_candidates)
            for payload, candidate in zip(payloads, reason_candidates):
                payload["reasoning"] = candidate.get("reasoning", payload["reasoning"])
                payload["model_verdict"] = candidate.get("model_verdict", payload["model_verdict"])

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
                "reasoning": pick.reasoning,
                "model_verdict": pick.model_verdict,
            })
        with temporary_env(self._runner_env()):
            algo_runner.enhance_pick_explanations_with_llm(candidates)
        updated = 0
        for pick, candidate in zip(picks, candidates):
            reasoning = candidate.get("reasoning") or pick.reasoning
            verdict = candidate.get("model_verdict") or pick.model_verdict
            if reasoning != pick.reasoning or verdict != pick.model_verdict:
                pick.reasoning = reasoning
                pick.model_verdict = verdict
                pick.save(update_fields=["reasoning", "model_verdict"])
                updated += 1
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
                "source_payload": fixture,
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
                fixtures = algo_runner.fetch_aps_fixtures(algo_run.target_date.isoformat())
                fixtures = self._limit_fixtures(fixtures)

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
                        source_payload=fixture,
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
                scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(source_payload)
                summary = algo_runner.serialize_fixture_summaries(
                    [scored_fixture],
                    [confs],
                    [real_odds],
                )[0]
                summary["source_payload"] = scored_fixture
                self._persist_fixture_summary(algo_run, summary)
                market_count = self._persist_fixture_market_predictions(algo_run, summary)
                algo_runner.clear_runtime_caches()
            return {"fixture_id": fixture.id, "status": "scored", "market_count": market_count}
        except Exception as exc:
            fixture.status = AlgoFixture.Status.FAILED
            fixture.error = str(exc)[:2000]
            fixture.save(update_fields=["status", "error", "updated_at"])
            return {"fixture_id": fixture.id, "status": "failed", "error": str(exc)}

    def publish_fanout_run(self, algo_run: AlgoRun):
        if not isinstance(algo_run, AlgoRun):
            algo_run = AlgoRun.objects.get(id=algo_run)
        algo_run.refresh_from_db()
        bankroll = algo_run.bankroll or 10000
        picks = self._publish_selected_predictions(algo_run, bankroll)
        scored_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.SCORED).count()
        failed_count = AlgoFixture.objects.filter(run=algo_run, status=AlgoFixture.Status.FAILED).count()
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
            "markets_70_plus": aggregate.get("markets_70_plus") or 0,
            "markets_65_plus": aggregate.get("markets_65_plus") or 0,
            "fixture_count": scored_count,
            "bankers": algo_run.bankers,
            "value_gems": algo_run.value_gems,
            "wild_cards": algo_run.wild_cards,
            "bankroll": float(bankroll),
            "storage": self._run_storage_payload(),
        }
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
                fixtures = algo_runner.fetch_aps_fixtures(algo_run.target_date.isoformat())
                fixtures = self._limit_fixtures(fixtures)

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
                for index, fixture in enumerate(fixtures, start=1):
                    try:
                        scored_fixture, confs, real_odds = algo_runner.score_aps_fixture_for_pipeline(fixture)
                        summary = algo_runner.serialize_fixture_summaries(
                            [scored_fixture],
                            [confs],
                            [real_odds],
                        )[0]
                        self._persist_fixture_summary(algo_run, summary)
                        market_count += self._persist_fixture_market_predictions(algo_run, summary)
                        markets_70_plus += summary.get("markets_70_plus", 0)
                        markets_65_plus += summary.get("markets_65_plus", 0)
                        scored_count += 1
                        if index % 10 == 0 or index == len(fixtures):
                            algo_runner.log_memory(f"staged_scored_{index}_of_{len(fixtures)}")
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
                "market_count": market_count,
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
