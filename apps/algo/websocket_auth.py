from urllib.parse import parse_qs
import hashlib
import logging

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from .models import SlipReviewStreamToken


log = logging.getLogger(__name__)


@database_sync_to_async
def _user_for_stream_ticket(ticket):
    token_hash = hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()
    stream_token = (
        SlipReviewStreamToken.objects.select_related("user")
        .filter(token_hash=token_hash, expires_at__gt=timezone.now())
        .first()
    )
    if not stream_token:
        log.warning("Websocket stream ticket authentication failed")
        return AnonymousUser(), None
    stream_token.last_used_at = timezone.now()
    stream_token.save(update_fields=["last_used_at"])
    return stream_token.user, stream_token.review_id


class JwtAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        ticket = (query.get("ticket") or [None])[0]
        if ticket:
            user, review_id = await _user_for_stream_ticket(ticket)
            scope["user"] = user
            scope["slip_review_stream_review_id"] = review_id
        else:
            scope["user"] = AnonymousUser()
            scope["slip_review_stream_review_id"] = None
        return await self.app(scope, receive, send)


def JwtAuthMiddlewareStack(inner):
    return JwtAuthMiddleware(inner)
