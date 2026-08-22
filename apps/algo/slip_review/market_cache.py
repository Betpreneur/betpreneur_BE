import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ..markets.api import describe_market
from ..models import SlipReviewMarketCache
from ..market_data.api import json_safe


logger = logging.getLogger(__name__)


class SlipReviewMarketCacheWriter:
    """
    Persist private, pre-scored markets for Match Checker slip review lookups.

    This cache is deliberately separate from MarketPrediction so broad slip-review
    coverage can grow without changing the public all-games/top-picks league gate.
    """

    def __init__(self, *, ttl_hours: int | None = None, cache_version: str | None = None):
        self.ttl_hours = ttl_hours or getattr(settings, "SLIP_REVIEW_MARKET_CACHE_TTL_HOURS", 72)
        self.cache_version = cache_version or getattr(settings, "SLIP_REVIEW_MARKET_CACHE_VERSION", "v1")

    def upsert_fixture_markets(
        self,
        fixture: dict[str, Any],
        *,
        source: str = SlipReviewMarketCache.Source.MERGED,
        cache_scope: str = SlipReviewMarketCache.Scope.SLIP_REVIEW,
        expires_at=None,
    ) -> dict[str, Any]:
        fixture = fixture or {}
        markets = fixture.get("markets") or []
        if not isinstance(markets, list):
            markets = []

        match_id = self._string(
            fixture.get("match_id")
            or fixture.get("id")
            or fixture.get("provider_match_id")
            or fixture.get("fixture_id")
        )
        match_date = self._match_date(fixture)
        fixture_name = self._string(
            fixture.get("fixture")
            or fixture.get("match")
            or self._join_fixture(fixture.get("home_team"), fixture.get("away_team"))
        )

        if not match_id or not match_date or not fixture_name:
            return {
                "cached": 0,
                "skipped": len(markets),
                "markets_seen": len(markets),
                "error": "missing_fixture_identity",
                "match_id": match_id,
                "fixture": fixture_name,
            }

        expiry = expires_at or self._expires_at(fixture)
        fixture_payload = self._fixture_payload(fixture)
        provider_merge = json_safe(fixture.get("provider_merge") or {})
        source_payload = fixture.get("source_payload") or fixture.get("fixture_context") or {}
        if not isinstance(source_payload, dict):
            source_payload = {}
        now = timezone.now()
        rows_by_key = {}
        skipped = 0
        deduped = 0

        for market_payload in markets:
            if not isinstance(market_payload, dict):
                skipped += 1
                continue
            market_name = self._string(market_payload.get("market"))
            if not market_name:
                skipped += 1
                continue

            descriptor = describe_market(market_name)
            canonical_market = descriptor.canonical or market_name
            row = SlipReviewMarketCache(
                cache_scope=cache_scope,
                source=source,
                match_date=match_date,
                fixture=fixture_name,
                home_team=self._team_name(fixture, "home"),
                away_team=self._team_name(fixture, "away"),
                home_logo=self._team_logo(fixture, "home"),
                away_logo=self._team_logo(fixture, "away"),
                league=self._league_name(fixture),
                league_id=self._string(
                    fixture.get("league_id")
                    or fixture.get("provider_competition_id")
                    or source_payload.get("league_id")
                ),
                league_logo=self._string(fixture.get("league_logo") or fixture.get("competition_logo")),
                country=self._string(fixture.get("country") or source_payload.get("country")),
                country_flag=self._string(fixture.get("country_flag")),
                kickoff=self._string(fixture.get("kickoff")),
                match_id=match_id,
                provider_match_id=self._provider_match_id(fixture, source_payload, provider_merge),
                provider_competition_id=self._string(
                    fixture.get("provider_competition_id")
                    or fixture.get("league_id")
                    or source_payload.get("provider_competition_id")
                ),
                home_team_id=self._team_id(fixture, "home", provider_merge),
                away_team_id=self._team_id(fixture, "away", provider_merge),
                market=canonical_market,
                market_family=self._market_family(market_payload, descriptor),
                meaning=self._string(market_payload.get("meaning")),
                raw_confidence=self._int(market_payload.get("raw_confidence")),
                confidence=self._int(market_payload.get("confidence")),
                final_confidence=self._final_confidence(market_payload),
                odds=self._decimal(market_payload.get("odds"), places=2),
                ev=self._decimal(market_payload.get("ev"), places=3),
                odds_source=self._string(market_payload.get("odds_source")),
                odds_meta=json_safe(market_payload.get("odds_meta") or {}),
                eligible=bool(market_payload.get("eligible")),
                risk_flags=json_safe(market_payload.get("risk_flags") or []),
                insights=json_safe(market_payload.get("insights") or {}),
                market_payload=json_safe(market_payload),
                fixture_payload=fixture_payload,
                provider_merge=provider_merge,
                data_quality=self._data_quality(market_payload),
                cache_version=self.cache_version,
                expires_at=expiry,
                created_at=now,
                updated_at=now,
            )
            key = (row.cache_scope, row.match_id, row.market)
            existing = rows_by_key.get(key)
            if existing is None:
                rows_by_key[key] = row
                continue
            deduped += 1
            if self._row_rank(row) > self._row_rank(existing):
                rows_by_key[key] = row

        rows = list(rows_by_key.values())

        if rows:
            SlipReviewMarketCache.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["cache_scope", "match_id", "market"],
                update_fields=[
                    "source",
                    "match_date",
                    "fixture",
                    "home_team",
                    "away_team",
                    "home_logo",
                    "away_logo",
                    "league",
                    "league_id",
                    "league_logo",
                    "country",
                    "country_flag",
                    "kickoff",
                    "provider_match_id",
                    "provider_competition_id",
                    "home_team_id",
                    "away_team_id",
                    "market_family",
                    "meaning",
                    "raw_confidence",
                    "confidence",
                    "final_confidence",
                    "odds",
                    "ev",
                    "odds_source",
                    "odds_meta",
                    "eligible",
                    "risk_flags",
                    "insights",
                    "market_payload",
                    "fixture_payload",
                    "provider_merge",
                    "data_quality",
                    "cache_version",
                    "expires_at",
                    "updated_at",
                ],
                batch_size=500,
            )

        summary = {
            "cached": len(rows),
            "skipped": skipped,
            "deduped": deduped,
            "markets_seen": len(markets),
            "match_id": match_id,
            "provider_match_id": rows[0].provider_match_id if rows else "",
            "fixture": fixture_name,
            "match_date": match_date.isoformat(),
            "expires_at": expiry.isoformat(),
            "source": source,
            "cache_version": self.cache_version,
        }
        logger.info(
            "Slip review market cache write match_id=%s fixture=%r markets_seen=%s cached=%s skipped=%s deduped=%s source=%s",
            summary["match_id"],
            summary["fixture"],
            summary["markets_seen"],
            summary["cached"],
            summary["skipped"],
            summary["deduped"],
            summary["source"],
        )
        return summary

    def _fixture_payload(self, fixture: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in fixture.items() if key != "markets"}
        return json_safe(payload)

    def _expires_at(self, fixture: dict[str, Any]):
        kickoff = self._parse_datetime(fixture.get("kickoff") or fixture.get("kickoff_at"))
        if kickoff:
            return kickoff + timezone.timedelta(hours=3)
        return timezone.now() + timezone.timedelta(hours=self.ttl_hours)

    def _match_date(self, fixture: dict[str, Any]):
        value = fixture.get("match_date") or fixture.get("date") or fixture.get("target_date")
        if hasattr(value, "date"):
            return value.date()
        parsed = parse_date(str(value or ""))
        if parsed:
            return parsed
        kickoff = self._parse_datetime(fixture.get("kickoff") or fixture.get("kickoff_at"))
        if kickoff:
            return kickoff.date()
        return None

    def _parse_datetime(self, value):
        if not value:
            return None
        if hasattr(value, "date") and hasattr(value, "tzinfo"):
            return value if timezone.is_aware(value) else timezone.make_aware(value)
        parsed = parse_datetime(str(value))
        if not parsed:
            return None
        return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)

    def _provider_match_id(self, fixture, source_payload, provider_merge):
        statpal = provider_merge.get("statpal") if isinstance(provider_merge, dict) else {}
        return self._string(
            fixture.get("provider_match_id")
            or fixture.get("statpal_provider_match_id")
            or source_payload.get("provider_match_id")
            or (statpal or {}).get("provider_match_id")
        )

    def _team_id(self, fixture, side, provider_merge):
        statpal = provider_merge.get("statpal") if isinstance(provider_merge, dict) else {}
        return self._string(
            fixture.get(f"{side}_team_id")
            or fixture.get(f"{side}_id")
            or (statpal or {}).get(f"{side}_team_id")
        )

    def _team_name(self, fixture, side):
        teams = fixture.get("teams") if isinstance(fixture.get("teams"), dict) else {}
        team = teams.get(side) if isinstance(teams.get(side), dict) else {}
        return self._string(fixture.get(f"{side}_team") or team.get("name"))

    def _team_logo(self, fixture, side):
        teams = fixture.get("teams") if isinstance(fixture.get("teams"), dict) else {}
        team = teams.get(side) if isinstance(teams.get(side), dict) else {}
        return self._string(fixture.get(f"{side}_logo") or team.get("logo"))

    def _league_name(self, fixture):
        competition = fixture.get("competition_info")
        if isinstance(competition, dict):
            return self._string(fixture.get("league") or fixture.get("competition") or competition.get("name"))
        return self._string(fixture.get("league") or fixture.get("competition"))

    def _market_family(self, market_payload, descriptor):
        return self._string(
            market_payload.get("market_family")
            or market_payload.get("family")
            or descriptor.family
        )

    def _final_confidence(self, market_payload):
        insights = market_payload.get("insights") if isinstance(market_payload.get("insights"), dict) else {}
        council = market_payload.get("council_review") or insights.get("council_review") or {}
        return self._float(
            market_payload.get("final_confidence")
            or (council if isinstance(council, dict) else {}).get("final_confidence")
        )

    def _data_quality(self, market_payload):
        capability = market_payload.get("market_capability")
        coverage = market_payload.get("statpal_market_coverage")
        return self._string(
            (capability if isinstance(capability, dict) else {}).get("data_quality")
            or (coverage if isinstance(coverage, dict) else {}).get("data_quality")
        )

    def _row_rank(self, row: SlipReviewMarketCache):
        confidence = row.final_confidence or row.confidence or row.raw_confidence or 0
        data_quality_rank = {
            "full": 4,
            "good": 3,
            "medium": 2,
            "partial": 1,
            "limited": 1,
            "poor": 0,
        }
        real_odds = 1 if row.odds and row.odds_source and row.odds_source != "estimated" else 0
        return (
            1 if row.eligible else 0,
            int(confidence),
            real_odds,
            data_quality_rank.get(row.data_quality, 0),
        )

    def _join_fixture(self, home, away):
        home = self._string(home)
        away = self._string(away)
        return f"{home} vs {away}" if home and away else ""

    def _string(self, value):
        return str(value or "").strip()

    def _int(self, value):
        try:
            return int(round(float(value or 0)))
        except (TypeError, ValueError):
            return 0

    def _float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _decimal(self, value, *, places):
        if value in (None, ""):
            return None
        try:
            quantizer = Decimal("1." + ("0" * places))
            return Decimal(str(value)).quantize(quantizer)
        except (InvalidOperation, TypeError, ValueError):
            return None
