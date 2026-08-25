import logging
from types import SimpleNamespace
from urllib.parse import parse_qs

from django.core import signing

from betpreneur.platform.config import env_int

log = logging.getLogger(__name__)

SLIP_REVIEW_STREAM_TICKET_SECONDS = env_int("SLIP_REVIEW_STREAM_TICKET_SECONDS", 30 * 60)
SLIP_REVIEW_STREAM_TICKET_SALT = "betpreneur.slip-review.stream"


def _user_for_stream_ticket(ticket):
    try:
        payload = signing.loads(
            ticket,
            salt=SLIP_REVIEW_STREAM_TICKET_SALT,
            max_age=max(60, SLIP_REVIEW_STREAM_TICKET_SECONDS),
        )
    except signing.BadSignature:
        log.warning("Websocket stream ticket authentication failed")
        return None, None

    try:
        user_id = int(payload["user_id"])
        review_id = int(payload["review_id"])
    except (KeyError, TypeError, ValueError):
        log.warning("Websocket stream ticket payload was malformed")
        return None, None

    return SimpleNamespace(id=user_id, is_authenticated=True), review_id


class JwtAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        ticket = (query.get("ticket") or [None])[0]
        if not ticket:
            await send({"type": "websocket.close", "code": 4401})
            return

        user, review_id = _user_for_stream_ticket(ticket)
        if not user or not user.is_authenticated:
            await send({"type": "websocket.close", "code": 4401})
            return

        scope["user"] = user
        scope["slip_review_stream_review_id"] = review_id
        return await self.app(scope, receive, send)


def JwtAuthMiddlewareStack(inner):
    return JwtAuthMiddleware(inner)
