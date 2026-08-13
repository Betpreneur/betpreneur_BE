from urllib.parse import parse_qs
import logging

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication


log = logging.getLogger(__name__)


@database_sync_to_async
def _user_for_token(token):
    try:
        auth = JWTAuthentication()
        validated_token = auth.get_validated_token(token)
        return auth.get_user(validated_token)
    except Exception as exc:
        log.warning("Websocket JWT authentication failed: %s", exc)
        return AnonymousUser()


class JwtAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        token = (query.get("token") or [None])[0]
        scope["user"] = await _user_for_token(token) if token else AnonymousUser()
        return await self.app(scope, receive, send)


def JwtAuthMiddlewareStack(inner):
    return JwtAuthMiddleware(inner)
