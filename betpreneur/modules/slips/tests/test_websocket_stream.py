"""The slip-review websocket, end to end.

Exercises the whole chain: the HTTP endpoint that mints a ticket, the ASGI
middleware that resolves it, and the consumer that accepts the socket. None of
it runs under ENABLE_WEBSOCKETS=False, which is why it needs its own override
rather than riding on the default test settings.

The design deliberately uses a short-lived, review-scoped ticket rather than
the JWT, so the real access token never appears in a websocket URL.
"""
import asyncio

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from betpreneur.modules.slips.models import SlipReview


# CHANNEL_LAYERS is only defined when ENABLE_WEBSOCKETS is true at settings-load
# time, so flipping the flag here is not enough — the in-memory layer has to be
# supplied too. Redis is not involved.
@override_settings(
    ENABLE_WEBSOCKETS=True,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class SlipReviewWebsocketTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="wsuser", email="ws@example.com", password="x"
        )
        self.review = SlipReview.objects.create(
            user=self.user, source="manual", status=SlipReview.Status.QUEUED
        )

    def _application(self):
        # Built here, not at import time: asgi.py only wires the websocket
        # protocol when ENABLE_WEBSOCKETS is on.
        from channels.routing import ProtocolTypeRouter, URLRouter

        from betpreneur.modules.slips.interface.routing import websocket_urlpatterns
        from betpreneur.modules.slips.interface.ws_auth import JwtAuthMiddlewareStack

        return ProtocolTypeRouter(
            {"websocket": JwtAuthMiddlewareStack(URLRouter(websocket_urlpatterns))}
        )

    def _ticket(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        response = client.post(f"/api/algo/slip-reviews/{self.review.id}/stream-token/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["ticket"]

    def _connect(self, query=""):
        async def run():
            comm = WebsocketCommunicator(
                self._application(), f"/ws/slip-reviews/{self.review.id}/{query}"
            )
            accepted, _ = await comm.connect()
            frame = None
            if accepted:
                try:
                    frame = await asyncio.wait_for(comm.receive_json_from(), timeout=5)
                except TimeoutError:
                    pass
            await comm.disconnect()
            return accepted, frame

        return asyncio.run(run())

    def test_a_socket_without_a_ticket_is_rejected(self):
        accepted, _ = self._connect()
        self.assertFalse(accepted)

    def test_a_forged_ticket_is_rejected(self):
        accepted, _ = self._connect("?ticket=not-a-real-ticket")
        self.assertFalse(accepted)

    def test_a_minted_ticket_connects_and_receives_progress(self):
        accepted, frame = self._connect(f"?ticket={self._ticket()}")

        self.assertTrue(accepted)
        self.assertIsNotNone(frame, "the consumer should push an opening progress frame")
        self.assertEqual(frame["review_id"], self.review.id)
        self.assertIn("status", frame)
        self.assertIn("progress", frame)
