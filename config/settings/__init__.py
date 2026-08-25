"""Environment-selected settings.

DJANGO_SETTINGS_MODULE stays "config.settings" everywhere (manage.py, wsgi,
asgi and all eleven docker-compose services). Which environment loads is
chosen by DJANGO_ENV:

    DJANGO_ENV=local        (default)
    DJANGO_ENV=test
    DJANGO_ENV=docker
    DJANGO_ENV=production

Import the concrete module directly if you prefer -- config.settings.test
works too, and is what CI uses.
"""
import importlib
import os

_ENV = os.environ.get("DJANGO_ENV", "local").strip().lower()
_ALLOWED = {"local", "test", "docker", "production"}

if _ENV not in _ALLOWED:
    raise ValueError(
        f"DJANGO_ENV={_ENV!r} is not one of {sorted(_ALLOWED)}"
    )

_module = importlib.import_module(f"config.settings.{_ENV}")

# Re-export every setting (uppercase names only, as Django requires).
globals().update({k: v for k, v in vars(_module).items() if k.isupper()})

SETTINGS_ENV = _ENV
