"""Shared API response helpers for algo endpoints."""

import hashlib
import json

from django.conf import settings
from django.http import HttpResponseNotModified
from rest_framework import status
from rest_framework.response import Response


def json_safe(value):
    return json.loads(json.dumps(value, default=str))


def strip_api_usage(value):
    if isinstance(value, dict):
        return {
            key: strip_api_usage(child)
            for key, child in value.items()
            if key != "api_usage"
        }
    if isinstance(value, list):
        return [strip_api_usage(item) for item in value]
    return value


def api_response_payload(value):
    return strip_api_usage(json_safe(value))


def payload_etag(payload):
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f'"{hashlib.sha256(raw).hexdigest()}"'


def cached_response(
    payload,
    *,
    request=None,
    seconds=None,
    status_code=status.HTTP_200_OK,
    private=True,
):
    ttl = int(seconds if seconds is not None else getattr(settings, "ALGO_READ_CACHE_SECONDS", 300))
    etag = payload_etag(payload)
    if request is not None and request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = Response(payload, status=status_code)
    response["ETag"] = etag
    visibility = "private" if private else "public"
    response["Cache-Control"] = (
        f"{visibility}, max-age={ttl}, stale-while-revalidate={ttl}, stale-if-error=86400"
    )
    if private:
        response["Vary"] = "Authorization, Cookie"
    return response


def private_cached_response(payload, *, request=None, seconds=None, status_code=status.HTTP_200_OK):
    return cached_response(
        payload,
        request=request,
        seconds=seconds,
        status_code=status_code,
        private=True,
    )


def public_cached_response(payload, *, request=None, seconds=None, status_code=status.HTTP_200_OK):
    return cached_response(
        payload,
        request=request,
        seconds=seconds,
        status_code=status_code,
        private=False,
    )


__all__ = [
    "api_response_payload",
    "cached_response",
    "json_safe",
    "payload_etag",
    "private_cached_response",
    "public_cached_response",
    "strip_api_usage",
]
