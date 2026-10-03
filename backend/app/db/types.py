"""Shared value types (Money, Quantity) and the base model mixin."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from app.db.currencies import exponent


class CurrencyMismatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Money:
    """Exact amount in integer minor units."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        exponent(self.currency)  # raises on unknown codes
        if not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an int")

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @classmethod
    def from_decimal(cls, value: Decimal | str | int, currency: str) -> Money:
        exp = exponent(currency)
        quantum = Decimal(1).scaleb(-exp)
        minor = (Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP) * (10**exp)).to_integral_value()
        return cls(int(minor), currency)

    @property
    def decimals(self) -> int:
        return exponent(self.currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.decimals)

    def to_display(self, locale: str = "en") -> str:
        text = f"{self.to_decimal():,.{self.decimals}f}"
        return f"{self.currency} {text}" if locale == "en" else f"{text} {self.currency}"

    def to_json(self) -> dict[str, Any]:
        return {
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "decimals": self.decimals,
            "display": self.to_display(),
        }

    def _check(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError("can only combine Money with Money")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor >= other.amount_minor

    def times(self, factor: Decimal | int) -> Money:
        """Multiply, rounding half-up."""
        value = (Decimal(self.amount_minor) * Decimal(str(factor))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return Money(int(value), self.currency)

    def percent(self, rate_percent: Decimal | int) -> Money:
        return self.times(Decimal(str(rate_percent)) / Decimal(100))


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal
    unit: str


class Base(DeclarativeBase):
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _clock_now() -> datetime:
    # lazy import to avoid a cycle
    from app.core.clock import clock_now

    return clock_now()


class TenantMixin:
    """id, business_id and business-clock timestamps."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)

    @declared_attr
    def business_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(Uuid, ForeignKey("business.id"), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now, onupdate=_clock_now)


def money_col(prefix: str) -> Any:
    """`<prefix>_minor` + `<prefix>_currency`. Not nullable, as NULLs load as Money(None, None); use 0."""
    from sqlalchemy import BigInteger
    from sqlalchemy.orm import composite

    return composite(
        Money,
        mapped_column(f"{prefix}_minor", BigInteger, nullable=False, default=0),
        mapped_column(f"{prefix}_currency", String(3), nullable=False),
    )
