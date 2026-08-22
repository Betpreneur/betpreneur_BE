"""Slip-review leg analysis cache operations."""

from datetime import timedelta
import hashlib
import json
import os
import time

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from apps.algo.models import SlipLegAnalysisCache
from apps.algo.slip_review.lifecycle import json_safe
from apps.algo.slip_review.market_descriptors import selection_market_descriptor
from apps.algo.slip_review.provider_payloads import provider_match_date, provider_metadata
from apps.algo.markets.api import normalize_market_text


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


LEG_CACHE_TTL_SECONDS = env_int("SLIP_REVIEW_LEG_CACHE_TTL_SECONDS", 15 * 60)
LEG_CACHE_LOCK_SECONDS = env_int("SLIP_REVIEW_LEG_CACHE_LOCK_SECONDS", 5 * 60)
LEG_CACHE_WAIT_SECONDS = env_int("SLIP_REVIEW_LEG_CACHE_WAIT_SECONDS", 45)


def cached_slip_leg_payload(cached, cache_key, *, status="hit"):
    payload = dict(cached.payload or {})
    payload["analysis_cache"] = {
        "status": status,
        "cache_key": cache_key,
        "updated_at": cached.updated_at.isoformat() if cached.updated_at else "",
        "expires_at": cached.expires_at.isoformat() if cached.expires_at else "",
    }
    return payload


def slip_leg_analysis_cache_key(selection):
    selection = selection or {}
    provider_payload = selection.get("provider_payload") or {}
    metadata = provider_metadata(selection)
    descriptor = selection_market_descriptor(selection, selection.get("market", ""))
    market_key = (
        getattr(descriptor, "code", "")
        or getattr(descriptor, "canonical", "")
        or selection.get("market")
        or ""
    )
    match_date = provider_match_date(selection)
    raw_key = {
        "provider": str(selection.get("provider") or metadata.get("provider") or "").lower(),
        "provider_event_id": metadata.get("provider_event_id") or "",
        "provider_competition_id": metadata.get("provider_competition_id") or "",
        "provider_date": match_date.isoformat() if match_date else "",
        "match": normalize_market_text(selection.get("match") or ""),
        "market": normalize_market_text(market_key),
        "odds": str(provider_payload.get("odds") or provider_payload.get("displayOdds") or ""),
        "market_id": str(provider_payload.get("marketId") or provider_payload.get("market_id") or ""),
        "outcome_id": str(provider_payload.get("outcomeId") or provider_payload.get("outcome_id") or ""),
        "specifier": str(provider_payload.get("specifier") or ""),
    }
    encoded = json.dumps(raw_key, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), raw_key


def get_or_lock_slip_leg_analysis_cache(selection, *, cache_key_builder=None):
    cache_key_builder = cache_key_builder or slip_leg_analysis_cache_key
    cache_key, raw_key = cache_key_builder(selection)
    now = timezone.now()
    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if (
        cached
        and cached.status == SlipLegAnalysisCache.Status.READY
        and cached.expires_at > now
        and cached.payload
    ):
        return cached_slip_leg_payload(cached, cache_key), cache_key, raw_key, False

    lock_until = now + timedelta(seconds=max(30, LEG_CACHE_LOCK_SECONDS))
    expires_at = now + timedelta(seconds=max(60, LEG_CACHE_TTL_SECONDS))
    if not cached:
        try:
            SlipLegAnalysisCache.objects.create(
                cache_key=cache_key,
                status=SlipLegAnalysisCache.Status.PROCESSING,
                source=raw_key.get("provider") or "",
                provider_event_id=raw_key.get("provider_event_id") or "",
                match_text=(selection or {}).get("match") or "",
                market_text=(selection or {}).get("market") or "",
                payload={},
                expires_at=expires_at,
                lock_expires_at=lock_until,
            )
            return None, cache_key, raw_key, True
        except IntegrityError:
            cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()

    now = timezone.now()
    if cached and cached.status == SlipLegAnalysisCache.Status.PROCESSING and cached.lock_expires_at and cached.lock_expires_at > now:
        deadline = time.monotonic() + max(0, LEG_CACHE_WAIT_SECONDS)
        while time.monotonic() < deadline:
            time.sleep(1)
            cached.refresh_from_db()
            if cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
                return cached_slip_leg_payload(cached, cache_key, status="wait_hit"), cache_key, raw_key, False

    updated = (
        SlipLegAnalysisCache.objects.filter(cache_key=cache_key)
        .filter(
            Q(lock_expires_at__lte=timezone.now())
            | Q(lock_expires_at__isnull=True)
            | Q(
                status__in=[
                    SlipLegAnalysisCache.Status.READY,
                    SlipLegAnalysisCache.Status.FAILED,
                ]
            )
        )
        .update(
            status=SlipLegAnalysisCache.Status.PROCESSING,
            lock_expires_at=lock_until,
            expires_at=expires_at,
        )
    )
    if updated:
        return None, cache_key, raw_key, True

    cached = SlipLegAnalysisCache.objects.filter(cache_key=cache_key).first()
    if cached and cached.status == SlipLegAnalysisCache.Status.READY and cached.expires_at > timezone.now() and cached.payload:
        return cached_slip_leg_payload(cached, cache_key, status="late_hit"), cache_key, raw_key, False
    return None, cache_key, raw_key, True


def store_slip_leg_analysis_cache(selection, result, *, cache_key_builder=None, cache_key=None, raw_key=None):
    result = json_safe(result or {})
    if result.get("status") not in {"analysed", "market_not_found", "insufficient_data"}:
        return
    cache_key_builder = cache_key_builder or slip_leg_analysis_cache_key
    cache_key = cache_key or cache_key_builder(selection)[0]
    raw_key = raw_key or cache_key_builder(selection)[1]
    matched = result.get("matched_fixture") or {}
    expires_at = timezone.now() + timedelta(seconds=max(60, LEG_CACHE_TTL_SECONDS))
    SlipLegAnalysisCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "status": SlipLegAnalysisCache.Status.READY,
            "source": raw_key.get("provider") or "",
            "provider_event_id": raw_key.get("provider_event_id") or "",
            "match_text": result.get("match") or (selection or {}).get("match") or "",
            "market_text": result.get("submitted_market") or (selection or {}).get("market") or "",
            "match_id": matched.get("match_id") or "",
            "payload": result,
            "expires_at": expires_at,
            "lock_expires_at": None,
        },
    )


def mark_slip_leg_analysis_cache_failed(selection, *, cache_key_builder=None, cache_key=None):
    cache_key_builder = cache_key_builder or slip_leg_analysis_cache_key
    cache_key = cache_key or cache_key_builder(selection)[0]
    SlipLegAnalysisCache.objects.filter(cache_key=cache_key).update(
        status=SlipLegAnalysisCache.Status.FAILED,
        lock_expires_at=None,
        expires_at=timezone.now() + timedelta(seconds=60),
    )


__all__ = [
    "cached_slip_leg_payload",
    "get_or_lock_slip_leg_analysis_cache",
    "mark_slip_leg_analysis_cache_failed",
    "slip_leg_analysis_cache_key",
    "store_slip_leg_analysis_cache",
]
