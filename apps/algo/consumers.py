from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import SlipReview, SlipReviewEvent


class SlipReviewConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.review_id = int(self.scope["url_route"]["kwargs"]["review_id"])
        self.group_name = f"slip_review_{self.review_id}"
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        if not await self._can_access_review(self.review_id, user.id):
            await self.close(code=4404)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(await self._snapshot_payload(self.review_id))

        last_event_id = self._last_event_id()
        events = await self._events_after(self.review_id, last_event_id)
        for event in events:
            await self.send_json(event)

    async def disconnect(self, close_code):
        if getattr(self, "group_name", None):
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

    @database_sync_to_async
    def _can_access_review(self, review_id, user_id):
        return SlipReview.objects.filter(id=review_id, user_id=user_id).exists()

    @database_sync_to_async
    def _snapshot_payload(self, review_id):
        review = SlipReview.objects.filter(id=review_id).only("id", "status", "summary", "updated_at").first()
        summary = review.summary or {}
        progress = summary.get("progress") or {}
        latest_event = SlipReviewEvent.objects.filter(review_id=review_id).order_by("-id").first()
        return {
            "type": "slip_review.snapshot",
            "review_id": review.id,
            "status": review.status,
            "progress": progress,
            "latest_event_id": latest_event.id if latest_event else None,
            "updated_at": review.updated_at.isoformat() if review.updated_at else "",
        }

    @database_sync_to_async
    def _events_after(self, review_id, last_event_id):
        queryset = (
            SlipReviewEvent.objects.filter(review_id=review_id, id__gt=last_event_id)
            .order_by("id")[:100]
        )
        return [
            {
                "type": "slip_review.event",
                "id": event.id,
                "review_id": review_id,
                "event_type": event.event_type,
                "payload": event.payload or {},
                "created_at": event.created_at.isoformat() if event.created_at else "",
            }
            for event in queryset
        ]
