"""Builds the StatPal client from Django settings.

The client itself lives in integrations/ and takes its config as an argument,
so it can be constructed in a test without Django. Reading GRIND_ALGO is a
catalog concern and lives on this side of the line.
"""
from __future__ import annotations

from django.conf import settings

from betpreneur.integrations.statpal import (
    StatPalClient,
    StatPalConfig,
    StatPalConfigurationError,
    StatPalError,
)

__all__ = [
    "StatPalClient",
    "StatPalConfig",
    "StatPalConfigurationError",
    "StatPalError",
    "statpal_client",
    "statpal_config",
]


def statpal_config() -> StatPalConfig:
    return StatPalConfig.from_mapping(getattr(settings, "GRIND_ALGO", {}) or {})


def statpal_client(session=None) -> StatPalClient:
    return StatPalClient(statpal_config(), session=session)
