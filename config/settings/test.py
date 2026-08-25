"""Test settings: no network, no broker, no real crypto.

Everything slow or external is stubbed here rather than in individual tests,
so a test that forgets to patch still cannot reach the outside world.
"""
from .base import *

DEBUG = False
ALLOWED_HOSTS = ["*"]

# Celery runs inline; no broker required.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Events fire immediately -- TestCase wraps each test in a transaction that
# never commits, so on_commit callbacks would otherwise never run.
EVENT_BUS_IMMEDIATE = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Websocket tests need Channels wired at settings-load time. Keeping the test
# layer in memory avoids Redis while matching the production ASGI shape.
ENABLE_WEBSOCKETS = True
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
SLIP_REVIEW_REDIS_PROGRESS_ENABLED = False

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "betpreneur-test",
    }
}

# Nothing may reach a real provider.
RESEND_API_KEY = ""
PAYFONTE_CLIENT_ID = ""
PAYFONTE_CLIENT_SECRET = ""
