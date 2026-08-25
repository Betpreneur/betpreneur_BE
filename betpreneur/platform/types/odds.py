"""Odds and probability as types, not floats.

`market["odds"]` being a bare float is how a decimal price ends up compared
against an implied probability without anyone noticing. These wrappers make
the two unmixable and put the conversions in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class OddsError(ValueError):
    """Raised when a value cannot be a valid decimal price."""


@dataclass(frozen=True, order=True)
class Probability:
    """A probability in [0, 1]."""

    value: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise OddsError(f"probability must be in [0, 1], got {self.value!r}")

    @property
    def percent(self) -> float:
        return self.value * 100.0

    def to_fair_odds(self) -> Odds:
        """The break-even decimal price for this probability."""
        if self.value == 0.0:
            raise OddsError("a zero probability has no finite fair price")
        return Odds(Decimal(1) / Decimal(str(self.value)))

    def __str__(self) -> str:
        return f"{self.percent:.1f}%"


@dataclass(frozen=True, order=True)
class Odds:
    """A decimal (European) price. 2.50 means stake 1 to return 2.50."""

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            try:
                object.__setattr__(self, "value", Decimal(str(self.value)))
            except (InvalidOperation, TypeError) as exc:
                raise OddsError(f"cannot read {self.value!r} as odds") from exc
        if self.value <= 1:
            raise OddsError(f"decimal odds must be greater than 1, got {self.value}")

    @classmethod
    def parse(cls, raw: object) -> Odds | None:
        """Best-effort read of a value from a bookmaker payload. None if unusable."""
        if raw is None or raw == "":
            return None
        try:
            return cls(Decimal(str(raw)))
        except (OddsError, InvalidOperation, TypeError):
            return None

    @property
    def implied(self) -> Probability:
        """The probability the price implies, before removing the bookmaker margin."""
        return Probability(float(Decimal(1) / self.value))

    def edge_over(self, true: Probability) -> float:
        """Expected value per unit staked, given a true probability.

        Positive means the price is better than the estimate justifies.
        """
        return float(self.value) * true.value - 1.0

    def __str__(self) -> str:
        return f"{self.value.normalize()}"
