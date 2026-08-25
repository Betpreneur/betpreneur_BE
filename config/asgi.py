import os

from django.conf import settings
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_asgi_app = get_asgi_application()

if getattr(settings, "ENABLE_WEBSOCKETS", False):
    from channels.routing import ProtocolTypeRouter, URLRouter

    # config is the composition root: it wires modules together, so it reaches
    # interface layers directly the same way urls.py does. Routing these through
    # slips.api would make every consumer of that facade require `channels`.
    from betpreneur.modules.slips.interface.routing import websocket_urlpatterns
    from betpreneur.modules.slips.interface.ws_auth import JwtAuthMiddlewareStack

    application = ProtocolTypeRouter(
        {
            "http": django_asgi_app,
            "websocket": JwtAuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
        }
    )
else:
    application = django_asgi_app
