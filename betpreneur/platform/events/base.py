"""The base every domain event inherits from."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Something that happened, stated in the past tense.

    Events are facts, not requests. A publisher never knows who is listening
    and never depends on what a subscriber does — that is precisely what lets
    a lower module tell a higher one something without importing it.
    """

    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(UTC), compare=False
    )

    @classmethod
    def name(cls) -> str:
        """Stable dotted name, used for logging and subscription keys."""
        return f"{cls.__module__}.{cls.__qualname__}"
