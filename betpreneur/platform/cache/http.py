"""ETag and Cache-Control for read endpoints.

Lifted verbatim from apps/algo/views.py. The details matter and are not to be
"improved" casually:

* the ETag is **sha256** — changing the hash changes every ETag in flight and
  invalidates every client's cache at once;
* a 304 is an `HttpResponseNotModified`, not a `Response(status=304)`;
* private responses carry `Vary: Authorization, Cookie`, without which a shared
  cache can serve one user's payload to another.
"""
from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.http import HttpResponseNotModified
from rest_framework import status
from rest_framework.response import Response

DEFAULT_TTL_SETTING = "ALGO_READ_CACHE_SECONDS"
DEFAULT_TTL = 300


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
    ttl = int(seconds if seconds is not None else getattr(settings, DEFAULT_TTL_SETTING, DEFAULT_TTL))
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
        payload, request=request, seconds=seconds, status_code=status_code, private=True,
    )


def public_cached_response(payload, *, request=None, seconds=None, status_code=status.HTTP_200_OK):
    return cached_response(
        payload, request=request, seconds=seconds, status_code=status_code, private=False,
    )
