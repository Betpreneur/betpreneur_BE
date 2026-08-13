import os

from django.conf import settings
from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_asgi_app = get_asgi_application()

if getattr(settings, "ENABLE_WEBSOCKETS", False):
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.security.websocket import AllowedHostsOriginValidator

    from apps.algo.routing import websocket_urlpatterns
    from apps.algo.websocket_auth import JwtAuthMiddlewareStack

    application = ProtocolTypeRouter(
        {
            "http": django_asgi_app,
            "websocket": AllowedHostsOriginValidator(
                JwtAuthMiddlewareStack(URLRouter(websocket_urlpatterns))
            ),
        }
    )
else:
    application = django_asgi_app
