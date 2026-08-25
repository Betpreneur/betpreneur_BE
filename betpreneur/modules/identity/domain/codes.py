"""Verification and reset codes.

Pure, so it is testable without Django and enforced framework-free by the
R5 import contract.
"""
from __future__ import annotations

import secrets

CODE_LENGTH = 6


def generate_code(length: int = CODE_LENGTH) -> str:
    """A numeric code for email verification or password reset.

    Uses secrets rather than random: these codes gate account access, and
    random's Mersenne Twister is predictable from observed output.
    """
    return "".join(secrets.choice("0123456789") for _ in range(length))
