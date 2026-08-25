"""Environment for the legacy grindalgo runner.

The runner reads its whole configuration from os.environ. Assembling that from
GRIND_ALGO settings is plain config work, and it sits here — below both the
fixture search that fetches with it and the daily run that scores with it — so
neither has to reach across to the other for it.
"""
from __future__ import annotations

from django.conf import settings


def runner_env(extra: dict | None = None) -> dict:
    """GRIND_ALGO settings as an environment mapping, minus empty values."""
    configured = getattr(settings, "GRIND_ALGO", {}) or {}
    env = {key: value for key, value in configured.items() if value not in (None, "")}
    if "APS_KEY" in env and "API_SPORTS_KEY" not in env:
        env["API_SPORTS_KEY"] = env["APS_KEY"]
    if extra:
        env.update(extra)
    return env


def runner_env_int(name: str, default: int) -> int:
    try:
        return int(runner_env().get(name, default))
    except (TypeError, ValueError):
        return default


def runner_env_bool(name: str, default: bool = False) -> bool:
    value = runner_env().get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
