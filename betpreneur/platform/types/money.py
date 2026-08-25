"""Minor-unit money. Never a float, never a bare int with an implied currency."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class CurrencyMismatch(ValueError):
    """Raised when two amounts in different currencies are combined."""


@dataclass(frozen=True, order=True)
class Money:
    """An amount held in the currency's minor unit (kobo for NGN, cents for USD)."""

    minor: int
    currency: str = "NGN"

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int):
            raise TypeError(f"minor units must be int, got {type(self.minor).__name__}")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def from_major(cls, amount: Decimal | str | int, currency: str = "NGN") -> Money:
        """Build from a major-unit amount — Money.from_major('990.50') is 99050 kobo."""
        return cls(int((Decimal(str(amount)) * 100).to_integral_value()), currency)

    @property
    def major(self) -> Decimal:
        return Decimal(self.minor) / 100

    def _same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __str__(self) -> str:
        return f"{self.currency} {self.major:,.2f}"
