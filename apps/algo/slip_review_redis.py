import json
import logging

import redis
from django.conf import settings


log = logging.getLogger(__name__)

_client = None


def _enabled():
    return bool(getattr(settings, "SLIP_REVIEW_REDIS_PROGRESS_ENABLED", True))


def _ttl():
    return max(60, int(getattr(settings, "SLIP_REVIEW_REDIS_PROGRESS_TTL_SECONDS", 3600)))


def _max_events():
    return max(20, int(getattr(settings, "SLIP_REVIEW_REDIS_MAX_EVENTS", 300)))


def _url():
    return (
        getattr(settings, "SLIP_REVIEW_REDIS_URL", "")
        or getattr(settings, "CELERY_BROKER_URL", "")
    )


def client():
    global _client
    if not _enabled():
        return None
    if _client is None:
        _client = redis.Redis.from_url(_url(), decode_responses=True)
    return _client


def _snapshot_key(review_id):
    return f"slip_review:{review_id}:snapshot"


def _events_key(review_id):
    return f"slip_review:{review_id}:events"


def _safe_json(payload):
    return json.dumps(payload or {}, default=str, separators=(",", ":"))


def store_snapshot(review_id, payload):
    try:
        conn = client()
        if not conn:
            return False
        conn.setex(_snapshot_key(review_id), _ttl(), _safe_json(payload))
        return True
    except Exception:
        log.exception("Slip review Redis snapshot write failed review=%s", review_id)
        return False


def get_snapshot(review_id):
    try:
        conn = client()
        if not conn:
            return None
        raw = conn.get(_snapshot_key(review_id))
        return json.loads(raw) if raw else None
    except Exception:
        log.exception("Slip review Redis snapshot read failed review=%s", review_id)
        return None


def push_event(review_id, event_payload, *, snapshot=None):
    try:
        conn = client()
        if not conn:
            return False
        pipe = conn.pipeline()
        key = _events_key(review_id)
        pipe.rpush(key, _safe_json(event_payload))
        pipe.ltrim(key, -_max_events(), -1)
        pipe.expire(key, _ttl())
        if snapshot:
            pipe.setex(_snapshot_key(review_id), _ttl(), _safe_json(snapshot))
        pipe.execute()
        return True
    except Exception:
        log.exception("Slip review Redis event write failed review=%s", review_id)
        return False


def get_events_after(review_id, after_id=0, limit=100):
    try:
        conn = client()
        if not conn:
            return None
        raw_items = conn.lrange(_events_key(review_id), 0, -1)
        events = []
        after_id = max(0, int(after_id or 0))
        limit = max(1, int(limit or 100))
        for raw in raw_items:
            event = json.loads(raw)
            try:
                event_id = int(event.get("id") or 0)
            except (TypeError, ValueError):
                event_id = 0
            if event_id > after_id:
                events.append(event)
            if len(events) >= limit:
                break
        return events
    except Exception:
        log.exception("Slip review Redis event read failed review=%s", review_id)
        return None
