import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from betpreneur.modules.slips.models import SlipReview, SlipReviewEvent
from betpreneur.modules.slips.services import progress as slip_review_redis

log = logging.getLogger(__name__)


def _public_slip_review_status(value):
    return SlipReview.Status.COMPLETED if value == SlipReview.Status.PARTIAL else value


def _public_slip_review_progress(progress):
    progress = dict(progress or {})
    if progress.get("final_status"):
        progress["final_status"] = _public_slip_review_status(progress["final_status"])
    return progress


def _public_slip_review_event(event):
    event = dict(event or {})
    payload = dict(event.get("payload") or {})
    if payload.get("status"):
        payload["status"] = _public_slip_review_status(payload["status"])
    if isinstance(payload.get("progress"), dict):
        payload["progress"] = _public_slip_review_progress(payload["progress"])
    event["payload"] = payload
    return event


class SlipReviewConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.review_id = int(self.scope["url_route"]["kwargs"]["review_id"])
        self.group_name = f"slip_review_{self.review_id}"
        user = self.scope.get("user")
        stream_review_id = self.scope.get("slip_review_stream_review_id")

        if not user or not user.is_authenticated:
            log.warning(
                "Slip review websocket rejected unauthenticated review=%s client=%s",
                self.review_id,
                self.scope.get("client"),
            )
            await self.close(code=4401)
            return

        if int(stream_review_id or 0) != self.review_id:
            log.warning(
                "Slip review websocket rejected ticket review mismatch path_review=%s ticket_review=%s user=%s client=%s",
                self.review_id,
                stream_review_id,
                user.id,
                self.scope.get("client"),
            )
            await self.close(code=4403)
            return

        if self.channel_layer is not None:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(
            {
                "type": "slip_review.snapshot",
                "review_id": self.review_id,
                "status": "connected",
                "progress": {},
                "latest_event_id": None,
                "updated_at": "",
            }
        )
        log.info(
            "Slip review websocket accepted review=%s user=%s client=%s",
            self.review_id,
            user.id,
            self.scope.get("client"),
        )
        await self.send_json(await self._snapshot_payload(self.review_id))

        last_event_id = self._last_event_id()
        events = await self._events_after(self.review_id, last_event_id)
        for event in events:
            await self.send_json(event)

    async def disconnect(self, close_code):
        if self.channel_layer is not None and getattr(self, "group_name", None):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def slip_review_event(self, event):
        await self.send_json(event["payload"])

    def _last_event_id(self):
        query = parse_qs(self.scope.get("query_string", b"").decode())
        raw = (query.get("last_event_id") or ["0"])[0]
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    async def _snapshot_payload(self, review_id):
        redis_snapshot = slip_review_redis.get_snapshot(review_id)
        if redis_snapshot:
            snapshot = dict(redis_snapshot)
            snapshot["status"] = _public_slip_review_status(snapshot.get("status"))
            snapshot["progress"] = _public_slip_review_progress(snapshot.get("progress") or {})
            return snapshot
        review = await SlipReview.objects.filter(id=review_id).only("id", "status", "summary", "updated_at").afirst()
        if not review:
            return {
                "type": "slip_review.snapshot",
                "review_id": review_id,
                "status": "missing",
                "progress": {},
                "latest_event_id": None,
                "updated_at": "",
            }
        summary = review.summary or {}
        progress = summary.get("progress") or {}
        latest_event = await SlipReviewEvent.objects.filter(review_id=review_id).order_by("-id").afirst()
        return {
            "type": "slip_review.snapshot",
            "review_id": review.id,
            "status": _public_slip_review_status(review.status),
            "progress": _public_slip_review_progress(progress),
            "latest_event_id": latest_event.id if latest_event else None,
            "updated_at": review.updated_at.isoformat() if review.updated_at else "",
        }

    async def _events_after(self, review_id, last_event_id):
        redis_events = slip_review_redis.get_events_after(review_id, after_id=last_event_id, limit=100)
        if redis_events is not None:
            return [_public_slip_review_event(event) for event in redis_events]
        queryset = (
            SlipReviewEvent.objects.filter(review_id=review_id, id__gt=last_event_id)
            .order_by("id")[:100]
        )
        events = []
        async for event in queryset:
            events.append(
                _public_slip_review_event({
                    "type": "slip_review.event",
                    "id": event.id,
                    "review_id": review_id,
                    "event_type": event.event_type,
                    "payload": event.payload or {},
                    "created_at": event.created_at.isoformat() if event.created_at else "",
                })
            )
        return events
