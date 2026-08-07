from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ProviderFixtureMap, ProviderPlayerMap, ProviderTeamMap, TeamAliasMap
from .services import json_safe, normalize_fixture_text


def _decimal_confidence(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("100")


class ProviderMappingService:
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
        confidence=100,
        resolution_method: str = "provider_id",
        payload: dict[str, Any] | None = None,
    ) -> ProviderTeamMap:
        now = timezone.now()
        defaults = {
            "provider_team_name": provider_team_name,
            "provider_team_normalized": normalize_fixture_text(provider_team_name),
            "internal_team_id": str(internal_team_id or ""),
            "internal_team_name": internal_team_name,
            "internal_team_normalized": normalize_fixture_text(internal_team_name),
            "api_team_id": api_team_id,
            "country": country,
            "confidence": _decimal_confidence(confidence),
            "resolution_method": resolution_method,
            "payload": json_safe(payload or {}),
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
            internal_team_name=internal_team_name or provider_team_name,
            api_team_id=api_team_id,
            country=country,
            confidence=confidence,
        )
        return row

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
