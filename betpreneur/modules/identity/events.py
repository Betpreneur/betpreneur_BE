"""Events identity publishes.

billing subscribes to UserEmailVerified to make the signup grant, which is how
a new account gets its starting balance without identity knowing that tokens
exist.
"""
from __future__ import annotations

from dataclasses import dataclass

from betpreneur.platform.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class UserRegistered(DomainEvent):
    user_id: int
    email: str


@dataclass(frozen=True, kw_only=True)
class UserEmailVerified(DomainEvent):
    user_id: int
    email: str
