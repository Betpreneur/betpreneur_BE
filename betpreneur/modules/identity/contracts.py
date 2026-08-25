"""Types crossing the identity boundary."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserRef:
    """A user, as every other module should see one.

    Deliberately not the User model: a module that needs an email and an id
    should not be able to reach through to password hashes or save().
    """

    id: int
    email: str
    username: str
    is_email_verified: bool
