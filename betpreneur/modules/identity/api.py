"""Accounts, authentication and account email — the public surface of identity.

identity sits at the bottom of the domain stack: it knows nothing about
tokens, slips or picks. Anything it needs to *cause* elsewhere it announces
through events.py instead.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from .contracts import UserRef
from .domain.codes import generate_code
from .events import UserEmailVerified, UserRegistered
from .services.verification import register_verification_contributor

__all__ = [
    "UserEmailVerified",
    "UserRef",
    "UserRegistered",
    "annotations",
    "generate_code",
    "get_user_model",
    "register_verification_contributor",
]
