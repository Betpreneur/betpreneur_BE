from datetime import UTC, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from betpreneur.modules.catalog.domain.text import normalize_fixture_text
from betpreneur.modules.catalog.models import (
    FixtureCache,
    ProviderFixtureMap,
    ProviderPlayerMap,
    ProviderTeamMap,
    TeamAliasMap,
    TeamProfile,
)
from betpreneur.platform.db.json import json_safe


def _decimal_confidence(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("100")


class ProviderMappingService:
    SPORTYBET_STATPAL_MIN_CONFIDENCE = 78

    def get_fixture(self, *, provider: str, provider_event_id: str) -> ProviderFixtureMap | None:
        if not provider or not provider_event_id:
            return None
        return (
            ProviderFixtureMap.objects.filter(
                provider=provider,
                provider_event_id=str(provider_event_id),
                active=True,
            )
            .order_by("-confidence", "-verified_at", "-updated_at")
            .first()
        )

    def match_sportybet_to_statpal(
        self,
        sportybet_event: dict[str, Any],
        *,
        candidates: list[dict[str, Any]] | None = None,
        persist: bool = True,
        min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a SportyBet event to a cached or supplied StatPal fixture."""
        provider_metadata = self._sportybet_fixture_metadata(sportybet_event)
        if not provider_metadata.get("provider_event_id"):
            return {"matched": False, "reason": "missing_provider_event_id", "provider": provider_metadata, "candidates": []}

        existing = self.get_fixture(provider="sportybet", provider_event_id=provider_metadata["provider_event_id"])
        if existing:
            return {
                "matched": True,
                "existing": True,
                "mapping": existing,
                "provider": provider_metadata,
                "candidate": self._mapping_candidate(existing),
                "candidates": [],
            }

        candidate_rows = candidates if candidates is not None else self._statpal_fixture_candidates(provider_metadata)
        scored = []
        for candidate in candidate_rows:
            normalized_candidate = self._statpal_candidate(candidate)
            if not normalized_candidate:
                continue
            score, method, details = self._score_sportybet_statpal_candidate(provider_metadata, normalized_candidate)
            normalized_candidate.update({"match_score": score, "resolution_method": method, "score_details": details})
            scored.append(normalized_candidate)
        scored.sort(key=lambda item: item.get("match_score") or 0, reverse=True)

        best = scored[0] if scored else None
        threshold = float(min_confidence if min_confidence is not None else self.SPORTYBET_STATPAL_MIN_CONFIDENCE)
        if not best or float(best.get("match_score") or 0) < threshold:
            return {
                "matched": False,
                "reason": "no_candidate_above_threshold",
                "provider": provider_metadata,
                "candidate": best,
                "candidates": scored[:10],
            }

        mapping = self.learn_sportybet_statpal_resolution(provider_metadata=provider_metadata, candidate=best) if persist else None
        return {
            "matched": True,
            "existing": False,
            "mapping": mapping,
            "provider": provider_metadata,
            "candidate": best,
            "candidates": scored[:10],
        }

    def learn_sportybet_statpal_resolution(self, *, provider_metadata: dict[str, Any], candidate: dict[str, Any]) -> ProviderFixtureMap:
        now = timezone.now()
        provider_event_id = str(provider_metadata.get("provider_event_id") or "").strip()
        score = _decimal_confidence(candidate.get("match_score") or 0)
        defaults = {
            "provider_competition_id": str(provider_metadata.get("provider_competition_id") or ""),
            "provider_competition_name": str(provider_metadata.get("competition") or ""),
            "api_fixture_id": str(candidate.get("match_id") or ""),
            "api_league_id": self._int_or_none(candidate.get("provider_competition_id")),
            "api_league_name": str(candidate.get("league") or ""),
            "provider_home_team": str(provider_metadata.get("home_team") or ""),
            "provider_away_team": str(provider_metadata.get("away_team") or ""),
            "api_home_team": str(candidate.get("home_team") or ""),
            "api_away_team": str(candidate.get("away_team") or ""),
            "kickoff_at": candidate.get("kickoff_utc") or None,
            "confidence": score,
            "resolution_method": str(candidate.get("resolution_method") or "sportybet_statpal_team_date"),
            "active": True,
            "payload": json_safe({"provider": provider_metadata, "candidate": candidate}),
            "verified_at": now,
        }
        try:
            with transaction.atomic():
                mapping, _ = ProviderFixtureMap.objects.update_or_create(
                    provider="sportybet",
                    provider_event_id=provider_event_id,
                    defaults=defaults,
                )
        except IntegrityError:
            mapping = ProviderFixtureMap.objects.get(provider="sportybet", provider_event_id=provider_event_id)

        self._learn_team_alias(
            provider="sportybet",
            provider_team_name=str(provider_metadata.get("home_team") or ""),
            internal_team_name=str(candidate.get("home_team") or ""),
            country=str(candidate.get("country") or ""),
            confidence=score,
        )
        self._learn_team_alias(
            provider="sportybet",
            provider_team_name=str(provider_metadata.get("away_team") or ""),
            internal_team_name=str(candidate.get("away_team") or ""),
            country=str(candidate.get("country") or ""),
            confidence=score,
        )
        return mapping

    def _sportybet_fixture_metadata(self, event: dict[str, Any]) -> dict[str, Any]:
        event = event or {}
        sport = event.get("sport") if isinstance(event.get("sport"), dict) else {}
        category = sport.get("category") if isinstance(sport.get("category"), dict) else {}
        tournament = category.get("tournament") if isinstance(category.get("tournament"), dict) else {}
        kickoff_at = self._sportybet_kickoff(event)
        return {
            "provider": "sportybet",
            "provider_event_id": str(event.get("eventId") or event.get("event_id") or "").strip(),
            "provider_game_id": str(event.get("gameId") or event.get("game_id") or "").strip(),
            "provider_competition_id": str(tournament.get("id") or category.get("id") or "").strip(),
            "competition": str(tournament.get("name") or category.get("name") or "").strip(),
            "country": str(category.get("name") or "").strip(),
            "home_team": str(event.get("homeTeamName") or event.get("home_team") or "").strip(),
            "away_team": str(event.get("awayTeamName") or event.get("away_team") or "").strip(),
            "kickoff_at": kickoff_at,
            "match_date": kickoff_at.date() if kickoff_at else None,
            "raw": event,
        }

    def _sportybet_kickoff(self, event: dict[str, Any]):
        value = event.get("estimateStartTime") or event.get("startTime") or event.get("kickoff_at")
        if value in ("", None):
            return None
        parsed = parse_datetime(str(value)) if isinstance(value, str) else None
        if parsed:
            return parsed if parsed.tzinfo else timezone.make_aware(parsed)
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (TypeError, ValueError, OSError):
            return None

    def _statpal_fixture_candidates(self, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        match_date = metadata.get("match_date")
        queryset = FixtureCache.objects.filter(source="statpal")
        if match_date:
            queryset = queryset.filter(match_date__range=(match_date - timedelta(days=1), match_date + timedelta(days=1)))
        tokens = [
            token
            for token in normalize_fixture_text(f"{metadata.get('home_team')} {metadata.get('away_team')}").split()
            if len(token) > 2
        ]
        rows = {}
        if tokens:
            query = Q()
            for token in tokens[:6]:
                query |= Q(fixture_normalized__icontains=token)
                query |= Q(home_team_normalized__icontains=token)
                query |= Q(away_team_normalized__icontains=token)
            for row in queryset.filter(query)[:200]:
                rows[row.pk] = row

        # An exact provider id is the strongest signal we have, and it was gated behind
        # the fuzzy name filter above: a fixture whose ids match but whose team names the
        # two providers spell differently never became a candidate, so the id match had
        # nothing to score against. `fallback_match_ids` is a JSON list and list-membership
        # lookups are not portable across backends, so the id sweep is done in Python over
        # the date-bounded set rather than in SQL.
        event_ids = set(self._provider_event_ids(metadata))
        if event_ids:
            for row in queryset[:500]:
                payload = row.api_payload or {}
                candidate_ids = {
                    str(payload.get("provider_match_id") or "").strip(),
                    *[str(item).strip() for item in (payload.get("fallback_match_ids") or [])],
                } - {""}
                if candidate_ids & event_ids:
                    rows[row.pk] = row

        return [self._fixture_candidate(row) for row in list(rows.values())[:200]]

    @staticmethod
    def _provider_event_ids(metadata: dict[str, Any]) -> list[str]:
        return [
            value
            for value in (
                str(metadata.get("provider_event_id") or "").strip(),
                str(metadata.get("provider_game_id") or "").strip(),
            )
            if value
        ]

    def _fixture_candidate(self, fixture: FixtureCache) -> dict[str, Any]:
        payload = fixture.api_payload or {}
        return {
            "match_id": fixture.match_id,
            "provider_match_id": str(payload.get("provider_match_id") or "").strip(),
            "fallback_match_ids": payload.get("fallback_match_ids") or [],
            "provider_competition_id": str(payload.get("provider_competition_id") or "").strip(),
            "league": fixture.league,
            "country": fixture.country,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "home_team_id": str(payload.get("provider_home_team_id") or payload.get("hid") or "").strip(),
            "away_team_id": str(payload.get("provider_away_team_id") or payload.get("aid") or "").strip(),
            "kickoff_utc": fixture.kickoff_utc,
            "match_date": fixture.match_date,
            "source": fixture.source,
            "raw": payload,
        }

    def _statpal_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = candidate or {}
        match_id = str(candidate.get("match_id") or "").strip()
        provider_match_id = str(candidate.get("provider_match_id") or "").strip()
        if not match_id and provider_match_id:
            match_id = f"statpal:{provider_match_id}"
        if not match_id:
            return {}
        return {
            "match_id": match_id,
            "provider_match_id": provider_match_id or match_id.replace("statpal:", "", 1),
            "fallback_match_ids": [str(item).strip() for item in (candidate.get("fallback_match_ids") or []) if str(item or "").strip()],
            "provider_competition_id": str(candidate.get("provider_competition_id") or candidate.get("league_id") or "").strip(),
            "league": str(candidate.get("league") or "").strip(),
            "country": str(candidate.get("country") or "").strip(),
            "home_team": str(candidate.get("home_team") or candidate.get("hname") or "").strip(),
            "away_team": str(candidate.get("away_team") or candidate.get("aname") or "").strip(),
            "home_team_id": str(candidate.get("home_team_id") or candidate.get("hid") or "").strip(),
            "away_team_id": str(candidate.get("away_team_id") or candidate.get("aid") or "").strip(),
            "kickoff_utc": candidate.get("kickoff_utc"),
            "match_date": candidate.get("match_date"),
            "raw": candidate,
        }

    def _score_sportybet_statpal_candidate(self, metadata: dict[str, Any], candidate: dict[str, Any]):
        home = normalize_fixture_text(metadata.get("home_team"))
        away = normalize_fixture_text(metadata.get("away_team"))
        candidate_home = normalize_fixture_text(candidate.get("home_team"))
        candidate_away = normalize_fixture_text(candidate.get("away_team"))
        direct = (self._name_similarity(home, candidate_home) + self._name_similarity(away, candidate_away)) / 2
        reversed_score = (self._name_similarity(home, candidate_away) + self._name_similarity(away, candidate_home)) / 2
        orientation = "reversed" if reversed_score > direct else "direct"
        team_score = max(direct, reversed_score)

        league_score = self._name_similarity(normalize_fixture_text(metadata.get("competition")), normalize_fixture_text(candidate.get("league")))
        date_score = self._date_score(metadata.get("match_date"), candidate.get("match_date"))
        time_score = self._time_score(metadata.get("kickoff_at"), candidate.get("kickoff_utc"))
        id_score = self._id_score(metadata, candidate)

        if id_score >= 1:
            score = 100.0
            method = "sportybet_statpal_provider_id"
        else:
            score = (team_score * 70) + (date_score * 15) + (league_score * 10) + (time_score * 5)
            method = f"sportybet_statpal_team_date_{orientation}"
        return round(score, 2), method, {
            "team_score": round(team_score * 100, 2),
            "league_score": round(league_score * 100, 2),
            "date_score": round(date_score * 100, 2),
            "time_score": round(time_score * 100, 2),
            "id_score": round(id_score * 100, 2),
            "orientation": orientation,
        }

    @staticmethod
    def _name_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
        fuzzy = SequenceMatcher(None, left, right).ratio()
        return max(token_score, fuzzy)

    @staticmethod
    def _date_score(left, right) -> float:
        if not left or not right:
            return 0.0
        delta = abs((left - right).days)
        if delta == 0:
            return 1.0
        if delta == 1:
            return 0.35
        return 0.0

    @staticmethod
    def _time_score(left, right) -> float:
        if not left or not right:
            return 0.0
        if timezone.is_naive(right):
            right = timezone.make_aware(right)
        if timezone.is_naive(left):
            left = timezone.make_aware(left)
        minutes = abs((left - right).total_seconds()) / 60
        if minutes <= 10:
            return 1.0
        if minutes <= 60:
            return 0.75
        if minutes <= 180:
            return 0.35
        return 0.0

    @staticmethod
    def _id_score(metadata: dict[str, Any], candidate: dict[str, Any]) -> float:
        ids = {
            str(metadata.get("provider_event_id") or "").strip(),
            str(metadata.get("provider_game_id") or "").strip(),
        } - {""}
        candidate_ids = {
            str(candidate.get("provider_match_id") or "").strip(),
            str(candidate.get("match_id") or "").replace("statpal:", "", 1).strip(),
            *[str(item).strip() for item in candidate.get("fallback_match_ids") or []],
        } - {""}
        return 1.0 if ids & candidate_ids else 0.0

    @staticmethod
    def _mapping_candidate(mapping: ProviderFixtureMap) -> dict[str, Any]:
        candidate = ((mapping.payload or {}).get("candidate") or {}) if isinstance(mapping.payload, dict) else {}
        cached_payload = {}
        if not (candidate.get("home_team_id") and candidate.get("away_team_id")):
            try:
                cached_payload = (
                    FixtureCache.objects.filter(match_id=str(mapping.api_fixture_id or ""))
                    .values_list("api_payload", flat=True)
                    .first()
                    or {}
                )
            except Exception:
                cached_payload = {}
        return {
            "match_id": mapping.api_fixture_id,
            "provider_match_id": candidate.get("provider_match_id") or cached_payload.get("provider_match_id") or "",
            "fallback_match_ids": candidate.get("fallback_match_ids") or cached_payload.get("fallback_match_ids") or [],
            "provider_competition_id": str(
                candidate.get("provider_competition_id") or cached_payload.get("provider_competition_id") or mapping.api_league_id or ""
            ),
            "league": mapping.api_league_name,
            "country": str(candidate.get("country") or cached_payload.get("country") or ""),
            "home_team": mapping.api_home_team,
            "away_team": mapping.api_away_team,
            "home_team_id": str(candidate.get("home_team_id") or cached_payload.get("provider_home_team_id") or cached_payload.get("hid") or ""),
            "away_team_id": str(candidate.get("away_team_id") or cached_payload.get("provider_away_team_id") or cached_payload.get("aid") or ""),
            "kickoff_utc": mapping.kickoff_at,
            "match_date": mapping.kickoff_at.date() if mapping.kickoff_at else candidate.get("match_date"),
            "source": "statpal",
            "match_score": float(mapping.confidence or 0),
        }

    @staticmethod
    def _int_or_none(value):
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def get_team(self, *, provider: str, provider_team_id: str = "", provider_team_name: str = "") -> ProviderTeamMap | None:
        qs = ProviderTeamMap.objects.filter(provider=provider, active=True)
        if provider_team_id:
            found = qs.filter(provider_team_id=str(provider_team_id)).first()
            if found:
                return found
        if provider_team_name:
            normalized = normalize_fixture_text(provider_team_name)
            found = qs.filter(provider_team_normalized=normalized).order_by("-confidence", "-updated_at").first()
            if found:
                return found
            alias = (
                TeamAliasMap.objects.filter(
                    provider=provider,
                    alias_normalized=normalized,
                    active=True,
                )
                .order_by("-confidence", "-last_seen_at", "-updated_at")
                .first()
            )
            if alias:
                return (
                    qs.filter(internal_team_normalized=alias.canonical_normalized)
                    .order_by("-confidence", "-updated_at")
                    .first()
                )
        return None

    def get_player(
        self,
        *,
        provider: str,
        provider_player_id: str = "",
        provider_player_name: str = "",
        provider_team_id: str = "",
    ) -> ProviderPlayerMap | None:
        qs = ProviderPlayerMap.objects.filter(provider=provider, active=True)
        if provider_player_id:
            found = qs.filter(provider_player_id=str(provider_player_id)).first()
            if found:
                return found
        if provider_player_name:
            normalized = normalize_fixture_text(provider_player_name)
            qs = qs.filter(provider_player_normalized=normalized)
            if provider_team_id:
                qs = qs.filter(provider_team_id=str(provider_team_id))
            return qs.order_by("-confidence", "-updated_at").first()
        return None

    def learn_team(
        self,
        *,
        provider: str,
        provider_team_id: str,
        provider_team_name: str,
        internal_team_id: str = "",
        internal_team_name: str = "",
        api_team_id: int | None = None,
        country: str = "",
        league_key: str = "",
        league_name: str = "",
        provider_league_id: str = "",
        season: str = "",
        aliases: list[str] | tuple[str, ...] | None = None,
        confidence=100,
        resolution_method: str = "provider_id",
        payload: dict[str, Any] | None = None,
    ) -> ProviderTeamMap:
        now = timezone.now()
        team = self.link_provider_team_identity(
            provider=provider,
            provider_team_id=provider_team_id,
            provider_team_name=provider_team_name,
            canonical_name=internal_team_name or provider_team_name,
            api_team_id=api_team_id,
            country=country,
            league_key=league_key,
            league_name=league_name,
            provider_league_id=provider_league_id,
            season=season,
            aliases=aliases,
            confidence=confidence,
            resolution_method=resolution_method,
            payload=payload,
        )
        defaults = {
            "provider_team_name": provider_team_name,
            "provider_team_normalized": normalize_fixture_text(provider_team_name),
            "internal_team_id": str(team.pk or internal_team_id or ""),
            "internal_team_name": team.canonical_name,
            "internal_team_normalized": team.canonical_normalized,
            "api_team_id": api_team_id,
            "country": country,
            "confidence": _decimal_confidence(confidence),
            "resolution_method": resolution_method,
            "payload": json_safe(
                {
                    **(payload or {}),
                    "team_profile_id": team.pk,
                    "legacy_internal_team_id": str(internal_team_id or ""),
                    "league_key": league_key,
                    "league_name": league_name,
                    "provider_league_id": str(provider_league_id or ""),
                    "season": str(season or ""),
                }
            ),
            "active": True,
            "verified_at": now,
        }
        try:
            with transaction.atomic():
                row, _ = ProviderTeamMap.objects.update_or_create(
                    provider=provider,
                    provider_team_id=str(provider_team_id),
                    defaults=defaults,
                )
        except IntegrityError:
            row = ProviderTeamMap.objects.get(provider=provider, provider_team_id=str(provider_team_id))
        self._learn_team_alias(
            provider=provider,
            provider_team_name=provider_team_name,
            internal_team_name=team.canonical_name,
            api_team_id=api_team_id,
            country=country,
            confidence=confidence,
        )
        return row

    def link_provider_team_identity(
        self,
        *,
        provider: str,
        provider_team_id: str,
        provider_team_name: str,
        canonical_name: str = "",
        api_team_id: int | None = None,
        country: str = "",
        league_key: str = "",
        league_name: str = "",
        provider_league_id: str = "",
        season: str = "",
        aliases: list[str] | tuple[str, ...] | None = None,
        confidence=100,
        resolution_method: str = "provider_id",
        payload: dict[str, Any] | None = None,
    ) -> TeamProfile:
        """Link one provider's team row to the canonical TeamProfile identity."""

        canonical = str(canonical_name or provider_team_name or "").strip()
        if not canonical:
            raise ValueError("canonical_name or provider_team_name is required")

        normalized = normalize_fixture_text(canonical)
        provider_key = self._provider_key(provider)
        provider_team_id = str(provider_team_id or "").strip()
        inferred_api_team_id = self._api_team_id_for_provider(provider_key, provider_team_id, api_team_id)

        with transaction.atomic():
            team, created = TeamProfile.objects.select_for_update().get_or_create(
                canonical_normalized=normalized,
                defaults={
                    "canonical_name": canonical,
                    "country": country,
                    "primary_league_key": league_key,
                    "primary_league_name": league_name,
                    "provider_ids": {},
                    "aliases": [],
                    "metadata": {},
                    "active": True,
                },
            )
            provider_ids = self._provider_ids_with_mapping(
                team.provider_ids,
                provider=provider_key,
                provider_team_id=provider_team_id,
                provider_league_id=str(provider_league_id or ""),
                season=str(season or ""),
                api_team_id=inferred_api_team_id,
            )
            alias_values = self._merged_aliases(team.aliases, [provider_team_name, *(aliases or [])])
            metadata = {
                **(team.metadata or {}),
                "last_provider_mapping": {
                    "provider": provider_key,
                    "team_id": provider_team_id,
                    "league_id": str(provider_league_id or ""),
                    "season": str(season or ""),
                    "confidence": str(_decimal_confidence(confidence)),
                    "resolution_method": resolution_method,
                },
            }
            if payload:
                metadata["last_provider_payload"] = json_safe(payload)

            update_fields = ["provider_ids", "aliases", "metadata", "active", "updated_at"]
            team.provider_ids = provider_ids
            team.aliases = alias_values
            team.metadata = json_safe(metadata)
            team.active = True
            if country and not team.country:
                team.country = country
                update_fields.append("country")
            if league_key and (created or not team.primary_league_key):
                team.primary_league_key = league_key
                update_fields.append("primary_league_key")
            if league_name and (created or not team.primary_league_name):
                team.primary_league_name = league_name
                update_fields.append("primary_league_name")
            team.save(update_fields=update_fields)

        for alias in [provider_team_name, *(aliases or [])]:
            self._learn_team_alias(
                provider=provider_key,
                provider_team_name=str(alias or "").strip(),
                internal_team_name=team.canonical_name,
                api_team_id=inferred_api_team_id,
                country=country or team.country,
                confidence=confidence,
            )
        return team

    @staticmethod
    def _provider_key(provider: str) -> str:
        return str(provider or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _api_team_id_for_provider(provider: str, provider_team_id: str, api_team_id: int | None) -> int | None:
        if api_team_id is not None:
            return api_team_id
        if provider in {"api_football", "apifootball"}:
            try:
                return int(provider_team_id)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _provider_ids_with_mapping(
        existing: dict[str, Any] | None,
        *,
        provider: str,
        provider_team_id: str,
        provider_league_id: str,
        season: str,
        api_team_id: int | None,
    ) -> dict[str, Any]:
        provider_ids = dict(existing or {})
        current = provider_ids.get(provider) or {}
        if not isinstance(current, dict):
            current = {"team_id": str(current)}
        if provider_team_id:
            current["team_id"] = provider_team_id
        if provider_league_id:
            current["league_id"] = provider_league_id
        if season:
            current["season"] = season
        if api_team_id is not None:
            current["api_team_id"] = api_team_id
        provider_ids[provider] = current
        return json_safe(provider_ids)

    @staticmethod
    def _merged_aliases(existing: list[Any] | None, aliases: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for value in [*(existing or []), *aliases]:
            alias = str(value or "").strip()
            key = normalize_fixture_text(alias)
            if alias and key and key not in seen:
                seen.add(key)
                merged.append(alias)
        return merged

    def learn_player(
        self,
        *,
        provider: str,
        provider_player_id: str,
        provider_player_name: str,
        internal_player_id: str = "",
        internal_player_name: str = "",
        provider_team_id: str = "",
        provider_team_name: str = "",
        internal_team_id: str = "",
        internal_team_name: str = "",
        position: str = "",
        nationality: str = "",
        confidence=100,
        resolution_method: str = "provider_id",
        payload: dict[str, Any] | None = None,
    ) -> ProviderPlayerMap:
        now = timezone.now()
        defaults = {
            "provider_player_name": provider_player_name,
            "provider_player_normalized": normalize_fixture_text(provider_player_name),
            "internal_player_id": str(internal_player_id or ""),
            "internal_player_name": internal_player_name,
            "internal_player_normalized": normalize_fixture_text(internal_player_name),
            "provider_team_id": str(provider_team_id or ""),
            "provider_team_name": provider_team_name,
            "internal_team_id": str(internal_team_id or ""),
            "internal_team_name": internal_team_name,
            "position": position,
            "nationality": nationality,
            "confidence": _decimal_confidence(confidence),
            "resolution_method": resolution_method,
            "payload": json_safe(payload or {}),
            "active": True,
            "verified_at": now,
        }
        try:
            with transaction.atomic():
                row, _ = ProviderPlayerMap.objects.update_or_create(
                    provider=provider,
                    provider_player_id=str(provider_player_id),
                    defaults=defaults,
                )
        except IntegrityError:
            row = ProviderPlayerMap.objects.get(provider=provider, provider_player_id=str(provider_player_id))
        return row

    def learn_statpal_player_payload(self, payload: dict[str, Any]) -> ProviderPlayerMap | None:
        player = (payload or {}).get("player") or {}
        player_id = str(player.get("id") or "")
        player_name = player.get("name") or " ".join(
            item for item in [player.get("firstname"), player.get("lastname")] if item
        ).strip()
        if not player_id or not player_name:
            return None
        team_id = str(player.get("team_id") or "")
        team_name = player.get("team") or ""
        if team_id and team_name:
            self.learn_team(
                provider="statpal",
                provider_team_id=team_id,
                provider_team_name=team_name,
                internal_team_id=team_id,
                internal_team_name=team_name,
                confidence=100,
                resolution_method="statpal_player_payload",
                payload={"source": "player_payload"},
            )
        return self.learn_player(
            provider="statpal",
            provider_player_id=player_id,
            provider_player_name=player_name,
            internal_player_id=player_id,
            internal_player_name=player_name,
            provider_team_id=team_id,
            provider_team_name=team_name,
            internal_team_id=team_id,
            internal_team_name=team_name,
            position=player.get("position") or "",
            nationality=player.get("nationality") or "",
            confidence=100,
            resolution_method="statpal_player_payload",
            payload=payload,
        )

    @staticmethod
    def _learn_team_alias(
        *,
        provider: str,
        provider_team_name: str,
        internal_team_name: str,
        api_team_id: int | None = None,
        country: str = "",
        confidence=100,
    ) -> None:
        if not provider_team_name or not internal_team_name:
            return
        TeamAliasMap.objects.update_or_create(
            provider=provider,
            alias_normalized=normalize_fixture_text(provider_team_name),
            canonical_normalized=normalize_fixture_text(internal_team_name),
            defaults={
                "alias": provider_team_name,
                "canonical_name": internal_team_name,
                "api_team_id": api_team_id,
                "country": country,
                "confidence": _decimal_confidence(confidence),
                "source": "provider_mapping",
                "active": True,
                "last_seen_at": timezone.now(),
            },
        )


provider_mapping_service = ProviderMappingService()
